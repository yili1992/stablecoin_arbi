# Plan — Config consolidation: `strategy.yaml` as the single non-secret source

**Branch:** `worktree-config-consolidation`
**Boss decisions:** all non-secret config → `config/strategy.yaml`; **secrets stay env-only** (yaml only names the env vars — unchanged); full process (plan → review → change).

---

## 1. Goal / Non-goals
**Goal**
- `config/strategy.yaml` becomes the single authoritative source for ALL non-secret config (launch symbol/seconds/mode/port/out_dir included).
- Slim `.env` to **secrets-only**; fix `.gitignore` (`.env` is currently NOT ignored → key-leak risk once Phase 3 keys land).

**Non-goals**
- No change to strategy/trading logic, the live safety gate, or the R1 reconcile.
- Secrets never move into a committed file. `SCA_CONFIG` (bootstrap path to the yaml itself) stays env-only — it cannot live inside the file it points to.

## 2. Current scatter (grounded)
- **engine** reads its launch defaults from the **`dryrun:`** block: `DEFAULT_SYMBOL=_D.get("symbol")`, `DEFAULT_SECONDS=_D.get("seconds")` (engine.py) → confusingly borrows the measurement tool's block; compose then overrides with `--symbol/--seconds/--mode` from `.env`.
- **`.env`/compose**: `SYMBOL/SECONDS/MODE/DASHBOARD_PORT` (redundant overrides) + `SCA_OUT_DIR/DATA/CONFIG` (paths).
- **out_dir** default is inconsistent: engine `"."`, dashboard `"./out"`.
- **secrets**: env (correct — stays).

## 3. Design
1. **New `runtime:` block** in `strategy.yaml` — the single launch-defaults source:
   ```yaml
   runtime:
     symbol: USD1USDT       # default symbol for paper/live engine
     seconds: 604800        # paper/live run length (7d); 0 = forever
     mode: paper            # paper | live  (sca live / --mode still override)
     dashboard_port: 3015
     # out_dir: ./out       # OPTIONAL unifier — UNSET by default so per-caller fallbacks
     #                        (engine ".", dashboard "./out") DON'T shift → no orphaned
     #                        bare-metal resume state (Codex P1). env SCA_OUT_DIR still wins.
   ```
   (The `dryrun:` block keeps its measurement-specific knobs: its own `seconds` (1-day measure), `horizons_sec`, `ws_url`. **`dryrun.symbol` is removed** — dryrun reads `runtime.symbol` (single symbol source). **The vestigial `live.mode` is removed** — confirmed read nowhere; `runtime.mode` is the sole mode default.)
2. **`config.py` single resolvers (functions, for testability + call-time env reads):**
   - `out_dir(fallback=".")` → `env SCA_OUT_DIR` **or** `runtime.out_dir` (only if set) **or** `fallback`. **Per-caller fallback preserved** (engine `"."`, dashboard `"./out"`) → defaults don't shift, no orphaned state (Codex P1). Callers keep `dirname(csv_path)` FIRST when a csv path is given (tmp_path test isolation preserved).
   - `runtime(cfg=None)` → resolved `{symbol, seconds, mode, dashboard_port}` (defaults baked); `cfg` injectable for tests.
   - **mode resolution (explicit — Codex P1):** engine `--mode` default = `env MODE` **or** `runtime.mode` **or** `"paper"`, validated to {paper,live}. Keeps docker live-enable (`.env MODE=live`) working after compose drops `--mode`. Armed gate UNCHANGED: `live_authorization` still needs `mode==live` AND confirm AND keys.
3. **Point readers at the single source:**
   - `engine.py`: `DEFAULT_SYMBOL/DEFAULT_SECONDS` from `runtime` (not `dryrun`); `main()` `--mode` default = `env MODE | runtime.mode | "paper"`; `__init__` out_dir = `dirname(csv_path) if csv_path else config.out_dir(".")` (csv precedence preserved — Codex P1).
   - `dryrun.py`: symbol default from `runtime.symbol` (keep its own `seconds`/`horizons`/`ws_url`); out_dir via `config.out_dir(".")`.
   - `dashboard.py`: `--out-dir` default `config.out_dir("./out")`; port default `runtime.dashboard_port`.
