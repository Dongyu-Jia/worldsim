"""Prompts and answer schemas.

Actor call: the world description, the actor's role, what it cares about, its decision questions (with guidance), its
autonomy (how many actions of its own, what kind), its private information, the current state (its visible indicators,
every state question's status, the narrative) and a short recent history -> one JSON object
{answers {question id: {value, rationale}}, actions [{title, detail, target, intended_effect}], public_statement,
 rationale, expected_consequences [str]}.

The synthesizer works in several short calls (model.synth_plan), each of which sees the world description, the full
previous state, every actor's decisions and everything written earlier in the iteration, and answers only its own keys:
  digest     {summary, digest (本季要事), threads [str]}
  values     {values {field id: {value, reason}}}               one chunk of at most x indicators
  statuses   {statuses {question id: {status, changed, reason}}}  one chunk of at most x state questions
  narrative  {narrative}                                         a ~100-字 summary of all new values and statuses

The chrome is English; the world's own text (description, roles, questions, state) is in the world's language, and
every generated text is asked for in that language.
"""
from __future__ import annotations

import json
from typing import Any

from . import model as M

ACTOR_SCHEMA_NAME = "actor_decision"
SYNTH_SCHEMA_NAMES = {"digest": "digest", "values": "indicator_values", "statuses": "question_statuses",
                      "narrative": "narrative"}
NARRATIVE_CHARS = 700
DIGEST_CHARS = 900
HISTORY_ANSWER_CHARS = 120


def fmt_value(field: dict, value: Any) -> str:
    if value is None:
        return "(unknown)"
    if field.get("type") == "boolean":
        return "true" if value else "false"
    if field.get("type") in M.NUMERIC_TYPES and isinstance(value, (int, float)):
        s = M._num_text(value)
        unit = field.get("unit") or ""
        if field.get("type") == "percent" and "%" not in unit:
            unit = ("% " + unit).strip()
        return f"{s} {unit}".strip()
    return str(value)


