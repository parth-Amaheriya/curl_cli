"""Async MongoDB conversion history repository"""
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional, List
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.models import ConversionHistory, ConversionHistoryCreate


async def save_conversion(
    db: AsyncIOMotorDatabase, 
    data: ConversionHistoryCreate
) -> ConversionHistory:
    doc = {
        key: value
        for key, value in data.model_dump(exclude_unset=True).items()
        if value is not None
    }
    now = datetime.now(timezone.utc)
    doc["updated_at"] = now
    if not doc.get("user_id") or not doc.get("collection_id") or not doc.get("snippet_id"):
        raise ValueError("Missing collection_id or snippet_id")

    filter_doc = {
        "user_id": doc["user_id"],
        "collection_id": doc["collection_id"],
        "snippet_id": doc["snippet_id"],
    }
    update_doc = {key: value for key, value in doc.items() if key != "created_at"}
    try:
        await db.conversions.update_one(
            filter_doc,
            {"$set": update_doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        existing = await db.conversions.find_one(filter_doc)
        if not existing:
            raise
        await db.conversions.update_one({"_id": existing["_id"]}, {"$set": update_doc})
    saved = await db.conversions.find_one(filter_doc)
    return ConversionHistory(**saved)


async def delete_conversion_snippet(
    db: AsyncIOMotorDatabase,
    user_id: str,
    collection_id: str,
    snippet_id: str
) -> int:
    result = await db.conversions.delete_many({
        "user_id": user_id,
        "collection_id": collection_id,
        "snippet_id": snippet_id,
    })
    return result.deleted_count


async def delete_conversion_collection(
    db: AsyncIOMotorDatabase,
    user_id: str,
    collection_id: str
) -> int:
    result = await db.conversions.delete_many({
        "user_id": user_id,
        "collection_id": collection_id,
    })
    return result.deleted_count


async def rename_conversion_collection(
    db: AsyncIOMotorDatabase,
    user_id: str,
    collection_id: str,
    collection_name: str
) -> int:
    result = await db.conversions.update_many(
        {
            "user_id": user_id,
            "collection_id": collection_id,
        },
        {
            "$set": {
                "collection_name": collection_name,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return result.modified_count


async def find_duplicate_conversion_groups(
    db: AsyncIOMotorDatabase,
    user_id: str
) -> List[dict]:
    pipeline = [
        {"$match": {"user_id": user_id, "collection_id": {"$exists": True}, "snippet_id": {"$exists": True}}},
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "collection_id": "$collection_id",
                    "snippet_id": "$snippet_id",
                },
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"},
                "updated_at": {"$max": "$updated_at"},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    return await db.conversions.aggregate(pipeline).to_list(length=None)


async def get_user_conversions(
    db: AsyncIOMotorDatabase, 
    user_id: str, 
    skip: int = 0, 
    limit: int = 20
) -> tuple[List[ConversionHistory], int]:
    query = {"user_id": user_id}
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
        "user_id": user_id
    })
    return result.deleted_count == 1
