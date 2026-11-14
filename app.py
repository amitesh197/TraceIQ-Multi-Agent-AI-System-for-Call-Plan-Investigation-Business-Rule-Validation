"""TraceIQ web app (runs on the server; users open it in their browser).

  streamlit run app.py
  # as a shared website:  streamlit run app.py --server.address 0.0.0.0 --server.port 8501

Sign in -> Ask (chat with follow-ups) | My history (search by meaning) | Rules library |
Review queue and Test results (analysts only).
"""
import datetime as dt
import html
import json
import urllib.parse

import pandas as pd
import streamlit as st

from traceiq import memory, tracing, vectors
from traceiq.access import list_users, load_user, verify_password
from traceiq.agents.graph import ask
from traceiq.agents.llm import MODEL, llm_enabled
from traceiq.db import source_description
from traceiq.tools.trace_tools import trace_customer

STAGE_NAMES = {
    "lnd": "iConnect input", "stg": "Staging", "inp_cppa_srchb_univ": "Input universe",
    "pre_proc_cppa_srchb_univ": "Alignment refresh", "proc_cppa_srchb_univ": "Processed",
    "post_proc_cppa_srchb_univ": "Input rules", "mstr_cppa_srchb_univ": "JCPM UI",
    "iconnect_cp_out": "After JCPM window", "iconnect_final_call_plan_out": "Final iConnect Output",
}
STARTERS = [
    "Why was 18146677 dropped from 106-NR-9396532?",
    "The rep added 69772426 to 200-GS-6160088. Why is it not in the final plan?",
    "Why is 80868286 in 200-SD-3010489?",
    "Is territory 250-OK-5713207 under capacity?",
]
STATUS = {"ANSWERED": ("Answered", "ok"), "ROUTED_TO_ANALYST": ("Sent to an analyst", "route"),
          "DENIED": ("Access denied", "deny"), "CLARIFY": ("Needs more detail", "ask"),
          "ERROR": ("Could not trace", "deny"), "HISTORY": ("From your history", "ask")}

