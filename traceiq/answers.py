"""Writes the final answer in the COR team's house style, from confirmed facts only.

Each sentence comes from a tool result or a direct table lookup, so the answer
can be trusted even without an LLM. The Response agent may polish the wording,
but must keep every ID, territory, rule name and number from this text.
"""
from .db import query
from .pipeline import NSR, NSS, spec_rules

QUARTER = "Q3'26"
JCPM_UNIVERSE = f"the {QUARTER} JCPM universe"
OUTPUT = f"the {QUARTER} iConnect Output"


BUSINESS_RULES = {
    "ALIGNMENT_REFRESH": "Customer-territory pairs that are not valid for the quarter in mstr_cust_terr_zip are removed during the alignment refresh.",
    "FORCED_DELETE": "Customer-territory pairs listed in lnd_ce_forced_del are deleted before the JCPM window.",
    "FORCED_INSERT": "Customer-territory pairs listed in lnd_ce_forced_ins are inserted into the universe even if iConnect did not send them.",
    "CUSTOMER_STATUS": "HCPs (PROF) with status INACTV, RETRD or DEAD are removed; accounts are not affected.",
    "SPEC_INCL": "In teams with a specialty inclusion list, an HCP is removed unless its primary or secondary specialty is on the list.",
    "SPEC_EXCL": "In teams with a specialty exclusion list, an HCP is removed if its primary or secondary specialty is on the list.",
    "KAISER_AFFILIATION": "200-SI (IMAAVY) HCPs affiliated with any account whose name contains KAISER are excluded.",
    "REP_DROP": "Reps can drop customers from their territory during the JCPM refinement window.",
    "REP_ADD": "Reps can add customers to their territory during the JCPM refinement window.",
    "REP_TARGET_CHANGE": "Reps can change a customer's target flag during the JCPM refinement window.",
    "SALES_DIRECTION_CHANGE": "Reps can change a customer's sales direction during the JCPM refinement window.",
    "ITCI_EXCLUSION": "106-NA, 106-NB and 106-NR HCPs on the ITCI exclusion list (itci_jnj_exclusion) are removed.",
    "INVALID_VEEVA": "Customers without a valid Veeva ID in mstr_jcpm_employee_veeva are removed.",
    "CAPLYTA_HCP_TIER": "For HCPs that are targets in both NSS (106-NA/NB) and NSR (106-NR): tiers 1-3 keep the NSS record and the NSR record is removed; tiers 4-6 keep the NSR record and the NSS record is removed.",
    "ONC_VSAP_REMOVAL": "All records in territories listed in onc_vsap_q326 are removed.",
    "IMM_DERM_MIRRORING": "In 200-SD, a customer present in only one territory of a base/mirror pair is copied into the other (base territories have 0 as the second-last digit, mirrors 8).",
    "UNDER_CAPACITY": "Territories with fewer distinct targets than the team minimum in under_capacity_terr are flagged in the output mail; no customers are added or dropped.",
    "NOT_IN_ICONNECT": "Only customer-territories sent by iConnect (or force-inserted) enter the call plan.",
}


def customer_type(jnj_id: str) -> str:
    for t in ("inp_cppa_srchb_univ", "mstr_cppa_srchb_univ", "iconnect_cp_out", "iconnect_final_call_plan_out"):
        r = query(f"SELECT acct_typ FROM jib_aws.{t} WHERE jnj_id = ? LIMIT 1", [jnj_id])
        if r:
            return "HCP" if r[0]["acct_typ"] == "PROF" else "Account"
    r = query("SELECT 1 FROM jib_aws.lnd_iconnect_call_plan_account WHERE jnj_id = ? LIMIT 1", [jnj_id])
    return "Account" if r else "HCP"


def in_final(jnj_id: str, territory: str) -> bool:
    return bool(query("SELECT 1 FROM jib_aws.iconnect_final_call_plan_out WHERE jnj_id=? AND territory=?", [jnj_id, territory]))


def _row(table, jnj_id, territory):
    r = query(f"SELECT * FROM jib_aws.{table} WHERE jnj_id=? AND territory=?", [jnj_id, territory])
    return r[0] if r else None