def _cut(text: Any, n: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _state_lines(schema: dict, state: dict, visible: list[str] | None = None) -> list[str]:
    fields = schema.get("fields", [])
    if visible is not None:
        keep = set(visible)
        fields = [f for f in fields if f["id"] in keep]
    lines: list[str] = []
    group = None
    vals = state.get("values") or {}
    for f in fields:
        if f["group"] != group:
            group = f["group"]
            lines.append(f"[{group}]")
        tag = f" ({f['tag']})" if f.get("tag") else ""
        lines.append(f"- {f['label']} [{f['id']}]{tag}: {fmt_value(f, vals.get(f['id']))}")
    return lines


def _status_lines(schema: dict, statuses: dict | None) -> list[str]:
    lines: list[str] = []
    group = None
    for q in schema.get("questions", []):
        if q["group"] != group:
            group = q["group"]
            lines.append(f"[{group}]")
        lines.append(f"- {q['text']} [{q['id']}]: {(statuses or {}).get(q['id']) or '(no status yet)'}")
    return lines or ["(no state questions)"]


def _state_block(world: dict, state: dict, visible: list[str] | None = None, title: str | None = None) -> list[str]:
    schema = world["schema"]
    lines = [title or f"# Current state: step {state.get('step')}, period {state.get('period')}",
             "## Narrative (a short summary of the whole state)", state.get("narrative") or "(none)", "",
             "## Indicators"]
    lines += _state_lines(schema, state, visible) or ["(no indicators)"]
    lines += ["", "## State questions (the current status of each)"] + _status_lines(schema, state.get("statuses"))
    return lines


def _world_lines(world: dict) -> list[str]:
    return [f"# World: {world['name']}", "## World description (fixed: the setting of the simulation)",
            world.get("description") or "(no description)"]


# ------------------------------------------------------------------------------------------------ decisions

def is_legacy_output(output: dict | None) -> bool:
    return bool(output) and "answers" not in output and "action_id" in output


def output_brief(output: dict | None, limit: int = 80) -> str:
    """A one-line gist of an actor's decision (its first answer, else its first action)."""
    if not output:
        return ""
    if is_legacy_output(output):
        return _cut(output.get("action_label") or output.get("action_id"), limit)
    for a in output.get("answers") or []:
        if a.get("value"):
            return _cut(a["value"], limit)
    for act in output.get("actions") or []:
        if act.get("title"):
            return _cut(act["title"], limit)
    return ""


def decision_lines(output: dict, indent: str = "  ", private: bool = True) -> list[str]:
    """An actor's decision as prompt lines: every question with its answer (and rationale when `private`), every
    autonomous action. A required question left unanswered is marked so that nobody invents its answer."""
    if is_legacy_output(output):
        out = [f"{indent}- action: {output.get('action_label')} [{output.get('action_id')}]"]
        if output.get("action_detail"):
            out.append(f"{indent}- detail: {output['action_detail'] if private else _cut(output['action_detail'], 200)}")
        return out
    lines: list[str] = []
    answers = output.get("answers") or []
    if answers:
        lines.append(f"{indent}Decision questions:")
    for a in answers:
        q = a.get("question") or a.get("id")
        if a.get("status") == "unanswered":
            lines.append(f"{indent}- {q} → UNANSWERED (a required question left without an answer after one repair: "
                         "do not invent an answer; treat this point as no new decision)")
        elif not a.get("value"):
            lines.append(f"{indent}- {q} → (no answer: optional question)")
        else:
            value = a["value"] if private else _cut(a["value"], HISTORY_ANSWER_CHARS)
            why = f" (rationale: {a['rationale']})" if private and a.get("rationale") else ""
            lines.append(f"{indent}- {q} → {value}{why}")
    acts = output.get("actions") or []
    lines.append(f"{indent}Autonomous actions:" + ("" if acts else " none"))
    for i, act in enumerate(acts, 1):
        head = act.get("title") or "(untitled)"
        if act.get("detail") and private:
            head += " — " + act["detail"]
        tail = []
        if act.get("target"):
            tail.append(f"target: {act['target']}")
        if act.get("intended_effect") and private:
            tail.append(f"intended effect: {act['intended_effect']}")
        lines.append(f"{indent}{i}. {head}" + (f" ({'; '.join(tail)})" if tail else ""))
    return lines


def _history_lines(history: list[dict], me: str | None = None, compact: bool = False) -> list[str]:
    """Earlier steps: summary, digest, everyone's public decisions (no rationales) and statements, the narrative.
    `compact`: only the summary, the digest and the narrative (the synthesizer's later steps, which build on the
    digest of this period)."""
    if not history:
        return ["(no earlier steps)"]
    lines: list[str] = []
    for i, h in enumerate(history):
        last = i == len(history) - 1
        lines.append(f"Step {h['step']} ({h['period']}): {h.get('summary') or ''}".rstrip(": "))
        if h.get("digest"):
            lines.append(f"  What happened: {_cut(h['digest'], DIGEST_CHARS)}")
        if h.get("decisions") and not compact:
            lines.append("  Decisions that led to it:")
            for d in h["decisions"]:
                who = f"{d['name']}{' (you)' if me and d.get('actor_id') == me else ''}"
                if d.get("status") != "ok" or not d.get("output"):
                    lines.append(f"  - {who}: no response")
                    continue
                o = d["output"]
                stmt = f' — said: "{_cut(o["public_statement"], 200)}"' if o.get("public_statement") else ""
                lines.append(f"  - {who}{stmt}")
                lines += decision_lines(o, indent="      ", private=False)
        if h.get("narrative") and not last:
            lines.append(f"  Narrative then: {_cut(h['narrative'], NARRATIVE_CHARS)}")
    return lines


# ------------------------------------------------------------------------------------------------ actor

def actor_schema(actor: dict) -> dict:
    qs = actor.get("decision_questions") or []
    auto = actor.get("autonomy") or {}
    props: dict[str, Any] = {}
    required: list[str] = []
    if qs:
        props["answers"] = {"type": "object", "additionalProperties": False, "required": [q["id"] for q in qs],
                            "properties": {q["id"]: {"type": "object", "additionalProperties": False,
                                                     "required": ["value", "rationale"],
                                                     "properties": {"value": {"type": "string"},
                                                                    "rationale": {"type": "string"}}} for q in qs}}
        required.append("answers")
    if auto.get("enabled"):
        props["actions"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "detail", "target", "intended_effect"],
            "properties": {k: {"type": "string"} for k in ("title", "detail", "target", "intended_effect")}}}
        required.append("actions")
    props.update({"public_statement": {"type": "string"}, "rationale": {"type": "string"},
                  "expected_consequences": {"type": "array", "items": {"type": "string"}}})
    required += ["public_statement", "rationale", "expected_consequences"]
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


