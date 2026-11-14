# TraceIQ setup guide

Follow the steps in order. Commands are for **Windows PowerShell**; run them from the project folder unless a step says otherwise. Steps you have already done (marked *done before*) only need a quick check.

Never paste API keys, AWS keys or passwords into chat, email or Git. They go only in `.env` on the machine that runs TraceIQ.

---

## Part A: the machine that runs TraceIQ

TraceIQ runs as a website: one machine (your laptop for the hackathon, or a server later) runs the app, the AI agents, DuckDB and ChromaDB. Users only need a browser.

### 1. Python 3.11 or 3.12 *(done before)*
```powershell
python --version
```
If it shows anything older than 3.11, install Python 3.12 from python.org and tick **Add python.exe to PATH**. Python 3.13 has not been tested.

### 2. AWS CLI *(done before)*
```powershell
aws --version
```

### 3. Get the project
1. Extract `traceiq.zip` into a **new folder**, for example `C:\Users\<you>\TraceIQ`. Do not extract over the old project.
2. Copy only your old `.env` file into the new `traceiq` folder (you will update it in step 5). Do not copy the old `.venv`; this version needs new packages.
```powershell
cd $HOME\TraceIQ\traceiq
dir
```
You should see `app.py`, `ask.py`, `SETUP.md`, `requirements.txt` and the folders `traceiq`, `data`, `scripts`, `knowledge`, `config`, `evals`.

### 4. Create the Python environment and install packages
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```
This takes several minutes (ChromaDB is large). If `Activate.ps1` is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, and activate again. Your prompt must start with `(.venv)` for every later step.

### 5. Create the settings file
If you copied an old `.env`, rename it to `.env.old` first, then:
```powershell
copy .env.example .env
notepad .env
```
Fill in the values as you go through the next steps. A complete example is at the end of this guide.

---

## Part B: keys

### 6. Gemini API key *(done before)*
aistudio.google.com → **Get API key** → **Create API key**. In `.env`:
```
TRACEIQ_MODEL=gemini/gemini-3.5-flash-lite
GEMINI_API_KEY=<your key>
```
Check it:
```powershell
python scripts\test_llm.py
```
Expected: `Reply: 'OK' ... The model connection works.`

### 7. LangSmith key
1. Sign up for the free plan at smith.langchain.com.
2. Settings → **API Keys** → **Create API key**, and copy it.
3. In `.env`:
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your key>
LANGSMITH_PROJECT=traceiq
```

---

## Part C: AWS

### 8. Update the IAM policy (adds DynamoDB)
1. AWS console (signed in as root or an admin) → IAM → **Policies** → `TraceIQDevPolicy` → **Edit** → **JSON**.
2. Replace the whole policy with the contents of `config\iam_policy.json` from this project.
3. Replace both `YOUR-BUCKET` with your bucket name, then **Next** → **Save changes**.

The new statements let `traceiq-dev` create and use only the `traceiq_memory` DynamoDB table.

### 9. Check the AWS CLI login *(done before)*
```powershell
aws sts get-caller-identity
aws s3 ls
```
The `Arn` must end with `user/traceiq-dev`, and your bucket must be listed.

### 10. Data in S3 and Athena *(done before)*
Re-run only if you changed the data:
```powershell
python scripts\load_athena.py --bucket <your-bucket>
python scripts\check_athena.py --bucket <your-bucket>
```
Expected: `All tables match.` Then in `.env`:
```
TRACEIQ_DATA_SOURCE=s3
TRACEIQ_S3_BUCKET=<your-bucket>
AWS_REGION=us-east-1
```

### 11. Create the DynamoDB memory table
```powershell
python scripts\setup_dynamodb.py
```
Expected: `Table is ready.` (or `already exists`). Then in `.env`:
```
TRACEIQ_MEMORY=dynamodb
TRACEIQ_DDB_TABLE=traceiq_memory
```
The table uses 5 read and 5 write units, inside DynamoDB's always-free allowance.

---

## Part D: the rules knowledge base (ChromaDB)

### 12. Build the knowledge base
```powershell
python scripts\build_knowledge_base.py
```
The first run downloads a small open-source embedding model (about 80 MB) onto this machine. Expected:
```
Indexed 58 rule entries into ChromaDB (local embeddings).
Test search: [...]
```
If it says it is using `hash embeddings`, the model could not be downloaded (usually a blocked network). Everything still works with lower search quality; fix the network and run the step again.

Run this step again whenever you edit `knowledge\rules_*.yaml` or add a new quarter's file.

---

## Part E: test

### 13. Offline test (no AI calls)
```powershell
python -m evals.run_eval
```
Expected: `Score: 28/28 correct`.

