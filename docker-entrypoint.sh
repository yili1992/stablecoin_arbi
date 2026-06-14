#!/bin/bash
# Command dispatcher (boros-style). Usage: <command> [args...]
#   dryrun | backtest | engine | sweep | fetch
set -e

cmd="${1:-dryrun}"; shift || true

case "$cmd" in
  dryrun)
    # Long-running measurement: mirror stdout/stderr to a per-boot log under the
    # out volume so partial summaries survive container restarts (boros pattern).
    mkdir -p "${SCA_OUT_DIR:-/app/out}/logs"
    _ts="$(date -u +%Y%m%dT%H%M%SZ)"
    exec > >(tee -a "${SCA_OUT_DIR:-/app/out}/logs/dryrun-${_ts}.log") 2>&1
    exec sca dryrun "$@"
    ;;
  backtest|engine|sweep|fetch)
    exec sca "$cmd" "$@"
    ;;
  *)
    exec sca "$cmd" "$@"
    ;;
esac
