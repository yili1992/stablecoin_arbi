"""Tests for src/sca/live/persistence.py — TDD red-green cycles."""
import json
import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.persistence import (  # noqa: E402
    atomic_write_json,
    save_state,
    load_state,
    append_event,
    read_events,
)


# ---------------------------------------------------------------------------
# atomic_write_json
# ---------------------------------------------------------------------------

def test_atomic_write_json_creates_file_with_correct_content(tmp_path):
    path = str(tmp_path / "out.json")
    doc = {"key": "value", "nested": {"n": 1}}
    atomic_write_json(path, doc)
    assert os.path.exists(path)
    with open(path) as f:
        loaded = json.load(f)
    assert loaded == doc


def test_atomic_write_json_no_tmp_residue(tmp_path):
    path = str(tmp_path / "out.json")
    atomic_write_json(path, {"x": 1})
    tmp = path + ".tmp"
    assert not os.path.exists(tmp), ".tmp file must not remain after atomic write"


def test_atomic_write_json_overwrites_existing_file(tmp_path):
    path = str(tmp_path / "out.json")
    atomic_write_json(path, {"v": 1})
    atomic_write_json(path, {"v": 2})
    with open(path) as f:
        loaded = json.load(f)
    assert loaded == {"v": 2}


def test_atomic_write_json_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "a" / "b" / "c" / "out.json")
    atomic_write_json(path, {"ok": True})
    assert os.path.exists(path)


# ---------------------------------------------------------------------------
# save_state / load_state round-trip
# ---------------------------------------------------------------------------

def test_save_load_state_roundtrip(tmp_path):
    out_dir = str(tmp_path)
    state = {"symbol": "USD1USDT", "pos": 100.0, "meta": {"ts": 1234567890}}
    save_state(out_dir, "USD1USDT", state)
    loaded = load_state(out_dir, "USD1USDT")
    assert loaded == state


def test_load_state_missing_file_returns_none(tmp_path):
    result = load_state(str(tmp_path), "NONEXISTENT")
    assert result is None


def test_load_state_corrupt_json_returns_none(tmp_path):
    out_dir = str(tmp_path)
    # Write a file with broken JSON content
    path = os.path.join(out_dir, "BROKEN_state.json")
    with open(path, "w") as f:
        f.write("{not json")
    result = load_state(out_dir, "BROKEN")
    assert result is None


def test_save_state_creates_parent_dirs(tmp_path):
    out_dir = str(tmp_path / "nested" / "dir")
    save_state(out_dir, "SYM", {"val": 42})
    assert load_state(out_dir, "SYM") == {"val": 42}


# ---------------------------------------------------------------------------
# append_event / read_events
# ---------------------------------------------------------------------------

def test_append_event_each_call_adds_one_line(tmp_path):
    out_dir = str(tmp_path)
    append_event(out_dir, "USD1USDT", {"seq": 1})
    append_event(out_dir, "USD1USDT", {"seq": 2})
    append_event(out_dir, "USD1USDT", {"seq": 3})
    path = os.path.join(out_dir, "USD1USDT_events.jsonl")
    with open(path) as f:
        lines = [l for l in f.readlines() if l.strip()]
    assert len(lines) == 3


def test_read_events_returns_ordered_events(tmp_path):
    out_dir = str(tmp_path)
    events = [{"seq": i, "data": f"ev{i}"} for i in range(5)]
    for ev in events:
        append_event(out_dir, "USD1USDT", ev)
    result = read_events(out_dir, "USD1USDT")
    assert result == events


def test_read_events_missing_file_returns_empty_list(tmp_path):
    result = read_events(str(tmp_path), "NOFILE")
    assert result == []


def test_read_events_tolerates_trailing_broken_line(tmp_path):
    out_dir = str(tmp_path)
    # Write two good events
    append_event(out_dir, "USD1USDT", {"seq": 0})
    append_event(out_dir, "USD1USDT", {"seq": 1})
    # Manually append a broken (incomplete) line without a trailing newline
    path = os.path.join(out_dir, "USD1USDT_events.jsonl")
    with open(path, "a") as f:
        f.write('{"broken')  # no closing brace, no newline
    # Must return the 2 good events and skip the broken tail — no exception
    result = read_events(out_dir, "USD1USDT")
    assert result == [{"seq": 0}, {"seq": 1}]


