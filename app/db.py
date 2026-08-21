"""
Async MongoDB connection via Motor
"""
import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import settings

logger = logging.getLogger(__name__)

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


async def connect_db():
    global client, db
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(settings.mongodb_url)
    db = client[settings.mongodb_database]
    await client.admin.command("ping")
    logger.info(f"Connected to MongoDB database: {settings.mongodb_database}")

    # Ensure indexes for content_chat_queries
    chat_queries = db["content_chat_queries"]
    await chat_queries.create_index(
        [("presentation_id", 1), ("date", 1)],
        name="idx_presentation_date",
    )
    await chat_queries.create_index(
        "created_at",
        name="idx_created_at_ttl",
        expireAfterSeconds=90 * 24 * 3600,  # auto-delete after 90 days
    )
    logger.info("Ensured indexes on content_chat_queries")


async def close_db():
    global client
    if client:
        client.close()
        logger.info("MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    assert db is not None, "Database not initialized — call connect_db() first"
    return db
