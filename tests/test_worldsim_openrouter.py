"""WorldSim's OpenRouter client through a fake transport: the request shape (URL, headers, model, structured outputs,
usage accounting, reasoning), the fallbacks (json_object, a provider that refuses json_schema), errors (readable,
redacted, retryable or not), cost accounting, the key check and the model catalogue. Never the network."""
import json
import os
import stat
import time

import pytest
from worldsim_fakes import MODELS, TEST_KEY, FakeTransport, chat_response, small_world

from worldsim import openrouter as OR
from worldsim import settings as S
from worldsim.engine import Engine
from worldsim.llm import CallRequest, LLMError, OpenRouterBackend
from worldsim.store import Store


class Catalog:
    def __init__(self, models=MODELS):
        self.models = {c["id"]: c for c in (OR._compact(m) for m in models) if c}

    def lookup(self, mid):
        return self.models.get(mid)


def request(model="google/gemini-4-argon", **kw):
    base = dict(purpose="actor", model=model, schema_name="actor_decision",
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}],
                json_schema={"type": "object", "properties": {"action_id": {"type": "string", "enum": ["hold"]}},
                             "required": ["action_id"], "additionalProperties": False},
                temperature=0.7, timeout_s=33)
    base.update(kw)
    return CallRequest(**base)


def test_the_chat_request_has_the_documented_shape():
    fake = FakeTransport(script=[(200, chat_response('{"action_id": "hold"}'))])
    comp = OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request(reasoning_effort="low",
                                                                                         max_tokens=4096))
    (r,) = fake.requests
    assert r["method"] == "POST" and r["url"] == "https://openrouter.ai/api/v1/chat/completions" and r["timeout"] == 33
    assert r["headers"] == {"Authorization": f"Bearer {TEST_KEY}", "HTTP-Referer": "http://localhost",
                            "X-Title": "WorldSim", "Content-Type": "application/json"}
    b = r["body"]
    assert b["model"] == "google/gemini-4-argon" and b["messages"][1] == {"role": "user", "content": "user"}
    assert b["temperature"] == 0.7 and b["max_tokens"] == 4096 and b["usage"] == {"include": True}
    assert b["reasoning"] == {"effort": "low", "exclude": True}
    rf = b["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "actor_decision" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["properties"]["action_id"]["enum"] == ["hold"]
    assert b["provider"] == {"require_parameters": True}
    assert comp.text == '{"action_id": "hold"}' and comp.response_format == "json_schema"
    assert comp.usage == {"prompt_tokens": 1000, "completion_tokens": 200, "reasoning_tokens": 50, "total_tokens": 1200}
    assert comp.cost == pytest.approx(0.0123) and comp.meta["cost_source"] == "openrouter"


@pytest.mark.parametrize("model, fmt", [("acme/json-only", "json_object"), ("acme/plain", None),
                                        ("someone/unknown-model", "json_object")])
def test_models_without_structured_outputs_get_json_mode_or_nothing(model, fmt):
    fake = FakeTransport(script=[(200, chat_response('{"action_id": "hold"}'))])
    OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request(model=model, reasoning_effort=""))
    b = fake.requests[0]["body"]
    assert (b.get("response_format") or {}).get("type") == fmt
    assert "provider" not in b and "reasoning" not in b


def test_a_provider_that_refuses_json_schema_is_retried_with_json_mode():
    refusal = {"error": {"code": 400, "message": "response_format json_schema is not supported"}}
    fake = FakeTransport(script=[(400, refusal), (200, chat_response('{"action_id": "hold"}'))])
    comp = OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request())
    assert [r["body"]["response_format"]["type"] for r in fake.requests] == ["json_schema", "json_object"]
    assert comp.response_format == "json_object"


@pytest.mark.parametrize("status, retryable, needle", [(401, False, "key was rejected"), (402, False, "no credits"),
                                                       (429, True, "rate limited"), (502, True, "provider failed")])
def test_http_errors_are_readable_and_say_whether_a_retry_helps(status, retryable, needle):
    data = {"error": {"code": status, "message": f"upstream said no to {TEST_KEY}"}}
    fake = FakeTransport(script=[(status, data)])
    with pytest.raises(LLMError) as ei:
        OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request(model="acme/json-only"))
    assert needle in str(ei.value) and ei.value.retryable is retryable and ei.value.status == status
    assert TEST_KEY not in str(ei.value) and "[redacted]" in str(ei.value)


def test_a_transport_exception_carrying_the_key_is_redacted():
    fake = FakeTransport(script=[RuntimeError(f"connection reset; headers Authorization: Bearer {TEST_KEY}")])
    with pytest.raises(LLMError) as ei:
        OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request())
    assert TEST_KEY not in str(ei.value) and "connection reset" in str(ei.value)


def test_no_key_fails_before_any_request():
    fake = FakeTransport()
    with pytest.raises(LLMError) as ei:
        OpenRouterBackend("", catalog=Catalog(), transport=fake).complete(request())
    assert "no OpenRouter key" in str(ei.value) and ei.value.retryable is False and fake.requests == []


def test_an_empty_answer_and_a_provider_error_in_the_choice_are_errors():
    empty = chat_response("")
    empty["choices"][0]["finish_reason"] = "length"
    errored = chat_response("x")
    errored["choices"][0]["error"] = {"code": 502, "message": "provider crashed"}
    for data, needle in ((empty, "finish reason: length"), (errored, "provider crashed"), ({"choices": []}, "no choices")):
        with pytest.raises(LLMError, match=needle):
            OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=FakeTransport(script=[(200, data)])).complete(request())


