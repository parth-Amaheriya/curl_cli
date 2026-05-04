"""Async MongoDB user repository"""
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.security import get_password_hash, verify_password
from app.models import UserInDB, UserCreate


async def get_user_by_id(db: AsyncIOMotorDatabase, user_id: str) -> Optional[UserInDB]:
    doc = await db.users.find_one({"_id": ObjectId(user_id)})
    return UserInDB(**doc) if doc else None


async def get_user_by_username(db: AsyncIOMotorDatabase, username: str) -> Optional[UserInDB]:
    doc = await db.users.find_one({"username": username})
    return UserInDB(**doc) if doc else None


async def get_user_by_email(db: AsyncIOMotorDatabase, email: str) -> Optional[UserInDB]:
    doc = await db.users.find_one({"email": email})
    return UserInDB(**doc) if doc else None


async def get_user_by_google_id(db: AsyncIOMotorDatabase, google_id: str) -> Optional[UserInDB]:
    doc = await db.users.find_one({"google_id": google_id})
    return UserInDB(**doc) if doc else None


async def create_user(db: AsyncIOMotorDatabase, user_data: UserCreate) -> UserInDB:
    # Check duplicates
    if await get_user_by_username(db, user_data.username):
        raise ValueError("Username already registered")
    if await get_user_by_email(db, user_data.email):
        raise ValueError("Email already registered")
    
    user_doc = {
        "username": user_data.username,
        "email": user_data.email,
        "hashed_password": get_password_hash(user_data.password),
        "provider": "password",
        "avatar_url": None,
        "scopes": ["convert:curl"],
        "is_active": True,
        "created_at": datetime.now(timezone.utc)
    }
    
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id
    return UserInDB(**user_doc)


async def authenticate_user(db: AsyncIOMotorDatabase, username: str, password: str) -> Optional[UserInDB]:
    user = await get_user_by_username(db, username)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


async def create_or_update_google_user(
    db: AsyncIOMotorDatabase,
    *,
    email: str,
    name: str,
    google_id: str,
    avatar_url: Optional[str] = None,
) -> UserInDB:
    now = datetime.now(timezone.utc)
    existing = await get_user_by_google_id(db, google_id) or await get_user_by_email(db, email)
    if existing:
        await db.users.update_one(
            {"_id": ObjectId(existing.id)},
            {"$set": {
                "email": email,
                "username": existing.username or email.split("@")[0],
                "provider": "google",
                "google_id": google_id,
                "avatar_url": avatar_url,
                "updated_at": now,
                "is_active": True,
            }}
        )
        return await get_user_by_id(db, existing.id)

    base_username = (name or email.split("@")[0]).strip().replace(" ", "_").lower()
    username = base_username or "google_user"
    suffix = 2
    while await get_user_by_username(db, username):
        username = f"{base_username}_{suffix}"
        suffix += 1

    user_doc = {
        "username": username,
        "email": email,
        "hashed_password": "",
        "provider": "google",
        "google_id": google_id,
        "avatar_url": avatar_url,
        "scopes": ["convert:curl"],
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id
    return UserInDB(**user_doc)


async def update_password_by_email(db: AsyncIOMotorDatabase, email: str, password: str) -> bool:
    result = await db.users.update_one(
        {"email": email},
        {"$set": {
            "hashed_password": get_password_hash(password),
            "provider": "password",
            "updated_at": datetime.now(timezone.utc),
        }}
    )
    return result.modified_count == 1


async def update_user_scopes(db: AsyncIOMotorDatabase, user_id: str, scopes: list[str]) -> Optional[UserInDB]:
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"scopes": scopes, "updated_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 1:
        return await get_user_by_id(db, user_id)
    return None
