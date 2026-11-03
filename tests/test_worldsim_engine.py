"""One WorldSim iteration end to end on the mock backend: actors in parallel (decision questions answered in text,
autonomous actions), the synthesizer's steps (digest, indicator chunks, question chunks, narrative), validation and
persistence; the failure paths (an actor that fails, bad JSON and its repair, an unanswered required question, a
synthesizer step that fails and is carried forward, a synthesizer that fails outright and its retry); rewind, fork and
the cost estimate."""
import json
import threading
import time

import pytest
from worldsim_fakes import small_world

from worldsim import model as M
from worldsim import settings as S
from worldsim.engine import Engine, RunManager
from worldsim.llm import CallRequest, Completion, LLMError, MockBackend, extract_json
from worldsim.store import Conflict, NotFound, Store

SYNTH_KEYS = ["synthesizer:digest", "synthesizer:values:1", "synthesizer:values:2", "synthesizer:statuses:1",
              "synthesizer:narrative"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ws")


def engine_for(store, backend):
    return Engine(store, lambda: S.load(store.root), backend_factory=lambda s: backend)


def synth_calls(backend, kind=None):
    return [c for c in backend.calls if c.purpose == "synth" and (kind is None or c.context["step"]["kind"] == kind)]


def test_an_iteration_runs_actors_then_every_synthesizer_step_and_saves_the_next_step(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0)
    result = engine_for(store, backend).run(w["id"])
    assert result["status"] == "done" and result["step"] == 1 and result["period"] == "2027Q1" and not result["partial"]
    assert store.step_numbers(w["id"]) == [0, 1]
    state = store.load_step(w["id"], 1)
    assert state["period"] == "2027Q1" and set(state["values"]) == {"rate", "tension", "phase"}
    assert set(state["statuses"]) == {"talks", "mood"} and all(state["statuses"].values())
    assert state["narrative"].startswith("[mock] 2027Q1") and len(state["narrative"]) <= 600
    assert state["summary"] == "(mock) 3 actors decided" and state["partial"] is False
    it = store.load_iteration(w["id"], 1)
    assert [r["status"] for r in it["actors"]] == ["ok", "ok", "ok"]
    syn = it["synthesizer"]
    assert syn["status"] == "ok" and syn["max_items_per_call"] == 2 and syn["digest"]["digest"].startswith("[mock]")
    assert [s["key"] for s in syn["steps"]] == [k.split(":", 1)[1] for k in SYNTH_KEYS]
    assert [s["items"] for s in syn["steps"]] == [[], ["rate"], ["tension", "phase"], ["talks", "mood"], []]
    assert it["usage"]["calls"] == 8 and it["cost"] == 0.0
    # every call is saved with its prompt and raw response, in order: the actors, then the synthesizer's steps
    assert len(it["calls"]) == 8 and all(c["messages"] and c["response_text"] for c in it["calls"])
    assert [c["who"] for c in it["calls"]][3:] == SYNTH_KEYS
    assert [c.purpose for c in backend.calls].count("actor") == 3 and len(synth_calls(backend)) == 5
    # each actor sees only its own private information; every synthesizer step sees every actor's decisions
    for c in backend.calls:
        if c.purpose == "actor":
            me = c.context["actor"]["id"]
            text = json.dumps(c.messages, ensure_ascii=False)
            assert f"SECRET-{me[1:]}" in text and sum(f"SECRET-{i}" in text for i in range(3)) == 1
    for c in synth_calls(backend):
        prompt = c.messages[1]["content"]
        assert all(f"## Actor {i} [a{i}]" in prompt for i in range(3)) and "SECRET" not in prompt
        assert "A small test world." in prompt                                  # the world description
    # the actors' answers and actions are saved
    out = it["actors"][0]["output"]
    assert [a["id"] for a in out["answers"]] == ["move", "note"] and all(a["value"] for a in out["answers"])
    assert len(out["actions"]) <= 2 and set(out) >= {"public_statement", "rationale", "expected_consequences"}


def test_three_iterations_build_a_history_that_later_prompts_see(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0)
    eng = engine_for(store, backend)
    for _ in range(3):
        assert eng.run(w["id"])["status"] == "done"
    assert [store.load_step(w["id"], n)["period"] for n in range(4)] == ["2026Q4", "2027Q1", "2027Q2", "2027Q3"]
    last_actor_prompt = [c for c in backend.calls if c.purpose == "actor"][-1].messages[1]["content"]
    assert "Step 2 (2027Q2)" in last_actor_prompt and "Decisions that led to it" in last_actor_prompt
    assert "What happened: [mock] 2027Q2" in last_actor_prompt                 # the earlier digests
    assert "(rationale:" not in last_actor_prompt.split("# Recent history")[1]  # history is public: no rationales


def test_actors_are_called_in_parallel_up_to_max_parallel(store):
    w = store.create_world(small_world(n_actors=6))
    active, peak, lock = [0], [0], threading.Lock()

    class Slow(MockBackend):
        def complete(self, req):
            if req.purpose == "actor":
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.15)
                with lock:
                    active[0] -= 1
            return super().complete(req)

    S.save({**S.DEFAULTS, "max_parallel": 3}, store.root)
    t0 = time.monotonic()
    assert engine_for(store, Slow(delay_s=0)).run(w["id"])["status"] == "done"
    assert peak[0] == 3 and time.monotonic() - t0 < 6 * 0.15


