"""The OpenRouter HTTP client and the public model catalogue.

Every request goes through a *transport* ``(method, url, headers, body, timeout) -> (status, data, text)`` (data: the
parsed JSON body or None). The default uses `requests`; tests pass a fake, so no test ever reaches the network.

- chat(body): POST {base}/chat/completions with ``Authorization: Bearer <key>``, ``HTTP-Referer: http://localhost``,
  ``X-Title: WorldSim``. The key is only ever put in that header: errors are redacted before they leave.
- key_info(): GET {base}/key (the "Test key" button).
- ModelCatalog: GET {base}/models (public, no key), cached in <data>/models_cache.json for a day; a built-in entry for
  the default model when the list cannot be fetched (WORLDSIM_OFFLINE=1, no network).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config
from .settings import redact
from .store import read_json, write_json

Transport = Callable[[str, str, dict, Any, float], tuple[int, Any, str]]
CACHE_TTL_S = 24 * 3600

# The default model as listed by GET /api/v1/models on 2026-11-02 (prices in USD per token).
BUILTIN_MODELS = [{
    "id": config.DEFAULT_MODEL, "name": "Google: Gemini 4 Argon", "context_length": 1048576,
    "pricing": {"prompt": "0.000002", "completion": "0.00001"},
    "supported_parameters": ["include_reasoning", "max_tokens", "reasoning", "reasoning_effort", "response_format",
                             "seed", "stop", "structured_outputs", "temperature", "tool_choice", "tools", "top_p"],
    "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"], "default_effort": "medium"},
}]


class OpenRouterError(RuntimeError):
    """An OpenRouter failure with a readable, redacted message. `retryable`: a later attempt may succeed."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = True, kind: str = "http"):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.kind = kind


def requests_transport(method: str, url: str, headers: dict, body: Any, timeout: float) -> tuple[int, Any, str]:
    import requests
    try:
        resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as err:
        raise OpenRouterError(f"OpenRouter did not answer within {timeout:.0f} s", kind="timeout") from err
    except requests.RequestException as err:
        raise OpenRouterError(f"could not reach OpenRouter: {type(err).__name__}", kind="network") from err
    text = resp.text
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data, text


_transport: Transport = requests_transport


def set_transport(transport: Transport | None) -> None:
    """Replace the process-wide transport (tests); None restores the requests transport."""
    global _transport
    _transport = transport or requests_transport


def get_transport() -> Transport:
    return _transport


def _error_text(data: Any, text: str) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            raw = meta.get("raw")
            if raw and isinstance(raw, str) and raw not in str(msg):
                msg = f"{msg} ({raw[:300]})"
            return str(msg)[:600]
        if isinstance(err, str):
            return err[:600]
    return (text or "")[:300]


STATUS_HINT = {400: "bad request", 401: "the key was rejected (401): check it in Settings",
               402: "the account has no credits left (402)", 403: "the request was refused (403)",
               404: "no endpoint for this model and these parameters (404)", 408: "OpenRouter timed out (408)",
               429: "rate limited (429): lower max parallel calls or wait", 502: "the provider failed (502)",
               503: "no provider available right now (503)"}


class OpenRouterClient:
    def __init__(self, api_key: str, transport: Transport | None = None, base: str = config.OPENROUTER_BASE):
        self._key = api_key or ""
        self._transport = transport
        self.base = base.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "HTTP-Referer": config.APP_REFERER,
                "X-Title": config.APP_TITLE, "Content-Type": "application/json"}

    def _send(self, method: str, path: str, body: Any, timeout: float) -> Any:
        if not self._key:
            raise OpenRouterError("no OpenRouter key: add one in Settings (or switch to Mock)", retryable=False,
                                  kind="auth")
        transport = self._transport or get_transport()
        try:
            status, data, text = transport(method, f"{self.base}{path}", self._headers(), body, timeout)
        except OpenRouterError as err:
            raise OpenRouterError(redact(str(err), [self._key]), err.status, err.retryable, err.kind) from None
        except Exception as err:  # noqa: BLE001 - any transport failure becomes a readable, redacted error
            raise OpenRouterError(redact(f"request failed: {type(err).__name__}: {err}", [self._key]),
                                  kind="network") from None
        if status >= 400 or (isinstance(data, dict) and data.get("error") and not data.get("choices")):
            detail = redact(_error_text(data, text), [self._key])
            hint = STATUS_HINT.get(status, f"HTTP {status}")
            retryable = status in (408, 429, 500, 502, 503, 504) or status >= 500
            raise OpenRouterError(f"OpenRouter: {hint}" + (f" — {detail}" if detail else ""), status, retryable,
                                  "auth" if status in (401, 402, 403) else "http")
        if data is None:
            raise OpenRouterError("OpenRouter returned a body that is not JSON", status, True, "parse")
        return data

    def chat(self, body: dict, timeout: float) -> dict:
        return self._send("POST", "/chat/completions", body, timeout)

    def key_info(self, timeout: float = 20) -> dict:
        data = self._send("GET", "/key", None, timeout)
        info = data.get("data") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            raise OpenRouterError("unexpected answer from /key", kind="parse")
        keep = ("limit", "limit_remaining", "limit_reset", "usage", "usage_daily", "usage_weekly", "usage_monthly",
                "is_free_tier", "is_provisioning_key")
        return {k: info.get(k) for k in keep if k in info}


