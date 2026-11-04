"""Checkpoints: an iteration interrupted by a server restart (simulated by an exception that escapes the run, as a
killed process would leave it) or paused by a refused key resumes from where it stopped. What finished is reused
(never called again), the rest is called, and the result is one normal iteration. Never the network."""
import json

import pytest
from fastapi.testclient import TestClient
from worldsim_fakes import TEST_KEY, FakeTransport, small_world

from worldsim import settings as S
from worldsim.engine import Engine
from worldsim.llm import LLMError, MockBackend
from worldsim.server import create_app
from worldsim.store import Conflict, NotFound, Store

BASE = "http://127.0.0.1:8940"


class Crash(BaseException):
    """Stands for the process dying: not an Exception, so nothing in the engine catches it."""


class Scripted(MockBackend):
    """The mock, with a crash or an error injected at the n-th call of a kind (counted across the backend's life)."""

    def __init__(self, crash_at=None, auth_at=None):
        super().__init__(delay_s=0)
        self.crash_at, self.auth_at = crash_at or {}, auth_at or {}
        self.count = {"actor": 0, "synth": 0}

    def complete(self, req):
        self.count[req.purpose] += 1
        n = self.count[req.purpose]
        if self.crash_at.get(req.purpose) == n:
            raise Crash()
        if self.auth_at.get(req.purpose) == n:
            self.calls.append(req)
            raise LLMError("HTTP 402: insufficient credits", "auth", retryable=False)
        return super().complete(req)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ws")


def engine(store, backend, max_parallel=1):
    settings = {**S.DEFAULTS, "backend": "mock", "max_parallel": max_parallel}
    return Engine(store, lambda: settings, backend_factory=lambda s: backend)


def asked(backend, purpose):
    return [r.context.get("actor", {}).get("id") if purpose == "actor" else (r.context.get("step") or {}).get("key")
            for r in backend.calls if r.purpose == purpose and not r.repair]


def test_a_crash_during_the_synthesizer_resumes_without_calling_anything_twice(store):
    w = store.create_world(small_world(n_actors=3))
    first = Scripted(crash_at={"synth": 3})                     # dies on the third synthesizer step
    with pytest.raises(Crash):
        engine(store, first).run(w["id"])
    cp = store.load_checkpoint(w["id"])
    assert cp["status"] == "running" and cp["phase"] == "synthesizer" and len(cp["results"]) == 3
    assert [s["key"] for s in cp["steps"]] == ["digest", "values:1"] and store.step_numbers(w["id"]) == [0]
    with pytest.raises(Conflict):                               # a new run would throw the work away
        engine(store, MockBackend(delay_s=0)).run(w["id"])

    again = MockBackend(delay_s=0)                              # a fresh engine, as after a restart
    result = engine(store, again).run(w["id"], resume=True)
    assert result["status"] == "done" and store.step_numbers(w["id"]) == [0, 1]
    assert asked(again, "actor") == []                          # every actor's answer was reused
    done_before = {"digest", "values:1"}
    assert done_before.isdisjoint(asked(again, "synth")) and "values:2" in asked(again, "synth")
    it = store.load_iteration(w["id"], 1)
    assert [s["key"] for s in it["synthesizer"]["steps"]][:2] == ["digest", "values:1"]
    assert {c["who"] for c in it["calls"]} >= {"a0", "a1", "a2", "synthesizer:digest", "synthesizer:narrative"}
    assert store.load_checkpoint(w["id"]) is None


def test_a_crash_while_actors_answer_keeps_the_answers_and_calls_only_the_rest(store):
    w = store.create_world(small_world(n_actors=3))
    first = Scripted(crash_at={"actor": 3})
    with pytest.raises(Crash):
        engine(store, first).run(w["id"])
    cp = store.load_checkpoint(w["id"])
    assert cp["phase"] == "actors" and sorted(cp["answered"]) == ["a0", "a1"]
    again = MockBackend(delay_s=0)
    assert engine(store, again).run(w["id"], resume=True)["status"] == "done"
    assert asked(again, "actor") == ["a2"]
    outs = {r["actor_id"]: r["output"] for r in store.load_iteration(w["id"], 1)["actors"]}
    assert outs["a0"] == cp["answered"]["a0"]["output"]         # the saved answer, not a new one


def test_no_credits_pauses_and_resume_finishes_the_iteration(store):
    w = store.create_world(small_world(n_actors=3))
    first = Scripted(auth_at={"actor": 2})                      # the second actor call: HTTP 402
    result = engine(store, first).run(w["id"])
    assert result["status"] == "paused" and "insufficient credits" in result["error"]
    assert len([r for r in first.calls if r.purpose == "actor"]) == 2 and not asked(first, "synth")
    cp = store.load_checkpoint(w["id"])
    assert cp["status"] == "paused" and sorted(cp["answered"]) == ["a0"]
    again = MockBackend(delay_s=0)
    assert engine(store, again).run(w["id"], resume=True)["status"] == "done"
    assert sorted(asked(again, "actor")) == ["a1", "a2"]


