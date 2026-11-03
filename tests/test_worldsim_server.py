"""The WorldSim server: world CRUD, schema (indicators and state questions) / initial state / actors / synthesizer
editing, runs with the mock backend, the migration of first-format worlds on load, rewind and fork, export / import,
settings and the key (stored 0600, masked, never returned or saved elsewhere), the origin guard, and the static UI.
TestClient only; the OpenRouter transport is a fake."""
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient
from worldsim_fakes import TEST_KEY, FakeTransport, legacy_world, small_world

from worldsim import config
from worldsim import model as M
from worldsim.server import create_app

BASE = "http://127.0.0.1:8940"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "ws", transport=FakeTransport())


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


def mock_backend(client):
    assert client.put("/api/settings", json={"backend": "mock"}).status_code == 200


def run(client, app, wid, mode="run"):
    r = client.post(f"/api/worlds/{wid}/{mode}", json={})
    assert r.status_code == 200, r.text
    app.state.runs.wait(wid, 60)
    return client.get(f"/api/worlds/{wid}/run").json()["run"]


def new_world(client, **over):
    w = small_world()
    w.update(over)
    r = client.post("/api/worlds/import", json=w)
    assert r.status_code == 200, r.text
    return r.json()["world"]


# ------------------------------------------------------------------------------------------------ worlds

def test_the_default_world_is_preloaded_on_first_start(client):
    worlds = client.get("/api/worlds").json()
    assert [w["id"] for w in worlds["worlds"]] == ["us_2026q4"] and worlds["template"]
    w = worlds["worlds"][0]
    assert w["name"] == "美国与世界 · 2026Q4" and w["period"] == "2026Q4" and w["actors"] == 14 and w["fields"] >= 45
    full = client.get("/api/worlds/us_2026q4").json()
    assert full["steps"] == [0] and full["pending"] is None and full["busy"] is False
    c = full["calls_per_iteration"]
    assert c["actors"] == 14 and c["synth_digest"] == c["synth_narrative"] == 1
    assert c["total"] == 14 + c["synthesizer"] and c["synth_values"] >= 62 // 6 and c["synth_statuses"] >= 22 // 6


def test_world_crud(client):
    r = client.post("/api/worlds", json={"mode": "blank", "name": "Blank one"})
    wid = r.json()["world"]["id"]
    assert r.status_code == 200 and r.json()["world"]["name"] == "Blank one"
    r = client.post("/api/worlds", json={"mode": "template", "name": "Copy of the template"})
    assert r.status_code == 200 and len(r.json()["world"]["actors"]) == 14
    r = client.put(f"/api/worlds/{wid}", json={"name": "Renamed", "language": "中文",
                                                "period_step": {"unit": "month", "count": 2}, "history_window": 5})
    assert r.status_code == 200 and r.json()["world"]["period_step"] == {"unit": "month", "count": 2}
    bad = client.put(f"/api/worlds/{wid}", json={"period_step": {"unit": "fortnight"}})
    assert bad.status_code == 400 and bad.json()["detail"]["problems"]
    dup = client.post(f"/api/worlds/{wid}/duplicate", json={"name": "Dup"}).json()["world"]
    assert dup["name"] == "Dup" and dup["id"] != wid
    assert client.request("DELETE", f"/api/worlds/{wid}", json={}).status_code == 400          # no confirmation
    assert client.request("DELETE", f"/api/worlds/{wid}", json={"confirm": True}).status_code == 200
    assert client.get(f"/api/worlds/{wid}").status_code == 404
    assert client.get("/api/worlds/..%2F..%2Fetc").status_code == 404
    names = {w["name"] for w in client.get("/api/worlds").json()["worlds"]}
    assert names == {"美国与世界 · 2026Q4", "Copy of the template", "Dup"}


