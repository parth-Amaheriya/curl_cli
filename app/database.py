"""Simple in-memory database for users (replace with real DB in production)"""
import uuid
import threading
from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

# Initialize MongoDB client (connection pooling handled automatically)
client = AsyncIOMotorClient(settings.mongodb_url)
db = client[settings.mongodb_db_name]

from .security import get_password_hash, verify_password

# Thread lock for concurrent dev requests
_db_lock = threading.Lock()

# ============ MODELS ============

class UserBase(BaseModel):
    """Base user model"""
    username: str = Field(..., min_length=3, max_length=50, pattern=r'^[a-zA-Z0-9_-]+$')
    email: EmailStr


class UserCreate(UserBase):
    """User creation request model"""
    password: str = Field(..., min_length=8)


class UserInDB(UserBase):
    """User model as stored in database"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    hashed_password: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    scopes: List[str] = Field(default_factory=list)
    
    def verify_password(self, plain_password: str) -> bool:
        """Verify password against stored hash"""
        return verify_password(plain_password, self.hashed_password)


# ============ IN-MEMORY STORAGE ============
# ⚠️ REPLACE WITH POSTGRESQL/MONGODB IN PRODUCTION
_users_db: dict[str, UserInDB] = {}
_username_index: dict[str, str] = {}
_email_index: dict[str, str] = {}


# ============ CRUD OPERATIONS ============

def get_user_by_id(user_id: str) -> Optional[UserInDB]:
    """Get user by ID"""
    return _users_db.get(user_id)


def get_user_by_username(username: str) -> Optional[UserInDB]:
    """Get user by username (case-insensitive)"""
    user_id = _username_index.get(username.lower())
    return _users_db.get(user_id) if user_id else None


def get_user_by_email(email: str) -> Optional[UserInDB]:
    """Get user by email (case-insensitive)"""
    user_id = _email_index.get(email.lower())
    return _users_db.get(user_id) if user_id else None


def create_user(user_data: UserCreate) -> UserInDB:
    """Create a new user account"""
    with _db_lock:
        # Check for duplicates
        if get_user_by_username(user_data.username):
            raise ValueError("Username already registered")
        if get_user_by_email(user_data.email):
            raise ValueError("Email already registered")
        
        # Create user instance
        user = UserInDB(
            username=user_data.username,
            email=user_data.email,
            hashed_password=get_password_hash(user_data.password),
            scopes=["convert:curl"]  # Default scope for new users
        )
        
        # Store in indexes
        _users_db[user.id] = user
        _username_index[user.username.lower()] = user.id
        _email_index[user.email.lower()] = user.id
        
        return user


def authenticate_user(username: str, password: str) -> Optional[UserInDB]:
    """Authenticate user with username & password"""
    user = get_user_by_username(username)
    if not user:
        return None
    if not user.verify_password(password):
        return None
    if not user.is_active:
        return None
    return user


def update_user_scopes(user_id: str, scopes: List[str]) -> Optional[UserInDB]:
    """Update user permissions/scopes"""
    with _db_lock:
        user = get_user_by_id(user_id)
        if user:
            user.scopes = scopes
            # Pydantic v2 models are mutable in memory
            _users_db[user_id] = user
        return user


def delete_user(user_id: str) -> bool:
    """Delete user (soft or hard)"""
    with _db_lock:
        user = _users_db.pop(user_id, None)
        if user:
            _username_index.pop(user.username.lower(), None)
            _email_index.pop(user.email.lower(), None)
            return True
        return False


def initialize_db():
    """Initialize database with default test users (development only)"""
    if not get_user_by_username("admin"):
        try:
            create_user(UserCreate(
                username="admin",
                email="admin@example.com",
                password="admin123"  # 🔒 CHANGE IN PRODUCTION!
            ))
            admin = get_user_by_username("admin")
            if admin:
                update_user_scopes(admin.id, ["convert:curl", "admin:all"])
            print("✅ Created test admin user: admin / admin123")
        except ValueError as e:
            print(f"⚠️ Could not create admin: {e}")
    else:
        print("✅ Database already initialized")


async def get_db():
    """FastAPI dependency to yield MongoDB database instance"""
    try:
        yield db
    finally:
        pass  # Motor manages connections; no need to close per-request


def close_db():
    """Close MongoDB client on application shutdown"""
    client.close()