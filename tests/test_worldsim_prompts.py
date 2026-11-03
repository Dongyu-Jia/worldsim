"""WorldSim's prompts: what an actor is shown (the world description, its decision questions with their guidance, its
autonomy and the guidance for it, the state's values, statuses and narrative) and what each synthesizer step is shown
(every actor's answers with rationales and its autonomous actions, the digest, what was written before it, its own
items only). No model is called."""
import copy

from worldsim_fakes import small_world

from worldsim import model as M
from worldsim import prompts as P


def world():
    w, problems = M.normalize_world(small_world())
    assert problems == []
    return w


def decision(i=0, unanswered=False):
    return {"actor_id": f"a{i}", "name": f"Actor {i}", "status": "ok", "output": {
        "answers": [{"id": "move", "question": f"How far does Actor {i} push?", "required": True,
                     "value": "" if unanswered else "push by two notches", "rationale": "PRIVATE-WHY",
                     "status": "unanswered" if unanswered else "ok"},
                    {"id": "note", "question": "Anything else to say?", "required": False, "value": "", "rationale": "",
                     "status": "skipped"}],
        "actions": [{"title": "Send an envoy", "detail": "a quiet visit", "target": "Actor 2",
                     "intended_effect": "open a back channel"}],
        "public_statement": "We stand firm.", "rationale": "OVERALL-WHY", "expected_consequences": ["talks resume"]}}


def test_the_actor_prompt_holds_the_description_questions_guidance_autonomy_and_the_state():
    w = world()
    a = w["actors"][0]
    msgs = P.actor_messages(w, a, w["initial_state"], [], "2027Q1")
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert '"answers": an object with one key per decision question id' in system
    assert "up to 2 autonomous actions" in system and '"actions": a list of 0-2 autonomous actions' in system
    assert "## World description (fixed: the setting of the simulation)\nA small test world." in user
    assert "- [move] (required) How far does Actor 0 push?\n  Guidance: hold or push; say how far" in user
    assert "- [note] (optional) Anything else to say?" in user
    assert "You may also take 0 to 2 actions of your own beyond the questions." in user
    assert "What fits this actor: GUIDE-0" in user
    assert "## Narrative (a short summary of the whole state)\nCalm, stalled talks, uneasy public." in user
    assert "- Where do the talks stand? [talks]: Talks are stalled." in user and "- Rate [rate]: 3.88 %" in user
    assert "SECRET-0" in user
    schema = P.actor_schema(a)
    assert schema["required"] == ["answers", "actions", "public_statement", "rationale", "expected_consequences"]
    assert schema["properties"]["answers"]["required"] == ["move", "note"]


def test_an_actor_without_autonomy_is_told_so_and_its_schema_has_no_actions():
    w = world()
    a = copy.deepcopy(w["actors"][0])
    a["autonomy"]["enabled"] = False
    msgs = P.actor_messages(w, a, w["initial_state"], [], "2027Q1")
    assert "Autonomous actions are switched off for this actor" in msgs[0]["content"]
    assert '"actions"' not in msgs[0]["content"] and "Switched off for this actor" in msgs[1]["content"]
    assert "actions" not in P.actor_schema(a)["properties"]


def test_every_synthesizer_step_sees_every_actors_answers_and_actions():
    w = world()
    plan = M.synth_plan(w)
    results = [decision(0), decision(1, unanswered=True),
               {"actor_id": "a2", "name": "Actor 2", "status": "no_response", "output": None, "error": "timeout"}]
    for step in plan:
        digest = None if step["kind"] == "digest" else {"summary": "s", "digest": "DIGEST-TEXT", "threads": ["a → b"]}
        user = P.synth_step_messages(w, step, plan, w["initial_state"], results, [], "2027Q1", digest, {})[1]["content"]
        assert "How far does Actor 0 push? → push by two notches (rationale: PRIVATE-WHY)" in user
        assert "1. Send an envoy — a quiet visit (target: Actor 2; intended effect: open a back channel)" in user
        assert "Rationale: OVERALL-WHY" in user and "Public statement: We stand firm." in user
        assert "How far does Actor 1 push? → UNANSWERED" in user
        assert "Actor 2 [a2]: NO RESPONSE (timeout)" in user
        assert "A small test world." in user and "Be consistent." in user          # description and instructions
        assert "- What is the public mood? [mood]: Uneasy." in user                # the previous state's statuses
        assert ("DIGEST-TEXT" in user) == (step["kind"] != "digest")


