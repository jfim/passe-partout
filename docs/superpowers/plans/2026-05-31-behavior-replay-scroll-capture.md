# Behavior Replay & Scroll-to-Settle Capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a thin server-side primitive that replays human-ish wheel-scroll "behaviors" against a tab via CDP, so a client can trigger below-the-fold lazy content before capturing.

**Architecture:** A new `behaviors.py` module holds a behavior model, a catalog (one built-in + traces loaded from `BEHAVIOR_TRACE_DIR`), per-replay perturbation, and a CDP wheel-replay function. Two routes expose it: `GET /behaviors` (catalog) and `POST /tabs/{id}/behaviors/play` (replay one behavior, returns 204). All policy — content extraction, stop condition, settle, WARC — stays in the client, which reads geometry via the existing `POST /eval` (isolated world). Spec: `docs/superpowers/specs/2026-05-31-behavior-replay-scroll-capture-design.md`.

**Tech Stack:** Python 3.12, FastAPI, nodriver (CDP `Input.dispatchMouseEvent{mouseWheel}`), Pydantic, pytest (`asyncio_mode = "auto"`), uv, ruff.

---

## File Structure

- **Create `src/passe_partout/behaviors.py`** — `Behavior` dataclass, `BehaviorCatalog`, `BUILTIN_SCROLL_DOWN`, `perturb_steps()`, `replay_wheel()`. One responsibility: model + load + replay behaviors.
- **Modify `src/passe_partout/config.py`** — add `behavior_trace_dir` field + `BEHAVIOR_TRACE_DIR` env parsing/validation.
- **Modify `src/passe_partout/models.py`** — add `BehaviorInfo`, `PerturbParams`, `PlayBehaviorRequest`.
- **Modify `src/passe_partout/app.py`** — load catalog into `app.state.behaviors`; add `GET /behaviors` and `POST /tabs/{id}/behaviors/play`.
- **Create `tests/fixtures/tall.html`** — a scrollable page for replay tests.
- **Create `tests/test_behaviors.py`** — catalog + perturbation unit tests + browser-backed replay test.
- **Modify `tests/test_config.py`** — `BEHAVIOR_TRACE_DIR` cases.
- **Modify `tests/test_tab_ops.py`** — `GET /behaviors` and `POST .../behaviors/play` endpoint tests.
- **Modify `CLAUDE.md`** — document the new module, routes, and config var.

Conventions to follow: `from __future__ import annotations` at top of every module; public types as Pydantic models in `models.py`; bare `JSONResponse({"error","detail"})` for errors; ruff clean (`E,W,F,I,B,UP`, line-length 100).

---

## Task 1: Config — `BEHAVIOR_TRACE_DIR`

**Files:**
- Modify: `src/passe_partout/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_behavior_trace_dir_set(monkeypatch, tmp_path):
    d = tmp_path / "traces"
    d.mkdir()
    monkeypatch.delenv("UNPACKED_EXTENSION_DIRS", raising=False)
    monkeypatch.setenv("BEHAVIOR_TRACE_DIR", str(d))
    cfg = Config.from_env()
    assert cfg.behavior_trace_dir == str(d)


def test_behavior_trace_dir_unset(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_TRACE_DIR", raising=False)
    assert Config.from_env().behavior_trace_dir is None


def test_behavior_trace_dir_invalid(monkeypatch, tmp_path):
    monkeypatch.delenv("UNPACKED_EXTENSION_DIRS", raising=False)
    monkeypatch.setenv("BEHAVIOR_TRACE_DIR", str(tmp_path / "does-not-exist"))
    with pytest.raises(ValueError):
        Config.from_env()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k behavior_trace_dir -v`
Expected: FAIL — `Config` has no attribute `behavior_trace_dir`.

- [ ] **Step 3: Implement the config field**

In `src/passe_partout/config.py`, add the field after `shared_profile` (line 27):

```python
    shared_profile: bool = False
    behavior_trace_dir: str | None = None
```

In `from_env`, before the `return cls(`, add validation:

```python
        behavior_trace_dir = os.environ.get("BEHAVIOR_TRACE_DIR") or None
        if behavior_trace_dir is not None and not os.path.isdir(behavior_trace_dir):
            raise ValueError(
                f"BEHAVIOR_TRACE_DIR is not a directory: {behavior_trace_dir}"
            )
```