def test_a_failing_actor_is_retried_once_then_marked_no_response_and_the_synthesizer_is_told(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0, script={"actor:a0": ["error", "ok"], "actor:a1": ["error", "timeout"]})
    assert engine_for(store, backend).run(w["id"])["status"] == "done"
    it = store.load_iteration(w["id"], 1)
    by = {r["actor_id"]: r for r in it["actors"]}
    assert by["a0"]["status"] == "ok" and by["a0"]["attempts"] == 2
    assert by["a1"]["status"] == "no_response" and by["a1"]["attempts"] == 2 and "timeout" in by["a1"]["error"]
    assert by["a2"]["status"] == "ok" and by["a2"]["attempts"] == 1
    for c in synth_calls(backend):
        assert "Actor 1 [a1]: NO RESPONSE" in c.messages[1]["content"]
    assert "No response: Actor 1" in it["synthesizer"]["digest"]["digest"]
    assert "no response: Actor 1" in store.load_step(w["id"], 1)["narrative"]


def test_bad_json_gets_one_repair_call(store):
    w = store.create_world(small_world(n_actors=1))
    backend = MockBackend(delay_s=0, script={"actor:a0": ["bad_json"], "synth:digest": ["bad_json"]})
    assert engine_for(store, backend).run(w["id"])["status"] == "done"
    it = store.load_iteration(w["id"], 1)
    assert [(c["who"], c["repair"]) for c in it["calls"]] == [
        ("a0", False), ("a0", True), ("synthesizer:digest", False), ("synthesizer:digest", True)] + [
        (k, False) for k in SYNTH_KEYS[1:]]
    assert it["calls"][0]["error"].startswith("unusable answer") and it["calls"][1]["error"] is None
    repair = [c for c in backend.calls if c.repair][0]
    assert repair.messages[-2]["role"] == "assistant" and "could not be used" in repair.messages[-1]["content"]


def test_an_actor_with_bad_json_twice_falls_back_to_a_retry(store):
    w = store.create_world(small_world(n_actors=1))
    backend = MockBackend(delay_s=0, script={"actor:a0": ["bad_json", "ok"], "actor:a0:repair": ["bad_json"]})
    assert engine_for(store, backend).run(w["id"])["status"] == "done"
    r = store.load_iteration(w["id"], 1)["actors"][0]
    assert r["status"] == "ok" and r["attempts"] == 2


