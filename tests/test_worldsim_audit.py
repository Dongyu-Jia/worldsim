"""The audit view: what each call records (the request as sent, the model's thinking as returned, the raw and parsed
answer, the verdict), the thinking request and its parsing, the HTTP attempts of a fallback, and the audit API for a
saved iteration, a failed attempt awaiting retry and an iteration saved before the audit fields. Never the network."""
import json

import pytest
from fastapi.testclient import TestClient
from worldsim_fakes import MODELS, TEST_KEY, FakeTransport, chat_response, small_world

from worldsim import audit as A
from worldsim import llm
from worldsim import openrouter as OR
from worldsim.llm import CallRequest, OpenRouterBackend, parse_thinking
from worldsim.server import create_app

BASE = "http://127.0.0.1:8940"
HEAVY = ("messages", "request", "thinking", "raw_response", "parsed", "response_text")


class Catalog:
    def __init__(self, models=MODELS):
        self.models = {c["id"]: c for c in (OR._compact(m) for m in models) if c}

    def lookup(self, mid):
        return self.models.get(mid)


def request(model="google/gemini-4-argon", **kw):
    base = dict(purpose="actor", model=model, schema_name="actor_decision",
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}],
                json_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"],
                             "additionalProperties": False}, temperature=0.7, timeout_s=33)
    base.update(kw)
    return CallRequest(**base)


def sent_reasoning(**kw):
    fake = FakeTransport(script=[(200, chat_response('{"x": "y"}'))])
    OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request(**kw))
    return fake.chats[0]["body"].get("reasoning", "absent")


def test_the_thinking_is_asked_for_only_from_models_that_take_the_reasoning_parameter():
    assert sent_reasoning() == {"exclude": False}
    assert sent_reasoning(reasoning_effort="low") == {"effort": "low", "exclude": False}
    assert sent_reasoning(record_thinking=False) == "absent"                          # the setting off: as before
    assert sent_reasoning(record_thinking=False, reasoning_effort="low") == {"effort": "low", "exclude": True}
    assert sent_reasoning(model="acme/json-only") == "absent"                         # no reasoning parameter
    assert sent_reasoning(model="acme/json-only", reasoning_effort="low") == "absent"
    assert sent_reasoning(model="someone/unknown") == "absent"                        # not in the list: nothing asked


def test_the_thinking_is_kept_exactly_as_the_provider_returned_it():
    plain = parse_thinking({"content": "{}", "reasoning": "Step 1.\nStep 2."}, "on", 42)
    assert plain["text"] == "Step 1.\nStep 2." and plain["source"] == "reasoning" and plain["reasoning_tokens"] == 42
    blocks = parse_thinking({"reasoning_details": [
        {"type": "reasoning.summary", "summary": "**Weighing the rate**\nInflation is high.", "format": "google", "index": 0},
        {"type": "reasoning.encrypted", "data": "c2VjcmV0", "format": "google", "index": 1},
        {"type": "reasoning.text", "text": "Then the balance sheet.", "signature": "sig", "index": 2}]}, "on")
    assert blocks["source"] == "reasoning_details" and blocks["encrypted_blocks"] == 1
    assert blocks["text"] == "**Weighing the rate**\nInflation is high.\n\nThen the balance sheet."
    assert [b["type"] for b in blocks["blocks"]] == ["summary", "text"] and blocks["blocks"][1]["signed"] is True
    assert "c2VjcmV0" not in json.dumps(blocks)                                       # encrypted data is not kept
    none = parse_thinking({"content": "{}"}, "unsupported")
    assert none["text"] == "" and none["source"] == "none" and none["capture"] == "unsupported"