And add to the `return cls(...)` kwargs:

```python
            shared_profile=shared_profile,
            behavior_trace_dir=behavior_trace_dir,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k behavior_trace_dir -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/config.py tests/test_config.py
git commit -m "feat: add BEHAVIOR_TRACE_DIR config option"
```

---

## Task 2: Behavior model + catalog

**Files:**
- Create: `src/passe_partout/behaviors.py`
- Test: `tests/test_behaviors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_behaviors.py`:

```python
from __future__ import annotations

import json

import pytest

from passe_partout.behaviors import BehaviorCatalog


def test_builtin_scroll_down_present():
    cat = BehaviorCatalog.load(None)
    names = {b.name for b in cat.list()}
    assert "scroll-down" in names
    b = cat.get("scroll-down")
    assert b is not None
    assert b.source == "builtin"
    assert b.kind == "scroll-down"
    assert len(b.steps) > 0
    # built-in scrolls downward (positive delta_y)
    assert all(dy > 0 for _, dy, _ in b.steps)


def test_load_recorded_trace(tmp_path):
    (tmp_path / "myscroll.json").write_text(
        json.dumps({"kind": "scroll-up", "steps": [[0, -100, 12], [0, -90, 20]]})
    )
    cat = BehaviorCatalog.load(str(tmp_path))
    b = cat.get("myscroll")
    assert b is not None
    assert b.source == "recorded"
    assert b.kind == "scroll-up"
    assert b.steps[0] == (0.0, -100.0, 12.0)


def test_unknown_behavior_returns_none():
    assert BehaviorCatalog.load(None).get("nope") is None


def test_invalid_kind_rejected(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"kind": "teleport", "steps": []}))
    with pytest.raises(ValueError):
        BehaviorCatalog.load(str(tmp_path))


def test_non_json_files_ignored(tmp_path):
    (tmp_path / "README.txt").write_text("not a trace")
    cat = BehaviorCatalog.load(str(tmp_path))
    assert {b.name for b in cat.list()} == {"scroll-down"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_behaviors.py -v`
Expected: FAIL — `No module named 'passe_partout.behaviors'`.

- [ ] **Step 3: Implement the module**

Create `src/passe_partout/behaviors.py`:

```python
"""Replayable input behaviors (wheel-scroll traces) and a catalog over them.

A Behavior is a parameterless sequence of relative wheel steps
(delta_x, delta_y, dt_ms) that the play endpoint replays via CDP. One built-in
ships (honestly-synthetic, evenly-spaced scroll-down); the rest are loaded from
BEHAVIOR_TRACE_DIR so realistic traces stay operator-private and are never
shipped (avoids a shared cross-client fingerprint).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# (delta_x, delta_y, dt_ms): relative wheel deltas and the pause before the next step.
WheelStep = tuple[float, float, float]

VALID_KINDS = frozenset({"scroll-down", "scroll-up", "jitter", "wheel-scrub"})


@dataclass(frozen=True)
class Behavior:
    name: str
    kind: str  # one of VALID_KINDS
    source: str  # "builtin" | "recorded"
    steps: tuple[WheelStep, ...]


# Honestly-synthetic default: evenly-spaced downward wheel, enough to trip
# IntersectionObserver lazy-load. Makes no claim to be human.
BUILTIN_SCROLL_DOWN = Behavior(
    name="scroll-down",
    kind="scroll-down",
    source="builtin",
    steps=tuple((0.0, 120.0, 16.0) for _ in range(40)),
)


class BehaviorCatalog:
    def __init__(self, behaviors: dict[str, Behavior]) -> None:
        self._behaviors = behaviors

    @classmethod
    def load(cls, trace_dir: str | None) -> BehaviorCatalog:
        items: dict[str, Behavior] = {BUILTIN_SCROLL_DOWN.name: BUILTIN_SCROLL_DOWN}
        if trace_dir:
            for entry in sorted(os.listdir(trace_dir)):
                if not entry.endswith(".json"):
                    continue
                name = entry[:-5]
                with open(os.path.join(trace_dir, entry)) as f:
                    data = json.load(f)
                kind = data.get("kind", "scroll-down")
                if kind not in VALID_KINDS:
                    raise ValueError(f"behavior trace {entry!r} has invalid kind {kind!r}")
                steps = tuple(
                    (float(dx), float(dy), float(dt)) for dx, dy, dt in data["steps"]
                )
                items[name] = Behavior(name=name, kind=kind, source="recorded", steps=steps)
        return cls(items)

    def list(self) -> list[Behavior]:
        return list(self._behaviors.values())

    def get(self, name: str) -> Behavior | None:
        return self._behaviors.get(name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_behaviors.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/behaviors.py tests/test_behaviors.py
git commit -m "feat: add Behavior model and BehaviorCatalog"
```

