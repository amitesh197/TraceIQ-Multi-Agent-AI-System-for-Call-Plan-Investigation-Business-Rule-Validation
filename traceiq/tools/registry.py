"""Tool registry: schemas the agents see, and a dispatcher that runs them with access control."""
import json

from .. import tracing
from ..access import AccessContext, AccessDenied
from . import rule_tools as R
from . import trace_tools as TT

_JT = {"jnj_id": {"type": "string", "description": "8-digit JNJ customer ID"},
       "territory": {"type": "string", "description": "14-character territory, e.g. 106-NR-1234567"}}


def _schema(name, desc, params):
    props = {p: _JT[p] for p in params}
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": params}}}


TRACE_TOOLS = {
    "find_customer": (TT.find_customer, ["jnj_id"],
                      "List every team/territory where the customer appears in any pipeline table."),
    "trace_customer": (TT.trace_customer, ["jnj_id", "territory"],
                       "Stage-by-stage record of the customer in one territory, from iConnect landing to the final output."),
    "locate_change": (TT.locate_change, ["jnj_id", "territory"],
                      "Find where the customer was dropped, added or updated between consecutive pipeline tables, "
                      "with the candidate rule checks for each change."),
    "territory_summary": (TT.territory_summary, ["territory"],
                          "Record and distinct target counts for a territory in the JCPM UI, cp_out and the final output."),
}

RULE_TOOLS = {
    "check_alignment_refresh": (R.check_alignment_refresh, ["jnj_id", "territory"], "Input: was the pair dropped by the alignment refresh?"),
    "check_forced_delete": (R.check_forced_delete, ["jnj_id", "territory"], "Input: was it removed by a forced delete?"),
    "check_forced_insert": (R.check_forced_insert, ["jnj_id", "territory"], "Input: was it added by a forced insert?"),
    "check_customer_status": (R.check_customer_status, ["jnj_id", "territory"], "Input: was the HCP dropped for INACTV/RETRD/DEAD status?"),
    "check_specialty": (R.check_specialty, ["jnj_id", "territory"], "Input: was the HCP dropped by the team's specialty INCL/EXCL list?"),
    "check_kaiser_affiliation": (R.check_kaiser_affiliation, ["jnj_id", "territory"], "Input: 200-SI Kaiser affiliation exclusion."),
    "check_jcpm_changes": (R.check_jcpm_changes, ["jnj_id", "territory"], "JCPM window: rep drop, add, target flag or sales direction change."),
    "check_itci_exclusion": (R.check_itci_exclusion, ["jnj_id", "territory"], "Output: ITCI exclusion for 106-NA/NB/NR HCPs."),
    "check_invalid_veeva": (R.check_invalid_veeva, ["jnj_id", "territory"], "Output: dropped for having no valid Veeva ID?"),
    "check_caplyta_tier": (R.check_caplyta_tier, ["jnj_id", "territory"], "Output: Caplyta HCP tier rule for targets in both NSS and NSR."),
    "check_onc_vsap": (R.check_onc_vsap, ["jnj_id", "territory"], "Output: territory removed as an ONC VSAP?"),
    "check_derm_mirroring": (R.check_derm_mirroring, ["jnj_id", "territory"], "Output: 200-SD base/mirror territory mirroring."),
    "check_under_capacity": (R.check_under_capacity, ["territory"], "Output: is the territory below its minimum number of targets? (flag only)"),
    "check_not_in_iconnect": (R.check_not_in_iconnect, ["jnj_id", "territory"], "Was the customer ever sent by iConnect for this territory?"),
}

ALL_TOOLS = {**TRACE_TOOLS, **RULE_TOOLS}


def schemas(names: dict) -> list:
    return [_schema(n, d, p) for n, (_, p, d) in names.items()]


def run_tool(ctx: AccessContext, name: str, args: dict) -> dict:
    """Run a tool by name. Arguments are validated; access is checked inside every tool."""
    if name not in ALL_TOOLS:
        return {"error": f"unknown tool {name}"}
    fn, params, _ = ALL_TOOLS[name]
    try:
        clean = {p: str(args[p]).strip() for p in params}
    except KeyError as e:
        return {"error": f"missing argument {e}"}
    if tracing.enabled():
        fn = tracing.traced(name, run_type="tool",
                            process_inputs=lambda i: {k: v for k, v in i.items() if k != "ctx"})(fn)
    try:
        return fn(ctx, **clean)
    except AccessDenied as e:
        return {"error": "ACCESS_DENIED", "detail": str(e)}
    except Exception as e:  # surface errors to the agent instead of crashing
        return {"error": type(e).__name__, "detail": str(e)}


def short(result: dict, limit: int = 3000) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"