def test_schema_initial_state_actors_and_synthesizer_are_edited_and_validated(client):
    w = new_world(client)
    wid = w["id"]
    fields = w["schema"]["fields"] + [{"id": "gdp", "label": "GDP", "group": "Growth", "type": "number", "min": -10, "max": 10}]
    r = client.put(f"/api/worlds/{wid}/schema", json={"fields": fields, "initial_values": {"gdp": 2.5, "rate": 4}})
    assert r.status_code == 200 and r.json()["world"]["initial_state"]["values"]["gdp"] == 2.5
    bad = client.put(f"/api/worlds/{wid}/schema", json={"fields": [{"id": "Bad!", "type": "number"}]})
    assert bad.status_code == 400 and "id must be" in " ".join(bad.json()["detail"]["problems"])
    r = client.put(f"/api/worlds/{wid}/initial", json={"period": "2027Q1", "values": {"rate": "5%"}, "narrative": "New start"})
    assert r.status_code == 200
    init = r.json()["world"]["initial_state"]
    assert init["period"] == "2027Q1" and init["values"]["rate"] == 5.0 and init["narrative"] == "New start"
    assert init["statuses"] == {"talks": "Talks are stalled.", "mood": "Uneasy."}
    assert client.get(f"/api/worlds/{wid}/steps/0").json()["state"]["period"] == "2027Q1"     # step 0 follows
    bad = client.put(f"/api/worlds/{wid}/initial", json={"values": {"phase": "nonsense"}})
    assert bad.status_code == 400
    assert r.json()["world"]["schema"]["questions"] == w["schema"]["questions"]           # kept when not sent
    actors = w["actors"] + [{"id": "a0", "name": "dup id", "decision_questions": [{"id": "x", "text": "X?"}]}]
    bad = client.put(f"/api/worlds/{wid}/actors", json={"actors": actors})
    assert bad.status_code == 400 and "used twice" in " ".join(bad.json()["detail"]["problems"])
    actors[-1]["id"] = "a9"
    actors[-1]["autonomy"] = {"enabled": True, "max_actions": 11}
    bad = client.put(f"/api/worlds/{wid}/actors", json={"actors": actors})
    assert bad.status_code == 400 and "max_actions" in " ".join(bad.json()["detail"]["problems"])
    actors[-1]["autonomy"] = {"enabled": False}
    r = client.put(f"/api/worlds/{wid}/actors", json={"actors": actors})
    assert r.status_code == 200 and len(r.json()["world"]["actors"]) == 4
    assert r.json()["world"]["actors"][-1]["autonomy"] == {"enabled": False, "max_actions": 3, "guidance": ""}
    old_style = [{"id": "z", "name": "Z", "action_space": [{"id": "x", "label": "X"}]}]          # an old client
    r = client.put(f"/api/worlds/{wid}/actors", json={"actors": old_style})
    assert r.status_code == 200 and r.json()["world"]["actors"][0]["decision_questions"][0]["id"] == "q_action"
    r = client.put(f"/api/worlds/{wid}/synthesizer", json={"instructions": "Be bold.", "model": "acme/x"})
    assert r.json()["world"]["synthesizer"] == {"instructions": "Be bold.", "model": "acme/x", "max_items_per_call": 6}
    r = client.put(f"/api/worlds/{wid}/synthesizer", json={"instructions": "x", "max_items_per_call": 3})
    assert r.json()["world"]["synthesizer"]["max_items_per_call"] == 3
    assert client.get(f"/api/worlds/{wid}").json()["calls_per_iteration"]["synth_values"] == 2
    for bad_x in (0, 31, "x", 2.5):
        bad = client.put(f"/api/worlds/{wid}/synthesizer", json={"instructions": "x", "max_items_per_call": bad_x})
        assert bad.status_code == 400 and "max_items_per_call" in bad.json()["detail"]["problems"][0]
    assert client.get(f"/api/worlds/{wid}").json()["world"]["synthesizer"]["max_items_per_call"] == 3


def test_state_questions_and_the_world_description_are_edited_and_validated(client, app):
    w = new_world(client)
    wid = w["id"]
    qs = w["schema"]["questions"] + [{"id": "oil", "text": "What about oil?", "group": "Energy", "guidance": "supply"}]
    r = client.put(f"/api/worlds/{wid}/schema", json={"fields": w["schema"]["fields"], "questions": qs,
                                                        "initial_statuses": {"oil": "Tight supply."}})
    assert r.status_code == 200
    world = r.json()["world"]
    assert [q["id"] for q in world["schema"]["questions"]] == ["talks", "mood", "oil"]
    assert world["initial_state"]["statuses"]["oil"] == "Tight supply."
    bad = client.put(f"/api/worlds/{wid}/schema", json={"fields": w["schema"]["fields"],
                                                          "questions": qs + [{"id": "oil", "text": "Again?"}]})
    assert bad.status_code == 400 and "used twice" in " ".join(bad.json()["detail"]["problems"])
    r = client.put(f"/api/worlds/{wid}/initial", json={"statuses": {"talks": "Talks resumed."}, "narrative": "Short."})
    assert r.json()["world"]["initial_state"]["statuses"]["talks"] == "Talks resumed."
    assert client.get(f"/api/worlds/{wid}/steps/0").json()["state"]["narrative"] == "Short."
    r = client.put(f"/api/worlds/{wid}", json={"description": "A new description."})
    assert r.json()["world"]["description"] == "A new description."
    mock_backend(client)
    run(client, app, wid)
    bad = client.put(f"/api/worlds/{wid}/schema", json={"fields": w["schema"]["fields"], "questions": qs,
                                                          "initial_statuses": {"oil": "Changed."}})
    assert bad.status_code == 409 and "statuses" in bad.json()["detail"]


