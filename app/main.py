import logging
from contextlib import asynccontextmanager
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
from app.repositories.user_repo import create_user, authenticate_user, get_user_by_id, update_user_scopes
from app.repositories.conversion_repo import save_conversion, get_user_conversions

from app.security import (
    create_access_token, create_refresh_token, decode_token,
    get_current_active_user, require_scope, security, get_current_user_optional
)
from app.models import (
    UserRegister, UserLogin, UserResponse, Token, TokenRefresh,
    ConvertRequest, ConversionResponse, ConversionHistoryCreate, ConversionHistory, UserCreate, CurlRequest
)
from app.converter import convert_curls

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 %s v%s starting...", settings.app_name, settings.app_version)
    
    try:
        # 1. Ping MongoDB to verify connection
        await db.command("ping")
        logger.info("✅ MongoDB connection successful")
        
        # 2. Create indexes for performance & uniqueness
        await db.users.create_index("username", unique=True)
        await db.users.create_index("email", unique=True)
        await db.conversions.create_index([("user_id", 1), ("created_at", -1)])
        logger.info("✅ Database indexes created")
        
    except Exception as e:
        logger.error(f"❌ MongoDB startup failed: {e}")
        raise RuntimeError(f"Failed to connect to MongoDB: {e}") from e
        
    yield  # Server runs here
    
    # 3. Shutdown cleanup
    close_db()
    logger.info("🛑 MongoDB client closed. Shutting down...")


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title=settings.app_name,
    description="JWT-protected curl-to-python converter with MongoDB history",
    version=settings.app_version,
    lifespan=lifespan,  # ✅ Properly attached
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
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
    user_create: UserCreate,  # ✅ Renamed from 'user_' to 'user_create'
    db = Depends(get_db)
):
    """Register a new user account"""
    try:
        # ✅ Use the correct variable name
        user = await create_user(db, user_create)
        logger.info(f"✅ New user registered: {user.username}")
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
    credentials: UserLogin,  # ✅ Clear variable name
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
    
    logger.info(f"🔐 User logged in: {user.username}")
    
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60
    )


@app.post("/api/v1/auth/refresh", response_model=Token, tags=["Authentication"])
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def refresh_token_endpoint(  # ✅ Renamed to avoid conflict with function
    request: Request, 
    token_request: TokenRefresh,  # ✅ Clear variable name
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
            function_name_prefix=convert_req.function_name_prefix
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