def test_a_completion_carries_the_thinking_the_request_and_the_raw_response():
    data = chat_response('{"x": "y"}')
    data["choices"][0]["message"]["reasoning"] = "I compare the options."
    fake = FakeTransport(script=[(200, data)])
    comp = OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request())
    assert comp.thinking["text"] == "I compare the options." and comp.thinking["capture"] == "on"
    assert comp.audit["raw_response"]["choices"][0]["message"]["reasoning"] == "I compare the options."
    sent = fake.chats[0]["body"]
    assert comp.audit["request"] == {k: v for k, v in sent.items() if k != "messages"}
    assert A.exact_request({"request": comp.audit["request"], "messages": sent["messages"]}) == sent
    assert comp.usage["cached_tokens"] == 0 and comp.meta["native_finish_reason"] == "STOP"
    assert comp.audit["http_attempts"] == [{"format": "json_schema", "status": 200, "ok": True, "error": None,
                                            "duration_s": comp.audit["http_attempts"][0]["duration_s"]}]
    assert TEST_KEY not in json.dumps(comp.audit) and TEST_KEY not in json.dumps(comp.thinking)


def test_a_refused_schema_and_its_fallback_are_both_recorded():
    fake = FakeTransport(script=[(400, {"error": {"message": "schema not supported"}}), (200, chat_response('{"x": "y"}'))])
    comp = OpenRouterBackend(TEST_KEY, catalog=Catalog(), transport=fake).complete(request())
    first, second = comp.audit["http_attempts"]
    assert (first["format"], first["status"], first["ok"]) == ("json_schema", 400, False)
    assert first["request"]["response_format"]["type"] == "json_schema"            # the refused body, schema included
    assert (second["format"], second["status"], second["ok"]) == ("json_object", 200, True)
    assert comp.audit["request"]["response_format"] == {"type": "json_object"}


# ------------------------------------------------------------------------------------------------ the audit API

def mock_client(tmp_path, monkeypatch, script=None):
    monkeypatch.setattr(llm, "_SCRIPTED", {})
    if script:
        monkeypatch.setenv("WORLDSIM_MOCK_SCRIPT", json.dumps(script))
    app = create_app(tmp_path / "ws")
    client = TestClient(app, base_url=BASE)
    assert client.put("/api/settings", json={"backend": "mock"}).status_code == 200
    r = client.post("/api/worlds/import", json=small_world())
    assert r.status_code == 200, r.text
    return app, client, r.json()["world"]["id"]


def run(app, client, wid, mode="run"):
    assert client.post(f"/api/worlds/{wid}/{mode}", json={}).status_code == 200
    app.state.runs.wait(wid, 60)
    return client.get(f"/api/worlds/{wid}/run").json()["run"]


