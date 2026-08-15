"""Tests for the test helpers themselves.

`tests/waiting.py` is depended on by every subprocess test, and its whole job
is enforcing deadlines. Both bugs it has had were silent — one hung the suite,
the other timed out on a line that had already arrived — so the helper needs
its own coverage rather than being trusted because the tests using it pass.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from tests import waiting


def _emitter(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )


class TestReadLine:
    def test_reads_a_line(self):
        p = _emitter("print('hello')")
        try:
            assert waiting.read_line(p, timeout=10) == "hello"
        finally:
            p.kill(); p.wait()

    def test_two_lines_in_one_write(self):
        """The buffered-read bug. Both lines land in one 8 KB `readline()`
        syscall, so the second sat in Python's buffer while `select` reported
        the pipe empty and the call timed out on a line already delivered."""
        # The child must stay ALIVE after writing. If it exits, the pipe hits
        # EOF and select() reports it readable forever, so even the buggy
        # buffered implementation drains its leftover and the test passes
        # vacuously. That is exactly how the first version of this test failed
        # to catch the bug it was written for.
        p = _emitter("import sys, time; sys.stdout.write('one\\ntwo\\n'); "
                     "sys.stdout.flush(); time.sleep(30)")
        try:
            assert waiting.read_line(p, timeout=10) == "one"
            assert waiting.read_line(p, timeout=10) == "two"
        finally:
            p.kill(); p.wait()

    def test_blank_line_before_content_in_one_write(self):
        """The other shape of the same bug: skipping the blank re-selected on
        an empty pipe and never saw the content that arrived with it."""
        p = _emitter("import sys, time; sys.stdout.write('\\n[msg] hello\\n'); "
                     "sys.stdout.flush(); time.sleep(30)")
        try:
            assert waiting.read_line(p, timeout=10) == "[msg] hello"
        finally:
            p.kill(); p.wait()

    def test_timeout_is_enforced_not_advertised(self):
        """The original defect: a child that writes nothing must produce a
        timely empty return, not block forever. The bound matters as much as
        the value — a hang gives no assertion message and leaves the caller's
        `finally` unrun."""
        p = _emitter("import time; time.sleep(30)")
        try:
            start = time.monotonic()
            assert waiting.read_line(p, timeout=1.0) == ""
            assert time.monotonic() - start < 5, "timeout was not enforced"
        finally:
            p.kill(); p.wait()

    def test_eof_returns_promptly(self):
        p = _emitter("pass")
        try:
            start = time.monotonic()
            assert waiting.read_line(p, timeout=10) == ""
            assert time.monotonic() - start < 5, "did not notice EOF"
        finally:
            p.kill(); p.wait()

    def test_partial_line_then_completion(self):
        p = _emitter(
            "import sys, time; sys.stdout.write('par'); sys.stdout.flush(); "
            "time.sleep(0.3); sys.stdout.write('tial\\n'); sys.stdout.flush()"
        )
        try:
            assert waiting.read_line(p, timeout=10) == "partial"
        finally:
            p.kill(); p.wait()


class TestWaitFor:
    def test_true_predicate_returns_immediately(self):
        start = time.monotonic()
        assert waiting.wait_for(lambda: True, timeout=10) is True
        assert time.monotonic() - start < 1

    def test_false_predicate_times_out_within_bounds(self):
        start = time.monotonic()
        assert waiting.wait_for(lambda: False, timeout=0.5) is False
        elapsed = time.monotonic() - start
        assert 0.4 < elapsed < 3, elapsed

    def test_oserror_is_swallowed_not_raised(self):
        """Callers poll paths that may not exist yet; an OSError mid-poll is a
        not-yet, not a failure."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("not yet")
            return True

        assert waiting.wait_for(flaky, timeout=10) is True

    def test_other_exceptions_still_propagate(self):
        """Swallowing everything would hide real bugs in the predicate."""
        with pytest.raises(ValueError):
            waiting.wait_for(lambda: (_ for _ in ()).throw(ValueError("boom")),
                             timeout=10)
