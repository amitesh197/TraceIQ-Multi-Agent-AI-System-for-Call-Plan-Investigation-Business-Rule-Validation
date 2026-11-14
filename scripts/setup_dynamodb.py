"""Create the DynamoDB table TraceIQ uses for per-user memory (safe to run again).

  python scripts/setup_dynamodb.py

Uses provisioned capacity of 5 read and 5 write units, inside DynamoDB's always-free allowance.
"""
import os
import sys

import boto3
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
TABLE = os.environ.get("TRACEIQ_DDB_TABLE", "traceiq_memory")
REGION = os.environ.get("AWS_REGION", "us-east-1")

ddb = boto3.client("dynamodb", region_name=REGION)
existing = ddb.list_tables().get("TableNames", [])
if TABLE in existing:
    print(f"Table {TABLE} already exists in {REGION}.")
else:
    ddb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )
    print(f"Creating table {TABLE} in {REGION} ...")
    ddb.get_waiter("table_exists").wait(TableName=TABLE)
    print("Table is ready.")
desc = ddb.describe_table(TableName=TABLE)["Table"]
print(f"Status: {desc['TableStatus']}, items: {desc.get('ItemCount', 0)}")
print("Set TRACEIQ_MEMORY=dynamodb in .env to use it.")
sys.exit(0)
