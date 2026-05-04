import logging
import ast
import hashlib
import json
import secrets
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, status, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from typing import List, Optional
from app.config import settings
from app.security import TokenData

from app.database import db, close_db, get_db
from app.repositories.user_repo import (
    authenticate_user,
    create_or_update_google_user,
    create_user,
    get_user_by_email,
    get_user_by_id,
    update_password_by_email,
    update_user_scopes,
)
from app.repositories.conversion_repo import save_conversion, get_user_conversions

from app.security import (
    create_access_token, create_refresh_token, decode_token,
    get_current_active_user, require_scope, security, get_current_user_optional
)
from app.models import (
    UserRegister, UserLogin, UserResponse, Token, TokenRefresh,
    ConvertRequest, ConversionResponse, ConversionHistoryCreate, ConversionHistory, UserCreate, CurlRequest,
    ForgotPasswordRequest, GoogleLoginRequest, MessageResponse, ResetPasswordRequest,
    RunWorkspaceRequest, RunWorkspaceResponse, UserWorkspaceState
)
from app.converter import convert_curls, normalize_proxy_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def _format_response_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    return f"{byte_count / 1024:.1f} KB"


def _response_file_name(workspace_name: str, extension: str) -> str:
    normalized = extension.lstrip(".")
    return f"{workspace_name}_response.{normalized}"


def _token_response_for_user(user) -> Token:
    token_data = {
        "sub": user.id,
        "username": user.username,
        "email": user.email,
        "scopes": user.scopes,
    }
    return Token(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
    )


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_google_id_token(credential: str) -> dict:
    if not settings.google_client_id:
        raise HTTPException(status_code=500, detail="Google login is not configured")

    query = urllib.parse.urlencode({"id_token": credential})
    url = f"https://oauth2.googleapis.com/tokeninfo?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Google credential") from exc

    if payload.get("aud") != settings.google_client_id:
        raise HTTPException(status_code=401, detail="Invalid Google audience")
    if payload.get("email_verified") not in ("true", True):
        raise HTTPException(status_code=401, detail="Google email is not verified")
    return payload


def _find_request_function_name(request_code: str) -> Optional[str]:
    try:
        tree = ast.parse(request_code)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "do_requests":
            return node.name
    return None


