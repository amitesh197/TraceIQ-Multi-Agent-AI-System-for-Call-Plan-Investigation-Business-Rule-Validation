"""TraceIQ: six agents in a LangGraph workflow.

  Intake -> Access -> Trace -> Rules -> Quality -> Response
  (questions about the user's own history go Intake -> Access -> Memory -> Response)

Memory: the user's access is remembered for the session (and still checked on every
request), follow-up questions reuse the customer and territory from the previous
turn, and past questions are searchable by meaning (ChromaDB on the server). The
Rules agent reads the relevant business rules from the rules knowledge base.

Option C design: the Trace and Rules agents decide WHAT to check and in which
order; the tools in traceiq/tools decide WHETHER a rule applied. Without an
LLM key the same graph runs in offline mode, where a fixed plan stands in for
the agents' choices (useful for testing the tools and for demo fallback).
"""
import json
import os
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from ..access import AccessDenied, load_user
from ..answers import answer_facts, build_answer, build_email
from ..tracing import current_run_id, traced
from .. import vectors
from ..pipeline import SEGMENTS
from ..tools.registry import RULE_TOOLS, TRACE_TOOLS, run_tool, schemas
from .llm import complete, llm_enabled, parse_json, tool_loop

SKILL = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills", "traceiq_rules_skill.md")).read()

RE_JNJ = re.compile(r"\b\d{8}\b")
RE_TERR = re.compile(r"\b\d{3}-[A-Z0-9]{2}-\d{7}\b")
RE_TEAM = re.compile(r"\b\d{3}-[A-Z0-9]{2}\b")
SUSPECT_WORDS = {
    "inactiv": "check_customer_status", "retired": "check_customer_status", "status": "check_customer_status",
    "kaiser": "check_kaiser_affiliation", "itci": "check_itci_exclusion", "veeva": "check_invalid_veeva",
    "nss": "check_caplyta_tier", "nsr": "check_caplyta_tier", "tier": "check_caplyta_tier",
    "vsap": "check_onc_vsap", "mirror": "check_derm_mirroring", "special": "check_specialty",
    "forced": "check_forced_insert", "never sent": "check_not_in_iconnect",
}


QUARTER = "Q3'26"
HISTORY_WORDS = ("did i ask", "have i asked", "i asked", "my questions", "my history", "my previous", "asked before",
                 "my past", "what did i", "show my", "search my")
FOLLOW_UP_WORDS = ("it", "this hcp", "this customer", "that hcp", "that customer", "the hcp", "the customer", "same",
                   "this account", "that one", "this one", "there")


class State(TypedDict, total=False):
    question: str
    user_id: str
    use_llm: bool
    ctx: Any
    context: dict
    history: dict
    intake: dict
    access: dict
    trace: dict
    rules: dict
    quality: dict
    response: dict
    evidence: list
    timings: dict


def _timed(name):
    def deco(fn):
        def wrap(state):
            t = time.time()
            out = fn(state)
            out.setdefault("timings", dict(state.get("timings", {})))
            out["timings"][name] = round(time.time() - t, 3)
            return out
        return wrap
    return deco


def _ckey(name, args):
    return (name, tuple(sorted((a, str(v)) for a, v in args.items())))


def _evidence(state, calls):
    """All tool calls so far, each (tool, arguments) recorded once."""
    out, seen = [], set()
    for c in list(state.get("evidence", [])) + calls:
        k = _ckey(c["tool"], c["args"])
        if k not in seen:
            seen.add(k); out.append(c)
    return out


def _cached_runner(ctx, state, allowed: dict):
    """Run only this agent's tools, each at most once per request (repeats return the earlier result)."""
    cache = {_ckey(c["tool"], c["args"]): c["result"] for c in state.get("evidence", [])}

    def run(name, args):
        if name not in allowed:
            return {"error": "TOOL_NOT_ALLOWED", "detail": f"{name} is not one of this agent's tools: {', '.join(allowed)}"}
        k = _ckey(name, args)
        if k not in cache:
            cache[k] = run_tool(ctx, name, args)
        return cache[k]
    return run


# ================================================================ 1. Intake
def _since_days(q: str):
    ql = q.lower()
    for w, d in (("today", 0), ("yesterday", 1), ("this week", 7), ("last week", 7), ("past week", 7),
                 ("last 2 weeks", 14), ("this month", 31), ("last month", 31)):
        if w in ql:
            return d
    m = re.search(r"last (\d+) days", ql)
    return int(m.group(1)) if m else None


