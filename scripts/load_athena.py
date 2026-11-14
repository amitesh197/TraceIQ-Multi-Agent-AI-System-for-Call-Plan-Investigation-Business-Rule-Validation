"""Upload the pipeline tables to S3 and create matching Athena tables (all columns string).

  python scripts/load_athena.py --bucket my-traceiq-bucket [--region us-east-1]

Creates s3://<bucket>/jib_aws/<table>/<table>.csv for every table, a Glue database
`jib_aws`, and one external table per CSV. Query results go to s3://<bucket>/athena-results/.
"""
import argparse
import csv
import os
import sys
import time

import boto3
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))
from traceiq.db import DATA_DIR, TABLES  # noqa: E402


def run(athena, sql, out, wait=True):
    qid = athena.start_query_execution(QueryString=sql, ResultConfiguration={"OutputLocation": out})["QueryExecutionId"]
    while wait:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            if st["State"] != "SUCCEEDED":
                raise RuntimeError(f"{st['State']}: {st.get('StateChangeReason')}\n{sql}")
            return qid
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=os.environ.get("TRACEIQ_S3_BUCKET"), required=not os.environ.get("TRACEIQ_S3_BUCKET"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    a = ap.parse_args()
    s3 = boto3.client("s3", region_name=a.region)
    athena = boto3.client("athena", region_name=a.region)
    out = f"s3://{a.bucket}/athena-results/"
    run(athena, "CREATE DATABASE IF NOT EXISTS jib_aws", out)
    for t in TABLES:
        path = os.path.join(DATA_DIR, f"{t}.csv")
        s3.upload_file(path, a.bucket, f"jib_aws/{t}/{t}.csv")
        with open(path, newline="") as f:
            cols = next(csv.reader(f))
        col_sql = ",\n  ".join(f"`{c}` string" for c in cols)
        run(athena, f"DROP TABLE IF EXISTS jib_aws.{t}", out)
        run(athena, f"""CREATE EXTERNAL TABLE jib_aws.{t} (
  {col_sql}
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"')
LOCATION 's3://{a.bucket}/jib_aws/{t}/'
TBLPROPERTIES ('skip.header.line.count' = '1')""", out)
        print(f"loaded jib_aws.{t}")
    print("Done. Try in the Athena console: SELECT COUNT(*) FROM jib_aws.iconnect_final_call_plan_out;")


if __name__ == "__main__":
    main()