def _ensure_parser_functions(request_code: str, parser_code: str) -> str:
    try:
        request_tree = ast.parse(request_code)
    except SyntaxError:
        return parser_code

    parser_names = set()
    try:
        parser_tree = ast.parse(parser_code or "")
        parser_names = {
            node.name
            for node in parser_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    except SyntaxError:
        pass

    required_names = set()
    for node in ast.walk(request_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("_parser"):
            required_names.add(node.func.id)

    missing_names = sorted(required_names - parser_names)
    if not missing_names:
        return parser_code

    fallback_lines = [parser_code.rstrip(), ""]
    for name in missing_names:
        fallback_lines.extend([
            f"def {name}(response):",
            "    content_type = response.headers.get('content-type', '')",
            "    if 'application/json' in content_type.lower():",
            "        return response.json()",
            "    return getattr(response, 'text', str(response))",
            "",
        ])
    return "\n".join(fallback_lines).lstrip()


def _run_workspace_code(payload: RunWorkspaceRequest) -> RunWorkspaceResponse:
    function_name = _find_request_function_name(payload.request_code)
    if not function_name:
        return RunWorkspaceResponse(
            success=False,
            workspace_name=payload.workspace_name,
            extension="json",
            response_file_name=_response_file_name(payload.workspace_name, "json"),
            file_name=_response_file_name(payload.workspace_name, "json"),
            logs="No runnable request function found in request.py",
            error="Execution failed",
        )

    proxy_json = json.dumps(normalize_proxy_config(payload.proxy))

    runner_code = textwrap.dedent(
        """
        import importlib.util
        import json
        import pathlib
        import sys
        import traceback

        function_name = sys.argv[1]
        output_path = pathlib.Path(sys.argv[2])
        proxy_config = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        request_meta = {
            "status": None,
            "size": 0,
            "content_type": "",
            "extension": "json",
            "response": None,
        }

        try:
            from curl_cffi import requests as curl_requests
            original_request = curl_requests.request

            def tracked_request(*args, **kwargs):
                if proxy_config and not kwargs.get("proxies"):
                    kwargs["proxies"] = proxy_config
                response = original_request(*args, **kwargs)
                request_meta["status"] = getattr(response, "status_code", None)
                content_type = getattr(response, "headers", {}).get("content-type", "") or ""
                request_meta["content_type"] = content_type
                request_meta["extension"] = "json" if "application/json" in content_type.lower() else "txt"
                content = getattr(response, "content", None)
                if content is not None:
                    request_meta["size"] = len(content)
                else:
                    request_meta["size"] = len(getattr(response, "text", "") or "")
                if request_meta["extension"] == "json":
                    try:
                        request_meta["response"] = response.json()
                    except Exception:
                        request_meta["response"] = getattr(response, "text", "")
                else:
                    request_meta["response"] = getattr(response, "text", "")
                return response

            curl_requests.request = tracked_request
        except Exception:
            pass

        def normalize(value):
            if value is None or isinstance(value, (str, int, float, bool, list, dict)):
                return value
            if hasattr(value, "json"):
                try:
                    return value.json()
                except Exception:
                    pass
            if hasattr(value, "text"):
                return value.text
            return str(value)

        try:
            spec = importlib.util.spec_from_file_location("workspace_request", "request.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for attr_name in dir(module):
                if not attr_name.endswith("_parser"):
                    continue
                parser_fn = getattr(module, attr_name)
                if not callable(parser_fn):
                    continue

                def safe_parser(response, _parser_fn=parser_fn):
                    try:
                        return _parser_fn(response)
                    except Exception:
                        content_type = response.headers.get("content-type", "")
                        if "application/json" in content_type.lower():
                            return response.json()
                        return getattr(response, "text", str(response))

                setattr(module, attr_name, safe_parser)
            result = getattr(module, function_name)()
            status = request_meta["status"]
            completed = status is None or int(status) == 200
            output_path.write_text(json.dumps({
                "success": completed,
                "status": request_meta["status"],
                "size_bytes": request_meta["size"],
                "content_type": request_meta["content_type"],
                "extension": request_meta["extension"],
                "response": request_meta["response"] if request_meta["response"] is not None else normalize(result),
                "parsed": None,
                "logs": "Request completed successfully" if completed else f"Request failed with status {status}",
                "error": None if completed else "Execution failed",
            }, default=str), encoding="utf-8")
        except Exception as exc:
            output_path.write_text(json.dumps({
                "success": False,
                "status": request_meta["status"],
                "size_bytes": request_meta["size"],
                "content_type": request_meta["content_type"],
                "extension": request_meta["extension"],
                "response": request_meta["response"],
                "parsed": None,
                "logs": traceback.format_exc(),
                "error": str(exc) or "Execution failed",
            }), encoding="utf-8")
            raise
        """
    )

    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="curl2py-run-") as tmpdir:
        from pathlib import Path

        tmp_path = Path(tmpdir)
        (tmp_path / "request.py").write_text(payload.request_code, encoding="utf-8")
        parser_code = _ensure_parser_functions(payload.request_code, payload.parser_code)
        (tmp_path / "parser.py").write_text(parser_code, encoding="utf-8")
        (tmp_path / "_runner.py").write_text(runner_code, encoding="utf-8")
        result_path = tmp_path / "_result.json"

        completed = subprocess.run(
            [sys.executable, "_runner.py", function_name, str(result_path), proxy_json],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        time_ms = int((time.perf_counter() - start) * 1000)
        output = {}
        if result_path.exists():
            output = json.loads(result_path.read_text(encoding="utf-8"))

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        run_logs = "\n".join(part for part in [output.get("logs"), stdout, stderr] if part)
        size_bytes = int(output.get("size_bytes") or 0)
        success = bool(output.get("success")) and completed.returncode == 0
        extension = (output.get("extension") or "json").lstrip(".")
        response_file_name = _response_file_name(payload.workspace_name, extension)

        return RunWorkspaceResponse(
            success=success,
            workspace_name=payload.workspace_name,
            status=output.get("status"),
            time_ms=time_ms,
            size=_format_response_size(size_bytes),
            content_type=output.get("content_type") or "",
            extension=extension,
            response_file_name=response_file_name,
            file_name=response_file_name,
            response=output.get("response"),
            parsed=output.get("parsed"),
            logs=run_logs or ("Request completed successfully" if success else "Execution failed"),
            error=None if success else output.get("error") or "Execution failed",
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s v%s starting...", settings.app_name, settings.app_version)
    
    try:
        # 1. Ping MongoDB to verify connection
        await db.command("ping")
        logger.info("MongoDB connection successful")
        
        # 2. Create indexes for performance & uniqueness
        await db.users.create_index("username", unique=True)
        await db.users.create_index("email", unique=True)
        await db.users.create_index("google_id", unique=True, sparse=True)
        await db.conversions.create_index([("user_id", 1), ("created_at", -1)])
        await db.user_projects.create_index("user_id", unique=True)
        await db.password_resets.create_index("expires_at", expireAfterSeconds=0)
        logger.info("Database indexes created")
        
    except Exception as e:
        logger.error(f" MongoDB startup failed: {e}")
        raise RuntimeError(f"Failed to connect to MongoDB: {e}") from e
        
    yield  # Server runs here
    
    # 3. Shutdown cleanup
    close_db()
    logger.info("MongoDB client closed. Shutting down...")


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title=settings.app_name,
    description="JWT-protected curl-to-python converter with MongoDB history",
    version=settings.app_version,
    lifespan=lifespan,  # Properly attached
    docs_url="/curl2py_docs", 

)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.router.lifespan_context = lifespan
app.state.db = None  # Will be set in lifespan

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

## ============ AUTH ENDPOINTS ============

@app.post("/api/v1/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED, tags=["Authentication"])
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def register(
    request: Request, 
    user_create: UserCreate,  # Renamed from 'user_' to 'user_create'
    db = Depends(get_db)
):
    """Register a new user account"""
    try:
        # Use the correct variable name
        user = await create_user(db, user_create)
        logger.info(f"New user registered: {user.username}")
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            scopes=user.scopes,
            created_at=user.created_at
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Registration error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@app.post("/api/v1/auth/login", response_model=Token, tags=["Authentication"])
@limiter.limit(f"{settings.rate_limit_per_minute * 2}/minute")
async def login(
    request: Request, 
    credentials: UserLogin,  #  Clear variable name
    db = Depends(get_db)
):
    """Login and receive JWT tokens"""
    user = await authenticate_user(db, credentials.username, credentials.password)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create tokens
    token_data = {
        "sub": user.id, 
        "username": user.username, 
        "email": user.email, 
        "scopes": user.scopes
    }
    
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)
    
    logger.info(f" User logged in: {user.username}")
    
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60
    )


@app.post("/api/v1/auth/google", response_model=Token, tags=["Authentication"])
async def google_login(
    payload: GoogleLoginRequest,
    db = Depends(get_db)
):
    google_payload = _verify_google_id_token(payload.credential)
    user = await create_or_update_google_user(
        db,
        email=google_payload["email"],
        name=google_payload.get("name") or google_payload["email"].split("@")[0],
        google_id=google_payload["sub"],
        avatar_url=google_payload.get("picture"),
    )
    return _token_response_for_user(user)


@app.post("/api/v1/auth/forgot-password", response_model=MessageResponse, tags=["Authentication"])
async def forgot_password(
    payload: ForgotPasswordRequest,
    db = Depends(get_db)
):
    user = await get_user_by_email(db, payload.email)
    if user:
        token = secrets.token_urlsafe(32)
        await db.password_resets.insert_one({
            "user_id": user.id,
            "email": payload.email,
            "token_hash": _hash_reset_token(token),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=2),
            "used_at": None,
            "created_at": datetime.now(timezone.utc),
        })
        logger.info("Password reset requested for %s", payload.email)
    return MessageResponse(message="If an account exists for that email, password reset instructions will be sent.")


