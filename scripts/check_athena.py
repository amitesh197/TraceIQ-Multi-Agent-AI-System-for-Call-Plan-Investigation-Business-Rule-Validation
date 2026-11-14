"""Check that every table in Athena has the same row count as the local CSV.

  python scripts/check_athena.py --bucket my-traceiq-bucket
"""
import argparse
import os
import sys
import time

import boto3
from dotenv import load_dotenv
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))
from traceiq.db import DATA_DIR, TABLES  # noqa: E402


def scalar(athena, sql, out):
    qid = athena.start_query_execution(QueryString=sql, ResultConfiguration={"OutputLocation": out})["QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st["State"] != "SUCCEEDED":
        raise RuntimeError(f"{st['State']}: {st.get('StateChangeReason')}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return rows[1]["Data"][0]["VarCharValue"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=os.environ.get("TRACEIQ_S3_BUCKET"), required=not os.environ.get("TRACEIQ_S3_BUCKET"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    a = ap.parse_args()
    athena = boto3.client("athena", region_name=a.region)
    out = f"s3://{a.bucket}/athena-results/"
    bad = 0
    for t in TABLES:
        local = len(pd.read_csv(os.path.join(DATA_DIR, f"{t}.csv"), dtype=str))
        remote = int(scalar(athena, f"SELECT COUNT(*) FROM jib_aws.{t}", out))
        ok = local == remote
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'}  {t:34s} local {local:5d}  athena {remote:5d}")
    print("\nAll tables match." if not bad else f"\n{bad} table(s) do not match.")


if __name__ == "__main__":
    main()