# ------------------------------------------------------------------------------------------------ running

def test_a_run_through_the_api_saves_a_step_viewable_with_its_iteration_and_calls(client, app):
    mock_backend(client)
    wid = new_world(client)["id"]
    est = client.get(f"/api/worlds/{wid}/estimate").json()
    assert est["backend"] == "mock" and est["calls"] == 8 and est["counts"]["synthesizer"] == 5
    final = run(client, app, wid)
    assert final["status"] == "done" and final["step"] == 1 and final["period"] == "2027Q1"
    steps = client.get(f"/api/worlds/{wid}/steps").json()["steps"]
    assert [s["step"] for s in steps] == [0, 1] and steps[1]["partial"] is False and "status_changes" in steps[1]
    assert [s["status"] for s in final["synthesizer"]["steps"]] == ["done"] * 5
    view = client.get(f"/api/worlds/{wid}/steps/1").json()
    assert view["previous"]["step"] == 0 and view["latest"] == 1
    assert set(view["state"]["statuses"]) == {"talks", "mood"} and view["state"]["narrative"]
    it = view["iteration"]
    assert len(it["actors"]) == 3 and all("messages" not in c and "response_text" not in c for c in it["calls"])
    assert [s["label"] for s in it["synthesizer"]["steps"]][0] == "Digest" and it["synthesizer"]["digest"]["digest"]
    calls = client.get(f"/api/worlds/{wid}/iterations/1/calls").json()["calls"]
    assert len(calls) == 8 and all(c["messages"] and c["response_text"] for c in calls)
    assert [c["who"] for c in calls][3:] == ["synthesizer:digest", "synthesizer:values:1", "synthesizer:values:2",
                                             "synthesizer:statuses:1", "synthesizer:narrative"]


def test_without_a_key_the_openrouter_backend_refuses_to_run(client):
    wid = new_world(client)["id"]
    r = client.post(f"/api/worlds/{wid}/run", json={})
    assert r.status_code == 400 and "No OpenRouter key" in r.json()["detail"]


def test_a_world_without_enabled_actors_cannot_run(client):
    mock_backend(client)
    w = small_world()
    for a in w["actors"]:
        a["enabled"] = False
    wid = new_world(client, actors=w["actors"])["id"]
    assert client.post(f"/api/worlds/{wid}/run", json={}).status_code == 400


def test_a_synthesizer_failure_is_shown_and_retried_through_the_api(tmp_path):
    from worldsim.llm import MockBackend
    backend = MockBackend(delay_s=0, script={"synth": ["error"] * 10})
    app = create_app(tmp_path / "ws", transport=FakeTransport(), backend_factory=lambda s: backend)
    client = TestClient(app, base_url=BASE)
    mock_backend(client)
    wid = new_world(client)["id"]
    final = run(client, app, wid)
    assert final["status"] == "failed" and "injected failure" in final["error"]
    info = client.get(f"/api/worlds/{wid}").json()
    assert info["steps"] == [0] and "injected failure" in info["pending"]["error"] and "calls" not in info["pending"]
    assert len(client.get(f"/api/worlds/{wid}/pending/calls").json()["calls"]) == 13
    final = run(client, app, wid, mode="retry")
    assert final["status"] == "done" and client.get(f"/api/worlds/{wid}").json()["steps"] == [0, 1]
    assert client.post(f"/api/worlds/{wid}/retry", json={}).status_code == 404               # nothing left to retry


