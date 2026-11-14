"""Pipeline structure and deterministic replays.

The real pipeline updates post_proc and iconnect_final_call_plan_out in place,
so the state *between* rules is not stored. These replays rebuild it from the
stored inputs so each rule check can look at exactly the data that rule saw.
"""
from functools import lru_cache

import pandas as pd

from .db import frame

KEY = ["selling_div_code", "territory", "jnj_id"]
NSS, NSR = {"106-NA", "106-NB"}, {"106-NR"}
BAD_STATUS = {"INACTV", "RETRD", "DEAD"}

# Tables a customer passes through, in order.
TRACE_STAGES = [
    ("lnd", "iConnect landing (lnd_iconnect_call_plan_hcp / _account)"),
    ("stg", "Staging copy (stg_iconnect_call_plan_hcp / _account)"),
    ("inp_cppa_srchb_univ", "Input universe"),
    ("pre_proc_cppa_srchb_univ", "After alignment refresh"),
    ("proc_cppa_srchb_univ", "Processed universe"),
    ("post_proc_cppa_srchb_univ", "After forced ins/del and the 3 input rules"),
    ("mstr_cppa_srchb_univ", "Universe shown in the JCPM UI"),
    ("iconnect_cp_out", "Received after the JCPM window"),
    ("iconnect_final_call_plan_out", "Final iConnect output after the 6 output rules"),
]

# What can explain a change between two consecutive stages.
SEGMENTS = {
    ("stg", "inp_cppa_srchb_univ"): ("UNIVERSE_BUILD", []),
    ("inp_cppa_srchb_univ", "pre_proc_cppa_srchb_univ"): ("ALIGNMENT_REFRESH", ["check_alignment_refresh"]),
    ("proc_cppa_srchb_univ", "post_proc_cppa_srchb_univ"): (
        "FORCED_AND_INPUT_RULES",
        ["check_forced_delete", "check_forced_insert", "check_customer_status", "check_specialty", "check_kaiser_affiliation"]),
    ("mstr_cppa_srchb_univ", "iconnect_cp_out"): ("JCPM_WINDOW", ["check_jcpm_changes"]),
    ("iconnect_cp_out", "iconnect_final_call_plan_out"): (
        "OUTPUT_RULES",
        ["check_itci_exclusion", "check_invalid_veeva", "check_caplyta_tier", "check_onc_vsap", "check_derm_mirroring"]),
}


def _keys(df: pd.DataFrame) -> set:
    return set(map(tuple, df[KEY].values.tolist()))


# ---------------------------------------------------------------- input replay
@lru_cache(maxsize=1)
def input_states() -> dict:
    """Snapshots of the post_proc data before each input step."""
    proc = frame("proc_cppa_srchb_univ")
    inp = frame("inp_cppa_srchb_univ")
    dels = frame("lnd_ce_forced_del")
    ins = frame("lnd_ce_forced_ins")
    states = {"before_forced_delete": proc.copy()}
    del_pairs = set(zip(dels.jnj_id, dels.territory))
    df = proc[[(j, t) not in del_pairs for j, t in zip(proc.jnj_id, proc.territory)]].copy()
    states["before_forced_insert"] = df.copy()
    have = set(zip(df.jnj_id, df.territory))
    post = frame("post_proc_cppa_srchb_univ")
    add_rows = []
    for j, t in zip(ins.jnj_id, ins.territory):
        if (j, t) not in have:
            row = post[(post.jnj_id == j) & (post.territory == t)]
            if len(row):
                add_rows.append(row.iloc[0].to_dict())
            else:  # inserted row was later removed by an input rule: rebuild it from the customer's record
                src = inp[inp.jnj_id == j]
                if len(src):
                    r = src.iloc[0].to_dict(); r.update(territory=t, selling_div_code=t[:6], cp_tgt_flag="TRUE")
                    add_rows.append(r)
    df = pd.concat([df, pd.DataFrame(add_rows)], ignore_index=True) if add_rows else df
    states["before_customer_status"] = df.copy()
    df = df[~((df.acct_typ == "PROF") & (df.customer_status.isin(BAD_STATUS)))]
    states["before_specialty"] = df.copy()
    keep = [not specialty_drop(r)[0] for r in df.to_dict("records")]
    df = df[keep]
    states["before_kaiser"] = df.copy()
    kaiser = kaiser_ids()
    df = df[~((df.selling_div_code == "200-SI") & (df.acct_typ == "PROF") & (df.jnj_id.isin(kaiser)))]
    states["after_input_rules"] = df.copy()
    return states


