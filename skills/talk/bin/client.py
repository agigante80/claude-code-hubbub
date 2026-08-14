"""Long-lived per-session client. Stdout = notifications to Claude."""

from __future__ import annotations

# Bootstrap: re-exec under the project's isolated venv if it exists,
# so we use isolated deps (websockets, psutil) rather than the user's
# system/user-level Python. Tests opt out via HUBBUB_NO_REEXEC=1.
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
import atexit
import errno
import fcntl
import json
import logging
import random
import secrets
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

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

# Allow running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import shared, spawn, profile

log = logging.getLogger("inter-session.client")


def _print_line(line: str) -> None:
    """Emit a single notification line to stdout, flushed."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _format_msg(msg: dict) -> str:
    sanitized = shared.sanitize_for_stdout(msg.get("text", ""))
    truncated, was_truncated, full_len = shared.truncate_for_stdout(sanitized)
    from_name = msg.get("from_name") or msg.get("from", "?")[:8]
    from_label = shared.sanitize_label_for_display(msg.get("from_label", ""))
    msg_id = msg.get("msg_id", "")
    label_part = f' "{from_label}"' if from_label else ""
    if was_truncated:
        prefix = f'[inter-session msg={msg_id} from="{from_name}"{label_part} truncated={full_len}]'
    else:
        prefix = f'[inter-session msg={msg_id} from="{from_name}"{label_part}]'
    return f"{prefix} {truncated}"


def _format_truncation_pointer(msg_id: str, full_len: int) -> str:
    log_path = shared.messages_log_path()
    return f"[inter-session msg={msg_id} cont] full text {full_len} bytes at {log_path}"


def _write_session_state(ppid: int, state: dict) -> None:
    """Atomically write the listener state file (0600), so helpers never
    observe a partially-written file."""
    shared.secure_dir(shared.clients_dir())
    shared.atomic_write_text(shared.client_session_path(ppid), json.dumps(state) + "\n")


def _delete_session_state(ppid: int) -> None:
    """Best-effort cleanup of `.session` on graceful exit. SIGKILL bypasses
    atexit; helpers handle that case via the `unknown_peer` path.

    Note: the `.lock` file is intentionally NOT unlinked. flock is fd-scoped
    and the kernel releases it when the process dies; the file can stay on
    disk indefinitely. Unlinking it would create a TOCTOU window where a
    new client's flock applies to a different inode than another concurrent
    actor's flock attempt — which can lead to two clients both holding "the
    lock" on different inodes for the same path.
    """
    try:
        shared.client_session_path(ppid).unlink()
    except OSError:
        pass


def _resolve_ppid() -> int:
    """Resolve the state-file key (also the dedup-lock key) for this listener.

    Delegates to shared.resolve_listener_key, which prefers the CC main process
    pid in real Claude Code so helper CLIs (run from a *different* Bash
    subshell than the monitor) can find the same key.
    """
    return shared.resolve_listener_key()


def _acquire_ppid_lock(ppid: int, attempts: int = 4) -> Optional[int]:
    """Return an open fd holding an exclusive lock for this ppid, or None if
    already held.

    Retries briefly before concluding "already held". A real holder keeps the
    lock for its whole lifetime, so a contention that clears in milliseconds is
    someone *probing* — `list.py --self` takes it non-blocking to decide
    staleness. Without the retry that probe can land in a starting monitor's
    acquire and make it exit as a spurious duplicate, which `when: "always"`
    makes routine by running status checks and session starts concurrently.
    """
    shared.secure_dir(shared.clients_dir())
    lock_path = shared.client_lock_path(ppid)
    for attempt in range(attempts):
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            os.close(fd)
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if attempt < attempts - 1:
                time.sleep(0.05)
    return None


def _read_existing_session_state(ppid: int) -> Optional[dict]:
    """Read the existing listener's session-state file. Used when our
    flock acquire fails so the caller can embed the existing
    connection's identity (name, listener_pid, session_id) in the
    'already running' error message — saves a follow-up `list.py --self`
    round-trip in the skill's connect flow."""
    try:
        path = shared.client_session_path(ppid)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _print_unless_auto(line: str, from_monitor: bool) -> None:
    """Housekeeping notices that are worth a notification when the *user* asked
    to connect, and pure noise when CC auto-started the monitor.

    **The rule, so it stops getting re-litigated:** route a line here only if
    it reports a *normal outcome* — auto-named from cwd, a duplicate monitor,
    deps not installed yet. A line that reports a **fault preventing the
    connection** stays on stdout even when auto-started, because with
    `when: "always"` the user no longer runs `/hubbub:talk connect`, so stderr
    is somewhere nobody looks. That is the whole point of always-on, and it is
    exactly why the port-squatter check must not be quiet: it is the
    defense-in-depth against a localhost squatter harvesting the bearer token,
    and silencing it means a squatted port produces no signal anywhere.

    With `when: "always"` this runs in every session on the machine, including
    ones whose user has never used hubbub — so a stdout line here means a
    notification before they have typed anything. stderr still reaches the
    monitor's output file, so nothing is lost, it just stops interrupting."""
    if from_monitor:
        print(line, file=sys.stderr, flush=True)
    else:
        _print_line(line)


