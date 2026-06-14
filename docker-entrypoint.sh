#!/bin/bash
# Command dispatcher (boros-style). Usage: <command> [args...]
#   paper | live | dryrun | backtest | engine | sweep | fetch | dashboard
set -e

cmd="${1:-paper}"; shift || true

case "$cmd" in
  paper|live|dryrun)
    # Long-running engine/measurement: mirror stdout/stderr to a per-boot log under
    # the out volume so partial summaries survive container restarts (boros pattern).
    mkdir -p "${SCA_OUT_DIR:-/app/out}/logs"
    _ts="$(date -u +%Y%m%dT%H%M%SZ)"
    exec > >(tee -a "${SCA_OUT_DIR:-/app/out}/logs/${cmd}-${_ts}.log") 2>&1
    exec sca "$cmd" "$@"
    ;;
  backtest|engine|sweep|fetch|dashboard)
    exec sca "$cmd" "$@"
    ;;
  *)
    exec sca "$cmd" "$@"
    ;;
esac