@_timed("intake")
def intake_agent(state: State) -> dict:
    q = state["question"]
    ql = q.lower()
    ctx_prev = state.get("context") or {}
    found = {"jnj_ids": RE_JNJ.findall(q), "territories": RE_TERR.findall(q)}
    teams = [t for t in RE_TEAM.findall(q) if not any(x.startswith(t) for x in found["territories"])]
    intent = "CAPACITY" if "capacity" in ql else "EXPLAIN"
    if any(w in ql for w in HISTORY_WORDS):
        intent = "HISTORY"
    intake = {"jnj_id": (found["jnj_ids"] or [None])[0], "territory": (found["territories"] or [None])[0],
              "team": (teams or [None])[0], "intent": intent, "since_days": _since_days(q),
              "suspected_checks": sorted({c for w, c in SUSPECT_WORDS.items() if w in ql}), "from_memory": []}
    if state.get("use_llm") and intent != "HISTORY":
        prompt = ("Extract from the user's call plan question a JSON object with keys: jnj_id (8 digits or null), "
                  "territory (like 106-NR-1234567 or null), team (like 106-NR or null), intent (one of EXPLAIN, CAPACITY), "
                  "follow_up (true if the question refers to the customer or territory of the previous question), "
                  "suspected_checks (list of tool names from: " + ", ".join(RULE_TOOLS) + " that the user hints at). "
                  "Previous question context: " + json.dumps({k: ctx_prev.get(k) for k in ("question", "jnj_id", "territory")}) +
                  ". Reply with JSON only.")
        out = parse_json(complete([{"role": "system", "content": prompt}, {"role": "user", "content": q}]).choices[0].message.content)
        allowed_ids = set(found["jnj_ids"]) | ({ctx_prev.get("jnj_id")} if ctx_prev.get("jnj_id") else set())
        allowed_terrs = set(found["territories"]) | ({ctx_prev.get("territory")} if ctx_prev.get("territory") else set())
        # grounding: IDs must appear in the question or in the remembered previous turn
        if out.get("jnj_id") in allowed_ids:
            intake["jnj_id"] = out["jnj_id"]
        if out.get("territory") in allowed_terrs:
            intake["territory"] = out["territory"]
        if out.get("intent") in ("EXPLAIN", "CAPACITY"):
            intake["intent"] = out["intent"]
        intake["follow_up"] = bool(out.get("follow_up"))
        intake["suspected_checks"] = sorted(set(intake["suspected_checks"]) |
                                            {c for c in out.get("suspected_checks", []) if c in RULE_TOOLS})
    # conversation memory: fill gaps from the previous turn
    if intake["intent"] != "HISTORY" and ctx_prev:
        words = set(re.findall(r"[a-z]+", ql))
        refers = intake.get("follow_up") or any((w in words) if " " not in w else (w in ql) for w in FOLLOW_UP_WORDS)
        if not intake["jnj_id"] and ctx_prev.get("jnj_id") and intake["intent"] == "EXPLAIN" and (refers or not found["territories"]):
            intake["jnj_id"] = ctx_prev["jnj_id"]
            intake["from_memory"].append("customer")
        if (not intake["territory"] and not intake["team"] and ctx_prev.get("territory")
                and intake["jnj_id"] == ctx_prev.get("jnj_id")):
            intake["territory"] = ctx_prev["territory"]
            intake["from_memory"].append("territory")
        if intake["intent"] == "CAPACITY" and not intake["territory"] and ctx_prev.get("territory"):
            intake["territory"] = ctx_prev["territory"]
            intake["from_memory"].append("territory")
    if intake["jnj_id"] and intake["jnj_id"] not in found["jnj_ids"] and "customer" not in intake["from_memory"]:
        intake["from_memory"].append("customer")
    if intake["territory"] and intake["territory"] not in found["territories"] and "territory" not in intake["from_memory"]:
        intake["from_memory"].append("territory")
    if intake["territory"] and not intake["team"]:
        intake["team"] = intake["territory"][:6]
    return {"intake": intake}