def describe(m: dict, jnj: str, terr: str, kind: str, first: bool) -> str:
    """One paragraph for one confirmed rule match."""
    rule, ev, team = m["rule"], m.get("evidence", {}), terr[:6]
    who = f"Customer {kind} {jnj}" if first else ("The HCP" if kind == "HCP" else "The account")
    dropped = f"{who} was dropped from territory {terr} due to the {rule} rule."
    added = f"{who} was added to territory {terr} due to the {rule} rule."

    if rule == "ALIGNMENT_REFRESH":
        return (f"{dropped} The customer-territory pair is not in the {QUARTER} alignment (mstr_cust_terr_zip). "
                f"As per the business rule, customer-territory pairs that are not valid for the quarter are removed "
                f"during the alignment refresh. Therefore, the record was excluded from {JCPM_UNIVERSE}.")
    if rule == "FORCED_DELETE":
        return (f"{dropped} The pair is listed in the forced deletion list (lnd_ce_forced_del). As per the business rule, "
                f"listed customer-territory pairs are deleted before the JCPM window. Therefore, the record was excluded "
                f"from {JCPM_UNIVERSE}.")
    if rule == "FORCED_INSERT":
        return (f"{added} iConnect did not send this customer for the territory, but the pair is listed in the forced "
                f"insertion list (lnd_ce_forced_ins). As per the business rule, listed pairs are inserted into the universe. "
                f"Therefore, the record is part of {JCPM_UNIVERSE}.")
    if rule == "CUSTOMER_STATUS":
        return (f"{dropped} The HCP has customer status {ev.get('customer_status')}. As per the business rule, HCPs with "
                f"status INACTV, RETRD or DEAD are removed from the call plan. Therefore, the record was excluded from "
                f"{JCPM_UNIVERSE}.")
    if rule in ("SPEC_INCL", "SPEC_EXCL"):
        lists = spec_rules().get(team, {})
        p, s = ev.get("primary_spec"), ev.get("secondary_spec")
        if rule == "SPEC_INCL":
            allowed = ", ".join(sorted(lists.get("INCL", [])))
            fact = (f"The HCP's specialties are {p} (primary) and {s} (secondary), and neither is on the {team} "
                    f"inclusion list ({allowed}).")
            biz = "HCPs in teams with a specialty inclusion list must have at least one listed specialty"
        else:
            banned = ", ".join(sorted(lists.get("EXCL", [])))
            hits = ", ".join(x for x in (p, s) if x in lists.get("EXCL", set()))
            fact = f"The HCP's specialty {hits} is on the {team} exclusion list ({banned})."
            biz = "HCPs with any specialty on the team's exclusion list are removed"
        return f"{dropped} {fact} As per the business rule, {biz}. Therefore, the record was excluded from {JCPM_UNIVERSE}."
    if rule == "KAISER_AFFILIATION":
        names = ", ".join(ev.get("kaiser_affiliations", [])) or "a Kaiser account"
        return (f"{dropped} The HCP is affiliated with {names}. As per the business rule, 200-SI (IMAAVY) HCPs affiliated "
                f"with any Kaiser account are excluded. Therefore, the record was excluded from {JCPM_UNIVERSE}.")
    if rule == "REP_DROP":
        return (f"{who} was dropped from territory {terr} by the rep during the {QUARTER} JCPM refinement window (REP_DROP). "
                f"The customer was in {JCPM_UNIVERSE} but not in the call plan returned after the window (iconnect_cp_out). "
                f"Therefore, the record was excluded from {OUTPUT}.")
    if rule == "REP_ADD":
        others = [r["territory"] for r in query(
            "SELECT territory FROM jib_aws.mstr_cppa_srchb_univ WHERE jnj_id=? AND selling_div_code=? AND territory<>?",
            [jnj, team, terr])]
        also = f" The customer is also aligned to {', '.join(others)}." if others else ""
        return (f"{who} was added to territory {terr} by the rep during the {QUARTER} JCPM refinement window (REP_ADD). The customer "
                f"was not in {JCPM_UNIVERSE} for this territory and appears in the call plan returned after the window "
                f"(iconnect_cp_out).{also}")
    if rule in ("REP_TARGET_CHANGE", "SALES_DIRECTION_CHANGE"):
        b, a = _row("mstr_cppa_srchb_univ", jnj, terr), _row("iconnect_cp_out", jnj, terr)
        subject = f"Customer {kind} {jnj}" if first else ("the HCP" if kind == "HCP" else "the account")
        out = []
        if b and a and b["cp_tgt_flag"] != a["cp_tgt_flag"]:
            now = "a target" if a["cp_tgt_flag"] == "TRUE" else "a non-target"
            out.append(f"During the {QUARTER} JCPM refinement window, the rep changed the target flag for {subject} in "
                       f"territory {terr} from {b['cp_tgt_flag']} to {a['cp_tgt_flag']} (REP_TARGET_CHANGE), so the customer "
                       f"is now {now}.")
            subject = "the customer"
        if b and a and b["sales_direction"] != a["sales_direction"]:
            out.append(f"During the {QUARTER} JCPM refinement window, the rep changed the sales direction for {subject} in "
                       f"territory {terr} from {b['sales_direction']} to {a['sales_direction']} (SALES_DIRECTION_CHANGE).")
        out.append(f"No pipeline rule changed this afterwards, so the record appears this way on {OUTPUT}.")
        return " ".join(out)
    if rule == "ITCI_EXCLUSION":
        return (f"{dropped} The HCP is on the ITCI exclusion list (itci_jnj_exclusion). As per the business rule, 106-NA, "
                f"106-NB and 106-NR HCPs on this list are removed. Therefore, the record was excluded from {OUTPUT}.")
    if rule == "INVALID_VEEVA":
        return (f"{dropped} The customer has no valid Veeva ID in mstr_jcpm_employee_veeva. As per the business rule, "
                f"customers without a valid Veeva ID are removed. Therefore, the record was excluded from {OUTPUT}.")
    if rule == "CAPLYTA_HCP_TIER":
        tier = ev.get("tier")
        nss = ", ".join(ev.get("nss_target_territories", []))
        nsr = ", ".join(ev.get("nsr_target_territories", []))
        if team in NSR:
            kept = [t for t in ev.get("nss_target_territories", []) if in_final(jnj, t)]
            biz = "NSR records for customers in tiers 1-3 are removed when they are also targets in NSS"
            dropped_side, kept_side = "NSR", "NSS"
        else:
            kept = [t for t in ev.get("nsr_target_territories", []) if in_final(jnj, t)]
            biz = "NSS records for customers in tiers 4-6 are removed when they are also targets in NSR"
            dropped_side, kept_side = "NSS", "NSR"
        keep_txt = (f" The {kept_side} record in {', '.join(kept)} is present on {OUTPUT}." if kept else "")
        return (f"{dropped} The HCP was a target in both NSS ({nss}) and NSR ({nsr}) territories and has Caplyta tier "
                f"{tier}. As per the business rule, {biz}. Therefore, the {dropped_side} record was excluded from "
                f"{OUTPUT}.{keep_txt}")
    if rule == "ONC_VSAP_REMOVAL":
        return (f"{dropped} Territory {terr} is an ONC VSAP territory for {QUARTER} (onc_vsap_q326). As per the business rule, "
                f"all records in VSAP territories are removed. Therefore, the record was excluded from {OUTPUT}.")
    if rule == "IMM_DERM_MIRRORING":
        other, role = ev.get("paired_territory"), ev.get("territory_role")
        other_role = "base" if role == "mirror" else "mirror"
        return (f"{added} The HCP is in the paired {other_role} territory {other} but was missing from this {role} territory. "
                f"As per the business rule, 200-SD customers present in only one territory of a base/mirror pair are copied "
                f"into the other. Therefore, the record was added to {OUTPUT}.")
    if rule == "NOT_IN_ICONNECT":
        others = ev.get("other_territories_in_team", [])
        elsewhere = f" It was sent for {', '.join(others)} instead." if others else ""
        return (f"Customer {kind} {jnj} is not in territory {terr} because iConnect never sent it for this territory in the "
                f"{QUARTER} input files.{elsewhere} No pipeline rule removed it.")
    return m.get("explanation", "")


