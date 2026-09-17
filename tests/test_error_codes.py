"""Every `shared.ErrorCode` produced end to end, with the path in the name.

`tests/test_server.py` already drives each code in-process, against a `Server`
object over a websocket the test opened itself. What it never touches is the
rest of the stack a user stands on: `send.py`/`list.py` finding their own
session through `discover.py`, the `role=control` handshake built from the
`.session` file, the exit status, and the stderr text that actually appears.
Every one of those layers has produced a real bug in this repo.

**The names carry the path, and that is a rule, not a convention.**
`docs/plans/behaviour-under-test.md` (Tier A) accepts a hybrid — subprocess
CLIs where the CLI can reach the code, raw frames where it cannot — only on
condition that no test reached by raw frame is ever described as going through
the CLI. Hence `_via_cli` / `_via_raw_frame` on every name here, terminal so
`tests/test_shared.py::TestErrorCodeMatrix` can anchor on it. Five codes are
raw-frame-only and say so: both `TEXT_TOO_LONG` caps exceed Linux
`MAX_ARG_STRLEN` (128 KiB per argv string) and `send.py --text` is argv-only;
`INVALID_NAME`/`INVALID_LABEL` are pre-validated in `client.py`/`relabel.py`
before the frame is built; `UNKNOWN_OP`/`INVALID_PAYLOAD` need a frame no CLI
constructs.

Three rules keep the shared-bus classes order- and `-k`-independent:

1. **Every test drains every line it causes, on every listener**, via
   `waiting.read_line`. Broadcasts fan out to every agent but the sender
   (`server.py:559-560`), so one broadcast from `src` is three lines. An
   undrained line is read by whichever test runs next, and the failure lands
   there rather than here. "Printed nothing" is `read_line(p, timeout=1.0) ==
   ""` — never a second reader on `proc.stdout`, which `waiting.read_line`
   owns (#17/#23/#27).
2. **Only the `RATE_LIMITED` tests spend `src`'s broadcast window**, and each
   fills it through its own `bus.fill_window()` call, so no test's Given is
   another test's leftover.
3. **A test that tampers with a `.session` restores its bytes in `finally`.**

Budget: `docs/plans/behaviour-under-test.md` caps this tier at 30 s added to
`make test`, so every test here is `@pytest.mark.slow` and `make test-fast`
is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import psutil
import pytest
import websockets

from bin import shared
from tests import control, waiting
from tests.test_helpers import (
    BIN_DIR,
    REPO,
    _run_helper,
    _spawn_listener,
    _wait_for_state,
)

NOT_CONNECTED = "not connected; run /hubbub:talk in this Claude Code session first"


def _free_port() -> int:
    """`conftest.free_port` is function-scoped and the bus is not.

    Same technique — bind 0, read the number, close — and the same residual
    risk CLAUDE.md records: a concurrent pytest session can be handed the port
    between the close and the bind, and surfaces as `server identity check
    failed`.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _reap(p) -> None:
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _kill_server_in(data_dir: Path, port: int) -> None:
    """Kill the elected server for *this* data dir.

    Deliberately not `tests.test_helpers._kill_server`, which globs
    `shared.data_dir()` — with `HUBBUB_DATA_DIR` cleared in the pytest process
    that resolves to the developer's real `~/.claude/data/hubbub/` and would
    SIGKILL a server other Claude Code sessions are using.
    """
    pid_path = data_dir / f"server.{port}.pid"
    try:
        os.kill(int(pid_path.read_text().strip()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


async def _await_exit(proc, timeout: float = 10.0) -> int:
    """`proc.wait()` for an `async` test: polls without stalling the loop.

    The blocking form would freeze the very loop an in-process stand-in server
    runs on, so the child would wait for a reply that can never be written and
    the test would fail on a timeout that says nothing about the behaviour.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if proc.poll() is not None:
            return proc.returncode
        await asyncio.sleep(0.05)
    raise AssertionError(f"{proc.args[1]} never exited within {timeout}s")


class Bus:
    """One elected `server.py` and four real `client.py` monitors.

    `listeners` is keyed by name and ordered by registration, which is what
    fixes the `matches:` order in the `AMBIGUOUS` stderr.
    """

    PPIDS = (("src", 70001), ("dst", 70002), ("alpha-1", 70003), ("alpha-2", 70004))

    def __init__(self, port: int, data_dir: Path, listeners: dict):
        self.port = port
        self.data_dir = data_dir
        self.listeners = listeners
        # Running total across every fill_window() call on this bus, so the
        # first filler can assert against BROADCAST_RATE_LIMIT_PER_MIN however
        # many tests `-k` selected.
        self.broadcasts_accepted = 0

    def ppid(self, name: str) -> int:
        return dict(self.PPIDS)[name]

    def session_path(self, name: str) -> Path:
        return self.data_dir / "clients" / f"{self.ppid(name)}.session"

    def state(self, name: str) -> dict:
        return json.loads(self.session_path(name).read_text())

    @property
    def log_path(self) -> Path:
        return self.data_dir / "messages.log"

    def log_size(self) -> int:
        return self.log_path.stat().st_size if self.log_path.exists() else 0

    def log_kinds(self, kind: str) -> int:
        if not self.log_path.exists():
            return 0
        return sum(1 for line in self.log_path.read_text().splitlines()
                   if line.strip() and json.loads(line).get("kind") == kind)

    def helper(self, script: str, sender: str, *args, **kw):
        return _run_helper(script, self.data_dir, self.ppid(sender), *args, **kw)

    async def fill_window(self) -> int:
        """Fill `src`'s broadcast window; return how many this call got in.

        Idempotent by construction, which is what lets each `RATE_LIMITED`
        test call it rather than depend on another test having run: a fresh
        window costs 60 accepted frames, an already-full one costs exactly one
        refused frame and returns 0.

        **The `ping` is a fence, not a health check.** A successful broadcast
        answers nothing, so there is no reply to wait for and a `wait_for`
        timeout per frame would cost 60 timeouts. `_dispatch_loop` is
        `async for raw in state.ws` with every handler awaited inline
        (`server.py:420-448`), so a `pong` cannot overtake the broadcast that
        preceded it: the next frame back is `pong` if the broadcast was
        accepted, and the `error` if it was refused.

        The loop is bounded at `BROADCAST_RATE_LIMIT_PER_MIN + 1` accepted
        frames, so no mutation of the window logic can turn the fill into a
        hang — it fails the test instead.
        """
        limit = shared.BROADCAST_RATE_LIMIT_PER_MIN
        ws = await control.open_control(self.port, self.state("src"))
        accepted = 0
        try:
            while accepted <= limit:
                tag = f"n-{self.broadcasts_accepted + accepted + 1}"
                await ws.send(json.dumps({"op": "broadcast", "text": tag}))
                reply = await control.exchange(ws, {"op": "ping"})
                if reply.get("op") == "pong":
                    accepted += 1
                    # Rule 1: drain what we caused, everywhere it landed.
                    # Broadcasts fan out to every agent but the sender.
                    for peer in ("dst", "alpha-1", "alpha-2"):
                        line = waiting.read_line(self.listeners[peer])
                        assert tag in line, f"{peer} missed {tag}: {line!r}"
                    continue
                assert reply.get("op") == "error", reply
                assert reply.get("code") == shared.ErrorCode.RATE_LIMITED, reply
                assert reply.get("message") == "broadcast rate limit exceeded", reply
                # The refusal `return`s before `window.append`, and
                # `_send_error` never closes, so the fence still answers.
                fence = await control._recv(ws)
                assert fence.get("op") == "pong", fence
                self.broadcasts_accepted += accepted
                return accepted
            pytest.fail(
                f"window never filled: {accepted} broadcasts accepted with "
                f"the cap at {limit}")
        finally:
            await ws.close()


@pytest.fixture(scope="class")
def bus(tmp_path_factory):
    """A bus that outlives one test, so the ~1.5 s election is amortised.

    Class-scoped rather than function-scoped for the budget, which means it
    cannot request `tmp_data_dir`, `free_port` or `monkeypatch` — all three
    are function-scoped. It therefore builds its own, and the `MonkeyPatch()`
    is not a convenience: `_spawn_listener` and `_run_helper` both start from
    `os.environ.copy()` and set only `INTER_SESSION_DATA_DIR`, while
    `shared.env()` prefers `HUBBUB_DATA_DIR` — so a developer with that
    exported would point every `client.py` this fixture spawns at their real
    `~/.claude/data/hubbub/`.

    For the same reason nothing here calls `shared.data_dir()` or
    `tests.test_helpers._kill_server`: with the env cleared both resolve to
    the real data dir, and the second one SIGKILLs whatever it finds there.
    """
    mp = pytest.MonkeyPatch()
    mp.delenv("HUBBUB_DATA_DIR", raising=False)
    mp.delenv("INTER_SESSION_DATA_DIR", raising=False)
    data_dir = tmp_path_factory.mktemp("bus") / "hubbub"
    port = _free_port()
    listeners: dict = {}
    try:
        for name, ppid in Bus.PPIDS:
            listeners[name] = _spawn_listener(port, name, data_dir, ppid)
            # Sequential, each waited for: registration order is what fixes
            # the `matches:` order the AMBIGUOUS assertion reads.
            assert _wait_for_state(data_dir, ppid), f"{name} never registered"
        yield Bus(port, data_dir, listeners)
        # "Nothing was mutated", for free: every negative in the class asserts
        # its own `.session` survived, and this catches one that deleted
        # someone else's.
        for name, ppid in Bus.PPIDS:
            assert (data_dir / "clients" / f"{ppid}.session").exists(), (
                f"{name}'s state file did not survive the class")
    finally:
        for p in listeners.values():
            _reap(p)
        _kill_server_in(data_dir, port)
        mp.undo()


def _list_names(bus: Bus) -> set:
    """The NAME column of every `list.py` row, as seen by `src`.

    Column equality, never a substring search of the whole table: the CWD
    column carries this checkout's path and can contain almost any word a
    parametrisation picks — the trap `tests/test_client.py::_list_label`
    documents.
    """
    r = bus.helper("list.py", "src")
    assert r.returncode == 0, r.stdout + r.stderr
    return {line[:24].strip() for line in r.stdout.splitlines()[1:]}


@pytest.mark.slow
class TestErrorCodesViaCli:
    """Codes a user can actually reach through `send.py` / `list.py`."""

    def test_unique_prefix_delivers_via_cli(self, bus):
        """The positive `AMBIGUOUS` is contrasted with: one match resolves."""
        before = bus.log_kinds("direct")
        r = bus.helper("send.py", "src", "--to", "ds", "--text", "hi")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "hi" in waiting.read_line(bus.listeners["dst"])
        assert bus.log_kinds("direct") == before + 1

    def test_ambiguous_prefix_via_cli(self, bus):
        """`alpha` prefixes two names, so the bus refuses to guess.

        `ds` in the positive above prefixes `dst` alone; `alp` would be
        ambiguous in the same way as `alpha` and proves nothing extra.
        """
        size = bus.log_size()
        r = bus.helper("send.py", "src", "--to", "alpha", "--text", "hi")
        assert r.returncode == 1, r.stdout + r.stderr
        assert ("error: ambiguous: ambiguous prefix 'alpha' "
                "(matches: alpha-1, alpha-2)") in r.stderr, r.stderr
        for peer in ("alpha-1", "alpha-2"):
            assert waiting.read_line(bus.listeners[peer], timeout=1.0) == ""
        assert bus.log_size() == size

    def _tampered(self, bus, **fields):
        """Rewrite `src`'s `.session`, restoring its bytes afterwards.

        A live monitor holds `clients/70001.lock`, so `unlink_if_matches`
        refuses to clean the file up while we are looking — which is itself
        one of the assertions.
        """
        path = bus.session_path("src")
        original = path.read_bytes()
        state = json.loads(original)
        state.update(fields)
        path.write_text(json.dumps(state) + "\n")
        return path, original

    def test_unauthorized_wrong_nonce_via_cli(self, bus):
        """The impersonation path: right `for_session` and token, wrong nonce.

        A sibling process that can read the state file but not the nonce a
        live monitor is holding is exactly what the cross-check at
        `server.py:311` exists to refuse.
        """
        path, original = self._tampered(bus, nonce="not-the-nonce")
        try:
            r = bus.helper("send.py", "src", "--to", "dst", "--text", "hi")
            assert r.returncode == 1, r.stdout + r.stderr
            assert NOT_CONNECTED in r.stderr, r.stderr
            assert path.exists(), (
                "unlink_if_matches must refuse while the listener holds its lock")
            assert bus.listeners["src"].poll() is None
            assert waiting.read_line(bus.listeners["dst"], timeout=1.0) == ""
        finally:
            path.write_bytes(original)

    def test_unauthorized_bad_token_via_cli(self, bus):
        """The other refusal shape, through `list.py`: a wrong bearer token.

        Refused at `server.py:240`, before the role or nonce is looked at, so
        it is a genuinely different branch from the nonce case above.
        """
        path, original = self._tampered(bus, token="0" * 64)
        try:
            r = bus.helper("list.py", "src")
            assert r.returncode == 1, r.stdout + r.stderr
            assert NOT_CONNECTED in r.stderr, r.stderr
            assert path.exists()
            assert bus.listeners["src"].poll() is None
        finally:
            path.write_bytes(original)

    @pytest.mark.asyncio
    async def test_rate_limited_after_sixty_raw_broadcasts_via_cli(self, bus):
        """The 61st broadcast in a minute is refused, through `send.py --all`.

        The window is filled by this test's own `fill_window()` — 60 raw
        frames in well under a second, where 60 `send.py --all` runs would
        cost at least 60 s (each waits 1.0 s for silence). The *refusal* is
        still through the CLI, which is what makes `_via_cli` honest.

        The running total is exactly the cap because the two rate-limit tests
        run seconds apart and `server.py:626-627` prunes only entries older
        than 60 s relative to *this* call's `now`: a second fill inside that
        gap accepts 0 and the total stands.
        """
        before = bus.log_kinds("broadcast")
        accepted = await bus.fill_window()
        assert bus.broadcasts_accepted == shared.BROADCAST_RATE_LIMIT_PER_MIN
        assert bus.log_kinds("broadcast") == before + accepted

        size = bus.log_size()
        r = bus.helper("send.py", "src", "--all", "--text", "61")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "error: rate_limited: broadcast rate limit exceeded" in r.stderr, r.stderr
        assert waiting.read_line(bus.listeners["dst"], timeout=1.0) == ""
        assert bus.log_size() == size

    @pytest.mark.asyncio
    async def test_rate_limited_is_per_op_via_cli(self, bus):
        """Being capped on broadcast is not a lockout: direct sends deliver.

        Its own `fill_window()` again (0 accepted when the window is already
        full), so this passes alone, in either order, and under any `-k`.
        """
        await bus.fill_window()
        before = bus.log_kinds("direct")
        r = bus.helper("send.py", "src", "--to", "dst", "--text", "direct")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "direct" in waiting.read_line(bus.listeners["dst"])
        assert bus.log_kinds("direct") == before + 1

        size = bus.log_size()
        r = bus.helper("send.py", "src", "--all", "--text", "62")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "error: rate_limited: broadcast rate limit exceeded" in r.stderr, r.stderr
        assert waiting.read_line(bus.listeners["dst"], timeout=1.0) == ""
        assert bus.log_size() == size


_INVALID_NAMES = [
    pytest.param("Alpha", id="uppercase"),
    pytest.param("a b", id="space"),
    pytest.param("-a", id="leading-dash"),
    pytest.param("a" * 41, id="too-long"),
]

_INVALID_LABELS = [
    pytest.param('x"y', id="double-quote"),
    pytest.param("a[b", id="open-bracket"),
    pytest.param("‮", id="bidi-override"),
    pytest.param("‍", id="zero-width-joiner"),
    pytest.param("x" * 61, id="too-long"),
]

# Every case is a *first* frame, which `server.py:212-226` rejects by
# returning — closing the connection — so each needs its own socket.
# ("hello", overrides) goes through `control.hello_agent` so the token is
# read from the `.session` and the mistyped value reaches the wire verbatim.
_INVALID_PAYLOADS = [
    pytest.param(("raw", [1, 2]), "frame must be a JSON object", id="non-object"),
    pytest.param(("hello", {"session_id": 7}),
                 "session_id must be a string", id="session-id-type"),
    pytest.param(("hello", {"session_id": "\n[hubbub"}),
                 "invalid session_id", id="hostile-session-id"),
    pytest.param(("hello", {"name": 5}),
                 "name and label must be strings", id="name-type"),
    pytest.param(("hello", {"role": "root"}), "bad role", id="bad-role"),
]

# `first` says whether the frame is the connection's first — which decides
# whether the server closes afterwards or keeps serving.
_UNKNOWN_OPS = [
    pytest.param(True, "not json", "malformed JSON", id="malformed-json"),
    pytest.param(True, {"op": "ping"}, "first frame must be hello", id="pre-hello-op"),
    pytest.param(False, {"op": "frobnicate"},
                 "unknown op 'frobnicate'", id="frobnicate"),
    pytest.param(False, {"op": "hello"}, "duplicate hello", id="duplicate-hello"),
]


@pytest.mark.slow
class TestErrorCodesViaRawFrame:
    """The five codes no shipped CLI can produce, against the real server.

    Not "through the CLI path" and never named as if they were: `client.py`
    and `relabel.py` pre-validate names and labels, no CLI sends an unknown
    `op` or a mistyped frame, and both `TEXT_TOO_LONG` caps exceed Linux
    `MAX_ARG_STRLEN` while `send.py --text` is argv-only.
    """

    @pytest.mark.asyncio
    async def test_control_hello_first_frame_welcomes_via_raw_frame(self, bus):
        """The positive the first-frame rejections are measured against."""
        ws = await control.open_control(bus.port, bus.state("src"))
        try:
            assert (await control.exchange(ws, {"op": "ping"}))["op"] == "pong"
        finally:
            await ws.close()

    @pytest.mark.asyncio
    async def test_ping_pong_on_elected_server_via_raw_frame(self, bus):
        """`ping` is answered with no role check, and changes no identity."""
        pid_path = bus.data_dir / f"server.{bus.port}.pid"
        before = pid_path.read_bytes()
        ws = await control.open_control(bus.port, bus.state("src"))
        try:
            assert (await control.exchange(ws, {"op": "ping"}))["op"] == "pong"
        finally:
            await ws.close()
        assert pid_path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_agent_hello_with_token_welcomes_via_raw_frame(self, bus):
        """A raw `role=agent` hello with the real token joins the bus."""
        ws, reply = await control.hello_agent(bus.port, bus.state("src"), name="eta")
        try:
            assert reply["op"] == "welcome", reply
            assert reply["assigned_name"] == "eta", reply
            assert "eta" in _list_names(bus)
        finally:
            await ws.close()
        assert waiting.wait_for(lambda: "eta" not in _list_names(bus)), (
            "eta stayed registered after its socket closed")

    @pytest.mark.asyncio
    async def test_unauthorized_missing_token_via_raw_frame(self, bus):
        """No `token` key at all — the shape no CLI can send.

        `send.py:63` indexes `state["token"]`, so a helper always sends one.
        An absent key must be refused exactly like a wrong one, before any
        role or name check (`payload.get("token")` is `None` at
        `server.py:240`).
        """
        size = bus.log_size()
        ws, reply = await control.hello_agent(
            bus.port, bus.state("src"), name="eta", token=None)
        try:
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.UNAUTHORIZED, reply
            assert reply["message"] == "bad token", reply
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
        finally:
            await ws.close()
        assert "eta" not in _list_names(bus)
        assert bus.log_size() == size

    @pytest.mark.asyncio
    async def test_valid_name_and_label_welcome_via_raw_frame(self, bus):
        """The positive for both validators: a legal name and a Unicode label."""
        ws, reply = await control.hello_agent(
            bus.port, bus.state("src"), name="zeta", label="Émile ✓")
        try:
            assert reply["op"] == "welcome", reply
            r = bus.helper("list.py", "src")
            assert r.returncode == 0, r.stdout + r.stderr
            row = [line for line in r.stdout.splitlines() if line[:24].strip() == "zeta"]
            assert row, r.stdout
            assert row[0][25:49].strip() == "Émile ✓", row[0]
        finally:
            await ws.close()
        assert waiting.wait_for(lambda: "zeta" not in _list_names(bus))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _INVALID_NAMES)
    async def test_invalid_name_via_raw_frame(self, bus, name):
        """`client.py:792` pre-validates, so this branch has no CLI route."""
        ws, reply = await control.hello_agent(bus.port, bus.state("src"), name=name)
        try:
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.INVALID_NAME, reply
            assert reply["message"] == "invalid name", reply
        finally:
            await ws.close()
        assert name not in _list_names(bus)
        assert {p.name for p in (bus.data_dir / "clients").glob("*.session")} == {
            f"{ppid}.session" for _, ppid in Bus.PPIDS}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", _INVALID_LABELS)
    async def test_invalid_label_via_raw_frame(self, bus, label):
        """The SEC-001 boundary reject, plus the BiDi/ZWJ category rules.

        `"`/`[` would close the notification header's bracket and forge sender
        attribution; the U+202E and U+200D cases are the category restriction
        that keeps a label from re-ordering or joining what follows it.
        """
        ws, reply = await control.hello_agent(
            bus.port, bus.state("src"), name="theta", label=label)
        try:
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.INVALID_LABEL, reply
            assert reply["message"] == "invalid label", reply
        finally:
            await ws.close()
        assert "theta" not in _list_names(bus)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case,message", _INVALID_PAYLOADS)
    async def test_invalid_payload_via_raw_frame(self, bus, case, message):
        """Mistyped and non-object frames, including the SEC-003 payload.

        `session_id="\\n[hubbub"` is the e2e twin of
        `TestSessionIdValidation::test_hostile_session_id_is_rejected`: eight
        characters of `session_id` render as the `sid=` fingerprint in a
        notification header, and a newline there splits the line so the
        injection becomes the leading — i.e. authoritative — prefix of its own.
        """
        kind, payload = case
        size = bus.log_size()
        if kind == "raw":
            ws = await control.open_raw(bus.port)
            reply = await control.exchange(ws, payload)
        else:
            ws, reply = await control.hello_agent(bus.port, bus.state("src"), **payload)
        try:
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.INVALID_PAYLOAD, reply
            assert reply["message"] == message, reply
            # A rejected first frame closes the connection.
            await asyncio.wait_for(ws.wait_closed(), timeout=5)
        finally:
            await ws.close()
        assert bus.log_size() == size

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first,frame,message", _UNKNOWN_OPS)
    async def test_unknown_op_via_raw_frame(self, bus, first, frame, message):
        """Malformed or unknown ops, before and after a valid `hello`.

        The difference is the point: a rejected *first* frame closes the
        connection, while a post-`hello` rejection leaves it serving — so the
        socket still answers `ping` afterwards.
        """
        size = bus.log_size()
        ws = (await control.open_raw(bus.port) if first
              else await control.open_control(bus.port, bus.state("src")))
        try:
            reply = await control.exchange(ws, frame)
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.UNKNOWN_OP, reply
            assert reply["message"] == message, reply
            if first:
                await asyncio.wait_for(ws.wait_closed(), timeout=5)
            else:
                assert (await control.exchange(ws, {"op": "ping"}))["op"] == "pong"
        finally:
            await ws.close()
        assert bus.log_size() == size

    @pytest.mark.asyncio
    async def test_broadcast_at_cap_delivers_truncated_via_raw_frame(self, bus):
        """Exactly `BROADCAST_TEXT_CAP` is accepted, and readable in full.

        Sent from `dst`, not `src`: the rate-limit tests spend `src`'s window,
        and this must not depend on whether they ran. The stdout notification
        is clipped to 400 body characters (issue #2) with a `cont` line
        pointing at `messages.log`, where the payload is kept whole.
        """
        text = "z" * shared.BROADCAST_TEXT_CAP
        ws = await control.open_control(bus.port, bus.state("dst"))
        try:
            await ws.send(json.dumps({"op": "broadcast", "text": text}))
            assert (await control.exchange(ws, {"op": "ping"}))["op"] == "pong"
        finally:
            await ws.close()

        msg_id = None
        for peer in ("src", "alpha-1", "alpha-2"):
            header = waiting.read_line(bus.listeners[peer])
            assert header.startswith("[hubbub msg="), (peer, header)
            assert f"truncated={shared.BROADCAST_TEXT_CAP}" in header, (peer, header)
            cont = waiting.read_line(bus.listeners[peer])
            assert " cont] full text " in cont, (peer, cont)
            this_id = header.split("msg=", 1)[1].split(None, 1)[0].split("]")[0]
            msg_id = msg_id or this_id
            assert this_id == msg_id, (peer, header)

        record = [json.loads(line) for line in bus.log_path.read_text().splitlines()
                  if line.strip() and json.loads(line).get("msg_id") == msg_id]
        assert len(record) == 1, record
        assert record[0]["text"] == text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op,size,message", [
        pytest.param("send", shared.TEXT_CAP + 1,
                     "text exceeds direct send cap", id="send"),
        pytest.param("broadcast", shared.BROADCAST_TEXT_CAP + 1,
                     "text exceeds broadcast cap", id="broadcast"),
    ])
    async def test_text_too_long_via_raw_frame(self, bus, op, size, message):
        """Both caps, at cap+1. Neither is reachable through `send.py --text`:
        10 MB and 256 KB both exceed Linux `MAX_ARG_STRLEN` (128 KiB per argv
        string), and the helper takes its text from argv only."""
        log_size = bus.log_size()
        frame = {"op": op, "text": "y" * size}
        if op == "send":
            frame["to"] = "src"
        ws = await control.open_control(bus.port, bus.state("dst"))
        try:
            reply = await control.exchange(ws, frame, timeout=15.0)
            assert reply["op"] == "error", reply
            assert reply["code"] == shared.ErrorCode.TEXT_TOO_LONG, reply
            assert reply["message"] == message, reply
        finally:
            await ws.close()
        assert waiting.read_line(bus.listeners["src"], timeout=1.0) == ""
        assert bus.log_size() == log_size

    def test_bus_still_delivers_after_rejected_frames_via_cli(self, bus):
        """The bus survived every rejection above: `src` still reaches `dst`.

        Last in the class on purpose — a rejected frame that took the server
        or a listener down with it would show up here rather than as a
        confusing failure in whatever ran next.
        """
        r = bus.helper("send.py", "src", "--to", "dst", "--text", "alive")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "alive" in waiting.read_line(bus.listeners["dst"])


@pytest.mark.slow
class TestNoServerViaCli:
    """The two cases with no bus behind them, on a per-test data dir.

    These take the **function-scoped** `tmp_data_dir`/`free_port` rather than
    the class bus: the dead-server case destroys its own bus, and the mid-send
    case calls `shared.write_server_identity`, which resolves `shared.data_dir()`
    in the pytest process and so needs `HUBBUB_DATA_DIR` set there — which is
    exactly what `tmp_data_dir` does and what the class bus deliberately
    undoes.
    """

    def _pair(self, tmp_data_dir, port) -> dict:
        """`src` (ppid 70021) and `dst` (70022) on a freshly elected server.

        Two listeners, not one: `--to dst` under `dst`'s own ppid resolves to
        the sender and is refused as `cannot send to self`, which would make
        the positive prove nothing about the identity check.
        """
        procs = {}
        for name, ppid in (("src", 70021), ("dst", 70022)):
            procs[name] = _spawn_listener(port, name, tmp_data_dir, ppid)
            assert _wait_for_state(tmp_data_dir, ppid), f"{name} never registered"
        return procs

    def test_send_delivers_while_server_alive_via_cli(self, tmp_data_dir, free_port):
        """The positive half: the same bus, before anything is killed."""
        procs = {}
        try:
            procs = self._pair(tmp_data_dir, free_port)
            r = _run_helper("send.py", tmp_data_dir, 70021,
                            "--to", "dst", "--text", "hi")
            assert r.returncode == 0, r.stdout + r.stderr
            assert "hi" in waiting.read_line(procs["dst"])
        finally:
            for p in procs.values():
                _reap(p)
            _kill_server_in(tmp_data_dir, free_port)

    def test_dead_server_identity_check_via_cli(self, tmp_data_dir, free_port):
        """No live `server.py` behind the pidfile: refuse before sending the token.

        Order matters. The listener is SIGKILLed **first** and `wait()`ed: its
        `.session` survives (SIGKILL bypasses `atexit`), and with it gone
        nothing can re-elect a server and race the helper — `send.py` only
        ever calls `verify_server_identity`, never `spawn.ensure_server_running`.

        Then the server is SIGKILLed and the test waits for its pid to be
        **reaped**, not merely signalled. An unreaped SIGKILLed pid is a
        zombie: `os.kill(pid, 0)` succeeds on it, the `.meta` still matches,
        and `psutil.Process(pid).cmdline()` raises `ZombieProcess` — a
        `NoSuchProcess` subclass — so `verify_server_identity` returns True and
        `send.py` prints `connect failed:` instead. That is #55; delete this
        wait when it lands.
        """
        procs = {}
        try:
            procs = self._pair(tmp_data_dir, free_port)
            state_path = tmp_data_dir / "clients" / "70021.session"
            pid_path = tmp_data_dir / f"server.{free_port}.pid"
            assert waiting.wait_for(pid_path.exists), "no server was elected"
            server_pid = int(pid_path.read_text().strip())

            for p in procs.values():
                p.send_signal(signal.SIGKILL)
                p.wait(timeout=10)
            os.kill(server_pid, signal.SIGKILL)

            def reaped() -> bool:
                # A plain `lambda: psutil.Process(...)` would not do: a zombie
                # answers `status()` without raising, and `wait_for` swallows
                # only OSError, so the NoSuchProcess of the reaped case would
                # propagate on the first successful poll instead of returning
                # True.
                try:
                    psutil.Process(server_pid).status()
                except psutil.NoSuchProcess:
                    return True
                return False

            assert waiting.wait_for(reaped), "the server pid was never reaped"

            r = _run_helper("send.py", tmp_data_dir, 70021,
                            "--to", "dst", "--text", "hi", timeout=5.0)
            assert r.returncode == 1, r.stdout + r.stderr
            assert (f"server identity check failed (127.0.0.1:{free_port} "
                    "not held by bin/server.py)") in r.stderr, r.stderr
            assert state_path.exists(), (
                "the helper never reached `hello`, so nothing should have "
                "unlinked the state file")
        finally:
            for p in procs.values():
                _reap(p)
            _kill_server_in(tmp_data_dir, free_port)

    # -- the connection dies after `hello` ----------------------------------

    def _write_state(self, tmp_data_dir, port, listener_pid, token) -> Path:
        """A `.session` written from scratch, pointing at the stand-in.

        All four of `discover._REQUIRED_STATE_KEYS` or `send.py` reads the
        session as *not connected* and never opens a socket at all; `host` and
        `port` too, because `send.py:61-62` otherwise dials the defaults (the
        tests that copy a live listener's state inherit them and so never list
        them).
        """
        shared.secure_dir(tmp_data_dir / "clients")
        path = tmp_data_dir / "clients" / "70011.session"
        path.write_text(json.dumps({
            "session_id": str(uuid.uuid4()),
            "name": "self",
            "nonce": "nonce-for-the-standin",
            "token": token,
            "listener_pid": listener_pid,
            "host": "127.0.0.1",
            "port": port,
        }) + "\n")
        return path

    def _start_send(self, tmp_data_dir):
        env = os.environ.copy()
        env["INTER_SESSION_DATA_DIR"] = str(tmp_data_dir)
        env["PYTHONPATH"] = str(REPO)
        env["INTER_SESSION_PPID_OVERRIDE"] = "70011"
        env.update(waiting.coverage_env())
        return subprocess.Popen(
            [sys.executable, str(BIN_DIR / "send.py"),
             "--to", "dst", "--text", "hi"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    async def _drive(self, tmp_data_dir, free_port, handler):
        """Run `send.py` against an in-process stand-in server.

        `_run_helper` cannot be used: its `subprocess.run` blocks, the stand-in
        never gets to answer `hello`, and the child sits until its 10 s
        timeout. Hence `Popen` plus `_await_exit`, the shape
        `TestHandshakeRejectionDuringShutdown` uses.

        The identity stand-in (a sleeping process whose argv carries
        `bin/server.py`, plus `write_server_identity`) is what lets
        `verify_server_identity` pass for a socket living in the pytest
        process, whose own cmdline never could.
        """
        shared.secure_dir(tmp_data_dir)
        token = shared.ensure_token(shared.token_path())
        standin = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", "bin/server.py"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server = proc = None
        try:
            shared.write_server_identity(standin.pid, "127.0.0.1", free_port)
            self._write_state(tmp_data_dir, free_port, standin.pid, token)
            server = await websockets.serve(handler, "127.0.0.1", free_port)
            proc = self._start_send(tmp_data_dir)
            rc = await _await_exit(proc)
            out, err = proc.communicate()
            return rc, out, err
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            if server is not None:
                server.close()
                await server.wait_closed()
            standin.kill()
            standin.wait()

    @pytest.mark.asyncio
    async def test_connection_stays_silent_when_unanswered_via_cli(
            self, tmp_data_dir, free_port):
        """Success is silence: no ack, exit 0 after the 1.0 s wait.

        The handler must `await ws.wait_closed()` rather than return — a
        `websockets` handler that returns performs a clean close, which is the
        *negative* below. A handler that returned here would make this test
        assert the other test's behaviour and pass for the wrong reason.
        """
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({
                "op": "welcome", "session_id": str(uuid.uuid4()),
                "assigned_name": "self", "for_session": "x",
            }))
            await ws.recv()
            await ws.wait_closed()

        rc, out, err = await self._drive(tmp_data_dir, free_port, handler)
        assert rc == 0, (rc, out, err)
        assert err == "", err

    @pytest.mark.asyncio
    async def test_connection_closed_mid_send_via_cli(
            self, tmp_data_dir, free_port):
        """A server that dies mid-send is a message, not a traceback.

        `send.py`'s outer `try:` had one `except`, around the *inner*
        `wait_for(ws.recv())`. A `ConnectionClosed` from the welcome `recv()`,
        the `send()` or the reply `recv()` escaped `asyncio.run` as an uncaught
        traceback — the output a user gets when the bus goes away underneath
        them.
        """
        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({
                "op": "welcome", "session_id": str(uuid.uuid4()),
                "assigned_name": "self", "for_session": "x",
            }))
            await ws.recv()
            await ws.close(code=1011, reason="gone")

        rc, out, err = await self._drive(tmp_data_dir, free_port, handler)
        assert rc == 1, (rc, out, err)
        assert "connection closed before the server answered:" in err, err
        assert "Traceback" not in err, err
