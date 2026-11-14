"""Rule check tools.

Each tool tests ONE documented rule for one customer and returns a fact:
  result = MATCH           the rule explains a change for this record
           NO_MATCH        the rule applies to this record but did not change it
           NOT_APPLICABLE  the rule does not cover this record (wrong team, type, ...)
The agent decides which checks to run; these functions decide whether a rule applied.
"""
from ..access import AccessContext
from ..db import query
from ..pipeline import (BAD_STATUS, NSR, NSS, caplyta_decisions, input_states, kaiser_ids,
                        output_states, present, specialty_drop)


def _r(check, rule, stage, result, action, explanation, **evidence):
    return {"check": check, "rule": rule, "stage": stage, "result": result, "action": action,
            "explanation": explanation, "evidence": evidence}


# ================================================================ input rules
def check_alignment_refresh(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    in_inp = bool(query("SELECT 1 FROM jib_aws.inp_cppa_srchb_univ WHERE jnj_id=? AND territory=?", [jnj_id, territory]))
    valid = bool(query("SELECT 1 FROM jib_aws.mstr_cust_terr_zip WHERE jnj_id=? AND territory=?", [jnj_id, territory]))
    if not in_inp:
        return _r("check_alignment_refresh", "ALIGNMENT_REFRESH", "INPUT", "NOT_APPLICABLE", None,
                  "Customer-territory was not in the input universe.", in_input_universe=False, in_mstr_cust_terr_zip=valid)
    if not valid:
        return _r("check_alignment_refresh", "ALIGNMENT_REFRESH", "INPUT", "MATCH", "DROP",
                  "The customer-territory pair is not in mstr_cust_terr_zip, so the alignment refresh dropped it.",
                  in_input_universe=True, in_mstr_cust_terr_zip=False)
    return _r("check_alignment_refresh", "ALIGNMENT_REFRESH", "INPUT", "NO_MATCH", None,
              "The pair is valid in mstr_cust_terr_zip.", in_input_universe=True, in_mstr_cust_terr_zip=True)


def check_forced_delete(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    listed = bool(query("SELECT 1 FROM jib_aws.lnd_ce_forced_del WHERE jnj_id=? AND territory=?", [jnj_id, territory]))
    was_there = present(input_states()["before_forced_delete"], jnj_id, territory) is not None
    if listed and was_there:
        return _r("check_forced_delete", "FORCED_DELETE", "INPUT", "MATCH", "DROP",
                  "Listed in lnd_ce_forced_del, so it was deleted.", in_forced_del=True)
    return _r("check_forced_delete", "FORCED_DELETE", "INPUT", "NO_MATCH", None,
              "Not deleted by a forced delete.", in_forced_del=listed)


def check_forced_insert(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    listed = bool(query("SELECT 1 FROM jib_aws.lnd_ce_forced_ins WHERE jnj_id=? AND territory=?", [jnj_id, territory]))
    was_there = present(input_states()["before_forced_insert"], jnj_id, territory) is not None
    if listed and not was_there:
        return _r("check_forced_insert", "FORCED_INSERT", "INPUT", "MATCH", "ADD",
                  "Listed in lnd_ce_forced_ins, so it was inserted even though iConnect did not send it.", in_forced_ins=True)
    return _r("check_forced_insert", "FORCED_INSERT", "INPUT", "NO_MATCH", None,
              "Not added by a forced insert.", in_forced_ins=listed)


def check_customer_status(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    rec = present(input_states()["before_customer_status"], jnj_id, territory)
    if not rec:
        return _r("check_customer_status", "CUSTOMER_STATUS", "INPUT", "NOT_APPLICABLE", None,
                  "Record did not reach the customer status rule.")
    ev = {"acct_typ": rec["acct_typ"], "customer_status": rec["customer_status"]}
    if rec["acct_typ"] != "PROF":
        return _r("check_customer_status", "CUSTOMER_STATUS", "INPUT", "NOT_APPLICABLE", None,
                  f"The status rule only applies to HCPs (PROF); this is an account with status {rec['customer_status']}.", **ev)
    if rec["customer_status"] in BAD_STATUS:
        return _r("check_customer_status", "CUSTOMER_STATUS", "INPUT", "MATCH", "DROP",
                  f"HCP with customer status {rec['customer_status']}; INACTV, RETRD and DEAD HCPs are removed.", **ev)
    return _r("check_customer_status", "CUSTOMER_STATUS", "INPUT", "NO_MATCH", None, "HCP is active.", **ev)


def check_specialty(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    rec = present(input_states()["before_specialty"], jnj_id, territory)
    if not rec:
        return _r("check_specialty", "SPECIALTY", "INPUT", "NOT_APPLICABLE", None, "Record did not reach the specialty rule.")
    drop, why = specialty_drop(rec)
    ev = {"primary_spec": rec["primary_spec"], "secondary_spec": rec["secondary_spec"], "team": rec["selling_div_code"]}
    if drop:
        rule = "SPEC_INCL" if "INCL" in why else "SPEC_EXCL"
        return _r("check_specialty", rule, "INPUT", "MATCH", "DROP", f"Dropped: {why}.", **ev)
    result = "NOT_APPLICABLE" if ("PROF only" in why or "no specialty list" in why) else "NO_MATCH"
    return _r("check_specialty", "SPECIALTY", "INPUT", result, None, why[:1].upper() + why[1:] + ".", **ev)


def check_kaiser_affiliation(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    affs = [a["account_name"] for a in query("SELECT account_name FROM jib_aws.jnj_affiliation WHERE jnj_id=?", [jnj_id])]
    kaiser = [a for a in affs if "KAISER" in a.upper()]
    rec = present(input_states()["before_kaiser"], jnj_id, territory)
    if territory[:6] != "200-SI":
        return _r("check_kaiser_affiliation", "KAISER_AFFILIATION", "INPUT", "NOT_APPLICABLE", None,
                  "The Kaiser affiliation rule only applies to 200-SI (IMAAVY) HCPs.", kaiser_affiliations=kaiser)
    if not rec or rec["acct_typ"] != "PROF":
        return _r("check_kaiser_affiliation", "KAISER_AFFILIATION", "INPUT", "NOT_APPLICABLE", None,
                  "Record did not reach the Kaiser rule or is not an HCP.", kaiser_affiliations=kaiser)
    if jnj_id in kaiser_ids():
        return _r("check_kaiser_affiliation", "KAISER_AFFILIATION", "INPUT", "MATCH", "DROP",
                  f"200-SI HCP affiliated with {', '.join(kaiser)}; HCPs affiliated with Kaiser accounts are excluded.",
                  kaiser_affiliations=kaiser)
    return _r("check_kaiser_affiliation", "KAISER_AFFILIATION", "INPUT", "NO_MATCH", None,
              "No affiliated account name contains KAISER.", affiliations=affs)


# ================================================================ JCPM window
def check_jcpm_changes(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    before = query("SELECT * FROM jib_aws.mstr_cppa_srchb_univ WHERE jnj_id=? AND territory=?", [jnj_id, territory])
    after = query("SELECT * FROM jib_aws.iconnect_cp_out WHERE jnj_id=? AND territory=?", [jnj_id, territory])
    events = []
    if before and not after:
        events.append({"rule": "REP_DROP", "action": "DROP", "detail": "rep dropped the customer in the JCPM window"})
    elif after and not before:
        other = [r["territory"] for r in query("SELECT territory FROM jib_aws.mstr_cppa_srchb_univ WHERE jnj_id=? AND selling_div_code=?",
                                               [jnj_id, territory[:6]]) if ctx.can_see(r["territory"])]
        events.append({"rule": "REP_ADD", "action": "ADD",
                       "detail": "rep added the customer in the JCPM window" + (f" (also aligned to {', '.join(other)})" if other else "")})
    elif before and after:
        b, a = before[0], after[0]
        if b["cp_tgt_flag"] != a["cp_tgt_flag"]:
            events.append({"rule": "REP_TARGET_CHANGE", "action": "UPDATE",
                           "detail": f"rep changed target flag {b['cp_tgt_flag']} -> {a['cp_tgt_flag']}"})
        if b["sales_direction"] != a["sales_direction"]:
            events.append({"rule": "SALES_DIRECTION_CHANGE", "action": "UPDATE",
                           "detail": f"sales direction {b['sales_direction']} -> {a['sales_direction']}"})
    if not events:
        return _r("check_jcpm_changes", "JCPM_WINDOW", "JCPM", "NO_MATCH", None, "No rep change in the JCPM window.",
                  in_jcpm_ui=bool(before), in_cp_out=bool(after))
    return _r("check_jcpm_changes", events[0]["rule"], "JCPM", "MATCH", events[0]["action"],
              (lambda t: t[0].upper() + t[1:])("; ".join(e["detail"] for e in events)).replace("jcpm", "JCPM") + ".", events=events,
              in_jcpm_ui=bool(before), in_cp_out=bool(after))


# ================================================================ output rules
def check_itci_exclusion(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    listed = bool(query("SELECT 1 FROM jib_aws.itci_jnj_exclusion WHERE jnj_id=?", [jnj_id]))
    rec = present(output_states()["before_itci"], jnj_id, territory)
    team = territory[:6]
    if team not in NSS | NSR:
        return _r("check_itci_exclusion", "ITCI_EXCLUSION", "OUTPUT", "NOT_APPLICABLE", None,
                  "The ITCI exclusion only applies to 106-NA, 106-NB and 106-NR.", on_itci_list=listed)
    if rec and rec["acct_typ"] == "PROF" and listed:
        return _r("check_itci_exclusion", "ITCI_EXCLUSION", "OUTPUT", "MATCH", "DROP",
                  "The HCP is on the ITCI exclusion list, so it was dropped.", on_itci_list=True)
    return _r("check_itci_exclusion", "ITCI_EXCLUSION", "OUTPUT", "NO_MATCH", None, "Not on the ITCI exclusion list.",
              on_itci_list=listed)


def check_invalid_veeva(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    v = query("SELECT veeva_id FROM jib_aws.mstr_jcpm_employee_veeva WHERE jnj_id=?", [jnj_id])
    rec = present(output_states()["before_veeva"], jnj_id, territory)
    if not rec:
        return _r("check_invalid_veeva", "INVALID_VEEVA", "OUTPUT", "NOT_APPLICABLE", None,
                  "Record did not reach the Veeva check.", has_valid_veeva=bool(v))
    if not v:
        return _r("check_invalid_veeva", "INVALID_VEEVA", "OUTPUT", "MATCH", "DROP",
                  "No valid Veeva ID in mstr_jcpm_employee_veeva, so the record was dropped.", has_valid_veeva=False)
    return _r("check_invalid_veeva", "INVALID_VEEVA", "OUTPUT", "NO_MATCH", None, f"Valid Veeva ID {v[0]['veeva_id']}.",
              has_valid_veeva=True)


def check_caplyta_tier(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    team = territory[:6]
    if team not in NSS | NSR:
        return _r("check_caplyta_tier", "CAPLYTA_HCP_TIER", "OUTPUT", "NOT_APPLICABLE", None,
                  "The Caplyta tier rule only applies to 106-NA, 106-NB and 106-NR.")
    df = output_states()["before_caplyta"]
    me = df[(df.jnj_id == jnj_id) & (df.acct_typ == "PROF")]
    tg = me[me.cp_tgt_flag == "TRUE"]
    in_nss = sorted(set(tg[tg.selling_div_code.isin(NSS)].territory))
    in_nsr = sorted(set(tg[tg.selling_div_code.isin(NSR)].territory))
    all_nss = sorted(set(me[me.selling_div_code.isin(NSS)].territory))
    all_nsr = sorted(set(me[me.selling_div_code.isin(NSR)].territory))
    tier = query("SELECT tier FROM jib_aws.caplyta_hcp_tier WHERE jnj_id=?", [jnj_id])
    tier = tier[0]["tier"] if tier else None
    ev = {"nss_target_territories": in_nss, "nsr_target_territories": in_nsr, "nss_all": all_nss, "nsr_all": all_nsr, "tier": tier}
    decision = caplyta_decisions(df).get((team, territory, jnj_id))
    if decision:
        return _r("check_caplyta_tier", "CAPLYTA_HCP_TIER", "OUTPUT", "MATCH", "DROP",
                  f"Target in both NSS (106-NA/NB) and NSR (106-NR) with Caplyta tier {tier}. "
                  f"For tiers {'1-3 the NSR (106-NR)' if int(tier) <= 3 else '4-6 the NSS (106-NA/NB)'} record is dropped, "
                  f"so this record was removed.", **ev)
    if in_nss and in_nsr and not tier:
        why = "Target in both NSS and NSR but has no tier on file, so no record was dropped (check the tier table)."
    elif in_nss and in_nsr:
        why = f"Target in both NSS and NSR with tier {tier}; the rule keeps this team's record."
    elif (all_nss and all_nsr):
        why = "In both NSS and NSR, but not a target in both, so the tier rule does not apply."
    else:
        why = "Not in both NSS and NSR, so the tier rule does not apply."
    return _r("check_caplyta_tier", "CAPLYTA_HCP_TIER", "OUTPUT", "NO_MATCH", None, why, **ev)


def check_onc_vsap(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    listed = bool(query("SELECT 1 FROM jib_aws.onc_vsap_q326 WHERE territory=?", [territory]))
    if not territory.startswith("250-"):
        return _r("check_onc_vsap", "ONC_VSAP_REMOVAL", "OUTPUT", "NOT_APPLICABLE", None, "VSAP removal only applies to ONC (250) territories.")
    if listed and present(output_states()["before_vsap"], jnj_id, territory):
        return _r("check_onc_vsap", "ONC_VSAP_REMOVAL", "OUTPUT", "MATCH", "DROP",
                  "The whole territory is listed in onc_vsap_q326, so every record in it was removed.", territory_is_vsap=True)
    return _r("check_onc_vsap", "ONC_VSAP_REMOVAL", "OUTPUT", "NO_MATCH", None, "Territory is not a VSAP.", territory_is_vsap=listed)


def check_derm_mirroring(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    if territory[:6] != "200-SD":
        return _r("check_derm_mirroring", "IMM_DERM_MIRRORING", "OUTPUT", "NOT_APPLICABLE", None, "Mirroring only applies to 200-SD.")
    pair = query("SELECT base_terr, mirror_terr FROM jib_aws.imm_derm_mapping WHERE base_terr=? OR mirror_terr=?", [territory, territory])
    if not pair:
        return _r("check_derm_mirroring", "IMM_DERM_MIRRORING", "OUTPUT", "NOT_APPLICABLE", None, "Territory has no mirror pair.")
    p = pair[0]
    other = p["mirror_terr"] if territory == p["base_terr"] else p["base_terr"]
    role = "base" if territory == p["base_terr"] else "mirror"
    before = output_states()["before_mirroring"]
    here, there = present(before, jnj_id, territory), present(before, jnj_id, other)
    ev = {"territory_role": role, "paired_territory": other, "in_this_before": bool(here), "in_pair_before": bool(there)}
    if not here and there:
        return _r("check_derm_mirroring", "IMM_DERM_MIRRORING", "OUTPUT", "MATCH", "ADD",
                  f"Present in paired {'mirror' if role == 'base' else 'base'} territory {other} but not here, so mirroring added it.", **ev)
    return _r("check_derm_mirroring", "IMM_DERM_MIRRORING", "OUTPUT", "NO_MATCH", None, "No mirroring change for this record.", **ev)


def check_under_capacity(ctx: AccessContext, territory: str) -> dict:
    ctx.require(territory)
    cap = query("SELECT capacity FROM jib_aws.under_capacity_terr WHERE team=?", [territory[:6]])
    if not cap:
        return _r("check_under_capacity", "UNDER_CAPACITY", "OUTPUT", "NOT_APPLICABLE", None,
                  f"No minimum capacity is set for {territory[:6]}.")
    n = int(query("""SELECT COUNT(DISTINCT jnj_id) AS n FROM jib_aws.iconnect_final_call_plan_out
                     WHERE territory=? AND cp_tgt_flag='TRUE'""", [territory])[0]["n"])
    c = int(cap[0]["capacity"])
    ev = {"distinct_targets": n, "capacity": c}
    if n < c:
        return _r("check_under_capacity", "UNDER_CAPACITY", "OUTPUT", "MATCH", "FLAG",
                  f"Only {n} distinct targets against a minimum of {c}; flag it in the mail. No add or drop is made.", **ev)
    return _r("check_under_capacity", "UNDER_CAPACITY", "OUTPUT", "NO_MATCH", None, f"{n} distinct targets meets the minimum of {c}.", **ev)


def check_not_in_iconnect(ctx: AccessContext, jnj_id: str, territory: str) -> dict:
    ctx.require(territory)
    team = territory[:6]
    sent_team = query("""SELECT territory FROM jib_aws.lnd_iconnect_call_plan_hcp WHERE jnj_id=? AND selling_div_code=?
                         UNION SELECT territory FROM jib_aws.lnd_iconnect_call_plan_account WHERE jnj_id=? AND selling_div_code=?""",
                      [jnj_id, team, jnj_id, team])
    sent_here = any(r["territory"] == territory for r in sent_team)
    if sent_here:
        return _r("check_not_in_iconnect", "NOT_IN_ICONNECT", "INPUT", "NO_MATCH", None, "iConnect sent this customer-territory.")
    others = [r["territory"] for r in sent_team if ctx.can_see(r["territory"])]
    return _r("check_not_in_iconnect", "NOT_IN_ICONNECT", "INPUT", "MATCH", None,
              "iConnect never sent this customer for this territory" + (f"; it was sent for {', '.join(others)}" if others else " or team") + ".",
              other_territories_in_team=others)
