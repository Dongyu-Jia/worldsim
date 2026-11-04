"""The audit view of an iteration (or of a failed attempt awaiting retry): every model call, grouped by participant.

A participant is an actor (who = the actor's id, as Engine._call logs it) or a synthesizer step (who =
"synthesizer:<step key>"). For each, every attempt in order (first call, repair, retry, a later retry round) with:

- input: the messages exactly as sent and the rest of the request body (model, temperature, max_tokens, reasoning,
  response_format with the JSON schema, provider, usage); exact_request() puts the two back together;
- thinking: the model's reasoning as the provider returned it (llm.parse_thinking), never written by WorldSim;
- output: the raw response text, the JSON parsed from it, the validator's verdict (accepted or why not, notes) and
  the finish reason, plus the raw response JSON.

Calls saved before the audit fields existed have only messages and the response text: they are marked `legacy`.

    index(record, world)        -> [participant summary]        (actors in world order, then the synthesizer steps)
    detail(record, world, who)  -> {participant, final, attempts} | None
"""
from __future__ import annotations

from typing import Any

SYNTH = "synthesizer:"


def exact_request(call: dict) -> dict | None:
    """The request body as sent: the recorded parameters with the messages put back, in the order they were sent."""
    req = call.get("request")
    if not isinstance(req, dict) or req.get("truncated"):
        return None
    body = {"model": req.get("model", call.get("model")), "messages": call.get("messages") or []}
    body.update({k: v for k, v in req.items() if k not in ("model", "messages")})
    return body


def _tokens(calls: list[dict]) -> dict:
    tot = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    for c in calls:
        u = c.get("usage") or {}
        for k in tot:
            v = u.get(f"{k}_tokens")
            if isinstance(v, (int, float)):
                tot[k] += int(v)
    return tot


def _has_thinking(c: dict) -> bool:
    t = c.get("thinking")
    return isinstance(t, dict) and bool((t.get("text") or "").strip())


def _participants(record: dict, world: dict | None) -> list[dict]:
    """[{who, kind, label, ref}] in display order. `ref` is the actor result or the synthesizer step record."""
    out: list[dict] = []
    results = record.get("actors") or []
    names = {a.get("id"): a.get("name") for a in (world or {}).get("actors") or []}
    order = {a.get("id"): i for i, a in enumerate((world or {}).get("actors") or [])}
    def rid(r: dict) -> Any:
        return r.get("actor_id") or r.get("id")

    for r in sorted(results, key=lambda r: order.get(rid(r), len(order))):
        out.append({"who": rid(r), "kind": "actor", "label": r.get("name") or names.get(rid(r)) or rid(r), "ref": r})
    steps = (record.get("synthesizer") or {}).get("steps") if isinstance(record.get("synthesizer"), dict) else None
    for st in steps if steps is not None else record.get("steps") or []:
        out.append({"who": SYNTH + str(st.get("key")), "kind": "synth", "label": st.get("label") or st.get("key"),
                    "ref": st})
    known = {p["who"] for p in out}
    for c in record.get("calls") or []:            # a call of a participant the record no longer lists
        w = c.get("who")
        if w and w not in known:
            known.add(w)
            out.append({"who": w, "kind": "synth" if str(w) == "synthesizer" or str(w).startswith(SYNTH) else "actor", "label": w, "ref": {}})
    return out


def _calls_of(record: dict, who: str) -> list[dict]:
    return [c for c in record.get("calls") or [] if c.get("who") == who]


def _status(p: dict, calls: list[dict]) -> str:
    ref = p["ref"] or {}
    if ref.get("status"):
        return str(ref["status"])
    return "ok" if any(c.get("accepted") for c in calls) else ("failed" if calls else "not run")


