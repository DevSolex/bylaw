"""
app/serv_client.py — thin wrapper over the SERV reasoning API.

SERV is OpenAI-SDK compatible.  All model calls in Bylaw go through
ServClient.chat() so that:
  - A system message is always present (SERV rejects requests without one).
  - Retries and timeouts are applied consistently.
  - The API key is never logged.
  - Latency and token counts are returned for the audit log.

SERV requirement (confirmed 2026-09-19):
  Every request MUST include a system message (role="system").
  Requests without one are rejected with HTTP 400:
  "A system prompt is required. Please include a system or developer message."

Usage
-----
    from app.serv_client import ServClient, ServResponse
    client = ServClient()
    resp = client.chat(
        system="You are a JSON-only assistant.",
        messages=[{"role": "user", "content": "..."}],
    )
    print(resp.content)   # the model's reply text
    print(resp.latency_ms, resp.prompt_tokens, resp.completion_tokens)

OFFLINE_DEMO mode
-----------------
When settings.offline_demo is True the real API is never called.
ServClient.chat() returns a deterministic stub response instead.
This is used in tests and for demo fallback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default system message — used when callers do not provide one.
# Callers should always supply a task-specific system message; this is a
# last-resort guard to satisfy the SERV requirement.
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM = (
    "You are a precise financial-allocation assistant. "
    "Follow instructions exactly. Output only what is asked for."
)

# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class ServResponse:
    content: str
    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    stubbed: bool = False           # True when OFFLINE_DEMO substituted real call


# ---------------------------------------------------------------------------
# Offline stub
# ---------------------------------------------------------------------------

_STUB_POLICY_JSON = (
    '{"max_per_vault": 0.4, "min_liquid": 0.2, "liquid_days": 1, '
    '"max_avg_risk": 3.0, "min_avg_apy": null, '
    '"max_redemption_days": null, "excluded_vault_ids": []}'
)

_STUB_PROPOSAL_JSON = (
    '{"allocation": {"mock-tbill": 0.5, "mock-bond": 0.3, "mock-credit": 0.2}, '
    '"rationale": "[OFFLINE STUB] Allocated 50% to T-bill vault (risk 1, instant), '
    '30% to bond vault (risk 2, 3 days), 20% to private credit (risk 4, 30 days). '
    'Weighted avg risk = 2.1, within the 3.0 ceiling."}'
)


def _make_stub(model: str, latency_ms: float = 50.0) -> ServResponse:
    """Return a deterministic offline stub that passes the verifier for mock vaults."""
    return ServResponse(
        content=_STUB_PROPOSAL_JSON,
        model=model,
        latency_ms=latency_ms,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        stubbed=True,
    )


def _make_policy_stub(model: str) -> ServResponse:
    return ServResponse(
        content=_STUB_POLICY_JSON,
        model=model,
        latency_ms=50.0,
        stubbed=True,
    )


# ---------------------------------------------------------------------------
# ServClient
# ---------------------------------------------------------------------------

class ServClient:
    """
    Wraps the OpenAI-compatible SERV API.

    Parameters
    ----------
    api_key, base_url, model:
        Override the values from settings (useful in tests).
    offline:
        Force offline stub mode regardless of settings.offline_demo.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        offline: bool | None = None,
    ) -> None:
        self._api_key = api_key or settings.serv_api_key
        self._base_url = base_url or settings.serv_base_url
        self._model = model or settings.serv_model
        self._offline = offline if offline is not None else settings.offline_demo
        self._client = None  # lazy-initialised to avoid import cost in offline mode

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # noqa: PLC0415
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=60.0,
                max_retries=0,  # we handle retries ourselves
            )
        return self._client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_completion_tokens: int = 2048,
        response_format: dict | None = None,
    ) -> ServResponse:
        """
        Send a chat-completion request to SERV.

        Parameters
        ----------
        messages:
            List of {"role": ..., "content": ...} dicts.  Must NOT include a
            system message — pass `system` separately so this method can
            guarantee one is always present.
        system:
            System message content.  Defaults to _DEFAULT_SYSTEM if omitted.
            This is the enforced entry point for the SERV requirement.
        max_completion_tokens, response_format:
            Forwarded to the API.
            Notes confirmed 2026-09-19:
            - SERV uses max_completion_tokens, not max_tokens.
            - SERV does not accept temperature; only the default (1) is supported.

        Returns
        -------
        ServResponse
            Always returns a ServResponse; raises on unrecoverable errors.
        """
        # ── Guard: no system message in messages list ──────────────────
        for msg in messages:
            if msg.get("role") in ("system", "developer"):
                raise ValueError(
                    "Do not include a system/developer message in the `messages` "
                    "list. Pass it via the `system=` parameter so ServClient can "
                    "guarantee the SERV requirement is met."
                )

        # ── Offline stub ───────────────────────────────────────────────
        if self._offline:
            logger.debug("ServClient: offline stub (OFFLINE_DEMO=1)")
            return _make_stub(self._model)

        # ── Build message list ─────────────────────────────────────────
        system_content = system or _DEFAULT_SYSTEM
        full_messages = [
            {"role": "system", "content": system_content},
            *messages,
        ]

        # ── API call with retry on network errors ──────────────────────
        from openai import APIConnectionError, APIStatusError, APITimeoutError  # noqa: PLC0415

        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                wait = 2 ** attempt  # 2s, 4s
                logger.warning("ServClient: retry %d after %.0fs", attempt, wait)
                time.sleep(wait)

            t0 = time.monotonic()
            try:
                kwargs: dict = {
                    "model": self._model,
                    "messages": full_messages,
                    "max_completion_tokens": max_completion_tokens,
                    # temperature not sent — SERV only accepts the default (1)
                }
                if response_format:
                    kwargs["response_format"] = response_format

                raw = self._get_client().chat.completions.create(**kwargs)
                latency_ms = (time.monotonic() - t0) * 1000

                content = (raw.choices[0].message.content or "").strip()
                resp = ServResponse(
                    content=content,
                    model=raw.model,
                    latency_ms=latency_ms,
                )
                if raw.usage:
                    resp.prompt_tokens = raw.usage.prompt_tokens
                    resp.completion_tokens = raw.usage.completion_tokens
                    resp.total_tokens = raw.usage.total_tokens

                logger.debug(
                    "ServClient: OK model=%s latency=%.0fms tokens=%s",
                    raw.model,
                    latency_ms,
                    resp.total_tokens,
                )
                return resp

            except APITimeoutError as exc:
                last_exc = exc
                logger.warning("ServClient: timeout on attempt %d", attempt + 1)
            except APIConnectionError as exc:
                last_exc = exc
                logger.warning("ServClient: connection error on attempt %d: %s", attempt + 1, exc)
            except APIStatusError as exc:
                # 4xx errors are not retried (except the JSON-repair retry handled
                # at the call site).  Re-raise immediately.
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("ServClient: unexpected error on attempt %d: %s", attempt + 1, exc)

        # All retries exhausted
        raise RuntimeError(
            f"ServClient: all 3 attempts failed. Last error: {last_exc}"
        ) from last_exc

    @property
    def model(self) -> str:
        return self._model