---

## Task 3: Per-replay perturbation

**Files:**
- Modify: `src/passe_partout/behaviors.py`
- Test: `tests/test_behaviors.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_behaviors.py`:

```python
from passe_partout.behaviors import perturb_steps  # noqa: E402


def test_perturb_disabled_is_passthrough():
    steps = ((0.0, 120.0, 16.0), (0.0, 120.0, 16.0))
    assert perturb_steps(steps, enabled=False) == list(steps)


def test_perturb_is_deterministic_with_seed():
    steps = tuple((0.0, 120.0, 16.0) for _ in range(5))
    a = perturb_steps(steps, seed=7)
    b = perturb_steps(steps, seed=7)
    assert a == b
    assert any(o != s for o, s in zip(a, steps))  # actually perturbed


def test_perturb_stays_within_bounds():
    steps = ((0.0, 100.0, 10.0),)
    dx, dy, dt = perturb_steps(steps, time_warp=0.2, delta_scale=0.1, seed=1)[0]
    assert 90.0 <= dy <= 110.0
    assert 8.0 <= dt <= 12.0
    assert dt >= 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_behaviors.py -k perturb -v`
Expected: FAIL — `cannot import name 'perturb_steps'`.

- [ ] **Step 3: Implement `perturb_steps`**

Add `import random` to the imports in `src/passe_partout/behaviors.py` (keep import order: `json`, `os`, `random`), then append:

```python
def perturb_steps(
    steps: tuple[WheelStep, ...],
    *,
    enabled: bool = True,
    time_warp: float = 0.15,
    delta_scale: float = 0.10,
    seed: int | None = None,
) -> list[WheelStep]:
    """Return a jittered copy of `steps`. Each step's deltas and gap are scaled
    by an independent factor in [1-x, 1+x] so repeated replays of the same trace
    are never byte-identical. Deterministic for a fixed `seed`."""
    if not enabled:
        return list(steps)
    rng = random.Random(seed)
    out: list[WheelStep] = []
    for dx, dy, dt in steps:
        ds = 1.0 + rng.uniform(-delta_scale, delta_scale)
        tw = 1.0 + rng.uniform(-time_warp, time_warp)
        out.append((dx * ds, dy * ds, max(0.0, dt * tw)))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_behaviors.py -k perturb -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/behaviors.py tests/test_behaviors.py
git commit -m "feat: add per-replay behavior perturbation"
```

---

## Task 4: CDP wheel replay + scrollable fixture

**Files:**
- Create: `tests/fixtures/tall.html`
- Modify: `src/passe_partout/behaviors.py`
- Test: `tests/test_behaviors.py`

- [ ] **Step 1: Create the scrollable fixture**

Create `tests/fixtures/tall.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>tall</title>
    <style>
      body { margin: 0; }
      #spacer { height: 5000px; }
    </style>
  </head>
  <body>
    <div id="spacer">tall page for scroll tests</div>
  </body>
</html>
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_behaviors.py`:

```python
from passe_partout.behaviors import BUILTIN_SCROLL_DOWN, replay_wheel  # noqa: E402


async def test_replay_wheel_scrolls_page(browser_pool):
    tall = (
        "data:text/html,<html><head><style>body{margin:0}#s{height:5000px}</style>"
        "</head><body><div id=s>tall</div></body></html>"
    )
    tab = await browser_pool.create_context(tall)
    try:
        import asyncio

        before = await tab.evaluate("window.scrollY")
        await replay_wheel(tab, list(BUILTIN_SCROLL_DOWN.steps))
        await asyncio.sleep(0.3)  # let the compositor commit the scroll
        after = await tab.evaluate("window.scrollY")
        assert before == 0
        assert after > before
    finally:
        await browser_pool.close_context(tab)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_behaviors.py -k replay_wheel_scrolls -v`