def kept_reason(r: dict, jnj: str, terr: str) -> str:
    """Why a rule the user suspected did not remove the customer."""
    ev, team = r.get("evidence", {}), terr[:6]
    c = r["check"]
    if c == "check_customer_status" and r["result"] == "NOT_APPLICABLE":
        return (f"Although the account has status {ev.get('customer_status')}, as per the CUSTOMER_STATUS business rule "
                f"only HCPs (PROF) with status INACTV, RETRD or DEAD are removed. Accounts are not affected, so this record was kept.")
    if c == "check_kaiser_affiliation" and r["result"] == "NOT_APPLICABLE":
        names = ", ".join(ev.get("kaiser_affiliations", []))
        return (f"Although the HCP is affiliated with {names or 'a Kaiser account'}, as per the KAISER_AFFILIATION business "
                f"rule only 200-SI (IMAAVY) HCPs are excluded. {team} is not affected, so this record was kept.")
    if c == "check_itci_exclusion" and r["result"] == "NOT_APPLICABLE":
        listed = "is on the ITCI exclusion list" if ev.get("on_itci_list") else "is not on the ITCI exclusion list"
        return (f"The HCP {listed}, but as per the ITCI_EXCLUSION business rule only 106-NA, 106-NB and 106-NR records "
                f"are removed. {team} is not affected, so this record was kept.")
    if c == "check_caplyta_tier" and r["result"] == "NO_MATCH":
        nss, nsr, tier = ev.get("nss_target_territories", []), ev.get("nsr_target_territories", []), ev.get("tier")
        if nss and nsr and tier:
            keep_side = "NSS" if int(tier) <= 3 else "NSR"
            return (f"The HCP is a target in both NSS ({', '.join(nss)}) and NSR ({', '.join(nsr)}) with Caplyta tier {tier}. "
                    f"As per the CAPLYTA_HCP_TIER business rule, customers in tiers {'1-3' if keep_side == 'NSS' else '4-6'} "
                    f"keep their {keep_side} record, so this record was kept.")
        if ev.get("nss_all") and ev.get("nsr_all"):
            return ("The HCP is in both NSS and NSR, but it is not a target in both. As per the CAPLYTA_HCP_TIER business "
                    "rule, only customers who are targets in both are checked, so no record was removed.")
        return ""
    if r["result"] in ("NOT_APPLICABLE", "NO_MATCH"):
        return r.get("explanation", "")
    return ""