def actor_messages(world: dict, actor: dict, state: dict, history: list[dict], next_period: str) -> list[dict]:
    lang = world.get("language") or "English"
    qs = actor.get("decision_questions") or []
    auto = actor.get("autonomy") or {}
    n = int(auto.get("max_actions") or M.DEFAULT_MAX_ACTIONS)
    keys = []
    if qs:
        keys.append('  "answers": an object with one key per decision question id, each {"value": your answer as text, '
                    '"rationale": a short why}. A required question must get a non-empty answer; an optional one may be '
                    '"" when it does not apply')
    if auto.get("enabled"):
        keys.append(f'  "actions": a list of 0-{n} autonomous actions, each {{"title", "detail": what exactly you do, '
                    '"target": who or what it acts on, "intended_effect": what you want it to change}; [] when nothing '
                    "else is worth doing")
    system = "\n".join([
        f'You play one actor in a turn-based world simulation ("{world["name"]}"). Stay in role: decide what '
        f"{actor['name']} does during the coming period, from {state.get('period')} to {next_period}, given what it "
        "cares about, its private information and the situation. Be realistic about its constraints.",
        ("Answer every decision question in plain text: concrete (direction, size, timing, conditions), following its "
         "guidance." if qs else "This actor has no decision questions."),
        (f"Beyond the questions you may take up to {n} autonomous actions of your own (none is fine); do not repeat "
         "what an answer already decides." if auto.get("enabled") else
         "Autonomous actions are switched off for this actor: answer the questions only."),
        "Answer with one JSON object only (no prose, no code fences) with these keys:",
        *keys,
        '  "public_statement": what you say in public (may be empty)',
        '  "rationale": why, overall, in a few sentences (private: only the synthesizer reads it)',
        '  "expected_consequences": a list of 1-4 short expected consequences',
        f"Write every text value in {lang}. Do not invent data you were not given; reason from it.",
    ])
    lines = _world_lines(world) + ["", f"# You are: {actor['name']}", actor.get("role") or "(no role given)", "",
                                   "# What you care about"]
    lines += [f"- {c}" for c in actor.get("cares_about") or []] or ["- (not specified)"]
    lines += ["", "# Decision questions (answer every one)"]
    for q in qs:
        lines.append(f"- [{q['id']}] ({'required' if q.get('required', True) else 'optional'}) {q['text']}")
        if q.get("guidance"):
            lines += ["  Guidance: " + q["guidance"].replace("\n", "\n  ")]
    if not qs:
        lines.append("(none)")
    lines += ["", "# Autonomous actions"]
    if auto.get("enabled"):
        lines.append(f"You may also take 0 to {n} actions of your own beyond the questions.")
        if auto.get("guidance"):
            lines.append("What fits this actor: " + auto["guidance"])
    else:
        lines.append('Switched off for this actor: leave them out.')
    if actor.get("private_info"):
        lines += ["", "# Private information (only you see this)", actor["private_info"]]
    lines += [""] + _state_block(world, state, actor.get("visible_fields"))
    lines += ["", "# Recent history (oldest first)"] + _history_lines(history, me=actor["id"])
    lines += ["", f"Decide {actor['name']}'s answers and actions for {state.get('period')} → {next_period}. JSON only."]
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


