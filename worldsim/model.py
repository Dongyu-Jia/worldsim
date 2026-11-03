"""The world's data model as plain dicts: normalization of a world (schema, actors, synthesizer, initial state), value
coercion, period arithmetic, the synthesizer's chunking, the validation of its next state against the schema, and the
migration of worlds saved in the first format.

World (format worldsim-world/2)::

    {id, name, description (the world description: fixed, shown to every actor and the synthesizer, never rewritten),
     language, period_step {unit quarter|month|year, count}, history_window,
     model {model, temperature, reasoning_effort}           blank = the global default from Settings
     schema {fields: [Field], questions: [StateQuestion]}, actors [Actor],
     synthesizer {instructions, model, max_items_per_call}, initial_state State, created, updated}

Field:          {id, label, group, type number|percent|integer|text|enum|boolean, unit, description, options [enum],
                 min, max (plausible bounds: a value outside is kept and flagged), hist (historical range note), tag}
StateQuestion:  {id, text, group, guidance}   a predefined question; every state holds a status (现状描述) for it
State:          {step, period, values {field id: value | None}, statuses {question id: text}, narrative, summary}
                the narrative is a short summary of all values and statuses (about 100 字 in a Chinese world)
Actor:          {id, name, enabled, role, cares_about [str],
                 decision_questions [{id, text, guidance, required}],      answered in text every iteration
                 autonomy {enabled, max_actions, guidance},                actions of its own beyond the questions
                 private_info, visible_fields null (all) | [field id], model}

Nothing here calls a model, touches the disk or the network.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any

FORMAT = "worldsim-world/2"
LEGACY_FORMATS = ("worldsim-world/1",)
FIELD_TYPES = ("number", "percent", "integer", "text", "enum", "boolean")
NUMERIC_TYPES = ("number", "percent", "integer")
PERIOD_UNITS = {"month": 1, "quarter": 3, "year": 12}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
WORLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REASONING_EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
LIMITS = {"name": 120, "label": 160, "short": 400, "text": 20000, "fields": 300, "actors": 60, "options": 60,
          "state_questions": 80, "decision_questions": 8, "cares": 20, "status": 4000, "answer": 4000,
          "max_actions": 10, "items_per_call": 30}
DEFAULT_MAX_ACTIONS = 3
DEFAULT_ITEMS_PER_CALL = 6
SYNTH_FAILED = {"zh": "合成失败，沿用上期", "en": "synthesis failed: carried forward from the previous step"}


class WorldError(ValueError):
    """A world (or a part of it) that does not validate: `problems` lists every reason, in English."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems) or "invalid world")


def is_cjk(language: str | None) -> bool:
    lang = (language or "").lower()
    return any(t in lang for t in ("中", "chinese", "日本", "japanese", "한국", "korean")) or lang.startswith(("zh", "ja", "ko"))


def narrative_limits(language: str | None) -> tuple[str, int]:
    """(the length asked for, the hard maximum in characters) of a state's narrative."""
    return ("about 100 Chinese characters (100 字)", 120) if is_cjk(language) else ("about 80 words", 600)


def synth_failed_reason(language: str | None) -> str:
    return SYNTH_FAILED["zh" if is_cjk(language) else "en"]


# ---------------------------------------------------------------------------------------------------------- helpers

def _text(raw: Any, limit: int = LIMITS["text"]) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return _num_text(raw)
    if not isinstance(raw, str):
        raw = str(raw)
    return raw.strip()[:limit]


def _num_text(x: float) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        return str(x)
    return f"{x:.6g}" if abs(x) < 1e15 else str(x)