Expected: FAIL — `cannot import name 'replay_wheel'`.

- [ ] **Step 4: Implement `replay_wheel`**

Add to the imports in `src/passe_partout/behaviors.py`: `import asyncio` (top of the stdlib group) and `import nodriver as uc` (after the stdlib imports, blank line between groups). Then append:

```python
async def replay_wheel(
    tab: uc.Tab,
    steps: list[WheelStep],
    *,
    anchor: tuple[float, float] = (100.0, 100.0),
) -> None:
    """Replay wheel `steps` against `tab` via trusted CDP wheel events, paced by
    each step's dt_ms. `anchor` is the cursor point the wheel dispatches at; any
    point over the scrollable document works."""
    ax, ay = anchor
    for dx, dy, dt in steps:
        await tab.send(
            uc.cdp.input_.dispatch_mouse_event(
                type_="mouseWheel", x=ax, y=ay, delta_x=dx, delta_y=dy
            )
        )
        if dt > 0:
            await asyncio.sleep(dt / 1000.0)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_behaviors.py -k replay_wheel_scrolls -v`
Expected: PASS. (Launches real Chromium via the `browser_pool` fixture.)

- [ ] **Step 6: Commit**

```bash
git add src/passe_partout/behaviors.py tests/test_behaviors.py tests/fixtures/tall.html
git commit -m "feat: add CDP wheel replay and scrollable test fixture"
```

---

## Task 5: API models

**Files:**
- Modify: `src/passe_partout/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py`:

```python
from passe_partout.models import BehaviorInfo, PerturbParams, PlayBehaviorRequest


def test_play_behavior_request_defaults():
    req = PlayBehaviorRequest(name="scroll-down")
    assert req.name == "scroll-down"
    assert req.perturb is None


def test_perturb_params_defaults_and_bounds():
    p = PerturbParams()
    assert p.enabled is True
    assert p.time_warp is None and p.delta_scale is None and p.seed is None
    with pytest.raises(ValueError):
        PerturbParams(time_warp=2.0)  # > 1.0


def test_play_behavior_request_rejects_blank_name():
    with pytest.raises(ValueError):
        PlayBehaviorRequest(name="")


def test_behavior_info_roundtrip():
    info = BehaviorInfo(name="scroll-down", kind="scroll-down", source="builtin")
    assert info.model_dump() == {
        "name": "scroll-down",
        "kind": "scroll-down",
        "source": "builtin",
    }
```

Ensure `tests/test_models.py` imports `pytest` (it likely already does; if not, add `import pytest` at the top).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k "behavior or perturb or play" -v`
Expected: FAIL — `cannot import name 'BehaviorInfo'`.

- [ ] **Step 3: Implement the models**

In `src/passe_partout/models.py`, add a bound constant next to the others (after line 19):

```python
BEHAVIOR_NAME_MAX = 128
```

Add the models after `EvalResponse` (line 153):

```python
class BehaviorInfo(BaseModel):
    name: str
    kind: str
    source: str


class PerturbParams(BaseModel):
    enabled: bool = True
    time_warp: float | None = Field(default=None, ge=0.0, le=1.0)
    delta_scale: float | None = Field(default=None, ge=0.0, le=1.0)
    seed: int | None = None


class PlayBehaviorRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=BEHAVIOR_NAME_MAX), NoControl]
    perturb: PerturbParams | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -k "behavior or perturb or play" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/models.py tests/test_models.py
git commit -m "feat: add behavior API models"
```

---

## Task 6: `GET /behaviors` endpoint + catalog wiring

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tab_ops.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tab_ops.py`:

