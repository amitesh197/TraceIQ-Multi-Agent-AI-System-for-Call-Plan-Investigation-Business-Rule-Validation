# TraceIQ

**Team Slaygents · Clash of Agents 2026 · "Serving answers, not tickets."**

TraceIQ answers the question the ZS COR team gets every day during the JCPM refinement window: *why was this HCP dropped, given fewer calls, moved, or changed in the call plan?* It traces the customer through every step of the call plan pipeline, finds the exact business rule behind the outcome, and writes the answer and a draft reply email in about a minute instead of the usual one-day turnaround.

> **Keep this repo private.** All data here is synthetic, but the table names, teams, products and business rules describe a real client pipeline. Never add real J&J or ZS data, and never commit API keys.

---

## Get it running (about 20 minutes)

You don't need AWS for any of this. These steps run everything on your laptop.

### 1. Prerequisites
- **Python 3.11 or 3.12** (python.org; tick *Add python.exe to PATH*). Check with `python --version`.
- **Git** (git-scm.com).

### 2. Clone and install
```powershell
git clone https://github.com/amitesh197/TraceIQ.git
cd TraceIQ
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```
On Mac or Linux, activate with `source .venv/bin/activate`. If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Your prompt should start with `(.venv)` from now on.

### 3. Your settings file
```powershell
copy .env.example .env
notepad .env
```
Set at least these lines:
```
TRACEIQ_MODEL=gemini/gemini-3.5-flash-lite
GEMINI_API_KEY=<your own key>
TRACEIQ_DATA_SOURCE=local
TRACEIQ_MEMORY=local
TRACEIQ_EMBEDDINGS=local
```
- Get a **free Gemini key** at aistudio.google.com → *Get API key*. Use your own key, not a shared one; each key has its own daily limit.
- `local` for data and memory means TraceIQ reads the tables from `data/` and keeps memory on your laptop, so no AWS account is needed.
- **LangSmith (optional):** for traces of every question, sign up free at smith.langchain.com and set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT=traceiq`.

`.env` is in `.gitignore`, so it is never uploaded. Keep it that way.

### 4. Check the model and build the rules knowledge base
```powershell
python scripts\test_llm.py
python scripts\build_knowledge_base.py
```
The first should end with `The model connection works.` The second downloads a small embedding model once (about 80 MB) and should print `Indexed 58 rule entries into ChromaDB`.

### 5. Run the tests
```powershell
python -m evals.run_eval
```
Expected: `Score: 28/28 correct`. This runs without the AI and takes a few seconds.

### 6. Start the website
```powershell
streamlit run app.py
```
It opens at `http://localhost:8501`. Sign in with a demo account:

| User ID | Password | Sees |
|---|---|---|
| `cor_analyst` | `cor-demo-2026` | All teams, plus the Review queue and Test results tabs |
| `ns_team_member` | `ns-demo-2026` | NS teams only (106-NA, NB, NR, PI, PR, TD) |
| `imm_team_member` | `imm-demo-2026` | IMM teams only |

Things to try:
1. `Why was 18146677 dropped from 106-NR-9396532?`, then the follow-up `Is it still in 106-NA?`
2. `The rep added 69772426 to 200-GS-6160088. Why is it not in the final plan?` (two steps: a rep add, then a Veeva drop)
3. As `ns_team_member`, ask about an IMM territory to see access denied.
4. The **My history** and **Rules library** tabs.

---

## How it works

```
Question → Intake → Access → Trace → Rules → Quality → Response
           (history questions: Intake → Access → Memory → Response)
```

| Agent | What it does | Uses the AI? |
|---|---|---|
| **Intake** | Pulls the jnj_id, territory and intent from the question; fills gaps from the previous question in the chat | Yes |
| **Access** | Checks the user's teams on every request | No |
| **Trace** | Follows the customer through all nine pipeline tables and finds where it was dropped, added or changed | Plans which tools to call |
| **Rules** | Reads the relevant business rules from the knowledge base, then runs the rule checks that could explain each change | Plans which checks to run |
| **Quality** | Each change must be explained by exactly one confirmed rule; anything unclear goes to an analyst | No |
| **Response** | Writes the answer and email from confirmed facts; a fact check rejects any invented ID, territory, rule or number | Yes |

**The key idea: the agents decide *what* to check; plain Python tools decide *whether* a rule applied.** The AI never decides facts, which is why answers can be trusted. Without an API key, the same agents run in *offline mode* with a fixed plan; that's also the demo fallback (the *Use AI agents* switch in the sidebar).