# ================================================================ 2. Access
@_timed("access")
def access_agent(state: State) -> dict:
    ctx, source = state.get("ctx"), "remembered for this session"
    if ctx is None or ctx.user_id != state["user_id"]:
        try:
            ctx, source = load_user(state["user_id"]), "loaded from the user directory"
        except AccessDenied as e:
            return {"access": {"allowed": False, "reason": str(e)}}
    if ctx.is_expired():
        return {"ctx": ctx, "access": {"allowed": False, "reason": f"access for {ctx.name} expired on {ctx.expires}"}}
    scope = state["intake"].get("territory") or state["intake"].get("team")
    if scope and not ctx.can_see(scope):
        return {"ctx": ctx, "access": {"allowed": False, "reason": f"{ctx.name} cannot view {scope[:6]}"}}
    return {"ctx": ctx, "access": {"allowed": True, "user": ctx.name, "role": ctx.role, "source": source,
                                   "checked": "every request"}}


# ================================================================ Memory (history questions)
@_timed("memory")
def memory_agent(state: State) -> dict:
    """Search the signed-in user's own past questions by meaning (never another user's)."""
    it = state["intake"]
    q = state["question"]
    text = re.sub(r"\b(what|did|i|ask|asked|about|show|my|questions|history|previous|last|week|month|today|yesterday|search)\b",
                  " ", q.lower()).strip() or q
    hits = vectors.search_history(state["user_id"], text, k=5, since_days=it.get("since_days"))
    if it.get("jnj_id"):
        exact = [h for h in hits if h.get("jnj_id") == it["jnj_id"]]
        hits = exact or hits
    return {"history": {"query": text, "since_days": it.get("since_days"), "hits": hits}}


# ================================================================ 3. Trace
@_timed("trace")
def trace_agent(state: State) -> dict:
    ctx, it = state["ctx"], state["intake"]
    run = _cached_runner(ctx, state, TRACE_TOOLS)
    calls = []
    if it["intent"] == "CAPACITY":
        if not it.get("territory"):
            return {"trace": {"needs_clarification": "Which territory should I check?"}}
        r = run("territory_summary", {"territory": it["territory"]})
        calls.append({"tool": "territory_summary", "args": {"territory": it["territory"]}, "result": r})
        return {"trace": {"summary": r}, "evidence": _evidence(state, calls)}
    if not it.get("jnj_id"):
        return {"trace": {"needs_clarification": "Which customer (8-digit JNJ ID) is this about?"}}

    if state.get("use_llm"):
        system = SKILL + "\n\nYou are the TRACE agent. Use the trace tools to find where this customer changed. " \
                         "Always call locate_change for the customer and territory. If the territory is unknown, " \
                         "call find_customer first and pick the territory in the user's team. Call each tool at most once. " \
                         "When done, reply with JSON: " \
                         '{"jnj_id": ..., "territory": ..., "notes": "..."}'
        user = json.dumps({"question": state["question"], "intake": it})
        out, calls = tool_loop(system, user, schemas(TRACE_TOOLS), run, max_steps=6)
        if out.get("territory") and RE_TERR.fullmatch(str(out["territory"])):
            it = {**it, "territory": out["territory"]}

    if not it.get("territory"):  # deterministic fallback: resolve the territory from the team
        r = run("find_customer", {"jnj_id": it["jnj_id"]})
        calls.append({"tool": "find_customer", "args": {"jnj_id": it["jnj_id"]}, "result": r})
        terrs = [t for t, v in r.get("territories", {}).items() if not it.get("team") or t.startswith(it["team"])]
        if len(terrs) != 1:
            return {"trace": {"needs_clarification": f"{it['jnj_id']} appears in {len(terrs)} matching territories: "
                                                      f"{', '.join(terrs) or 'none'}. Which one?"},
                    "evidence": _evidence(state, calls)}
        it = {**it, "territory": terrs[0]}

    calls = [c for c in calls if c.get("result", {}).get("error") != "TOOL_NOT_ALLOWED"]
    lc = next((c["result"] for c in calls if c["tool"] == "locate_change"
               and c["args"].get("territory") == it["territory"]), None)
    if lc is None or "error" in lc:
        lc = run("locate_change", {"jnj_id": it["jnj_id"], "territory": it["territory"]})
        calls.append({"tool": "locate_change", "args": {"jnj_id": it["jnj_id"], "territory": it["territory"]}, "result": lc})
    if "error" in lc:
        return {"trace": {"error": lc}, "evidence": _evidence(state, calls)}
    return {"intake": it, "trace": {"locate_change": lc}, "evidence": _evidence(state, calls)}