def test_a_refused_key_in_the_synthesizer_pauses_with_the_finished_steps_saved(store):
    w = store.create_world(small_world(n_actors=2))
    first = Scripted(auth_at={"synth": 2})
    assert engine(store, first).run(w["id"])["status"] == "paused"
    cp = store.load_checkpoint(w["id"])
    assert cp["status"] == "paused" and [s["key"] for s in cp["steps"]] == ["digest"]
    again = MockBackend(delay_s=0)
    assert engine(store, again).run(w["id"], resume=True)["status"] == "done"
    assert asked(again, "actor") == [] and "digest" not in asked(again, "synth")


def test_resume_is_refused_after_the_world_was_edited_or_without_a_checkpoint(store):
    w = store.create_world(small_world(n_actors=2))
    with pytest.raises(NotFound):
        engine(store, MockBackend(delay_s=0)).run(w["id"], resume=True)
    with pytest.raises(Crash):
        engine(store, Scripted(crash_at={"synth": 1})).run(w["id"])
    world = store.load_world(w["id"])
    world["description"] = "edited"
    store.save_world(world)
    with pytest.raises(Conflict):
        engine(store, MockBackend(delay_s=0)).run(w["id"], resume=True)


def test_rewind_drops_an_interrupted_iteration(store):
    w = store.create_world(small_world(n_actors=1))
    engine(store, MockBackend(delay_s=0)).run(w["id"])
    with pytest.raises(Crash):
        engine(store, Scripted(crash_at={"synth": 1})).run(w["id"])
    assert store.load_checkpoint(w["id"]) is not None
    store.rewind(w["id"], 0)
    assert store.load_checkpoint(w["id"]) is None


# ------------------------------------------------------------------------------------------------ through the API

def test_the_api_shows_resumes_and_discards_an_interrupted_iteration(tmp_path):
    fake = FakeTransport(script=["auto", (402, {"error": {"message": "Insufficient credits"}})])
    app = create_app(tmp_path / "ws", transport=fake)
    client = TestClient(app, base_url=BASE)
    client.put("/api/settings", json={"api_key": TEST_KEY, "backend": "openrouter", "max_parallel": 1})
    wid = client.post("/api/worlds/import", json=small_world()).json()["world"]["id"]
    assert client.post(f"/api/worlds/{wid}/run", json={}).status_code == 200
    app.state.runs.wait(wid, 60)
    run = client.get(f"/api/worlds/{wid}/run").json()["run"]
    assert run["status"] == "paused" and "402" in run["error"]
    info = client.get(f"/api/worlds/{wid}").json()["interrupted"]
    assert info["status"] == "paused" and info["actors_done"] == 1 and info["actors_total"] == 3 and info["resumable"]
    audit = client.get(f"/api/worlds/{wid}/interrupted/audit").json()
    assert audit["interrupted"] and [p["who"] for p in audit["participants"]][:1] == ["a0"]
    assert client.post(f"/api/worlds/{wid}/run", json={}).status_code == 409      # resume or discard first
    assert TEST_KEY not in json.dumps(client.get(f"/api/worlds/{wid}").json())

    assert client.post(f"/api/worlds/{wid}/resume", json={}).status_code == 200
    app.state.runs.wait(wid, 60)
    assert client.get(f"/api/worlds/{wid}/run").json()["run"]["status"] == "done"
    assert client.get(f"/api/worlds/{wid}").json()["interrupted"] is None
    actor_bodies = [c["body"] for c in fake.chats if c["body"]["response_format"]["json_schema"]["name"] == "actor_decision"]
    assert len(actor_bodies) == 4                                # a0, the refused a1, then a1 and a2: a0 not again

    # a second interruption, discarded
    app.state.engine.store.save_checkpoint(wid, {"format": "worldsim-checkpoint/1", "from_step": 1, "status": "running",
                                                 "world_hash": "x", "answered": {}, "results": None, "steps": [],
                                                 "calls": []})
    assert client.get(f"/api/worlds/{wid}").json()["interrupted"]["resumable"] is False
    assert client.post(f"/api/worlds/{wid}/resume", json={}).status_code == 409
    assert client.post(f"/api/worlds/{wid}/interrupted/discard", json={}).status_code == 200
    assert client.get(f"/api/worlds/{wid}").json()["interrupted"] is None