@lru_cache(maxsize=1)
def spec_rules() -> dict:
    sp = frame("spec_incl_excl")
    out = {}
    for team, g in sp.groupby("team_id"):
        out[team] = {"INCL": set(g[g.incl_excl == "INCL"].spec), "EXCL": set(g[g.incl_excl == "EXCL"].spec)}
    return out


def specialty_drop(r: dict) -> tuple[bool, str]:
    if r.get("acct_typ") != "PROF":
        return False, "specialty rule applies to PROF only"
    rule = spec_rules().get(r["selling_div_code"])
    if not rule:
        return False, f"no specialty list for {r['selling_div_code']}"
    specs = {r["primary_spec"], r["secondary_spec"]}
    if rule["INCL"] and not (specs & rule["INCL"]):
        return True, f"neither {r['primary_spec']} nor {r['secondary_spec']} is in the INCL list {sorted(rule['INCL'])}"
    if rule["EXCL"] and (specs & rule["EXCL"]):
        return True, f"{sorted(specs & rule['EXCL'])} is in the EXCL list {sorted(rule['EXCL'])}"
    return False, "specialties pass the team's list"


@lru_cache(maxsize=1)
def kaiser_ids() -> frozenset:
    a = frame("jnj_affiliation")
    return frozenset(a[a.account_name.str.upper().str.contains("KAISER")].jnj_id)


# ---------------------------------------------------------------- output replay
@lru_cache(maxsize=1)
def output_states() -> dict:
    """Snapshots of iconnect_final_call_plan_out before each output rule."""
    df = frame("iconnect_cp_out")
    states = {"before_itci": df.copy()}
    itci = set(frame("itci_jnj_exclusion").jnj_id)
    df = df[~((df.selling_div_code.isin(NSS | NSR)) & (df.acct_typ == "PROF") & (df.jnj_id.isin(itci)))]
    states["before_veeva"] = df.copy()
    veeva = set(frame("mstr_jcpm_employee_veeva").jnj_id)
    df = df[df.jnj_id.isin(veeva)]
    states["before_caplyta"] = df.copy()
    drops = caplyta_decisions(df)
    df = df[[k not in drops for k in map(tuple, df[KEY].values.tolist())]]
    states["before_vsap"] = df.copy()
    vsap = set(frame("onc_vsap_q326").territory)
    df = df[~df.territory.isin(vsap)]
    states["before_mirroring"] = df.copy()
    pairs = frame("imm_derm_mapping")
    for b, m in zip(pairs.base_terr, pairs.mirror_terr):
        for a, c in ((b, m), (m, b)):
            in_a = df[df.territory == a]
            in_c = set(df[df.territory == c].jnj_id)
            add = in_a[~in_a.jnj_id.isin(in_c)].copy()
            if len(add):
                names = df[df.territory == c].territory_name
                add["territory"] = c
                if len(names):
                    add["territory_name"] = names.iloc[0]
                df = pd.concat([df, add], ignore_index=True)
    states["after_output_rules"] = df.copy()
    return states


def caplyta_decisions(df: pd.DataFrame) -> dict:
    """Records dropped by the Caplyta tier rule -> reason. Works on the state before the rule."""
    tiers = dict(zip(*[frame("caplyta_hcp_tier")[c] for c in ("jnj_id", "tier")]))
    tg = df[(df.acct_typ == "PROF") & (df.cp_tgt_flag == "TRUE")]
    common = set(tg[tg.selling_div_code.isin(NSS)].jnj_id) & set(tg[tg.selling_div_code.isin(NSR)].jnj_id)
    out = {}
    for r in tg[tg.jnj_id.isin(common)].to_dict("records"):
        t = tiers.get(r["jnj_id"])
        if not t:
            continue
        t = int(t)
        if t <= 3 and r["selling_div_code"] in NSR:
            out[(r["selling_div_code"], r["territory"], r["jnj_id"])] = f"tier {t} (1-3): NSR record dropped"
        if t >= 4 and r["selling_div_code"] in NSS:
            out[(r["selling_div_code"], r["territory"], r["jnj_id"])] = f"tier {t} (4-6): NSS record dropped"
    return out


def present(df: pd.DataFrame, jnj_id: str, territory: str) -> dict | None:
    m = df[(df.jnj_id == jnj_id) & (df.territory == territory)]
    return m.iloc[0].to_dict() if len(m) else None
