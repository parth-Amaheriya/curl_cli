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


async def update_user_scopes(db: AsyncIOMotorDatabase, user_id: str, scopes: list[str]) -> Optional[UserInDB]:
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"scopes": scopes, "updated_at": datetime.now(timezone.utc)}}
    )
    if result.modified_count == 1:
        return await get_user_by_id(db, user_id)
    return None