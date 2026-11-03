"""The WorldSim server: a JSON API under /api and the static UI (worldsim/web) at /.

    python -m worldsim                  # http://127.0.0.1:8940 (WORLDSIM_PORT)

Security: the server binds 127.0.0.1. Every /api request must name this machine in its Host header (DNS rebinding);
every state-changing request (POST, PUT, PATCH, DELETE) must come from the app's own pages (Origin / Sec-Fetch-Site)
and carry a JSON body (Content-Type: application/json), which another site cannot send without a CORS preflight that
this server never answers. The OpenRouter key is never returned: GET /api/settings shows it masked.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, config
from . import model as M
from . import settings as S
from .engine import Busy, Engine, RunManager
from .openrouter import ModelCatalog, OpenRouterClient, OpenRouterError, Transport
from .store import Conflict, NotFound, Store

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
NOT_LOCAL = "WorldSim only answers pages served from this machine (http://127.0.0.1 or http://localhost)."
NOT_SAME_ORIGIN = "Refused: this request comes from another site (Origin / Sec-Fetch-Site). Nothing was changed."
NEEDS_JSON = "Changes need a JSON body (Content-Type: application/json)."
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


def local_host(request: Request) -> bool:
    host = (request.headers.get("host") or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else ""
    else:
        host = host.rsplit(":", 1)[0]
    return host in LOCAL_HOSTS


def same_origin(request: Request) -> bool:
    """Whether a request comes from this server's own pages. Browsers send Sec-Fetch-Site on every request and
    Origin on every cross-origin one; a request with neither (curl, tests) is not from a browser page."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        return False
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin:
        return True
    if origin == "null":
        return False
    host = (request.headers.get("host") or "").strip().lower()
    return origin in (f"http://{host}", f"https://{host}") and local_host(request)


def refusal(request: Request) -> str | None:
    if not local_host(request):
        return NOT_LOCAL
    if request.method in MUTATING:
        if not same_origin(request):
            return NOT_SAME_ORIGIN
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return NEEDS_JSON
    return None


def _json(obj: Any, status: int = 200, headers: dict | None = None) -> Response:
    return Response(content=json.dumps(obj, ensure_ascii=False, allow_nan=False), status_code=status,
                    media_type="application/json", headers=headers)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "the request body must be a JSON object")
    return data


def _strip_calls(record: dict | None) -> dict | None:
    """An iteration (or pending) record without the heavy prompt and response texts of its calls."""
    if not record:
        return record
    out = copy.deepcopy(record)
    out["calls"] = [{k: v for k, v in c.items() if k not in ("messages", "response_text")}
                    for c in record.get("calls") or []]
    return out


