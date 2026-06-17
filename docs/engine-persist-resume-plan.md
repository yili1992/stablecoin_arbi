# Plan — Engine crash-safe persistence & restart resume

**Status:** DRAFT (awaiting plan-review + owner approval; no production code yet)
**Branch:** `worktree-dev-engine-persist-resume`
**Owner decision locked:** driving toward **real live trading** → correctness bar = full state recovery; depth = **full resume**.

---

## 1. Problem (root cause, confirmed)

`PaperEngine` (the `paper`/`live` service in `src/sca/live/engine.py`) keeps **all** state in memory and
**never reloads it on startup**:

- `__init__`: `events=[]`, `history=[]`, `slices=[]`, `realized_capture=0.0`, `interest=DailyMinInterest(...)` (fresh).
- `bootstrap()`: REST-loads klines for the EMA anchor **and calls `_deploy()` which resets `slices`**.
- Main loop `_tick()` → `write_status()` every `STATUS_EVERY = 12 s` truncate-writes `status_<symbol>.json`
  from the (empty-after-restart) in-memory state. `_write_csv()` truncate-writes **only at run end**.

⇒ Any restart starts empty and, within 12 s, overwrites the persisted snapshot with empty data. A long
`--seconds 604800` run that is restarted before completion **never writes the CSV at all**. The compose
bind-mount `./out:/app/out` was always present, so the *files* survived on the host — **the engine clobbered
them**. The already-lost data is unrecoverable (no container/volume/file remains).

Matches knowledge: `runtime-state-file-overwritten-by-memory`, `usd1-interest-min-hourly-snapshot-daily`.

## 2. Goal

A restart (process restart, `docker restart`, or `down`+`up`/recreate **as long as `out_dir` is on a
persistent mount**) resumes seamlessly:

- Position (`slices`, `deployed`, `realized_capture`) restored exactly.
- USD1 carry (`DailyMinInterest` internal state) restored exactly — no forfeited day from a reset.
- Dashboard history + full trade ledger preserved and continued, never regressed to empty.
- Crash durability: data on disk is current to the **last appended fill**, not "last completed run".

Non-goal (this plan): wiring real Bybit order placement. See **R1** — that requires exchange reconciliation
on top of this and is a separate, explicitly-authorized task.

## 3. Design

### 3.1 Two artifacts in `out_dir` (the persistent mount)

| File | Write mode | Role | Reload |
|---|---|---|---|
| `<symbol>_state.json` | atomic tmp+rename, **on every fill** + every `STATUS_EVERY` | full reconstructable snapshot | **sole** resume source |
| `<symbol>_events.jsonl` | **append-only** (`"a"` + flush) per fill, uncapped | durable audit ledger / CSV source | tail → repopulate in-mem `events` only |
| `status_<symbol>.json` | unchanged (atomic, every 12s) | dashboard projection | (read-only by dashboard) |
| `<symbol>_adv.csv` | derived export, now **periodic** not end-only | human/CSV consumer | — |

**Key simplification (ce-review):** because `state.json` is snapshotted **synchronously on every fill**, the
snapshot is always ≥ the ledger ⇒ **no event-replay / `event_seq` reconciliation on resume**. Resume = "load
the snapshot." The ledger is append-only audit + CSV source only; `read_events` just repopulates the capped
in-memory `events` for the status doc. (Fills are rare — only on rung crossings — so a synchronous snapshot per
fill is cheap.)

`state.json` schema (versioned):
```json
{ "v": 1, "symbol": "...", "mode": "paper|live", "start": 1.0, "deployed": true,
  "realized_capture": 0.0,
  "slices": [{"state":"usd1|usdt","qty":0,"cash":0,"sell_px":0,"entry":null}],
  "interest": {"daily_rate":0,"settled":0,"_snap_hour":0,"_day_idx":0,
               "_day_hours":[0,1,...],"_day_min":0.0},
  "anchor": 1.0, "ema": 1.0, "last_1h_start": 0,
  "history": [...], "done": [["buy",1.0,{"5":0.1,"30":0.2}]], "event_seq": 0 }
```

### 3.2 `DailyMinInterest` serialization (`src/sca/interest.py`)

Add `to_dict()` / `from_dict(d)`:
- `_day_hours` (a `set`) ⇄ sorted list; `_day_min` may be `None`.
- Restore the persisted `daily_rate` (the rate `settled` was accrued under) — do **not** silently re-derive
  from a possibly-changed config APR mid-run. Changing APR ⇒ requires a fresh start (documented).
- Round-trip MUST be exact; `observe()` after `from_dict` continues as if uninterrupted (no forfeited day).
- Pure data, no behavior change to the model itself.
- **Init order:** `__init__` constructs a fresh `DailyMinInterest(APR/365)`; `_maybe_resume()` (called at the
  END of `__init__`) **replaces** it via `from_dict`. Resume path must run after the default is set.

### 3.3 New module `src/sca/live/persistence.py`

- `atomic_write_json(path, doc)` — tmp+`os.replace`.
- `save_state(engine, dir)` — serialize the fields above.
- `load_state(dir, symbol) -> dict | None` — return None if absent; tolerate corrupt/partial file (log + None).
- `append_event(dir, symbol, event)` — open `"a"`, write one JSON line, `flush()` + `os.fsync` (best-effort).
- `read_events(dir, symbol) -> list` — tolerate a trailing partial line (skip it).