def test_the_cost_is_computed_from_prices_when_openrouter_does_not_report_it():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}
    fake = FakeTransport(script=[(200, chat_response('{"action_id": "hold"}', usage=usage))])
    comp = OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request())
    assert comp.cost == pytest.approx(2.0 + 1.0) and comp.meta["cost_source"] == "computed"


def test_the_key_check_calls_get_key_and_drops_the_label():
    fake = FakeTransport()
    info = OR.OpenRouterClient(TEST_KEY, transport=fake).key_info()
    (r,) = fake.requests
    assert r["method"] == "GET" and r["url"] == "https://openrouter.ai/api/v1/key" and r["body"] is None
    assert r["headers"]["Authorization"] == f"Bearer {TEST_KEY}"
    assert info == {"limit": 10.0, "limit_remaining": 7.5, "usage": 2.5, "is_free_tier": False}


def test_the_model_catalogue_is_fetched_without_a_key_cached_and_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLDSIM_OFFLINE", "0")
    fake = FakeTransport(models=MODELS)
    cat = OR.ModelCatalog(tmp_path, transport=fake)
    data = cat.get()
    (r,) = fake.requests
    assert r["method"] == "GET" and r["url"] == "https://openrouter.ai/api/v1/models"
    assert "Authorization" not in r["headers"]
    assert data["source"] == "openrouter" and [m["id"] for m in data["models"]][0] == "acme/json-only"
    g = cat.lookup("google/gemini-4-argon")
    assert g["prompt_price"] == 2e-6 and g["completion_price"] == 1e-5 and g["structured_outputs"]
    assert g["reasoning_efforts"] == ["high", "medium", "low"] and g["reasoning_mandatory"]
    assert cat.lookup("openrouter/auto")["prompt_price"] is None           # "-1": unknown price, not negative
    assert OR.ModelCatalog(tmp_path, transport=fake).get()["source"] == "cache" and len(fake.requests) == 1


def test_the_catalogue_falls_back_to_the_default_model_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLDSIM_OFFLINE", "0")
    cat = OR.ModelCatalog(tmp_path, transport=FakeTransport(script=[]))

    def down(*args, **kwargs):
        raise RuntimeError("offline")
    cat._transport = down
    data = cat.get()
    assert data["source"] == "builtin" and [m["id"] for m in data["models"]] == ["google/gemini-4-argon"]
    assert "could not fetch" in data["note"]


def test_an_iteration_over_openrouter_never_writes_or_logs_the_key(tmp_path, caplog):
    caplog.set_level("DEBUG")
    store = Store(tmp_path / "ws")
    S.save({**S.DEFAULTS, "api_key": TEST_KEY, "backend": "openrouter"}, store.root)
    w = store.create_world(small_world())
    fake = FakeTransport(script=["auto", (500, {"error": {"message": f"oops {TEST_KEY}"}})])
    catalog = Catalog()
    eng = Engine(store, lambda: S.load(store.root),
                 backend_factory=lambda s: OpenRouterBackend(s["api_key"], catalog=catalog, transport=fake))
    assert eng.run(w["id"])["status"] == "done"
    it = store.load_iteration(w["id"], 1)
    assert len(fake.chats) == 9                                    # 3 actors, 1 retry, the synthesizer's 5 steps
    assert it["cost"] == pytest.approx(0.0123 * 8) and it["usage"]["prompt_tokens"] == 8000
    assert [s["status"] for s in it["synthesizer"]["steps"]] == ["ok"] * 5
    schemas = [r["body"]["response_format"]["json_schema"] for r in fake.chats]
    assert [x["name"] for x in schemas[4:]] == ["digest", "indicator_values", "indicator_values", "question_statuses",
                                                "narrative"]
    assert store.load_step(w["id"], 1)["statuses"] == {"talks": "status of talks", "mood": "status of mood"}
    assert any("redacted" in (c.get("error") or "") for c in it["calls"])        # the 500 was retried, its text redacted
    assert all(r["headers"]["Authorization"] == f"Bearer {TEST_KEY}" for r in fake.chats)
    # the key appears in exactly one file, settings.json, which only the owner can read
    for path in (tmp_path / "ws").rglob("*"):
        if path.is_file():
            has = TEST_KEY in path.read_text(encoding="utf-8", errors="replace")
            assert has == (path.name == "settings.json"), path
    mode = stat.S_IMODE(os.stat(tmp_path / "ws" / "settings.json").st_mode)
    assert mode == 0o600
    assert TEST_KEY not in caplog.text
    assert json.dumps(S.public(S.load(store.root))).find(TEST_KEY) == -1


def test_settings_are_saved_0600_masked_and_validated(tmp_path):
    S.save({**S.DEFAULTS, "api_key": TEST_KEY}, tmp_path)
    assert stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode) == 0o600
    pub = S.public(S.load(tmp_path))
    assert pub["has_key"] and pub["key_masked"] == "sk-or-…cdef" and "api_key" not in pub
    assert S.mask_key("short") == "•••• (short key)" and S.mask_key("") == ""
    new, problems = S.validate({"temperature": 5, "max_parallel": 0, "backend": "x", "api_key": "a b"}, S.DEFAULTS)
    assert len(problems) == 4 and new == S.DEFAULTS
    assert S.redact(f"Bearer {TEST_KEY} and {TEST_KEY[:30]}xyz") == "Bearer [redacted] and [redacted]"


def test_tests_never_reach_the_network():
    with pytest.raises(AssertionError, match="never reach the network"):
        OR.get_transport()("GET", "https://openrouter.ai/api/v1/models", {}, None, 1)
    assert time.time() > 0
