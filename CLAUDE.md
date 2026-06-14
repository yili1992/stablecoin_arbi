# CLAUDE.md — stablecoin_arbi

Project-specific background, architecture, and conventions for Claude Code.
(Team/global workflow conventions live in the user's global CLAUDE.md; this is project-scoped.)

## What this project is
0-fee stablecoin buy-low-sell-high research on Bybit + a live fill-quality measurement tool.
**Honest conclusion:** the yield is the real return; the price/trading edge is ~0 out-of-sample;
holding USD1 (~10% UTA carry) is the rational default. See `docs/FINDINGS.md`.

## Architecture
All params in `config/strategy.yaml` (read via `sca.config`); package under `src/sca/`;
commands via `sca <cmd>` / `python scripts/run.py <cmd>`; docker profiles `dryrun` (live
measurement) and `tools` (backtest/engine/sweep/fetch). Details in `ONBOARDING.md` and
`docs/conventions.md`.

## Where things are documented
- `docs/conventions.md` — config path, package structure, backtest-fidelity rules, ship flow
- `docs/decisions.md` — why only-USD1, no-stop, floating-anchor ladder, yield-is-the-engine, adv-needs-live
- `docs/METHODOLOGY.md` — no-lookahead, fill models, adverse selection, IS/OOS, adversarial verification
- `docs/FINDINGS.md` / `docs/STRATEGY.md` — the full narrative + the recommended strategy

## Hard rules
1. Params in yaml, not code.
2. No fabricated edge — out-of-sample + independent re-implementation gate every "win".
3. `dryrun` = measurement only (no orders, no keys). Live trading is unbuilt; needs explicit authorization.
4. Push to `main` only on the owner's call.
