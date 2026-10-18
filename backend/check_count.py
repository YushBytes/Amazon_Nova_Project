import pymongo
try:
    client = pymongo.MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=2000)
    db = client['novaflow_video']
    count = db.frame_metadata.count_documents({"video_id": "test_8k_v2"})
    print(f"COUNT_RESULT:{count}")
except Exception as e:
    print(f"ERROR:{e}")