def test_an_unanswered_required_question_gets_one_repair_then_is_marked_and_the_synthesizer_is_told(store):
    w = store.create_world(small_world(n_actors=2))
    backend = MockBackend(delay_s=0, script={"actor:a0": ["invalid"], "actor:a1": ["invalid"],
                                             "actor:a1:repair": ["invalid"]})
    assert engine_for(store, backend).run(w["id"])["status"] == "done"
    it = store.load_iteration(w["id"], 1)
    by = {r["actor_id"]: r for r in it["actors"]}
    assert [a["status"] for a in by["a0"]["output"]["answers"]] == ["ok", "ok"]            # repaired
    assert [a["status"] for a in by["a1"]["output"]["answers"]] == ["unanswered", "skipped"]
    assert by["a1"]["status"] == "ok" and "marked unanswered: move" in by["a1"]["notes"][0]
    first = [c for c in it["calls"] if c["who"] == "a1"][0]
    assert "required decision questions have no answer: move" in first["error"]
    prompt = synth_calls(backend, "digest")[0].messages[1]["content"]
    assert "How far does Actor 1 push? → UNANSWERED" in prompt and "do not invent an answer" in prompt
    assert "Anything else to say? → (no answer: optional question)" in prompt


def test_autonomous_actions_are_capped_and_ignored_when_switched_off(store):
    wd = small_world(n_actors=2)
    wd["actors"][0]["autonomy"] = {"enabled": True, "max_actions": 1, "guidance": ""}
    wd["actors"][1]["autonomy"] = {"enabled": False, "max_actions": 3, "guidance": ""}
    w = store.create_world(wd)

    class Busy(MockBackend):
        def complete(self, req):
            if req.purpose != "actor":
                return super().complete(req)
            self.calls.append(req)
            obj = {"answers": {"move": {"value": "push 2", "rationale": "r"}}, "public_statement": "", "rationale": "",
                   "expected_consequences": [],
                   "actions": [{"title": f"act {i}", "detail": "d", "target": "t", "intended_effect": "e"} for i in range(3)]}
            return Completion(json.dumps(obj), {}, 0.0, "m", "mock")
    backend = Busy(delay_s=0)
    assert engine_for(store, backend).run(w["id"])["status"] == "done"
    by = {r["actor_id"]: r for r in store.load_iteration(w["id"], 1)["actors"]}
    assert [a["title"] for a in by["a0"]["output"]["actions"]] == ["act 0"]
    assert "3 autonomous actions given, at most 1" in by["a0"]["notes"][0]
    assert by["a1"]["output"]["actions"] == [] and "autonomous actions are off" in by["a1"]["notes"][0]
    schemas = {c.context["actor"]["id"]: c.json_schema for c in backend.calls if c.purpose == "actor"}
    assert "actions" in schemas["a0"]["properties"] and "actions" not in schemas["a1"]["properties"]
    assert schemas["a0"]["properties"]["answers"]["required"] == ["move", "note"]


def test_each_synthesizer_step_has_a_schema_with_only_its_own_keys(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0)
    engine_for(store, backend).run(w["id"])
    by = {c.context["step"]["key"]: c for c in synth_calls(backend)}
    assert set(by["digest"].json_schema["properties"]) == {"summary", "digest", "threads"}
    v2 = by["values:2"].json_schema
    assert v2["required"] == ["values"] and v2["properties"]["values"]["required"] == ["tension", "phase"]
    assert set(v2["properties"]["values"]["properties"]) == {"tension", "phase"} and v2["additionalProperties"] is False
    assert v2["properties"]["values"]["properties"]["phase"]["properties"]["value"]["enum"] == ["calm", "crisis"]
    st = by["statuses:1"].json_schema["properties"]["statuses"]
    assert st["required"] == ["talks", "mood"] and set(st["properties"]["talks"]["properties"]) == {"status", "changed", "reason"}
    assert set(by["narrative"].json_schema["properties"]) == {"narrative"}
    assert {c.schema_name for c in by.values()} == {"digest", "indicator_values", "question_statuses", "narrative"}


