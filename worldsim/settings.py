"""Settings in <data>/settings.json, written with mode 0600 (it holds the OpenRouter key).

The key never leaves this module except to the OpenRouter client: public() returns a masked view
(``sk-or-…abcd``) and every error text that could carry it goes through redact().
"""
from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import config
from . import model as M
from .store import read_json, write_json

DEFAULTS: dict[str, Any] = {
    "backend": "openrouter",          # openrouter | mock
    "api_key": "",
    "default_model": config.DEFAULT_MODEL,
    "temperature": 0.7,
    "reasoning_effort": "",           # "" = the model's default
    "max_parallel": 6,
    "timeout_s": 240,
    "max_tokens": None,               # None = the provider's default
}
_LOCK = threading.Lock()
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I)
KEYLIKE_RE = re.compile(r"sk-or-[A-Za-z0-9._\-]{6,}")


def settings_path(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else config.data_dir()) / "settings.json"


def load(root: Path | None = None) -> dict:
    path = settings_path(root)
    data: dict = {}
    if path.exists():
        try:
            raw = read_json(path)
            data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            data = {}
    out = copy.deepcopy(DEFAULTS)
    for k in DEFAULTS:
        if k in data:
            out[k] = data[k]
    clean, _ = validate(out, base=copy.deepcopy(DEFAULTS))
    return clean


def save(settings: dict, root: Path | None = None) -> None:
    path = settings_path(root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}, mode=0o600)
        os.chmod(path, 0o600)


def validate(patch: dict, base: dict) -> tuple[dict, list[str]]:
    """`base` updated with the readable entries of `patch`; problems for the rest (base values kept)."""
    out = dict(base)
    problems: list[str] = []
    if "backend" in patch:
        b = str(patch.get("backend") or "").strip().lower()
        if b in config.BACKENDS:
            out["backend"] = b
        else:
            problems.append("backend must be openrouter or mock")
    if "api_key" in patch:
        key = patch.get("api_key")
        key = key.strip() if isinstance(key, str) else ""
        if key and (len(key) > 400 or any(c.isspace() for c in key)):
            problems.append("the API key must be one line without spaces")
        else:
            out["api_key"] = key
    if "default_model" in patch:
        m = str(patch.get("default_model") or "").strip()
        if not m or len(m) > 200 or any(c.isspace() for c in m):
            problems.append("default model must be a model id such as google/gemini-4-argon")
        else:
            out["default_model"] = m
    if "temperature" in patch:
        num, _, ok = M.coerce_value({"type": "number"}, patch.get("temperature"))
        if not ok or not 0 <= num <= 2:
            problems.append("temperature must be between 0 and 2")
        else:
            out["temperature"] = num
    if "reasoning_effort" in patch:
        e = str(patch.get("reasoning_effort") or "").strip().lower()
        if e not in M.REASONING_EFFORTS:
            problems.append("reasoning effort must be blank (model default) or one of none, minimal, low, medium, "
                            "high, xhigh, max")
        else:
            out["reasoning_effort"] = e
    for key, lo, hi, label in (("max_parallel", 1, 32, "max parallel calls"), ("timeout_s", 10, 1800, "timeout")):
        if key in patch:
            num, _, ok = M.coerce_value({"type": "integer"}, patch.get(key))
            if not ok or not lo <= num <= hi:
                problems.append(f"{label} must be an integer from {lo} to {hi}")
            else:
                out[key] = num
    if "max_tokens" in patch:
        v = patch.get("max_tokens")
        if v in (None, "", 0):
            out["max_tokens"] = None
        else:
            num, _, ok = M.coerce_value({"type": "integer"}, v)
            if not ok or not 256 <= num <= 1_000_000:
                problems.append("max tokens must be blank or an integer from 256 to 1000000")
            else:
                out["max_tokens"] = num
    return out, problems


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) < 16:
        return "•••• (short key)"
    head = "sk-or-" if key.startswith("sk-or-") else ""
    return f"{head}…{key[-4:]}"


def effective_backend(settings: dict) -> str:
    return config.forced_backend() or settings.get("backend") or "openrouter"


def public(settings: dict) -> dict:
    """What the browser may see: everything except the key, which is masked."""
    out = {k: v for k, v in settings.items() if k != "api_key"}
    out["has_key"] = bool(settings.get("api_key"))
    out["key_masked"] = mask_key(settings.get("api_key") or "")
    out["effective_backend"] = effective_backend(settings)
    out["forced_backend"] = config.forced_backend()
    return out


def redact(text: Any, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """`text` with every given secret, every 'Bearer …' token and every sk-or-… string replaced by [redacted]."""
    s = str(text if text is not None else "")
    for secret in secrets:
        if secret and len(secret) >= 4:
            s = s.replace(secret, "[redacted]")
    s = BEARER_RE.sub(r"\1[redacted]", s)
    return KEYLIKE_RE.sub("[redacted]", s)
