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

import selectors
import time

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


def read_line(proc, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Read one non-empty line from `proc.stdout`, or return "" on timeout.

    `proc.stdout.readline()` blocks until a line arrives or the pipe closes,
    so a loop that checks its deadline only *between* reads cannot enforce
    one — the read that never returns is precisely the one the timeout exists
    for. The old helper advertised `timeout=5.0` and could not honour it, so a
    message that never arrived hung the whole suite: no assertion message, no
    traceback, the enclosing `finally` never running to reap the subprocesses,
    just a job-level timeout. A hang is strictly worse than a failure.

    Blank lines are skipped rather than returned, matching what every caller
    wanted.

    Residual limitation, stated rather than hidden: `select` reports the fd
    readable, and we then call `readline()`, which can still block if the
    child has written a partial line and stopped. That is a far narrower
    window than "wrote nothing at all", which is the failure actually seen,
    and closing it would mean reading raw bytes and reassembling lines here.
    """
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0 or not sel.select(remaining):
                return ""
            line = proc.stdout.readline()
            if line == "":
                return ""  # EOF: the child closed its stdout
            if line.strip():
                return line.strip()
    finally:
        sel.unregister(proc.stdout)
        sel.close()
