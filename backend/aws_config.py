import re
import boto3

def parse_s3_uri(s3_uri):
    """
    Parses an S3 URI into bucket and key.
    Example: s3://my-bucket/path/to/video.mp4 -> ('my-bucket', 'path/to/video.mp4')
    """
    match = re.match(r's3://([^/]+)/(.*)', s3_uri)
    if not match:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    return match.groups()

def get_nova_video_input(s3_uri):
    """
    Prepares the input structure for Amazon Nova 2 Pro video analysis via S3.
    """
    bucket, key = parse_s3_uri(s3_uri)
    return {
        "s3Location": {
            "uri": s3_uri,
            "bucket": bucket,
            "key": key
        }
    }

# AWS Client Initialization
session = boto3.Session()
bedrock_runtime = session.client('bedrock-runtime')
s3_client = session.client('s3')
stepfunctions = session.client('stepfunctions')
