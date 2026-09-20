"""
scripts/serv_smoke.py — one-shot SERV connectivity check.

Usage (inside the container):
    docker compose run --rm app python scripts/serv_smoke.py

Probes the SERV reasoning API with three checks:
  1. Basic chat completion with a system message (main check)
  2. Models list endpoint (GET /models) — prints available model names
  3. JSON output mode (response_format=json_object) — prints whether supported

Prints reply, latency, token usage, and any error for each check.
The API key is NEVER printed, even in error output.

SERV requirement (confirmed 2026-09-19): every request MUST include a system
message. Requests without one are rejected with HTTP 400.

Exit codes:
  0  main chat completion succeeded
  1  configuration error (missing key, bad URL)
  2  main chat completion failed
"""

import os
import sys
import time

# ---------------------------------------------------------------------------
# Read config from environment (mirrors app/config.py but standalone)
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SERV_API_KEY", "")
BASE_URL = os.environ.get("SERV_BASE_URL", "https://inference-api.openserv.ai/v1")
MODEL = os.environ.get("SERV_MODEL", "serv-model-placeholder")

if not API_KEY or API_KEY in ("", "your-serv-api-key-here", "offline"):
    print("ERROR: SERV_API_KEY is not set or is still the placeholder value.")
    print("       Set it in .env and re-run.")
    sys.exit(1)


def _mask(text: str) -> str:
    """Replace the API key in any string so it is never printed."""
    if API_KEY and len(API_KEY) > 8:
        return text.replace(API_KEY, "sk-***")
    return text


# ---------------------------------------------------------------------------
# Import openai
# ---------------------------------------------------------------------------

try:
    from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError
except ImportError:
    print("ERROR: openai package not installed.")
    sys.exit(1)

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    timeout=60.0,
    max_retries=0,
)

print("SERV smoke test")
print(f"  base_url : {BASE_URL}")
print(f"  model    : {MODEL}")
print()

# ---------------------------------------------------------------------------
# CHECK 1: Models list (GET /models)
# ---------------------------------------------------------------------------

print("── Check 1: models list ──────────────────────────────────────────────")
t0 = time.monotonic()
try:
    models_response = client.models.list()
    elapsed_ms = (time.monotonic() - t0) * 1000
    # SERV may return a non-standard object; defensively extract ids
    try:
        model_ids = [m.id for m in models_response.data]
    except (AttributeError, TypeError):
        # Fall back to whatever the response looks like
        model_ids = list(models_response) if models_response else []
    print(f"  status   : OK ({elapsed_ms:.0f} ms)")
    print(f"  models   : {model_ids}")
    # Resolve OQ-5: update if SERV_MODEL is still the placeholder
    if MODEL == "serv-model-placeholder":
        print(f"  ⚠  SERV_MODEL is still the placeholder — set it to one of the above")
    elif model_ids and MODEL in model_ids:
        print(f"  ✓  SERV_MODEL={MODEL!r} is in the list")
    else:
        print(f"  ⚠  SERV_MODEL={MODEL!r} not found in list (may still work)")
except (APIStatusError, APIConnectionError, APITimeoutError, Exception) as exc:
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"  status   : NOT SUPPORTED / error ({elapsed_ms:.0f} ms)")
    print(f"  detail   : {_mask(str(exc))}")
print()

# ---------------------------------------------------------------------------
# CHECK 2: Basic chat completion WITH system message
# ---------------------------------------------------------------------------

print("── Check 2: chat completion (with system message) ───────────────────")

SYSTEM_MSG = (
    "You are a helpful assistant. "
    "Follow the user's instructions exactly and be concise."
)
USER_PROMPT = "Reply with exactly three words: the sky is."

print(f"  system   : {SYSTEM_MSG!r}")
print(f"  prompt   : {USER_PROMPT!r}")

t0 = time.monotonic()
main_error: Exception | None = None
reply_text = ""
usage_info = ""

try:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": USER_PROMPT},
        ],
        max_completion_tokens=64,
        # Note: SERV does not support temperature=0; only the default (1) is accepted.
        # See docs/OPEN_QUESTIONS.md FINDING-4.
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    reply_text = (response.choices[0].message.content or "").strip()
    if response.usage:
        usage_info = (
            f"prompt={response.usage.prompt_tokens} "
            f"completion={response.usage.completion_tokens} "
            f"total={response.usage.total_tokens}"
        )
except (APITimeoutError, APIConnectionError, APIStatusError, Exception) as exc:
    elapsed_ms = (time.monotonic() - t0) * 1000
    main_error = exc

print(f"  latency  : {elapsed_ms:.0f} ms")
if main_error is None:
    display = reply_text[:200] + ("…" if len(reply_text) > 200 else "")
    print(f"  reply    : {display!r}")
    if usage_info:
        print(f"  tokens   : {usage_info}")
    print(f"  status   : OK")
else:
    print(f"  error    : {type(main_error).__name__}: {_mask(str(main_error))}")
    print(f"  status   : FAILED")
print()

# ---------------------------------------------------------------------------
# CHECK 3: JSON output mode (response_format=json_object)
# ---------------------------------------------------------------------------

print("── Check 3: JSON output mode (response_format) ──────────────────────")
t0 = time.monotonic()
try:
    json_response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You output only valid JSON objects."},
            {"role": "user", "content": 'Return {"ok": true}'},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=32,
        # temperature not sent — SERV only accepts the default (1)
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    json_reply = (json_response.choices[0].message.content or "").strip()
    print(f"  status   : SUPPORTED ({elapsed_ms:.0f} ms)")
    print(f"  reply    : {json_reply!r}")
except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"  status   : NOT SUPPORTED / error ({elapsed_ms:.0f} ms)")
    print(f"  detail   : {_mask(str(exc))}")
except Exception as exc:  # noqa: BLE001
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"  status   : ERROR ({elapsed_ms:.0f} ms)")
    print(f"  detail   : {_mask(str(exc))}")
print()

# ---------------------------------------------------------------------------
# Final verdict
# ---------------------------------------------------------------------------

if main_error is None:
    print("RESULT: OK — SERV API is reachable and chat completion works.")
    sys.exit(0)
else:
    print("RESULT: FAILED — main chat completion did not succeed.")
    sys.exit(2)