# ------------------------------------------------------------------------------------------------ synthesizer

def _value_schema(field: dict) -> dict:
    t = field["type"]
    if t in ("number", "percent"):
        return {"type": "number"}
    if t == "integer":
        return {"type": "integer"}
    if t == "boolean":
        return {"type": "boolean"}
    if t == "enum":
        return {"type": "string", "enum": list(field.get("options") or [])}
    return {"type": "string"}


def digest_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["summary", "digest", "threads"],
            "properties": {"summary": {"type": "string"}, "digest": {"type": "string"},
                           "threads": {"type": "array", "items": {"type": "string"}}}}


def values_schema(fields: list[dict]) -> dict:
    props = {f["id"]: {"type": "object", "additionalProperties": False, "required": ["value", "reason"],
                       "properties": {"value": _value_schema(f), "reason": {"type": "string"}}} for f in fields}
    return {"type": "object", "additionalProperties": False, "required": ["values"],
            "properties": {"values": {"type": "object", "additionalProperties": False,
                                      "required": [f["id"] for f in fields], "properties": props}}}


def statuses_schema(questions: list[dict]) -> dict:
    props = {q["id"]: {"type": "object", "additionalProperties": False, "required": ["status", "changed", "reason"],
                       "properties": {"status": {"type": "string"}, "changed": {"type": "boolean"},
                                      "reason": {"type": "string"}}} for q in questions}
    return {"type": "object", "additionalProperties": False, "required": ["statuses"],
            "properties": {"statuses": {"type": "object", "additionalProperties": False,
                                        "required": [q["id"] for q in questions], "properties": props}}}


def narrative_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["narrative"],
            "properties": {"narrative": {"type": "string"}}}


def step_schema(world: dict, step: dict) -> dict:
    if step["kind"] == "values":
        fmap = M.field_map(world["schema"])
        return values_schema([fmap[i] for i in step["items"]])
    if step["kind"] == "statuses":
        qmap = M.question_map(world["schema"])
        return statuses_schema([qmap[i] for i in step["items"]])
    return digest_schema() if step["kind"] == "digest" else narrative_schema()


def _schema_lines(fields: list[dict]) -> list[str]:
    lines: list[str] = []
    group = None
    for f in fields:
        if f["group"] != group:
            group = f["group"]
            lines.append(f"[{group}]")
        bits = [f"type {f['type']}"]
        if f.get("unit"):
            bits.append(f"unit {f['unit']}")
        if f["type"] == "enum":
            bits.append("options: " + " | ".join(f.get("options") or []))
        if f.get("min") is not None or f.get("max") is not None:
            lo = M._num_text(f["min"]) if f.get("min") is not None else "-inf"
            hi = M._num_text(f["max"]) if f.get("max") is not None else "+inf"
            bits.append(f"plausible {lo} … {hi}")
        if f.get("tag"):
            bits.append(f"tag {f['tag']}")
        line = f"- {f['id']} — {f['label']} ({'; '.join(bits)})"
        if f.get("hist"):
            line += f"; history: {f['hist']}"
        if f.get("description"):
            line += f"\n    {f['description'][:400]}"
        lines.append(line)
    return lines


def _question_lines(questions: list[dict]) -> list[str]:
    lines: list[str] = []
    group = None
    for q in questions:
        if q["group"] != group:
            group = q["group"]
            lines.append(f"[{group}]")
        lines.append(f"- {q['id']} — {q['text']}")
        if q.get("guidance"):
            lines.append(f"    what the status should cover: {q['guidance']}")
    return lines


