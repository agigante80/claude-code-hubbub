"""Raw WebSocket frames against a *spawned* `bin/server.py`.

Five of the ten `shared.ErrorCode` members cannot be produced by any shipped
CLI: `TEXT_TOO_LONG`'s two caps both exceed Linux `MAX_ARG_STRLEN` and
`send.py --text` is argv-only; `INVALID_NAME` and `INVALID_LABEL` are
pre-validated in `client.py`/`relabel.py` before the frame is built; and no CLI
sends an unknown `op` or a mistyped payload. `docs/plans/behaviour-under-test.md`
(Tier A) accepts a raw-frame helper for exactly those on one condition — the
test names say which path each code took — so everything reached through this
module is named `_via_raw_frame`, never `_via_cli`.

The three coroutines are built on `tests.test_server`'s `_send_op`/`_recv`/
`_recv_until` rather than copies of them, the way `tests/test_client.py`
imports `_run_helper` from `tests.test_helpers`. `_connect` is *not* reused:
it discards its `**kwargs` (`tests/test_server.py:35-36`), and these sockets
need `ping_interval=None`.

**`ping_interval=None` is load-bearing, not tidiness.** The rate-limit tests
are `async` and make blocking `_run_helper`/`waiting.read_line` calls while a
control socket is open. websockets 13.1's legacy client defaults to
`ping_interval=20` and sleeps the full interval before its *first* keepalive
ping, so those tests pass today only because every socket here is younger than
20 s — a condition nothing enforces and no failure would explain. Disabling
the keepalive removes it. The server never pings either (`server.py:118`).

**No `token=` parameter, and no token literal anywhere in this file.** Every
credential comes from the parsed `clients/<ppid>.session` dict the caller
hands in, which is the same place `send.py:62-65` reads it. A helper that let
a test pass a literal token would make the `UNAUTHORIZED` cases prove nothing.
"""

from __future__ import annotations

import json
import os
import uuid

import websockets

from bin import shared
from tests.test_server import _recv, _recv_until, _send_op  # noqa: F401

# `token=None` means "omit the key entirely", which is the one shape no CLI
# can send (`send.py:63` indexes `state["token"]`), so it cannot also mean
# "use the default". This sentinel keeps the two apart.
_OMIT = object()


async def open_raw(port: int):
    """A connection with nothing sent on it yet.

    The first frame is what `server.py:212-226` validates, and a rejection
    there closes the connection — so the non-object, malformed-JSON and
    pre-`hello` cases each need their own fresh socket.
    """
    return await websockets.connect(
        f"ws://127.0.0.1:{port}/",
        max_size=shared.WS_FRAME_CAP,
        ping_interval=None,
    )


async def open_control(port: int, state: dict):
    """A `role=control` connection, welcomed, handed back open.

    `state` is a parsed `.session` dict. Raises on any reply that is not a
    `welcome`, so a caller can never go on to assert against a socket the
    server refused.
    """
    ws = await open_raw(port)
    await _send_op(
        ws,
        "hello",
        session_id=str(uuid.uuid4()),
        name="",
        label="",
        cwd=os.getcwd(),
        pid=os.getpid(),
        role=shared.Role.CONTROL.value,
        for_session=state["session_id"],
        nonce=state["nonce"],
        token=state["token"],
    )
    reply = await _recv(ws)
    if reply.get("op") != "welcome":
        await ws.close()
        raise AssertionError(f"control hello refused: {reply}")
    return ws


async def hello_agent(port: int, state: dict, **overrides):
    """A `role=agent` hello on a fresh connection; returns `(ws, reply)`.

    `overrides` are applied **verbatim** over the default frame, so a mistyped
    value (`name=5`, `session_id=7`) reaches the wire rather than being
    coerced on the way — which is the whole point for the `INVALID_PAYLOAD`
    cases. `token=None` removes the key from the frame.

    The socket is returned open and unclosed: the `INVALID_NAME` positive
    holds it while `list.py` runs so the peer is visible in the table. The
    negatives close it at once (the server has already closed its end).
    """
    ws = await open_raw(port)
    frame = {
        "op": "hello",
        "session_id": str(uuid.uuid4()),
        "name": "",
        "label": "",
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "role": shared.Role.AGENT.value,
        "token": state["token"],
        "nonce": "",
    }
    frame.update(overrides)
    if overrides.get("token", _OMIT) is None:
        del frame["token"]
    await ws.send(json.dumps(frame))
    return ws, await _recv(ws)


async def exchange(ws, frame, timeout: float = 5.0) -> dict:
    """Send `frame` and return the next real reply, parsed.

    A `str` goes to the wire as-is, which is how the malformed-JSON case is
    written. **Anything else** is JSON-encoded — not just a `dict`, because
    the non-object case sends `[1, 2]` and `_send_op` cannot carry a list; it
    always builds `{"op": …}`.

    `_recv_until` with an empty `ops` drops only the `peer_joined`/`peer_left`/
    `renamed` notifications an agent socket sees and returns everything else,
    errors included.
    """
    await ws.send(frame if isinstance(frame, str) else json.dumps(frame))
    return await _recv_until(ws, (), timeout=timeout)
