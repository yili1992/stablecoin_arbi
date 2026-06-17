"""Tests for src/sca/live/persistence.py — TDD red-green cycles."""
import json
import os
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
