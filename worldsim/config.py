"""Paths, ports and environment switches. Everything is read at call time, so tests can change the environment."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
DEFAULT_WORLD_FILE = DEFAULTS_DIR / "us_2026q4.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8940
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
APP_REFERER = "http://localhost"
APP_TITLE = "WorldSim"
BACKENDS = ("openrouter", "mock")


def data_dir() -> Path:
    """Where worlds, settings and the model cache live: $WORLDSIM_DATA_DIR, else <checkout>/data (git-ignored) when
    running from a source checkout, else ~/.worldsim (an installed package)."""
    env = (os.environ.get("WORLDSIM_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    if (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "worldsim").is_dir():
        return REPO_ROOT / "data"
    return Path.home() / ".worldsim"


def port() -> int:
    raw = (os.environ.get("WORLDSIM_PORT") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_PORT
    except ValueError:
        value = DEFAULT_PORT
    return value if 1 <= value <= 65535 else DEFAULT_PORT


def forced_backend() -> str | None:
    """WORLDSIM_LLM=mock (or openrouter) overrides the backend chosen in Settings."""
    raw = (os.environ.get("WORLDSIM_LLM") or "").strip().lower()
    return raw if raw in BACKENDS else None


def offline() -> bool:
    """WORLDSIM_OFFLINE=1: never fetch the public model list (the built-in fallback entry is used)."""
    return (os.environ.get("WORLDSIM_OFFLINE") or "").strip().lower() in ("1", "true", "yes")


def mock_delay_s() -> float:
    """Seconds each mock call sleeps (WORLDSIM_MOCK_DELAY_S, default 0.3) so live progress is visible in the UI."""
    raw = (os.environ.get("WORLDSIM_MOCK_DELAY_S") or "").strip()
    try:
        return max(0.0, min(10.0, float(raw))) if raw else 0.3
    except ValueError:
        return 0.3


def mock_script() -> str:
    """WORLDSIM_MOCK_SCRIPT: a JSON object of scripted mock behaviours (see llm.MockBackend), or ""."""
    return (os.environ.get("WORLDSIM_MOCK_SCRIPT") or "").strip()
