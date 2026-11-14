"""One quick call to the model in .env, with no retries, so any error shows immediately.

  python scripts/test_llm.py
"""
import os
import sys
import time

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
import litellm  # noqa: E402

litellm.drop_params = True  # same setting as the app: ignore parameters a model doesn't support

model = os.environ.get("TRACEIQ_MODEL", "")
key = os.environ.get("GEMINI_API_KEY", "")
print(f"Model: {model!r}")
print(f"Gemini key loaded: {'yes, ' + str(len(key)) + ' characters' if key else 'NO'}")
t = time.time()
try:
    r = litellm.completion(model=model, messages=[{"role": "user", "content": "Reply with the single word OK."}],
                           num_retries=0, timeout=60)
    print(f"Reply: {r.choices[0].message.content!r} in {time.time() - t:.1f}s. The model connection works.")
except Exception as e:
    print(f"FAILED after {time.time() - t:.1f}s with {type(e).__name__}:\n{e}")
    sys.exit(1)