### 14. One question with the AI agents
```powershell
python ask.py --debug "Why was 18146677 dropped from 106-NR-9396532?"
```
Expected: `[ANSWERED] confidence: HIGH`, an answer naming CAPLYTA_HCP_TIER, and `(written by: LLM (attempt 1))`. Then open smith.langchain.com → **Projects** → **traceiq**: the question appears as a trace with all agents, Gemini calls and tool calls.

---

## Part F: run the website

### 15. Start TraceIQ
```powershell
streamlit run app.py
```
The browser opens at `http://localhost:8501`. If Windows Firewall asks, allow access on private networks.

### 16. Try it
1. Sign in as `ns_team_member` / `ns-demo-2026`.
2. Ask: `Why was 18146677 dropped from 106-NR-9396532?`
3. Follow up: `Is it still in 106-NA?` The answer shows *Using customer 18146677 from your previous question.*
4. Ask: `Why was 33440026 removed from 200-RM-8474173?` This is an IMM territory, so access is denied.
5. **My history** tab: search `Caplyta tier`.
6. **Rules library** tab: search `what happens to NR targets with tier 2`.
7. Sign out, sign in as `cor_analyst` / `cor-demo-2026`, open **Review queue**, and mark an answer verified. It is saved to DynamoDB and sent to LangSmith as feedback.
8. **Test results** tab: **Run tests**.

Open **Settings and status** in the sidebar to confirm: Model, Data (S3 bucket), Memory (DynamoDB table), Vector search (ChromaDB, local embeddings), LangSmith tracing on.

---

## Part G: full evaluation with the AI agents

### 17. Run the 28 test cases as a LangSmith experiment
```powershell
python scripts\langsmith_eval.py --llm
```
This takes 20 to 40 minutes on the free tier because of rate limits. It ends with `Score: X/28 correct`. In LangSmith, open **Datasets & Experiments** → `traceiq-test-cases` to see each question's scores. Use this score in the deck.

---

## Part H: running it as a shared website

For the hackathon, step 15 on your laptop is enough. To let others open it from their own browsers:

1. Run on the server (the machine everyone connects to):
   ```powershell
   streamlit run app.py --server.address 0.0.0.0 --server.port 8501
   ```
   Others open `http://<server-address>:8501`. Allow port 8501 through the server's firewall.
2. All memory, history and vectors stay on the server side: DynamoDB in AWS, ChromaDB in `TRACEIQ_STATE_DIR` (default `state\` in the project folder). Set `TRACEIQ_STATE_DIR` to a folder that is backed up.
3. To run ChromaDB as its own service instead of inside the app:
   ```powershell
   chroma run --path C:\traceiq-state\chroma --port 8000
   ```
   and set `CHROMA_HOST=localhost` (or that server's address) and `CHROMA_PORT=8000` in `.env`, then re-run step 12.
4. Change every demo password before sharing the link:
   ```powershell
   python scripts\set_password.py cor_analyst
   ```
5. For anything beyond a demo, put it behind HTTPS and real sign-in (SSO); see `IMPROVEMENTS.md`.

---

## Complete `.env` example
```
TRACEIQ_MODE=
TRACEIQ_MODEL=gemini/gemini-3.5-flash-lite
GEMINI_API_KEY=<your Gemini key>
TRACEIQ_PAUSE_SECONDS=0

TRACEIQ_DATA_SOURCE=s3
TRACEIQ_S3_BUCKET=<your-bucket>
AWS_REGION=us-east-1

TRACEIQ_MEMORY=dynamodb
TRACEIQ_DDB_TABLE=traceiq_memory
TRACEIQ_STATE_DIR=

TRACEIQ_EMBEDDINGS=local
TRACEIQ_EMBED_MODEL=gemini/gemini-embedding-001
CHROMA_HOST=
CHROMA_PORT=8000

LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your LangSmith key>
LANGSMITH_PROJECT=traceiq
```

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| `No module named ...` | Environment not active or install failed | Check the prompt shows `(.venv)`; rerun `pip install -r requirements.txt` and read the last lines |
| Sidebar says `Memory: DynamoDB unavailable` | Table missing or policy not updated | Redo steps 8 and 11 |
| `AccessDenied` on S3, Athena or DynamoDB | Policy missing an action, or wrong bucket name in it | Compare the policy with `config\iam_policy.json` |
| Sidebar says `hash embeddings` | Embedding model download blocked | Use another network and rerun step 12 |
| Rules library is empty | Knowledge base not built | Run step 12 |
| Questions take minutes | Gemini free-tier rate limits | Set `TRACEIQ_PAUSE_SECONDS=8`; check `[LLM] waiting` lines in the terminal |
| `Timeout` from the model | Network blocks Google's API, or a retired model name | Try another network; check the model name in AI Studio |
| `Run compression is not enabled` | LangSmith notice | Harmless |
