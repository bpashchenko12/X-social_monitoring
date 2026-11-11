"""OpenRouter chat client with a monthly budget guard (SPEC section 12).

Stdlib only. Never logs or raises the API key. Every request that returns usage is written to an
append-only ledger (data/llm_runs.jsonl) so the monthly cap survives across CI runs (the ledger is
committed to git like the Apify one).
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from . import apify
from .common import ROOT, iso, now_utc

LLM_LEDGER_PATH = os.path.join(ROOT, "data", "llm_runs.jsonl")
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(RuntimeError):
    """A single request failed (retry/hold the affected posts, keep going)."""


class LLMFatal(LLMError):
    """Auth/credit/model problems: no point sending more requests in this run."""


class LLMBudgetError(LLMFatal):
    """Monthly LLM budget reached."""


def _scrub(text, secret):
    text = str(text)
    return text.replace(secret, "***") if secret else text


class OpenRouter:
    def __init__(self, api_key, cfg, ledger_path=LLM_LEDGER_PATH, urlopen=urllib.request.urlopen,
                 sleep=time.sleep, now=now_utc):
        if not api_key:
            raise LLMFatal("OPENROUTER_API_KEY is not set")
        self._key = api_key
        self.cfg = cfg
        self.ledger_path = ledger_path
        self._urlopen = urlopen
        self._sleep = sleep
        self._now = now
        self.base = cfg.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self.session_cost = 0.0
        self.requests = 0

    # ---- budget -----------------------------------------------------------
    def spent_month(self):
        return apify.spent_usd(self.ledger_path, self._now().strftime("%Y-%m"))

    def check_budget(self):
        cap = self.cfg.get("monthly_budget_usd")
        if cap is not None and self.spent_month() >= float(cap):
            raise LLMBudgetError(f"LLM monthly budget reached (${self.spent_month():.2f} >= ${float(cap):.2f})")

    def _cost(self, usage):
        if isinstance(usage.get("cost"), (int, float)):
            return float(usage["cost"])
        price = self.cfg.get("assumed_usd_per_mtok", {"prompt": 6.0, "completion": 30.0})
        return (usage.get("prompt_tokens", 0) * price["prompt"] + usage.get("completion_tokens", 0) * price["completion"]) / 1e6

    def _ledger(self, model, purpose, usage, ok):
        apify._append_ledger({"ts": iso(self._now()), "model": model, "purpose": purpose, "ok": ok,
                              "prompt_tokens": usage.get("prompt_tokens", 0),
                              "completion_tokens": usage.get("completion_tokens", 0),
                              "cost_usd": round(self._cost(usage), 6)}, self.ledger_path)

    # ---- transport ----------------------------------------------------------
    def _request(self, path, payload=None, timeout=None):
        req = urllib.request.Request(
            self.base + path, data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method="GET" if payload is None else "POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "X-Title": "xm-monitor"})
        with self._urlopen(req, timeout=timeout or self.cfg.get("timeout_s", 120)) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def list_models(self):
        """Set of model ids, or None when the listing is unavailable (do not block on it)."""
        try:
            data = self._request("/models", timeout=30)
            return {m["id"] for m in data["data"]}
        except Exception:
            return None

    def chat(self, messages, model, purpose="translate", json_mode=True):
        """-> {"text", "model", "cost_usd", "finish_reason"}. Retries transient errors."""
        self.check_budget()
        payload = {"model": model, "messages": messages, "temperature": self.cfg.get("temperature", 0),
                   "max_tokens": self.cfg.get("max_tokens", 8000), "usage": {"include": True}}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.cfg.get("provider"):
            payload["provider"] = self.cfg["provider"]
        attempts = int(self.cfg.get("retries", 2)) + 1
        last = None
        for i in range(attempts):
            if i:
                self._sleep(float(self.cfg.get("retry_backoff_s", 4)) * (2 ** (i - 1)))
            try:
                self.requests += 1
                data = self._request("/chat/completions", payload)
            except urllib.error.HTTPError as e:
                body = _scrub(e.read().decode("utf-8", "replace")[:300], self._key)
                if e.code in (401, 403):
                    raise LLMFatal(f"OpenRouter auth error {e.code}: {body}") from None
                if e.code == 402:
                    raise LLMFatal(f"OpenRouter: insufficient credits or key limit reached: {body}") from None
                if e.code in (400, 404) and "response_format" in body and "response_format" in payload:
                    payload.pop("response_format")  # model does not support JSON mode: rely on the prompt
                    last = LLMError(f"HTTP {e.code} (response_format unsupported)")
                    continue
                if e.code in (400, 404):
                    raise LLMFatal(f"OpenRouter rejected the request ({e.code}); check the model id "
                                   f"'{model}': {body}") from None
                last = LLMError(f"OpenRouter HTTP {e.code}: {body}")
                if e.code in (408, 409, 425, 429) or e.code >= 500:
                    continue
                raise last from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ValueError) as e:
                last = LLMError(f"OpenRouter request failed: {_scrub(type(e).__name__ + ': ' + str(e), self._key)}")
                continue
            usage = data.get("usage") or {}
            err = data.get("error")
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                msg = _scrub(err.get("message", err) if isinstance(err, dict) else err, self._key)
                if code in (401, 402, 403):
                    raise LLMFatal(f"OpenRouter error {code}: {msg}")
                last = LLMError(f"OpenRouter error {code}: {msg}")
                continue
            try:
                choice = data["choices"][0]
                text = choice["message"]["content"]
            except (KeyError, IndexError, TypeError):
                last = LLMError("OpenRouter response has no choices")
                continue
            ok = bool(text and str(text).strip())
            self._ledger(model, purpose, usage, ok)
            cost = self._cost(usage)
            self.session_cost += cost
            if not ok:
                last = LLMError("empty completion")
                continue
            return {"text": text, "model": data.get("model", model), "cost_usd": cost,
                    "finish_reason": choice.get("finish_reason")}
        raise last or LLMError("OpenRouter request failed")

    def resolve_model(self):
        """Pick the configured model, else the fallback; (id, note). Raises LLMFatal if neither is
        usable. When the listing endpoint is unreachable the configured model is used as is."""
        primary, fallback = self.cfg["model"], self.cfg.get("fallback_model")
        if not self.cfg.get("verify_model_listing", True):
            return primary, ""
        ids = self.list_models()
        if ids is None or primary in ids:
            return primary, ""
        if fallback and fallback in ids:
            return fallback, f"model '{primary}' not found on OpenRouter; using fallback '{fallback}'"
        raise LLMFatal(f"model '{primary}' not found on OpenRouter"
                       + (f" (fallback '{fallback}' also missing)" if fallback else " and no fallback_model is set"))