def build_answer(state: dict) -> str:
    it, q, checks = state["intake"], state["quality"], state["rules"]["checks"]
    jnj, terr = it.get("jnj_id") or "", it.get("territory") or ""

    if it.get("intent") == "CAPACITY":
        r = next((c["result"] for c in checks if c["tool"] == "check_under_capacity"), {})
        ev, team = r.get("evidence", {}), terr[:6]
        if r.get("result") == "MATCH":
            return (f"Territory {terr} is under capacity due to the UNDER_CAPACITY rule. It has {ev['distinct_targets']} "
                    f"distinct targets on {OUTPUT}, below the {team} minimum of {ev['capacity']}. As per the business rule, "
                    f"under-capacity territories are flagged for review and no customers are added or dropped. Therefore, "
                    f"this territory will be highlighted in the output mail.")
        if r.get("result") == "NO_MATCH":
            return (f"Territory {terr} is not under capacity. It has {ev['distinct_targets']} distinct targets on {OUTPUT}, "
                    f"which meets the {team} minimum of {ev['capacity']}.")
        return r.get("explanation", "No capacity rule applies to this territory.")

    kind = customer_type(jnj) if jnj else "customer"
    paras, first = [], True
    for f in q["findings"]:
        for m in f["explained_by"]:
            paras.append(describe(m, jnj, terr, kind, first))
            first = False
    if any(f["change"] == "NO_CHANGE" for f in q["findings"]):
        present = in_final(jnj, terr)
        paras.append(f"Customer {kind} {jnj} is {'present in' if present else 'not in'} territory {terr} on {OUTPUT} "
                     f"exactly as sent by iConnect. No input rule, rep change or output rule removed or changed it.")
        for r in q["non_matches"]:
            relevant = r["check"] in it.get("suspected_checks", []) or (r["check"] == "check_caplyta_tier" and terr[:6] in NSS | NSR)
            if relevant and "did not reach" not in r["explanation"]:
                txt = kept_reason(r, jnj, terr)
                if txt:
                    paras.append(txt)
    if not paras:
        paras.append("No rule in the pipeline explains this result.")
    if q.get("route_to_analyst"):
        paras.append("This case has been routed to a ZS COR analyst to confirm.")
    return "\n\n".join(dict.fromkeys(paras))