def index(record: dict, world: dict | None) -> list[dict]:
    out = []
    for p in _participants(record, world):
        calls = _calls_of(record, p["who"])
        cost = [c.get("cost") for c in calls if isinstance(c.get("cost"), (int, float))]
        dur = [c.get("duration_s") for c in calls if isinstance(c.get("duration_s"), (int, float))]
        last = calls[-1] if calls else {}
        out.append({"who": p["who"], "kind": p["kind"], "label": p["label"], "status": _status(p, calls),
                    "attempts": len(calls), "has_thinking": any(_has_thinking(c) for c in calls),
                    "legacy": bool(calls) and all("thinking" not in c for c in calls),
                    "tokens": _tokens(calls), "cost": round(sum(cost), 6) if cost else None,
                    "duration_s": round(sum(dur), 3) if dur else None,
                    "model": last.get("served_model") or last.get("model")})
    return out


def _attempt(c: dict, i: int) -> dict:
    req = c.get("request") if isinstance(c.get("request"), dict) else None
    rf = (req or {}).get("response_format") or {}
    params = None
    if req is not None and not req.get("truncated"):
        params = {"model": req.get("model", c.get("model")), "temperature": req.get("temperature"),
                  "max_tokens": req.get("max_tokens"), "reasoning": req.get("reasoning"),
                  "response_format": rf.get("type") or c.get("response_format"),
                  "schema_name": (rf.get("json_schema") or {}).get("name") or c.get("schema_name"),
                  "provider": req.get("provider")}
    return {
        "n": i + 1, "attempt": c.get("attempt"), "repair": bool(c.get("repair")), "round": c.get("round"),
        "started": c.get("started"), "duration_s": c.get("duration_s"),
        "legacy": "thinking" not in c and "request" not in c,
        "input": {"messages": c.get("messages") or [], "params": params,
                  "schema": (rf.get("json_schema") or {}).get("schema"), "request": exact_request(c),
                  "request_truncated": bool(c.get("request_truncated")),
                  "request_text": req.get("text") if req and req.get("truncated") else None},
        "thinking": c.get("thinking") if isinstance(c.get("thinking"), dict) else None,
        "output": {"text": c.get("response_text"), "parsed": c.get("parsed"), "accepted": bool(c.get("accepted")),
                   "validation_error": c.get("validation_error"), "notes": c.get("notes") or [],
                   "finish_reason": c.get("finish_reason"), "native_finish_reason": c.get("native_finish_reason"),
                   "raw": c.get("raw_response"), "raw_truncated": bool(c.get("raw_response_truncated"))},
        "meta": {"model": c.get("model"), "served_model": c.get("served_model"), "provider": c.get("provider"),
                 "generation_id": c.get("generation_id"), "usage": c.get("usage"), "cost": c.get("cost"),
                 "cost_source": c.get("cost_source"), "response_format": c.get("response_format"),
                 "error": c.get("error"), "http_attempts": c.get("http_attempts") or []},
    }


def detail(record: dict, world: dict | None, who: str) -> dict | None:
    p = next((x for x in _participants(record, world) if x["who"] == who), None)
    if p is None:
        return None
    calls = _calls_of(record, who)
    ref = p["ref"] or {}
    if p["kind"] == "actor":
        final = {"status": ref.get("status"), "output": ref.get("output"), "notes": ref.get("notes") or [],
                 "error": ref.get("error"), "attempts": ref.get("attempts"), "model": ref.get("model")}
    else:
        final = {"status": ref.get("status"), "output": ref.get("output"), "notes": [], "error": ref.get("error"),
                 "attempts": ref.get("attempts"), "items": ref.get("items") or [], "dropped": ref.get("dropped") or []}
    summary = next((x for x in index(record, world) if x["who"] == who), {})
    return {"participant": summary, "final": final, "attempts": [_attempt(c, i) for i, c in enumerate(calls)]}


def light_call(c: dict) -> dict:
    """A call entry without its heavy texts (messages, request, thinking, raw response, parsed JSON): what the
    Simulation page's step payload carries."""
    keep = ("who", "purpose", "step", "attempt", "repair", "round", "model", "served_model", "started", "usage",
            "cost", "cost_source", "error", "duration_s", "accepted", "finish_reason", "response_format")
    out: Any = {k: c[k] for k in keep if k in c}
    out["has_thinking"] = _has_thinking(c)
    return out
