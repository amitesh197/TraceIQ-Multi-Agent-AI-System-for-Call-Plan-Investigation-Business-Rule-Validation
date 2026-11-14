# Pending improvements

Agreed to do later. Ask for any of these by number.

1. **Intake speed-up.** Skip the LLM intake call when the question already contains the jnj_id and territory (saves about 5 to 7 seconds per question).
2. **Athena mode (optional).** Fetch each question's rows live from Athena with two parallel queries, then check in memory (about 3 to 5 seconds extra per question).
3. **Full LLM evaluation and tuning.** Run `python scripts/langsmith_eval.py --llm`, fix failing cases, and if Flash-Lite stops tool calls early, raise its thinking level or use a stronger model for the Trace and Rules agents.
4. **Similar resolved cases.** Use analyst-verified answers from the review queue as dynamic examples for the Response agent (vector search use 1, deferred).
5. **Confirm data assumptions.** sales_direction column, forced-insert attributes, under-capacity per territory, 200-SB and 200-BM products, and whether post_proc holds data before or after the three input rules.
6. **Real sign-in.** Replace demo passwords with SSO (for example Amazon Cognito or the client's identity provider) before real users.
7. **Pitch material.** Update the deck with the real test score; demo run sheet; presenter script for two speakers; likely leadership questions with answers.
8. **AWS hosting (optional, declined for now).** Static page on S3 plus TraceIQ on Lambda; Streamlit stays for the demo.
