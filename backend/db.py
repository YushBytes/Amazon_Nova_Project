import os
from motor.motor_asyncio import AsyncIOMotorClient

# MongoDB Connection Configuration
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("MONGODB_DB_NAME", "novaflow")

class Database:
    client: AsyncIOMotorClient = None
    db = None

db = Database()

async def get_database():
    if db.client is None:
        # Add serverSelectionTimeoutMS for better handling of cloud connection failures
        db.client = AsyncIOMotorClient(
            MONGODB_URL, 
            serverSelectionTimeoutMS=5000
        )
        db.db = db.client[DATABASE_NAME]
    return db.db

async def close_database():
    if db.client:
        db.client.close()