# ================================================================ 4. Rules
def _offline_plan(it: dict, trace: dict) -> list:
    if it["intent"] == "CAPACITY":
        return [("check_under_capacity", {"territory": it["territory"]})]
    lc = trace["locate_change"]
    args = {"jnj_id": it["jnj_id"], "territory": it["territory"]}
    plan = []
    if not lc["changes"] and not lc.get("in_iconnect_input"):
        plan.append("check_not_in_iconnect")
    for ch in lc["changes"]:
        plan += ch["candidate_checks"]
    plan += it.get("suspected_checks", [])
    if not lc["changes"] and lc.get("in_iconnect_input"):
        # nothing changed: check the rules an analyst would expect to apply to this team
        team = it["territory"][:6]
        plan += ["check_jcpm_changes", "check_customer_status", "check_specialty"]
        if team in ("106-NA", "106-NB", "106-NR"):
            plan += ["check_itci_exclusion", "check_caplyta_tier"]
        if team == "200-SI":
            plan.append("check_kaiser_affiliation")
        if team == "200-SD":
            plan.append("check_derm_mirroring")
        if team.startswith("250-"):
            plan.append("check_onc_vsap")
    seen, out = set(), []
    for p in plan:
        if p not in seen and p != "check_under_capacity":
            seen.add(p); out.append((p, args))
    return out


@_timed("rules")
def rules_agent(state: State) -> dict:
    ctx, it, tr = state["ctx"], state["intake"], state["trace"]
    run = _cached_runner(ctx, state, RULE_TOOLS)
    calls = []
    segs = " ".join(f"{c['change']} in {c['segment']}" for c in tr.get("locate_change", {}).get("changes", []))
    knowledge = vectors.search_rules(f"{state['question']} {segs}", QUARTER, it.get("team"), k=4)
    if state.get("use_llm"):
        kb = "\n".join(f"- [{k['rule']}] {k['text']}" for k in knowledge)
        system = SKILL + ("\n\nRelevant business rules retrieved from the knowledge base for this team and quarter:\n" + kb
                          if kb else "") + "\n\nYou are the RULES agent. Given the trace, decide which rule checks to run and run them. " \
                         "Run every candidate check that can apply to the team for each change, plus checks for any rule " \
                         "the user suspects. Call each check at most once; call them all in one turn when you can. " \
                         "Do not conclude anything a tool did not return. When done reply with JSON: " \
                         '{"explanations": [{"change": "<segment and change>", "check": "<tool name>", "rule": "<rule>"}], ' \
                         '"unexplained": ["..."]}'
        user = json.dumps({"question": state["question"], "intake": it, "trace": tr}, default=str)
        out, calls = tool_loop(system, user, schemas(RULE_TOOLS), run, max_steps=12)
        llm_claims = out.get("explanations", [])
    else:
        llm_claims = None
        for name, args in _offline_plan(it, tr):
            calls.append({"tool": name, "args": args, "result": run(name, args)})
    if it["intent"] == "CAPACITY" and not any(c["tool"] == "check_under_capacity" for c in calls):
        a = {"territory": it["territory"]}
        calls.append({"tool": "check_under_capacity", "args": a, "result": run("check_under_capacity", a)})
    seen, unique = set(), []
    for c in calls:
        k = _ckey(c["tool"], c["args"])
        if k not in seen and c.get("result", {}).get("error") != "TOOL_NOT_ALLOWED":
            seen.add(k); unique.append(c)
    return {"rules": {"checks": unique, "llm_claims": llm_claims, "knowledge": knowledge}, "evidence": _evidence(state, unique)}


# ================================================================ 5. Quality
SEG_OF_CHECK = {c: seg for (_, _), (seg, cs) in SEGMENTS.items() for c in cs}
SEG_OF_CHECK["check_not_in_iconnect"] = "NOT_IN_PIPELINE"
SEG_OF_CHECK["check_under_capacity"] = "CAPACITY"


