"""Score TraceIQ on the seeded test cases.

  python -m evals.run_eval            # offline mode (no LLM): tests tools and planning
  python -m evals.run_eval --llm      # full agents with the LLM in TRACEIQ_MODEL

A case passes when the rule TraceIQ identifies matches the answer key, the
confidence is HIGH, and (for "kept" cases) no rule is wrongly said to drop it.
"""
import argparse
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traceiq.agents.graph import ask  # noqa: E402

KEY = os.path.join(os.path.dirname(__file__), "answer_key", "test_cases.csv")
RULE_ALIASES = {"SPEC_INCL": {"SPEC_INCL"}, "SPEC_EXCL": {"SPEC_EXCL"}}


def matched_rules(out: dict) -> list:
    rules = []
    for f in out.get("quality", {}).get("findings", []):
        rules += [m["rule"] for m in f["explained_by"]]
    for c in out.get("rules", {}).get("checks", []):
        r = c.get("result", {})
        if c["tool"] in ("check_under_capacity", "check_not_in_iconnect") and r.get("result") == "MATCH":
            rules.append(r["rule"])
        if c["tool"] == "check_jcpm_changes" and r.get("result") == "MATCH":
            rules += [e["rule"] for e in r["evidence"].get("events", [])]
    return list(dict.fromkeys(rules))


def grade(case: dict, out: dict) -> tuple[bool, str]:
    got = matched_rules(out)
    conf = out.get("quality", {}).get("confidence")
    stage, rule = case["expected_stage"], case["expected_rule"]
    if stage == "NONE":
        if rule == "NOT_IN_ICONNECT":
            return ("NOT_IN_ICONNECT" in got, f"got {got}")
        drops = [f for f in out.get("quality", {}).get("findings", []) if f["change"].startswith("DROPPED")]
        return (not drops and conf == "HIGH", f"got {got or 'no change'}")
    return (rule in got and conf == "HIGH", f"got {got}, confidence {conf}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--user", default="cor_analyst")
    a = ap.parse_args()
    cases = pd.read_csv(KEY, dtype=str, keep_default_na=False).to_dict("records")
    rows, t0 = [], time.time()
    for c in cases:
        t = time.time()
        out = ask(c["question"], a.user, use_llm=a.llm)
        ok, why = grade(c, out)
        rows.append({"case": c["case_id"], "pass": ok, "expected": c["expected_rule"], "detail": why,
                     "seconds": round(time.time() - t, 2), "answer": out.get("response", {}).get("answer", "")})
        print(f"{'PASS' if ok else 'FAIL'}  {c['case_id']}  expected {c['expected_rule']:<24} {why}")
    df = pd.DataFrame(rows)
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    path = os.path.join(os.path.dirname(__file__), "results", f"eval_{'llm' if a.llm else 'offline'}.csv")
    df.to_csv(path, index=False)
    print(f"\nScore: {int(df['pass'].sum())}/{len(df)} correct  |  avg {df.seconds.mean():.2f}s per question  |  "
          f"total {time.time() - t0:.1f}s  |  results: {path}")


if __name__ == "__main__":
    main()
