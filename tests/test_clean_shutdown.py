"""SIGINT/SIGTERM clean shutdown: the kill-switch raises KeyboardInterrupt (to unwind run()
AFTER cancelling every resting order); main() must treat that as a CLEAN exit — a tidy stderr
line + exit 0 — NOT propagate it as an uncaught traceback (docker stop printed a scary stack
on every restart). The order cancellation is upstream (in _on_exit_signal + run()'s finally),
so this only changes exit cosmetics, never the kill-switch's real-money safety.

Run: PYTHONPATH=src python3 -m pytest tests/test_clean_shutdown.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sca.live.engine as engine          # noqa: E402


class _StubEng:
    async def run(self):
        return None


def test_run_until_signal_swallows_killswitch_keyboardinterrupt(monkeypatch, capsys):
    # mimic asyncio.run propagating the kill-switch's KeyboardInterrupt out of run()
    def _raise_ki(coro):
        coro.close()                       # finalize the stub coroutine (no "never awaited" warn)
        raise KeyboardInterrupt("shutdown signal 15")
    monkeypatch.setattr(engine.asyncio, "run", _raise_ki)

    engine._run_until_signal(_StubEng())   # must NOT raise — clean shutdown
    err = capsys.readouterr().err
    assert "shutdown" in err.lower()       # tidy shutdown line emitted
