"""Ask TraceIQ a question from the command line.

  python ask.py "Why was 12345678 dropped from 106-NR-1234567?"
  python ask.py --user imm_team_member "Why is 12345678 in 200-SD-3010489?"
  python ask.py --offline "..."      # force offline mode (no LLM)
"""
import argparse
import json

from traceiq.agents.graph import ask

ap = argparse.ArgumentParser()
ap.add_argument("question")
ap.add_argument("--user", default="cor_analyst")
ap.add_argument("--offline", action="store_true")
ap.add_argument("--debug", action="store_true", help="print every tool call and result")
a = ap.parse_args()
out = ask(a.question, a.user, use_llm=False if a.offline else None)
r = out["response"]
print(f"\n[{r['status']}]  confidence: {out.get('quality', {}).get('confidence', '-')}\n")
print(r["answer"])
if r.get("written_by"):
    print(f"\n(written by: {r['written_by']})")
for line in r.get("fact_check", []):
    print(f"  fact check: {line}")
if r.get("email_body"):
    print(f"\n--- Draft email ---\nSubject: {r['email_subject']}\n\n{r['email_body']}")
if a.debug:
    print("\n--- Evidence ---")
    for c in out.get("evidence", []):
        print(f"\n{c['tool']}({c['args']})\n{json.dumps(c['result'], indent=1, default=str)[:1500]}")
print(f"\nTimings: {out.get('timings')}")