@_timed("quality")
def quality_agent(state: State) -> dict:
    it, tr, checks = state["intake"], state.get("trace", {}), state["rules"]["checks"]
    results = [c["result"] for c in checks if isinstance(c.get("result"), dict) and "check" in c["result"]]
    errors = [c for c in checks if "error" in c.get("result", {})]
    matches = [r for r in results if r["result"] == "MATCH"]
    findings, issues = [], []
    if it["intent"] == "CAPACITY":
        findings = [{"change": "CAPACITY", "explained_by": matches}]
    else:
        lc = tr.get("locate_change", {})
        for ch in lc.get("changes", []):
            cands = set(ch["candidate_checks"])
            hit = [m for m in matches if m["check"] in cands and
                   (m["action"] == {"DROPPED": "DROP", "ADDED": "ADD", "UPDATED": "UPDATE"}[ch["change"]])]
            findings.append({"change": f"{ch['change']} between {ch['between'][0]} and {ch['between'][1]}",
                             "segment": ch["segment"], "explained_by": hit})
            if not hit:
                issues.append(f"no rule explains {ch['change']} in {ch['segment']}")
            elif len(hit) > 1:
                issues.append(f"more than one rule matches {ch['change']} in {ch['segment']}")
        if not lc.get("changes"):
            findings.append({"change": "NO_CHANGE" if lc.get("in_iconnect_input") else "NOT_IN_PIPELINE",
                             "explained_by": [m for m in matches if m["check"] == "check_not_in_iconnect"]})
    # groundedness of the LLM's own conclusions: every cited check must have returned MATCH
    claims = state["rules"].get("llm_claims")
    grounded = None
    if claims is not None:
        ok = [c for c in claims if any(m["check"] == c.get("check") for m in matches)]
        grounded = round(len(ok) / len(claims), 2) if claims else 1.0
        if grounded < 1:
            issues.append("the Rules agent cited a rule that no tool confirmed")
    if errors:
        issues.append(f"{len(errors)} tool call(s) failed")
    confidence = "HIGH" if not issues else ("MEDIUM" if all("more than one" in i for i in issues) else "LOW")
    return {"quality": {"findings": findings, "non_matches": [r for r in results if r["result"] != "MATCH"],
                        "issues": issues, "confidence": confidence, "groundedness": grounded,
                        "route_to_analyst": confidence == "LOW"}}


# ================================================================ 6. Response
def it_get(state, k):
    return state.get("intake", {}).get(k) or ""


def _template(state: State) -> dict:
    it, q = state["intake"], state["quality"]
    who, terr = it.get("jnj_id") or "", it.get("territory") or ""
    lines = []
    if it.get("intent") == "CAPACITY":
        lines += [c["result"]["explanation"] for c in state["rules"]["checks"]
                  if c["tool"] == "check_under_capacity" and "explanation" in c["result"]]
    else:
        for f in q["findings"]:
            lines += [m["explanation"] for m in f["explained_by"]]
        if any(f["change"] == "NO_CHANGE" for f in q["findings"]):
            lines.append(f"{who} was sent by iConnect for {terr} and no rule or rep change removed or altered it.")
            for r in q["non_matches"]:
                relevant = r["check"] in it.get("suspected_checks", []) or r["check"] == "check_caplyta_tier"
                if relevant and "did not reach" not in r["explanation"]:
                    lines.append(r["explanation"])
    if not lines:
        lines.append("No rule in the pipeline explains this result.")
    if q["route_to_analyst"]:
        lines.append("This case has been routed to a ZS COR analyst to confirm.")
    answer = " ".join(dict.fromkeys(lines))
    subject = f"Call plan query: {who} in {terr}" if who else f"Call plan query: {terr}"
    email = (f"Hi,\n\nThanks for your question about {who + ' in ' if who else ''}{terr}.\n\n{answer}"
             "\n\nBest regards,\nZS COR team")
    return {"answer": answer, "email_subject": subject, "email_body": email}


ONE_SHOT_FACTS = {
    "quarter": "Q3'26", "question": "Why was 12345678 dropped from 106-NR-1234567?", "territory": "106-NR-1234567",
    "team": "106-NR", "customer_id": "12345678", "customer_type": "HCP", "in_final_output": False,
    "events": [{"order": 1, "rule": "CAPLYTA_HCP_TIER", "action": "DROP", "pipeline_step": "OUTPUT_RULES",
                "evidence": {"nss_target_territories": ["106-NA-7654321"], "nsr_target_territories": ["106-NR-1234567"],
                             "tier": "2", "present_on_final_output": {"106-NA-7654321": True}},
                "business_rule": "For HCPs that are targets in both NSS (106-NA/NB) and NSR (106-NR): tiers 1-3 keep the "
                                 "NSS record and the NSR record is removed; tiers 4-6 keep the NSR record and the NSS record is removed."}]}
