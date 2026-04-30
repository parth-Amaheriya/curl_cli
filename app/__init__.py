"""MongoDB async client setup"""
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

client = AsyncIOMotorClient(settings.mongodb_url)
db = client[settings.mongodb_db_name]

async def close_db():
    client.close()

# Dependency for FastAPI
async def get_db():
    try:
        yield db
    finally:
        pass  # Motor handles connection pooling automatically