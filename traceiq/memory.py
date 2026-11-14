"""Per-user memory, stored on the server side.

  TRACEIQ_MEMORY=dynamodb  table TRACEIQ_DDB_TABLE (default traceiq_memory), created by scripts/setup_dynamodb.py
  TRACEIQ_MEMORY=local     JSON files under TRACEIQ_STATE_DIR/memory (default when DynamoDB isn't set up)

Items (partition key user_id, sort key sk):
  sk = "PROFILE"               the user's remembered access (teams, role, expiry) and last sign-in
  sk = "TURN#<iso time>#<id>"  one question with its answer, IDs, rules, confidence and review status
"""
import datetime as dt
import json
import os
import threading
import uuid
from functools import lru_cache

from .vectors import STATE_DIR

BACKEND = os.environ.get("TRACEIQ_MEMORY", "local").strip().lower()
TABLE = os.environ.get("TRACEIQ_DDB_TABLE", "traceiq_memory")
REGION = os.environ.get("AWS_REGION", "us-east-1")
LOCAL_DIR = os.path.join(STATE_DIR, "memory")
_lock = threading.Lock()
_status = {"backend": BACKEND, "note": ""}


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def new_turn_id() -> str:
    return uuid.uuid4().hex[:10]


# ---------------------------------------------------------------- DynamoDB backend
@lru_cache(maxsize=1)
def _table():
    import boto3
    t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    t.load()  # fails fast if the table is missing or there is no permission
    return t


def _ddb_ok() -> bool:
    if _status["backend"] != "dynamodb":
        return False
    try:
        _table()
        return True
    except Exception as e:
        _status.update(backend="local", note=f"DynamoDB unavailable ({type(e).__name__}); using local memory")
        return False


def _pack(user_id: str, sk: str, data: dict) -> dict:
    return {"user_id": user_id, "sk": sk, "data": json.dumps(data, default=str)}


def _unpack(item: dict) -> dict:
    d = json.loads(item["data"])
    d["sk"] = item["sk"]
    return d


# ---------------------------------------------------------------- local backend
def _local_path(user_id: str) -> str:
    os.makedirs(LOCAL_DIR, exist_ok=True)
    safe = "".join(c for c in user_id if c.isalnum() or c in "-_")
    return os.path.join(LOCAL_DIR, f"{safe}.json")


def _local_load(user_id: str) -> dict:
    p = _local_path(user_id)
    if not os.path.exists(p):
        return {"profile": None, "turns": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _local_save(user_id: str, blob: dict):
    tmp = _local_path(user_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blob, f, default=str)
    os.replace(tmp, _local_path(user_id))


# ---------------------------------------------------------------- public API
def status() -> str:
    _ddb_ok()
    return _status["note"] or ("DynamoDB table " + TABLE if _status["backend"] == "dynamodb" else "local files on the server")


def save_profile(ctx) -> dict:
    """Remember the user's access for this and later sessions."""
    prof = {"user_id": ctx.user_id, "name": ctx.name, "role": ctx.role, "all_teams": ctx.all_teams,
            "teams": sorted(ctx.teams), "expires": ctx.expires, "last_sign_in": now_iso()}
    if _ddb_ok():
        _table().put_item(Item=_pack(ctx.user_id, "PROFILE", prof))
    else:
        with _lock:
            blob = _local_load(ctx.user_id)
            blob["profile"] = prof
            _local_save(ctx.user_id, blob)
    return prof


def get_profile(user_id: str) -> dict | None:
    if _ddb_ok():
        it = _table().get_item(Key={"user_id": user_id, "sk": "PROFILE"}).get("Item")
        return _unpack(it) if it else None
    return _local_load(user_id).get("profile")


def add_turn(user_id: str, turn: dict) -> dict:
    turn = dict(turn)
    turn.setdefault("id", new_turn_id())
    turn.setdefault("time", now_iso())
    turn["sk"] = f"TURN#{dt.datetime.now().isoformat(timespec='microseconds')}#{turn['id']}"  # sorts newest last
    if _ddb_ok():
        _table().put_item(Item=_pack(user_id, turn["sk"], turn))
    else:
        with _lock:
            blob = _local_load(user_id)
            blob["turns"].append(turn)
            _local_save(user_id, blob)
    return turn


def recent_turns(user_id: str, limit: int = 20) -> list[dict]:
    if _ddb_ok():
        from boto3.dynamodb.conditions import Key
        r = _table().query(KeyConditionExpression=Key("user_id").eq(user_id) & Key("sk").begins_with("TURN#"),
                           ScanIndexForward=False, Limit=limit)
        return [_unpack(i) for i in r.get("Items", [])]
    return sorted(_local_load(user_id)["turns"], key=lambda t: t["sk"], reverse=True)[:limit]


def update_turn(user_id: str, sk: str, **fields) -> None:
    if _ddb_ok():
        it = _table().get_item(Key={"user_id": user_id, "sk": sk}).get("Item")
        if it:
            d = _unpack(it); d.update(fields); d.pop("sk", None)
            _table().put_item(Item=_pack(user_id, sk, d))
        return
    with _lock:
        blob = _local_load(user_id)
        for t in blob["turns"]:
            if t["sk"] == sk:
                t.update(fields)
        _local_save(user_id, blob)


def all_turns(limit: int = 200) -> list[dict]:
    """Every user's turns, newest first (for the analyst review queue)."""
    rows = []
    if _ddb_ok():
        from boto3.dynamodb.conditions import Attr
        kw = {"FilterExpression": Attr("sk").begins_with("TURN#")}
        while True:
            r = _table().scan(**kw)
            rows += [{**_unpack(i), "user_id": i["user_id"]} for i in r.get("Items", [])]
            if "LastEvaluatedKey" not in r or len(rows) >= limit:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    else:
        if os.path.isdir(LOCAL_DIR):
            for fn in os.listdir(LOCAL_DIR):
                if fn.endswith(".json"):
                    uid = fn[:-5]
                    rows += [{**t, "user_id": uid} for t in _local_load(uid)["turns"]]
    return sorted(rows, key=lambda t: t["time"], reverse=True)[:limit]