class Client:
    def __init__(
        self,
        port: int = shared.DEFAULT_PORT,
        host: str = "127.0.0.1",
        name: str = "",
        label: str = "",
        idle_shutdown_minutes: float = 10,
        ppid: Optional[int] = None,
        verbose: bool = False,
        max_collision_retries: int = 3,
        from_monitor: bool = False,
    ):
        self.port = port
        self.host = host
        self.name = name
        self.label = label
        self.idle_shutdown_minutes = idle_shutdown_minutes
        # CC auto-started us: routine housekeeping notices go to stderr rather
        # than becoming a notification in a session nobody asked to connect.
        self.from_monitor = from_monitor
        self.ppid = ppid if ppid is not None else _resolve_ppid()
        self.verbose = verbose
        self.session_id = str(uuid.uuid4())
        self.nonce = secrets.token_urlsafe(16)
        self._stop = asyncio.Event()
        self._lock_fd: Optional[int] = None
        self._max_collision_retries = max_collision_retries
        self._collision_retries = 0
        # Every name this session has asked for. Passed as `taken` when we
        # generate our own candidates, so a fallback cannot re-offer a name we
        # already know is in use — which is the same "burns a retry per hop"
        # loop the server-side filter exists to prevent.
        self._tried_names: set[str] = {name} if name else set()
        self._connect_task: Optional[asyncio.Task] = None

    def stop(self) -> None:
        self._stop.set()
        t = self._connect_task
        if t is not None and not t.done():
            t.cancel()

    async def run(self) -> int:
        # Per-CC-session dedup lock (skill mode). Plugin mode uses monitor name dedup.
        self._lock_fd = _acquire_ppid_lock(self.ppid)
        if self._lock_fd is None:
            info = _read_existing_session_state(self.ppid)
            if info:
                # Housekeeping when CC respawned us (e.g. /reload-plugins) into
                # a session that already has a monitor: the session is
                # connected, which is what the user wanted. Only worth a
                # notification when they asked for the connection themselves.
                _print_unless_auto(
                    "[inter-session] another monitor for this session is already running "
                    f"— name={info.get('name', '')!r}, "
                    f"listener_pid={info.get('listener_pid', '')}, "
                    f"session_id={info.get('session_id', '')}; exiting",
                    self.from_monitor,
                )
            else:
                _print_unless_auto(
                    "[inter-session] another monitor for this session is already "
                    "running — exiting", self.from_monitor)
            return 0

        # Best-effort: delete state file on graceful exit so helpers don't
        # see a stale entry pointing at our dead session_id.
        atexit.register(_delete_session_state, self.ppid)

        try:
            backoff = shared.RECONNECT_BACKOFF_MIN_S
            while not self._stop.is_set():
                spawn.ensure_server_running(self.port, self.host, self.idle_shutdown_minutes)
                self._connect_task = asyncio.create_task(self._connect_and_serve())
                try:
                    await self._connect_task
                    backoff = shared.RECONNECT_BACKOFF_MIN_S  # successful disconnect; reset
                except asyncio.CancelledError:
                    # stop() cancels the active connect task to break out of
                    # blocking recv loops on SIGTERM/SIGINT.
                    pass
                except (ConnectionRefusedError, OSError) as e:
                    if self.verbose:
                        log.info("connect failed: %s", e)
                except websockets.InvalidHandshake as e:
                    _print_line(
                        f"[inter-session] connected to a non-inter-session "
                        f"service on port {self.port}: {e}")
                    return 1
                except websockets.ConnectionClosed:
                    pass
                finally:
                    self._connect_task = None
                if self._stop.is_set():
                    break
                jitter = backoff * shared.RECONNECT_JITTER_FRAC
                delay = backoff + random.uniform(-jitter, jitter)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, shared.RECONNECT_BACKOFF_MAX_S)
            return 0
        finally:
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass

    async def _connect_and_serve(self) -> None:
        # Defense-in-depth against port squatting: refuse to send the bearer
        # token to a process that doesn't claim to be our server.
        if not shared.verify_server_identity(self.host, self.port):
            # Loud even when auto-started. It only fires when the port is
            # held by something that is not our server — a fault, not routine
            # housekeeping — and it is the token-harvesting defense. N sessions
            # each reporting a genuinely squatted port beats no session
            # reporting it at all.
            _print_line(
                "[inter-session] server identity check failed "
                f"(port {self.port} is held by something that isn't bin/server.py); "
                "refusing to connect"
            )
            self._stop.set()
            return
        token = shared.ensure_token(shared.token_path())
        async with websockets.connect(
            f"ws://{self.host}:{self.port}/",
            max_size=shared.WS_FRAME_CAP,
        ) as ws:
            await ws.send(json.dumps({
                "op": "hello",
                "session_id": self.session_id,
                "name": self.name,
                "label": self.label,
                "cwd": os.getcwd(),
                "pid": os.getpid(),
                "role": shared.Role.AGENT.value,
                "token": token,
                "nonce": self.nonce,
            }))
            welcome_raw = await ws.recv()
            welcome = json.loads(welcome_raw)
            if welcome.get("op") == "error":
                code = welcome.get("code", "")
                if code == shared.ErrorCode.NAME_TAKEN:
                    # Only adopt a candidate we know will pass: an older
                    # server suggests a bare f"{name}-{i}", which NAME_RE
                    # rejects for a 39-40 char name, and adopting it turns a
                    # recoverable collision into `hello rejected: invalid_name`
                    # and stops the monitor.
                    candidates = [c for c in welcome.get("candidates", [])
                                  if isinstance(c, str) and shared.validate_name(c)]
                    if not candidates:
                        # Filtering everything out must not become an instant
                        # give-up — a pre-0.2.0 server is still electable on a
                        # part-migrated fleet, and it suggests exactly the
                        # over-long names we just discarded. Generate our own.
                        candidates = shared.suffixed_name_candidates(
                            self.name, taken=self._tried_names)
                    if (candidates and
                            self._collision_retries < self._max_collision_retries):
                        # Auto-retry with server's first suggestion. Common case:
                        # two CC sessions start in the same cwd, propose the same
                        # name, second retries to <name>-2 transparently.
                        new_name = candidates[0]
                        old_name = self.name
                        self.name = new_name
                        self._tried_names.add(new_name)
                        self._collision_retries += 1
                        _print_unless_auto(
                            f"[inter-session] name {old_name!r} taken; "
                            f"using {new_name!r}",
                            self.from_monitor,
                        )
                        return  # main loop will reconnect with self.name = new_name
                    # Out of retries — surface and stop. Caller picks a new name.
                    # Stays loud even when auto-started, unlike the notices
                    # above: those report one machine-wide fact N times, but
                    # this is *this* session failing to join at all. Several
                    # sessions opened in one repo exhaust the retry budget on
                    # the same cwd-derived name, and silence would leave them
                    # quietly missing from `list` with nothing to explain it.
                    # SKILL.md documents a user-facing reaction to this line.
                    _print_line(
                        f"[inter-session] name {self.name!r} taken after "
                        f"{self._collision_retries} retries; "
                        f"run /hubbub:talk connect <other-name>"
                    )
                    self._stop.set()
                    return
                # `unauthorized` here is the documented symptom of a forked
                # token namespace, which is a fault, not housekeeping.
                _print_line(
                    f"[inter-session] hello rejected: {code} "
                    f"{welcome.get('message', '')}")
                self._stop.set()
                return
            if welcome.get("op") != "welcome":
                _print_line(
                    f"[inter-session] unexpected hello response: {welcome}")
                return

            # Budget is per-collision-episode, not per-process. The monitor now
            # lives for the whole session and reconnects across every server
            # restart and idle-shutdown, so without this a session that
            # resolved three *separate* collisions over some hours would treat
            # the fourth as exhaustion and drop off the bus for good.
            self._collision_retries = 0

            _write_session_state(self.ppid, {
                "session_id": self.session_id,
                "name": self.name,
                "label": self.label,
                "token": token,
                "nonce": self.nonce,
                "listener_pid": os.getpid(),
                "host": self.host,
                "port": self.port,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
            })

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    op = payload.get("op")
                    if op == "msg":
                        line = _format_msg(payload)
                        _print_line(line)
                        sanitized = shared.sanitize_for_stdout(payload.get("text", ""))
                        truncated, was_truncated, full_len = shared.truncate_for_stdout(sanitized)
                        if was_truncated:
                            _print_line(_format_truncation_pointer(payload.get("msg_id", ""), full_len))
                    elif op in ("peer_joined", "peer_left", "renamed", "relabeled"):
                        if self.verbose:
                            _print_line(f"[inter-session] {op}: {payload}")
                    elif op == "pong":
                        pass
                    else:
                        if self.verbose:
                            _print_line(f"[inter-session] {op}: {payload}")
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(shared.PING_INTERVAL_S)
                await ws.send(json.dumps({"op": "ping"}))
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                return