def _actor_output_lines(results: list[dict]) -> list[str]:
    lines: list[str] = []
    for r in results:
        if r.get("status") != "ok" or not r.get("output"):
            lines += [f"## {r['name']} [{r['actor_id']}]: NO RESPONSE ({r.get('error') or 'failed'})",
                      "  Treat this actor as having taken no new action this period (status quo), and say so if it matters.", ""]
            continue
        o = r["output"]
        lines += [f"## {r['name']} [{r['actor_id']}]"] + decision_lines(o, indent="  ", private=True)
        lines += [f"  Rationale: {o.get('rationale') or ''}", f"  Public statement: {o.get('public_statement') or ''}",
                  "  Expected consequences: " + "; ".join(o.get("expected_consequences") or []), ""]
    return lines or ["(no actors)"]


def _written_lines(world: dict, state: dict, written: dict) -> list[str]:
    """What earlier calls of this iteration wrote: new values (with the change and reason), then new statuses."""
    lines: list[str] = []
    fmap = M.field_map(world["schema"])
    qmap = M.question_map(world["schema"])
    prev_vals = state.get("values") or {}
    vals = written.get("values") or {}
    reasons = written.get("reasons") or {}
    if vals:
        lines.append("## New indicator values")
        for fid, v in vals.items():
            f = fmap.get(fid)
            if not f:
                continue
            same = M.values_equal(f, prev_vals.get(fid), v)
            change = "unchanged" if same else f"was {fmt_value(f, prev_vals.get(fid))}"
            why = f" — {reasons[fid]}" if reasons.get(fid) else ""
            lines.append(f"- {f['label']} [{fid}]: {fmt_value(f, v)} ({change}){why}")
    sts = written.get("statuses") or {}
    if sts:
        lines.append("## New state question statuses")
        changed = written.get("status_changed") or {}
        sreasons = written.get("status_reasons") or {}
        for qid, text in sts.items():
            q = qmap.get(qid)
            if not q:
                continue
            why = f" — {sreasons[qid]}" if sreasons.get(qid) else ""
            lines.append(f"- {q['text']} [{qid}] ({'changed' if changed.get(qid) else 'unchanged'}): {text}{why}")
    return lines


def _synth_system(world: dict, step: dict, plan: list[dict], next_period: str, task: list[str]) -> str:
    lang = world.get("language") or "English"
    n = next((i for i, s in enumerate(plan, 1) if s["key"] == step["key"]), 1)
    counts = {k: sum(1 for s in plan if s["kind"] == k) for k in ("values", "statuses")}
    return "\n".join([
        f'You are the synthesizer (game master) of a world simulation ("{world["name"]}"): you resolve every '
        f"actor's decisions jointly into ONE coherent next state for {next_period}, following the world's instructions.",
        f"You work in {len(plan)} short steps (1 digest, {counts['values']} indicator steps, {counts['statuses']} "
        f"question steps, 1 narrative) so that each one gets full attention. This is step {n} of {len(plan)}: "
        f"{step['label']}.",
        *task,
        "Answer with one JSON object only (no prose, no code fences), with exactly the keys asked for: no other keys.",
        f"Write every text value in {lang}.",
    ])


def _synth_user(world: dict, state: dict, results: list[dict], history: list[dict], next_period: str,
                digest: dict | None, written: dict | None) -> list[str]:
    lines = _world_lines(world)
    if (world.get("synthesizer") or {}).get("instructions"):
        lines += ["", "# Synthesizer instructions", world["synthesizer"]["instructions"]]
    lines += [""] + _state_block(world, state, title=f"# Previous state: step {state.get('step')}, period "
                                                     f"{state.get('period')}")
    lines += ["", "# Recent history (oldest first)"] + _history_lines(history, compact=digest is not None)
    lines += ["", f"# Actors' decisions for {state.get('period')} → {next_period}"] + _actor_output_lines(results)
    if digest:
        lines += ["", "# This period's digest (step 1)", digest.get("digest") or "(none)"]
        if digest.get("threads"):
            lines += ["Causal threads:"] + [f"- {t}" for t in digest["threads"]]
    elif digest is not None:
        lines += ["", "# This period's digest (step 1)", "(unavailable: the digest step failed; reason from the decisions)"]
    w = _written_lines(world, state, written or {})
    if w:
        lines += ["", "# Already written in this iteration (keep consistent with it)"] + w
    return lines


