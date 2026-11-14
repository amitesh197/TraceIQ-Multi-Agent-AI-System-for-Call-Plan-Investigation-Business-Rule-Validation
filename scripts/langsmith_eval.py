"""Run the 28 seeded test cases as a LangSmith experiment.

  python scripts/langsmith_eval.py            # offline mode (fixed plan, no LLM)
  python scripts/langsmith_eval.py --llm      # AI agents with TRACEIQ_MODEL

Creates (once) the dataset "traceiq-test-cases" from evals/answer_key/test_cases.csv,
then records an experiment with these scores per question:
  correct_rule      the rule TraceIQ found matches the answer key
  high_confidence   Quality marked the answer HIGH
  grounded          every rule the Rules agent cited was confirmed by a tool
  llm_wrote_answer  the LLM's answer passed the fact check (LLM mode)
plus an overall accuracy. Each run is fully traced, so you can open any question.
"""
import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traceiq import tracing  # noqa: E402  (loads .env)
from traceiq.agents.graph import ask  # noqa: E402
from traceiq.agents.llm import MODEL  # noqa: E402
from evals.run_eval import KEY, grade, matched_rules  # noqa: E402

DATASET = "traceiq-test-cases"


def ensure_dataset(client):
    if client.has_dataset(dataset_name=DATASET):
        return
    cases = pd.read_csv(KEY, dtype=str, keep_default_na=False).to_dict("records")
    ds = client.create_dataset(DATASET, description="TraceIQ seeded call plan questions with expected rules (synthetic data)")
    client.create_examples(dataset_id=ds.id, examples=[
        {"inputs": {"question": c["question"]},
         "outputs": {k: c[k] for k in ("case_id", "jnj_id", "territory", "expected_stage", "expected_rule", "expected_outcome")},
         "metadata": {"case_id": c["case_id"]}} for c in cases])
    print(f"Created dataset {DATASET} with {len(cases)} examples")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="score locally without uploading (for testing)")
    a = ap.parse_args()
    if not tracing.enabled() and not a.dry_run:
        sys.exit("Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env first.")
    from langsmith import Client, evaluate
    client = None if a.dry_run else Client()
    if client:
        ensure_dataset(client)

    full = {}  # question -> full TraceIQ state, kept locally (only a summary is uploaded)

    def target(inputs: dict) -> dict:
        out = ask(inputs["question"], "cor_analyst", use_llm=a.llm)
        full[inputs["question"]] = out
        q = out.get("quality", {})
        return {"answer": out.get("response", {}).get("answer"), "rules": matched_rules(out),
                "confidence": q.get("confidence"), "groundedness": q.get("groundedness"),
                "written_by": out.get("response", {}).get("written_by")}

    def correct_rule(inputs: dict, reference_outputs: dict) -> dict:
        ok, why = grade(reference_outputs, full[inputs["question"]])
        return {"key": "correct_rule", "score": int(ok), "comment": why}

    def high_confidence(outputs: dict) -> dict:
        return {"key": "high_confidence", "score": int(outputs.get("confidence") == "HIGH")}

    def grounded(outputs: dict) -> dict:
        g = outputs.get("groundedness")
        return {"key": "grounded", "score": 1.0 if g is None else float(g)}

    def llm_wrote_answer(outputs: dict) -> dict:
        return {"key": "llm_wrote_answer", "score": int(str(outputs.get("written_by", "")).startswith("LLM"))}

    def accuracy(inputs: list, reference_outputs: list) -> dict:
        oks = [grade(r, full[i["question"]])[0] for i, r in zip(inputs, reference_outputs)]
        return {"key": "accuracy", "score": sum(oks) / len(oks)}

    evaluators = [correct_rule, high_confidence, grounded] + ([llm_wrote_answer] if a.llm else [])
    mode = MODEL.split("/")[-1] if a.llm else "offline"
    data = DATASET
    if a.dry_run:
        import uuid
        from langsmith.schemas import Example
        cases = pd.read_csv(KEY, dtype=str, keep_default_na=False).to_dict("records")
        dsid = uuid.uuid4()
        data = [Example(id=uuid.uuid4(), dataset_id=dsid, inputs={"question": c["question"]},
                        outputs={k: c[k] for k in ("expected_stage", "expected_rule")}) for c in cases]
    res = evaluate(target, data=data, evaluators=evaluators, summary_evaluators=[accuracy], upload_results=not a.dry_run,
                   experiment_prefix=f"traceiq-{mode}", max_concurrency=1,
                   metadata={"mode": "llm" if a.llm else "offline", "model": MODEL if a.llm else None})
    df = res.to_pandas()
    score = int(df["feedback.correct_rule"].sum()) if "feedback.correct_rule" in df else "?"
    print(f"\nScore: {score}/{len(df)} correct. Open LangSmith > Datasets & Experiments > {DATASET} to see the run.")


if __name__ == "__main__":
    main()