ONE_SHOT_ANSWER = ("Customer HCP 12345678 was dropped from territory 106-NR-1234567 due to the CAPLYTA_HCP_TIER rule. "
                   "The HCP was present as a target in both the NSS territory 106-NA-7654321 and the NSR territory "
                   "106-NR-1234567, and has Caplyta tier 2. As per the business rule, NSR records for customers in tiers "
                   "1-3 are removed when the customer is also a target in NSS. Therefore, the NSR record was excluded from "
                   "the Q3'26 iConnect Output. The NSS record in 106-NA-7654321 is present on the Q3'26 iConnect Output.")
RE_RULE = re.compile(r"\b[A-Z]+(?:_[A-Z0-9]+)+\b")
RE_NUM = re.compile(r"\b\d+(?:-\d+)?\b")

RESPONSE_PROMPT = (
    "You are the RESPONSE agent for the ZS COR call plan team. You receive verified facts as JSON. Write the answer a "
    "ZS analyst would send to a sales rep or client.\n"
    "Structure for each event, in order: (1) what happened to the customer in which territory and the rule name, "
    "(2) the evidence, (3) 'As per the business rule, ...' in plain words, (4) 'Therefore, ...' the outcome on the "
    "quarter's JCPM universe or iConnect Output, (5) where the customer still remains, if the facts show it. Use one "
    "paragraph per event. If no_change is true, say the customer is on the output as sent by iConnect, then explain why "
    "each rule in rules_checked_that_did_not_apply did not remove it. For capacity_check, state the target count, the "
    "minimum and whether it is flagged. If routed_to_analyst is true, end by saying an analyst will confirm.\n"
    "Rules: use ONLY the facts given. Use IDs, territories, rule names, tiers and counts exactly as written; add none. "
    "No internal words like MATCH, segment or tool. Professional, clear, concise.\n"
    "Reply with JSON only: {\"answer\": \"...\"}\n\n"
    "Example facts:\n" + json.dumps(ONE_SHOT_FACTS) + "\n\nExample answer:\n" + ONE_SHOT_ANSWER)


def _key_facts(text: str) -> set:
    """IDs, territories, rule names and numbers in a text."""
    t = RE_TERR.sub(" ", text)
    return set(RE_JNJ.findall(text)) | set(RE_TERR.findall(text)) | set(RE_RULE.findall(text)) | set(RE_NUM.findall(RE_JNJ.sub(" ", t)))


def _check_answer(answer: str, facts: dict, verified: str) -> list:
    """Problems with an LLM-written answer: invented facts or missing essentials."""
    allowed = _key_facts(json.dumps(facts, default=str)) | _key_facts(verified) | {"26"}
    found = _key_facts(answer)
    problems = [f"'{x}' is not in the facts" for x in sorted(found - allowed)]
    must = {facts.get("customer_id"), facts.get("territory")} | {e["rule"] for e in facts.get("events", [])}
    problems += [f"missing '{x}'" for x in sorted(m for m in must if m) if x not in answer]
    return problems


def _history_answer(state: State) -> dict:
    h = state.get("history", {})
    hits = h.get("hits", [])
    when = {None: "", 0: " today", 1: " since yesterday", 7: " in the last week", 14: " in the last two weeks",
            31: " in the last month"}.get(h.get("since_days"), f" in the last {h.get('since_days')} days")
    if not hits:
        return {"answer": f"I couldn't find any earlier questions of yours matching that{when}.", "status": "HISTORY", "hits": []}
    lines = [f"Here is what you asked{when} that matches:"]
    for x in hits:
        q, a = (x["text"].split("\nAnswer: ", 1) + [""])[:2]
        first = a.split(". ")[0].strip().rstrip(".")
        lines.append(f"- {x['time'][:16].replace('T', ' ')}: {q.replace('Question: ', '')} Answer: {first}.")
    return {"answer": "\n".join(lines), "status": "HISTORY", "hits": hits}