def test_every_attempt_of_an_actor_is_auditable(tmp_path, monkeypatch):
    app, client, wid = mock_client(tmp_path, monkeypatch, {"actor:a1": ["bad_json"]})
    assert run(app, client, wid)["status"] == "done"
    idx = client.get(f"/api/worlds/{wid}/iterations/1/audit").json()
    who = [p["who"] for p in idx["participants"]]
    assert who[:3] == ["a0", "a1", "a2"] and who[3] == "synthesizer:digest" and who[-1] == "synthesizer:narrative"
    a1 = idx["participants"][1]
    assert a1["attempts"] == 2 and a1["has_thinking"] and a1["status"] == "ok" and not a1["legacy"]

    d = client.get(f"/api/worlds/{wid}/iterations/1/audit", params={"who": "a1"}).json()
    bad, fixed = d["attempts"]
    assert not bad["output"]["accepted"] and bad["output"]["parsed"] is None and bad["output"]["validation_error"]
    assert bad["output"]["text"].startswith("Sure! Here is my answer")
    assert fixed["repair"] and fixed["output"]["accepted"] and fixed["output"]["validation_error"] is None
    roles = [m["role"] for m in fixed["input"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]                          # the repair shows the bad answer
    assert fixed["input"]["messages"][2]["content"] == bad["output"]["text"]
    for at in (bad, fixed):
        assert at["input"]["request"]["messages"] == at["input"]["messages"]
        assert at["input"]["schema"]["type"] == "object" and at["input"]["params"]["schema_name"] == "actor_decision"
        assert at["thinking"]["mock"] and at["thinking"]["text"].startswith("(mock thinking)")
        assert at["thinking"]["reasoning_tokens"] > 0 and at["output"]["raw"]["choices"][0]["message"]["content"]
    saved = client.get(f"/api/worlds/{wid}/steps/1").json()["iteration"]["actors"][1]
    assert d["final"]["output"] == saved["output"] and d["participant"]["label"] == "Actor 1"

    step = client.get(f"/api/worlds/{wid}/iterations/1/audit", params={"who": "synthesizer:values:1"}).json()
    assert step["attempts"][0]["output"]["accepted"] and step["final"]["items"]
    assert client.get(f"/api/worlds/{wid}/iterations/1/audit", params={"who": "nobody"}).status_code == 404
    assert client.get(f"/api/worlds/{wid}/iterations/9/audit").status_code == 404
    assert client.get(f"/api/worlds/{wid}/pending/audit").status_code == 404


def test_the_step_payload_does_not_carry_the_heavy_audit_fields(tmp_path, monkeypatch):
    app, client, wid = mock_client(tmp_path, monkeypatch)
    run(app, client, wid)
    calls = client.get(f"/api/worlds/{wid}/steps/1").json()["iteration"]["calls"]
    assert calls and not any(k in c for c in calls for k in HEAVY)
    assert all("accepted" in c and "has_thinking" in c for c in calls)


def test_a_failed_attempt_awaiting_retry_is_auditable(tmp_path, monkeypatch):
    app, client, wid = mock_client(tmp_path, monkeypatch, {"synth": ["error"] * 40})
    assert run(app, client, wid)["status"] == "failed"
    idx = client.get(f"/api/worlds/{wid}/pending/audit").json()
    assert idx["pending"] and len(idx["participants"]) == 3 + len([p for p in idx["participants"] if p["kind"] == "synth"])
    d = client.get(f"/api/worlds/{wid}/pending/audit", params={"who": "synthesizer:digest"}).json()
    assert d["attempts"] and all(not a["output"]["accepted"] and a["meta"]["error"] for a in d["attempts"])
    assert client.get(f"/api/worlds/{wid}/pending/audit", params={"who": "a2"}).json()["attempts"][0]["output"]["accepted"]


def test_an_iteration_saved_before_the_audit_fields_still_audits():
    record = {"actors": [{"actor_id": "a", "name": "A", "status": "ok", "output": {"answers": []}}],
              "synthesizer": {"steps": [{"key": "narrative", "kind": "narrative", "label": "Narrative", "status": "ok"}]},
              "calls": [{"who": "a", "attempt": 1, "repair": False, "messages": [{"role": "user", "content": "hi"}],
                         "response_text": "{}"}]}
    (p, s) = A.index(record, None)
    assert p["legacy"] and p["attempts"] == 1 and s["who"] == "synthesizer:narrative" and s["attempts"] == 0
    at = A.detail(record, None, "a")["attempts"][0]
    assert at["legacy"] and at["thinking"] is None and at["input"]["params"] is None
    assert at["input"]["messages"] == [{"role": "user", "content": "hi"}] and at["output"]["text"] == "{}"


def test_the_key_appears_in_no_audit_response(tmp_path):
    fake = FakeTransport()
    app = create_app(tmp_path / "ws", transport=fake)
    client = TestClient(app, base_url=BASE)
    client.put("/api/settings", json={"api_key": TEST_KEY, "backend": "openrouter"})
    wid = client.post("/api/worlds/import", json=small_world()).json()["world"]["id"]
    run(app, client, wid)
    for params in (None, {"who": "a1"}, {"who": "synthesizer:digest"}):
        r = client.get(f"/api/worlds/{wid}/iterations/1/audit", params=params)
        assert r.status_code == 200 and TEST_KEY not in r.text
    assert "Bearer" not in client.get(f"/api/worlds/{wid}/iterations/1/audit", params={"who": "a1"}).text


@pytest.mark.parametrize("on", [True, False])
def test_the_thinking_setting_reaches_the_request(tmp_path, on):
    fake = FakeTransport()
    app = create_app(tmp_path / "ws", transport=fake)
    client = TestClient(app, base_url=BASE)
    client.put("/api/settings", json={"api_key": TEST_KEY, "backend": "openrouter", "record_thinking": on})
    wid = client.post("/api/worlds/import", json=small_world()).json()["world"]["id"]
    run(app, client, wid)
    assert {json.dumps(c["body"].get("reasoning")) for c in fake.chats} == {json.dumps({"exclude": False}) if on else "null"}
