import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Captures S3 ObjectCreated events and logs the S3 URI.
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    try:
        for record in event.get('Records', []):
            bucket_name = record['s3']['bucket']['name']
            object_key = record['s3']['object']['key']
            s3_uri = f"s3://{bucket_name}/{object_key}"
            
            logger.info(f"New object detected: {s3_uri}")
            
            # Next Steps: Trigger Step Function or Nova 2 Pro Audit
            # logger.info(f"Triggering analysis for {s3_uri}...")
            
        return {
            'statusCode': 200,
            'body': json.dumps('Event processed successfully')
        }
    except Exception as e:
        logger.error(f"Error processing S3 event: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {str(e)}")
        }