```python
async def test_list_behaviors_includes_builtin(client):
    r = await client.get("/behaviors")
    assert r.status_code == 200
    items = r.json()
    by_name = {b["name"]: b for b in items}
    assert "scroll-down" in by_name
    assert by_name["scroll-down"]["source"] == "builtin"
    assert by_name["scroll-down"]["kind"] == "scroll-down"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tab_ops.py -k list_behaviors -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Wire the catalog and add the route**

In `src/passe_partout/app.py`, add imports. After line 24 (`from passe_partout.isolated import ...`):

```python
from passe_partout.behaviors import BehaviorCatalog, perturb_steps, replay_wheel
```

Add to the `models` import block (lines 25-46) — insert alphabetically among the names:

```python
    BehaviorInfo,
    PerturbParams,
    PlayBehaviorRequest,
```

In `lifespan`, after `app.state.recorder.set_registry(app.state.registry)` (line 114), add:

```python
        app.state.behaviors = BehaviorCatalog.load(cfg.behavior_trace_dir)
```

Add the route near the other top-level (non-tab) routes — e.g. right after the `list_tabs` handler (after line 211):

```python
    @app.get(
        "/behaviors",
        response_model=list[BehaviorInfo],
        summary="List available scroll/input behaviors",
    )
    async def list_behaviors():
        return [
            BehaviorInfo(name=b.name, kind=b.kind, source=b.source)
            for b in app.state.behaviors.list()
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tab_ops.py -k list_behaviors -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/app.py tests/test_tab_ops.py
git commit -m "feat: add GET /behaviors endpoint"
```

---

## Task 7: `POST /tabs/{id}/behaviors/play` endpoint

**Files:**
- Modify: `src/passe_partout/app.py`
- Test: `tests/test_tab_ops.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tab_ops.py`:

```python
async def test_play_behavior_scrolls_tab(client, fixture_server):
    import asyncio

    tid = await _open(client, f"{fixture_server}/tall.html")
    try:
        before = await client.post(
            f"/tabs/{tid}/eval", json={"js": "window.scrollY", "world": "isolated"}
        )
        assert before.json()["result"] == 0

        r = await client.post(
            f"/tabs/{tid}/behaviors/play",
            json={"name": "scroll-down", "perturb": {"enabled": False}},
        )
        assert r.status_code == 204

        await asyncio.sleep(0.3)
        after = await client.post(
            f"/tabs/{tid}/eval", json={"js": "window.scrollY", "world": "isolated"}
        )
        assert after.json()["result"] > 0
    finally:
        await _close(client, tid)


async def test_play_behavior_unknown_tab_404(client):
    r = await client.post("/tabs/999999/behaviors/play", json={"name": "scroll-down"})
    assert r.status_code == 404


async def test_play_behavior_unknown_behavior_404(client, fixture_server):
    tid = await _open(client, f"{fixture_server}/tall.html")
    try:
        r = await client.post(f"/tabs/{tid}/behaviors/play", json={"name": "no-such-behavior"})
        assert r.status_code == 404
        assert r.json()["error"] == "behavior_not_found"
    finally:
        await _close(client, tid)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tab_ops.py -k play_behavior -v`
Expected: FAIL — 404 for all (route not defined; note the "unknown tab" case may coincidentally 404 but the happy-path and unknown-behavior cases will fail).

- [ ] **Step 3: Add the route**

In `src/passe_partout/app.py`, add after the `eval_js` handler (after line 764):

```python
    @app.post(
        "/tabs/{tab_id}/behaviors/play",
        status_code=204,
        summary="Replay a behavior (e.g. scroll burst) against the tab",
    )
    async def play_behavior(tab_id: int, req: PlayBehaviorRequest):
        rec = await _require_tab(tab_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"error": "tab_not_found", "detail": ""})
        behavior = app.state.behaviors.get(req.name)
        if behavior is None:
            return JSONResponse(
                status_code=404,
                content={"error": "behavior_not_found", "detail": req.name},
            )
        p = req.perturb or PerturbParams()
        steps = perturb_steps(
            behavior.steps,
            enabled=p.enabled,
            time_warp=p.time_warp if p.time_warp is not None else 0.15,
            delta_scale=p.delta_scale if p.delta_scale is not None else 0.10,
            seed=p.seed,
        )
        async with rec.lock:
            try:
                await replay_wheel(rec.tab, steps)
            except Exception as e:
                return JSONResponse(
                    status_code=502, content={"error": "browser_error", "detail": str(e)}
                )
            # Bump TTL: the idle sweeper closes tabs purely on last_used_at without
            # taking rec.lock, so a long client-driven scroll session must keep the
            # tab alive across bursts.
            app.state.registry.touch(tab_id)
        return Response(status_code=204)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tab_ops.py -k play_behavior -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/passe_partout/app.py tests/test_tab_ops.py