def test_later_steps_see_the_digest_and_everything_written_before_them(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0, script={"synth:values:1": ["ok"]})
    engine_for(store, backend).run(w["id"])
    by = {c.context["step"]["key"]: c.messages[1]["content"] for c in synth_calls(backend)}
    assert "This period's digest" not in by["digest"] and "Already written" not in by["digest"]
    assert "[mock] 2027Q1: Actor 0" in by["values:1"] and "Already written" not in by["values:1"]
    written_v2 = by["values:2"].split("# Already written in this iteration")[1]
    assert "Rate [rate]" in written_v2 and "Tension [tension]" not in written_v2
    written_q = by["statuses:1"].split("# Already written in this iteration")[1]
    assert all(f"{n} [{i}]" in written_q for n, i in (("Rate", "rate"), ("Tension", "tension"), ("Phase", "phase")))
    written_n = by["narrative"].split("# Already written in this iteration")[1]
    assert "Where do the talks stand? [talks]" in written_n and "What is the public mood? [mood]" in written_n
    assert "Your indicators" in by["values:2"] and "- tension — Tension" in by["values:2"] and "- rate — Rate" not in by["values:2"]
    assert by["narrative"].count("at most 600 characters") >= 1


def test_a_synthesizer_step_that_fails_twice_is_carried_forward_and_the_iteration_is_partial(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0, script={"synth:values:2": ["error", "timeout"]})
    result = engine_for(store, backend).run(w["id"])
    assert result["status"] == "done" and result["partial"] is True
    state = store.load_step(w["id"], 1)
    assert state["partial"] is True and state["values"]["tension"] == 5 and state["values"]["phase"] == "calm"
    assert state["reasons"]["tension"] == M.synth_failed_reason("English")
    failed = [f for f in state["flags"] if f["code"] == "synth_failed"]
    assert [f["field"] for f in failed] == ["tension", "phase"] and all(f["level"] == "error" for f in failed)
    it = store.load_iteration(w["id"], 1)
    step = {s["key"]: s for s in it["synthesizer"]["steps"]}["values:2"]
    assert step["status"] == "failed" and step["attempts"] == 2 and "timeout" in step["error"]
    assert it["partial"] is True and it["synthesizer"]["status"] == "partial"
    later = synth_calls(backend, "statuses")[0].messages[1]["content"].split("# Already written")[1]
    assert "Tension [tension]: 5 (unchanged) — synthesis failed" in later
    zh = small_world()
    zh.update(id="zh", language="中文")
    wz = store.create_world(zh)
    engine_for(store, MockBackend(delay_s=0, script={"synth:statuses": ["error", "error"]})).run(wz["id"])
    sz = store.load_step(wz["id"], 1)
    assert sz["statuses"] == {"talks": "Talks are stalled.", "mood": "Uneasy."}
    assert sz["status_reasons"] == {"talks": "合成失败，沿用上期", "mood": "合成失败，沿用上期"}
    assert {f["question"] for f in sz["flags"] if f["code"] == "synth_failed"} == {"talks", "mood"}


def test_keys_outside_a_step_are_dropped_and_flagged(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0, script={"synth:values:1": ["extra"]})
    engine_for(store, backend).run(w["id"])
    it = store.load_iteration(w["id"], 1)
    step = {s["key"]: s for s in it["synthesizer"]["steps"]}["values:1"]
    assert step["dropped"] == ["not_in_this_chunk"] and list(step["output"]["values"]) == ["rate"]
    assert "not_in_this_chunk" not in store.load_step(w["id"], 1)["values"]
    assert any(f["code"] == "dropped_keys" for f in it["flags"])


def test_a_narrative_over_the_limit_gets_a_repair_then_is_cut(store):
    wd = small_world()
    wd["language"] = "中文"
    w = store.create_world(wd)
    backend = MockBackend(delay_s=0, script={"synth:narrative": ["long"]})
    engine_for(store, backend).run(w["id"])
    it = store.load_iteration(w["id"], 1)
    calls = [c for c in it["calls"] if c["who"] == "synthesizer:narrative"]
    assert [c["repair"] for c in calls] == [False, True] and "it must be about 100 Chinese characters" in calls[0]["error"]
    assert len(store.load_step(w["id"], 1)["narrative"]) <= 120
    backend = MockBackend(delay_s=0, script={"synth:narrative": ["long"], "synth:narrative:repair": ["long"]})
    engine_for(store, backend).run(w["id"])
    s2 = store.load_step(w["id"], 2)
    assert len(s2["narrative"]) == 120 and "narrative_length" in {f["code"] for f in s2["flags"]}