@app.post("/api/v1/auth/reset-password", response_model=MessageResponse, tags=["Authentication"])
async def reset_password(
    payload: ResetPasswordRequest,
    db = Depends(get_db)
):
    reset_doc = await db.password_resets.find_one({
        "token_hash": _hash_reset_token(payload.token),
        "used_at": None,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not reset_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    await update_password_by_email(db, reset_doc["email"], payload.password)
    await db.password_resets.update_one(
        {"_id": reset_doc["_id"]},
        {"$set": {"used_at": datetime.now(timezone.utc)}}
    )
    return MessageResponse(message="Password has been reset.")


@app.post("/api/v1/auth/refresh", response_model=Token, tags=["Authentication"])
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def refresh_token_endpoint(  # Renamed to avoid conflict with function
    request: Request, 
    token_request: TokenRefresh,  # Clear variable name
    db = Depends(get_db)
):
    """Refresh access token using refresh token"""
    try:
        payload = decode_token(token_request.refresh_token, token_type="refresh")
        user = await get_user_by_id(db, payload.user_id)
        
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        new_token_data = {
            "sub": user.id, 
            "username": user.username, 
            "email": user.email, 
            "scopes": user.scopes
        }
        
        return Token(
            access_token=create_access_token(new_token_data),
            refresh_token=create_refresh_token(new_token_data),
            expires_in=settings.access_token_expire_minutes * 60
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/api/v1/auth/me", response_model=UserResponse, tags=["Authentication"])
async def get_me(
    current_user: TokenData = Depends(get_current_active_user),
    db = Depends(get_db)
):
    """Get current authenticated user info"""
    user = await get_user_by_id(db, current_user.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        scopes=user.scopes,
        created_at=user.created_at
    )


@app.get("/api/v1/workspace", response_model=UserWorkspaceState, tags=["Workspace"])
async def get_workspace_state(
    current_user: TokenData = Depends(get_current_active_user),
    db = Depends(get_db)
):
    doc = await db.user_projects.find_one({"user_id": current_user.user_id})
    if not doc:
        return UserWorkspaceState(collections={}, activeCollectionId=None, theme="dark")
    return UserWorkspaceState(
        collections=doc.get("collections") or {},
        activeCollectionId=doc.get("activeCollectionId"),
        theme=doc.get("theme") or "dark",
        openResponseTabs=doc.get("openResponseTabs") or [],
        activeResponseTabId=doc.get("activeResponseTabId"),
        updatedAt=doc.get("updated_at"),
    )


@app.put("/api/v1/workspace", response_model=UserWorkspaceState, tags=["Workspace"])
async def save_workspace_state(
    workspace: UserWorkspaceState,
    current_user: TokenData = Depends(get_current_active_user),
    db = Depends(get_db)
):
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": current_user.user_id,
        "collections": workspace.collections,
        "activeCollectionId": workspace.activeCollectionId,
        "theme": workspace.theme,
        "openResponseTabs": workspace.openResponseTabs,
        "activeResponseTabId": workspace.activeResponseTabId,
        "updated_at": now,
    }
    await db.user_projects.update_one(
        {"user_id": current_user.user_id},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return UserWorkspaceState(**{**workspace.model_dump(), "updatedAt": now})
# ============ PUBLIC CONVERSION ENDPOINT ============
@app.post("/api/v1/convert", response_model=ConversionResponse, tags=["Conversion"])
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def convert(
    request: Request,
    convert_req: ConvertRequest,
    current_user: Optional[TokenData] = Depends(get_current_user_optional),
    db = Depends(get_db)
):
    """
    Converts a cURL command to a Python script.

    - **Public Endpoint**: No authentication required for conversion.
    - **Authenticated Users**: If a valid JWT is provided, the conversion will be saved to the user's history.
    - **Rate Limited**: To prevent abuse, this endpoint is rate-limited.
    """
    try:
        commands = convert_req.get_commands()
        result = convert_curls(
            input_data=commands if convert_req.is_batch() else commands[0],
            function_name=convert_req.curl.function_name if not convert_req.is_batch() and isinstance(convert_req.curl, CurlRequest) else None,
            function_name_prefix=convert_req.function_name_prefix,
            proxy=convert_req.proxy,
        )
        
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Conversion failed"))
        
        # Save to history ONLY if the user is authenticated
        if current_user:
            history_data = ConversionHistoryCreate(
                user_id=current_user.user_id,
                curl_command=commands[0]["curl"] if not convert_req.is_batch() else "; ".join([c.get("curl","") for c in commands]),
                python_code=result.get("python_code"),
                parser_code=result.get("parser_script"),
                function_names=result.get("function_names", []),
                status="success",
                request_type="batch" if convert_req.is_batch() else "single"
            )
            await save_conversion(db, history_data)
        
        if result.get("is_batch"):
            return ConversionResponse(success=True, request_script=result["request_script"], parser_script=result["parser_script"], function_names=result["function_names"])
        return ConversionResponse(success=True, python_code=result["python_code"], parser_code=result["parser_code"], function_name=result["function_name"])
    except HTTPException: 
        raise
    except Exception as e:
        logger.error(f"Conversion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

@app.post("/run-workspace", response_model=RunWorkspaceResponse, tags=["Execution"])
async def run_workspace(run_req: RunWorkspaceRequest):
    """Execute a single active workspace request through the backend."""
    try:
        return _run_workspace_code(run_req)
    except subprocess.TimeoutExpired:
        return RunWorkspaceResponse(
            success=False,
            workspace_name=run_req.workspace_name,
            status=None,
            time_ms=30000,
            size="0 KB",
            extension="json",
            response_file_name=_response_file_name(run_req.workspace_name, "json"),
            file_name=_response_file_name(run_req.workspace_name, "json"),
            response=None,
            parsed=None,
            logs="Execution timed out after 30 seconds",
            error="Execution failed",
        )
    except Exception as e:
        logger.error(f"Workspace execution error: {e}", exc_info=True)
        return RunWorkspaceResponse(
            success=False,
            workspace_name=run_req.workspace_name,
            status=None,
            time_ms=0,
            size="0 KB",
            extension="json",
            response_file_name=_response_file_name(run_req.workspace_name, "json"),
            file_name=_response_file_name(run_req.workspace_name, "json"),
            response=None,
            parsed=None,
            logs=str(e),
            error="Execution failed",
        )

# ============ HISTORY ENDPOINTS ============
@app.get("/api/v1/history", response_model=List[ConversionHistory], tags=["History"])
async def get_history(
    request: Request,
    skip: int = 0,
    limit: int = 20,
    current_user = Depends(get_current_active_user),
    db = Depends(get_db)
):
    histories, total = await get_user_conversions(db, current_user.user_id, skip, limit)
    return histories

@app.delete("/api/v1/history/{conv_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["History"])
async def delete_history(
    request: Request,
    conv_id: str,
    current_user = Depends(get_current_active_user),
    db = Depends(get_db)
):
    from app.repositories.conversion_repo import delete_conversion
    success = await delete_conversion(db, conv_id, current_user.user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Conversion not found or unauthorized")

# ============ PUBLIC ============
@app.get("/health", tags=["Health"])
async def health(request: Request):
    return {"status": "healthy", "version": settings.app_version, "db": "mongodb"}

@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"error": "Endpoint not found"})
@app.exception_handler(500)
async def server_error(request, exc):
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
