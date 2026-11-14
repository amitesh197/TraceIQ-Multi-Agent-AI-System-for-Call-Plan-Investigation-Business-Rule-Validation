"""Optional LangSmith tracing. Turned on by LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env.

When off, every helper here is a no-op, so TraceIQ works the same without LangSmith.
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def enabled() -> bool:
    on = os.environ.get("LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", "")).lower() == "true"
    return on and bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))


def traced(name: str, run_type: str = "chain", **kw):
    """Decorator: record the function as a LangSmith run when tracing is on."""
    def deco(fn):
        try:
            from langsmith import traceable
        except ImportError:
            return fn
        return traceable(name=name, run_type=run_type, **kw)(fn)
    return deco


def current_run_id() -> str | None:
    if not enabled():
        return None
    try:
        from langsmith.run_helpers import get_current_run_tree
        rt = get_current_run_tree()
        return str(rt.id) if rt else None
    except Exception:
        return None


def send_feedback(run_id: str, key: str, score: float, comment: str = "") -> str | None:
    """Attach feedback (e.g. an analyst's verdict) to a traced answer. Returns an error message or None."""
    if not (enabled() and run_id):
        return "LangSmith tracing is off"
    try:
        from langsmith import Client
        client = Client()
        try:  # newer LangSmith versions want the project (session) id with the feedback
            pid = client.read_project(project_name=os.environ.get("LANGSMITH_PROJECT", "default")).id
        except Exception:
            pid = None
        kw = {"session_id": pid} if pid else {}
        client.create_feedback(run_id, key=key, score=score, comment=comment or None, **kw)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
