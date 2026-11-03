"""WorldSim's data model: schema (indicators and predefined state questions) and world validation, actors' decision
questions and autonomy, value coercion, periods, the synthesizer's chunking, the next state checked against the schema,
the migration of first-format worlds, and the shipped default world (which must load and validate without data/)."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from worldsim_fakes import legacy_world

from worldsim import config
from worldsim import model as M

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = {"fields": [
    {"id": "rate", "label": "Rate", "group": "Policy", "type": "percent", "unit": "%", "min": 0, "max": 20},
    {"id": "jobs", "label": "Jobs", "group": "Labour", "type": "integer", "unit": "k"},
    {"id": "mood", "label": "Mood", "group": "Politics", "type": "enum", "options": ["calm", "tense", "crisis"]},
    {"id": "open", "label": "Open", "group": "Politics", "type": "boolean"},
    {"id": "note", "label": "Note", "group": "Politics", "type": "text"}],
    "questions": [{"id": "fed", "text": "Where is the Fed?", "group": "Policy", "guidance": "stance and path"},
                  {"id": "talks", "text": "Where are the talks?", "group": "Politics"}]}
CURRENT = {"step": 0, "period": "2026Q4", "values": {"rate": 3.88, "jobs": 50, "mood": "tense", "open": True, "note": "x"},
           "statuses": {"fed": "Hawkish hold.", "talks": "Stalled."}, "narrative": "Start."}
ACTOR = {"id": "a", "name": "A", "decision_questions": [{"id": "move", "text": "How far?", "guidance": "in bp"}]}


def world(**kw):
    w = {"id": "w1", "name": "Test", "language": "English", "schema": copy.deepcopy(SCHEMA),
         "actors": [copy.deepcopy(ACTOR)], "initial_state": copy.deepcopy(CURRENT)}
    w.update(kw)
    return w


# ------------------------------------------------------------------------------------------------ schema and world

def test_a_valid_world_normalizes_without_problems():
    w, problems = M.normalize_world(world())
    assert problems == []
    assert w["format"] == M.FORMAT
    assert [f["id"] for f in w["schema"]["fields"]] == ["rate", "jobs", "mood", "open", "note"]
    assert [q["id"] for q in w["schema"]["questions"]] == ["fed", "talks"] and w["schema"]["questions"][1]["guidance"] == ""
    a = w["actors"][0]
    assert a["decision_questions"] == [{"id": "move", "text": "How far?", "guidance": "in bp", "required": True}]
    assert a["autonomy"] == {"enabled": True, "max_actions": 3, "guidance": ""} and a["visible_fields"] is None
    assert w["synthesizer"]["max_items_per_call"] == 6
    assert w["period_step"] == {"unit": "quarter", "count": 1} and w["initial_state"]["values"]["rate"] == 3.88
    assert w["initial_state"]["statuses"] == {"fed": "Hawkish hold.", "talks": "Stalled."}
    assert w["initial_state"]["narrative"] == "Start."


@pytest.mark.parametrize("bad, needle", [
    ({"id": "Bad Id", "label": "x", "type": "number"}, "id must be"),
    ({"id": "x", "type": "color"}, "type must be one of"),
    ({"id": "x", "type": "enum", "options": []}, "at least one option"),
    ({"id": "x", "type": "number", "min": 5, "max": 1}, "min is greater than max"),
    ({"id": "x", "type": "number", "min": "abc"}, "min must be a number"),
])
def test_bad_fields_are_reported(bad, needle):
    _, problems = M.normalize_schema({"fields": [bad]})
    assert any(needle in p for p in problems), problems


def test_duplicate_ids_and_bad_actors_are_reported():
    _, problems = M.normalize_schema({"fields": [{"id": "a", "type": "number"}, {"id": "a", "type": "text"}]})
    assert any("used twice" in p for p in problems)
    actors = [{"id": "x", "name": "X", "decision_questions": [{"id": "q", "text": "A?"}, {"id": "q", "text": "B?"}]},
              {"id": "x", "name": "Y", "decision_questions": [{"id": "Bad Id", "text": "C?"}, {"id": "e", "text": ""}]},
              {"id": "z", "name": "Z", "decision_questions": [], "autonomy": {"enabled": False}},
              {"id": "w", "name": "W", "decision_questions": ["D?"], "autonomy": {"max_actions": 0}},
              {"id": "v", "name": "V", "decision_questions": [{"text": f"Q{i}?"} for i in range(9)],
               "autonomy": {"max_actions": "2.5"}}]
    _, problems = M.normalize_actors(actors)
    text = " ".join(problems)
    assert "decision question id q is used twice" in text and "actor id x is used twice" in text
    assert "decision question id 'Bad Id'" in text and "decision question e has no text" in text
    assert "needs at least one decision question, or autonomous actions switched on" in text
    assert text.count("autonomy.max_actions must be a whole number from 1 to 10") == 2
    assert "at most 8 decision questions" in text


def test_decision_questions_get_ids_and_defaults():
    a, problems = M.normalize_actor({"id": "a", "name": "A", "decision_questions": [
        "What now?", {"text": "And then?", "required": False, "guidance": "  hint  "}],
        "autonomy": {"enabled": False, "max_actions": "4", "guidance": "speeches"}})
    assert problems == []
    assert a["decision_questions"] == [{"id": "q1", "text": "What now?", "guidance": "", "required": True},
                                       {"id": "q2", "text": "And then?", "guidance": "hint", "required": False}]
    assert a["autonomy"] == {"enabled": False, "max_actions": 4, "guidance": "speeches"}


def test_state_questions_are_validated_and_get_ids():
    schema, problems = M.normalize_schema({"fields": [{"id": "rate", "type": "number"}],
                                           "questions": [{"text": "A?"}, "B?", {"id": "rate", "text": "C?"},
                                                         {"id": "x", "text": ""}, {"id": "y", "text": "D?"},
                                                         {"id": "y", "text": "E?"}]})
    text = " ".join(problems)
    assert [q["id"] for q in schema["questions"]][:2] == ["sq1", "sq2"] and schema["questions"][0]["group"] == "General"
    assert "state question id rate is also a field id" in text and "state question x: the question text is empty" in text
    assert "state question id y is used twice" in text
    assert M.normalize_schema({"fields": [], "questions": "nope"})[1] == ["schema.questions must be a list"]


@pytest.mark.parametrize("x, ok", [(1, True), (6, True), (30, True), ("12", True), (None, True), (0, False), (31, False),
                                   (2.5, False), ("many", False), (True, False)])
def test_the_items_per_call_setting_is_validated(x, ok):
    synth, problems = M.normalize_synthesizer({"instructions": "i", "max_items_per_call": x})
    assert (problems == []) == ok, problems
    assert 1 <= synth["max_items_per_call"] <= 30
    if ok:
        assert synth["max_items_per_call"] == (6 if x is None else int(x))


def test_initial_values_are_typed_and_unreadable_ones_reported():
    state, problems = M.normalize_state(SCHEMA, {"values": {"rate": "3.5%", "jobs": 12.0, "mood": "Tense",
                                                            "open": "yes", "note": 7, "ghost": 1}})
    assert problems == []
    assert state["values"] == {"rate": 3.5, "jobs": 12, "mood": "tense", "open": True, "note": "7"}
    _, problems = M.normalize_state(SCHEMA, {"values": {"rate": "high", "mood": "chaos"}})
    assert len(problems) == 2


def test_visible_fields_drop_unknown_ids():
    w, problems = M.normalize_world(world(actors=[{**ACTOR, "visible_fields": ["rate", "nope"]}]))
    assert problems == [] and w["actors"][0]["visible_fields"] == ["rate"]


@pytest.mark.parametrize("period, unit, count, nxt", [
    ("2026Q4", "quarter", 1, "2027Q1"), ("2026Q2", "quarter", 3, "2027Q1"), ("2026Q4", "year", 1, "2027Q4"),
    ("2026-11", "month", 1, "2026-12"), ("2026-12", "month", 2, "2027-02"), ("2026", "year", 1, "2027"),
    ("Turn 3", "quarter", 1, "Turn 4"), ("start", "quarter", 1, "start +1")])
def test_periods_advance(period, unit, count, nxt):
    assert M.advance_period(period, unit, count) == nxt


def test_blank_world_is_valid():
    w = M.blank_world("abc", "Blank")
    assert M.normalize_world(w)[1] == [] and w["name"] == "Blank"
    assert w["schema"]["questions"] and w["actors"][0]["decision_questions"] and w["initial_state"]["narrative"]


# ------------------------------------------------------------------------------------------------ chunking

def items(*groups):
    out = []
    for g, n in groups:
        out += [{"id": f"{g}{i}", "group": g} for i in range(n)]
    return out


@pytest.mark.parametrize("groups, x, sizes", [
    ((("a", 3), ("b", 2), ("c", 4)), 6, [5, 4]),          # small neighbours merge; a group is never torn when it fits
    ((("a", 9),), 6, [5, 4]),                             # a group larger than x is split into balanced pieces
    ((("a", 13), ("b", 1)), 6, [5, 4, 4, 1]),
    ((("a", 2), ("b", 2), ("c", 2)), 2, [2, 2, 2]),
    ((("a", 1),) * 1 + (("b", 1),), 1, [1, 1]),
    ((), 6, []),
])
def test_chunks_keep_groups_together_split_large_ones_and_merge_small_ones(groups, x, sizes):
    its = items(*groups)
    chunks = M.chunk_by_group(its, x)
    assert [len(c) for c in chunks] == sizes
    flat = [it["id"] for c in chunks for it in c]
    assert flat == [it["id"] for it in its]                 # every item exactly once, in schema (group) order
    assert all(len(c) <= x for c in chunks)
    for c in chunks:                                        # a chunk never holds part of a group that fits elsewhere whole
        for g in {it["group"] for it in c}:
            size = sum(1 for it in its if it["group"] == g)
            if size <= x:
                assert sum(1 for it in c if it["group"] == g) == size


def test_the_synthesizer_plan_covers_every_field_and_question_once():
    w, _ = M.normalize_world(world(synthesizer={"max_items_per_call": 2}))
    plan = M.synth_plan(w)
    assert [s["key"] for s in plan] == ["digest", "values:1", "values:2", "values:3", "statuses:1", "narrative"]
    assert [s["items"] for s in plan if s["kind"] == "values"] == [["rate", "jobs"], ["mood", "open"], ["note"]]
    assert [s["groups"] for s in plan if s["kind"] == "values"] == [["Policy", "Labour"], ["Politics"], ["Politics"]]
    vals = [i for s in plan if s["kind"] == "values" for i in s["items"]]
    qs = [i for s in plan if s["kind"] == "statuses" for i in s["items"]]
    assert sorted(vals) == sorted(f["id"] for f in w["schema"]["fields"]) and len(vals) == len(set(vals))
    assert qs == ["fed", "talks"] and all(len(s["items"]) <= 2 for s in plan)
    assert plan[1]["label"] == "Indicators 1/3" and plan[4]["label"] == "Questions 1/1"


# ------------------------------------------------------------------------------------------------ the next state

def answer(**values):
    vals = {"rate": {"value": 4.13, "reason": "the Fed hiked 25bp"}, "jobs": {"value": 50, "reason": ""},
            "mood": {"value": "tense", "reason": ""}, "open": {"value": True, "reason": ""},
            "note": {"value": "x", "reason": ""}}
    vals.update(values)
    return {"summary": "A hike.", "values": vals, "narrative": "The Fed hiked; talks stalled.",
            "statuses": {"fed": {"status": "Hiked 25bp, hawkish.", "changed": True, "reason": "the hike"},
                         "talks": {"status": "Stalled.", "changed": False, "reason": ""}}}


def test_a_clean_answer_becomes_the_next_state_with_changes_and_reasons():
    state, flags, changes = M.validate_next_state(SCHEMA, CURRENT, answer(), period="2027Q1", step=1, narrative_max=120)
    assert flags == [] and state["step"] == 1 and state["period"] == "2027Q1"
    assert state["values"]["rate"] == 4.13 and state["reasons"]["rate"] == "the Fed hiked 25bp"
    assert changes == [{"field": "rate", "label": "Rate", "from": 3.88, "to": 4.13, "reason": "the Fed hiked 25bp"}]
    assert state["statuses"] == {"fed": "Hiked 25bp, hawkish.", "talks": "Stalled."}
    assert state["status_changes"] == [{"question": "fed", "text": "Where is the Fed?", "from": "Hawkish hold.",
                                        "to": "Hiked 25bp, hawkish.", "reason": "the hike"}]
    assert state["status_reasons"] == {"fed": "the hike"} and state["narrative"] == "The Fed hiked; talks stalled."


def test_missing_null_and_mistyped_values_are_carried_forward_and_flagged():
    out = answer(jobs={"value": None, "reason": ""}, mood={"value": "panic", "reason": "?"},
                 open={"value": "maybe", "reason": ""})
    del out["values"]["note"]
    out["values"]["invented"] = {"value": 1, "reason": "made up"}
    state, flags, _ = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1)
    by = {(f["field"], f["code"]) for f in flags}
    assert {("jobs", "null"), ("mood", "type"), ("open", "type"), ("note", "missing"), ("invented", "unknown_field")} <= by
    assert state["values"]["jobs"] == 50 and state["values"]["mood"] == "tense" and state["values"]["note"] == "x"
    assert "invented" not in state["values"]


def test_out_of_range_values_are_kept_but_flagged_and_a_change_needs_a_reason():
    out = answer(rate={"value": 25.0, "reason": ""}, jobs={"value": "61", "reason": ""})
    state, flags, changes = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1)
    codes = {(f["field"], f["code"]) for f in flags}
    assert ("rate", "range") in codes and ("rate", "no_reason") in codes and ("jobs", "coerced") in codes
    assert state["values"]["rate"] == 25.0 and state["values"]["jobs"] == 61
    assert {c["field"] for c in changes} == {"rate", "jobs"}


def test_every_question_needs_a_non_empty_status_or_it_is_carried_forward_and_flagged():
    out = answer()
    out["statuses"] = {"fed": {"status": "  ", "changed": True, "reason": "?"}, "ghost": "boo"}
    state, flags, _ = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1)
    by = {(f["question"], f["code"]) for f in flags}
    assert {("fed", "empty"), ("talks", "missing"), ("ghost", "unknown_question")} <= by
    assert state["statuses"] == CURRENT["statuses"] and state["status_changes"] == []


def test_a_status_change_needs_a_reason_and_changed_defaults_to_whether_the_text_differs():
    out = answer()
    out["statuses"] = {"fed": "A new stance.", "talks": {"status": "Stalled.", "changed": True, "reason": ""}}
    state, flags, _ = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1)
    assert [c["question"] for c in state["status_changes"]] == ["fed"]           # "talks" restated: not a change
    assert ("fed", "no_reason") in {(f["question"], f["code"]) for f in flags}


def test_the_narrative_is_kept_within_its_limit():
    out = answer()
    out["narrative"] = "长" * 150
    state, flags, _ = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1, narrative_max=120)
    assert len(state["narrative"]) == 120 and state["narrative"].endswith("…")
    assert [f["code"] for f in flags] == ["narrative_length"]
    assert M.narrative_limits("中文") == ("about 100 Chinese characters (100 字)", 120)
    assert M.narrative_limits("English")[1] == 600


def test_an_answer_without_values_statuses_or_narrative_keeps_the_current_state():
    state, flags, changes = M.validate_next_state(SCHEMA, CURRENT, {"summary": ""}, period="2027Q1", step=1)
    assert state["values"] == CURRENT["values"] and changes == []
    assert state["statuses"] == CURRENT["statuses"] and state["narrative"] == "Start."
    codes = {f["code"] for f in flags}
    assert {"values", "statuses", "narrative", "summary"} <= codes


def test_plain_values_without_reason_objects_are_accepted():
    out = answer()
    out["values"] = {k: v["value"] for k, v in out["values"].items()}
    state, flags, _ = M.validate_next_state(SCHEMA, CURRENT, out, period="2027Q1", step=1)
    assert state["values"]["rate"] == 4.13 and [f["code"] for f in flags] == ["no_reason"]


# ------------------------------------------------------------------------------------------------ migration

def test_a_first_format_world_migrates_with_its_states():
    old = legacy_world()
    later = {"step": 1, "period": "2027Q1", "values": {"rate": 4.1},
             "text": {"narrative": "第二步。", "key_questions": [
                 {"question": "会加息吗？", "status": "answered", "answer": "加息 25bp"},
                 {"question": "会停摆吗？", "status": "open", "answer": ""}],
                 "attributes": {"货币政策": "更鹰"}}}
    assert M.needs_migration(old)
    w, states = M.migrate_v1(old, [copy.deepcopy(old["initial_state"]), later])
    qs = w["schema"]["questions"]
    assert [(q["id"], q["text"], q["group"]) for q in qs] == [("kq_1", "会加息吗？", "关键问题"), ("attr_1", "货币政策", "属性"),
                                                              ("kq_2", "会停摆吗？", "关键问题")]
    assert w["initial_state"]["statuses"] == {"kq_1": "未定。当前判断：未定", "attr_1": "偏鹰", "kq_2": "（这一步没有记录）"}
    assert w["initial_state"]["narrative"] == "很长的旧叙事。" and "text" not in w["initial_state"]
    assert states[1]["statuses"] == {"kq_1": "已有答案：加息 25bp", "attr_1": "更鹰", "kq_2": "未定。"}
    assert states[1]["narrative"] == "第二步。" and w["format"] == M.FORMAT
    norm, problems = M.normalize_world(w)
    assert problems == [] and not M.needs_migration(norm)
    again, _ = M.migrate_v1(norm, [])                               # idempotent on the current format
    assert again["schema"]["questions"] == norm["schema"]["questions"]


def test_an_old_action_space_becomes_one_required_question_with_the_actions_as_guidance():
    norm, problems = M.normalize_world(legacy_world())
    assert problems == []
    a = norm["actors"][0]
    (q,) = a["decision_questions"]
    assert q["id"] == "q_action" and q["text"] == "本季你会怎么做？" and q["required"] is True
    assert "- 维持：维持现状（现状）。" in q["guidance"] and "- 加码：加大力度。" in q["guidance"]
    assert "也可以提出清单之外的做法" in q["guidance"]
    assert a["autonomy"] == {"enabled": True, "max_actions": 3, "guidance": ""}
    assert "action_space" not in a and "allow_free_action" not in a
    en, _ = M.normalize_actor({"id": "b", "name": "B", "action_space": [{"id": "x", "label": "X"}], "allow_free_action": False},
                              language="English", period_unit="month")
    assert en["decision_questions"][0]["text"] == "What will you do this month?"
    assert en["decision_questions"][0]["guidance"].endswith("Choose among these options.")


# ------------------------------------------------------------------------------------------------ the default world

def default_world():
    return json.loads(config.DEFAULT_WORLD_FILE.read_text(encoding="utf-8"))


def test_the_default_world_loads_and_validates_without_data():
    w = default_world()
    norm, problems = M.normalize_world(w)
    assert problems == []
    assert norm["name"] == "美国与世界 · 2026Q4" and norm["language"] == "中文"
    assert norm["initial_state"]["period"] == "2026Q4"
    fields = norm["schema"]["fields"]
    assert len(fields) >= 45
    vals = norm["initial_state"]["values"]
    assert all(vals[f["id"]] is not None for f in fields), "every field has a value today"
    quoted = {"tips10": 2.93, "debt_gdp": 97.37, "consumer_sentiment": 48.1, "initial_claims": 200.5,
              "labor_share": 93.4, "profit_share": 13.14, "dividend_yield": 1.03, "it_invest_gdp": 5.05}
    for k, v in quoted.items():
        assert vals[k] == pytest.approx(v, abs=0.06), k
    for f in fields:
        if f["type"] in M.NUMERIC_TYPES:
            assert f["min"] is not None and f["max"] is not None and f["min"] <= vals[f["id"]] <= f["max"], f["id"]
            assert f["hist"], f["id"]
    qualitative = {f["id"]: f for f in fields if f["type"] in ("enum", "text")}
    for fid in ("fed_stance", "ieepa_status", "oil_disruption", "house_control", "china_truce", "opec_policy"):
        assert fid in qualitative
    assert qualitative["ieepa_status"]["tag"] == "未确认"
    assert w["format"] == M.FORMAT and not M.needs_migration(w)


def test_the_default_world_has_a_description_state_questions_and_a_short_narrative():
    w = default_world()
    desc = w["description"]
    assert len(desc) > 300 and "每一步推进一个季度" in desc and "14 个" in desc and "没有定价模型" in desc
    qs = w["schema"]["questions"]
    assert 15 <= len(qs) <= 25 and len({q["id"] for q in qs}) == len(qs)
    sts = w["initial_state"]["statuses"]
    for q in qs:
        assert q["text"].endswith("？") and q["guidance"] and q["group"], q["id"]
        assert 40 <= len(sts[q["id"]]) <= 400, q["id"]                       # 2-4 sentences, in Chinese
    covered = " ".join(q["text"] for q in qs)
    for topic in ("美联储", "财政", "债务上限", "关税", "中美", "石油", "欧佩克", "通胀", "劳动力", "消费者", "AI",
                  "盈利", "金融条件", "银行", "房地产", "中期选举", "全球增长", "美元", "资产市场"):
        assert topic in covered, topic
    assert sum("未确认" in s for s in sts.values()) >= 6
    narrative = w["initial_state"]["narrative"]
    assert 80 <= len(narrative) <= 120 and "text" not in w["initial_state"]


def test_the_default_world_has_asset_prices_from_local_data():
    w = default_world()
    assets = {f["id"]: f for f in w["schema"]["fields"] if f["group"] == "资产价格"}
    assert set(assets) == {"spx", "ndx", "qqq", "gold", "tlt", "vix", "case_shiller", "fhfa_hpi"}
    vals = w["initial_state"]["values"]
    assert vals["spx"] == pytest.approx(7669.4, abs=0.1) and vals["ndx"] == pytest.approx(30408.5, abs=0.1)
    assert vals["qqq"] == pytest.approx(739.61, abs=0.01) and vals["gold"] == 4319.0
    assert assets["qqq"]["tag"] == assets["tlt"]["tag"] == "估算"               # Causeway's estimates, not trades
    for f in assets.values():
        assert "（读数：" in f["description"] and f["unit"], f["id"]
    synth = w["synthesizer"]["instructions"]
    assert "资产价格" in synth and "不做资产定价" not in synth and "不要写股票指数点位" not in synth
    assert w["synthesizer"]["max_items_per_call"] == 6


def test_the_default_world_has_all_fourteen_actors_with_sound_decision_questions():
    w = default_world()
    names = [a["name"] for a in w["actors"]]
    assert names == ["美联储", "总统与白宫", "国会", "财政部", "最高法院与法院", "选民", "中国", "欧佩克+", "冲突方",
                     "超大型科技公司 CEO", "消费者/居民", "华尔街投资者", "银行/放贷机构", "非科技企业"]
    for a in w["actors"]:
        assert a["enabled"] and a["role"] and a["private_info"], a["id"]
        assert 3 <= len(a["cares_about"]) <= 6, a["id"]
        qs = a["decision_questions"]
        assert 1 <= len(qs) <= 4 and any(q["required"] for q in qs), a["id"]
        for q in qs:
            assert q["text"].endswith("？") and len(q["guidance"]) >= 15, (a["id"], q["id"])
        auto = a["autonomy"]
        assert auto["enabled"] and 1 <= auto["max_actions"] <= 3 and auto["guidance"].startswith("例如"), a["id"]
        assert "action_space" not in a
    fed = {q["id"]: q for q in w["actors"][0]["decision_questions"]}
    assert "25bp" in fed["rate"]["guidance"] and set(fed) == {"rate", "balance_sheet", "guidance"}
    opec = w["actors"][7]["decision_questions"][0]
    assert "万桶/日" in opec["text"]
    synth = w["synthesizer"]["instructions"]
    assert "联合" in synth and "未回答" in synth and "100 字" in synth


def test_worldsim_stays_light():
    """Importing the server must not pull in heavy numeric libraries: WorldSim needs fastapi, uvicorn and requests."""
    code = ("import sys, worldsim.server, worldsim.engine; "
            "bad = [m for m in ('pandas', 'numpy') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