st.set_page_config(page_title="TraceIQ", page_icon="🔎", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, input, textarea, button { font-family: 'Inter', system-ui, sans-serif; }
:root { --ink:#0B192C; --slate:#5B6B80; --line:#E2E8F0; --keep:#059669; --drop:#E11D48; --accent:#0E7490; --soft:#F1F5F9; }
.block-container { max-width: 1120px; padding-top: 1.6rem; padding-bottom: 6rem; }
[data-testid="stMarkdownContainer"] p.brand { font-size: 2.1rem; font-weight: 700; color: var(--ink); letter-spacing: -0.02em; margin: 0; line-height: 1.2; }
[data-testid="stMarkdownContainer"] p.brand.sm { font-size: 1.5rem; }
[data-testid="stMarkdownContainer"] p.tagline { color: var(--slate); margin: 0.15rem 0 1.1rem 0; font-size: 1rem; }
.pill { display:inline-block; padding: 0.12rem 0.55rem; border-radius: 6px; font-weight:600; font-size:0.8rem; margin-right:0.5rem; }
.pill.ok { background:#DCFCE7; color:#065F46; } .pill.route { background:#FEF3C7; color:#92400E; }
.pill.deny { background:#FFE4E6; color:#9F1239; } .pill.ask { background:#E0F2FE; color:#075985; }
.meta { color: var(--slate); font-size: 0.85rem; }
.answer { color: var(--ink); font-size: 1.02rem; line-height: 1.65; max-width: 78ch; white-space: pre-wrap; margin: 0.45rem 0 0.4rem 0; }
.memo { color: var(--accent); font-size: 0.85rem; margin-bottom: 0.3rem; }
.path { display:flex; flex-wrap:wrap; gap:0; margin: 0.2rem 0 0.6rem 0; }
.stage { flex: 1 1 0; min-width: 74px; padding: 0.5rem 0.4rem; border-top: 4px solid var(--line); }
.stage .n { font-size: 0.76rem; color: var(--slate); line-height: 1.25; }
.stage .v { font-size: 0.86rem; font-weight: 600; color: var(--ink); margin-top: 0.2rem; }
.stage.in { border-top-color: var(--keep); } .stage.out .v { color: #94A3B8; font-weight: 500; }
.stage.event { background: #FFF1F2; border-top-color: var(--drop); flex-grow: 1.4; }
.stage.event.add { background: #ECFDF5; border-top-color: var(--keep); }
.stage .why { font-size: 0.76rem; color: var(--drop); margin-top: 0.25rem; font-weight: 600; overflow-wrap: anywhere; }
.stage.event.add .why { color: var(--keep); }
table.checks { border-collapse: collapse; width: 100%; }
.checks td, .checks th { font-size: 0.86rem; padding: 0.32rem 0.6rem 0.32rem 0; text-align:left; vertical-align: top; border-bottom: 1px solid var(--line); }
.checks th { color: var(--slate); font-weight: 600; }
.r-MATCH { color: var(--drop); font-weight: 700; } .r-NO_MATCH { color: var(--slate); } .r-NOT_APPLICABLE { color: #94A3B8; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 0.8rem 0.95rem; margin-bottom: 0.6rem; background: #fff; }
.card .t { font-weight: 600; color: var(--ink); } .card .s { color: var(--slate); font-size: 0.82rem; margin: 0.1rem 0 0.35rem 0; }
.card .b { color: var(--ink); font-size: 0.92rem; line-height: 1.55; white-space: pre-wrap; }
.who { font-weight: 600; color: var(--ink); } .small { color: var(--slate); font-size: 0.82rem; line-height: 1.45; }
</style>
""", unsafe_allow_html=True)

USERS = list_users()


# ================================================================ sign-in
def sign_in_view():
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown("<div style='height:6vh'></div><p class='brand'>TraceIQ</p>"
                    "<p class='tagline'>Sign in to trace call plan questions.</p>", unsafe_allow_html=True)
        with st.form("signin"):
            uid = st.text_input("User ID", placeholder="e.g. cor_analyst")
            pw = st.text_input("Password", type="password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            uid = uid.strip()
            if uid in USERS and verify_password(uid, pw):
                ctx = load_user(uid)
                if ctx.is_expired():
                    st.error(f"Access for {ctx.name} expired on {ctx.expires}. Ask the ZS COR team to renew it.")
                else:
                    memory.save_profile(ctx)
                    st.session_state.update(user_id=uid, ctx=ctx, messages=[], conv={})
                    st.rerun()
            else:
                st.error("User ID or password is incorrect.")
        with st.expander("Demo accounts"):
            st.markdown("`cor_analyst` / `cor-demo-2026`: ZS COR analyst, all teams  \n"
                        "`imm_team_member` / `imm-demo-2026`: IMM teams only  \n"
                        "`ns_team_member` / `ns-demo-2026`: NS teams only")


if "user_id" not in st.session_state:
    sign_in_view()
    st.stop()

user_id, ctx = st.session_state["user_id"], st.session_state["ctx"]
is_analyst = ctx.role == "analyst"


# ================================================================ helpers
def trace_path(out) -> str:
    it = out.get("intake", {})
    jnj, terr = it.get("jnj_id"), it.get("territory")
    if not (jnj and terr) or not out.get("access", {}).get("allowed"):
        return ""
    try:
        stages = trace_customer(ctx, jnj, terr)["stages"]
    except Exception:
        return ""
    events = {}
    for f in out.get("quality", {}).get("findings", []):
        if " between " in f.get("change", ""):
            kind, rest = f["change"].split(" between ", 1)
            events[rest.split(" and ")[1]] = (kind, ", ".join(m["rule"] for m in f["explained_by"]) or "unexplained")
    cells = []
    for s in stages:
        cls, val, why = ("in" if s["present"] else "out"), ("Present" if s["present"] else "Not present"), ""
        if s["stage"] in events:
            kind, rules = events[s["stage"]]
            cls += " event" + (" add" if kind == "ADDED" else "")
            label = {"ADDED": "Added", "DROPPED": "Dropped"}.get(kind, "Changed")
            why = f"<div class='why'>{label}: {html.escape(rules)}</div>"
        if s["present"] and s["record"]:
            val += ", target" if s["record"]["cp_tgt_flag"] == "TRUE" else ", non-target"
        cells.append(f"<div class='stage {cls}'><div class='n'>{STAGE_NAMES.get(s['stage'], s['stage'])}</div>"
                     f"<div class='v'>{val}</div>{why}</div>")
    return "<div class='path'>" + "".join(cells) + "</div>"


def render_answer(out: dict, key: str):
    r = out["response"]
    label, cls = STATUS.get(r.get("status"), (r.get("status"), "ask"))
    conf = out.get("quality", {}).get("confidence")
    total = sum(out.get("timings", {}).values())
    meta = ", ".join(x for x in [f"confidence {conf}" if conf and r.get("status") != "HISTORY" else "",
                                  f"written by {r['written_by']}" if r.get("written_by") else "", f"{total:.1f} s"] if x)
    st.markdown(f"<span class='pill {cls}'>{label}</span><span class='meta'>{html.escape(meta)}</span>", unsafe_allow_html=True)
    if r.get("memory_note"):
        st.markdown(f"<div class='memo'>{html.escape(r['memory_note'])}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='answer'>{html.escape(r.get('answer', ''))}</div>", unsafe_allow_html=True)
    for line in r.get("fact_check", []):
        st.caption(f"Fact check: {line}")
    if r.get("status") == "HISTORY":
        return
    path = trace_path(out)
    if path:
        with st.expander("Trace path", expanded=True):
            st.markdown(path, unsafe_allow_html=True)
    if r.get("email_body"):
        with st.expander("Draft email"):
            st.text_input("Subject", r["email_subject"], key=f"subj-{key}")
            body = st.text_area("Body", r["email_body"], height=260, key=f"body-{key}")
            st.link_button("Open in email",
                           f"mailto:?subject={urllib.parse.quote(r['email_subject'])}&body={urllib.parse.quote(body)}")
    checks = [c["result"] for c in out.get("rules", {}).get("checks", []) if "check" in c.get("result", {})]
    if checks or out.get("timings"):
        with st.expander("How it decided"):
            t = out.get("timings", {})
            steps = [("Intake", "intake"), ("Access", "access"), ("Trace", "trace"), ("Rules", "rules"),
                     ("Quality", "quality"), ("Response", "response")]
            st.markdown("  →  ".join(f"**{n}** {t[k]:.1f} s" for n, k in steps if k in t))
            src = out.get("access", {}).get("source")
            if src:
                st.caption(f"Access {src}, checked on every request.")
            if checks:
                rows = "".join(f"<tr><td>{html.escape(x['rule'])}</td><td class='r-{x['result']}'>"
                               f"{x['result'].replace('_', ' ').lower()}</td><td>{html.escape(x['explanation'])}</td></tr>"
                               for x in checks)
                st.markdown(f"<table class='checks'><tr><th>Rule</th><th>Result</th><th>Finding</th></tr>{rows}</table>",
                            unsafe_allow_html=True)
            kb = out.get("rules", {}).get("knowledge", [])
            if kb:
                st.markdown("**Business rules consulted**")
                st.markdown("\n".join(f"- {k['title']}" for k in kb))
    with st.expander("Evidence"):
        for c in out.get("evidence", []):
            st.markdown(f"**{c['tool']}** `{json.dumps(c['args'])}`")
            st.json(c["result"], expanded=False)


def run_question(q: str):
    use_ai = st.session_state.get("use_ai", llm_enabled())
    with st.spinner("Tracing through the pipeline..."):
        out = ask(q, user_id, use_llm=use_ai, ctx=ctx, context=st.session_state["conv"])
    r, it = out["response"], out.get("intake", {})
    turn = memory.add_turn(user_id, {
        "question": q, "answer": r.get("answer"), "status": r.get("status"),
        "confidence": out.get("quality", {}).get("confidence"), "written_by": r.get("written_by"),
        "jnj_id": it.get("jnj_id"), "territory": it.get("territory"),
        "rules": [m["rule"] for f in out.get("quality", {}).get("findings", []) for m in f["explained_by"]],
        "review": "Pending" if r.get("status") in ("ANSWERED", "ROUTED_TO_ANALYST") else "Not needed",
        "review_note": "", "run_id": out.get("run_id")})
    vectors.add_history(user_id, turn)
    if r.get("status") in ("ANSWERED", "ROUTED_TO_ANALYST") and (it.get("jnj_id") or it.get("territory")):
        st.session_state["conv"] = {"question": q, "jnj_id": it.get("jnj_id"), "territory": it.get("territory")}
    st.session_state["messages"].append({"q": q, "out": out, "id": turn["id"]})


# ================================================================ sidebar
with st.sidebar:
    st.markdown("<p class='brand sm'>TraceIQ</p>", unsafe_allow_html=True)
    teams = "All teams" if ctx.all_teams else ", ".join(sorted(ctx.teams))
    st.markdown(f"<div class='who'>{html.escape(ctx.name)}</div><div class='small'>{html.escape(teams)}"
                + (f"<br>Access until {ctx.expires}" if ctx.expires else "") + "</div>", unsafe_allow_html=True)
    st.caption("Access is remembered for this session and checked on every request.")
    c1, c2 = st.columns(2)
    if c1.button("New chat", width="stretch"):
        st.session_state.update(messages=[], conv={})
        st.rerun()
    if c2.button("Sign out", width="stretch"):
        for k in ("user_id", "ctx", "messages", "conv"):
            st.session_state.pop(k, None)
        st.rerun()
    st.divider()
    st.markdown("**Recent questions**")
    seen_q, recent = set(), []
    for t in memory.recent_turns(user_id, 20):
        if t.get("status") != "HISTORY" and t["question"] not in seen_q:
            seen_q.add(t["question"]); recent.append(t)
    recent = recent[:6]
    if not recent:
        st.caption("Your questions will appear here.")
    for t in recent:
        if st.button(t["question"], key=f"recent-{t['id']}", width="stretch"):
            st.session_state["pending"] = t["question"]
    st.divider()
    with st.expander("Settings and status"):
        ai_ok = llm_enabled()
        st.session_state["use_ai"] = st.toggle("Use AI agents", value=ai_ok, disabled=not ai_ok)
        st.caption(f"Model: {MODEL}" if ai_ok else "No LLM key in .env: agents follow a fixed plan.")
        st.caption(f"Data: {source_description()}")
        st.caption(f"Memory: {memory.status()}")
        st.caption(f"Vector search: ChromaDB, {vectors.provider_note()}")
        st.caption("LangSmith tracing: on" if tracing.enabled() else "LangSmith tracing: off")

st.markdown("<p class='brand'>TraceIQ</p><p class='tagline'>Ask why a customer was dropped, added or changed in the "
            "Q3'26 call plan. Every answer is traced through the pipeline and backed by the exact rule.</p>",
            unsafe_allow_html=True)
tab_names = ["Ask", "My history", "Rules library"] + (["Review queue", "Test results"] if is_analyst else [])
tabs = dict(zip(tab_names, st.tabs(tab_names)))

# ================================================================ Ask
with tabs["Ask"]:
    for m in st.session_state["messages"]:
        with st.chat_message("user"):
            st.markdown(m["q"])
        with st.chat_message("assistant", avatar="🔎"):
            render_answer(m["out"], m["id"])
    q = st.session_state.pop("pending", None)
    if not st.session_state["messages"] and not q:
        st.markdown("**Try one of these**")
        cols = st.columns(2)
        for i, s in enumerate(STARTERS):
            if cols[i % 2].button(s, key=f"start-{i}", width="stretch"):
                q = s
    typed = st.chat_input("Ask about a customer or territory. Follow-ups like \"is it still in 106-NA?\" work too.")
    q = typed or q
    if q:
        run_question(q.strip())
        st.rerun()

# ================================================================ My history
with tabs["My history"]:
    c1, c2 = st.columns([3, 1])
    term = c1.text_input("Search your questions by meaning", placeholder="e.g. Caplyta tier drops, Kaiser, mirroring")
    period = c2.selectbox("Period", ["Any time", "Today", "Last 7 days", "Last 30 days"])
    days = {"Any time": None, "Today": 0, "Last 7 days": 7, "Last 30 days": 30}[period]
    if term.strip():
        hits = vectors.search_history(user_id, term.strip(), k=8, since_days=days)
        if not hits:
            st.info("No matching questions. Try different words or a longer period.")
        for h in hits:
            qtext, atext = (h["text"].split("\nAnswer: ", 1) + [""])[:2]
            st.markdown(f"<div class='card'><div class='t'>{html.escape(qtext.replace('Question: ', ''))}</div>"
                        f"<div class='s'>{h['time'][:16].replace('T', ' ')}, match {h['score']:.2f}"
                        f"{', rules: ' + html.escape(h['rules']) if h.get('rules') else ''}</div>"
                        f"<div class='b'>{html.escape(atext)}</div></div>", unsafe_allow_html=True)
    st.markdown("**All your questions**")
    rows = memory.recent_turns(user_id, 100)
    if rows:
        st.dataframe(pd.DataFrame([{"Time": r["time"].replace("T", " "), "Question": r["question"], "Status": r.get("status"),
                                    "Confidence": r.get("confidence"), "Review": r.get("review")} for r in rows]),
                     hide_index=True, width="stretch")
    else:
        st.caption("Nothing yet. Ask a question in the Ask tab.")

# ================================================================ Rules library
with tabs["Rules library"]:
    rules = vectors.all_rules()
    if not rules:
        st.info("The rules knowledge base is empty. Run `python scripts/build_knowledge_base.py` on the server.")
    else:
        quarters = sorted({r["quarter"] for r in rules})
        c1, c2, c3 = st.columns([1, 1, 2])
        quarter = c1.selectbox("Quarter", quarters, index=len(quarters) - 1)
        team_opts = ["All teams"] + sorted({t for r in rules for t in r.get("teams", "").strip(",").split(",") if t and t != "ALL"})
        team = c2.selectbox("Team", team_opts)
        term = c3.text_input("Search the rules", placeholder="e.g. what happens to NR targets with tier 2")
        team_f = None if team == "All teams" else team
        order = {"OVERVIEW": 0, "INPUT": 1, "JCPM": 2, "OUTPUT": 3}
        if term.strip():
            shown = [{**r, "teams": next((x.get("teams", "") for x in rules if x["id"] == r["id"]), "")}
                     for r in vectors.search_rules(term.strip(), quarter, team_f, k=6)]
        else:
            shown = [r for r in rules if r["quarter"] == quarter and (team_f or r.get("rule") != "TEAM") and
                     (not team_f or ",ALL," in r.get("teams", "") or f",{team_f}," in r.get("teams", ""))]
            shown.sort(key=lambda r: (order.get(r.get("stage"), 9), r.get("rule") == "TEAM", r["id"]))
        st.caption(f"{len(shown)} rule{'s' if len(shown) != 1 else ''}")
        for r in shown:
            title = r.get("title") or r["id"]
            body = r["text"][len(title) + 2:] if r["text"].startswith(title + ". ") else r["text"]
            teams_txt = r.get("teams", "").strip(",").replace(",", ", ")
            st.markdown(f"<div class='card'><div class='t'>{html.escape(title)}</div>"
                        f"<div class='s'>{html.escape(r.get('stage', '').title())} rule, {html.escape(r.get('rule', ''))}"
                        f"{', teams ' + html.escape(teams_txt) if teams_txt and teams_txt != 'ALL' else ''}</div>"
                        f"<div class='b'>{html.escape(body)}</div></div>", unsafe_allow_html=True)

# ================================================================ Review queue (analysts)
if is_analyst:
    with tabs["Review queue"]:
        rows = memory.all_turns(300)
        answers = [r for r in rows if r.get("review") in ("Pending", "Verified", "Needs correction")]
        done = [r for r in answers if r.get("review") in ("Verified", "Needs correction")]
        ver = [r for r in done if r["review"] == "Verified"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Answers logged", len(answers))
        m2.metric("Reviewed", len(done))
        m3.metric("Verified correct", f"{len(ver) / len(done):.0%}" if done else "–")
        show = st.radio("Show", ["Pending review", "Sent to an analyst", "All"], horizontal=True)
        sel = [x for x in answers if show == "All" or (show == "Pending review" and x.get("review") == "Pending")
               or (show == "Sent to an analyst" and x.get("status") == "ROUTED_TO_ANALYST")]
        if not sel:
            st.info("Nothing to review here.")
        for x in sel:
            when = dt.datetime.fromisoformat(x["time"]).strftime("%d %b %H:%M")
            who = USERS.get(x["user_id"], {}).get("name", x["user_id"])
            with st.expander(f"{when}   {x['question']}   ({who})"):
                st.markdown(f"<div class='answer'>{html.escape(x.get('answer') or '')}</div>", unsafe_allow_html=True)
                st.caption(f"Status {x.get('status')}, confidence {x.get('confidence')}, written by {x.get('written_by')}, "
                           f"review: {x.get('review')}")
                note = st.text_input("Analyst note", x.get("review_note", ""), key=f"note-{x['id']}")
                a, b, _ = st.columns([1, 1, 3])
                for col, verdict, label in ((a, "Verified", "Mark verified"), (b, "Needs correction", "Needs correction")):
                    if col.button(label, key=f"{verdict}-{x['id']}"):
                        memory.update_turn(x["user_id"], x["sk"], review=verdict, review_note=note,
                                           reviewed_by=user_id, reviewed_at=memory.now_iso())
                        if x.get("run_id") and tracing.enabled():
                            err = tracing.send_feedback(x["run_id"], "analyst_review", 1 if verdict == "Verified" else 0, note)
                            st.toast("Review saved and sent to LangSmith" if not err else f"Review saved. LangSmith: {err}")
                        st.rerun()

    with tabs["Test results"]:
        st.markdown("Runs the 28 seeded test questions against the answer key with the fixed plan (no LLM). "
                    "For the AI agents, run `python scripts/langsmith_eval.py --llm` on the server.")
        if st.button("Run tests", type="primary"):
            from evals.run_eval import KEY, grade
            cases = pd.read_csv(KEY, dtype=str, keep_default_na=False).to_dict("records")
            res, bar = [], st.progress(0.0)
            for i, c in enumerate(cases, 1):
                o = ask(c["question"], "cor_analyst", use_llm=False)
                ok, why = grade(c, o)
                res.append({"Case": c["case_id"], "Result": "Pass" if ok else "Fail", "Expected rule": c["expected_rule"],
                            "Question": c["question"], "Detail": why})
                bar.progress(i / len(cases))
            df = pd.DataFrame(res)
            st.metric("Score", f"{(df.Result == 'Pass').sum()} / {len(df)} correct")
            st.dataframe(df, width="stretch", hide_index=True)