def create_app(root=None, transport: Transport | None = None, backend_factory=None) -> FastAPI:
    """The app. `root`: the data directory (default config.data_dir()); `transport`: the OpenRouter HTTP transport
    (tests pass a fake); `backend_factory(settings) -> backend` replaces the LLM backend (tests)."""
    store = Store(root)
    catalog = ModelCatalog(store.root, transport=transport)

    def load_settings() -> dict:
        return S.load(store.root)

    from .llm import make_backend
    factory = backend_factory or (lambda s: make_backend(s, catalog=catalog, transport=transport))
    engine = Engine(store, load_settings, catalog=catalog, backend_factory=factory)
    runs = RunManager(engine)
    store.ensure_default()

    app = FastAPI(title="WorldSim", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine, app.state.runs, app.state.catalog = store, engine, runs, catalog

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/") or request.url.path == "/api":
            why = refusal(request)
            if why:
                return _json({"detail": why}, 403)
        elif not local_host(request):
            return _json({"detail": NOT_LOCAL}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        path = request.url.path
        if path.startswith("/api/") or path.startswith(("/js/", "/css/")) or path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, err: StarletteHTTPException):
        detail = err.detail if isinstance(err.detail, (str, dict, list)) else str(err.detail)
        return _json({"detail": detail}, err.status_code)

    def world_or_404(wid: str) -> dict:
        try:
            return store.load_world(wid)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    def not_busy(wid: str) -> None:
        if runs.busy(wid):
            raise HTTPException(409, "an iteration is running for this world: wait for it to finish")

    def save(world: dict) -> dict:
        try:
            return store.save_world(world)
        except M.WorldError as e:
            raise HTTPException(400, {"message": "the world does not validate", "problems": e.problems}) from None

    def history_len(wid: str) -> int:
        steps = store.step_numbers(wid)
        return (steps[-1] if steps else 0)

    # ------------------------------------------------------------------ health and settings
    @app.get("/api/health")
    def health():
        s = load_settings()
        return {"ok": True, "version": __version__, "backend": S.effective_backend(s),
                "forced_backend": config.forced_backend(), "has_key": bool(s.get("api_key"))}

    @app.get("/api/settings")
    def get_settings():
        return S.public(load_settings())

    @app.put("/api/settings")
    async def put_settings(request: Request):
        patch = await _body(request)
        current = load_settings()
        if patch.get("clear_key"):
            patch = {**patch, "api_key": ""}
        elif "api_key" in patch and not (patch.get("api_key") or "").strip():
            patch = {k: v for k, v in patch.items() if k != "api_key"}     # a blank box keeps the stored key
        new, problems = S.validate(patch, current)
        if problems:
            raise HTTPException(400, {"message": "settings not saved", "problems": problems})
        S.save(new, store.root)
        return S.public(new)

    @app.post("/api/settings/test_key")
    async def test_key(request: Request):
        await _body(request)
        s = load_settings()
        if not s.get("api_key"):
            return {"ok": False, "message": "No OpenRouter key is set."}
        try:
            info = OpenRouterClient(s["api_key"], transport=transport).key_info(timeout=min(30, s["timeout_s"]))
        except OpenRouterError as e:
            return {"ok": False, "message": S.redact(str(e), [s["api_key"]])}
        return {"ok": True, "message": "The key works.", "info": info}

    @app.get("/api/models")
    def models(refresh: int = 0):
        data = catalog.get(refresh=bool(refresh))
        s = load_settings()
        return {"models": data.get("models", []), "source": data.get("source"), "note": data.get("note"),
                "fetched_at": data.get("fetched_at"), "default_model": s.get("default_model")}

    # ------------------------------------------------------------------ worlds
    @app.get("/api/worlds")
    def list_worlds():
        return {"worlds": store.list_worlds(), "template": config.DEFAULT_WORLD_FILE.exists()}

    @app.post("/api/worlds")
    async def create_world(request: Request):
        body = await _body(request)
        mode = body.get("mode") or "blank"
        name = M._text(body.get("name"), M.LIMITS["name"])
        if mode == "template":
            try:
                world = json.loads(config.DEFAULT_WORLD_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise HTTPException(404, "the default world template is missing") from None
            world["id"] = "new"
        elif mode == "blank":
            world = M.blank_world("new", name or "New world")
        else:
            raise HTTPException(400, "mode must be blank or template")
        if name:
            world["name"] = name
        try:
            created = store.create_world(world)
        except M.WorldError as e:
            raise HTTPException(400, {"message": "the world does not validate", "problems": e.problems}) from None
        return {"world": created}

    @app.post("/api/worlds/import")
    async def import_world(request: Request):
        body = await _body(request)
        world = body.get("world") if isinstance(body.get("world"), dict) else body
        history = world.get("history") if isinstance(world.get("history"), dict) else None
        world = {k: v for k, v in world.items() if k not in ("history", "exported")}
        try:
            created = store.create_world(world, history)
        except M.WorldError as e:
            raise HTTPException(400, {"message": "the file does not hold a valid world", "problems": e.problems}) from None
        return {"world": created}

    @app.get("/api/worlds/{wid}")
    def get_world(wid: str):
        world = world_or_404(wid)
        pending = _strip_calls(store.load_pending(wid))
        if pending:
            pending.pop("calls", None)
        return {"world": world, "steps": store.step_numbers(wid), "pending": pending, "busy": runs.busy(wid),
                "calls_per_iteration": engine.call_counts(world)}

    @app.put("/api/worlds/{wid}")
    async def put_world(wid: str, request: Request):
        body = await _body(request)
        world = world_or_404(wid)
        not_busy(wid)
        for key in ("name", "description", "language", "period_step", "history_window", "model"):
            if key in body:
                world[key] = body[key]
        return {"world": save(world)}

    @app.put("/api/worlds/{wid}/schema")
    async def put_schema(wid: str, request: Request):
        body = await _body(request)
        world = world_or_404(wid)
        not_busy(wid)
        questions = body.get("questions") if "questions" in body else world["schema"].get("questions", [])
        schema, problems = M.normalize_schema({"fields": body.get("fields"), "questions": questions})
        if problems:
            raise HTTPException(400, {"message": "the schema does not validate", "problems": problems})
        world["schema"] = schema
        init = world["initial_state"]
        new_vals = body.get("initial_values")
        new_sts = body.get("initial_statuses")
        read_only = ("the schema was not saved: this world has steps after the initial state, so its initial %s "
                     "cannot change. Rewind to step 0 (or fork) first.")
        if isinstance(new_vals, dict):
            if history_len(wid) > 0:
                old = {f["id"]: init["values"].get(f["id"]) for f in schema["fields"]}
                given = {fid: new_vals.get(fid) for fid in old}
                changed = [fid for fid in old if fid in new_vals and given[fid] is not None and
                           not M.values_equal(M.field_map(schema)[fid], old[fid],
                                              M.coerce_value(M.field_map(schema)[fid], given[fid])[0])]
                if changed:
                    raise HTTPException(409, read_only % "values")
            else:
                init = {**init, "values": new_vals}
        if isinstance(new_sts, dict):
            if history_len(wid) > 0:
                old_sts = init.get("statuses") or {}
                changed = [q for q in new_sts if q in old_sts and (old_sts.get(q) or "") != M._text(new_sts[q])]
                if changed:
                    raise HTTPException(409, read_only % "statuses")
            else:
                init = {**init, "statuses": new_sts}
        init_vals = {f["id"]: (init.get("values") or {}).get(f["id"]) for f in schema["fields"]}
        world["initial_state"] = {**init, "values": init_vals,
                                  "statuses": M.normalize_statuses(schema, init.get("statuses"))}
        actor_fields = set(M.field_map(schema))
        for a in world["actors"]:
            if a.get("visible_fields") is not None:
                a["visible_fields"] = [f for f in a["visible_fields"] if f in actor_fields]
        return {"world": save(world)}

    @app.put("/api/worlds/{wid}/initial")
    async def put_initial(wid: str, request: Request):
        body = await _body(request)
        world = world_or_404(wid)
        not_busy(wid)
        if history_len(wid) > 0:
            raise HTTPException(409, "this world has steps after the initial state: rewind to step 0 (or fork a new "
                                     "world) before editing the initial state")
        state, problems = M.normalize_state(world["schema"], {**world["initial_state"], **{
            k: body[k] for k in ("period", "values", "statuses", "narrative") if k in body}, "step": 0})
        if problems:
            raise HTTPException(400, {"message": "the initial state does not validate", "problems": problems})
        world["initial_state"] = state
        return {"world": save(world)}

    @app.put("/api/worlds/{wid}/actors")
    async def put_actors(wid: str, request: Request):
        body = await _body(request)
        world = world_or_404(wid)
        not_busy(wid)
        actors, problems = M.normalize_actors(body.get("actors"), set(M.field_map(world["schema"])),
                                              world.get("language") or "", world["period_step"]["unit"])
        if problems:
            raise HTTPException(400, {"message": "the actors do not validate", "problems": problems})
        world["actors"] = actors
        return {"world": save(world)}

    @app.put("/api/worlds/{wid}/synthesizer")
    async def put_synth(wid: str, request: Request):
        body = await _body(request)
        world = world_or_404(wid)
        not_busy(wid)
        synth, problems = M.normalize_synthesizer(body)
        if problems:
            raise HTTPException(400, {"message": "the synthesizer settings do not validate", "problems": problems})
        world["synthesizer"] = synth
        return {"world": save(world)}

    @app.post("/api/worlds/{wid}/duplicate")
    async def duplicate(wid: str, request: Request):
        body = await _body(request)
        world_or_404(wid)
        return {"world": store.duplicate(wid, M._text(body.get("name"), M.LIMITS["name"]) or None)}

    @app.get("/api/worlds/{wid}/export")
    def export(wid: str):
        world_or_404(wid)
        return _json(store.export(wid), headers={"Content-Disposition": f'attachment; filename="{wid}.worldsim.json"'})

    @app.delete("/api/worlds/{wid}")
    async def delete_world(wid: str, request: Request):
        body = await _body(request)
        world_or_404(wid)
        not_busy(wid)
        if body.get("confirm") is not True:
            raise HTTPException(400, 'deleting a world needs {"confirm": true}')
        store.delete_world(wid)
        runs.forget(wid)
        return {"ok": True}

    # ------------------------------------------------------------------ steps and iterations
    @app.get("/api/worlds/{wid}/steps")
    def steps(wid: str):
        world_or_404(wid)
        out = []
        for n in store.step_numbers(wid):
            st = store.load_step(wid, n)
            out.append({"step": n, "period": st.get("period"), "summary": st.get("summary") or "",
                        "cost": st.get("cost"), "cost_complete": st.get("cost_complete"),
                        "flags": len(st.get("flags") or []), "changes": len(st.get("changes") or []),
                        "status_changes": len(st.get("status_changes") or []), "partial": bool(st.get("partial")),
                        "created": st.get("created")})
        return {"steps": out}

    @app.get("/api/worlds/{wid}/steps/{n}")
    def step(wid: str, n: int):
        world_or_404(wid)
        try:
            state = store.load_step(wid, n)
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
        previous = None
        if n > 0:
            try:
                previous = store.load_step(wid, n - 1)
            except NotFound:
                previous = None
        iteration = None
        if n > 0:
            try:
                iteration = _strip_calls(store.load_iteration(wid, n))
            except NotFound:
                iteration = None
        return {"state": state, "previous": previous, "iteration": iteration,
                "latest": (store.step_numbers(wid) or [0])[-1]}

    @app.get("/api/worlds/{wid}/iterations/{n}/calls")
    def iteration_calls(wid: str, n: int):
        world_or_404(wid)
        try:
            return {"calls": store.load_iteration(wid, n).get("calls") or []}
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/api/worlds/{wid}/pending/calls")
    def pending_calls(wid: str):
        world_or_404(wid)
        pending = store.load_pending(wid)
        return {"calls": (pending or {}).get("calls") or []}

    # ------------------------------------------------------------------ running
    @app.post("/api/worlds/{wid}/run")
    async def run(wid: str, request: Request):
        await _body(request)
        world = world_or_404(wid)
        if not any(a.get("enabled") for a in world["actors"]):
            raise HTTPException(400, "no actor is enabled: enable at least one on the Actors page")
        s = load_settings()
        if S.effective_backend(s) == "openrouter" and not s.get("api_key"):
            raise HTTPException(400, "No OpenRouter key: add one in Settings, or choose Mock (no key) there.")
        try:
            return {"run": runs.start(wid, "run")}
        except Busy as e:
            raise HTTPException(409, str(e)) from None

    @app.get("/api/worlds/{wid}/run")
    def run_status(wid: str):
        world_or_404(wid)
        return {"run": runs.status(wid), "busy": runs.busy(wid)}

    @app.post("/api/worlds/{wid}/retry")
    async def retry(wid: str, request: Request):
        await _body(request)
        world_or_404(wid)
        try:
            return {"run": runs.start(wid, "retry")}
        except Busy as e:
            raise HTTPException(409, str(e)) from None
        except NotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.post("/api/worlds/{wid}/discard")
    async def discard(wid: str, request: Request):
        await _body(request)
        world_or_404(wid)
        not_busy(wid)
        store.clear_pending(wid)
        return {"ok": True}

    @app.post("/api/worlds/{wid}/rewind")
    async def rewind(wid: str, request: Request):
        body = await _body(request)
        world_or_404(wid)
        not_busy(wid)
        if body.get("confirm") is not True:
            raise HTTPException(400, 'rewinding drops later steps and needs {"confirm": true}')
        try:
            n = int(body.get("step"))
            dropped = store.rewind(wid, n)
        except (TypeError, ValueError):
            raise HTTPException(400, "step must be an integer") from None
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
        runs.forget(wid)
        return {"ok": True, "dropped": dropped, "step": n}

    @app.post("/api/worlds/{wid}/fork")
    async def fork(wid: str, request: Request):
        body = await _body(request)
        world_or_404(wid)
        try:
            n = int(body.get("step"))
            created = store.fork(wid, n, M._text(body.get("name"), M.LIMITS["name"]) or None)
        except (TypeError, ValueError):
            raise HTTPException(400, "step must be an integer") from None
        except NotFound as e:
            raise HTTPException(404, str(e)) from None
        except (M.WorldError, Conflict) as e:
            raise HTTPException(400, str(e)) from None
        return {"world": created}

    @app.get("/api/worlds/{wid}/estimate")
    def estimate(wid: str):
        world_or_404(wid)
        return engine.estimate(wid)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unknown(rest: str):
        raise HTTPException(404, f"no such API: /api/{rest}")

    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
    return app


def main() -> None:
    import uvicorn
    port = config.port()
    print(f"WorldSim on http://{config.DEFAULT_HOST}:{port}  (data: {config.data_dir()})", flush=True)
    forced = config.forced_backend()
    if forced:
        print(f"LLM backend forced by WORLDSIM_LLM={forced}", flush=True)
    uvicorn.run(create_app(), host=config.DEFAULT_HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