def test_a_synthesizer_that_fails_outright_leaves_the_state_unchanged_and_a_retry_completes_it(store):
    w = store.create_world(small_world())
    backend = MockBackend(delay_s=0, script={"synth": ["error"] * 10})
    eng = engine_for(store, backend)
    result = eng.run(w["id"])
    assert result["status"] == "failed" and "injected failure" in result["error"]
    assert store.step_numbers(w["id"]) == [0]
    pending = store.load_pending(w["id"])
    assert pending["from_step"] == 0 and len(pending["actors"]) == 3 and len(pending["calls"]) == 13
    assert [s["status"] for s in pending["steps"]] == ["failed"] * 5
    n_actor_calls = sum(c.purpose == "actor" for c in backend.calls)
    result = eng.retry(w["id"])
    assert result["status"] == "done" and store.step_numbers(w["id"]) == [0, 1] and not result["partial"]
    assert sum(c.purpose == "actor" for c in backend.calls) == n_actor_calls      # the actors were not called again
    assert store.load_pending(w["id"]) is None
    it = store.load_iteration(w["id"], 1)
    assert len(it["calls"]) == 18 and it["usage"]["calls"] == 18                  # the failed attempt's calls are kept


def test_a_synthesizer_error_is_shown_and_a_retry_without_pending_is_refused(store):
    w = store.create_world(small_world())
    eng = engine_for(store, MockBackend(delay_s=0, script={"synth": ["error"] * 10}))
    result = eng.run(w["id"])
    assert result["status"] == "failed" and "injected failure" in result["error"]
    store.clear_pending(w["id"])
    with pytest.raises(NotFound):
        eng.retry(w["id"])


def test_a_stale_pending_attempt_is_refused(store):
    w = store.create_world(small_world())
    eng = engine_for(store, MockBackend(delay_s=0, script={"synth": ["error"] * 10}))
    eng.run(w["id"])
    pending = store.load_pending(w["id"])
    store.save_pending(w["id"], {**pending, "from_step": 7})
    with pytest.raises(Conflict):
        eng.retry(w["id"])


def test_rewind_and_fork(store):
    w = store.create_world(small_world())
    eng = engine_for(store, MockBackend(delay_s=0))
    for _ in range(3):
        eng.run(w["id"])
    fork = store.fork(w["id"], 2, "Fork")
    assert fork["name"] == "Fork" and store.step_numbers(fork["id"]) == [0, 1, 2]
    assert store.load_step(fork["id"], 2) == store.load_step(w["id"], 2)
    assert store.rewind(w["id"], 1) == 2 and store.step_numbers(w["id"]) == [0, 1]
    assert eng.run(w["id"])["step"] == 2
    with pytest.raises(NotFound):
        store.rewind(w["id"], 9)


def test_the_run_manager_runs_in_the_background_and_shows_every_synthesizer_step(store):
    w = store.create_world(small_world())
    rm = RunManager(engine_for(store, MockBackend(delay_s=0.1)))
    snap = rm.start(w["id"])
    assert snap["status"] == "running" and [a["status"] for a in snap["actors"]] == ["queued"] * 3
    assert [s["label"] for s in snap["synthesizer"]["steps"]] == ["Digest", "Indicators 1/2", "Indicators 2/2",
                                                                  "Questions 1/1", "Narrative"]
    assert all(s["status"] == "waiting" for s in snap["synthesizer"]["steps"])
    with pytest.raises(Exception, match="already running"):
        rm.start(w["id"])
    final = rm.wait(w["id"], 30)
    assert final["status"] == "done" and final["step"] == 1 and final["partial"] is False
    assert all(a["status"] == "done" and a["summary"] for a in final["actors"])
    assert final["synthesizer"]["status"] == "done" and all(s["status"] == "done" for s in final["synthesizer"]["steps"])