def _finite(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(float(raw)) else None
    return None


def slug(text: str, fallback: str = "item") -> str:
    """A lower-case id from free text ("Policy rate (%)" -> "policy_rate"); `fallback` when nothing is left."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}" if s else fallback
    return s[:48].rstrip("_") or fallback


def unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    for i in range(2, 10000):
        cand = f"{base[:44]}_{i}"
        if cand not in taken:
            return cand
    raise WorldError(f"could not make a unique id from {base!r}")


# ---------------------------------------------------------------------------------------------------------- periods

Q_RE = re.compile(r"^(\d{4})\s*[Qq]([1-4])$")
M_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
Y_RE = re.compile(r"^(\d{4})$")
TAIL_RE = re.compile(r"^(.*?)(\d+)$")


def advance_period(period: str, unit: str = "quarter", count: int = 1) -> str:
    """The period `count` units after `period`: 2026Q4 + 1 quarter = 2027Q1, 2026-11 + 1 month = 2026-12,
    2026 + 1 year = 2027, "Turn 3" + 1 = "Turn 4"; anything else gets " +n"."""
    p = str(period or "").strip()
    months = PERIOD_UNITS.get(unit, 3) * max(1, int(count or 1))
    m = Q_RE.match(p)
    if m:
        idx = int(m.group(1)) * 12 + (int(m.group(2)) - 1) * 3 + months
        y, mm = divmod(idx, 12)
        return f"{y}Q{mm // 3 + 1}"
    m = M_RE.match(p)
    if m and 1 <= int(m.group(2)) <= 12:
        idx = int(m.group(1)) * 12 + int(m.group(2)) - 1 + months
        y, mm = divmod(idx, 12)
        return f"{y}-{mm + 1:02d}"
    m = Y_RE.match(p)
    if m:
        return str(int(m.group(1)) + max(1, months // 12))
    m = TAIL_RE.match(p)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + max(1, int(count or 1))}"
    return f"{p} +{max(1, int(count or 1))}" if p else f"+{max(1, int(count or 1))}"


# ---------------------------------------------------------------------------------------------------------- values

def coerce_value(field: dict, raw: Any) -> tuple[Any, str | None, bool]:
    """(value, note, ok) for `raw` as a value of `field`. ok False: the value cannot be read as this type (the caller
    decides what to do: carry the previous value forward, keep null). A note says what was changed to read it."""
    ftype = field.get("type")
    if raw is None:
        return None, None, False
    if ftype in NUMERIC_TYPES:
        num = _finite(raw)
        note = None
        if num is None and isinstance(raw, str):
            cleaned = (raw.strip().replace("，", "").replace(",", "").replace("％", "%").replace("−", "-")
                       .replace("pp", "").replace("%", "").replace("+", "").strip())
            try:
                num = float(cleaned)
                note = f"read {raw!r} as a number"
            except ValueError:
                num = None
            if num is not None and not math.isfinite(num):
                num = None
        if num is None:
            return None, None, False
        if ftype == "integer":
            if num != int(num):
                return int(round(num)), f"rounded {_num_text(num)} to an integer", True
            return int(num), note, True
        return float(num), note, True
    if ftype == "boolean":
        if isinstance(raw, bool):
            return raw, None, True
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw), f"read {raw!r} as a boolean", True
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("true", "yes", "y", "1", "是", "真", "对", "有"):
                return True, f"read {raw!r} as true", True
            if s in ("false", "no", "n", "0", "否", "假", "不", "无"):
                return False, f"read {raw!r} as false", True
        return None, None, False
    if ftype == "enum":
        options = field.get("options") or []
        if not isinstance(raw, str):
            raw = _text(raw)
        if raw in options:
            return raw, None, True
        key = re.sub(r"\s+", "", raw).lower()
        for opt in options:
            if re.sub(r"\s+", "", opt).lower() == key:
                return opt, f"matched {raw!r} to the option {opt!r}", True
        return None, None, False
    # text
    if isinstance(raw, (dict, list)):
        return None, None, False
    if isinstance(raw, str):
        return raw.strip()[:LIMITS["text"]], None, True
    return _text(raw), f"read {raw!r} as text", True


def values_equal(field: dict, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if field.get("type") in NUMERIC_TYPES:
        fa, fb = _finite(a), _finite(b)
        if fa is None or fb is None:
            return a == b
        return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))
    return a == b


def range_note(field: dict, value: Any) -> str | None:
    """Why a numeric value lies outside the field's plausible [min, max], or None."""
    if field.get("type") not in NUMERIC_TYPES:
        return None
    v = _finite(value)
    if v is None:
        return None
    lo, hi = field.get("min"), field.get("max")
    if lo is not None and v < lo:
        return f"{_num_text(v)} is below the plausible minimum {_num_text(lo)}"
    if hi is not None and v > hi:
        return f"{_num_text(v)} is above the plausible maximum {_num_text(hi)}"
    return None


# ---------------------------------------------------------------------------------------------------------- schema

def normalize_field(raw: Any, index: int = 0) -> tuple[dict, list[str]]:
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, [f"field #{index + 1} is not an object"]
    fid = _text(raw.get("id"), 48)
    label = _text(raw.get("label"), LIMITS["label"]) or fid
    where = f"field {fid or '#' + str(index + 1)}"
    if not ID_RE.match(fid):
        problems.append(f"{where}: id must be lower-case letters, digits and _ (starting with a letter, at most 48)")
    ftype = _text(raw.get("type"), 20).lower() or "number"
    if ftype not in FIELD_TYPES:
        problems.append(f"{where}: type must be one of {', '.join(FIELD_TYPES)}")
    field = {"id": fid, "label": label, "group": _text(raw.get("group"), 80) or "General", "type": ftype,
             "unit": _text(raw.get("unit"), 40), "description": _text(raw.get("description"), 2000),
             "options": [], "min": None, "max": None, "hist": _text(raw.get("hist"), 400),
             "tag": _text(raw.get("tag"), 40)}
    if ftype == "enum":
        opts = raw.get("options")
        if isinstance(opts, str):
            opts = [o for o in re.split(r"[\n,，;；|]", opts)]
        seen: list[str] = []
        for o in opts or []:
            t = _text(o, LIMITS["label"])
            if t and t not in seen:
                seen.append(t)
        if not seen:
            problems.append(f"{where}: an enum needs at least one option")
        field["options"] = seen[:LIMITS["options"]]
    if ftype in NUMERIC_TYPES:
        for key in ("min", "max"):
            v = raw.get(key)
            if v in (None, ""):
                continue
            num, _, ok = coerce_value({"type": "number"}, v)
            if not ok:
                problems.append(f"{where}: {key} must be a number")
            else:
                field[key] = num
        if field["min"] is not None and field["max"] is not None and field["min"] > field["max"]:
            problems.append(f"{where}: min is greater than max")
    return field, problems


def normalize_state_question(raw: Any, index: int = 0) -> tuple[dict, list[str]]:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}, [f"state question #{index + 1} is not an object"]
    qid = _text(raw.get("id"), 48)
    text = _text(raw.get("text") or raw.get("question"), LIMITS["short"])
    where = f"state question {qid or '#' + str(index + 1)}"
    problems: list[str] = []
    if not ID_RE.match(qid):
        problems.append(f"{where}: id must be lower-case letters, digits and _ (starting with a letter, at most 48)")
    if not text:
        problems.append(f"{where}: the question text is empty")
    return {"id": qid, "text": text, "group": _text(raw.get("group"), 80) or "General",
            "guidance": _text(raw.get("guidance"), 2000)}, problems