def build_email(state: dict, answer: str) -> dict:
    it = state["intake"]
    jnj, terr = it.get("jnj_id") or "", it.get("territory") or ""
    rules = [m["rule"] for f in state["quality"]["findings"] for m in f["explained_by"]]
    tag = f" – {', '.join(dict.fromkeys(rules))}" if rules else ""
    subject = (f"Call plan query: {customer_type(jnj)} {jnj} in {terr}{tag}" if jnj else f"Call plan query: {terr}{tag}")
    about = f"{customer_type(jnj)} {jnj} in territory {terr}" if jnj else f"territory {terr}"
    body = (f"Hi,\n\nThank you for your query on {about}.\n\n{answer}\n\n"
            "Please let us know if you have any further questions.\n\nBest regards,\nZS COR team")
    return {"email_subject": subject, "email_body": body}


def answer_facts(state: dict) -> dict:
    """Everything the Response agent may use, as structured facts (no free text from the LLM)."""
    it, q, checks = state["intake"], state["quality"], state["rules"]["checks"]
    jnj, terr = it.get("jnj_id") or "", it.get("territory") or ""
    facts = {"quarter": QUARTER, "question": state["question"], "territory": terr, "team": terr[:6],
             "routed_to_analyst": q.get("route_to_analyst", False)}
    if jnj:
        facts.update(customer_id=jnj, customer_type=customer_type(jnj), in_final_output=in_final(jnj, terr))
    if it.get("intent") == "CAPACITY":
        r = next((c["result"] for c in checks if c["tool"] == "check_under_capacity"), {})
        facts["capacity_check"] = {"result": r.get("result"), "evidence": r.get("evidence", {}),
                                   "business_rule": BUSINESS_RULES["UNDER_CAPACITY"]}
        return facts
    events = []
    for f in q["findings"]:
        for m in f["explained_by"]:
            ev = dict(m.get("evidence", {}))
            if m["rule"] == "CAPLYTA_HCP_TIER":  # where the customer still is
                others = ev.get("nss_target_territories", []) + ev.get("nsr_target_territories", [])
                ev["present_on_final_output"] = {t: in_final(jnj, t) for t in others if t != terr}
            if m["rule"] in ("REP_TARGET_CHANGE", "SALES_DIRECTION_CHANGE"):
                b, a = _row("mstr_cppa_srchb_univ", jnj, terr), _row("iconnect_cp_out", jnj, terr)
                if b and a:
                    ev["before"] = {"cp_tgt_flag": b["cp_tgt_flag"], "sales_direction": b["sales_direction"]}
                    ev["after"] = {"cp_tgt_flag": a["cp_tgt_flag"], "sales_direction": a["sales_direction"]}
            events.append({"order": len(events) + 1, "rule": m["rule"], "action": m.get("action"),
                           "pipeline_step": f.get("segment") or m.get("stage"), "tool_finding": m["explanation"],
                           "evidence": ev, "business_rule": BUSINESS_RULES.get(m["rule"], "")})
    facts["events"] = events
    if any(f["change"] == "NO_CHANGE" for f in q["findings"]):
        facts["no_change"] = True
        facts["rules_checked_that_did_not_apply"] = [
            {"rule": r["rule"], "result": r["result"], "tool_finding": r["explanation"], "evidence": r.get("evidence", {})}
            for r in q["non_matches"] if "did not reach" not in r["explanation"]]
    return facts