4. **`docker-compose.yml`:** `paper` command → drop `--symbol/--seconds/--mode` **and `--csv`** (engine reads yaml; the `--csv ${SYMBOL}_adv.csv` would otherwise diverge from `runtime.symbol` — Codex P2; the dashboard reads `status_<symbol>.json` regardless). `dashboard` command → drop `--port` so yaml `runtime.dashboard_port` drives it (Codex P2). Keep `SCA_OUT_DIR=/app/out` + `env_file: .env` (secrets incl. `MODE=live`) + host-port map `${DASHBOARD_PORT:-3015}:3015`.
5. **`.env.example`:** slim to the **live-enable bundle only** (`MODE=live`, `LIVE_TRADING_CONFIRM=yes`, `BYBIT_API_KEY`, `BYBIT_API_SECRET`) + one line: "all non-secret config lives in `config/strategy.yaml`". `MODE`/`SCA_OUT_DIR` remain *optional env overrides* of the yaml.
6. **`.gitignore`:** add `.env` (keep `.env.example` tracked).
7. **Docs (Codex P2):** update `README.md` / `ONBOARDING.md` / compose comments that point users at `SYMBOL`/`SECONDS` env knobs → redirect to `config/strategy.yaml` (`runtime:`), keeping the `.env` `MODE=live`+keys live-enable note.

## 4. Backward-compat / safety
- **Env still overrides yaml** everywhere → docker unaffected (it sets `SCA_OUT_DIR=/app/out`; that still wins).
- **Live safety gate UNCHANGED:** `runtime.mode` default `paper`; `sca live` forces `--mode live`; armed still needs confirm + keys. Mode-default move is paper→paper (cosmetic).
- Removing compose `--symbol/--seconds/--mode` → engine falls back to `runtime.*` (same values: 604800/USD1USDT/paper) → **docker launches identically**.

## 5. Tests (TDD)
- `config.out_dir(fallback)` precedence: env `SCA_OUT_DIR` > `runtime.out_dir` (if set) > `fallback`; unset runtime → returns the caller's fallback (engine `"."`, dashboard `"./out"`).
- engine `__init__`: `csv_path` dirname still wins over `out_dir()` (preserve tmp_path isolation — the existing resume tests must stay green).
- `config.runtime(cfg)`: defaults from a loaded yaml; fallbacks when `runtime:` absent.
- **mode resolution**: `env MODE=live` → main `--mode` default `live`; unset → `runtime.mode`; bad value rejected. And **armed gate still requires mode==live AND confirm AND keys** (regression on `live_authorization`).
- Regression: full suite green; paper/dryrun still keyless; armed gate untouched.

## 6. Risks
- **R-a** compose launch change → validate `docker compose config` parses + paper command still valid (no live run needed).
- **R-b** `dryrun.seconds` (measure, 1d) vs `runtime.seconds` (run, 7d) — intentionally separate; only `symbol` shared. Documented.
- **R-c** `SCA_CONFIG` can't be in the yaml (chicken-egg) — stays env, documented as the one bootstrap exception.
- **R-d** [RESOLVED by grep] `DEFAULT_SYMBOL/DEFAULT_SECONDS` are referenced **only inside engine.py** (lines 89-90, 224-225, 851-852) — safe to repoint at `runtime`.
- **R-e [Codex P1]** Defaults DO NOT shift: `runtime.out_dir` is left UNSET by default and per-caller fallbacks are preserved (engine `"."`, dashboard `"./out"`), so existing bare-metal resume files (`./<symbol>_state.json`) are never orphaned. Setting `runtime.out_dir` is an opt-in unifier; the engine's `dirname(csv_path)` precedence is preserved.
- **R-f [Codex P1]** Mode must resolve explicitly (`env MODE | runtime.mode | paper`) or docker live-enable (`.env MODE=live`) breaks once compose drops `--mode`. Covered by explicit resolution + an armed-gate regression test.
- **R-g [Codex P2]** Stale docs (README/ONBOARDING/compose comments) reference the old `SYMBOL/SECONDS/.env` knobs → updated in-scope.

## 7. Rollback
All additive/config; revert commits. Env overrides preserve prior behavior; `.gitignore`/`.env.example` are safe-only.
