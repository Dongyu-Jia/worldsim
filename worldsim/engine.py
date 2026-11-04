"""One iteration = one click.

(a) every enabled actor is called in parallel (at most `max_parallel` at a time) with the world description, its
    role, cares, decision questions and autonomy, private info, the current state (its visible indicators, every
    state question's status, the narrative) and the recent history; it answers every question in text and may take
    autonomous actions of its own;
(b) the synthesizer runs as several short calls, one after another (model.synth_plan): a digest (本季要事), the
    indicators in chunks of at most x, the state questions in chunks of at most x, the narrative. Every call sees the
    world description, the previous state, every actor's decisions and everything written before it, and answers only
    its own keys;
(c) the merged answer is validated against the schema (model.validate_next_state: types, enums, ranges, every field
    and question present; what is missing is carried forward and flagged; nothing invented);
(d) the next state and the iteration record (every call's prompt, raw response, usage and cost) are saved.

Every call is logged, attempt by attempt (repairs and retries included), with what the audit view shows: the
messages, the request as sent (without its messages), the model's thinking as returned, the raw response JSON, the
parsed JSON, the validator's error and notes, whether this attempt's answer was accepted, the finish reason, the
served model and provider, the generation id and the HTTP attempts (audit.py builds the view from these).

Failures: an actor call that fails is retried once, then the actor is marked "no response" and the synthesizer is
told. Bad JSON (or a required question left unanswered) gets one repair call; a required question still unanswered
is marked so. A synthesizer step that fails is retried once; if it still fails its items are carried forward
(「合成失败，沿用上期」, flagged) and the iteration is marked partial. Only when every indicator and question step fails
does the iteration fail: the state is unchanged, the actors' outputs are kept in pending.json and retry() runs the
synthesizer again with them.

Checkpoints: the iteration in flight is saved (checkpoint.json) after every actor that answers and every synthesizer
step that finishes. A server restart, a crash or a pause loses at most the calls that were running: run(resume=True)
continues it, reusing what was done. A refused key or exhausted credits (an `auth` error) pauses the iteration instead
of marking actors "no response" or carrying steps forward, so it resumes once the key works.

RunManager runs iterations in a background thread per world and keeps the live progress the UI polls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import model as M
from . import prompts as P
from .llm import CallRequest, LLMError, est_tokens, extract_json, make_backend, scrub
from .openrouter import ModelCatalog
from .settings import effective_backend, redact
from .store import Conflict, NotFound, Store, now_iso

ITERATION_FORMAT = "worldsim-iteration/2"
PENDING_FORMAT = "worldsim-pending/2"
CHECKPOINT_FORMAT = "worldsim-checkpoint/1"
REASONING_ALLOWANCE = {"none": 0, "minimal": 300, "low": 1000, "medium": 2500, "high": 6000, "xhigh": 10000,
                       "max": 16000}
ACTOR_OUT_BASE, ACTOR_OUT_PER_QUESTION, ACTOR_OUT_PER_ACTION = 350, 160, 120
SYNTH_OUT = {"digest": 900, "values": 150, "per_field": 80, "statuses": 150, "per_question": 240, "narrative": 250}


class Busy(RuntimeError):
    pass


def _step_progress(plan: list[dict]) -> list[dict]:
    return [{"key": s["key"], "kind": s["kind"], "label": s["label"], "items": len(s["items"]), "status": "waiting",
             "error": None} for s in plan]


class Progress:
    """The live state of one run, read by the UI while the run's thread updates it."""

    def __init__(self, world_id: str, mode: str, actors: list[dict], step_from: int, period: str,
                 plan: list[dict] | None = None):
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "run_id": uuid.uuid4().hex[:12], "world_id": world_id, "mode": mode, "status": "running",
            "phase": "actors" if mode == "run" else "synthesizer", "started": now_iso(), "finished": None,
            "step_from": step_from, "step": None, "period": period, "error": None, "cost": 0.0, "partial": False,
            "actors": [{"id": a["id"], "name": a["name"], "status": "queued", "attempts": 0, "error": None,
                        "summary": None} for a in actors],
            "synthesizer": {"status": "waiting", "error": None, "steps": _step_progress(plan or [])}}

    def actor(self, aid: str, **kw) -> None:
        with self._lock:
            for a in self.data["actors"]:
                if a["id"] == aid:
                    a.update(kw)

    def synth(self, **kw) -> None:
        with self._lock:
            self.data["synthesizer"].update(kw)

    def synth_step(self, key: str, **kw) -> None:
        with self._lock:
            for s in self.data["synthesizer"]["steps"]:
                if s["key"] == key:
                    s.update(kw)

    def set(self, **kw) -> None:
        with self._lock:
            self.data.update(kw)

    def add_cost(self, cost: float | None) -> None:
        if cost:
            with self._lock:
                self.data["cost"] = round(self.data["cost"] + cost, 8)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)


