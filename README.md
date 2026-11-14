# TraceIQ

Explains why any HCP or account ended up the way it did in the JCPM call plan, citing the exact pipeline step and rule. Team Slaygents, Clash of Agents 2026. All data in `data/` is synthetic.

**Setup:** follow [SETUP.md](SETUP.md) step by step. **Backlog:** [IMPROVEMENTS.md](IMPROVEMENTS.md).

## What it does

- **Six agents** (LangGraph): Intake → Access → Trace → Rules → Quality → Response. The agents decide what to check; Python tools decide whether a rule applied; the Response agent writes the answer from verified facts and a fact check rejects anything invented.
- **Memory per user** (DynamoDB, or local files on the server): access is remembered for the session and still checked on every request; follow-up questions reuse the customer and territory from the previous turn; every question and answer is kept.
- **Vector search** (ChromaDB on the server): search your own history by meaning ("what did I ask about Kaiser last week?"), and a rules knowledge base per quarter and team that the Rules agent reads before checking.
- **Data**: pipeline tables in S3 (and Athena), queried in memory with DuckDB.
- **LangSmith**: traces of every question, a 28-case evaluation dataset, and analyst review feedback.
- **Website** (Streamlit): sign-in, chat, trace path, draft email, history, rules library, review queue and test results.

## Commands

```bash
streamlit run app.py                          # the website
python ask.py --debug "Why was 18146677 dropped from 106-NR-9396532?"
python -m evals.run_eval                      # 28 test cases, offline
python scripts/langsmith_eval.py --llm        # 28 test cases with the AI agents, as a LangSmith experiment
python scripts/build_knowledge_base.py        # (re)build the rules knowledge base
python scripts/setup_dynamodb.py              # create the memory table
python scripts/load_athena.py --bucket <b>    # upload tables to S3 and create Athena tables
python scripts/check_athena.py --bucket <b>   # compare Athena row counts with the local files
python scripts/test_llm.py                    # one quick model call
python scripts/set_password.py <user_id>      # change a password
```

## Layout

```
app.py                       website (Streamlit)
ask.py                       command line
traceiq/db.py                DuckDB over the pipeline tables (local files or S3)
traceiq/access.py            users, roles, team access, sign-in
traceiq/memory.py            per-user memory (DynamoDB or local)
traceiq/vectors.py           ChromaDB: rules knowledge base and history search
traceiq/pipeline.py          stage order and replays of the in-place rules
traceiq/tools/               trace tools, one check per rule, tool registry
traceiq/answers.py           verified facts and fallback answers
traceiq/agents/graph.py      the agents (LangGraph)
traceiq/agents/llm.py        LiteLLM wrapper with retries
traceiq/tracing.py           LangSmith hooks
traceiq/skills/              pipeline instructions for the agents
knowledge/rules_Q3_26.yaml   business rules for the quarter
config/users.yaml            demo users (hashed passwords)
config/iam_policy.json       least-privilege AWS policy
evals/                       evaluation runner and answer key (never read by the agents)
scripts/                     setup and evaluation scripts
```