@_timed("response")
def response_agent(state: State) -> dict:
    if not state.get("access", {}).get("allowed", False):
        return {"response": {"answer": f"Access denied: {state['access']['reason']}.", "status": "DENIED"}}
    if state.get("intake", {}).get("intent") == "HISTORY":
        return {"response": _history_answer(state)}
    if state.get("trace", {}).get("needs_clarification"):
        return {"response": {"answer": state["trace"]["needs_clarification"], "status": "CLARIFY"}}
    if state.get("trace", {}).get("error"):
        return {"response": {"answer": f"Could not trace this request: {state['trace']['error']}", "status": "ERROR"}}
    verified = build_answer(state)          # rule-by-rule answer from the tool results
    status = "ROUTED_TO_ANALYST" if state["quality"]["route_to_analyst"] else "ANSWERED"
    answer, writer, review = verified, "verified template", []
    if state.get("use_llm"):
        facts = answer_facts(state)
        messages = [{"role": "system", "content": RESPONSE_PROMPT},
                    {"role": "user", "content": json.dumps(facts, default=str)}]
        for attempt in (1, 2):  # write, and if the fact check fails, revise once with the feedback
            try:
                reply = complete(messages).choices[0].message.content
                cand = (parse_json(reply).get("answer") or "").strip()
            except Exception as e:
                review.append(f"attempt {attempt}: LLM call failed ({type(e).__name__})")
                break
            problems = _check_answer(cand, facts, verified) if cand else ["empty answer"]
            if not problems:
                answer, writer = cand, f"LLM (attempt {attempt})"
                break
            review.append(f"attempt {attempt}: " + "; ".join(problems))
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "Your answer failed the fact check: " + "; ".join(problems) +
                          ". Rewrite it using only the facts given, keeping the same structure. Reply with JSON only."}]
    resp = {"answer": answer, **build_email(state, answer), "status": status, "written_by": writer}
    mem = state.get("intake", {}).get("from_memory")
    if mem:
        it = state["intake"]
        resp["memory_note"] = ("Using " + " and ".join(
            f"{'customer ' + it['jnj_id'] if m == 'customer' else 'territory ' + it['territory']}" for m in mem)
            + " from your previous question.")
    if review:
        resp["fact_check"] = review
    return {"response": resp}


# ================================================================ graph
def build_graph():
    g = StateGraph(State)
    for name, fn in (("intake", intake_agent), ("access", access_agent), ("memory", memory_agent), ("trace", trace_agent),
                     ("rules", rules_agent), ("quality", quality_agent), ("response", response_agent)):
        g.add_node(name, fn)
    g.set_entry_point("intake")
    g.add_edge("intake", "access")
    g.add_conditional_edges("access", lambda s: "response" if not s["access"]["allowed"]
                            else ("memory" if s["intake"].get("intent") == "HISTORY" else "trace"))
    g.add_edge("memory", "response")
    g.add_conditional_edges("trace", lambda s: "response" if (s["trace"].get("needs_clarification") or s["trace"].get("error")) else "rules")
    g.add_edge("rules", "quality")
    g.add_edge("quality", "response")
    g.add_edge("response", END)
    return g.compile()


_GRAPH = None


def _summary(out: dict) -> dict:
    r = out.get("response", {})
    return {"answer": r.get("answer"), "status": r.get("status"), "written_by": r.get("written_by"),
            "confidence": out.get("quality", {}).get("confidence"),
            "groundedness": out.get("quality", {}).get("groundedness"),
            "rules": [m["rule"] for f in out.get("quality", {}).get("findings", []) for m in f["explained_by"]],
            "fact_check": r.get("fact_check"), "timings": out.get("timings")}


@traced("TraceIQ", run_type="chain", process_outputs=_summary,
        process_inputs=lambda i: {"question": i.get("question"), "user_id": i.get("user_id"), "use_llm": i.get("use_llm"),
                                  "context": i.get("context")})
def ask(question: str, user_id: str = "cor_analyst", use_llm: bool | None = None,
        ctx=None, context: dict | None = None) -> dict:
    """Answer one question.

    ctx      the user's AccessContext remembered at sign-in (re-checked on every request)
    context  the previous turn in this conversation ({question, jnj_id, territory}) for follow-ups
    """
    global _GRAPH
    _GRAPH = _GRAPH or build_graph()
    use_llm = llm_enabled() if use_llm is None else use_llm
    init = {"question": question, "user_id": user_id, "use_llm": use_llm, "evidence": [], "timings": {},
            "context": context or {}}
    if ctx is not None:
        init["ctx"] = ctx
    out = _GRAPH.invoke(init, config={"run_name": "TraceIQ agents"})
    out["run_id"] = current_run_id()
    return out
