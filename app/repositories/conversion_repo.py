"""Async MongoDB conversion history repository"""
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models import ConversionHistory, ConversionHistoryCreate


async def save_conversion(
    db: AsyncIOMotorDatabase, 
    data: ConversionHistoryCreate
) -> ConversionHistory:
    doc = data.model_dump(exclude_unset=True)
    doc["created_at"] = datetime.now(timezone.utc)
    result = await db.conversions.insert_one(doc)
    doc["_id"] = result.inserted_id
    return ConversionHistory(**doc)


async def get_user_conversions(
    db: AsyncIOMotorDatabase, 
    user_id: str, 
    skip: int = 0, 
    limit: int = 20
) -> tuple[List[ConversionHistory], int]:
    query = {"user_id": ObjectId(user_id)}
    total = await db.conversions.count_documents(query)
    
    cursor = db.conversions.find(query).sort("created_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [ConversionHistory(**doc) for doc in docs], total


async def get_conversion_by_id(
    db: AsyncIOMotorDatabase, 
    conv_id: str
) -> Optional[ConversionHistory]:
    doc = await db.conversions.find_one({"_id": ObjectId(conv_id)})
    return ConversionHistory(**doc) if doc else None


async def delete_conversion(
    db: AsyncIOMotorDatabase, 
    conv_id: str, 
    user_id: str
) -> bool:
    result = await db.conversions.delete_one({
        "_id": ObjectId(conv_id),
        "user_id": ObjectId(user_id)
    })
    return result.deleted_count == 1