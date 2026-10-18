import sys
import pymongo
import gridfs
from bson.objectid import ObjectId

# Configuration
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "novaflow_video"

def verify_frame(video_id, frame_number=0):
    client = pymongo.MongoClient(MONGO_URI)
    db = client[DB_NAME]
    fs = gridfs.GridFS(db)
    metadata_col = db["frame_metadata"]

    # Find metadata
    query = {"video_id": video_id, "frame_number": int(frame_number)}
    metadata = metadata_col.find_one(query)

    if not metadata:
        print(f"No frame found for video_id: {video_id} and frame_number: {frame_number}")
        return

    print(f"Retrieving frame {frame_number} for video {video_id}...")
    
    # Retrieve from GridFS
    try:
        gridfs_id = metadata["gridfs_id"]
        frame_data = fs.get(gridfs_id).read()
        
        # Save to file
        output_path = "test_output.jpg"
        with open(output_path, "wb") as f:
            f.write(frame_data)
        
        print(f"Success! Frame saved to {output_path}")
    except Exception as e:
        print(f"Error retrieving frame: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verifier.py video_id [frame_number]")
        sys.exit(1)
    
    vid = sys.argv[1]
    frame_num = sys.argv[2] if len(sys.argv) > 2 else 0
    verify_frame(vid, frame_num)
