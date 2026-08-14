"""List connected agent sessions, then exit. role=control."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists.
import os
import sys
from pathlib import Path
_DATA = Path.home() / ".claude" / "data"
# Prefer the current location; fall back to the pre-rename one for installs
# no entry-point has migrated yet (shared.migrate_legacy_data_dir runs in
# main(), which is after this).
_VENV = _DATA / "hubbub" / "venv"
if not (_VENV / "bin" / "python").is_file():
    _VENV = _DATA / "inter-session" / "venv"
_VENV_PY = _VENV / "bin" / "python"
if (not (os.environ.get("HUBBUB_NO_REEXEC")
         or os.environ.get("INTER_SESSION_NO_REEXEC"))
        and _VENV_PY.is_file()
        and Path(sys.prefix).resolve() != _VENV.resolve()):
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Imported at module scope, so a missing dep must be caught *here* — a bare
# `import websockets` would abort with a traceback before main() ever runs,
# and the friendly "dependencies missing" line the skill tells the agent to
# react to would never be printed.
_MISSING_DEP: ImportError | None = None
try:
    import websockets
    # psutil is guarded too, even though it is only imported lazily deeper in.
    # Without it find_cc_ancestor_pid() returns -1 and resolve_listener_key()
    # falls back to getppid() — but the monitor and the helper CLIs are
    # spawned by *different* subshells, so they then compute different keys,
    # the ppid flock stops deduping and self-discovery breaks. Silently. A
    # half-installed runtime should say so, not degrade into that.
    import psutil  # noqa: F401
except ImportError as _e:
    websockets = None  # type: ignore[assignment]
    _MISSING_DEP = _e

from bin import shared, discover


def _format_since(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    delta = datetime.now(tz=timezone.utc) - t
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


async def _run(args) -> int:
    state, state_path = discover.find_listener_state_with_path()
    if state is None:
        if args.self:
            print("not connected")
            return 0
        print("not connected; run /hubbub:talk in this Claude Code session first",
              file=sys.stderr)
        return 1

    if args.self:
        listener_pid = state.get("listener_pid", 0)
        # The flock, not the pid, decides. It is keyed to this session and
        # released by the kernel however the monitor dies, so it cannot confuse
        # a reused pid — or another session's monitor — for ours. That matters
        # because the pid printed below is what the disconnect flow tells an
        # agent to kill.
        lock_path = state_path.with_name(
            state_path.name[: -len(".session")] + ".lock")
        # Both signals, pid first. The flock proves *a* monitor holds this
        # session's lock but not that it wrote this state file: a respawned
        # monitor takes the lock in run() and writes state only after the
        # server is up, so during reconnect backoff the lock is held while the
        # file still names the previous, dead pid — which is the pid the
        # disconnect flow would tell an agent to kill. Checking the pid first
        # also keeps the flock probe off the path when it is already answered.
        try:
            pid_alive = shared.safe_pid_alive(int(listener_pid))
        except (TypeError, ValueError):
            # Hand-edited or foreign state file. `--self` is what the
            # disconnect flow runs, so a traceback here would break it.
            pid_alive = False
        if not (pid_alive and shared.listener_lock_held(lock_path)):
            # No live listener. TOCTOU-safe cleanup — unlink_if_matches
            # re-checks under the same flock and refuses if one reappeared.
            if discover.unlink_if_matches(state_path, state):
                print("not connected (stale state cleaned up)")
            else:
                # It refused, so don't claim we tidied up.
                print("not connected (stale state left in place)")
            return 0
        print(f"name={state.get('name', '') or '(unnamed)'}")
        print(f"session_id={state['session_id']}")
        print(f"listener_pid={listener_pid}")
        print(f"host={state.get('host', '127.0.0.1')}")
        print(f"port={state.get('port', shared.DEFAULT_PORT)}")
        return 0

    host = state.get("host", "127.0.0.1")
    port = state.get("port", shared.DEFAULT_PORT)
    token = state["token"]

    if not shared.verify_server_identity(host, port):
        print(f"server identity check failed ({host}:{port} not held by bin/server.py)",
              file=sys.stderr)
        return 1

    try:
        ws = await websockets.connect(
            f"ws://{host}:{port}/", max_size=shared.WS_FRAME_CAP,
        )
    except OSError as e:
        print(f"connect failed: {e}", file=sys.stderr)
        return 1

    try:
        await ws.send(json.dumps({
            "op": "hello",
            "session_id": str(uuid.uuid4()),
            "name": "",
            "label": "",
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "role": shared.Role.CONTROL.value,
            "for_session": state["session_id"],
            "nonce": state["nonce"],
            "token": token,
        }))
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        if welcome.get("op") == "error":
            code = welcome.get("code", "")
            if code in (shared.ErrorCode.UNKNOWN_PEER, shared.ErrorCode.UNAUTHORIZED):
                discover.unlink_if_matches(state_path, state)
                print("not connected; run /hubbub:talk in this Claude Code session first",
                      file=sys.stderr)
                return 1
            print(f"hello error: {code}", file=sys.stderr)
            return 1
        await ws.send(json.dumps({"op": "list"}))
        resp_raw = await ws.recv()
        resp = json.loads(resp_raw)
        if resp.get("op") != "list_ok":
            print(f"unexpected response: {resp}", file=sys.stderr)
            return 1
        print(f"{'NAME':<24} {'LABEL':<24} {'CWD':<40} {'SINCE':<8} ID")
        for s in resp["sessions"]:
            name = s.get("name", "") or "(unnamed)"
            label = shared.sanitize_label_for_display(s.get("label", ""))
            cwd = s.get("cwd", "")
            if len(cwd) > 38:
                cwd = "…" + cwd[-37:]
            since = _format_since(s.get("since", ""))
            sid = s.get("session_id", "")[:8]
            print(f"{name:<24} {label:<24} {cwd:<40} {since:<8} {sid}")
    finally:
        await ws.close()
    return 0


def main() -> int:
    # Move ~/.claude/data/inter-session → …/hubbub if this install predates
    # the rename. No-op once done; leaves a symlink so older builds still
    # running on this machine share one token/lock/pidfile with us.
    shared.migrate_legacy_data_dir()
    parser = argparse.ArgumentParser()
    parser.add_argument("--self", action="store_true",
                        help="print only this session's status")
    args = parser.parse_args()
    if _MISSING_DEP is not None:
        print(f"dependencies missing — run /hubbub:talk install-deps ({_MISSING_DEP})",
              file=sys.stderr)
        return 1
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