### 3.4 Engine wiring (`src/sca/live/engine.py`) — additive, gated by file presence + `live.persist`

1. `__init__`: after defaults, call `self._maybe_resume()`:
   - `st = load_state(out_dir, symbol)`. If present & `live.persist`: restore `slices, deployed,
     realized_capture, start, history, done, anchor/ema/last_1h_start`; `self.interest =
     DailyMinInterest.from_dict(st["interest"])`; `self.events = read_events(...)[-EVENTS_CAP:]`;
     `self._event_seq = st["event_seq"]`; set `self._resumed = True`.
2. `bootstrap()`: **guard the deploy** — `if not self._resumed: self._deploy(deploy_px)`. Always rebuild
   anchor/klines from REST (more correct than the persisted anchor; persisted anchor is fallback if REST fails).
3. `_log_event()`: also `append_event(...)` **and `save_state(...)` synchronously** (snapshot ≥ ledger ⇒ no
   replay needed on resume; fills are rare).
4. `write_status()`: after writing status, also `save_state(...)` (covers history/markout drift between
   fills). Status doc is now seeded from restored history/events ⇒ never regresses to empty. **`status_doc`
   keys are unchanged — dashboard contract preserved.**
5. CSV: write periodically (derive from in-memory `events`, or stream from the ledger) in addition to final.
6. `seconds <= 0` ⇒ run indefinitely (recommended for live); `t_end` computed from the **persisted** `start`.
   Backward-compat: positive `seconds` keeps current finite-window semantics from the persisted start.

### 3.5 Config (`config/strategy.yaml`, data-only)

```yaml
live:
  persist: true            # default on; false = legacy in-memory-only behavior
  run_forever: false       # true (or --seconds 0) => ignore the finite window
```

### 3.6 Durability (`docker-compose.yml`)

`./out:/app/out` already persists on the host across recreate — keep it (host-inspectable). Document that the
SAME `out_dir`/symbol must be used on restart. (Named volume is an alternative; bind-mount is sufficient and
visible — no change required, just documented.)

## 4. Impact

- `paper`/`live` engine startup + write paths. **Default-off-safe**: with no state file (first run) behavior is
  byte-identical to today (fresh deploy). Existing smoke/interest invariants must stay green.
- Dashboard unaffected (still reads `status_*.json`).
- `dryrun.py` CSV-only-at-end has the same class of bug but is **measurement-only** and lower-stakes →
  *optional* secondary item (periodic CSV flush), not in the critical path. Flag, don't bundle.

## 5. Risks & dependencies

- **R1 (LIVE BLOCKER, must surface):** local state file is **necessary but NOT sufficient** for *real* orders.
  When real placement is wired, startup MUST **reconcile local position against the exchange** (query real
  balances + open orders) — real fills can occur during downtime that the file never saw. Today fills are
  simulated/deterministic, so the file is sufficient for paper + the gated scaffold. **This plan does not make
  flipping the live switch safe by itself.** (cf. `api-failure-tristate`, `recovery-position-check`.)
- **R2:** `DailyMinInterest.observe` backfills skipped hours with the *current* qty. A multi-hour restart gap
  backfills with the post-restart holding — exact for paper (engine down = no fills), approximate for live
  (covered by R1). Document.
- **R3:** anchor recompute (REST) may differ slightly from the pre-crash anchor → a borderline fill triggers a
  tick later. Deterministic from closed candles; acceptable.
- **R4:** crash mid-write — `state.json` atomic (tmp+rename, so it is never half-written; a crash leaves the
  *previous* complete snapshot). `events.jsonl` per-line append+flush; `read_events` tolerates a trailing
  partial line. Because the snapshot is taken synchronously on every fill, a crash loses **at most** the
  markout/history drift since the last 12s write — never a position-changing fill. Tested.
- **R6 (APR change):** persisted `daily_rate` is authoritative within a run; changing `strategy.interest_apr`
  across a restart needs a fresh start (delete the state file) — documented, not auto-handled.
- **R5:** backward compat — feature is additive + presence/flag-gated; default path unchanged.

## 6. Verification (TDD — RED first)

1. `interest.to_dict/from_dict` round-trip exact; `observe()` after restore continues correctly (no lost day).
2. `save_state`→`load_state` reconstructs `slices/realized_capture/deployed/start` identically.
3. **Bug regression:** engine with N fills → save → fresh engine load → `status_doc` is **non-empty** and
   position/realized/interest equal; immediate `write_status()` does NOT zero the files.
4. `bootstrap()` on a resumed engine does **not** reset `slices`.
5. `events.jsonl` is append-only and grows across restarts; `read_events` skips a trailing partial line.
6. Existing `tests/test_smoke.py`, `test_interest_accrual.py`, `test_interest_model.py` stay green.

Test cmd: `pytest -q` (project uses plain pytest; tests under `tests/`).

## 7. Rollback

Single feature branch; merge `--no-ff`. Revert = drop the merge commit. Runtime kill-switch:
`live.persist: false` restores legacy in-memory-only behavior without code changes.

## 8. Execution mode

~6–7 files (interest.py, persistence.py [new], engine.py, strategy.yaml, tests [new], docs, maybe compose) but
tightly coupled around one state machine. Lean **紧凑/single careful implementer with strict TDD**; revisit if
it fragments. Owner approval of direction = hard gate #1 (this doc). Codex heterogeneous review before
implementation; full ce+qa+Codex review before the `--no-ff` merge (hard gate #2).
