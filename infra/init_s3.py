"""SUPERSEDED - do not run. Kept for reference only.

This script creates buckets named "brand-guardian-raw" and
"brand-guardian-final", which no application code reads: the backend resolves
its single bucket through backend/aws_resources.py (S3_BUCKET_NAME, default
"novaflow-media"). Running this would create two unused buckets and leave the
real one unprovisioned.

It also creates them with no public-access block, no default encryption, no
versioning, no TLS-only bucket policy, and no lifecycle rules.

Use infra/cloudformation/storage.yaml instead. It provisions the bucket the
code actually uses, with those controls applied, plus the two DynamoDB tables
this script never created. See docs/PIPELINE_OPERATIONS.md.

Retained rather than deleted because it documents the original naming scheme,
which an operator with existing buckets may need to reconcile.
"""

import boto3
from botocore.exceptions import ClientError

def create_bucket(bucket_name, region=None):
    """Create an S3 bucket in a specified region

    If a region is not specified, the bucket is created in the S3 default
    region (us-east-1).

    :param bucket_name: Bucket to create
    :param region: String region to create bucket in, e.g., 'us-west-2'
    :return: True if bucket created, else False
    """
    try:
        if region is None:
            s3_client = boto3.client('s3')
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client = boto3.client('s3', region_name=region)
            location = {'LocationConstraint': region}
            s3_client.create_bucket(Bucket=bucket_name,
                                    CreateBucketConfiguration=location)
    except ClientError as e:
        print(f"Error creating bucket {bucket_name}: {e}")
        return False
    return True

if __name__ == "__main__":
    buckets = ["brand-guardian-raw", "brand-guardian-final"]
    # You can specify your region here
    region = boto3.Session().region_name or 'us-east-1'
    
    print(f"Initializing S3 buckets in region: {region}")
    for bucket in buckets:
        if create_bucket(bucket, region if region != 'us-east-1' else None):
            print(f"Successfully created bucket: {bucket}")
        else:
            print(f"Failed to create bucket or it already exists: {bucket}")
