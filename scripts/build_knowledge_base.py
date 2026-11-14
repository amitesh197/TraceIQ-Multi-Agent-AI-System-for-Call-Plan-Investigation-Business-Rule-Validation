"""Build (or refresh) the rules knowledge base in ChromaDB.

  python scripts/build_knowledge_base.py

Reads every knowledge/rules_*.yaml (one file per quarter) and adds team-specific facts from the
reference tables (specialty lists, capacity minimums, mirror pairs, VSAP territories, products).
Safe to run again: entries are updated in place.
"""
import glob
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traceiq import vectors  # noqa: E402
from traceiq.db import query  # noqa: E402


def table_docs(quarter: str) -> list[dict]:
    q = quarter.replace("'", "").lower()
    docs = []
    spec = query("SELECT team_id, incl_excl, string_agg(spec, ', ' ORDER BY spec) AS specs FROM jib_aws.spec_incl_excl GROUP BY 1, 2")
    for r in spec:
        kind = "inclusion" if r["incl_excl"] == "INCL" else "exclusion"
        docs.append({"id": f"{q}-spec-{r['team_id']}", "quarter": quarter, "stage": "INPUT",
                     "rule": "SPEC_INCL" if r["incl_excl"] == "INCL" else "SPEC_EXCL", "teams": [r["team_id"]],
                     "title": f"{r['team_id']} specialty {kind} list",
                     "text": f"{r['team_id']} has a specialty {kind} list: {r['specs']}. " +
                             ("HCPs are removed unless their primary or secondary specialty is on it."
                              if kind == "inclusion" else "HCPs whose primary or secondary specialty is on it are removed.")})
    for r in query("SELECT team, capacity FROM jib_aws.under_capacity_terr"):
        docs.append({"id": f"{q}-capacity-{r['team']}", "quarter": quarter, "stage": "OUTPUT", "rule": "UNDER_CAPACITY",
                     "teams": [r["team"]], "title": f"{r['team']} minimum targets per territory",
                     "text": f"Each {r['team']} territory needs at least {r['capacity']} distinct targets on the final "
                             f"iConnect Output. Territories below this are highlighted in the output mail; nothing is dropped."})
    pairs = query("SELECT base_terr, mirror_terr FROM jib_aws.imm_derm_mapping")
    if pairs:
        listing = "; ".join(f"{p['base_terr']} (base) with {p['mirror_terr']} (mirror)" for p in pairs)
        docs.append({"id": f"{q}-mirror-pairs", "quarter": quarter, "stage": "OUTPUT", "rule": "IMM_DERM_MIRRORING",
                     "teams": ["200-SD"], "title": "200-SD base and mirror territory pairs",
                     "text": f"The {quarter} 200-SD mirror pairs are: {listing}."})
    vs = [r["territory"] for r in query("SELECT territory FROM jib_aws.onc_vsap_q326")]
    if vs:
        docs.append({"id": f"{q}-vsap-list", "quarter": quarter, "stage": "OUTPUT", "rule": "ONC_VSAP_REMOVAL",
                     "teams": sorted({t[:6] for t in vs}), "title": f"{quarter} ONC VSAP territories",
                     "text": f"The {quarter} ONC VSAP territories, removed from the final output, are: {', '.join(vs)}."})
    tt = query("""SELECT team_id, string_agg(customer_type, ' and ' ORDER BY customer_type) AS types, max(going_live) AS live
                  FROM jib_aws.team_type GROUP BY 1 ORDER BY 1""")
    for r in tt:
        live = "is processed" if r["live"] == "YES" else "is not processed (not going live)"
        docs.append({"id": f"{q}-team-{r['team_id']}", "quarter": quarter, "stage": "OVERVIEW", "rule": "TEAM",
                     "teams": [r["team_id"]], "title": f"{r['team_id']} team setup",
                     "text": f"In {quarter}, {r['team_id']} {live} and covers {r['types']} customers."})
    return docs


def main():
    docs = []
    files = sorted(glob.glob(os.path.join(ROOT, "knowledge", "rules_*.yaml")))
    if not files:
        sys.exit("No knowledge/rules_*.yaml files found.")
    for path in files:
        with open(path, encoding="utf-8") as f:
            kb = yaml.safe_load(f)
        quarter = kb["quarter"]
        docs += [{**r, "quarter": quarter, "teams": r.get("teams", ["ALL"])} for r in kb["rules"]]
        if quarter == "Q3'26":   # the loaded pipeline tables are Q3'26
            docs += table_docs(quarter)
        print(f"{os.path.basename(path)}: quarter {quarter}")
    n = vectors.index_rules(docs)
    print(f"Indexed {n} rule entries into ChromaDB ({vectors.provider_note()}).")
    hits = vectors.search_rules("why was an NR HCP dropped when it is also a target in NSS", "Q3'26", "106-NR", k=2)
    print("Test search:", [h["title"] for h in hits])


if __name__ == "__main__":
    main()