def normalize_schema(raw: Any) -> tuple[dict, list[str]]:
    """{fields, questions}: the indicator fields and the predefined state questions (a blank question id is filled)."""
    fields_raw = raw.get("fields") if isinstance(raw, dict) else raw
    if not isinstance(fields_raw, list):
        return {"fields": [], "questions": []}, ["schema.fields must be a list"]
    problems: list[str] = []
    fields: list[dict] = []
    seen: set[str] = set()
    if len(fields_raw) > LIMITS["fields"]:
        problems.append(f"a schema has at most {LIMITS['fields']} fields")
    for i, f in enumerate(fields_raw[:LIMITS["fields"]]):
        field, probs = normalize_field(f, i)
        problems += probs
        if field.get("id") in seen:
            problems.append(f"field id {field['id']} is used twice")
        seen.add(field.get("id", ""))
        fields.append(field)
    q_raw = raw.get("questions") if isinstance(raw, dict) else None
    if q_raw is None:
        q_raw = []
    if not isinstance(q_raw, list):
        problems.append("schema.questions must be a list")
        q_raw = []
    if len(q_raw) > LIMITS["state_questions"]:
        problems.append(f"a schema has at most {LIMITS['state_questions']} state questions")
    questions: list[dict] = []
    taken: set[str] = set()
    for i, q in enumerate(q_raw[:LIMITS["state_questions"]]):
        if isinstance(q, str):
            q = {"text": q}
        if isinstance(q, dict) and not _text(q.get("id"), 48):
            q = {**q, "id": unique_id(f"sq{i + 1}", taken | seen)}
        question, probs = normalize_state_question(q, i)
        problems += probs
        qid = question.get("id", "")
        if qid in taken:
            problems.append(f"state question id {qid} is used twice")
        elif qid in seen:
            problems.append(f"state question id {qid} is also a field id")
        taken.add(qid)
        if question:
            questions.append(question)
    return {"fields": fields, "questions": questions}, problems


def field_map(schema: dict) -> dict[str, dict]:
    return {f["id"]: f for f in (schema or {}).get("fields", [])}


def question_map(schema: dict) -> dict[str, dict]:
    return {q["id"]: q for q in (schema or {}).get("questions", [])}


# ---------------------------------------------------------------------------------------------------------- state

def normalize_statuses(schema: dict, raw: Any) -> dict[str, str]:
    """A status text for every state question ("" when none is given); unknown ids are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for q in (schema or {}).get("questions", []):
        v = raw.get(q["id"])
        if isinstance(v, dict):
            v = v.get("status")
        out[q["id"]] = "" if v is None or isinstance(v, (dict, list)) else _text(v, LIMITS["status"])
    return out


def normalize_state(schema: dict, raw: Any, *, step: int = 0) -> tuple[dict, list[str]]:
    """An initial (or imported) state: values coerced to their field types (a value that cannot be read is an error;
    a missing value is null), unknown ids dropped, a status for every state question. Returns (state, problems)."""
    problems: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    fmap = field_map(schema)
    vals_raw = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    values: dict[str, Any] = {}
    for fid, field in fmap.items():
        v = vals_raw.get(fid)
        if v is None or (isinstance(v, str) and v.strip() == "" and field["type"] != "text"):
            values[fid] = None
            continue
        val, _, ok = coerce_value(field, v)
        if not ok:
            problems.append(f"value of {fid} ({field['label']}) cannot be read as {field['type']}: {v!r}")
            values[fid] = None
        else:
            values[fid] = val
    narrative = raw.get("narrative")
    if narrative is None and isinstance(raw.get("text"), dict):
        narrative = raw["text"].get("narrative")
    state = {"step": int(raw.get("step", step) or step), "period": _text(raw.get("period"), 40) or "T0",
             "values": values, "statuses": normalize_statuses(schema, raw.get("statuses")),
             "narrative": _text(narrative), "summary": _text(raw.get("summary"), LIMITS["short"])}
    for key in ("flags", "changes", "status_changes", "created", "reasons", "status_reasons", "usage", "cost",
                "cost_complete", "partial"):
        if key in raw:
            state[key] = copy.deepcopy(raw[key])
    return state, problems


# ---------------------------------------------------------------------------------------------------------- actors

def _period_word(unit: str, zh: bool) -> str:
    if zh:
        return {"month": "本月", "year": "今年"}.get(unit, "本季")
    return {"month": "this month", "year": "this year"}.get(unit, "this quarter")


def is_legacy_actor(raw: Any) -> bool:
    """An actor saved with the first format's fixed action space (and no decision questions)."""
    return isinstance(raw, dict) and "decision_questions" not in raw and ("action_space" in raw or "allow_free_action" in raw)