def synth_step_messages(world: dict, step: dict, plan: list[dict], state: dict, results: list[dict],
                        history: list[dict], next_period: str, digest: dict | None = None,
                        written: dict | None = None) -> list[dict]:
    """The messages of one synthesizer step (model.synth_plan): the shared context plus this step's own task."""
    zh = M.is_cjk(world.get("language"))
    target, max_chars = M.narrative_limits(world.get("language"))
    schema = world["schema"]
    kind = step["kind"]
    if kind == "digest":
        task = [f"Read the previous state and every actor's decisions and work out what happens from "
                f"{state.get('period')} to {next_period}. Write no values or statuses yet. Keys:",
                '  "summary": one line on what happened this period',
                '  "digest": ' + ("本季要事, about 300 Chinese characters" if zh else "this period's main events, about "
                                  "200 words") + ": the main events, how the actors' decisions interact or conflict, "
                "and which forces dominate; nothing an actor did not do happens by assumption unless it follows from "
                "the situation",
                '  "threads": 2-6 short causal threads ("cause → effect") the later steps should follow']
        tail = [f"Write the digest for {state.get('period')} → {next_period}. JSON only."]
    elif kind == "values":
        fmap = M.field_map(schema)
        fields = [fmap[i] for i in step["items"]]
        task = [f"Write the value at the start of {next_period} of ONLY these {len(fields)} indicators: "
                + ", ".join(step["items"]) + ". Keys:",
                '  "values": an object with exactly these ids, each {"value": <typed value>, "reason": <why it changed, '
                'or "" when unchanged>}. Use the field\'s type and unit; an enum takes one of its options exactly.',
                "Rules: follow the digest and the decisions; stay consistent with every value already written and with "
                "each other; change only what this period's events move; give a reason for every change; keep values "
                "plausible against their range and history and say so in the reason when one goes beyond them."]
        tail = ["# Your indicators (definitions)"] + _schema_lines(fields) + [
            "", f"Write the values of: {', '.join(step['items'])}. JSON only."]
    elif kind == "statuses":
        qmap = M.question_map(schema)
        qs = [qmap[i] for i in step["items"]]
        task = [f"Every indicator is settled. Write the status (现状描述) at the start of {next_period} of ONLY these "
                f"{len(qs)} state questions: " + ", ".join(step["items"]) + ". Keys:",
                '  "statuses": an object with exactly these ids, each {"status": the current status in 2-4 sentences '
                '(restate it in full when it did not change), "changed": true|false, "reason": why it changed, or "" '
                "when unchanged}",
                "Rules: ground every status in the new values and the decisions; keep it consistent with what was "
                "already written; be honest about what is still uncertain; keep unconfirmed facts marked as such."]
        tail = ["# Your questions"] + _question_lines(qs) + [
            "", f"Write the statuses of: {', '.join(step['items'])}. JSON only."]
    else:
        task = [f"Every value and status is settled. Write the narrative of the new state at {next_period}: a summary "
                f"of the whole state — the indicators and every question's status — in {target} (at most {max_chars} "
                'characters). Key: "narrative".']
        tail = [f"Write the narrative of {next_period} ({target}, at most {max_chars} characters). JSON only."]
    system = _synth_system(world, step, plan, next_period, task)
    lines = _synth_user(world, state, results, history, next_period, None if kind == "digest" else (digest or {}),
                        written) + [""] + tail
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


# ------------------------------------------------------------------------------------------------ repair

def repair_messages(messages: list[dict], bad_text: str, error: str) -> list[dict]:
    return messages + [
        {"role": "assistant", "content": (bad_text or "")[:8000]},
        {"role": "user", "content": f"That answer could not be used: {error}. Reply again with ONLY the corrected "
                                    "JSON object in the required format: no prose, no code fences."}]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
