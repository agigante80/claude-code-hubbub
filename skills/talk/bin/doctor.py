"""Report the state of this install's data directory, then exit. Read-only.

fork #15. `migrate_legacy_data_dir` refuses — correctly — when both
`~/.claude/data/inter-session` and `~/.claude/data/hubbub` hold live state
under the same name: a colliding token means the namespace has already forked,
and picking a winner would silently destroy one side's bus. The problem is
what happens next, which until now was nothing:

  1. migration refuses, warns to stderr
  2. for the auto-started monitor that stderr goes to the monitor's output
     file, which the user has no reason to open
  3. `data_dir()` returns `hubbub/` and a fresh token gets minted there
  4. old builds keep using `inter-session/` with the original token
  5. both work, neither can see the other, no error on either side

Same silent-on-both-ends shape as the cross-transport reply bug in
`docs/DELIVERY.md`, which is the kind this project treats as high priority.

**Read-only on purpose.** The ticket also asks for a guided `--repair`. That
means choosing between two live tokens and discarding one, which ends one
side's bus and cannot be undone — a decision for the human in front of the
output, not for a subcommand. This tells them exactly what they need to decide
and gets out of the way.
"""

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
import json
import socket
from datetime import datetime, timezone

_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from bin import shared  # noqa: E402

OK = "ok"
WARN = "warn"
BAD = "PROBLEM"