def test_the_mock_script_env_drives_the_failure_path_in_the_real_app(tmp_path, monkeypatch):
    from worldsim import llm
    monkeypatch.setattr(llm, "_SCRIPTED", {})
    monkeypatch.setenv("WORLDSIM_LLM", "mock")
    monkeypatch.setenv("WORLDSIM_MOCK_SCRIPT", json.dumps({"synth": ["error"] * 10}))
    app = create_app(tmp_path / "ws", transport=FakeTransport())
    client = TestClient(app, base_url=BASE)
    assert client.get("/api/settings").json()["effective_backend"] == "mock"
    wid = new_world(client)["id"]
    assert run(client, app, wid)["status"] == "failed"
    assert run(client, app, wid, mode="retry")["status"] == "done"


def test_after_a_step_the_initial_state_is_read_only_but_the_schema_can_grow(client, app):
    mock_backend(client)
    w = new_world(client)
    wid = w["id"]
    run(client, app, wid)
    r = client.put(f"/api/worlds/{wid}/initial", json={"period": "2030Q1"})
    assert r.status_code == 409 and "rewind" in r.json()["detail"]
    fields = w["schema"]["fields"] + [{"id": "extra", "label": "Extra", "type": "number"}]
    r = client.put(f"/api/worlds/{wid}/schema", json={"fields": fields, "initial_values": {"rate": 99}})
    assert r.status_code == 409                                                               # would change step 0
    r = client.put(f"/api/worlds/{wid}/schema", json={"fields": fields})
    assert r.status_code == 200
    qs = w["schema"]["questions"] + [{"id": "later", "text": "A later question?"}]
    r = client.put(f"/api/worlds/{wid}/schema", json={"fields": fields, "questions": qs})
    assert r.status_code == 200
    run(client, app, wid)
    st = client.get(f"/api/worlds/{wid}/steps/2").json()["state"]
    assert st["values"]["extra"] == 0.0 and "extra" in st["reasons"]                         # the mock filled it
    assert st["statuses"]["later"] and "later" in st["status_reasons"]


def test_a_first_format_world_on_disk_is_migrated_when_loaded_with_a_backup(tmp_path):
    root = tmp_path / "ws"
    app = create_app(root, transport=FakeTransport())
    client = TestClient(app, base_url=BASE)
    d = root / "worlds" / "old"
    (d / "steps").mkdir(parents=True)
    (d / "iterations").mkdir()
    old = legacy_world()
    (d / "world.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    step1 = {"step": 1, "period": "2027Q1", "values": {"rate": 4.1}, "summary": "加息",
             "text": {"narrative": "第二步。", "key_questions": [{"question": "会加息吗？", "status": "answered",
                                                               "answer": "加息 25bp"}], "attributes": []}}
    (d / "steps" / "000000.json").write_text(json.dumps(old["initial_state"], ensure_ascii=False), encoding="utf-8")
    (d / "steps" / "000001.json").write_text(json.dumps(step1, ensure_ascii=False), encoding="utf-8")
    old_it = {"format": "worldsim-iteration/1", "step": 1, "period_from": "2026Q4", "period": "2027Q1",
              "actors": [{"actor_id": "a0", "name": "Actor 0", "status": "ok", "output": {
                  "action_id": "hold", "action_label": "维持", "action_detail": "按兵不动", "rationale": "r",
                  "public_statement": "", "expected_consequences": []}}],
              "synthesizer": {"status": "ok", "output": {}}, "changes": [], "flags": [], "calls": []}
    (d / "iterations" / "000001.json").write_text(json.dumps(old_it, ensure_ascii=False), encoding="utf-8")
    world = client.get("/api/worlds/old").json()["world"]
    assert world["format"] == M.FORMAT and [q["id"] for q in world["schema"]["questions"]] == ["kq_1", "attr_1"]
    assert world["actors"][0]["decision_questions"][0]["text"] == "本季你会怎么做？"
    assert (d / "pre_v2" / "world.json").exists() and (d / "pre_v2" / "steps" / "000001.json").exists()
    assert json.loads((d / "pre_v2" / "world.json").read_text(encoding="utf-8"))["format"] == "worldsim-world/1"
    st1 = client.get("/api/worlds/old/steps/1").json()
    assert st1["state"]["statuses"] == {"kq_1": "已有答案：加息 25bp", "attr_1": "（这一步没有记录）"}
    assert st1["state"]["narrative"] == "第二步。" and "text" not in st1["state"]
    assert st1["iteration"]["actors"][0]["output"]["action_label"] == "维持"                  # old records stay readable
    mock_backend(client)
    final = run(client, app, "old")
    assert final["status"] == "done" and final["step"] == 2
    prompt = client.get("/api/worlds/old/iterations/2/calls").json()["calls"][0]["messages"][1]["content"]
    assert "action: 维持 [hold]" in prompt                                                    # the old decision in history


