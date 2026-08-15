"""Deadline-honouring waits for the subprocess tests.

One module rather than a copy per test file: the identical bug has now been
found three times in this suite (fork #17, #23, #27), and each time only the
copy that had failed got fixed.

The rule these exist to enforce: **never assert on a fixed `time.sleep()`, and
never read from a pipe without a deadline you can actually enforce.** The sleep
that is comfortably long on an idle machine is too short under a full-suite run
or a concurrent pytest session, and the resulting failure reads like a product
bug rather than a scheduling one.
"""

from __future__ import annotations

import os
import selectors
import time
import weakref

DEFAULT_TIMEOUT = 15.0


def wait_for(predicate, timeout: float = DEFAULT_TIMEOUT,
             interval: float = 0.05) -> bool:
    """Poll `predicate` until it is true or `timeout` elapses.

    Waiting on the condition costs nothing when it is already true, so the
    generous default is free on a pass and only spends time on the runs that
    would otherwise have failed spuriously.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except OSError:
            pass
        time.sleep(interval)
    return False


# Leftover text per process, so a chunk containing several
# lines is not lost. Weak keys: a finished Popen should still be
# collectable.
_BUFFERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def read_line(proc, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Read one non-empty line from `proc.stdout`, or return "" on timeout.

    Two bugs' worth of history, both about deadlines that were not real.

    **Blocking read.** `proc.stdout.readline()` blocks until a line arrives or
    the pipe closes, so a loop checking its deadline only *between* reads
    cannot enforce one — the read that never returns is precisely the one the
    timeout exists for. The old helper advertised `timeout=5.0` and could not
    honour it, so a message that never arrived hung the whole suite: no
    assertion message, no traceback, the enclosing `finally` never running to
    reap the subprocesses. A hang is strictly worse than a failure.

    **Buffered read behind a raw select.** The first fix selected on the fd and
    then called `readline()` on the `TextIOWrapper`. `readline()` pulls up to
    8 KB from the pipe in one syscall and keeps what it does not return, so
    any further line already delivered sat invisible in Python's buffer while
    `select` reported the *pipe* empty — and the call timed out on a line that
    had already arrived. Two ways to hit it: a child flushing `"\\n[msg] x\\n"`
    in one write (the blank-skip path re-selects and never sees `[msg] x`), or
    simply two `read_line` calls when both lines came in one chunk. Latent
    with today's callers, which read one line from a one-line-per-message
    emitter, but `client.py` already has a two-line path for truncated
    messages.

    So this reads raw bytes and splits lines itself, keeping the remainder per
    process. It therefore owns `proc.stdout`: do not mix it with
    `readline()`/`read()`/`communicate()` on the same process.

    Blank lines are skipped rather than returned, matching what every caller
    wanted.
    """
    fd = proc.stdout.fileno()
    buf = _BUFFERS.get(proc, "")
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    try:
        end = time.monotonic() + timeout
        while True:
            # Drain whole lines already buffered before waiting on the pipe:
            # the bug above was doing this in the other order.
            while "\n" in buf:
                line, _, buf = buf.partition("\n")
                if line.strip():
                    _BUFFERS[proc] = buf
                    return line.strip()
            remaining = end - time.monotonic()
            if remaining <= 0 or not sel.select(remaining):
                _BUFFERS[proc] = buf
                return ""
            chunk = os.read(fd, 65536)
            if not chunk:
                _BUFFERS[proc] = buf
                return ""  # EOF: the child closed its stdout
            buf += chunk.decode("utf-8", errors="replace")
    finally:
        sel.unregister(fd)
        sel.close()


def coverage_env() -> dict:
    """Env additions that let a subprocess be measured by `make coverage`.

    Subprocess helpers deliberately build a *clean* env dict rather than
    inheriting `os.environ` — that isolation is why they are trustworthy. But
    it also means `COVERAGE_PROCESS_START` never reaches the child, so
    `auto_start.py` and `doctor.py`, which are only ever exercised through
    subprocesses, reported **0%** and dragged the headline figure from 82% to
    68%. Reading that as "untested" would be exactly wrong: they are 90% and
    78%.

    Returns an empty dict outside a coverage run, so normal runs keep the
    clean env unchanged.
    """
    start = os.environ.get("COVERAGE_PROCESS_START")
    return {"COVERAGE_PROCESS_START": start} if start else {}
