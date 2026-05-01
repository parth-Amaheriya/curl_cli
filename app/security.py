"""Security utilities: JWT tokens, password hashing"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, List
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from .config import settings

# Password hashing
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)
# JWT bearer scheme
security = HTTPBearer(auto_error=False)


class Token(BaseModel):
    """JWT token response"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    """Decoded JWT token data"""
    user_id: str
    username: str
    email: Optional[str] = None
    scopes: List[str] = []


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password for storage"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(
        to_encode, 
        settings.jwt_secret_key, 
        algorithm=settings.jwt_algorithm
    )


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT refresh token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.refresh_token_expire_minutes)
    )
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(
        to_encode, 
        settings.jwt_secret_key, 
        algorithm=settings.jwt_algorithm
    )


def decode_token(token: str, token_type: str = "access") -> TokenData:
    """Decode and validate a JWT token"""
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret_key, 
            algorithms=[settings.jwt_algorithm]
        )
        
        # Verify token type matches expected
        if payload.get("type") != token_type:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token type. Expected '{token_type}'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # Verify expiration
        exp = payload.get("exp")
        if exp and datetime.fromtimestamp(exp, tz=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # Extract user info
        user_id = payload.get("sub")
        username = payload.get("username")
        
        if user_id is None or username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        return TokenData(
            user_id=user_id,
            username=username,
            email=payload.get("email"),
            scopes=payload.get("scopes", [])
        )
        
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> TokenData:
    """Dependency to get current authenticated user from JWT"""
    if credentials is None or credentials.credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return decode_token(credentials.credentials, token_type="access")


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[TokenData]:
    """Dependency to get current user if authenticated, None otherwise"""
    if credentials is None or credentials.credentials is None:
        return None
    
    try:
        return decode_token(credentials.credentials, token_type="access")
    except HTTPException:
        return None


async def get_current_active_user(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Dependency to get current active user"""
    # TODO: Add database check for user.is_active status in production
    # from .database import get_user_by_id
    # user = get_user_by_id(current_user.user_id)
    # if not user or not user.is_active:
    #     raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def require_scope(required_scope: str):
    """
    Dependency factory to require specific OAuth2 scope.
    
    Usage:
        @app.get("/admin", dependencies=[Depends(require_scope("admin:all"))])
        async def admin_endpoint():
            ...
    """
    async def scope_checker(
        current_user: TokenData = Depends(get_current_active_user)
    ) -> TokenData:
        if required_scope not in current_user.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required scope: '{required_scope}'. Your scopes: {current_user.scopes}",
            )
        return current_user
    
    return scope_checker


def require_any_scope(required_scopes: List[str]):
    """
    Dependency factory to require at least one of multiple scopes.
    
    Usage:
        @app.get("/resource", dependencies=[Depends(require_any_scope(["read:own", "read:all"]))])
        async def read_resource():
            ...
    """
    async def scope_checker(
        current_user: TokenData = Depends(get_current_active_user)
    ) -> TokenData:
        if not any(scope in current_user.scopes for scope in required_scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required one of: {required_scopes}. Your scopes: {current_user.scopes}",
            )
        return current_user
    
    return scope_checker


def get_token_from_request(authorization: Optional[str] = None) -> Optional[str]:
    """Extract bearer token from Authorization header string"""
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:]  # Remove "Bearer " prefix
    return None


def create_password_reset_token(email: str) -> str:
    """Create a short-lived token for password reset (2 hours)"""
    data = {"sub": email, "type": "password_reset"}
    return create_access_token(data, expires_delta=timedelta(hours=2))


def verify_password_reset_token(token: str) -> Optional[str]:
    """Verify password reset token and return email if valid"""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "password_reset":
            return None
        exp = payload.get("exp")
        if exp and datetime.fromtimestamp(exp, tz=timezone.utc) < datetime.now(timezone.utc):
            return None
        return payload.get("sub")  # Returns email
    except JWTError:
        return None