def _resolve_label(cli_label, env_label, cwd=None) -> str:
    """Resolve (and persist) the session's display label. Precedence:

      1. `--label` given (cli_label is not None): validate, persist it for this
         project ('' clears the persisted label), and use it.
      2. else `$HUBBUB_LABEL` (env_label truthy): validate and use as a
         one-off runtime override — NOT persisted.
      3. else: load the persisted per-project label (or '').

    Raises ValueError(bad_label) if an explicitly-supplied label is invalid, so
    the caller can surface it and exit.
    """
    if cli_label is not None:
        if not shared.validate_label(cli_label):
            raise ValueError(cli_label)
        return profile.resolve_label(cli_label, cwd)
    if env_label:
        if not shared.validate_label(env_label):
            raise ValueError(env_label)
        return env_label
    return profile.resolve_label(None, cwd)


def _env_int(*keys, default: int) -> int:
    for k in keys:
        v = os.environ.get(k)
        if v:
            try:
                return int(v)
            except ValueError:
                continue
    return default


def _env_float(*keys, default: float) -> float:
    for k in keys:
        v = os.environ.get(k)
        if v:
            try:
                return float(v)
            except ValueError:
                continue
    return default


def main() -> int:
    # Our stdout is the notification channel, so hard migration failures —
    # the ones that say "resolve by hand" — should reach it rather than dying
    # in the monitor's output file. Informational ones stay on stderr.
    shared.set_migration_reporter(_print_line)
    # Move ~/.claude/data/inter-session → …/hubbub if this install predates
    # the rename. No-op once done; leaves a symlink so older builds still
    # running on this machine share one token/lock/pidfile with us.
    shared.migrate_legacy_data_dir()
    # Resolution order for port / idle-shutdown:
    #   1. CLI arg (explicit override)
    #   2. CLAUDE_PLUGIN_OPTION_<KEY>  (set by CC when installed via /plugin install + userConfig)
    #   3. HUBBUB_<KEY> / INTER_SESSION_<KEY>  (manual env override)
    #   4. Hard-coded default
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=shared.env("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int(
        "CLAUDE_PLUGIN_OPTION_PORT", "HUBBUB_PORT", "INTER_SESSION_PORT",
        default=shared.DEFAULT_PORT,
    ))
    parser.add_argument("--name", default=shared.env("NAME", ""))
    # Default None (not "") so we can tell "flag absent" (→ load the persisted
    # per-project label) apart from an explicit "--label ''" (→ clear it).
    parser.add_argument("--label", default=None)
    parser.add_argument("--idle-shutdown-minutes", type=float, default=_env_float(
        "CLAUDE_PLUGIN_OPTION_IDLE_SHUTDOWN_MINUTES", "HUBBUB_IDLE_MINUTES",
        "INTER_SESSION_IDLE_MINUTES",
        default=10,
    ))
    parser.add_argument("--verbose", action="store_true")
    # Set only by monitors.json. Distinguishes "CC auto-started me at session
    # open" from "the user asked for a connection", which is what makes a
    # durable opt-out and a quiet missing-deps exit possible.
    parser.add_argument("--from-monitor", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.from_monitor and shared.autostart_optout_path().exists():
        # `/hubbub:talk auto-start off`. The shipped monitors.json says
        # "always" and a plugin update restores it, so the opt-out has to be
        # enforced here rather than trusted to survive in the plugin dir.
        return 0

    if _MISSING_DEP is not None:
        if args.from_monitor:
            # Auto-started in a session that may never touch the bus, so this
            # must not become a notification at every session open. stderr
            # still lands in the monitor's output file, which is the
            # difference between "quiet" and "no trace anywhere" — a
            # half-finished install-deps (websockets built, psutil didn't)
            # would otherwise show up only as a monitor that exits instantly
            # in every session, with nothing saying why.
            print(f"[inter-session] dependencies missing — run "
                  f"/hubbub:talk install-deps ({_MISSING_DEP})", file=sys.stderr)
            return 0
        _print_line(f"[inter-session] dependencies missing — run /hubbub:talk install-deps ({_MISSING_DEP})")
        return 0

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)

    if args.name and not shared.validate_name(args.name):
        _print_line(f"[inter-session] invalid name {args.name!r}")
        return 1

    try:
        final_label = _resolve_label(args.label, shared.env("LABEL"))
    except ValueError as e:
        _print_line(f"[inter-session] invalid label {e.args[0]!r}")
        return 1

    # Plugin auto-start path: monitors.json doesn't pass --name (so the user
    # doesn't have to set HUBBUB_NAME). Fall back to a name derived from
    # the cwd basename so the listener doesn't show as `(unnamed)`.
    final_name = args.name
    auto_named = False
    if not final_name:
        final_name = shared.auto_name_from_cwd()
        if final_name:
            auto_named = True
            _print_unless_auto(
                f"[inter-session] no --name given; auto-named {final_name!r} "
                f"from cwd (rename with /hubbub:talk rename)",
                args.from_monitor,
            )

    client = Client(
        host=args.host, port=args.port, name=final_name, label=final_label,
        idle_shutdown_minutes=args.idle_shutdown_minutes,
        verbose=args.verbose, from_monitor=args.from_monitor,
    )

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, client.stop)
        except NotImplementedError:
            pass
    try:
        return loop.run_until_complete(client.run())
    except ImportError as e:
        _print_line(f"[inter-session] dependencies missing — run /hubbub:talk install-deps ({e})")
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
