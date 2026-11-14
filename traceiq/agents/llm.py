"""LLM access through LiteLLM, so any provider works by changing TRACEIQ_MODEL.

Examples: gemini/gemini-flash-latest, openai/<model>, anthropic/<model>,
bedrock/<model-id>, ollama/llama3.1. Check your provider for current model names.
Free-tier APIs have low per-minute limits, so calls retry with backoff.
"""
import json
import os
import re

from dotenv import load_dotenv

from ..tracing import traced

load_dotenv()

MODEL = os.environ.get("TRACEIQ_MODEL", "gemini/gemini-flash-latest")
RETRIES = int(os.environ.get("TRACEIQ_RETRIES", "6"))
PAUSE = float(os.environ.get("TRACEIQ_PAUSE_SECONDS", "0"))  # wait between calls on free tiers
KEY_VARS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "OLLAMA_API_BASE")


def llm_enabled() -> bool:
    mode = os.environ.get("TRACEIQ_MODE", "").lower()
    if mode in ("offline", "llm"):
        return mode == "llm"
    return any(os.environ.get(k) for k in KEY_VARS)


def _llm_outputs(resp):
    try:
        return resp.model_dump()
    except Exception:
        return {"output": str(resp)}


@traced("gemini_call" if "gemini" in MODEL else "llm_call", run_type="llm", process_outputs=_llm_outputs,
        metadata={"ls_provider": MODEL.split("/")[0], "ls_model_name": MODEL.split("/")[-1]})
def complete(messages: list, tools: list | None = None, temperature: float = 0.0):
    import time
    import litellm  # imported lazily so offline mode works without a key
    litellm.suppress_debug_info = True  # hide LiteLLM's "Give Feedback / Get Help" banner on retried errors
    litellm.drop_params = True          # drop settings a model doesn't support (e.g. temperature/tool_choice) instead of failing
    kw = {"model": MODEL, "messages": messages, "temperature": temperature}
    if tools:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"
    delay = 5
    for attempt in range(RETRIES + 1):
        try:
            if PAUSE:
                time.sleep(PAUSE)
            return litellm.completion(**kw)
        except (litellm.RateLimitError, litellm.ServiceUnavailableError, litellm.APIConnectionError) as e:
            if attempt == RETRIES:
                raise
            print(f"  [LLM] {type(e).__name__}: waiting {delay}s before retry {attempt + 1}/{RETRIES} "
                  f"({str(e).splitlines()[0][:160]})", flush=True)
            time.sleep(delay)  # free tiers allow only a few requests per minute
            delay = min(delay * 2, 60)


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply."""
    text = (text or "").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def tool_loop(system: str, user: str, tools: list, run, max_steps: int = 12) -> tuple[dict, list]:
    """Let the model call tools until it answers. `run(name, args)` executes a tool.

    Returns (final JSON reply, list of tool calls with results)."""
    from ..tools.registry import short
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    calls = []
    for _ in range(max_steps):
        resp = complete(messages, tools)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return parse_json(msg.content or ""), calls
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                        for tc in tool_calls]})
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run(tc.function.name, args)
            calls.append({"tool": tc.function.name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": short(result)})
    return {}, calls