### The pipeline being traced (Q3'26)
1. **Input processing:** iConnect landing tables → staging → input universe → alignment refresh → forced inserts/deletes → customer status, specialty include/exclude and 200-SI Kaiser rules → the JCPM UI universe (`mstr_cppa_srchb_univ`).
2. **JCPM refinement window:** reps add, drop, change target flags and sales direction → `iconnect_cp_out`.
3. **Output processing:** ITCI exclusion → invalid Veeva → Caplyta HCP tier → ONC VSAP removal → IMM Derm mirroring → under-capacity flag → `iconnect_final_call_plan_out`.

`knowledge/rules_Q3_26.yaml` describes every rule in plain English. Start there if you're new to the process.

### Tech stack
LangGraph (agents) · LiteLLM (any model; Gemini now) · DuckDB (queries in memory) · ChromaDB (history search and the rules knowledge base) · DynamoDB or local files (per-user memory) · AWS S3 + Athena (data, optional) · LangSmith (tracing and evaluation) · Streamlit (website).

---

## Where things are

| Path | What it is | Change it when... |
|---|---|---|
| `app.py` | The website | ...the UI needs changing |
| `ask.py` | Ask one question from the terminal: `python ask.py --debug "..."` | |
| `traceiq/agents/graph.py` | The agents and their prompts | ...an agent should behave differently |
| `traceiq/tools/rule_tools.py` | One check per business rule | ...a rule's logic changes |
| `traceiq/tools/trace_tools.py` | Following a customer through the tables | |
| `traceiq/pipeline.py` | Stage order and rebuilding the in-between states of in-place rules | ...the pipeline order changes |
| `traceiq/answers.py` | Facts given to the Response agent, and the fallback answer wording | ...answer wording changes |
| `traceiq/skills/traceiq_rules_skill.md` | Pipeline instructions the agents read | ...agents need more guidance |
| `knowledge/rules_Q3_26.yaml` | Business rules per quarter | ...rules change (then rerun `build_knowledge_base.py`) |
| `traceiq/memory.py`, `traceiq/vectors.py` | Per-user memory and ChromaDB | |
| `config/users.yaml` | Demo users, teams, hashed passwords | ...adding a demo user (`python scripts/set_password.py <user>`) |
| `data/jib_aws/` | The 23 synthetic pipeline tables | Don't edit by hand |
| `evals/answer_key/` | The 28 test questions and correct answers. **The agents never read this.** | ...adding a test case |
| `SETUP.md` | Full setup, including AWS, DynamoDB and LangSmith | |
| `IMPROVEMENTS.md` | The agreed backlog | ...picking what to work on |

---

## Making changes

1. **Never push straight to `main`.** Create a branch:
   ```powershell
   git checkout main
   git pull
   git checkout -b short-description-of-change
   ```
2. Make the change and **run the tests**. They must stay at 28/28:
   ```powershell
   python -m evals.run_eval
   ```
3. Commit and push:
   ```powershell
   git add .
   git status            # .env, .venv and state must NOT be listed
   git commit -m "What you changed and why"
   git push -u origin short-description-of-change
   ```
4. On GitHub, open a **pull request**. Say what you changed, why, and the test score. Amitesh reviews and merges.
5. Ideas without code: open an **Issue**.

**Ground rules**
- Synthetic data only; no real client data, ever.
- No keys, passwords or `.env` in commits. If a key is ever committed by mistake, tell the team and create a new key straight away.
- Rule checks stay as plain Python. Don't move factual decisions into prompts.
- Pick work from `IMPROVEMENTS.md` and mention the item number in your pull request.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named ...` | Make sure the prompt shows `(.venv)`, then rerun `pip install -r requirements.txt` |
| A question takes minutes, or `RateLimitError` | The Gemini free tier allows only a few requests per minute and per day (the daily limit resets at 1:30 PM IST). Wait, or set `TRACEIQ_PAUSE_SECONDS=8` in `.env` |
| `Timeout` from the model | Check the model name in AI Studio, or try another network (office networks and VPNs sometimes block it) |
| Sidebar says `hash embeddings` | The embedding model couldn't be downloaded. Try another network and rerun `build_knowledge_base.py` |
| Rules library is empty | Run `python scripts\build_knowledge_base.py` |
| `Run compression is not enabled` | A harmless LangSmith notice |

Questions: ask Amitesh.