def test_append_event_creates_parent_dirs(tmp_path):
    out_dir = str(tmp_path / "new_dir")
    append_event(out_dir, "SYM", {"x": 1})
    result = read_events(out_dir, "SYM")
    assert result == [{"x": 1}]


def test_load_state_non_utf8_corruption_returns_none(tmp_path):
    # Regression (QA-found P2, fixed): a state.json holding NON-UTF8 bytes
    # (disk bit-rot / external corruption) raises UnicodeDecodeError, not
    # JSONDecodeError. load_state must tolerate it (return None) so the engine
    # falls back to a fresh start instead of crashing on boot. (OSError covers
    # the dir/perms variants.)
    path = os.path.join(str(tmp_path), "ROT_state.json")
    with open(path, "wb") as f:
        f.write(b"\xff\xfe\x00 corrupt non-utf8 payload \x80\x81")
    assert load_state(str(tmp_path), "ROT") is None


# ---------------------------------------------------------------------------
# FIX 2 (security) — state / events files must be created mode 0o600 so they
# are NOT world-readable. They contain position, realized PnL and the full
# fill audit trail; a default-umask 0o644 file leaks that to any local user.
# atomic_write_json (via save_state) and append_event are the two file-creating
# primitives; both must land 0o600. Permission bits are an OS-level guarantee
# we can read straight off os.stat — no mocking needed.
# ---------------------------------------------------------------------------

def test_save_state_file_is_owner_only_0600(tmp_path):
    out_dir = str(tmp_path)
    save_state(out_dir, "USD1USDT", {"pos": 100.0, "realized": 1.23})
    path = os.path.join(out_dir, "USD1USDT_state.json")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"state file must be 0o600, got {oct(mode)}"


def test_append_event_file_is_owner_only_0600(tmp_path):
    out_dir = str(tmp_path)
    append_event(out_dir, "USD1USDT", {"side": "sell", "price": 1.0005})
    path = os.path.join(out_dir, "USD1USDT_events.jsonl")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"events file must be 0o600, got {oct(mode)}"


