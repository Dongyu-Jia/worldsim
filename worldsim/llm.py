"""LLM backends and JSON extraction.

A backend answers ``complete(CallRequest) -> Completion`` or raises LLMError:

- OpenRouterBackend: one chat completion; structured outputs (response_format json_schema, strict) when the model
  lists them, else json_object; a json_schema request the provider refuses (HTTP 400/404/422) is retried once as
  json_object. Usage accounting on; the cost comes from OpenRouter's usage.cost, else from the catalogue's prices.
  For a model that takes the `reasoning` parameter, the reasoning is asked for (``reasoning: {exclude: false}``,
  plus the effort when one is set) unless "Record the model's thinking" is off; a model without it never gets the
  parameter (with provider.require_parameters it would find no endpoint).
- MockBackend: deterministic, schema-valid answers built from the request's context (no key, no network), with a
  marked mock thinking text and a raw response in OpenRouter's shape. A script ``{call key: [behaviour, ...]}``
  (call keys "actor:<id>", "synth:<step key>", "synth:<step kind>", "synth"; behaviours ok | error | bad_json |
  timeout | invalid | long | extra) makes the next calls behave in a chosen way, for tests and demos.

Every Completion (and every LLMError) carries what the audit view shows: ``thinking`` (parse_thinking: the reasoning
as the provider returned it) and ``audit``: the request body as sent without its messages (the caller keeps those),
the raw response JSON (each capped at AUDIT_CAP bytes, a truncated copy marked so), and the HTTP attempts (format,
status, error), two when the json_schema request fell back to json_object. The key is only ever in the Authorization
header, never in a body, and every recorded text is redacted.

extract_json(text) -> (object | None, error | None): the JSON object in a model's text (code fences, prose around it,
trailing commas, Python literals and smart quotes outside strings are tolerated).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config
from . import model as M
from .openrouter import ModelCatalog, OpenRouterClient, OpenRouterError, Transport, accepts_reasoning
from .settings import redact

AUDIT_CAP = 256 * 1024                 # bytes kept of a recorded request body, raw response or thinking text


@dataclass
class CallRequest:
    purpose: str                         # actor | synth
    model: str
    messages: list[dict]
    schema_name: str
    json_schema: dict
    temperature: float | None = None
    reasoning_effort: str = ""
    max_tokens: int | None = None
    timeout_s: float = 240
    context: dict = field(default_factory=dict)      # structured inputs (the mock answers from them)
    repair: bool = False
    record_thinking: bool = True                      # ask a reasoning model to return its reasoning


@dataclass
class Completion:
    text: str
    usage: dict
    cost: float | None
    model: str
    response_format: str
    meta: dict = field(default_factory=dict)
    thinking: dict = field(default_factory=dict)     # parse_thinking()
    audit: dict = field(default_factory=dict)        # audit_record(): request, raw response, HTTP attempts


class LLMError(RuntimeError):
    def __init__(self, message: str, kind: str = "error", retryable: bool = True, status: int | None = None,
                 audit: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status = status
        self.audit = audit or {}                      # what was sent (and received) before the failure


# ------------------------------------------------------------------------------------------------ the audit record

def scrub(obj: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    """obj with every string redacted (settings.redact): what is recorded never holds the key."""
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, secrets) for v in obj]
    return obj


def capped(obj: Any, limit: int = AUDIT_CAP) -> tuple[Any, bool]:
    """(obj, False) when its JSON fits in `limit` bytes, else ({truncated, bytes, text: the first bytes}, True)."""
    if obj is None:
        return None, False
    text = json.dumps(obj, ensure_ascii=False)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return obj, False
    return {"truncated": True, "bytes": len(raw), "text": raw[:limit].decode("utf-8", "ignore")}, True


def audit_record(body: dict | None, raw: Any, http_attempts: list[dict], secrets: list[str] | tuple[str, ...] = (),
                 thinking: dict | None = None) -> dict:
    """What one call sent and got back. The body is kept without "messages": the caller records those, and the exact
    body is {"model", "messages", ...the rest} again (audit.exact_request)."""
    params = {k: v for k, v in (body or {}).items() if k != "messages"} if body is not None else None
    req, req_cut = capped(scrub(params, secrets))
    resp, resp_cut = capped(scrub(raw, secrets))
    out = {"request": req, "request_truncated": req_cut, "raw_response": resp, "raw_response_truncated": resp_cut,
           "http_attempts": scrub(http_attempts, secrets)}
    if thinking is not None:
        out["thinking"] = thinking
    return out


def parse_thinking(msg: dict | None, capture: str = "on", reasoning_tokens: Any = None) -> dict:
    """The model's reasoning as OpenRouter returned it in choices[0].message: `reasoning` (a plain string) and
    `reasoning_details` (blocks reasoning.text {text, signature}, reasoning.summary {summary}, reasoning.encrypted
    {data}; each with format, index, id). -> {text, blocks [{type text|summary, text, format, index, id, signed}],
    encrypted_blocks, source reasoning|reasoning_details|none, capture, reasoning_tokens}. `capture`: on (asked
    for), off (the setting is off), unsupported (the model does not take the parameter), unknown (not in the model
    list, nothing asked). Encrypted blocks cannot be read: only their number is kept."""
    msg = msg if isinstance(msg, dict) else {}
    plain = msg.get("reasoning") if isinstance(msg.get("reasoning"), str) else ""
    details = msg.get("reasoning_details") if isinstance(msg.get("reasoning_details"), list) else []
    blocks: list[dict] = []
    encrypted = 0
    for i, d in enumerate(details):
        if not isinstance(d, dict):
            continue
        kind = str(d.get("type") or "")
        if kind == "reasoning.encrypted":
            encrypted += 1
            continue
        first = "summary" if kind == "reasoning.summary" else "text"
        body = next((d[k] for k in (first, "text", "summary") if isinstance(d.get(k), str) and d[k].strip()), "")
        if not body:
            continue
        blocks.append({"type": kind.rsplit(".", 1)[-1] or "text", "text": body, "format": d.get("format"),
                       "index": d.get("index", i), "id": d.get("id"), "signed": bool(d.get("signature"))})
    if plain.strip():
        text, source = plain, "reasoning"
    elif blocks:
        text, source = "\n\n".join(b["text"] for b in blocks), "reasoning_details"
    else:
        text, source = "", "none"
    cut, truncated = capped(text)
    return {"text": cut["text"] if truncated else text, "text_truncated": truncated, "blocks": blocks,
            "encrypted_blocks": encrypted, "source": source, "capture": capture,
            "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, (int, float)) else None}


def request_body(req: CallRequest, fmt: str, reasoning: dict | None, temperature: bool = True) -> dict:
    """The chat-completions body for this request (the key goes in the Authorization header, never here).
    `temperature`: False for a model that does not take it (with provider.require_parameters it would find no
    endpoint), so it is left out."""
    body: dict[str, Any] = {"model": req.model, "messages": req.messages, "usage": {"include": True}}
    if req.temperature is not None and temperature:
        body["temperature"] = req.temperature
    if req.max_tokens:
        body["max_tokens"] = req.max_tokens
    if reasoning:
        body["reasoning"] = reasoning
    if fmt == "json_schema":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": req.schema_name, "strict": True, "schema": req.json_schema}}
        body["provider"] = {"require_parameters": True}
    elif fmt == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body


# ------------------------------------------------------------------------------------------------ JSON extraction

FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n?(.*?)```", re.S)
SMART = {"“": '"', "”": '"', "„": '"', "＂": '"'}


def _balanced(s: str, start: int) -> int | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _outside_strings(s: str, fn) -> str:
    """Apply fn to the parts of s outside JSON strings."""
    out, buf, in_str, esc = [], [], False, False
    for c in s:
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            out.append(fn("".join(buf)))
            buf = []
            out.append(c)
            in_str = True
        else:
            buf.append(c)
    out.append(fn("".join(buf)))
    return "".join(out)


def _fix(seg: str) -> str:
    seg = re.sub(r",(\s*[}\]])", r"\1", seg)
    seg = re.sub(r"\bTrue\b", "true", seg)
    seg = re.sub(r"\bFalse\b", "false", seg)
    seg = re.sub(r"\bNone\b", "null", seg)
    seg = re.sub(r"\bNaN\b|-?\bInfinity\b", "null", seg)
    return seg.replace("：", ":").replace("，", ",")


def extract_json(text: Any) -> tuple[Any, str | None]:
    if isinstance(text, (dict, list)):
        return text, None
    s = str(text or "").strip()
    if not s:
        return None, "the answer is empty"
    try:
        obj = json.loads(s)
        return (obj, None) if isinstance(obj, dict) else (None, "the answer is JSON but not an object")
    except ValueError:
        pass
    candidates = [m.group(1).strip() for m in FENCE_RE.finditer(s)] + [s]
    last_err = "no JSON object found"
    for cand in candidates:
        for c0 in (cand, "".join(SMART.get(ch, ch) for ch in cand)):
            start = c0.find("{")
            while start >= 0:
                end = _balanced(c0, start)
                if end is None:
                    last_err = "the JSON object is not closed (the answer may be truncated)"
                    break
                chunk = c0[start:end]
                for attempt in (chunk, _outside_strings(chunk, _fix)):
                    try:
                        obj = json.loads(attempt)
                        if isinstance(obj, dict):
                            return obj, None
                    except ValueError as err:
                        last_err = f"invalid JSON: {err}"
                start = c0.find("{", end)
    return None, last_err


# ------------------------------------------------------------------------------------------------ OpenRouter

class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, api_key: str, catalog: ModelCatalog | None = None, transport: Transport | None = None):
        self._client = OpenRouterClient(api_key, transport=transport)
        self._key = api_key
        self.catalog = catalog or ModelCatalog(transport=transport)

    def _format_for(self, model_id: str) -> str:
        info = self.catalog.lookup(model_id)
        if info is None:
            return "json_object"
        if info.get("structured_outputs"):
            return "json_schema"
        return "json_object" if info.get("response_format") else "none"

    def _reasoning(self, req: CallRequest) -> tuple[dict | None, str]:
        """(the `reasoning` parameter or None, the thinking capture: on | off | unsupported | unknown)."""
        ok = accepts_reasoning(self.catalog.lookup(req.model))
        effort = req.reasoning_effort
        if ok is False:
            return None, "unsupported"                 # never sent: require_parameters would find no endpoint
        if not req.record_thinking:
            # as before the audit view: the reasoning text is not returned (exclude); its tokens are still billed
            return ({"effort": effort, "exclude": True} if effort else None), "off"
        if ok:
            return ({"effort": effort, "exclude": False} if effort else {"exclude": False}), "on"
        return ({"effort": effort, "exclude": False} if effort else None), ("on" if effort else "unknown")

    def _body(self, req: CallRequest, fmt: str) -> dict:
        info = self.catalog.lookup(req.model)
        return request_body(req, fmt, self._reasoning(req)[0], not (info and info.get("temperature") is False))

    def complete(self, req: CallRequest) -> Completion:
        fmt = self._format_for(req.model)
        reasoning, capture = self._reasoning(req)
        attempts: list[dict] = []
        sent: dict = {}

        info = self.catalog.lookup(req.model)
        temperature = not (info and info.get("temperature") is False)

        def send(fmt: str) -> dict:
            body = request_body(req, fmt, reasoning, temperature)
            if sent:                                    # the refused body, kept with its attempt (the schema)
                attempts[-1]["request"] = {k: v for k, v in sent["body"].items() if k != "messages"}
            sent["body"] = body
            t0 = time.monotonic()
            try:
                status, data = self._client.chat_exchange(body, req.timeout_s)
            except OpenRouterError as err:
                attempts.append({"format": fmt, "status": err.status, "ok": False, "error": redact(str(err), [self._key]),
                                 "duration_s": round(time.monotonic() - t0, 3)})
                raise
            attempts.append({"format": fmt, "status": status, "ok": True, "error": None,
                             "duration_s": round(time.monotonic() - t0, 3)})
            return data

        def failed(err: OpenRouterError) -> LLMError:
            return LLMError(redact(str(err), [self._key]), err.kind, err.retryable, err.status,
                            audit_record(sent.get("body"), None, attempts, [self._key], parse_thinking(None, capture)))
        try:
            data = send(fmt)
        except OpenRouterError as err:
            if fmt == "json_schema" and err.status in (400, 404, 422):
                fmt = "json_object"
                try:
                    data = send(fmt)
                except OpenRouterError as err2:
                    raise failed(err2) from None
            else:
                raise failed(err) from None
        return self._parse(req, data, fmt, sent["body"], attempts, capture)

    def _parse(self, req: CallRequest, data: dict, fmt: str, body: dict | None = None,
               attempts: list[dict] | None = None, capture: str = "on") -> Completion:
        secrets = [self._key]
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        pdetails = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        u = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
             "reasoning_tokens": details.get("reasoning_tokens"), "cached_tokens": pdetails.get("cached_tokens"),
             "total_tokens": usage.get("total_tokens")}
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        thinking = scrub(parse_thinking(msg, capture, u["reasoning_tokens"]), secrets)
        audit = audit_record(body, data, attempts or [], secrets, thinking)

        def fail(message: str, kind: str) -> LLMError:
            return LLMError(redact(message, secrets), kind, audit=audit)
        if not isinstance(choices, list) or not choices:
            raise fail("OpenRouter returned no choices", "empty")
        if isinstance(choice.get("error"), dict):
            raise fail(f"provider error: {choice['error'].get('message', '')}"[:400], "http")
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            fin = choice.get("finish_reason") or choice.get("native_finish_reason") or "unknown"
            raise fail(f"empty answer (finish reason: {fin})", "empty")
        cost = usage.get("cost")
        cost_source = "openrouter"
        if not isinstance(cost, (int, float)):
            cost, cost_source = price(self.catalog.lookup(req.model), u), "computed"
        audit.pop("thinking", None)
        return Completion(text=content, usage=u, cost=float(cost) if cost is not None else None,
                          model=str(data.get("model") or req.model), response_format=fmt,
                          meta={"id": data.get("id"), "provider": data.get("provider"),
                                "finish_reason": choice.get("finish_reason"),
                                "native_finish_reason": choice.get("native_finish_reason"), "cost_source": cost_source},
                          thinking=thinking, audit=audit)


def price(info: dict | None, usage: dict) -> float | None:
    if not info or info.get("prompt_price") is None or info.get("completion_price") is None:
        return None
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not isinstance(pt, (int, float)) or not isinstance(ct, (int, float)):
        return None
    return pt * info["prompt_price"] + ct * info["completion_price"]


# ------------------------------------------------------------------------------------------------ mock

def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _zh(language: str) -> bool:
    return M.is_cjk(language)


def est_tokens(text: str) -> int:
    """A rough token count: CJK characters count about one each, other text about four characters per token."""
    s = str(text or "")
    cjk = sum(1 for c in s if "　" <= c <= "鿿" or "＀" <= c <= "￯")
    return int(cjk + (len(s) - cjk) / 4) + 1


def _short(text: Any, n: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


VARIANTS = {"zh": ["维持现状，暂不调整", "小幅调整，力度温和", "明显加码，力度较大"],
            "en": ["keep the status quo for now", "a small, measured adjustment", "a clear, larger move"]}
ACTION_KINDS = {"zh": [("公开表态", "就局势发表公开讲话，释放政策信号"), ("与他方沟通", "与相关各方进行非公开沟通"),
                       ("调整内部安排", "调整内部资源与优先事项")],
                "en": [("A public signal", "a public speech that signals the next steps"),
                       ("Talks with others", "private talks with the parties concerned"),
                       ("An internal adjustment", "a shift of internal resources and priorities")]}


class MockBackend:
    """Deterministic, schema-valid answers from the request context (no key, no network, cost 0). Actors answer every
    decision question with a short text picked by hash of (world, step, actor, question) and add 0-2 autonomous
    actions; the synthesizer's digest lists the decisions, each indicator chunk moves about a third of its numeric
    fields a little (inside their plausible range), each question chunk updates about a third of its statuses, and
    the narrative counts what moved. Every answer comes with a mock thinking text (marked 「（模拟思考）」 /
    "(mock thinking)") that walks through the actor's questions or the step's items, counted as reasoning tokens and
    returned as OpenRouter would (message.reasoning and a reasoning.summary block) unless the thinking capture is off;
    the recorded request is the body OpenRouter would get, and the raw response has OpenRouter's shape.

    A script {call key: [behaviour, ...]} makes the next calls of that key behave in a chosen way: behaviours ok |
    error | timeout | auth (a refused key: the iteration pauses) | bad_json | invalid (an actor leaves its required questions unanswered; a synthesizer step
    answers {}) | long (a narrative over the limit) | extra (an indicator chunk adds a key outside its chunk). Call
    keys: "actor:<id>"; for the synthesizer the most specific queue that has entries is used: "synth:values:2", then
    "synth:values" (any indicator chunk), then "synth" (any step). Repairs use the same keys with ":repair"."""
    name = "mock"

    def __init__(self, delay_s: float | None = None, script: dict[str, list[str]] | None = None):
        self.delay_s = config.mock_delay_s() if delay_s is None else delay_s
        self._script = {k: list(v) for k, v in (script or {}).items()}
        self._lock = threading.Lock()
        self.calls: list[CallRequest] = []

    def _next(self, keys: list[str]) -> str:
        with self._lock:
            for key in keys:
                queue = self._script.get(key)
                if queue:
                    return queue.pop(0)
            return "ok"

    def complete(self, req: CallRequest) -> Completion:
        with self._lock:
            self.calls.append(req)
        if self.delay_s:
            time.sleep(self.delay_s)
        ctx = req.context or {}
        sfx = ":repair" if req.repair else ""
        if req.purpose == "actor":
            keys = [f"actor:{ctx.get('actor', {}).get('id')}{sfx}"]
        else:
            step = ctx.get("step") or {}
            keys = [f"synth:{step.get('key')}{sfx}", f"synth:{step.get('kind')}{sfx}", f"synth{sfx}"]
        behaviour = self._next(keys)
        effort = req.reasoning_effort
        capture = "on" if req.record_thinking else "off"
        body = request_body(req, "json_schema", {"effort": effort, "exclude": not req.record_thinking} if effort
                            else {"exclude": not req.record_thinking})
        if behaviour == "auth":
            raise LLMError("mock: injected refused key (HTTP 401)", "auth", retryable=False,
                           audit=audit_record(body, None, [], thinking=parse_thinking(None, capture)))
        if behaviour in ("error", "timeout"):
            raise LLMError(f"mock: injected {'failure' if behaviour == 'error' else 'timeout'}",
                           "http" if behaviour == "error" else "timeout",
                           audit=audit_record(body, None, [], thinking=parse_thinking(None, capture)))
        if req.purpose == "actor":
            obj = self._actor(ctx)
            if behaviour == "invalid":
                obj["answers"] = {}
        else:
            obj = {} if behaviour == "invalid" else self._synth(ctx, behaviour)
        text = json.dumps(obj, ensure_ascii=False)
        thought = self._thinking(req, ctx, obj, behaviour)
        if behaviour == "bad_json":
            text = "Sure! Here is my answer: {\"answers\": oops, not json"
        prompt = "".join(m.get("content", "") for m in req.messages)
        rt = est_tokens(thought)
        usage = {"prompt_tokens": est_tokens(prompt), "completion_tokens": est_tokens(text) + rt, "reasoning_tokens": rt,
                 "cached_tokens": 0}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if req.record_thinking:
            msg["reasoning"] = thought
            msg["reasoning_details"] = [{"type": "reasoning.summary", "summary": thought, "format": "mock", "index": 0,
                                         "id": None}]
        gen = f"gen-mock-{_h(req.model, text, thought) % 10**12:012d}"
        raw = {"id": gen, "provider": "Mock", "model": f"mock/{req.model}", "object": "chat.completion",
               "created": int(time.time()),
               "choices": [{"index": 0, "finish_reason": "stop", "native_finish_reason": "stop", "message": msg}],
               "usage": {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                         "total_tokens": usage["total_tokens"], "cost": 0,
                         "completion_tokens_details": {"reasoning_tokens": rt},
                         "prompt_tokens_details": {"cached_tokens": 0}}}
        thinking = {**parse_thinking(msg, capture, rt), "mock": True}
        return Completion(text=text, usage=usage, cost=0.0, model=f"mock/{req.model}", response_format="mock",
                          meta={"cost_source": "mock", "id": gen, "provider": "Mock", "finish_reason": "stop",
                                "native_finish_reason": "stop"},
                          thinking=thinking, audit=audit_record(body, raw, []))

    # ------------------------------------------------------------------ mock thinking
    @staticmethod
    def _thinking(req: CallRequest, ctx: dict, obj: dict, behaviour: str) -> str:
        """A short, marked walk through what the call answers: the actor's questions (and the variant it picks), or
        the step's items (and what it writes for them)."""
        world = ctx.get("world") or {}
        zh = _zh(world.get("language", ""))
        head = "（模拟思考）" if zh else "(mock thinking) "
        lines: list[str] = []
        if req.repair:
            lines.append(head + ("上一次的回答无法使用：这次只给出更正后的 JSON 对象。" if zh
                                 else "The previous answer could not be used: this time only the corrected JSON object."))
        if req.purpose == "actor":
            actor = ctx.get("actor") or {}
            state = ctx.get("state") or {}
            qs = actor.get("decision_questions") or []
            given = obj.get("answers") or {}
            lines.append(head + (f"我是{actor.get('name')}，{state.get('period', '')} 之后的这一期要回答 {len(qs)} 个决策问题。"
                                 if zh else f"I am {actor.get('name')}; after {state.get('period', '')} I answer "
                                            f"{len(qs)} decision question(s)."))
            for i, q in enumerate(qs, 1):
                a = given.get(q["id"]) or {}
                pick = a.get("value") or ("（先不回答）" if zh else "(no answer this time)")
                lines.append(f"{i}. {_short(q['text'], 60)} → {_short(pick, 80)}")
            acts = obj.get("actions") or []
            if (actor.get("autonomy") or {}).get("enabled"):
                lines.append((f"自主行动：本期采取 {len(acts)} 项。" if zh else f"Autonomous actions: {len(acts)} this time.")
                             + "".join(f" {a.get('title')}" for a in acts))
            lines.append("最后写公开表态和总体理由。" if zh else "Then a public statement and the overall rationale.")
        else:
            step = ctx.get("step") or {}
            kind = step.get("kind")
            label = step.get("label") or kind
            results = ctx.get("actor_results") or []
            if kind == "digest":
                ok = [r.get("name") for r in results if r.get("status") == "ok" and r.get("output")]
                lines.append(head + (f"{label}：读 {len(results)} 方的决策，其中 {len(ok)} 方作出了决定："
                                     if zh else f"{label}: reading {len(results)} actors' decisions, {len(ok)} decided: ")
                             + ("、" if zh else ", ").join(map(str, ok)))
                lines.append("找出相互作用与主导力量，写成本季要事。" if zh else "Find how they interact and what dominates.")
            elif kind == "values":
                lines.append(head + (f"{label}：逐项判断这些指标的新值。" if zh else f"{label}: the new value of each indicator."))
                prev = (ctx.get("state") or {}).get("values") or {}
                for fid, e in ((obj.get("values") or {}).items()):
                    v = e.get("value") if isinstance(e, dict) else e
                    lines.append(f"- {fid}: {prev.get(fid)} → {v}")
            elif kind == "statuses":
                lines.append(head + (f"{label}：逐个问题判断现状是否变化。" if zh else f"{label}: does each status change?"))
                for qid, e in ((obj.get("statuses") or {}).items()):
                    changed = bool(e.get("changed")) if isinstance(e, dict) else False
                    lines.append(f"- {qid}: " + (("有变化" if changed else "不变") if zh else ("changed" if changed else "unchanged")))
            else:
                lines.append(head + ("把新的指标和现状浓缩成一段叙述，控制字数。" if zh
                                     else "Condense the new values and statuses into the narrative, within the limit."))
        if behaviour == "bad_json":
            lines.append("（这次故意写出无法解析的回答。）" if zh else "(this time the answer is deliberately unreadable.)")
        return "\n".join(lines)

    # ------------------------------------------------------------------ actors
    @staticmethod
    def _actor(ctx: dict) -> dict:
        actor = ctx.get("actor") or {}
        world = ctx.get("world") or {}
        state = ctx.get("state") or {}
        lang = "zh" if _zh(world.get("language", "")) else "en"
        name = actor.get("name") or actor.get("id")
        cares = actor.get("cares_about") or ["自身目标" if lang == "zh" else "its goals"]
        seed = (world.get("id"), state.get("step"), actor.get("id"))
        answers = {}
        for i, q in enumerate(actor.get("decision_questions") or []):
            variant = VARIANTS[lang][_h(*seed, q["id"]) % 3]
            if lang == "zh":
                answers[q["id"]] = {"value": f"（模拟）关于「{_short(q['text'], 18)}」：{variant}。",
                                    "rationale": f"（模拟）优先考虑：{cares[i % len(cares)]}。"}
            else:
                answers[q["id"]] = {"value": f"(mock) On “{_short(q['text'], 40)}”: {variant}.",
                                    "rationale": f"(mock) The priority is: {cares[i % len(cares)]}."}
        out: dict[str, Any] = {"answers": answers}
        auto = actor.get("autonomy") or {}
        if auto.get("enabled"):
            n = min(_h(*seed, "actions") % 3, int(auto.get("max_actions") or 0))
            peers = ctx.get("peers") or []
            acts = []
            for k in range(n):
                title, detail = ACTION_KINDS[lang][_h(*seed, "kind", k) % 3]
                target = peers[_h(*seed, "target", k) % len(peers)] if peers else ("公众" if lang == "zh" else "the public")
                if lang == "zh":
                    acts.append({"title": f"（模拟）{title}", "detail": f"（模拟）{detail}。", "target": target,
                                 "intended_effect": "（模拟）稳定预期，为下一步争取空间。"})
                else:
                    acts.append({"title": f"(mock) {title}", "detail": f"(mock) {detail}.", "target": target,
                                 "intended_effect": "(mock) steady expectations and buy time."})
            out["actions"] = acts
        if lang == "zh":
            out.update({"public_statement": f"（模拟）{name}：我们已作出本期决定，并将密切关注局势。",
                        "rationale": f"（模拟）基于{state.get('period', '')}的状态，优先考虑：{cares[0]}。",
                        "expected_consequences": ["（模拟）影响有限且可控。"]})
        else:
            out.update({"public_statement": f"(mock) {name}: we have decided for this period and will watch closely.",
                        "rationale": f"(mock) Given the state at {state.get('period', '')}, the priority is: {cares[0]}.",
                        "expected_consequences": ["(mock) a limited, contained effect."]})
        return out

    # ------------------------------------------------------------------ synthesizer
    @staticmethod
    def _synth(ctx: dict, behaviour: str = "ok") -> dict:
        kind = (ctx.get("step") or {}).get("kind")
        if kind == "digest":
            return MockBackend._digest(ctx)
        if kind == "values":
            out = MockBackend._values(ctx)
            if behaviour == "extra":
                out["values"]["not_in_this_chunk"] = {"value": 1, "reason": "extra"}
            return out
        if kind == "statuses":
            return MockBackend._statuses(ctx)
        out = MockBackend._narrative(ctx)
        if behaviour == "long":
            out["narrative"] = out["narrative"] * 40
        return out

    @staticmethod
    def _digest(ctx: dict) -> dict:
        from .prompts import output_brief
        world = ctx.get("world") or {}
        results = ctx.get("actor_results") or []
        zh = _zh(world.get("language", ""))
        period = ctx.get("next_period", "")
        acted = [r for r in results if r.get("status") == "ok" and r.get("output")]
        silent = [r for r in results if r.get("status") != "ok" or not r.get("output")]
        if zh:
            parts = [f"【模拟】{period} 要事：" + "；".join(f"{r['name']}——{output_brief(r['output'], 40)}" for r in acted) + "。"]
            if silent:
                parts.append("未回应：" + "、".join(r["name"] for r in silent) + "。")
            parts.append("各方决策相互作用有限，局势总体延续上一季。")
            return {"summary": f"（模拟）{len(acted)} 方作出决策", "digest": "".join(parts),
                    "threads": ["（模拟）政策决定 → 金融条件", "（模拟）能源供给 → 通胀与信心"]}
        parts = [f"[mock] {period}: " + "; ".join(f"{r['name']} — {output_brief(r['output'], 60)}" for r in acted) + "."]
        if silent:
            parts.append("No response: " + ", ".join(r["name"] for r in silent) + ".")
        parts.append("The decisions interact little; the situation mostly continues.")
        return {"summary": f"(mock) {len(acted)} actors decided", "digest": " ".join(parts),
                "threads": ["(mock) policy decisions → financial conditions", "(mock) supply → prices and confidence"]}

    @staticmethod
    def _values(ctx: dict) -> dict:
        world = ctx.get("world") or {}
        state = ctx.get("state") or {}
        schema = ctx.get("schema") or {"fields": []}
        zh = _zh(world.get("language", ""))
        step_no = int(state.get("step") or 0) + 1
        cur = state.get("values") or {}
        fmap = {f["id"]: f for f in schema.get("fields", [])}
        values: dict[str, dict] = {}
        for fid in (ctx.get("step") or {}).get("items") or list(fmap):
            f = fmap.get(fid)
            if not f:
                continue
            prev = cur.get(fid)
            val, reason = prev, ""
            h = _h(world.get("id"), step_no, fid)
            if f["type"] in M.NUMERIC_TYPES and isinstance(prev, (int, float)) and not isinstance(prev, bool) and h % 3 == 0:
                lo, hi = f.get("min"), f.get("max")
                span = (hi - lo) if (lo is not None and hi is not None and hi > lo) else 0.0
                step_size = 0.01 * abs(prev) + 0.001 * span + (0.01 if not prev and not span else 0.0)
                delta = ((h // 3) % 7 - 3) / 3 * step_size              # small, deterministic, either sign
                new = prev + delta
                if lo is not None:
                    new = max(lo, new)
                if hi is not None:
                    new = min(hi, new)
                new = int(round(new)) if f["type"] == "integer" else round(new, 2)
                if new != prev:
                    val = new
                    reason = "（模拟）各方决策的综合结果带来小幅变动。" if zh else "(mock) a small move from the decisions combined."
            if val is None:
                val = {"number": 0.0, "percent": 0.0, "integer": 0, "boolean": False,
                       "enum": (f.get("options") or [""])[0], "text": ""}.get(f["type"], "")
                reason = "（模拟）此前没有数值，取默认值。" if zh else "(mock) no previous value: a default."
            values[fid] = {"value": val, "reason": reason}
        return {"values": values}

    @staticmethod
    def _statuses(ctx: dict) -> dict:
        world = ctx.get("world") or {}
        state = ctx.get("state") or {}
        schema = ctx.get("schema") or {"questions": []}
        zh = _zh(world.get("language", ""))
        period = ctx.get("next_period", "")
        step_no = int(state.get("step") or 0) + 1
        cur = state.get("statuses") or {}
        qmap = {q["id"]: q for q in schema.get("questions", [])}
        out: dict[str, dict] = {}
        for qid in (ctx.get("step") or {}).get("items") or list(qmap):
            prev = (cur.get(qid) or "").strip()
            if not prev:
                out[qid] = {"status": "（模拟）此前没有现状描述：本季开始跟踪。" if zh else "(mock) No status before: tracked from now on.",
                            "changed": True, "reason": "（模拟）补上缺失的现状。" if zh else "(mock) a missing status filled in."}
            elif _h(world.get("id"), step_no, qid, "status") % 3 == 0:
                base = re.sub(r"^(（模拟）截至 [^：]+：|\(mock\) As of [^:]+: )+", "", prev)
                head = re.split(r"(?<=[。！？])|(?<=[.!?])\s", base)[0].strip()
                head = head if len(head) <= 80 else head[:79] + "…"
                new = (f"（模拟）截至 {period}：{head}本季各方决策后出现小幅变化。" if zh
                       else f"(mock) As of {period}: {head} A small change after this period's decisions.")
                out[qid] = {"status": new, "changed": True,
                            "reason": "（模拟）各方决策的综合影响。" if zh else "(mock) the combined effect of the decisions."}
            else:
                out[qid] = {"status": prev, "changed": False, "reason": ""}
        return {"statuses": out}

    @staticmethod
    def _narrative(ctx: dict) -> dict:
        world = ctx.get("world") or {}
        state = ctx.get("state") or {}
        schema = ctx.get("schema") or {}
        written = ctx.get("written") or {}
        zh = _zh(world.get("language", ""))
        period = ctx.get("next_period", "")
        results = ctx.get("actor_results") or []
        acted = sum(1 for r in results if r.get("status") == "ok" and r.get("output"))
        silent = [r["name"] for r in results if r.get("status") != "ok" or not r.get("output")]
        fmap = {f["id"]: f for f in schema.get("fields", [])}
        prev = state.get("values") or {}
        moved = sum(1 for fid, v in (written.get("values") or {}).items()
                    if fid in fmap and not M.values_equal(fmap[fid], prev.get(fid), v))
        changed = sum(1 for v in (written.get("status_changed") or {}).values() if v)
        if zh:
            text = (f"（模拟）{period}：{acted} 方作出决策" + (f"，{len(silent)} 方未回应" if silent else "")
                    + f"；{moved} 个指标小幅变动，{changed} 个问题的现状有更新，其余大体延续。")
        else:
            text = (f"[mock] {period}: {acted} actors decided" + (f"; no response: {', '.join(silent)}" if silent else "")
                    + f"; {moved} values moved a little and {changed} statuses changed; the rest held.")
        limit = int(ctx.get("narrative_max") or 0)
        return {"narrative": text[:limit] if limit else text}


_SCRIPTED: dict[str, MockBackend] = {}


def make_backend(settings: dict, catalog: ModelCatalog | None = None, transport: Transport | None = None):
    """The backend for these settings (WORLDSIM_LLM overrides the choice in Settings). WORLDSIM_MOCK_SCRIPT (JSON,
    e.g. {"synth": ["error"]}) gives the mock one scripted queue for the whole process, to try the failure paths."""
    name = config.forced_backend() or settings.get("backend") or "openrouter"
    if name == "mock":
        raw = config.mock_script()
        if raw:
            if raw not in _SCRIPTED:
                try:
                    script = json.loads(raw)
                except ValueError:
                    script = {}
                _SCRIPTED[raw] = MockBackend(script=script if isinstance(script, dict) else {})
            return _SCRIPTED[raw]
        return MockBackend()
    return OpenRouterBackend(settings.get("api_key") or "", catalog=catalog, transport=transport)