def legacy_question(raw: dict, language: str = "", period_unit: str = "quarter") -> dict:
    """The one required decision question an old action space becomes: 「本季你会怎么做？」 with the former actions
    (status quo first) listed as guidance."""
    zh = is_cjk(language)
    lines = []
    for a in raw.get("action_space") or []:
        if not isinstance(a, dict):
            continue
        label = _text(a.get("label"), LIMITS["label"]) or _text(a.get("id"), 48)
        desc = _text(a.get("description"), 400)
        if label:
            lines.append(f"- {label}" + ((f"：{desc}" if zh else f": {desc}") if desc else ""))
    free = bool(raw.get("allow_free_action", True))
    if zh:
        text = f"{_period_word(period_unit, True)}你会怎么做？"
        head = "可以从下列做法中选择或组合（原来的行动空间，现状在前），并说明幅度、时机与条件："
        tail = "也可以提出清单之外的做法。" if free else "请在下列做法中选择。"
    else:
        text = f"What will you do {_period_word(period_unit, False)}?"
        head = ("Choose or combine from these options (the former action space, status quo first) and say the size, "
                "timing and conditions:")
        tail = "You may also propose something outside the list." if free else "Choose among these options."
    return {"id": "q_action", "text": text, "guidance": "\n".join([head] + lines + [tail]) if lines else tail,
            "required": True}