def world_hash(world: dict) -> str:
    """A fingerprint of the world's configuration (not its timestamps): an interrupted iteration resumes only into
    the world it started in."""
    body = {k: v for k, v in world.items() if k not in ("created", "updated")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


class Checkpoint:
    """The iteration in flight on disk (checkpoint.json), saved after every actor that answers and every synthesizer
    step that finishes. Phase "actors": the actors that answered are kept (`answered`); phase "synthesizer": every
    actor result is fixed (`results`) and the finished steps are kept (`steps`), restored as they were on resume."""

    def __init__(self, store: Store, wid: str, data: dict, calls: list[dict]):
        self.store, self.wid, self.data, self.calls = store, wid, data, calls
        self._lock = threading.Lock()

    @classmethod
    def start(cls, store: Store, wid: str, world: dict, state: dict, period: str, mode: str, calls: list[dict],
              results: list[dict] | None = None) -> "Checkpoint":
        data = {"format": CHECKPOINT_FORMAT, "world_id": wid, "mode": mode, "from_step": state["step"],
                "period": period, "world_hash": world_hash(world), "started": now_iso(), "updated": None,
                "status": "running", "error": None, "resumes": 0,
                "phase": "synthesizer" if results is not None else "actors", "answered": {}, "results": results,
                "steps": [], "round": 1}
        cp = cls(store, wid, data, calls)
        cp.save()
        return cp

    def save(self, **kw) -> None:
        with self._lock:
            self.data.update(kw)
            self.data["updated"] = now_iso()
            self.data["calls"] = list(self.calls)
            self.store.save_checkpoint(self.wid, self.data)

    def answered(self, result: dict) -> None:
        with self._lock:
            self.data["answered"][result["actor_id"]] = result
        self.save()

    def step_done(self, rec: dict) -> None:
        with self._lock:
            self.data["steps"] = [s for s in self.data["steps"] if s.get("key") != rec["key"]] + [copy.deepcopy(rec)]
        self.save()


def checkpoint_summary(data: dict | None, world: dict, latest_step: int, busy: bool) -> dict | None:
    """What the UI shows for an interrupted or paused iteration; None while it runs, or when there is none."""
    if not data or busy:
        return None
    results = data.get("results")
    enabled = [a for a in world.get("actors") or [] if a.get("enabled")]
    ok_steps = {s.get("key") for s in data.get("steps") or [] if s.get("status") == "ok"}
    usage, cost, complete = _sum_usage(data.get("calls") or [])
    return {"status": "paused" if data.get("status") == "paused" else "interrupted", "error": data.get("error"),
            "mode": data.get("mode"), "period": data.get("period"), "from_step": data.get("from_step"),
            "started": data.get("started"), "updated": data.get("updated"), "phase": data.get("phase"),
            "resumes": data.get("resumes") or 0,
            "actors_done": len(results) if results is not None else len(data.get("answered") or {}),
            "actors_total": len(results) if results is not None else len(enabled),
            "steps_done": len(ok_steps), "steps_total": len(M.synth_plan(world)), "calls": usage["calls"],
            "cost": cost, "cost_complete": complete,
            "resumable": data.get("from_step") == latest_step and data.get("world_hash") == world_hash(world)}


# ------------------------------------------------------------------------------------------------ actor answers

def validate_actor_output(world: dict, actor: dict, obj: Any, final: bool = False) -> tuple[dict | None, str | None, list[str]]:
    """(clean output, error, notes). An error means the answer is unusable and the caller repairs or retries it. A
    required decision question without an answer is an error on the first attempt; on the `final` (repair) attempt
    the answer is kept with that question marked "unanswered" (shown, and told to the synthesizer)."""
    if not isinstance(obj, dict):
        return None, "the answer is not a JSON object", []
    notes: list[str] = []
    qs = actor.get("decision_questions") or []
    ids = {q["id"] for q in qs}
    by_text = {q["text"]: q["id"] for q in qs}
    raw = obj.get("answers")
    if isinstance(raw, list):                       # tolerate [{id, value, rationale}]
        raw = {str(a.get("id") or ""): a for a in raw if isinstance(a, dict)}
    if not isinstance(raw, dict):
        raw = {}
    given: dict[str, Any] = {}
    for k, v in raw.items():
        key = k if k in ids else by_text.get(k)
        if key is None:
            notes.append(f"an answer to an unknown question {k!r} was ignored")
            continue
        given[key] = v
    answers: list[dict] = []
    missing: list[dict] = []
    for q in qs:
        entry = given.get(q["id"])
        if isinstance(entry, dict):
            value, rationale = entry.get("value", entry.get("answer")), entry.get("rationale")
        else:
            value, rationale = entry, ""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = M._text(value, M.LIMITS["answer"])
        required = bool(q.get("required", True))
        status = "ok" if value else ("unanswered" if required else "skipped")
        if status == "unanswered":
            missing.append(q)
        answers.append({"id": q["id"], "question": q["text"], "required": required, "value": value,
                        "rationale": M._text(rationale, 2000), "status": status})
    if missing and not final:
        return None, ("required decision questions have no answer: "
                      + "; ".join(f"{q['id']} ({q['text'][:60]})" for q in missing)), []
    if missing:
        notes.append(f"still no answer after one repair, marked unanswered: {', '.join(q['id'] for q in missing)}")
    auto = actor.get("autonomy") or {}
    acts_raw = obj.get("actions")
    if isinstance(acts_raw, dict):
        acts_raw = [acts_raw]
    acts: list[dict] = []
    for a in acts_raw if isinstance(acts_raw, list) else []:
        if isinstance(a, str):
            a = {"title": a}
        if not isinstance(a, dict):
            continue
        title, detail = M._text(a.get("title"), 200), M._text(a.get("detail"), 2000)
        if not (title or detail):
            continue
        acts.append({"title": title or P._cut(detail, 60), "detail": detail, "target": M._text(a.get("target"), 200),
                     "intended_effect": M._text(a.get("intended_effect") or a.get("effect"), 600)})
    if acts and not auto.get("enabled"):
        notes.append(f"autonomous actions are off for this actor: {len(acts)} action(s) ignored")
        acts = []
    limit = int(auto.get("max_actions") or M.DEFAULT_MAX_ACTIONS)
    if len(acts) > limit:
        notes.append(f"{len(acts)} autonomous actions given, at most {limit}: the first {limit} were kept")
        acts = acts[:limit]
    cons = obj.get("expected_consequences")
    if isinstance(cons, str):
        cons = [cons]
    cons = [M._text(c, 600) for c in (cons if isinstance(cons, list) else []) if M._text(c, 600)][:8]
    return ({"answers": answers, "actions": acts, "public_statement": M._text(obj.get("public_statement"), 2000),
             "rationale": M._text(obj.get("rationale"), 4000), "expected_consequences": cons}, None, notes)


# ------------------------------------------------------------------------------------------------ synthesizer steps

def validate_step_output(world: dict, step: dict, obj: Any, final: bool = False) -> tuple[dict | None, str | None]:
    """One synthesizer step's answer, reduced to its own keys (extra keys are dropped and listed in "dropped").
    Missing items, an empty digest or status, or a narrative over the limit are errors on the first attempt (the
    caller repairs); on the `final` attempt what is there is kept and validate_next_state flags the rest."""
    if not isinstance(obj, dict):
        return None, "the answer is not a JSON object"
    kind = step["kind"]
    if kind == "digest":
        digest = M._text(obj.get("digest"), 8000)
        if not digest and not final:
            return None, "the digest is empty"
        threads = obj.get("threads")
        if isinstance(threads, str):
            threads = [threads]
        threads = [M._text(t, 300) for t in (threads if isinstance(threads, list) else []) if M._text(t, 300)][:8]
        return {"summary": M._text(obj.get("summary"), M.LIMITS["short"]), "digest": digest, "threads": threads}, None
    if kind in ("values", "statuses"):
        raw = obj.get(kind)
        if not isinstance(raw, dict):
            if any(k in obj for k in step["items"]):          # a bare {id: …} object
                raw = {k: v for k, v in obj.items() if k != kind}
            else:
                return None, f"the answer has no '{kind}' object"
        items = step["items"]
        missing = [k for k in items if k not in raw]
        if kind == "statuses":
            missing += [k for k in items if k in raw and not (
                M._text(raw[k].get("status") if isinstance(raw[k], dict) else raw[k], 10))]
        if missing and not final:
            what = "values" if kind == "values" else "non-empty statuses"
            return None, f"{what} are missing for: {', '.join(missing)} (write exactly: {', '.join(items)})"
        return {kind: {k: raw[k] for k in items if k in raw}, "dropped": [k for k in raw if k not in items]}, None
    text = M._text(obj.get("narrative")) if isinstance(obj.get("narrative"), str) else ""
    target, mx = M.narrative_limits(world.get("language"))
    if not final:
        if not text:
            return None, "the narrative is empty"
        if len(text) > mx:
            return None, f"the narrative has {len(text)} characters: it must be {target}, at most {mx} characters"
    return {"narrative": text}, None


def _sum_usage(calls: list[dict]) -> tuple[dict, float, bool]:
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "calls": len(calls)}
    cost, complete = 0.0, True
    for c in calls:
        u = c.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens"):
            if isinstance(u.get(k), (int, float)):
                tot[k] += int(u[k])
        if c.get("error") and not u:
            continue                                  # a call that failed before an answer: no usage, no cost
        if isinstance(c.get("cost"), (int, float)):
            cost += float(c["cost"])
        else:
            complete = False
    return tot, round(cost, 8), complete


