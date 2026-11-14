"""Data access for TraceIQ.

Loads every pipeline table into an in-memory DuckDB database under the
`jib_aws` schema, all columns as VARCHAR. With TRACEIQ_DATA_SOURCE=s3 the
tables are read from the S3 bucket that scripts/load_athena.py filled (the same
files Athena queries); otherwise from data/jib_aws. Queries then run in memory,
so answers stay fast. The rest of the code only calls `query()`.
"""
import os
from functools import lru_cache

import duckdb
import pandas as pd
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
DATA_DIR = os.environ.get("TRACEIQ_DATA_DIR", os.path.join(ROOT, "data", "jib_aws"))
SOURCE = os.environ.get("TRACEIQ_DATA_SOURCE", "local").strip().lower()   # "local" or "s3"
BUCKET = os.environ.get("TRACEIQ_S3_BUCKET", "").strip()
CACHE_DIR = os.path.join(ROOT, ".cache", "s3", "jib_aws")

TABLES = [
    "team_type", "lnd_iconnect_call_plan_hcp", "lnd_iconnect_call_plan_account",
    "stg_iconnect_call_plan_hcp", "stg_iconnect_call_plan_account", "inp_cppa_srchb_univ",
    "mstr_cust_terr_zip", "pre_proc_cppa_srchb_univ", "proc_cppa_srchb_univ",
    "lnd_ce_forced_ins", "lnd_ce_forced_del", "post_proc_cppa_srchb_univ", "spec_incl_excl",
    "jnj_affiliation", "mstr_cppa_srchb_univ", "iconnect_cp_out", "iconnect_final_call_plan_out",
    "itci_jnj_exclusion", "mstr_jcpm_employee_veeva", "caplyta_hcp_tier", "onc_vsap_q326",
    "imm_derm_mapping", "under_capacity_terr",
]


def _download_from_s3() -> str:
    """Copy every table from s3://<bucket>/jib_aws/<table>/<table>.csv to a local cache; return the folder."""
    import boto3
    if not BUCKET:
        raise RuntimeError("TRACEIQ_DATA_SOURCE=s3 needs TRACEIQ_S3_BUCKET in .env")
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    os.makedirs(CACHE_DIR, exist_ok=True)
    for t in TABLES:
        s3.download_file(BUCKET, f"jib_aws/{t}/{t}.csv", os.path.join(CACHE_DIR, f"{t}.csv"))
    return CACHE_DIR


def source_description() -> str:
    return f"S3 bucket {BUCKET} (jib_aws)" if SOURCE == "s3" else "local files (data/jib_aws)"


@lru_cache(maxsize=1)
def connection() -> duckdb.DuckDBPyConnection:
    folder = _download_from_s3() if SOURCE == "s3" else DATA_DIR
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA IF NOT EXISTS jib_aws")
    for t in TABLES:
        path = os.path.join(folder, f"{t}.csv").replace("'", "''")
        con.execute(f"CREATE TABLE jib_aws.{t} AS SELECT * FROM read_csv('{path}', all_varchar=true, header=true)")
    return con


def query(sql: str, params: list | None = None) -> list[dict]:
    """Run a query and return rows as dicts. Always use parameters for user input."""
    cur = connection().execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def frame(table: str) -> pd.DataFrame:
    """Whole table as a DataFrame (used by the rule replays)."""
    return connection().execute(f"SELECT * FROM jib_aws.{table}").df().fillna("")