def test_rewind_and_fork_through_the_api(client, app):
    mock_backend(client)
    wid = new_world(client)["id"]
    for _ in range(3):
        run(client, app, wid)
    r = client.post(f"/api/worlds/{wid}/fork", json={"step": 2, "name": "Branch"})
    fork = r.json()["world"]
    assert r.status_code == 200 and fork["name"] == "Branch"
    assert [s["step"] for s in client.get(f"/api/worlds/{fork['id']}/steps").json()["steps"]] == [0, 1, 2]
    assert client.post(f"/api/worlds/{wid}/rewind", json={"step": 1}).status_code == 400      # needs confirm
    r = client.post(f"/api/worlds/{wid}/rewind", json={"step": 1, "confirm": True})
    assert r.json() == {"ok": True, "dropped": 2, "step": 1}
    assert client.post(f"/api/worlds/{wid}/rewind", json={"step": 7, "confirm": True}).status_code == 404
    assert client.post(f"/api/worlds/{wid}/fork", json={"step": "x"}).status_code == 400


def test_export_and_import_round_trip_with_history(client, app):
    mock_backend(client)
    wid = new_world(client)["id"]
    run(client, app, wid)
    r = client.get(f"/api/worlds/{wid}/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert len(data["history"]["steps"]) == 2 and len(data["history"]["iterations"]) == 1
    imported = client.post("/api/worlds/import", json=data).json()["world"]
    assert imported["id"] != wid
    assert client.get(f"/api/worlds/{imported['id']}/steps/1").json()["state"]["period"] == "2027Q1"
    bad = client.post("/api/worlds/import", json={"name": "x", "schema": {"fields": [{"id": "1bad"}]}})
    assert bad.status_code == 400


# ------------------------------------------------------------------------------------------------ settings and the key

def test_the_key_is_stored_0600_masked_and_never_returned(client, tmp_path):
    r = client.put("/api/settings", json={"api_key": TEST_KEY, "temperature": 0.3})
    assert r.status_code == 200 and TEST_KEY not in r.text
    pub = client.get("/api/settings").json()
    assert pub["has_key"] and pub["key_masked"] == "sk-or-…cdef" and pub["temperature"] == 0.3
    path = tmp_path / "ws" / "settings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and json.loads(path.read_text())["api_key"] == TEST_KEY
    assert client.put("/api/settings", json={"api_key": ""}).json()["has_key"]                  # a blank box keeps it
    bad = client.put("/api/settings", json={"temperature": 9})
    assert bad.status_code == 400 and client.get("/api/settings").json()["temperature"] == 0.3
    assert client.put("/api/settings", json={"clear_key": True}).json()["has_key"] is False
    assert client.post("/api/settings/test_key", json={}).json() == {"ok": False, "message": "No OpenRouter key is set."}


def test_the_test_key_button_calls_get_key(tmp_path):
    fake = FakeTransport()
    client = TestClient(create_app(tmp_path / "ws", transport=fake), base_url=BASE)
    client.put("/api/settings", json={"api_key": TEST_KEY})
    r = client.post("/api/settings/test_key", json={})
    assert r.json()["ok"] and r.json()["info"]["limit_remaining"] == 7.5 and TEST_KEY not in r.text
    assert fake.requests[-1]["url"] == "https://openrouter.ai/api/v1/key"


def test_an_openrouter_run_leaves_the_key_only_in_settings(tmp_path, caplog):
    caplog.set_level("DEBUG")
    fake = FakeTransport()
    app = create_app(tmp_path / "ws", transport=fake)
    client = TestClient(app, base_url=BASE)
    client.put("/api/settings", json={"api_key": TEST_KEY, "backend": "openrouter"})
    wid = new_world(client)["id"]
    final = run(client, app, wid)
    assert final["status"] == "done" and len(fake.chats) == 8 and final["cost"] == pytest.approx(0.0123 * 8)
    assert fake.chats[0]["body"]["model"] == "google/gemini-4-argon"
    for path in (tmp_path / "ws").rglob("*"):
        if path.is_file():
            assert (TEST_KEY in path.read_text(encoding="utf-8", errors="replace")) == (path.name == "settings.json"), path
    for url in ("/api/settings", "/api/worlds", f"/api/worlds/{wid}", f"/api/worlds/{wid}/steps/1",
                f"/api/worlds/{wid}/iterations/1/calls", f"/api/worlds/{wid}/export", f"/api/worlds/{wid}/run", "/api/health"):
        assert TEST_KEY not in client.get(url).text, url
    assert TEST_KEY not in caplog.text


# ------------------------------------------------------------------------------------------------ the guard and the UI

@pytest.mark.parametrize("headers, ok", [
    ({}, True),
    ({"Origin": BASE}, True),
    ({"Origin": "http://localhost:8940", "Host": "localhost:8940"}, True),
    ({"Origin": "https://evil.example"}, False),
    ({"Origin": "null"}, False),
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "same-site"}, False),
    ({"Sec-Fetch-Site": "same-origin", "Origin": BASE}, True),
    ({"Host": "evil.example"}, False),
])
def test_the_origin_guard(client, headers, ok):
    r = client.put("/api/settings", json={"temperature": 0.5}, headers=headers)
    assert (r.status_code == 200) == ok, (r.status_code, r.text)
    if not ok:
        assert r.status_code == 403


