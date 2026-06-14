# AGENTS.md

Guidance for AI agents working in this repo.

- **Config-driven.** Change params in `config/strategy.yaml`, not in code. Code reads `sca.config.CFG`.
- **Run via the package.** `sca <cmd>` (after `pip install -e .`) or `python scripts/run.py <cmd>`.
  Modules do `from sca.config import ...`, so run them as modules (`python -m sca.<module>`), not as loose scripts.
- **Backtest honesty (non-negotiable).** No lookahead; sweep adverse-selection `{0,0.5,1,1.5}`bp;
  benchmark = mark-to-market hold (~10.27%); any "win" must hold **out-of-sample** AND survive an
  **independent from-scratch re-implementation**. In-sample max = overfitting. See `docs/METHODOLOGY.md`.
- **Tests encode the findings.** `tests/test_smoke.py` asserts the recommended strategy thinly beats
  flat-10 in-sample and the original baseline loses to hold. Keep them green.
- **Don't fabricate edge.** The honest conclusion is "hold dominates; trading edge ~0 OOS." Report truthfully.
- **dryrun places no orders and needs no keys.** A real-order mode would require explicit authorization + API keys.
- **Push to main only on the owner's call.**