# --------------------------------------------------------------------------------------------- the model catalogue

def _compact(m: dict) -> dict | None:
    if not isinstance(m, dict) or not isinstance(m.get("id"), str):
        return None
    pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}

    def per_token(key):
        try:
            v = float(pricing.get(key))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None

    params = [p for p in (m.get("supported_parameters") or []) if isinstance(p, str)]
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else None
    efforts = []
    if reasoning and isinstance(reasoning.get("supported_efforts"), list):
        efforts = [e for e in reasoning["supported_efforts"] if isinstance(e, str)]
    elif "reasoning" in params or "reasoning_effort" in params:
        efforts = ["low", "medium", "high"]
    return {"id": m["id"], "name": str(m.get("name") or m["id"])[:120],
            "context_length": m.get("context_length") if isinstance(m.get("context_length"), int) else None,
            "prompt_price": per_token("prompt"), "completion_price": per_token("completion"),
            "structured_outputs": "structured_outputs" in params, "response_format": "response_format" in params,
            "reasoning": bool(efforts), "reasoning_efforts": efforts,
            "reasoning_mandatory": bool(reasoning and reasoning.get("mandatory")),
            "default_effort": (reasoning or {}).get("default_effort") if reasoning else None,
            "temperature": "temperature" in params}


class ModelCatalog:
    def __init__(self, root: Path | None = None, transport: Transport | None = None):
        self.root = Path(root) if root is not None else config.data_dir()
        self._transport = transport
        self._lock = threading.Lock()
        self._mem: dict | None = None

    @property
    def path(self) -> Path:
        return self.root / "models_cache.json"

    def _fallback(self, why: str) -> dict:
        return {"models": [c for c in (_compact(m) for m in BUILTIN_MODELS) if c], "fetched_at": None,
                "source": "builtin", "note": why}

    def _fetch(self, timeout: float = 20) -> dict:
        transport = self._transport or get_transport()
        status, data, text = transport("GET", f"{config.OPENROUTER_BASE}/models", {"HTTP-Referer": config.APP_REFERER,
                                                                                   "X-Title": config.APP_TITLE},
                                       None, timeout)
        if status >= 400 or not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise OpenRouterError(f"model list: HTTP {status}", status)
        models = [c for c in (_compact(m) for m in data["data"]) if c]
        models.sort(key=lambda r: r["id"])
        return {"models": models, "fetched_at": time.time(), "source": "openrouter", "note": ""}

    def get(self, refresh: bool = False) -> dict:
        """{models [compact], fetched_at, source openrouter|cache|builtin, note}."""
        with self._lock:
            if not refresh and self._mem and self._fresh(self._mem):
                return self._mem
            cached = None
            if self.path.exists():
                try:
                    cached = read_json(self.path)
                except (OSError, ValueError):
                    cached = None
            if not refresh and cached and self._fresh(cached):
                self._mem = {**cached, "source": "cache"}
                return self._mem
            if config.offline():
                self._mem = {**cached, "source": "cache", "note": "offline (WORLDSIM_OFFLINE)"} if cached \
                    else self._fallback("offline (WORLDSIM_OFFLINE): only the default model is listed")
                return self._mem
            try:
                fresh = self._fetch()
            except (OpenRouterError, Exception) as err:  # noqa: BLE001 - the catalogue is a convenience
                why = f"could not fetch the model list ({type(err).__name__}); "
                self._mem = {**cached, "source": "cache", "note": why + "showing the cached list"} if cached \
                    else self._fallback(why + "only the default model is listed")
                return self._mem
            write_json(self.path, fresh)
            self._mem = fresh
            return fresh

    @staticmethod
    def _fresh(entry: dict) -> bool:
        t = entry.get("fetched_at")
        return isinstance(t, (int, float)) and time.time() - t < CACHE_TTL_S

    def lookup(self, model_id: str) -> dict | None:
        for m in self.get().get("models", []):
            if m["id"] == model_id:
                return m
        for m in (_compact(x) for x in BUILTIN_MODELS):
            if m and m["id"] == model_id:
                return m
        return None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