def test_changes_need_a_json_body(client):
    for kw in ({"content": "temperature=1", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
               {"content": '{"temperature": 1}', "headers": {"Content-Type": "text/plain"}}):
        r = client.put("/api/settings", **kw)
        assert r.status_code == 403 and "JSON body" in r.json()["detail"]
    r = client.request("DELETE", "/api/worlds/us_2026q4")
    assert r.status_code == 403 and client.get("/api/worlds/us_2026q4").status_code == 200
    r = client.put("/api/settings", content="[1, 2]", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_reads_from_another_host_name_are_refused(client):
    assert client.get("/api/worlds", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/api/nope").status_code == 404


def test_the_ui_is_served_with_a_strict_content_security_policy(client):
    r = client.get("/")
    assert r.status_code == 200 and '<script type="module" src="js/app.js">' in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    for path in ("/js/app.js", "/js/api.js", "/js/ui.js", "/css/app.css") + tuple(
            f"/js/pages/{p}.js" for p in ("sim", "actors", "schema", "synth", "settings", "worlds")):
        assert client.get(path).status_code == 200, path


def test_the_ui_has_the_decision_question_state_question_and_synthesizer_step_views():
    web = config.WEB_DIR / "js"
    src = {p.stem: p.read_text(encoding="utf-8") for p in (web / "pages").glob("*.js")}
    for needle in ("Decision questions", "+ Add question", "Required", "Allow autonomous actions", "per iteration",
                   "guidance (optional)", "Move up", "Remove question"):
        assert needle in src["actors"], needle
    for needle in ("World description", "Predefined state questions", "Delete this state question?", "initial status",
                   "characters", "{ fields, questions }", "initial_statuses"):
        assert needle in src["schema"], needle
    for needle in ("World description", "calls per iteration", "unanswered", "Autonomous actions", "Show input & raw output",
                   "State questions", "Previous status", "Status changes", "Synthesizer · ", "partial", "Narrative"):
        assert needle in src["sim"], needle
    assert "Items per call" in src["synth"] and "max_items_per_call" in src["synth"]
    assert "Schema page" in src["worlds"]
    for name, text in src.items():
        assert "action_space" not in text and "allow_free_action" not in text and "key_questions:" not in text, name
    css = (config.WEB_DIR / "css" / "app.css").read_text(encoding="utf-8")
    assert ".synth-step" in css and ".sq-item" in css and ".dq-row" in css and ".answer.bad" in css


def test_the_ui_never_renders_server_text_as_html():
    web = config.WEB_DIR / "js"
    for path in list(web.glob("*.js")) + list((web / "pages").glob("*.js")):
        src = path.read_text(encoding="utf-8")
        assert "innerHTML" not in src.replace("'innerHTML'", "") and "insertAdjacentHTML" not in src, path.name
        assert "outerHTML" not in src and "document.write" not in src, path.name


def test_the_models_endpoint_works_offline(client):
    data = client.get("/api/models").json()
    assert data["source"] == "builtin" and data["models"][0]["id"] == "google/gemini-4-argon"
    assert data["default_model"] == "google/gemini-4-argon"