def test_the_estimate_prices_every_call_with_the_model_list(store):
    w = store.create_world(small_world())
    est = engine_for(store, MockBackend(delay_s=0)).estimate(w["id"])
    assert est["calls"] == 8 and est["priced"] and est["cost"] > 0
    assert est["counts"] == {"actors": 3, "synthesizer": 5, "total": 8, "synth_digest": 1, "synth_values": 2,
                             "synth_statuses": 1, "synth_narrative": 1}
    assert {r["model"] for r in est["rows"]} == {"google/gemini-4-argon"}
    assert est["prompt_tokens"] > 0 and est["next_period"] == "2027Q1"
    assert [r["who"] for r in est["rows"]][3:] == ["synthesizer · Digest", "synthesizer · Indicators 1/2",
                                                   "synthesizer · Indicators 2/2", "synthesizer · Questions 1/1",
                                                   "synthesizer · Narrative"]


def test_model_overrides_reach_the_requests(store):
    wd = small_world(n_actors=2)
    wd["actors"][1]["model"] = "openai/gpt-x"
    wd["synthesizer"]["model"] = "anthropic/claude-y"
    wd["model"] = {"model": "google/world-model", "temperature": 0.2, "reasoning_effort": "low"}
    w = store.create_world(wd)
    backend = MockBackend(delay_s=0)
    engine_for(store, backend).run(w["id"])
    actors = {c.context["actor"]["id"]: c for c in backend.calls if c.purpose == "actor"}
    assert actors["a0"].model == "google/world-model" and actors["a1"].model == "openai/gpt-x"
    assert {c.model for c in synth_calls(backend)} == {"anthropic/claude-y"}
    assert actors["a0"].temperature == 0.2 and actors["a0"].reasoning_effort == "low"


def test_visible_fields_limit_what_an_actor_sees(store):
    wd = small_world(n_actors=1)
    wd["actors"][0]["visible_fields"] = ["rate"]
    w = store.create_world(wd)
    backend = MockBackend(delay_s=0)
    engine_for(store, backend).run(w["id"])
    prompt = backend.calls[0].messages[1]["content"]
    assert "Rate [rate]" in prompt and "Tension [tension]" not in prompt
    assert "Where do the talks stand? [talks]: Talks are stalled." in prompt      # every status is visible


def test_an_unexpected_backend_exception_in_one_actor_does_not_stop_the_iteration(store):
    w = store.create_world(small_world())

    class Buggy(MockBackend):
        def complete(self, req):
            if req.purpose == "actor" and req.context["actor"]["id"] == "a2":
                raise KeyError("boom")
            return super().complete(req)
    assert engine_for(store, Buggy(delay_s=0)).run(w["id"])["status"] == "done"
    by = {r["actor_id"]: r for r in store.load_iteration(w["id"], 1)["actors"]}
    assert by["a2"]["status"] == "no_response" and "internal error" in by["a2"]["error"]


def test_a_non_retryable_error_is_not_retried_and_stops_the_synthesizer(store):
    w = store.create_world(small_world(n_actors=1))

    class NoKey(MockBackend):
        def complete(self, req):
            self.calls.append(req)
            raise LLMError("no OpenRouter key", "auth", retryable=False)
    backend = NoKey(delay_s=0)
    result = engine_for(store, backend).run(w["id"])
    assert result["status"] == "failed" and len(backend.calls) == 2      # one actor call, one synthesizer call
    pending = store.load_pending(w["id"])
    assert [s["status"] for s in pending["steps"]] == ["failed"] * 5 and pending["steps"][1]["error"].startswith("not run")


@pytest.mark.parametrize("text, ok", [
    ('{"a": 1}', True), ('```json\n{"a": 1,}\n```', True), ('Here: {"a": True, "b": None} thanks', True),
    ('{“a”: 1}', True), ('{"a": "x，y"}', True), ('{"a": 1', False), ('[1, 2]', False), ('', False)])
def test_extract_json(text, ok):
    obj, err = extract_json(text)
    assert (obj is not None) == ok, err
    if ok:
        assert "a" in obj
    if text == '{"a": "x，y"}':
        assert obj["a"] == "x，y"                     # punctuation inside strings is left alone


def test_completion_and_request_types_are_plain():
    req = CallRequest(purpose="actor", model="m", messages=[], schema_name="s", json_schema={})
    assert req.repair is False and Completion("t", {}, None, "m", "json_object").meta == {}
    assert M.DEFAULT_ITEMS_PER_CALL == 6 and M.DEFAULT_MAX_ACTIONS == 3
