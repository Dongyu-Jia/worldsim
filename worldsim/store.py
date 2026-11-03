"""Worlds on disk (under config.data_dir(), git-ignored)::

    worlds/<id>/world.json              the world's configuration (model.normalize_world) incl. the initial state
    worlds/<id>/steps/000000.json       the state at each step (step 0 = the initial state)
    worlds/<id>/iterations/000001.json  the iteration that produced step n: actors' outputs, the synthesizer's
                                        answer, flags, changes, every LLM call (prompt, raw response, usage, cost)
    worlds/<id>/pending.json            a failed synthesizer attempt (actors' outputs kept for a retry)
    worlds/<id>/pre_v2/                 world.json and steps/ as they were before a first-format world was migrated

Every write is atomic (temp file + rename). World ids are checked against model.WORLD_ID_RE, so no path escapes.
A world saved in the first format is migrated when it is first loaded (model.migrate_v1: key questions and
attributes become predefined state questions, an action space becomes a decision question); the files as they were
are copied to pre_v2/ first.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from . import config
from . import model as M

_LOCK = threading.RLock()


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    """Atomic JSON write; `mode` (e.g. 0o600) is applied to the temp file before it replaces the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def new_world_id() -> str:
    return "w" + uuid.uuid4().hex[:10]


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else config.data_dir()

    # ------------------------------------------------------------------ paths
    @property
    def worlds_dir(self) -> Path:
        return self.root / "worlds"

    def world_dir(self, wid: str) -> Path:
        if not isinstance(wid, str) or not M.WORLD_ID_RE.match(wid):
            raise NotFound(f"no world {wid!r}")
        return self.worlds_dir / wid

    def _step_path(self, wid: str, n: int) -> Path:
        return self.world_dir(wid) / "steps" / f"{int(n):06d}.json"

    def _iter_path(self, wid: str, n: int) -> Path:
        return self.world_dir(wid) / "iterations" / f"{int(n):06d}.json"

    # ------------------------------------------------------------------ worlds
    def exists(self, wid: str) -> bool:
        try:
            return (self.world_dir(wid) / "world.json").exists()
        except NotFound:
            return False

    def list_ids(self) -> list[str]:
        if not self.worlds_dir.exists():
            return []
        return sorted(p.name for p in self.worlds_dir.iterdir()
                      if p.is_dir() and M.WORLD_ID_RE.match(p.name) and (p / "world.json").exists())

    def list_worlds(self) -> list[dict]:
        out = []
        for wid in self.list_ids():
            try:
                w = self.load_world(wid)
                steps = self.step_numbers(wid)
                last = self.load_step(wid, steps[-1]) if steps else w["initial_state"]
            except (OSError, ValueError, NotFound):
                continue
            out.append({"id": wid, "name": w["name"], "description": w["description"][:300],
                        "language": w["language"], "steps": len(steps) - 1 if steps else 0,
                        "period": last.get("period"), "actors": len(w["actors"]),
                        "fields": len(w["schema"]["fields"]), "created": w.get("created") or "",
                        "updated": w.get("updated") or w.get("created")})
        out.sort(key=lambda r: (r["created"], r["name"]))      # a stable order: the world picker does not jump
        return out

    def load_world(self, wid: str) -> dict:
        path = self.world_dir(wid) / "world.json"
        if not path.exists():
            raise NotFound(f"no world {wid!r}")
        world = read_json(path)
        if world.get("format") != M.FORMAT or M.needs_migration(world) or any(
                M.is_legacy_actor(a) for a in world.get("actors") or []):
            world = self._migrate(wid, world)
        return world

    def _migrate(self, wid: str, raw: dict) -> dict:
        """Bring a world saved in an older format (and its steps) to the current one, on disk. The files as they
        were are copied to pre_v2/ first (once)."""
        with _LOCK:
            d = self.world_dir(wid)
            steps = [read_json(self._step_path(wid, n)) for n in self.step_numbers(wid)]
            world, states = M.migrate_v1(raw, steps)
            norm, problems = M.normalize_world(world)
            if problems:
                raise M.WorldError([f"world {wid} could not be migrated: {p}" for p in problems])
            backup = d / "pre_v2"
            if not backup.exists():
                tmp = d / ".pre_v2.tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir()
                shutil.copy2(d / "world.json", tmp / "world.json")
                if (d / "steps").exists():
                    shutil.copytree(d / "steps", tmp / "steps")
                os.replace(tmp, backup)
            norm["created"] = raw.get("created") or norm.get("created") or now_iso()
            norm["updated"] = raw.get("updated") or norm["created"]
            for st in states:
                state, _ = M.normalize_state(norm["schema"], st, step=int(st.get("step") or 0))
                self.save_step(wid, state)
            write_json(d / "world.json", norm)
            return norm

    def save_world(self, world: dict) -> dict:
        """Normalize and save a world's configuration (raises model.WorldError); keeps step 0 in sync with the
        initial state while the world has no later steps."""
        norm, problems = M.normalize_world(world)
        if problems:
            raise M.WorldError(problems)
        with _LOCK:
            norm["created"] = norm.get("created") or now_iso()
            norm["updated"] = now_iso()
            write_json(self.world_dir(norm["id"]) / "world.json", norm)
            steps = self.step_numbers(norm["id"])
            if not steps or steps == [0]:
                self.save_step(norm["id"], copy.deepcopy(norm["initial_state"]))
        return norm

    def create_world(self, world: dict, history: dict | None = None) -> dict:
        """A new world (a fresh id when the given one is taken or missing), with optional history
        {steps: [state], iterations: [record]} (validated: step numbers 0..n, iterations 1..n)."""
        w = copy.deepcopy(world)
        if M.needs_migration(w):
            steps_raw = (history or {}).get("steps") if isinstance(history, dict) else None
            w, migrated = M.migrate_v1(w, steps_raw if isinstance(steps_raw, list) else [])
            if history and migrated:
                history = {**history, "steps": migrated}
        with _LOCK:
            wid = w.get("id")
            if not isinstance(wid, str) or not M.WORLD_ID_RE.match(wid) or self.exists(wid):
                wid = new_world_id()
            w["id"] = wid
            w["created"] = now_iso()
            norm, problems = M.normalize_world(w)
            if problems:
                raise M.WorldError(problems)
            steps, iters = self._check_history(norm, history)
            write_json(self.world_dir(wid) / "world.json", {**norm, "updated": now_iso()})
            if steps:
                for s in steps:
                    self.save_step(wid, s)
                for it in iters:
                    self.save_iteration(wid, it["step"], it)
            else:
                self.save_step(wid, copy.deepcopy(norm["initial_state"]))
            return self.load_world(wid)

    @staticmethod
    def _check_history(world: dict, history: dict | None) -> tuple[list[dict], list[dict]]:
        if not history:
            return [], []
        steps_raw = history.get("steps") if isinstance(history, dict) else None
        iters_raw = history.get("iterations") if isinstance(history, dict) else None
        if not isinstance(steps_raw, list) or not steps_raw:
            return [], []
        steps = []
        for i, s in enumerate(steps_raw):
            if not isinstance(s, dict) or s.get("step") != i:
                raise M.WorldError(f"history.steps[{i}] must be the state of step {i}")
            state, problems = M.normalize_state(world["schema"], s, step=i)
            if problems:
                raise M.WorldError([f"history step {i}: {p}" for p in problems])
            steps.append(state)
        iters = []
        for it in iters_raw if isinstance(iters_raw, list) else []:
            if isinstance(it, dict) and isinstance(it.get("step"), int) and 1 <= it["step"] < len(steps):
                iters.append(it)
        return steps, iters

    def delete_world(self, wid: str) -> None:
        d = self.world_dir(wid)
        if not (d / "world.json").exists():
            raise NotFound(f"no world {wid!r}")
        with _LOCK:
            shutil.rmtree(d)

    # ------------------------------------------------------------------ steps
    def step_numbers(self, wid: str) -> list[int]:
        d = self.world_dir(wid) / "steps"
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.json"):
            try:
                out.append(int(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def load_step(self, wid: str, n: int) -> dict:
        path = self._step_path(wid, n)
        if not path.exists():
            raise NotFound(f"world {wid} has no step {n}")
        return read_json(path)

    def latest_step(self, wid: str) -> dict:
        steps = self.step_numbers(wid)
        if not steps:
            w = self.load_world(wid)
            self.save_step(wid, copy.deepcopy(w["initial_state"]))
            return copy.deepcopy(w["initial_state"])
        return self.load_step(wid, steps[-1])

    def save_step(self, wid: str, state: dict) -> None:
        write_json(self._step_path(wid, state["step"]), state)

    def load_iteration(self, wid: str, n: int) -> dict:
        path = self._iter_path(wid, n)
        if not path.exists():
            raise NotFound(f"world {wid} has no iteration for step {n}")
        return read_json(path)

    def save_iteration(self, wid: str, n: int, record: dict) -> None:
        write_json(self._iter_path(wid, n), record)

    def append(self, wid: str, state: dict, record: dict) -> None:
        """Save the next step and the iteration that produced it (the iteration first: a step never lacks it)."""
        with _LOCK:
            steps = self.step_numbers(wid)
            expected = (steps[-1] + 1) if steps else 1
            if state["step"] != expected:
                raise Conflict(f"step {state['step']} does not follow step {expected - 1}")
            self.save_iteration(wid, state["step"], record)
            self.save_step(wid, state)
            self.clear_pending(wid)
            self._touch(wid)

    def _touch(self, wid: str) -> None:
        path = self.world_dir(wid) / "world.json"
        w = read_json(path)
        w["updated"] = now_iso()
        write_json(path, w)

    def rewind(self, wid: str, n: int) -> int:
        """Drop every step after n (and their iterations, and a pending failure). Returns how many were dropped."""
        with _LOCK:
            steps = self.step_numbers(wid)
            if n not in steps:
                raise NotFound(f"world {wid} has no step {n}")
            dropped = 0
            for s in steps:
                if s > n:
                    self._step_path(wid, s).unlink(missing_ok=True)
                    self._iter_path(wid, s).unlink(missing_ok=True)
                    dropped += 1
            self.clear_pending(wid)
            self._touch(wid)
            return dropped

    def fork(self, wid: str, n: int, name: str | None = None) -> dict:
        """A new world with this world's configuration and its history up to step n."""
        world = self.load_world(wid)
        steps = [s for s in self.step_numbers(wid) if s <= n]
        if n not in steps:
            raise NotFound(f"world {wid} has no step {n}")
        history = {"steps": [self.load_step(wid, s) for s in steps],
                   "iterations": [self.load_iteration(wid, s) for s in steps if s > 0 and self._iter_path(wid, s).exists()]}
        fork = copy.deepcopy(world)
        fork["id"] = new_world_id()
        fork["name"] = (name or f"{world['name']} (fork @ step {n})")[:M.LIMITS["name"]]
        return self.create_world(fork, history)

    def duplicate(self, wid: str, name: str | None = None) -> dict:
        """A copy of this world's configuration and initial state, without its history."""
        world = copy.deepcopy(self.load_world(wid))
        world["id"] = new_world_id()
        world["name"] = (name or f"{world['name']} (copy)")[:M.LIMITS["name"]]
        return self.create_world(world)

    def export(self, wid: str) -> dict:
        world = self.load_world(wid)
        steps = self.step_numbers(wid)
        iters = []
        for s in steps:
            if s > 0:
                try:
                    iters.append(self.load_iteration(wid, s))
                except NotFound:
                    pass
        return {**world, "history": {"steps": [self.load_step(wid, s) for s in steps], "iterations": iters},
                "exported": now_iso()}

    # ------------------------------------------------------------------ pending (a failed synthesizer attempt)
    def load_pending(self, wid: str) -> dict | None:
        path = self.world_dir(wid) / "pending.json"
        return read_json(path) if path.exists() else None

    def save_pending(self, wid: str, record: dict) -> None:
        write_json(self.world_dir(wid) / "pending.json", record)

    def clear_pending(self, wid: str) -> None:
        (self.world_dir(wid) / "pending.json").unlink(missing_ok=True)

    # ------------------------------------------------------------------ the default world
    def ensure_default(self) -> str | None:
        """On a first start (no worlds at all) create the preloaded default world. Returns its id, or None."""
        if self.list_ids():
            return None
        try:
            world = read_json(config.DEFAULT_WORLD_FILE)
        except (OSError, ValueError):
            return None
        return self.create_world(world)["id"]
