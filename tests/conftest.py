"""Shared fixtures. No test ever reaches the network or spends tokens.

* `worldsim_isolated` (every test): WorldSim keeps its data under the test's tmp_path, never fetches the model
  list (WORLDSIM_OFFLINE), its mock answers at once, and its OpenRouter transport refuses every request: a test
  that needs OpenRouter passes its own fake transport. No test can reach openrouter.ai.
"""
import pytest

from worldsim import openrouter as ws_openrouter


def _no_network(method, url, headers, body, timeout):
    raise AssertionError(f"tests never reach the network (WorldSim {method} {url})")


@pytest.fixture(autouse=True)
def worldsim_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORLDSIM_DATA_DIR", str(tmp_path / "worldsim"))
    monkeypatch.setenv("WORLDSIM_OFFLINE", "1")
    monkeypatch.setenv("WORLDSIM_MOCK_DELAY_S", "0")
    monkeypatch.delenv("WORLDSIM_LLM", raising=False)
    monkeypatch.delenv("WORLDSIM_PORT", raising=False)
    monkeypatch.delenv("WORLDSIM_MOCK_SCRIPT", raising=False)
    ws_openrouter.set_transport(_no_network)
    yield
    ws_openrouter.set_transport(_no_network)
