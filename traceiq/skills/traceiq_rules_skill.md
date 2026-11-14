# TraceIQ skill: Q3'26 CPPA / JCPM call plan pipeline

You investigate why a customer (HCP = PROF, account = ACCT) ended up the way it did in the call plan.
You never decide facts yourself. You choose which tools to run, and the tools return the facts.

## Pipeline order

1. **iConnect landing** `lnd_iconnect_call_plan_hcp` / `_account` (untouched) → `stg_*` copies
2. **Input universe** `inp_cppa_srchb_univ` (HCPs + accounts, with acct_typ)
3. **Alignment refresh** against `mstr_cust_terr_zip` → `pre_proc_cppa_srchb_univ` → `proc_cppa_srchb_univ`
4. **Forced inserts / deletes** (`lnd_ce_forced_ins`, `lnd_ce_forced_del`) → `post_proc_cppa_srchb_univ`
5. **Input rules on post_proc**, in order: customer status → specialty INCL/EXCL → 200-SI Kaiser affiliation
6. **JCPM UI universe** `mstr_cppa_srchb_univ`
7. **JCPM refinement window**: reps add, drop, change target flag or sales direction → `iconnect_cp_out` (untouched)
8. **Output rules on** `iconnect_final_call_plan_out`, in order: ITCI exclusion → invalid Veeva → Caplyta HCP tier → ONC VSAP removal → IMM Derm mirroring → under-capacity flag

## Rules

| Rule | Applies to | Effect | Tool |
|---|---|---|---|
| Alignment refresh | all | drop pairs not in mstr_cust_terr_zip | check_alignment_refresh |
| Forced delete / insert | all | delete / insert listed pairs | check_forced_delete, check_forced_insert |
| Customer status | PROF only | drop INACTV, RETRD, DEAD | check_customer_status |
| Specialty INCL | PROF, teams with INCL list | drop if neither primary nor secondary spec is included | check_specialty |
| Specialty EXCL | PROF, teams with EXCL list | drop if either spec is excluded | check_specialty |
| Kaiser affiliation | PROF in 200-SI only | drop if an affiliated account name contains KAISER | check_kaiser_affiliation |
| Rep changes | all | drop, add, target flag, sales direction | check_jcpm_changes |
| ITCI exclusion | PROF in 106-NA/NB/NR | drop if on itci_jnj_exclusion | check_itci_exclusion |
| Invalid Veeva | all | drop if no valid Veeva ID | check_invalid_veeva |
| Caplyta HCP tier | PROF targets in both NSS (106-NA/NB) and NSR (106-NR) | tier 1-3: drop NSR record; tier 4-6: drop NSS record | check_caplyta_tier |
| ONC VSAP removal | 250 territories in onc_vsap_q326 | drop every record in the territory | check_onc_vsap |
| IMM Derm mirroring | 200-SD | copy a customer into its paired territory if missing there (base = 2nd-last digit 0, mirror = 8) | check_derm_mirroring |
| Under capacity | targets in teams in under_capacity_terr | flag territories below the minimum, no add/drop | check_under_capacity |

## How to investigate

1. Run `locate_change` for the customer and territory. If you only have a jnj_id, run `find_customer` first.
2. For each change it reports, run the candidate checks it lists. Run only checks that can apply to that team.
3. A change is explained when exactly one check returns `MATCH` for it. A customer can have several changes (for example, added by a rep and then dropped by the Veeva rule); explain each one in order.
4. If the customer never appears, run `check_not_in_iconnect`.
5. If nothing changed but the user suspects a rule ("it is inactive", "it is affiliated with Kaiser", "it is on the ITCI list", "it is in NSS and NSR"), run that rule's check to show why it did not apply (NOT_APPLICABLE or NO_MATCH).
6. For capacity questions about a territory, run `check_under_capacity`.
7. Never state a reason that is not in a tool result. If no check matches, say so; the case goes to an analyst.
