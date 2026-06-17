"""Atomic dict↔file persistence primitives for engine restart / resume.

Only uses stdlib (os, json, sys). Must NOT import sca.live.engine or sca.interest.
"""
import json
import os
import sys


def atomic_write_json(path: str, doc: dict) -> None:
    """Write *doc* to *path* atomically via a .tmp sibling + os.replace.

    Creates parent directories as needed.
    Raises on NaN/Infinity in *doc* (allow_nan=False).

    SECURITY: the tmp file is created mode 0o600 (owner-only) via os.open, so the
    state snapshot — which holds position, realized PnL and dashboard state — is
    never world-readable, regardless of the process umask. os.replace then moves
    that 0o600 file into place, preserving the mode.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, allow_nan=False)
    os.replace(tmp, path)


def save_state(out_dir: str, symbol: str, state: dict) -> None:
    """Persist *state* for *symbol* to ``<out_dir>/<symbol>_state.json``."""
    path = os.path.join(out_dir, f"{symbol}_state.json")
    atomic_write_json(path, state)


def load_state(out_dir: str, symbol: str) -> "dict | None":
    """Load state previously saved by :func:`save_state`.

    Returns ``None`` (never raises) when the file is absent, unreadable, or
    its contents are corrupt — so the engine can fall back to a fresh start
    instead of crashing on boot. A missing file (normal first run) is silent;
    genuine corruption is logged to stderr.

    NOTE (live safety, see plan R1): "corrupt -> fresh start" is fail-OPEN for
    real live trading — a fresh flat ladder would ignore the exchange's real
    position. This is safe today because fills are simulated; once real orders
    are wired, the corrupt/missing-state path MUST be gated behind exchange
    reconciliation, not a silent fresh deploy.
    """
    path = os.path.join(out_dir, f"{symbol}_state.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None  # normal: no prior state (first run)
    except (OSError, ValueError) as e:
        # Corrupt/unreadable state: non-UTF8 bit-rot & bad JSON (ValueError,
        # incl. UnicodeDecodeError/JSONDecodeError), dir/perms (OSError).
        print(f"[persistence] ignoring unreadable state for {symbol}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def append_event(out_dir: str, symbol: str, event: dict) -> None:
    """Append *event* as a JSON line to ``<out_dir>/<symbol>_events.jsonl``.

    Creates parent directories as needed.
    Flushes and attempts fsync (fsync failure is swallowed — best-effort
    durability, consistent with the caller's fire-and-forget pattern).

    SECURITY: the file is opened (and, on first write, created) mode 0o600 via
    os.open so the append-only fill audit trail is never world-readable. O_APPEND
    keeps the atomic-append semantics; the mode argument only applies when the
    file is created, so an existing 0o600 ledger is left untouched.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{symbol}_events.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def read_events(out_dir: str, symbol: str) -> list:
    """Read all events from ``<out_dir>/<symbol>_events.jsonl``.

    Tolerates trailing broken / incomplete lines (skips them) AND a non-UTF8 /
    unreadable ledger (bit-rot, external corruption, perms) — it NEVER raises.
    Returns ``[]`` when the file does not exist, and the prefix successfully
    parsed before any read error otherwise.

    The ledger is a BEST-EFFORT audit source; the state snapshot (load_state) is
    the authority. read_events is called OUTSIDE the engine's atomic resume guard
    (after the snapshot has already been committed), so a raise here would crash
    boot even with a perfectly valid snapshot. Returning the parsed prefix (or
    ``[]``) is the acceptable degradation — mirrors load_state's (OSError,
    ValueError) tolerance.
    """
    path = os.path.join(out_dir, f"{symbol}_events.jsonl")
    events: list = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Skip corrupt / incomplete lines (e.g. mid-write crash tail)
                    continue
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        # Non-UTF8 bit-rot (UnicodeDecodeError is a ValueError) or dir/perms
        # (OSError). Iterating the text stream raises lazily, so any line decoded
        # before the bad bytes is already in `events` — keep that prefix.
        print(f"[persistence] unreadable events ledger for {symbol}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
    return events