class Engine:
    def __init__(self, store: Store, settings_loader: Callable[[], dict], catalog: ModelCatalog | None = None,
                 backend_factory: Callable[[dict], Any] | None = None):
        self.store = store
        self.settings_loader = settings_loader
        self.catalog = catalog or ModelCatalog(store.root)
        self.backend_factory = backend_factory or (lambda s: make_backend(s, catalog=self.catalog))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def model_for(world: dict, settings: dict, override: str = "") -> str:
        return override or (world.get("model") or {}).get("model") or settings.get("default_model")

    @staticmethod
    def params(world: dict, settings: dict) -> dict:
        wm = world.get("model") or {}
        temp = wm.get("temperature")
        return {"temperature": settings.get("temperature") if temp is None else temp,
                "reasoning_effort": wm.get("reasoning_effort") or settings.get("reasoning_effort") or "",
                "timeout_s": settings.get("timeout_s") or 240, "max_tokens": settings.get("max_tokens"),
                "record_thinking": settings.get("record_thinking", True) is not False}

    def history(self, wid: str, world: dict, upto: int) -> list[dict]:
        """The last `history_window` steps up to `upto`: period, summary, narrative, the digest and the decisions
        that led to it."""
        window = int(world.get("history_window", 3) or 0)
        if window <= 0:
            return []
        out = []
        for n in range(max(0, upto - window + 1), upto + 1):
            try:
                st = self.store.load_step(wid, n)
            except NotFound:
                continue
            entry = {"step": n, "period": st.get("period"), "summary": st.get("summary") or "",
                     "narrative": st.get("narrative") or (st.get("text") or {}).get("narrative") or "",
                     "digest": "", "decisions": []}
            if n > 0:
                try:
                    it = self.store.load_iteration(wid, n)
                    entry["digest"] = (((it.get("synthesizer") or {}).get("digest")) or {}).get("digest") or ""
                    entry["decisions"] = [{"actor_id": r.get("actor_id"), "name": r.get("name"),
                                           "status": r.get("status"), "output": r.get("output")}
                                          for r in it.get("actors") or []]
                except NotFound:
                    pass
            out.append(entry)
        return out

    def _call(self, backend, req: CallRequest, calls: list[dict], lock: threading.Lock, who: str, attempt: int,
              progress: Progress | None, secrets: list[str], step_key: str | None = None) -> tuple[Any, str | None]:
        """One request (+ one repair on an unusable answer) -> (parsed object | None, error | None). Every call is
        logged with its audit fields. The validator gets final=True on the repair."""
        validator = req.context.get("_validator")
        notes_box = req.context.get("_notes")
        text, err = "", None
        for repair in (False, True):
            r = req if not repair else CallRequest(**{**req.__dict__, "messages": P.repair_messages(req.messages, text, err),
                                                      "repair": True})
            entry = {"who": who, "purpose": req.purpose, "step": step_key, "attempt": attempt, "repair": repair,
                     "model": req.model, "started": now_iso(), "messages": r.messages, "response_format": None,
                     "response_text": None, "usage": None, "cost": None, "cost_source": None, "error": None,
                     "duration_s": None, "schema_name": req.schema_name, "request": None, "request_truncated": False,
                     "thinking": None, "raw_response": None, "raw_response_truncated": False, "http_attempts": [],
                     "finish_reason": None, "native_finish_reason": None, "provider": None, "generation_id": None,
                     "parsed": None, "validation_error": None, "notes": [], "accepted": False}
            t0 = time.monotonic()
            try:
                comp = backend.complete(r)
            except LLMError as e:
                entry.update(error=redact(str(e), secrets), duration_s=round(time.monotonic() - t0, 3),
                             **self._audit_fields(getattr(e, "audit", None), secrets))
                with lock:
                    calls.append(entry)
                if repair:
                    return None, f"the repair call failed: {entry['error']}"
                raise
            entry.update(response_format=comp.response_format, response_text=comp.text, usage=comp.usage,
                         cost=comp.cost, cost_source=comp.meta.get("cost_source"), served_model=comp.model,
                         duration_s=round(time.monotonic() - t0, 3),
                         finish_reason=comp.meta.get("finish_reason"),
                         native_finish_reason=comp.meta.get("native_finish_reason"),
                         provider=comp.meta.get("provider"), generation_id=comp.meta.get("id"),
                         **self._audit_fields({**(comp.audit or {}), "thinking": comp.thinking or None}, secrets))
            if progress:
                progress.add_cost(comp.cost)
            text = comp.text
            obj, err = extract_json(comp.text)
            entry["parsed"] = copy.deepcopy(obj)
            if obj is not None and validator:
                if notes_box is not None:
                    notes_box[:] = []
                obj, err = validator(obj, repair)
                if notes_box:
                    entry["notes"] = list(notes_box)
                if isinstance(obj, dict) and obj.get("dropped"):
                    entry["notes"] = entry["notes"] + ["keys outside this step were dropped: "
                                                       + ", ".join(map(str, obj["dropped"][:12]))]
            entry["validation_error"] = err
            entry["accepted"] = not err
            if err:
                entry["error"] = f"unusable answer: {err}"
            with lock:
                calls.append(entry)
            if not err:
                return obj, None
            if not repair and progress and step_key:
                progress.synth_step(step_key, status="repairing")
        return None, f"bad answer after one repair: {err}"

    @staticmethod
    def _audit_fields(audit: dict | None, secrets: list[str]) -> dict:
        """The audit part of a call entry from a Completion's (or an LLMError's) audit record, redacted again."""
        a = audit or {}
        keep = ("request", "request_truncated", "thinking", "raw_response", "raw_response_truncated", "http_attempts")
        return {k: scrub(a[k], secrets) for k in keep if k in a and a[k] is not None}

    # ------------------------------------------------------------------ actors
    def _run_actor(self, backend, world: dict, settings: dict, actor: dict, state: dict, history: list[dict],
                   next_period: str, calls: list[dict], lock: threading.Lock, progress: Progress | None,
                   stop: threading.Event | None = None) -> dict:
        model = self.model_for(world, settings, actor.get("model") or "")
        if stop is not None and stop.is_set():          # the run is pausing: not called, resumed later
            return {"actor_id": actor["id"], "name": actor["name"], "status": "not_run", "model": model,
                    "attempts": 0, "output": None, "notes": [], "error": None}
        params = self.params(world, settings)
        notes_box: list[str] = []

        def validator(obj, final):
            clean, err, notes = validate_actor_output(world, actor, obj, final)
            notes_box[:] = notes
            return clean, err

        peers = [a["name"] for a in world["actors"] if a.get("enabled") and a["id"] != actor["id"]]
        ctx = {"world": {"id": world["id"], "name": world["name"], "language": world["language"]},
               "actor": actor, "state": state, "peers": peers, "_validator": validator, "_notes": notes_box}
        req = CallRequest(purpose="actor", model=model, schema_name=P.ACTOR_SCHEMA_NAME,
                          messages=P.actor_messages(world, actor, state, history, next_period),
                          json_schema=P.actor_schema(actor), context=ctx, **params)
        secrets = [settings.get("api_key") or ""]
        error = None
        attempts = 0
        for attempt in (1, 2):
            attempts = attempt
            if progress:
                progress.actor(actor["id"], status="running" if attempt == 1 else "retrying", attempts=attempt)
            try:
                out, error = self._call(backend, req, calls, lock, actor["id"], attempt, progress, secrets)
            except LLMError as e:
                out, error = None, redact(str(e), secrets)
                if not e.retryable:
                    if e.kind == "auth" and stop is not None:
                        stop.set()                  # a refused key or no credits: pause, never "no response"
                        if progress:
                            progress.actor(actor["id"], status="failed", error=error)
                        return {"actor_id": actor["id"], "name": actor["name"], "status": "paused", "model": model,
                                "attempts": attempt, "output": None, "notes": [], "error": error}
                    break
            if out is not None:
                if progress:
                    progress.actor(actor["id"], status="done", summary=P.output_brief(out), error=None)
                return {"actor_id": actor["id"], "name": actor["name"], "status": "ok", "model": model,
                        "attempts": attempt, "output": out, "notes": list(notes_box), "error": None}
        if progress:
            progress.actor(actor["id"], status="failed", error=error)
        return {"actor_id": actor["id"], "name": actor["name"], "status": "no_response", "model": model,
                "attempts": attempts, "output": None, "notes": [], "error": error}

    def run_actors(self, backend, world: dict, settings: dict, state: dict, history: list[dict], next_period: str,
                   calls: list[dict], progress: Progress | None, answered: dict | None = None,
                   on_answer: Callable[[dict], None] | None = None) -> tuple[list[dict], str | None]:
        """Every enabled actor in parallel -> (results in world order, the pause error or None). `answered`: actors
        that already answered in an interrupted run (reused, not called again); `on_answer(result)` gets each new
        answer as it arrives (the checkpoint). A refused key or exhausted credits pauses: actors not started yet are
        skipped and the error comes back."""
        actors = [a for a in world["actors"] if a.get("enabled")]
        answered = answered or {}
        lock = threading.Lock()
        stop = threading.Event()
        for a in actors:
            r = answered.get(a["id"])
            if r and progress:
                progress.actor(a["id"], status="done", summary=P.output_brief(r.get("output")),
                               attempts=r.get("attempts", 1), error=None)
        todo = [a for a in actors if a["id"] not in answered]
        fresh: dict[str, dict] = {}
        if todo:
            workers = max(1, min(int(settings.get("max_parallel") or 6), len(todo)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="worldsim-actor") as ex:
                futures = {ex.submit(self._run_actor, backend, world, settings, a, state, history, next_period, calls,
                                     lock, progress, stop): a for a in todo}
                for f in as_completed(futures):
                    a = futures[f]
                    try:
                        r = f.result()
                    except Exception as e:  # noqa: BLE001 - one actor's bug must not stop the iteration
                        r = {"actor_id": a["id"], "name": a["name"], "status": "no_response", "attempts": 1,
                             "output": None, "notes": [], "model": None,
                             "error": redact(f"internal error: {type(e).__name__}: {e}", [settings.get("api_key") or ""])}
                    fresh[a["id"]] = r
                    if r.get("status") == "ok" and on_answer:
                        on_answer(r)
        paused = next((r["error"] for r in fresh.values() if r.get("status") == "paused"), None)
        return [answered.get(a["id"]) or fresh[a["id"]] for a in actors], paused

    # ------------------------------------------------------------------ synthesizer
    def _run_step(self, backend, world: dict, step: dict, plan: list[dict], state: dict, results: list[dict],
                  history: list[dict], next_period: str, digest: dict | None, written: dict, model: str, params: dict,
                  calls: list[dict], lock: threading.Lock, progress: Progress | None,
                  secrets: list[str]) -> tuple[dict | None, str | None, int, bool]:
        """One synthesizer step, retried once -> (clean output | None, error, attempts, fatal)."""
        def validator(obj, final):
            return validate_step_output(world, step, obj, final)

        ctx = {"world": {"id": world["id"], "name": world["name"], "language": world["language"]},
               "schema": world["schema"], "state": state, "actor_results": results, "next_period": next_period,
               "step": step, "digest": digest, "written": copy.deepcopy(written),
               "narrative_max": M.narrative_limits(world.get("language"))[1], "_validator": validator}
        req = CallRequest(purpose="synth", model=model, schema_name=P.SYNTH_SCHEMA_NAMES[step["kind"]],
                          messages=P.synth_step_messages(world, step, plan, state, results, history, next_period,
                                                         digest, written),
                          json_schema=P.step_schema(world, step), context=ctx, **params)
        err = None
        for attempt in (1, 2):
            if progress:
                progress.synth_step(step["key"], status="running" if attempt == 1 else "retrying")
            try:
                out, err = self._call(backend, req, calls, lock, f"synthesizer:{step['key']}", attempt, progress,
                                      secrets, step_key=step["key"])
            except LLMError as e:
                out, err = None, redact(str(e), secrets)
                if not e.retryable:
                    return None, err, attempt, e.kind == "auth"     # auth pauses the run; others fail this step
            if out is not None:
                return out, None, attempt, False
        return None, err, 2, False

    def run_synth(self, backend, world: dict, settings: dict, state: dict, results: list[dict], history: list[dict],
                  next_period: str, calls: list[dict], progress: Progress | None, cp: "Checkpoint | None" = None) -> dict:
        """Every synthesizer step in order -> {output (merged), digest, steps, flags, partial, model, error}. output is
        None when the iteration fails outright (every indicator and question step failed, or a call failed for good)."""
        model = self.model_for(world, settings, (world.get("synthesizer") or {}).get("model") or "")
        params = self.params(world, settings)
        plan = M.synth_plan(world)
        lang = world.get("language")
        failed_reason = M.synth_failed_reason(lang)
        fmap, qmap = M.field_map(world["schema"]), M.question_map(world["schema"])
        prev_vals, prev_sts = state.get("values") or {}, state.get("statuses") or {}
        if progress:
            progress.set(phase="synthesizer")
            progress.synth(status="running", error=None)
        secrets = [settings.get("api_key") or ""]
        lock = threading.Lock()
        digest: dict | None = None
        written: dict = {"values": {}, "reasons": {}, "statuses": {}, "status_changed": {}, "status_reasons": {}}
        merged: dict = {"summary": "", "values": {}, "statuses": {}, "narrative": None}
        steps: list[dict] = []
        flags: list[dict] = []
        paused = None
        saved = {s.get("key"): s for s in ((cp.data.get("steps") if cp else None) or []) if s.get("status") == "ok"}
        for step in plan:
            rec = {"key": step["key"], "kind": step["kind"], "label": step["label"], "items": list(step["items"]),
                   "groups": list(step["groups"]), "status": "ok", "attempts": 0, "error": None, "output": None,
                   "dropped": []}
            old = saved.get(step["key"])
            restored = bool(old and old.get("items") == rec["items"] and isinstance(old.get("output"), dict))
            if restored:                                # finished before an interruption: reused as it was
                out, err, attempts = copy.deepcopy(old["output"]), None, old.get("attempts", 1)
                out["dropped"] = list(old.get("dropped") or [])
            else:
                out, err, attempts, fatal = self._run_step(backend, world, step, plan, state, results, history,
                                                           next_period, digest, written, model, params, calls, lock,
                                                           progress, secrets)
                if fatal:                               # a refused key or no credits: pause, resume later
                    paused = err
                    if progress:
                        progress.synth_step(step["key"], status="failed", error=err)
                    break
            rec["attempts"] = attempts
            kind = step["kind"]
            if out is None:
                rec.update(status="failed", error=err)
                msg = f"{step['label']} failed ({P._cut(err, 160)}): carried forward from the previous step"
                if kind == "values":
                    for fid in step["items"]:
                        merged["values"][fid] = {"value": prev_vals.get(fid), "reason": failed_reason}
                        written["values"][fid] = prev_vals.get(fid)
                        written["reasons"][fid] = failed_reason
                        flags.append(M._flag(fid, "error", "synth_failed", msg))
                elif kind == "statuses":
                    for qid in step["items"]:
                        merged["statuses"][qid] = {"status": prev_sts.get(qid) or "", "changed": False,
                                                   "reason": failed_reason}
                        written["statuses"][qid] = prev_sts.get(qid) or ""
                        written["status_reasons"][qid] = failed_reason
                        flags.append(M._flag(None, "error", "synth_failed", msg, qid))
                else:
                    if kind == "digest":
                        digest = {}
                    flags.append(M._flag(None, "error", "synth_failed", msg))
            else:
                rec["dropped"] = out.pop("dropped", [])
                rec["output"] = out
                if rec["dropped"]:
                    flags.append(M._flag(None, "info", "dropped_keys",
                                         f"{step['label']}: keys outside this step were dropped: "
                                         + ", ".join(map(str, rec["dropped"][:12]))))
                if kind == "digest":
                    digest = out
                    merged["summary"] = out.get("summary") or ""
                elif kind == "values":
                    merged["values"].update(out["values"])
                    vals, reasons, _, _ = M.validate_values([fmap[i] for i in step["items"]], prev_vals, out["values"])
                    written["values"].update(vals)
                    written["reasons"].update(reasons)
                elif kind == "statuses":
                    merged["statuses"].update(out["statuses"])
                    sts, reasons, _, changes = M.validate_statuses([qmap[i] for i in step["items"]], prev_sts,
                                                                   out["statuses"])
                    written["statuses"].update(sts)
                    written["status_reasons"].update(reasons)
                    changed = {c["question"] for c in changes}
                    written["status_changed"].update({q: q in changed for q in sts})
                else:
                    merged["narrative"] = out.get("narrative")
            steps.append(rec)
            if cp is not None and not restored:
                cp.step_done(rec)
            if progress:
                progress.synth_step(step["key"], status="done" if out is not None else "failed",
                                    error=None if out is not None else err)
        if paused:
            if progress:
                progress.synth(status="paused", error=paused)
            return {"output": None, "paused": paused, "digest": digest, "steps": steps, "flags": flags,
                    "partial": False, "model": model, "error": paused, "plan": plan}
        item_steps = [s for s in steps if s["kind"] in ("values", "statuses")] or steps
        partial = any(s["status"] == "failed" for s in steps)
        failed_all = all(s["status"] == "failed" for s in item_steps)
        error = None
        if failed_all:
            error = next((s["error"] for s in item_steps if s["error"]), "every synthesizer step failed")
        if progress:
            progress.synth(status="failed" if error else ("partial" if partial else "done"), error=error)
        return {"output": None if error else merged, "digest": digest, "steps": steps, "flags": flags,
                "partial": partial, "model": model, "error": error, "plan": plan}

    # ------------------------------------------------------------------ an iteration
    def _prepare(self, wid: str) -> tuple[dict, dict, dict, str, list[dict]]:
        world = self.store.load_world(wid)
        settings = self.settings_loader()
        state = self.store.latest_step(wid)
        unit, count = world["period_step"]["unit"], world["period_step"]["count"]
        next_period = M.advance_period(state.get("period"), unit, count)
        history = self.history(wid, world, int(state["step"]))
        return world, settings, state, next_period, history

    def resumable(self, wid: str, world: dict, state: dict) -> dict:
        """The interrupted iteration's checkpoint, if it can resume into the world as it is now."""
        data = self.store.load_checkpoint(wid)
        if not data:
            raise NotFound("there is no interrupted iteration to resume")
        if data.get("from_step") != state["step"]:
            raise Conflict("the interrupted iteration no longer follows the latest step: discard it")
        if data.get("world_hash") != world_hash(world):
            raise Conflict("the world was edited after the interrupted iteration started: discard it and run a new one")
        return data

    def run(self, wid: str, progress: Progress | None = None, resume: bool = False) -> dict:
        """One iteration: every actor, then the synthesizer, checkpointed as it goes. With `resume`, the interrupted
        iteration continues: the actors that answered and the steps that finished are reused, the rest are called."""
        world, settings, state, next_period, history = self._prepare(wid)
        backend = self.backend_factory(settings)
        if resume:
            data = self.resumable(wid, world, state)
            calls = list(data.get("calls") or [])
            cp = Checkpoint(self.store, wid, data, calls)
            cp.save(status="running", error=None, resumes=int(data.get("resumes") or 0) + 1)
        else:
            if self.store.load_checkpoint(wid):
                raise Conflict("an interrupted iteration is waiting: resume it or discard it first")
            self.store.clear_pending(wid)
            calls = []
            cp = Checkpoint.start(self.store, wid, world, state, next_period, "run", calls)
        results = cp.data.get("results")
        if results is None:
            results, paused = self.run_actors(backend, world, settings, state, history, next_period, calls, progress,
                                              answered=cp.data.get("answered") or {}, on_answer=cp.answered)
            if paused:
                return self._pause(cp, paused, state, calls)
            cp.save(phase="synthesizer", results=results)
        elif progress:
            for r in results:
                progress.actor(r["actor_id"], status="done" if r.get("status") == "ok" else "failed",
                               summary=P.output_brief(r.get("output")), error=r.get("error"),
                               attempts=r.get("attempts", 0))
        return self._finish(wid, world, settings, backend, state, results, history, next_period, calls,
                            cp.data.get("started") or now_iso(), progress, round_no=int(cp.data.get("round") or 1),
                            cp=cp)

    @staticmethod
    def _pause(cp: "Checkpoint", error: str, state: dict, calls: list[dict]) -> dict:
        cp.save(status="paused", error=error)
        usage, cost, _ = _sum_usage(calls)
        return {"status": "paused", "error": error, "step": state["step"], "cost": cost, "usage": usage}

    def retry(self, wid: str, progress: Progress | None = None) -> dict:
        pending = self.store.load_pending(wid)
        if not pending:
            raise NotFound("there is no failed synthesizer attempt to retry")
        world, settings, state, next_period, history = self._prepare(wid)
        if pending.get("from_step") != state["step"]:
            self.store.clear_pending(wid)
            raise Conflict("the failed attempt no longer matches the current step: run a new iteration")
        backend = self.backend_factory(settings)
        calls = list(pending.get("calls") or [])
        for c in calls:
            c.setdefault("round", 1)               # a call of an earlier, failed synthesizer run
        round_no = max((int(c.get("round") or 1) for c in calls), default=0) + 1
        if self.store.load_checkpoint(wid):
            raise Conflict("an interrupted iteration is waiting: resume it or discard it first")
        cp = Checkpoint.start(self.store, wid, world, state, next_period, "retry", calls,
                              results=pending.get("actors") or [])
        cp.save(round=round_no, started=pending.get("started") or cp.data["started"])
        return self._finish(wid, world, settings, backend, state, pending.get("actors") or [], history, next_period,
                            calls, pending.get("started") or now_iso(), progress, round_no=round_no, cp=cp)

    def _finish(self, wid, world, settings, backend, state, results, history, next_period, calls, started, progress,
                round_no: int = 1, cp: "Checkpoint | None" = None):
        """`round_no`: 1 for a run, 2+ for a retry of a failed synthesizer (each call is marked with its round)."""
        res = self.run_synth(backend, world, settings, state, results, history, next_period, calls, progress, cp=cp)
        for c in calls:
            c.setdefault("round", round_no)
        if res.get("paused") and cp is not None:
            return self._pause(cp, res["paused"], state, calls)
        usage, cost, complete = _sum_usage(calls)
        if res["output"] is None:
            pending = {"format": PENDING_FORMAT, "world_id": wid, "from_step": state["step"], "period": next_period,
                       "started": started, "failed_at": now_iso(), "error": res["error"], "actors": results,
                       "calls": calls, "synthesizer_model": res["model"], "steps": res["steps"], "usage": usage,
                       "cost": cost, "cost_complete": complete, "backend": getattr(backend, "name", "?"),
                       "rounds": round_no}
            self.store.save_pending(wid, pending)
            self.store.clear_checkpoint(wid)
            return {"status": "failed", "error": res["error"], "step": state["step"], "cost": cost, "usage": usage}
        n = int(state["step"]) + 1
        nxt, flags, changes = M.validate_next_state(world["schema"], state, res["output"], period=next_period, step=n,
                                                    narrative_max=M.narrative_limits(world.get("language"))[1])
        flags = res["flags"] + flags
        nxt["flags"] = flags
        nxt["partial"] = res["partial"]
        nxt["created"] = now_iso()
        record = {"format": ITERATION_FORMAT, "world_id": wid, "step": n, "from_step": state["step"],
                  "period_from": state.get("period"), "period": next_period, "started": started, "finished": now_iso(),
                  "backend": getattr(backend, "name", "?"), "actors": results,
                  "synthesizer": {"status": "partial" if res["partial"] else "ok", "model": res["model"],
                                  "max_items_per_call": world["synthesizer"]["max_items_per_call"],
                                  "digest": res["digest"], "steps": res["steps"], "output": res["output"]},
                  "partial": res["partial"], "flags": flags, "changes": changes,
                  "status_changes": nxt["status_changes"], "usage": usage, "cost": cost, "cost_complete": complete,
                  "rounds": round_no, "calls": calls}
        nxt["usage"], nxt["cost"], nxt["cost_complete"] = usage, cost, complete
        self.store.append(wid, nxt, record)
        return {"status": "done", "step": n, "period": next_period, "cost": cost, "usage": usage, "partial": res["partial"],
                "flags": len(flags), "changes": len(changes), "status_changes": len(nxt["status_changes"])}

    # ------------------------------------------------------------------ estimate
    def _allowance(self, effort: str, info: dict | None) -> int:
        if effort:
            return REASONING_ALLOWANCE.get(effort, 2500)
        if info and info.get("reasoning") and (info.get("reasoning_mandatory") or info.get("default_effort")):
            return REASONING_ALLOWANCE.get(info.get("default_effort") or "medium", 2500)
        return 0

    @staticmethod
    def call_counts(world: dict) -> dict:
        plan = M.synth_plan(world)
        actors = sum(1 for a in world["actors"] if a.get("enabled"))
        by = {k: sum(1 for s in plan if s["kind"] == k) for k in ("digest", "values", "statuses", "narrative")}
        return {"actors": actors, "synthesizer": len(plan), "total": actors + len(plan), **{f"synth_{k}": v for k, v in by.items()}}

    def estimate(self, wid: str) -> dict:
        """A rough cost of the next iteration: the real prompts' token counts (estimated from their length, with
        placeholder answers) and assumed answer lengths (plus a reasoning allowance), priced with the model list."""
        world, settings, state, next_period, history = self._prepare(wid)
        params = self.params(world, settings)
        rows: list[dict] = []
        actors = [a for a in world["actors"] if a.get("enabled")]
        placeholder = []
        for a in actors:
            model = self.model_for(world, settings, a.get("model") or "")
            msgs = P.actor_messages(world, a, state, history, next_period)
            info = self.catalog.lookup(model)
            qs = a.get("decision_questions") or []
            auto = a.get("autonomy") or {}
            n_act = min(2, int(auto.get("max_actions") or 0)) if auto.get("enabled") else 0
            rows.append({"who": a["name"], "model": model, "prompt_tokens": est_tokens("".join(m["content"] for m in msgs)),
                         "completion_tokens": ACTOR_OUT_BASE + ACTOR_OUT_PER_QUESTION * len(qs)
                         + ACTOR_OUT_PER_ACTION * n_act + self._allowance(params["reasoning_effort"], info),
                         "info": info})
            placeholder.append({"actor_id": a["id"], "name": a["name"], "status": "ok", "output": {
                "answers": [{"id": q["id"], "question": q["text"], "value": "x" * 160, "rationale": "x" * 120,
                             "status": "ok", "required": True} for q in qs],
                "actions": [{"title": "x" * 30, "detail": "x" * 160, "target": "x" * 20, "intended_effect": "x" * 60}] * n_act,
                "public_statement": "x" * 150, "rationale": "x" * 300, "expected_consequences": ["x" * 80] * 2}})
        smodel = self.model_for(world, settings, (world.get("synthesizer") or {}).get("model") or "")
        sinfo = self.catalog.lookup(smodel)
        plan = M.synth_plan(world)
        digest = {"summary": "x" * 60, "digest": "x" * 600, "threads": ["x" * 60] * 4}
        written: dict = {"values": {}, "reasons": {}, "statuses": {}, "status_changed": {}, "status_reasons": {}}
        allowance = self._allowance(params["reasoning_effort"], sinfo)
        for step in plan:
            msgs = P.synth_step_messages(world, step, plan, state, placeholder, history, next_period,
                                         None if step["kind"] == "digest" else digest, written)
            k = len(step["items"])
            out = {"digest": SYNTH_OUT["digest"], "narrative": SYNTH_OUT["narrative"],
                   "values": SYNTH_OUT["values"] + SYNTH_OUT["per_field"] * k,
                   "statuses": SYNTH_OUT["statuses"] + SYNTH_OUT["per_question"] * k}[step["kind"]]
            rows.append({"who": f"synthesizer · {step['label']}", "model": smodel,
                         "prompt_tokens": est_tokens("".join(m["content"] for m in msgs)),
                         "completion_tokens": out + allowance, "info": sinfo})
            for i in step["items"]:
                if step["kind"] == "values":
                    written["values"][i] = (state.get("values") or {}).get(i)
                    written["reasons"][i] = "x" * 60
                elif step["kind"] == "statuses":
                    written["statuses"][i] = "x" * 160
                    written["status_reasons"][i] = "x" * 60
        total, priced = 0.0, True
        for r in rows:
            info = r.pop("info")
            if info and info.get("prompt_price") is not None and info.get("completion_price") is not None:
                r["cost"] = r["prompt_tokens"] * info["prompt_price"] + r["completion_tokens"] * info["completion_price"]
                total += r["cost"]
            else:
                r["cost"] = None
                priced = False
        return {"calls": len(rows), "counts": self.call_counts(world), "rows": rows, "next_period": next_period,
                "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
                "completion_tokens": sum(r["completion_tokens"] for r in rows),
                "cost": round(total, 6) if rows else 0.0, "priced": priced,
                "backend": effective_backend(settings),
                "assumptions": (f"prompt tokens estimated from the prompts' length; answers assumed ~{ACTOR_OUT_BASE} + "
                                f"{ACTOR_OUT_PER_QUESTION}/question + {ACTOR_OUT_PER_ACTION}/action tokens per actor; "
                                f"synthesizer steps ~{SYNTH_OUT['digest']} (digest), {SYNTH_OUT['per_field']}/indicator, "
                                f"{SYNTH_OUT['per_question']}/question, {SYNTH_OUT['narrative']} (narrative), plus a "
                                "reasoning allowance for reasoning models; repairs and retries not included")}


class RunManager:
    """At most one iteration per world at a time, each in its own thread; progress kept in memory."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._runs: dict[str, Progress] = {}
        self._threads: dict[str, threading.Thread] = {}

    def busy(self, wid: str) -> bool:
        with self._lock:
            t = self._threads.get(wid)
            return bool(t and t.is_alive())

    def start(self, wid: str, mode: str = "run") -> dict:
        world = self.engine.store.load_world(wid)
        with self._lock:
            t = self._threads.get(wid)
            if t and t.is_alive():
                raise Busy("an iteration is already running for this world")
            state = self.engine.store.latest_step(wid)
            pending = None
            cp = None
            if mode == "resume":
                cp = self.engine.resumable(wid, world, state)
                fixed = cp.get("results")
                actors = ([{"id": r["actor_id"], "name": r["name"]} for r in fixed] if fixed is not None
                          else [a for a in world["actors"] if a.get("enabled")])
            elif self.engine.store.load_checkpoint(wid):
                raise Conflict("an interrupted iteration is waiting: resume it or discard it first")
            elif mode == "retry":
                pending = self.engine.store.load_pending(wid)
                if not pending:
                    raise NotFound("there is no failed synthesizer attempt to retry")
                actors = [{"id": r["actor_id"], "name": r["name"]} for r in pending.get("actors") or []]
            else:
                actors = [a for a in world["actors"] if a.get("enabled")]
            period = M.advance_period(state.get("period"), world["period_step"]["unit"], world["period_step"]["count"])
            progress = Progress(wid, mode, actors, int(state["step"]), period, M.synth_plan(world))
            if cp is not None:
                progress.set(phase="synthesizer" if cp.get("results") is not None else "actors", resumed=True)
                for s_ in cp.get("steps") or []:
                    if s_.get("status") == "ok":
                        progress.synth_step(s_.get("key"), status="done")
            for r in (pending or {}).get("actors") or []:
                progress.actor(r["actor_id"], status="done" if r.get("status") == "ok" else "failed",
                               summary=P.output_brief(r.get("output")), error=r.get("error"),
                               attempts=r.get("attempts", 0))
            self._runs[wid] = progress
            thread = threading.Thread(target=self._work, args=(wid, mode, progress), daemon=True,
                                      name=f"worldsim-run-{wid}")
            self._threads[wid] = thread
            thread.start()
        return progress.snapshot()

    def _work(self, wid: str, mode: str, progress: Progress) -> None:
        try:
            if mode == "retry":
                result = self.engine.retry(wid, progress)
            else:
                result = self.engine.run(wid, progress, resume=mode == "resume")
            if result["status"] == "done":
                progress.set(status="done", phase="done", step=result["step"], finished=now_iso(), cost=result["cost"],
                             partial=bool(result.get("partial")))
            elif result["status"] == "paused":
                progress.set(status="paused", error=result.get("error"), finished=now_iso(),
                             cost=result.get("cost", 0.0))
            else:
                progress.set(status="failed", phase="synthesizer", error=result.get("error"), finished=now_iso(),
                             cost=result.get("cost", 0.0))
        except Exception as e:  # noqa: BLE001 - shown in the UI, never raised into the server
            secrets = [self.engine.settings_loader().get("api_key") or ""]
            progress.set(status="failed", error=redact(f"{type(e).__name__}: {e}", secrets), finished=now_iso())

    def status(self, wid: str) -> dict | None:
        with self._lock:
            p = self._runs.get(wid)
        return p.snapshot() if p else None

    def wait(self, wid: str, timeout: float = 30) -> dict | None:
        with self._lock:
            t = self._threads.get(wid)
        if t:
            t.join(timeout)
        return self.status(wid)

    def forget(self, wid: str) -> None:
        with self._lock:
            self._runs.pop(wid, None)