def _whole(raw: Any, default: int) -> int | None:
    """An integer from a number or a numeric string; None when it is not a whole number."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            f = float(raw.strip())
        except ValueError:
            return None
        return int(f) if f.is_integer() else None
    return None


def normalize_actor(raw: Any, index: int = 0, field_ids: set[str] | None = None, language: str = "",
                    period_unit: str = "quarter") -> tuple[dict, list[str]]:
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, [f"actor #{index + 1} is not an object"]
    if is_legacy_actor(raw):
        raw = {**raw, "decision_questions": [legacy_question(raw, language, period_unit)],
               "autonomy": {"enabled": True, "max_actions": DEFAULT_MAX_ACTIONS, "guidance": ""}}
    aid = _text(raw.get("id"), 48)
    name = _text(raw.get("name"), LIMITS["name"]) or aid
    where = f"actor {aid or '#' + str(index + 1)}"
    if not ID_RE.match(aid):
        problems.append(f"{where}: id must be lower-case letters, digits and _ (starting with a letter)")
    cares = raw.get("cares_about")
    if isinstance(cares, str):
        cares = [c for c in cares.splitlines()]
    cares_list = [t for t in (_text(c, LIMITS["short"]) for c in (cares or [])) if t][:LIMITS["cares"]]
    q_raw = raw.get("decision_questions")
    if q_raw is None:
        q_raw = []
    if not isinstance(q_raw, list):
        problems.append(f"{where}: decision_questions must be a list")
        q_raw = []
    questions: list[dict] = []
    taken: set[str] = set()
    for j, q in enumerate(q_raw):
        if isinstance(q, str):
            q = {"text": q}
        if not isinstance(q, dict):
            problems.append(f"{where}: decision question #{j + 1} is not an object")
            continue
        qid = _text(q.get("id"), 48) or unique_id(f"q{j + 1}", taken)
        text = _text(q.get("text") or q.get("question"), LIMITS["short"])
        if not ID_RE.match(qid):
            problems.append(f"{where}: decision question id {qid!r} must be lower-case letters, digits and _")
        elif qid in taken:
            problems.append(f"{where}: decision question id {qid} is used twice")
        taken.add(qid)
        if not text:
            problems.append(f"{where}: decision question {qid} has no text")
        questions.append({"id": qid, "text": text, "guidance": _text(q.get("guidance"), 2000),
                          "required": bool(q.get("required", True))})
    if len(questions) > LIMITS["decision_questions"]:
        problems.append(f"{where}: at most {LIMITS['decision_questions']} decision questions")
    auto = raw.get("autonomy") if isinstance(raw.get("autonomy"), dict) else {}
    max_actions = _whole(auto.get("max_actions"), DEFAULT_MAX_ACTIONS)
    if max_actions is None or not 1 <= max_actions <= LIMITS["max_actions"]:
        problems.append(f"{where}: autonomy.max_actions must be a whole number from 1 to {LIMITS['max_actions']}")
        max_actions = DEFAULT_MAX_ACTIONS if max_actions is None else max(1, min(LIMITS["max_actions"], max_actions))
    autonomy = {"enabled": bool(auto.get("enabled", True)), "max_actions": max_actions,
                "guidance": _text(auto.get("guidance"), 2000)}
    if not questions and not autonomy["enabled"]:
        problems.append(f"{where}: needs at least one decision question, or autonomous actions switched on")
    visible = raw.get("visible_fields")
    if visible is not None:
        if not isinstance(visible, list):
            problems.append(f"{where}: visible_fields must be a list of field ids or null (all)")
            visible = None
        else:
            visible = [_text(v, 48) for v in visible if _text(v, 48)]
            if field_ids is not None:
                visible = [v for v in visible if v in field_ids]
    actor = {"id": aid, "name": name, "enabled": bool(raw.get("enabled", True)),
             "role": _text(raw.get("role")), "cares_about": cares_list, "decision_questions": questions,
             "autonomy": autonomy, "private_info": _text(raw.get("private_info")),
             "visible_fields": visible, "model": _text(raw.get("model"), 200)}
    return actor, problems


def normalize_actors(raw: Any, field_ids: set[str] | None = None, language: str = "",
                     period_unit: str = "quarter") -> tuple[list[dict], list[str]]:
    if not isinstance(raw, list):
        return [], ["actors must be a list"]
    problems: list[str] = []
    out: list[dict] = []
    seen: set[str] = set()
    if len(raw) > LIMITS["actors"]:
        problems.append(f"a world has at most {LIMITS['actors']} actors")
    for i, a in enumerate(raw[:LIMITS["actors"]]):
        actor, probs = normalize_actor(a, i, field_ids, language, period_unit)
        problems += probs
        if actor.get("id") in seen:
            problems.append(f"actor id {actor['id']} is used twice")
        seen.add(actor.get("id", ""))
        out.append(actor)
    return out, problems


def normalize_synthesizer(raw: Any) -> tuple[dict, list[str]]:
    raw = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    x = _whole(raw.get("max_items_per_call"), DEFAULT_ITEMS_PER_CALL)
    if x is None or not 1 <= x <= LIMITS["items_per_call"]:
        problems.append(f"synthesizer.max_items_per_call must be a whole number from 1 to {LIMITS['items_per_call']}")
        x = DEFAULT_ITEMS_PER_CALL if x is None else max(1, min(LIMITS["items_per_call"], x))
    return {"instructions": _text(raw.get("instructions")), "model": _text(raw.get("model"), 200),
            "max_items_per_call": x}, problems


def normalize_model_settings(raw: Any) -> tuple[dict, list[str]]:
    raw = raw if isinstance(raw, dict) else {}
    problems: list[str] = []
    temp = raw.get("temperature")
    if temp in ("", None):
        temp = None
    else:
        t, _, ok = coerce_value({"type": "number"}, temp)
        if not ok or not 0 <= t <= 2:
            problems.append("model.temperature must be between 0 and 2 (or blank)")
            temp = None
        else:
            temp = t
    effort = _text(raw.get("reasoning_effort"), 20).lower()
    if effort not in REASONING_EFFORTS:
        problems.append(f"model.reasoning_effort must be one of {', '.join(e or '(default)' for e in REASONING_EFFORTS)}")
        effort = ""
    return {"model": _text(raw.get("model"), 200), "temperature": temp, "reasoning_effort": effort}, problems


# ---------------------------------------------------------------------------------------------------------- world

def normalize_world(raw: Any, *, require_id: bool = True) -> tuple[dict, list[str]]:
    """A whole world (a first-format world is migrated first). Returns (world, problems); the caller refuses the
    world when problems is not empty."""
    if not isinstance(raw, dict):
        return {}, ["a world must be a JSON object"]
    if needs_migration(raw):
        raw, _ = migrate_v1(raw, [])
    problems: list[str] = []
    wid = _text(raw.get("id"), 64)
    if require_id and not WORLD_ID_RE.match(wid):
        problems.append("world id must be lower-case letters, digits, _ or - (at most 64)")
    schema, probs = normalize_schema(raw.get("schema") or {"fields": []})
    problems += probs
    fids = set(field_map(schema))
    language = _text(raw.get("language"), 40) or "English"
    step = raw.get("period_step") if isinstance(raw.get("period_step"), dict) else {}
    unit = _text(step.get("unit"), 20).lower() or "quarter"
    if unit not in PERIOD_UNITS:
        problems.append("period_step.unit must be month, quarter or year")
        unit = "quarter"
    try:
        count = int(step.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    if not 1 <= count <= 120:
        problems.append("period_step.count must be between 1 and 120")
        count = max(1, min(120, count))
    actors, probs = normalize_actors(raw.get("actors") or [], fids, language, unit)
    problems += probs
    model, probs = normalize_model_settings(raw.get("model"))
    problems += probs
    synth, probs = normalize_synthesizer(raw.get("synthesizer"))
    problems += probs
    try:
        window = int(raw.get("history_window", 3))
    except (TypeError, ValueError):
        window = 3
    window = max(0, min(20, window))
    initial, probs = normalize_state(schema, raw.get("initial_state") or {}, step=0)
    problems += [f"initial state: {p}" for p in probs]
    initial["step"] = 0
    world = {"format": FORMAT, "id": wid, "name": _text(raw.get("name"), LIMITS["name"]) or "Untitled world",
             "description": _text(raw.get("description")), "language": language,
             "period_step": {"unit": unit, "count": count}, "history_window": window, "model": model,
             "schema": schema, "actors": actors, "synthesizer": synth,
             "initial_state": initial, "created": _text(raw.get("created"), 40), "updated": _text(raw.get("updated"), 40)}
    return world, problems


def blank_world(wid: str, name: str = "New world") -> dict:
    """A small valid world to start from."""
    world, problems = normalize_world({
        "id": wid, "name": name, "language": "English",
        "description": "Describe the world: what it is, its scope, the setting, the actors and what one step means.",
        "schema": {"fields": [
            {"id": "tension", "label": "Tension", "group": "General", "type": "number", "unit": "0-10",
             "description": "How tense the situation is (0 calm, 10 crisis).", "min": 0, "max": 10},
            {"id": "phase", "label": "Phase", "group": "General", "type": "enum", "options": ["calm", "dispute", "crisis"],
             "description": "The phase of the situation."}],
            "questions": [{"id": "situation", "text": "Where does the dispute stand?", "group": "General",
                           "guidance": "Who wants what, what was offered, and how close are a deal or an escalation."}]},
        "actors": [{"id": "actor_a", "name": "Actor A", "role": "Describe who this actor is.",
                    "cares_about": ["What this actor wants"],
                    "decision_questions": [{"id": "move", "text": "What do you do this period?",
                                            "guidance": "Keep the status quo, escalate or de-escalate; say how far.",
                                            "required": True}],
                    "autonomy": {"enabled": True, "max_actions": DEFAULT_MAX_ACTIONS, "guidance": ""}}],
        "synthesizer": {"instructions": "Resolve all decisions jointly into one coherent next state.",
                        "max_items_per_call": DEFAULT_ITEMS_PER_CALL},
        "initial_state": {"period": "T0", "values": {"tension": 3, "phase": "calm"},
                          "statuses": {"situation": "A quiet dispute over access rights; talks continue."},
                          "narrative": "Calm but uneasy: tension 3 of 10, talks on access rights continue."},
    })
    if problems:                                     # pragma: no cover - a fixed literal
        raise WorldError(problems)
    return world


# ---------------------------------------------------------------------------------------------------------- synthesizer plan

def chunk_by_group(items: list[dict], size: int) -> list[list[dict]]:
    """Items (fields or state questions, in schema order) in chunks of at most `size`: consecutive items of one group
    stay together when they fit; a group larger than `size` is split into balanced pieces; small consecutive groups
    are merged up to `size`. The order is kept and every item appears exactly once."""
    size = max(1, int(size or 1))
    runs: list[list[dict]] = []
    for it in items:
        if runs and runs[-1][0].get("group") == it.get("group"):
            runs[-1].append(it)
        else:
            runs.append([it])
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for run in runs:
        if len(run) > size:
            if cur:
                chunks.append(cur)
                cur = []
            n = math.ceil(len(run) / size)
            base, extra = divmod(len(run), n)
            i = 0
            for k in range(n):
                m = base + (1 if k < extra else 0)
                chunks.append(run[i:i + m])
                i += m
        elif len(cur) + len(run) <= size:
            cur += run
        else:
            chunks.append(cur)
            cur = list(run)
    if cur:
        chunks.append(cur)
    return chunks


def synth_plan(world: dict) -> list[dict]:
    """The synthesizer's calls for one iteration, in order: the digest (本季要事), the indicator chunks, the state
    question chunks, the narrative. Each step: {key, kind, label, index, total, items [ids], groups [names]}."""
    x = (world.get("synthesizer") or {}).get("max_items_per_call") or DEFAULT_ITEMS_PER_CALL
    schema = world.get("schema") or {}
    plan = [{"key": "digest", "kind": "digest", "label": "Digest", "index": 1, "total": 1, "items": [], "groups": []}]
    for kind, items, name in (("values", schema.get("fields") or [], "Indicators"),
                              ("statuses", schema.get("questions") or [], "Questions")):
        chunks = chunk_by_group(items, x)
        for i, ch in enumerate(chunks):
            groups: list[str] = []
            for it in ch:
                if it.get("group") not in groups:
                    groups.append(it.get("group"))
            plan.append({"key": f"{kind}:{i + 1}", "kind": kind, "label": f"{name} {i + 1}/{len(chunks)}",
                         "index": i + 1, "total": len(chunks), "items": [it["id"] for it in ch], "groups": groups})
    plan.append({"key": "narrative", "kind": "narrative", "label": "Narrative", "index": 1, "total": 1, "items": [],
                 "groups": []})
    return plan


# ---------------------------------------------------------------------------------------------------------- next state

def _flag(fid: str | None, level: str, code: str, message: str, question: str | None = None) -> dict:
    return {"field": fid, "question": question, "level": level, "code": code, "message": message}


def validate_values(fields: list[dict], current: dict, raw_vals: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    """The values of `fields` from `raw_vals` ({id: {value, reason}} or {id: value}) -> (values, reasons, flags,
    changes). Missing, null or unreadable values carry the current value forward (flagged); a value outside the
    plausible range is kept and flagged; a change without a reason is flagged; ids not in `fields` are flagged."""
    flags: list[dict] = []
    changes: list[dict] = []
    values: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    current = current or {}
    for field in fields:
        fid = field["id"]
        prev = current.get(fid)
        entry = raw_vals.get(fid, None)
        has = fid in raw_vals
        reason = ""
        if isinstance(entry, dict) and ("value" in entry or "reason" in entry):
            reason = _text(entry.get("reason"), 600)
            raw = entry.get("value")
        else:
            raw = entry
        if not has:
            values[fid] = prev
            flags.append(_flag(fid, "warn", "missing", "no value given: the previous value was carried forward"))
            continue
        if raw is None:
            values[fid] = prev
            if reason:
                reasons[fid] = reason
            flags.append(_flag(fid, "warn", "null", "the value was null: the previous value was carried forward"))
            continue
        val, note, ok = coerce_value(field, raw)
        if not ok:
            values[fid] = prev
            what = f"not one of the options ({', '.join(field.get('options') or [])})" if field["type"] == "enum" \
                else f"not a valid {field['type']}"
            flags.append(_flag(fid, "error", "type", f"{_text(raw, 80)!r} is {what}: the previous value was carried forward"))
            continue
        if note:
            flags.append(_flag(fid, "info", "coerced", note))
        rng = range_note(field, val)
        if rng:
            flags.append(_flag(fid, "warn", "range", f"{rng} (kept; check it)"))
        values[fid] = val
        if not values_equal(field, prev, val):
            if not reason:
                flags.append(_flag(fid, "warn", "no_reason", "the value changed without a reason"))
            reasons[fid] = reason
            changes.append({"field": fid, "label": field["label"], "from": prev, "to": val, "reason": reason})
        elif reason:
            reasons[fid] = reason
    known = {f["id"] for f in fields}
    for fid in raw_vals:
        if fid not in known:
            flags.append(_flag(fid, "warn", "unknown_field", "not a field of this call: ignored"))
    return values, reasons, flags, changes


def _bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "yes", "1", "是"):
        return True
    if isinstance(raw, str) and raw.strip().lower() in ("false", "no", "0", "否"):
        return False
    return None


def validate_statuses(questions: list[dict], current: dict, raw: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    """The status of every question in `questions` from `raw` ({id: {status, changed, reason}} or {id: text}) ->
    (statuses, reasons, flags, changes). A missing or empty status carries the current one forward (flagged); a
    change without a reason is flagged; `changed` defaults to whether the text differs; unknown ids are flagged."""
    flags: list[dict] = []
    changes: list[dict] = []
    statuses: dict[str, str] = {}
    reasons: dict[str, str] = {}
    current = current or {}
    for q in questions:
        qid = q["id"]
        prev = current.get(qid) or ""
        if qid not in raw:
            statuses[qid] = prev
            flags.append(_flag(None, "warn", "missing", "no status given: the previous status was carried forward", qid))
            continue
        entry = raw[qid]
        if isinstance(entry, dict):
            text = _text(entry.get("status"), LIMITS["status"])
            changed = _bool(entry.get("changed"))
            reason = _text(entry.get("reason"), 600)
        elif isinstance(entry, str):
            text, changed, reason = entry.strip()[:LIMITS["status"]], None, ""
        else:
            statuses[qid] = prev
            flags.append(_flag(None, "error", "type", "the status is not text: the previous status was carried forward", qid))
            continue
        if not text:
            statuses[qid] = prev
            if reason:
                reasons[qid] = reason
            flags.append(_flag(None, "warn", "empty", "the status was empty: the previous status was carried forward", qid))
            continue
        same = text == prev
        if changed is None or same:
            changed = not same
        statuses[qid] = text
        if reason:
            reasons[qid] = reason
        if changed:
            if not reason:
                flags.append(_flag(None, "warn", "no_reason", "the status changed without a reason", qid))
            changes.append({"question": qid, "text": q["text"], "from": prev, "to": text, "reason": reason})
    known = {q["id"] for q in questions}
    for qid in raw:
        if qid not in known:
            flags.append(_flag(None, "warn", "unknown_question", "not a question of this call: ignored", qid))
    return statuses, reasons, flags, changes


def validate_narrative(raw: Any, previous: str, max_chars: int | None) -> tuple[str, list[dict]]:
    """The narrative: an empty one carries the previous narrative forward; one over `max_chars` is cut (flagged)."""
    text = _text(raw) if isinstance(raw, str) else ""
    if not text:
        return previous or "", [_flag(None, "error", "narrative", "no narrative: the previous narrative was carried forward")]
    if max_chars and len(text) > max_chars:
        return text[:max_chars - 1] + "…", [_flag(None, "warn", "narrative_length",
                                                  f"the narrative had {len(text)} characters (limit {max_chars}): it was cut")]
    return text, []


def validate_next_state(schema: dict, current: dict, output: Any, *, period: str, step: int,
                        narrative_max: int | None = None) -> tuple[dict, list[dict], list[dict]]:
    """The synthesizer's (merged) answer {summary, values, statuses, narrative} checked against the schema ->
    (next state, flags, changes). Every field and every state question gets a value or a status (nothing is invented:
    what is missing or unusable is carried forward and flagged). The state also holds `status_changes`."""
    out = output if isinstance(output, dict) else {}
    flags: list[dict] = []
    current = current or {}
    fields = (schema or {}).get("fields", [])
    questions = (schema or {}).get("questions", [])
    raw_vals = out.get("values")
    if not isinstance(raw_vals, dict):
        if fields:
            flags.append(_flag(None, "error", "values", "the answer has no 'values' object: every value was carried forward"))
        raw_vals = {}
    values, reasons, f, changes = validate_values(fields, current.get("values") or {}, raw_vals)
    flags += f
    raw_st = out.get("statuses")
    if not isinstance(raw_st, dict):
        if questions:
            flags.append(_flag(None, "error", "statuses",
                               "the answer has no 'statuses' object: every status was carried forward"))
        raw_st = {}
    statuses, status_reasons, f, status_changes = validate_statuses(questions, current.get("statuses") or {}, raw_st)
    flags += f
    narrative, f = validate_narrative(out.get("narrative"), current.get("narrative") or "", narrative_max)
    flags += f
    summary = _text(out.get("summary"), LIMITS["short"])
    if not summary:
        flags.append(_flag(None, "info", "summary", "no one-line summary"))
    state = {"step": step, "period": period, "values": values, "statuses": statuses, "narrative": narrative,
             "summary": summary, "reasons": reasons, "status_reasons": status_reasons, "flags": flags,
             "changes": changes, "status_changes": status_changes}
    return state, flags, changes


# ---------------------------------------------------------------------------------------------------------- migration

def needs_migration(world: Any) -> bool:
    """Whether a world is in the first format (free-floating key questions and attributes in its state)."""
    if not isinstance(world, dict):
        return False
    if world.get("format") in LEGACY_FORMATS:
        return True
    schema = world.get("schema") if isinstance(world.get("schema"), dict) else {}
    init = world.get("initial_state") if isinstance(world.get("initial_state"), dict) else {}
    return "questions" not in schema and isinstance(init.get("text"), dict)


def _legacy_status(q: dict, zh: bool) -> str:
    ans = _text(q.get("answer"), LIMITS["status"])
    if _text(q.get("status"), 20).lower() == "answered":
        return (f"已有答案：{ans}" if zh else f"Answered: {ans}") if ans else ("已有答案。" if zh else "Answered.")
    return (f"未定。当前判断：{ans}" if zh else f"Open. Current view: {ans}") if ans else ("未定。" if zh else "Open.")


def _legacy_lists(st: dict) -> tuple[list[dict], list[dict]]:
    text = st.get("text") if isinstance(st.get("text"), dict) else {}
    qs = [q if isinstance(q, dict) else {"question": q} for q in text.get("key_questions") or []
          if isinstance(q, (dict, str))]
    attrs = text.get("attributes") or []
    if isinstance(attrs, dict):
        attrs = [{"name": k, "value": v} for k, v in attrs.items()]
    return qs, [a for a in attrs if isinstance(a, dict)]


def migrate_v1(world: dict, states: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """A world saved in the first format, and its saved states, in the current format. Its key questions and
    attributes become predefined state questions (in order of first appearance, the initial state first), each
    state's own text becoming the status at that step; the old narrative is kept. An old action space becomes one
    required decision question with the former actions as guidance (normalize_actor). Parts already in the current
    format are left alone."""
    w = copy.deepcopy(world) if isinstance(world, dict) else {}
    sts = [copy.deepcopy(s) for s in (states or []) if isinstance(s, dict)]
    zh = is_cjk(w.get("language"))
    schema = w.get("schema") if isinstance(w.get("schema"), dict) else {"fields": []}
    init = w.get("initial_state") if isinstance(w.get("initial_state"), dict) else {}
    if "questions" not in schema:
        questions: list[dict] = []
        by_q: dict[str, str] = {}
        by_a: dict[str, str] = {}
        taken = {f.get("id") for f in schema.get("fields") or [] if isinstance(f, dict)}
        for st in [init] + sts:
            qs, attrs = _legacy_lists(st)
            for q in qs:
                qt = _text(q.get("question"), LIMITS["short"])
                if qt and qt not in by_q:
                    qid = unique_id(f"kq_{len(by_q) + 1}", taken)
                    taken.add(qid)
                    by_q[qt] = qid
                    questions.append({"id": qid, "text": qt, "group": "关键问题" if zh else "Key questions", "guidance": ""})
            for a in attrs:
                name = _text(a.get("name"), LIMITS["short"])
                if name and name not in by_a:
                    qid = unique_id(f"attr_{len(by_a) + 1}", taken)
                    taken.add(qid)
                    by_a[name] = qid
                    questions.append({"id": qid, "text": name, "group": "属性" if zh else "Attributes", "guidance": ""})
        w["schema"] = {**schema, "questions": questions}
        missing = "（这一步没有记录）" if zh else "(not recorded at this step)"

        def convert(st: dict) -> dict:
            if not isinstance(st.get("text"), dict):
                return st
            qs, attrs = _legacy_lists(st)
            statuses = {q["id"]: missing for q in questions}
            for q in qs:
                qt = _text(q.get("question"), LIMITS["short"])
                if qt in by_q:
                    statuses[by_q[qt]] = _legacy_status(q, zh)
            for a in attrs:
                name = _text(a.get("name"), LIMITS["short"])
                if name in by_a:
                    statuses[by_a[name]] = _text(a.get("value"), LIMITS["status"]) or missing
            text = st.pop("text")
            st.setdefault("narrative", _text(text.get("narrative")))
            st["statuses"] = statuses
            return st
        w["initial_state"] = convert(init)
        sts = [convert(s) for s in sts]
    else:
        for st in [init] + sts:
            text = st.pop("text", None)
            if isinstance(text, dict):
                st.setdefault("narrative", _text(text.get("narrative")))
    w["format"] = FORMAT
    return w, sts