git commit -m "feat: add POST /tabs/{id}/behaviors/play endpoint"
```

---

## Task 8: Lint, full suite, and docs

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean. If format check fails, run `uv run ruff format .` and re-stage.

- [ ] **Step 2: Run the full (non-smoke) suite**

Run: `uv run pytest`
Expected: all pass (existing + new). Investigate any failure before continuing.

- [ ] **Step 3: Document in CLAUDE.md**

In `CLAUDE.md`, under the Architecture component list, add a bullet describing the new module (place it after the `ResourceRecorder` bullet):

```markdown
- **`BehaviorCatalog` (`behaviors.py`)** — owns the catalog of replayable wheel-scroll behaviors: one honestly-synthetic built-in (`scroll-down`, evenly-spaced) plus traces loaded at startup from `BEHAVIOR_TRACE_DIR` (operator-private; never shipped, to avoid a shared cross-client fingerprint). `GET /behaviors` lists them; `POST /tabs/{id}/behaviors/play` replays one burst against a tab via trusted CDP `Input.dispatchMouseEvent{mouseWheel}`, perturbing the trace (time-warp/delta-scale, seedable) on each replay. The endpoint is a thin mechanism: all policy — content extraction, stop condition, settle (network-idle), WARC — stays in the client, which reads geometry via `POST /tabs/{id}/eval` in the isolated world. Input must be CDP-side because in-page JS can't reach CDP and JS-synthesized events are `isTrusted:false`.
```

In the env-var paragraph that mentions `DOWNLOAD_DIR`, add a sentence:

```markdown
`BEHAVIOR_TRACE_DIR` (unset by default) points at a directory of operator-recorded behavior trace JSON files (`{"kind": ..., "steps": [[dx, dy, dt_ms], ...]}`), loaded at startup and surfaced via `GET /behaviors`.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document behavior replay module, routes, and config"
```

---

## Self-Review (completed during planning)

- **Spec coverage:** `GET /behaviors` (Task 6) ✓; `POST .../behaviors/play` → 204 (Task 7) ✓; reads via isolated `eval` (used by tests, no new endpoint) ✓; `BEHAVIOR_TRACE_DIR` (Task 1) ✓; one built-in `scroll-down`, no canned realistic traces (Task 2) ✓; trace format `(dx, dy, dt_ms)` + perturbation on replay (Tasks 2–3) ✓; CDP-side trusted input (Task 4) ✓; TTL `touch()` so the sweeper doesn't reap mid-session (Task 7) ✓; behavior-vs-targeted-action split honored — only behaviors implemented, move/click left to future work per spec ✓.
- **Out of scope (per spec), intentionally not in this plan:** targeted move/click actions, trace upload endpoint, recorder tooling, the client-side orchestration loop (readability, stop condition, settle) — those live in the client.
- **Placeholder scan:** none — every code/test step contains complete code.
- **Type consistency:** `Behavior`, `BehaviorCatalog.load/list/get`, `BUILTIN_SCROLL_DOWN`, `perturb_steps(enabled,time_warp,delta_scale,seed)`, `replay_wheel(tab,steps,anchor)`, `BehaviorInfo`, `PerturbParams`, `PlayBehaviorRequest`, `behavior_trace_dir` / `BEHAVIOR_TRACE_DIR`, error code `behavior_not_found` — names match across all tasks.

## Notes for the implementer

- `browser_pool` is a session-scoped fixture that launches real Chromium; the replay and play tests use it (and are deselected only by `-m smoke`, which these are not). They run in the default suite.
- The 0.3s sleeps in the scroll tests absorb the compositor's async scroll commit; don't remove them or the `scrollY` read can race.
- Wheel `dt_ms` of 16 over 40 steps means the built-in replay takes ~0.6s — fine for a single burst. Clients pace multiple bursts themselves.