def test_a_step_asks_for_its_own_items_only():
    w = world()
    plan = M.synth_plan(w)
    by = {s["key"]: s for s in plan}
    sys_v, user_v = (m["content"] for m in P.synth_step_messages(w, by["values:2"], plan, w["initial_state"], [], [],
                                                                    "2027Q1", {"digest": "d"}, {}))
    assert "This is step 3 of 5: Indicators 2/2." in sys_v and "ONLY these 2 indicators: tension, phase" in sys_v
    assert "exactly the keys asked for: no other keys" in sys_v
    yours = user_v.split("# Your indicators (definitions)")[1]
    assert "- tension — Tension" in yours and "- phase — Phase (type enum; options: calm | crisis)" in yours
    assert "- rate — Rate" not in yours
    sys_s = P.synth_step_messages(w, by["statuses:1"], plan, w["initial_state"], [], [], "2027Q1", {"digest": "d"}, {})[0]["content"]
    assert "ONLY these 2 state questions: talks, mood" in sys_s and "restate it in full when it did not change" in sys_s
    user_s = P.synth_step_messages(w, by["statuses:1"], plan, w["initial_state"], [], [], "2027Q1", {"digest": "d"}, {})[1]["content"]
    assert "what the status should cover: Who offered what." in user_s
    zh = copy.deepcopy(w)
    zh["language"] = "中文"
    sys_n = P.synth_step_messages(zh, by["narrative"], plan, zh["initial_state"], [], [], "2027Q1", {"digest": "d"}, {})[0]["content"]
    assert "about 100 Chinese characters (100 字) (at most 120 characters)" in sys_n
    sys_d = P.synth_step_messages(zh, by["digest"], plan, zh["initial_state"], [], [], "2027Q1")[0]["content"]
    assert "本季要事, about 300 Chinese characters" in sys_d and "Write no values or statuses yet" in sys_d


def test_history_shows_public_decisions_without_rationales():
    hist = [{"step": 1, "period": "2027Q1", "summary": "one line", "digest": "DIGEST", "narrative": "N1",
             "decisions": [decision(0)]}, {"step": 2, "period": "2027Q2", "summary": "two", "narrative": "N2", "decisions": []}]
    lines = "\n".join(P._history_lines(hist, me="a0"))
    assert "Step 1 (2027Q1): one line" in lines and "What happened: DIGEST" in lines
    assert "- Actor 0 (you) — said: \"We stand firm.\"" in lines and "How far does Actor 0 push? → push by two notches" in lines
    assert "PRIVATE-WHY" not in lines and "OVERALL-WHY" not in lines and "Narrative then: N1" in lines
    assert "N2" not in lines                                                       # the current state is shown elsewhere
    compact = "\n".join(P._history_lines(hist, compact=True))
    assert "Decisions that led to it" not in compact and "What happened: DIGEST" in compact


def test_an_old_actor_output_is_still_shown_to_the_synthesizer():
    legacy = {"action_id": "hold", "action_label": "Hold", "action_detail": "wait", "rationale": "r",
              "public_statement": "", "expected_consequences": []}
    lines = "\n".join(P.decision_lines(legacy))
    assert "action: Hold [hold]" in lines and "detail: wait" in lines
    assert P.output_brief(legacy) == "Hold" and P.output_brief(decision(0)["output"]) == "push by two notches"