class Report:
    """Collects lines plus the worst severity seen, so the exit code and the
    headline agree with the body."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.worst = OK

    def add(self, level: str, text: str) -> None:
        self.lines.append((level, text))
        if level == BAD or (level == WARN and self.worst == OK):
            self.worst = level

    def emit(self) -> int:
        for level, text in self.lines:
            prefix = "  " if level == OK else f"  {level}: "
            print(f"{prefix}{text}")
        return 0 if self.worst == OK else 1


def _describe_path(p: Path) -> str:
    """What is actually at this path, distinguishing the cases that matter.

    `exists()` follows symlinks and reports False for a dangling one, which is
    exactly the confusion this whole area keeps producing, so this uses
    `lexists` and reports the link separately from its target.
    """
    if not os.path.lexists(p):
        return "missing"
    if p.is_symlink():
        target = shared.resolve_safe(p)
        if target is None:
            return "symlink → unresolvable (loop, or a target we cannot read)"
        if not p.is_dir():
            return f"symlink → {target} (not a directory: dangling, or a file)"
        return f"symlink → {target}"
    if p.is_dir():
        return "real directory"
    return "a file, not a directory"


def _token_of(d: Path) -> str | None:
    try:
        return (d / "token").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _live_clients(d: Path) -> list[str]:
    """Never raises. `Path.glob` on an unreadable directory raises
    PermissionError on CPython 3.12 and returns empty on 3.14 — and an
    unreadable data dir is precisely one of the states this command exists to
    report, so crashing there made the diagnostic useless exactly when it was
    needed. Caught by `make test-system` (#24); `make test` alone was green."""
    out: list[str] = []
    try:
        entries = sorted((d / "clients").glob("*.session"))
    except OSError:
        return out
    for f in entries:
        try:
            s = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = s.get("listener_pid")
        if isinstance(pid, int) and shared.safe_pid_alive(pid):
            out.append(f"{s.get('name', '?')} (pid {pid})")
    return out


def _port_holder(host: str, port: int) -> tuple[str, str]:
    """(severity, description). Returns the level too, because the first
    version always reported this line as `ok`: the headline said "healthy"
    while the body said the bus was down with no pidfile. A diagnostic whose
    summary disagrees with its own findings is worse than none."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            pass
    except OSError:
        # Not a fault: no server runs until a client elects one.
        return OK, "nothing is listening"
    pid_path = shared.pidfile_path(port, host)
    if not pid_path.exists():
        # The exact shape of the pre-flock election bug: a live listener whose
        # identity file was deleted by the loser's cleanup. Every client then
        # refuses to connect and the bus is down with zero clients.
        return BAD, ("something is listening but there is NO pidfile — this is "
                     "the identity-wipe shape; kill the listener and let a "
                     "client re-elect")
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return BAD, "listening, but the pidfile is unreadable"
    alive = shared.safe_pid_alive(pid)
    verified = shared.verify_server_identity(host, port)
    level = OK if (alive and verified) else BAD
    return level, (f"pid {pid}, {'alive' if alive else 'DEAD'}, "
                   f"identity {'verified' if verified else 'FAILED'}")


def build_report(port: int, host: str) -> Report:
    r = Report()
    legacy = shared.legacy_data_dir()
    new = shared.default_data_dir()
    override = shared.env("DATA_DIR")

    live = shared.data_dir()
    r.add(OK, f"live data dir: {live}")
    if override:
        r.add(WARN, f"overridden by HUBBUB_DATA_DIR={override} — the paths "
                    f"below are informational only")

    r.add(OK, f"new path  {new}: {_describe_path(new)}")
    r.add(OK, f"legacy    {legacy}: {_describe_path(legacy)}")
    r.add(OK, f"migration marker: "
              f"{'present' if (new / shared.MIGRATION_MARKER).is_file() else 'absent'}")

    parked = sorted(legacy.parent.glob("inter-session.pre-rename*"))
    if parked:
        r.add(WARN, "parked leftovers from a partial migration, safe to delete "
                    "once you have looked: " + ", ".join(p.name for p in parked))

    # The fork itself: two real directories, each with its own token.
    legacy_is_link = legacy.is_symlink()
    if os.path.lexists(legacy) and not legacy_is_link and new.is_dir():
        t_old, t_new = _token_of(legacy), _token_of(new)
        if t_old and t_new and t_old != t_new:
            r.add(BAD,
                  "THE BUS IS FORKED. Both directories are real and hold "
                  "different tokens, so old and new builds are on separate "
                  "buses that cannot see each other and neither reports an "
                  "error.")
            for label, d in (("legacy", legacy), ("new", new)):
                clients = _live_clients(d)
                try:
                    mtime = (d / "token").stat().st_mtime
                except OSError:
                    mtime = 0
                when = (datetime.fromtimestamp(mtime, tz=timezone.utc)
                        .isoformat(timespec="seconds") if mtime else "?")
                r.add(BAD, f"  {label} {d}: token mtime {when}, "
                           f"live listeners: {', '.join(clients) or 'none'}")
            r.add(BAD,
                  "  Pick the side with the listeners you want to keep, stop "
                  "the others, delete the losing directory's token, and let a "
                  "client re-elect. Deliberately not automated: choosing here "
                  "ends one side's bus and cannot be undone.")
        elif t_old and t_new and t_old == t_new:
            r.add(WARN, "both directories are real but share a token, so the "
                        "bus is intact; the migration still has not completed")
    elif legacy_is_link and not legacy.is_dir():
        r.add(BAD, "the legacy path is a symlink that leads nowhere. Old "
                   "builds hardcode it and will fail to start; new builds are "
                   "fine. Remove it, or point it at the new directory.")

    # An unreadable legacy target reads as live state and takes the whole
    # startup down with an uncaught PermissionError out of ensure_token.
    if os.path.lexists(legacy) and legacy.is_dir():
        try:
            os.listdir(legacy)
        except PermissionError:
            r.add(BAD, f"{legacy} exists but cannot be read. State appears to "
                       f"be there, so we route to it and every entry-point "
                       f"then dies at startup. Fix the permissions, or move "
                       f"the directory aside.")

    if getattr(shared, "_unmigrated_this_run", False):
        r.add(WARN, "this process could not confirm the migration; it is "
                    "using the legacy path for safety")

    port_level, port_text = _port_holder(host, port)
    r.add(port_level, f"port {host}:{port}: {port_text}")
    clients = _live_clients(live)
    r.add(OK, f"live listeners: {', '.join(clients) if clients else 'none'}")
    return r


def main() -> int:
    shared.migrate_legacy_data_dir()
    parser = argparse.ArgumentParser(
        description="Report the state of hubbub's data directory. Read-only.")
    parser.add_argument("--port", type=int, default=shared.DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    try:
        report = build_report(args.port, args.host)
    except Exception as e:  # noqa: BLE001 - see below
        # Belt and braces over the per-probe guards. This command is reached
        # precisely when the install is in a state nobody anticipated, so a
        # traceback here is a diagnostic failing at its one job. Reporting the
        # exception is still useful; dying is not.
        print("hubbub doctor: NEEDS ATTENTION")
        print(f"  {BAD}: the check itself failed: {type(e).__name__}: {e}")
        print(f"  {BAD}: that is itself a finding — the data directory is in a "
              f"state this command could not read.")
        return 1
    headline = {OK: "healthy", WARN: "needs a look", BAD: "NEEDS ATTENTION"}
    print(f"hubbub doctor: {headline[report.worst]}")
    return report.emit()


if __name__ == "__main__":
    sys.exit(main())