def test_atomic_write_json_file_is_owner_only_0600(tmp_path):
    path = str(tmp_path / "secret.json")
    atomic_write_json(path, {"k": "v"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"atomic_write_json file must be 0o600, got {oct(mode)}"


def test_save_state_overwrite_preserves_0600(tmp_path):
    # Re-writing an existing state file (the common per-fill path) must keep
    # 0o600 — os.replace of a 0o600 tmp preserves the tmp's mode, but an
    # already-loose pre-existing file must not silently stay 0o644 either.
    out_dir = str(tmp_path)
    save_state(out_dir, "SYM", {"v": 1})
    save_state(out_dir, "SYM", {"v": 2})
    path = os.path.join(out_dir, "SYM_state.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_append_event_existing_file_stays_0600(tmp_path):
    # Second append to an existing events file must keep 0o600 (O_APPEND on an
    # existing 0o600 file does not loosen it).
    out_dir = str(tmp_path)
    append_event(out_dir, "SYM", {"seq": 1})
    append_event(out_dir, "SYM", {"seq": 2})
    path = os.path.join(out_dir, "SYM_events.jsonl")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# FIX (Codex P1) — read_events must tolerate a NON-UTF8 events ledger.
# The events ledger is a best-effort AUDIT source (the state snapshot is the
# authority). A ledger that has bit-rotted / been externally corrupted with raw
# non-UTF8 bytes makes f.read() raise UnicodeDecodeError (a ValueError subclass).
# This call sits OUTSIDE _maybe_resume's atomic guard (engine reads it AFTER
# committing the snapshot), so an unhandled raise here crashes boot even with a
# perfectly valid snapshot. read_events must swallow it and return [] (or the
# already-parsed prefix) — never raise. Mirrors load_state's (OSError, ValueError)
# tolerance, which the ledger path had been missing.
# ---------------------------------------------------------------------------

def test_read_events_non_utf8_file_returns_empty_no_raise(tmp_path):
    out_dir = str(tmp_path)
    path = os.path.join(out_dir, "ROT_events.jsonl")
    # raw non-UTF8 bytes — f.read() in text mode raises UnicodeDecodeError
    with open(path, "wb") as f:
        f.write(b"\xff\xfe bad")
    # must NOT raise; ledger is best-effort, snapshot is authoritative
    assert read_events(out_dir, "ROT") == []


# ---------------------------------------------------------------------------
# D15 — per-mode state-file segregation (prevent a dryrun run polluting live).
# The 4 primitives take an optional ``tag``. tag=="" keeps the legacy
# ``<symbol>_state.json`` / ``<symbol>_events.jsonl`` path (backward compatible:
# the standalone dryrun tool + every direct unit test above are untouched). A
# non-empty tag qualifies the filename to ``<symbol>_<tag>_state.json`` so two
# tags (e.g. "dryrun" and "live") NEVER share a file — a leftover untagged or
# other-tagged snapshot is invisible to a given tag's load.
# ---------------------------------------------------------------------------

def test_save_state_tag_writes_mode_qualified_path(tmp_path):
    out_dir = str(tmp_path)
    state = {"v": 2, "pos": 7.0}
    save_state(out_dir, "USD1USDT", state, tag="live")
    # the qualified file exists with the expected name, and the untagged one does NOT
    assert os.path.exists(os.path.join(out_dir, "USD1USDT_live_state.json"))
    assert not os.path.exists(os.path.join(out_dir, "USD1USDT_state.json"))
    assert load_state(out_dir, "USD1USDT", tag="live") == state


def test_save_state_no_tag_keeps_legacy_path(tmp_path):
    # explicit backward-compat pin: empty tag == the historical untagged path.
    out_dir = str(tmp_path)
    save_state(out_dir, "USD1USDT", {"v": 2}, tag="")
    assert os.path.exists(os.path.join(out_dir, "USD1USDT_state.json"))
    assert not os.path.exists(os.path.join(out_dir, "USD1USDT_live_state.json"))


def test_state_tags_are_isolated_from_each_other_and_untagged(tmp_path):
    # the core D15 invariant: dryrun, live, and legacy-untagged states coexist in
    # ONE out_dir without ever reading each other's file.
    out_dir = str(tmp_path)
    save_state(out_dir, "USD1USDT", {"who": "dryrun"}, tag="dryrun")
    save_state(out_dir, "USD1USDT", {"who": "live"}, tag="live")
    save_state(out_dir, "USD1USDT", {"who": "legacy"})            # untagged
    assert load_state(out_dir, "USD1USDT", tag="dryrun") == {"who": "dryrun"}
    assert load_state(out_dir, "USD1USDT", tag="live") == {"who": "live"}
    assert load_state(out_dir, "USD1USDT") == {"who": "legacy"}
    # a tag with no file of its own resolves to None, never another tag's state
    assert load_state(out_dir, "USD1USDT", tag="other") is None


def test_load_state_live_does_not_see_dryrun_or_untagged(tmp_path):
    # the exact pollution case D15 prevents: only an untagged + a dryrun snapshot
    # exist; a live load must find NEITHER (returns None -> fresh seed).
    out_dir = str(tmp_path)
    save_state(out_dir, "USD1USDT", {"who": "dryrun"}, tag="dryrun")
    save_state(out_dir, "USD1USDT", {"who": "legacy"})            # untagged leftover
    assert load_state(out_dir, "USD1USDT", tag="live") is None


def test_append_read_events_tag_writes_mode_qualified_path(tmp_path):
    out_dir = str(tmp_path)
    append_event(out_dir, "USD1USDT", {"seq": 1}, tag="live")
    append_event(out_dir, "USD1USDT", {"seq": 2}, tag="live")
    assert os.path.exists(os.path.join(out_dir, "USD1USDT_live_events.jsonl"))
    assert not os.path.exists(os.path.join(out_dir, "USD1USDT_events.jsonl"))
    assert read_events(out_dir, "USD1USDT", tag="live") == [{"seq": 1}, {"seq": 2}]


def test_events_tags_are_isolated_from_each_other_and_untagged(tmp_path):
    out_dir = str(tmp_path)
    append_event(out_dir, "USD1USDT", {"who": "dryrun"}, tag="dryrun")
    append_event(out_dir, "USD1USDT", {"who": "live"}, tag="live")
    append_event(out_dir, "USD1USDT", {"who": "legacy"})          # untagged
    assert read_events(out_dir, "USD1USDT", tag="dryrun") == [{"who": "dryrun"}]
    assert read_events(out_dir, "USD1USDT", tag="live") == [{"who": "live"}]
    assert read_events(out_dir, "USD1USDT") == [{"who": "legacy"}]
    # a tag with no ledger of its own returns [] (never another tag's events)
    assert read_events(out_dir, "USD1USDT", tag="other") == []
