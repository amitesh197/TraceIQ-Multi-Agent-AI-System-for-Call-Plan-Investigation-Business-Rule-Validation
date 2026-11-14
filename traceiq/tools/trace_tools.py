"""Trace tools. Deterministic: they only read tables and report facts."""
from ..access import AccessContext
from ..db import query
from ..pipeline import SEGMENTS, TRACE_STAGES

FIELDS = ["selling_div_code", "territory", "cp_tgt_flag", "customer_status", "primary_spec",
          "secondary_spec", "sales_direction"]


def _rows(stage: str, jnj_id: str, territory: str | None = None) -> list[dict]:
    if stage in ("lnd", "stg"):
        tables = [f"{stage}_iconnect_call_plan_hcp", f"{stage}_iconnect_call_plan_account"]
    else:
        tables = [stage]
    out = []
    for t in tables:
        sql = f"SELECT * FROM jib_aws.{t} WHERE jnj_id = ?" + (" AND territory = ?" if territory else "")
        out += query(sql, [jnj_id] + ([territory] if territory else []))
    return out


def find_customer(ctx: AccessContext, jnj_id: str) -> dict:
    """Every team and territory where the customer appears in any pipeline table (within the user's access)."""
    seen = {}
    for stage, _ in TRACE_STAGES:
        for r in _rows(stage, jnj_id):
            if not ctx.can_see(r["territory"]):
                continue
            seen.setdefault(r["territory"], {"selling_div_code": r["selling_div_code"], "stages": []})
            if stage not in seen[r["territory"]]["stages"]:
                seen[r["territory"]]["stages"].append(stage)
    return {"jnj_id": jnj_id, "territories": seen,
            "note": "only teams the user can access are shown" if not ctx.all_teams else ""}


def trace_customer(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    """Stage-by-stage record for one customer in one territory."""
    ctx.require(territory)
    stages = []
    for stage, label in TRACE_STAGES:
        rows = _rows(stage, jnj_id, territory)
        rec = rows[0] if rows else None
        stages.append({"stage": stage, "label": label, "present": rec is not None,
                       "record": {k: rec.get(k, "") for k in FIELDS} if rec else None})
    return {"jnj_id": jnj_id, "territory": territory, "stages": stages}


def locate_change(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    """Where the customer appeared, disappeared or changed between consecutive tables."""
    tr = trace_customer(ctx, jnj_id, territory)
    st = tr["stages"]
    changes = []
    if not st[0]["present"]:
        first = next((s for s in st if s["present"]), None)
        if first is None:
            return {"jnj_id": jnj_id, "territory": territory, "changes": [],
                    "summary": "NOT_IN_PIPELINE: never sent by iConnect for this territory and never added"}
    for a, b in zip(st, st[1:]):
        seg = SEGMENTS.get((a["stage"], b["stage"]))
        name, candidates = seg if seg else ("NONE", [])
        if a["present"] and not b["present"]:
            changes.append({"between": [a["stage"], b["stage"]], "segment": name, "change": "DROPPED",
                            "candidate_checks": candidates})
        elif not a["present"] and b["present"]:
            changes.append({"between": [a["stage"], b["stage"]], "segment": name, "change": "ADDED",
                            "candidate_checks": candidates})
        elif a["present"] and b["present"]:
            diff = {k: [a["record"][k], b["record"][k]] for k in ("cp_tgt_flag", "sales_direction")
                    if a["record"][k] != b["record"][k]}
            if diff:
                changes.append({"between": [a["stage"], b["stage"]], "segment": name, "change": "UPDATED",
                                "fields": diff, "candidate_checks": candidates})
    final = st[-1]["present"]
    summary = ("IN_FINAL_PLAN" if final else "NOT_IN_FINAL_PLAN") + ("" if changes else ": no changes along the way")
    return {"jnj_id": jnj_id, "territory": territory, "changes": changes, "summary": summary,
            "in_iconnect_input": st[0]["present"], "in_final_plan": final}


def territory_summary(ctx: AccessContext, territory: str) -> dict:
    """Counts of customers and targets for a territory at the JCPM UI and in the final plan."""
    ctx.require(territory)
    out = {"territory": territory, "team": territory[:6]}
    for t in ("mstr_cppa_srchb_univ", "iconnect_cp_out", "iconnect_final_call_plan_out"):
        r = query(f"""SELECT COUNT(*) AS records,
                             COUNT(DISTINCT CASE WHEN cp_tgt_flag = 'TRUE' THEN jnj_id END) AS distinct_targets
                      FROM jib_aws.{t} WHERE territory = ?""", [territory])[0]
        out[t] = {"records": int(r["records"]), "distinct_targets": int(r["distinct_targets"])}
    return out
