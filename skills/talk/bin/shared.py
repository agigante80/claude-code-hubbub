"""Shared helpers: paths, validation, sanitization, atomic token, role enum, protocol constants."""

from __future__ import annotations

import enum
import fcntl
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from urllib.parse import quote
from pathlib import Path

DEFAULT_PORT = 9473
WS_FRAME_CAP = 16 * 1024 * 1024
TEXT_CAP = 10 * 1024 * 1024
BROADCAST_TEXT_CAP = 256 * 1024
# Body cap for the stdout notification line that Claude Code's monitor
# delivers to the receiving LLM. Empirically (issue #2), CC clips each
# notification at ~512 chars total, so above this budget the truncated=
# marker and cont-pointer line never reach the LLM. 400 leaves room for
# our prefix (`[inter-session msg=… from="…" "…" truncated=N] `) under
# the 512 limit in typical cases; very long name+label combos may still
# clip, but the cont-pointer line is short and always fits, so the LLM
# still gets the messages.log path for full content.
STDOUT_CAP = 400
MAX_HOPS = 4
PING_INTERVAL_S = 15
PONG_TIMEOUT_S = 30
RECONNECT_BACKOFF_MIN_S = 0.25
RECONNECT_BACKOFF_MAX_S = 4.0
RECONNECT_JITTER_FRAC = 0.2
ELECTION_BIND_RETRIES = 8
BROADCAST_RATE_LIMIT_PER_MIN = 60

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LABEL_MAX_CP = 60
# Structural characters of the stdout notification header
# (`[inter-session … from="…" "<label>"]`). Forbidden in a label so a
# peer-controlled label can never reconstruct or corrupt that header on ANY
# surface that reflects it — the notification line, the `list` table, and the
# raw `from_label` written to messages.log (which the truncated-message flow
# greps and shows to the receiving agent). This boundary reject is the primary
# defense; sanitize_label_for_display neutralizes the same characters at render
# time as belt-and-suspenders for labels that never passed validate_label.
LABEL_FORBIDDEN_CHARS = frozenset('"[]')


class Role(str, enum.Enum):
    AGENT = "agent"
    CONTROL = "control"


class ErrorCode:
    INVALID_NAME = "invalid_name"
    INVALID_LABEL = "invalid_label"
    INVALID_PAYLOAD = "invalid_payload"
    NAME_TAKEN = "name_taken"
    UNKNOWN_PEER = "unknown_peer"
    AMBIGUOUS = "ambiguous"
    TEXT_TOO_LONG = "text_too_long"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    HOP_LIMIT = "hop_limit"
    UNKNOWN_OP = "unknown_op"


def env(key: str, default: str | None = None) -> str | None:
    """Read `HUBBUB_<key>`, falling back to the pre-rename `INTER_SESSION_<key>`.

    Both spellings are supported indefinitely: a user's shell profile or a
    launcher may still export the old names, and silently ignoring them would
    look like the override simply didn't work.
    """
    for prefix in ("HUBBUB_", "INTER_SESSION_"):
        val = os.environ.get(prefix + key)
        if val:
            return val
    return default


MIGRATION_MARKER = ".migrated-from-inter-session"


def legacy_data_dir() -> Path:
    return Path.home() / ".claude" / "data" / "inter-session"


def default_data_dir() -> Path:
    return Path.home() / ".claude" / "data" / "hubbub"


def _migration_lock_path() -> Path:
    # Beside the two directories, not inside either — one of them gets renamed.
    return Path.home() / ".claude" / "data" / ".hubbub-migration.lock"


def _acquire_migration_lock(timeout_s: float = 5.0) -> int | None:
    """Serialize the migration across processes, or None if we can't.

    Every entry-point runs the migration at startup and `monitors.json` ships
    `when: "always"`, so N sessions opening together enter this concurrently —
    and `_drain_into` is reached whenever `hubbub/` already exists, which is
    the documented common upgrade case (`install-deps` created `hubbub/venv`
    first). Every other race here is handled by re-checking disk state, which
    works only because a winner never moves state *backwards*. `_rollback`
    broke that: a loser rolling back could rename a winner's already-migrated
    `clients/` and `messages.log` back into the legacy dir, which the winner
    then parks wholesale as `inter-session.pre-rename` — every connected
    session loses its state file and reports "not connected".

    Polling LOCK_NB rather than blocking, like `spawn._acquire_election_lock`:
    a stuck holder must never wedge every session on the machine at open.
    """
    path = _migration_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as e:
        # Distinct from "a peer holds it", which is benign. Not being able to
        # lock at all means the migration never runs, and data_dir() is pure
        # now — so it returns hubbub/ while the live state stays under
        # inter-session/, the next ensure_token() mints a fresh token there,
        # and connected peers start getting `unauthorized`.
        _migration_error(f"could not open {path}: {e}; not migrating")
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.02)


def migrate_legacy_data_dir() -> None:
    """One-time `inter-session` → `hubbub` move, leaving a symlink behind.

    The symlink is the whole point. Older builds hardcode the legacy path, and
    on a machine mid-upgrade they must keep resolving to the *same* token,
    election lock and pidfile as new builds. Split that namespace and two
    clients each win their own election, both bind the port, and the loser's
    `_unlink_own_identity` wipes the winner's pidfile — the exact race the
    election flock exists to prevent (see CLAUDE.md).

    `os.rename` is atomic within a filesystem and keeps inodes, so flocks held
    by running clients survive it untouched.

    Called explicitly by each entry-point at startup, never from `data_dir()`.
    A path resolver that mutates the filesystem is a resolver that mutates the
    developer's real `$HOME` the first time a test forgets the `tmp_data_dir`
    fixture — which is exactly what happened while this was being written.
    """
    if env("DATA_DIR"):
        # An explicit override means the caller is pointing us at its own
        # directory (tests, debugging). Nothing to migrate, and the legacy
        # path is none of our business.
        return
    new = default_data_dir()
    legacy = legacy_data_dir()
    if not os.path.lexists(legacy) and not (new / MIGRATION_MARKER).is_file():
        # Never had an `inter-session` directory, and nothing was ever migrated
        # here — so there is no migration and no repair to do. Checked before
        # the lock because `_migration_complete` is False forever on such a
        # machine, which would mean every session open and every helper CLI
        # taking a machine-wide flock (and creating one) to learn nothing.
        return
    if _migration_complete(legacy, new):
        # The overwhelmingly common path once migrated: don't make every
        # session open contend for a lock to discover there is nothing to do.
        return
    lock_fd = _acquire_migration_lock()
    if lock_fd is None:
        # Another process holds it, or we cannot lock at all. Either way,
        # racing it is what this lock exists to prevent — but we must also not
        # start writing into the new path behind whoever is migrating.
        _note_unmigrated(legacy, new)
        return
    try:
        if _migration_complete(legacy, new):
            return  # a peer finished while we waited
        if legacy.is_symlink():
            if legacy.resolve() == new.resolve():
                # Migrated by 0.2.0's first cut, which predates the marker.
                # Backfill it, or the repair branch below can never fire for
                # this install if the symlink is later lost.
                _mark_migrated(new)
            else:
                _migration_error(f"{legacy} is a symlink to {legacy.resolve()}, "
                                f"not {new}; leaving it alone")
            return
        if legacy.is_dir():
            new.parent.mkdir(parents=True, exist_ok=True)
            # lexists, like every other existence check here: a *dangling*
            # symlink at hubbub/ reports exists() == False, so this would take
            # the rename branch, fail with ENOTDIR, and report a hand-repair
            # failure on a machine _drain_into could have handled.
            if not os.path.lexists(new):
                os.rename(legacy, new)
            elif not _drain_into(legacy, new):
                return
            # Marker, then symlink, then perms — in that order, and none of
            # them able to abort the next. These run with live state already
            # moved, so a raise here leaves `new` holding the token with no
            # symlink *and* no marker, which is the one state the repair branch
            # above cannot fix: legacy is neither a symlink, nor a dir, nor
            # present, and the marker it looks for was never written.
            _mark_migrated(new)
            _link_legacy(legacy, new)
            # `uv venv` creates the dir with the default umask, so an
            # install-deps-first machine would otherwise leave the bearer
            # token sitting in a 0755 directory. Cosmetic next to the above,
            # hence last: secure_dir swallows its own errors.
            secure_dir(new)
        elif not legacy.exists() and (new / MIGRATION_MARKER).is_file():
            # A previous run moved the directory but died — or lost a race —
            # before creating the symlink. Without repair an older build finds
            # no legacy path, recreates it as a real directory with its own
            # token, and the namespace forks silently.
            _link_legacy(legacy, new)
    except OSError as e:
        # One handler, deciding by the state on disk. A peer finishing between
        # two of our checks surfaces as whatever syscall we happened to be in
        # (ENOENT from a vanished source, EISDIR from renaming a symlink onto
        # the live directory), and with `when: "always"` every session open
        # races every other one. Special-casing FileNotFoundError as "benign"
        # was wrong in the other direction too: it also fires on the late
        # `os.rename(src, aside)`, where returning quietly would leave a
        # half-drained real directory and call it success.
        if _migration_complete(legacy, new):
            return
        if isinstance(e, FileNotFoundError) and not os.path.lexists(legacy) \
                and new.exists() and _await_peer_completion(legacy, new):
            # A peer has done the rename and is a few instructions away from
            # the marker and the symlink. With `when: "always"` N sessions open
            # together, so warning here means N-1 apparent failures on the one
            # run that actually succeeds.
            #
            # The marker wait is what separates that from a peer that won the
            # rename and then died (SIGKILL, OOM) before writing it. Those look
            # identical on disk at this instant, but only the live one goes on
            # to finish — and the dead one leaves the state the repair branch
            # cannot fix, which is precisely what deserves a warning.
            return
        _migration_error(f"could not migrate {legacy} → {new}: {e}")
    finally:
        os.close(lock_fd)
        # From the state on disk, not from which branch we took. Winning the
        # lock and then failing — the live-collision refusal, or any OSError —
        # leaves the legacy directory holding the live token just as surely as
        # never getting the lock does, and those paths used to fall through
        # with the flag still clear.
        _note_unmigrated(legacy, new)


# Regenerable artifacts: colliding copies of these can be set aside without
# asking, because nothing on the bus depends on which one survives. `venv` is
# rebuilt by `/hubbub:talk install-deps`; the marker is our own bookkeeping.
_DISPOSABLE = frozenset({"venv", MIGRATION_MARKER})


def _drain_into(src: Path, dst: Path) -> bool:
    """Move `src`'s entries into `dst`. False (having changed nothing) if the
    two hold live state under the same name.

    `dst` existing does not mean it holds state. `/hubbub:talk install-deps`
    creates `<data-dir>/venv` at the *new* path and is a documented standalone
    command, so a user upgrading from a pre-rename install can easily create
    `hubbub/venv` before any monitor has migrated anything — while the old
    install's own `inter-session/venv` still sits there. Treating that pair as
    a conflict would refuse the migration on exactly the machines it exists
    for, permanently and with no self-repair.
    """
    if src.resolve() == dst.resolve():
        # A peer entry-point completed the whole migration between our
        # is_symlink() and is_dir() checks, so `src` now reaches `dst` through
        # the symlink it just created. Every entry would "collide" with itself
        # and we would report a hand-repair job on a correctly migrated
        # machine. With `when: "always"` every session runs this at open, so
        # the window gets hit for real.
        return True
    # lexists, like _free_aside_path: a *dangling* symlink at dst/token reports
    # exists() == False, so it would not count as a collision and os.rename
    # would silently replace it — walking straight past the "both hold live
    # state, refuse loudly" guard.
    collisions = {e.name for e in src.iterdir()
                  if os.path.lexists(dst / e.name)}
    live = collisions - _DISPOSABLE
    if live:
        # Both sides hold real bus state under the same name, so the fork has
        # already happened. Picking a winner would silently destroy one side's
        # session; a human has to say which token is the live one.
        _migration_error(
            f"cannot merge {src} into {dst}: both hold {sorted(live)}. Old and "
            f"new builds will use separate tokens until this is resolved by hand."
        )
        return False
    # Live state first, and all-or-nothing. Bailing out *after* moving some of
    # it is the same fork by another route: `token` stuck behind while
    # `clients/` and the pidfile have already gone leaves no symlink and no
    # marker, so the next client mints a fresh token, connected peers get
    # `unauthorized`, and the run after that sees `token` on both sides and
    # refuses for good.
    # Sorted: the order decides which entries are already moved when a later
    # one fails, so it should not vary with directory layout between runs.
    pending = sorted((e for e in src.iterdir() if e.name not in collisions),
                     key=lambda e: e.name)
    live_entries = [e for e in pending if e.name not in _DISPOSABLE]
    moved: list[tuple[Path, Path]] = []
    for e in live_entries:
        dest = dst / e.name
        try:
            if os.path.lexists(dest):
                # Recheck immediately before the rename: `collisions` was
                # computed a loop ago, and with `when: "always"` a peer
                # entry-point can call ensure_token() in that window. os.rename
                # would silently replace the live token with the legacy one.
                raise FileExistsError(f"{dest} appeared while draining")
            os.rename(e, dest)
            moved.append((dest, e))
        except OSError as err:
            if _migration_complete(src, dst):
                # A peer finished the whole migration while we were draining,
                # so what looked like a conflict is the winner's own work.
                # `when: "always"` plus every helper CLI running this at
                # startup makes losing the race routine, not exceptional.
                return True
            _migration_warn(f"could not move {e.name} out of {src}: {err}")
            _rollback(moved)
            _migration_error(f"leaving {src} in place; live state must not be "
                            f"relocated behind a running bus")
            return False

    # Everything from here runs with live state already moved, so every exit
    # has to either finish the migration or put it back. Three rounds of review
    # found three separate escapes from this section — a returned False, an
    # unguarded rename, a lexists bug — each leaving `dst` holding the token
    # while `src` stayed a real directory an older build would repopulate. One
    # guard over the lot, rather than a fourth patched branch.
    try:
        stuck = []
        for e in (x for x in pending if x.name in _DISPOSABLE):
            try:
                if os.path.lexists(dst / e.name):
                    stuck.append(e.name)
                    continue
                os.rename(e, dst / e.name)
            except OSError:
                # Regenerable, so a failure here is not worth abandoning the
                # migration over — the set-aside path below takes the rest.
                stuck.append(e.name)
        if stuck:
            _migration_warn(f"could not move {sorted(stuck)} out of {src}")
        _finish_drain(src, dst)
    except OSError as err:
        if _migration_complete(src, dst):
            return True
        _migration_error(f"could not finish draining {src}: {err}")
        _rollback(moved)
        return False
    return True


def _finish_drain(src: Path, dst: Path) -> None:
    """Empty `src` so the caller can replace it with a symlink. Raises OSError
    rather than returning a status: the caller owns the rollback."""
    try:
        src.rmdir()
        return
    except OSError:
        pass
    # Either a disposable collision is still sitting there, or an old build
    # recreated something (e.g. clients/) between the scan and now. Set the
    # remainder aside wholesale so the symlink can still go in — leaving
    # `src` a real directory is the one outcome that forks the namespace.
    aside = _free_aside_path(src)
    if aside is None:
        raise OSError(f"every {src.name}.pre-rename* name is taken")
    leftovers = {e.name for e in src.iterdir()}
    os.rename(src, aside)
    if leftovers - _DISPOSABLE:
        _migration_warn(f"set {aside} aside so {src} could become a "
                        f"symlink; review it before deleting")
    else:
        # Only regenerable files — almost always the pre-rename venv,
        # superseded by the one at the new path. Say what it is rather than
        # sending every upgrading user off to audit a directory.
        _migration_warn(f"superseded {sorted(leftovers)} moved to {aside}; "
                        f"safe to delete")


def _legacy_points_at(legacy: Path, new: Path) -> bool:
    try:
        return legacy.is_symlink() and legacy.resolve() == new.resolve()
    except OSError:
        return False


def _migration_complete(legacy: Path, new: Path) -> bool:
    """The full end state, symlink *and* marker — not just the symlink.

    `_mark_migrated` can fail on its own (read-only mount, ENOSPC, stale perms)
    while the symlink is already correct. Judging by the symlink alone would
    suppress that warning, and the marker never being written means the
    "repair a lost symlink" branch can never fire for this install again."""
    try:
        return _legacy_points_at(legacy, new) and (new / MIGRATION_MARKER).is_file()
    except OSError:
        return False


def _link_legacy(legacy: Path, new: Path) -> None:
    """Point the legacy path at the live one.

    `FileExistsError` here does NOT reliably mean "a peer symlinked it first".
    Between our `os.rename` and this call the legacy path does not exist, and
    an old 0.1.x build starting in that window recreates it as a *real
    directory* with a fresh token — so swallowing the exception would report
    the forked namespace this function exists to prevent as a clean migration.
    Check what is actually on disk instead of inferring it from the error."""
    try:
        legacy.symlink_to(new)
    except FileExistsError:
        if not _legacy_points_at(legacy, new):
            _migration_error(
                f"{legacy} reappeared as a real directory while migrating to "
                f"{new}; an older build has recreated it with its own token. "
                f"Old and new builds will use separate tokens until one of "
                f"them is removed by hand."
            )


def _await_peer_completion(legacy: Path, new: Path, timeout_s: float = 0.5) -> bool:
    """Wait briefly for a peer to finish the migration it just won the rename
    for. A live peer gets there within microseconds; one that died in between
    never will.

    Either the marker *or* the symlink counts. `_mark_migrated` is deliberately
    best-effort — on ENOSPC or a perms problem it warns and the peer goes on to
    link and complete correctly — so a marker-only wait would call a fully
    migrated machine a failure, and stall 500ms before saying so."""
    deadline = time.monotonic() + timeout_s
    while True:
        if (new / MIGRATION_MARKER).is_file() or _legacy_points_at(legacy, new):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _rollback(moved: list[tuple[Path, Path]]) -> None:
    """Undo a partial drain. Leaving live state split across the two paths is
    worse than not migrating at all: the new path would have no token, so the
    next client mints one and every connected peer is dropped."""
    for dest, origin in reversed(moved):
        try:
            os.rename(dest, origin)
        except OSError as back:
            # A rollback that fails leaves state split across both paths.
            _migration_error(f"could not put {origin.name} back: {back}")


def _free_aside_path(src: Path) -> Path | None:
    """First unused `<src>.pre-rename[-N]`. A previous migration may already
    have parked a directory there, and the user is told it is safe to delete
    rather than required to — so a taken name must not be a dead end."""
    for suffix in ("", *(f"-{n}" for n in range(2, 20))):
        candidate = src.with_name(f"{src.name}.pre-rename{suffix}")
        # lexists, not exists: a *dangling* symlink parked here reports
        # exists() == False, so we would hand back an occupied name and the
        # rename onto it fails with ENOTDIR.
        if not os.path.lexists(candidate):
            return candidate
    return None


def _mark_migrated(new: Path) -> None:
    """Best-effort, and deliberately non-fatal: failing to write the marker
    must not stop the symlink going in, because the symlink is the half that
    keeps old and new builds on one token."""
    marker = new / MIGRATION_MARKER
    try:
        if not marker.exists():
            marker.touch()
    except OSError as e:
        _migration_warn(f"could not write {marker}: {e}")


# Entry-points whose stdout *is* a channel someone reads (client.py's monitor
# lines become Claude Code notifications) install a reporter here. Short-lived
# CLIs leave it alone: their stdout is parsed output, and their stderr is
# surfaced by the Bash call that ran them.
_migration_reporter = None


def set_migration_reporter(fn) -> None:
    global _migration_reporter
    _migration_reporter = fn


def _migration_warn(msg: str) -> None:
    """Informational: something was set aside, something regenerable didn't
    move. Never raises — once the aside rename succeeds the migration is past
    the point of no return, and an EPIPE/ENOSPC from a warning after that would
    otherwise reach the caller's rollback and try to move the token back into a
    directory that no longer exists."""
    try:
        print(f"[inter-session] data-dir migration: {msg}", file=sys.stderr)
    except OSError:
        pass


def _migration_error(msg: str) -> None:
    """A failure that leaves the install un-migrated or forked.

    These are the ones that say "must be resolved by hand", and stderr is where
    nobody reads them: `migrate_legacy_data_dir` runs before argument parsing
    in every entry-point, so the monitor's notification channel was never
    offered them. By the rule in client.py's `_print_unless_auto`, a fault that
    prevents the session working stays on the channel the user actually sees.
    """
    line = f"[inter-session] data-dir migration: {msg}"
    # stderr unconditionally, so the record exists even when the escalation
    # below is dropped (an opted-out session must stay silent on stdout).
    try:
        print(line, file=sys.stderr)
    except OSError:
        pass
    if _migration_reporter is not None:
        try:
            _migration_reporter(line)
        except OSError:
            pass


# Set when a run could not perform or confirm the migration. See data_dir().
_unmigrated_this_run = False


def _still_on_legacy(legacy: Path, new: Path) -> bool:
    """Is this run's live state still under the legacy path?

    Not the same question as "is the legacy path a real directory". If we
    completed the move, the marker is in `new` and our state is there — even if
    an older build has since recreated `inter-session/` as a real directory of
    its own, which `_link_legacy` warns about. Answering by directory type sent
    such a run back to read the *other* build's token and orphan its own.
    """
    return (legacy.is_dir() and not legacy.is_symlink()
            and not (new / MIGRATION_MARKER).is_file())


def _note_unmigrated(legacy: Path, new: Path) -> None:
    global _unmigrated_this_run
    _unmigrated_this_run = _still_on_legacy(legacy, new)


def data_dir() -> Path:
    """Pure path resolution — no filesystem side effects. See
    `migrate_legacy_data_dir` for the `inter-session` → `hubbub` move."""
    override = env("DATA_DIR")
    if override:
        return Path(override)
    if _unmigrated_this_run:
        # We could not migrate and could not confirm someone else had, while a
        # real legacy directory still holds the live state. Using `hubbub/`
        # anyway would have ensure_token() mint a token there and
        # secure_dir(clients_dir()) create `clients/` — and a peer that then
        # gets the lock finds both names on both sides, hits the live-collision
        # branch, and refuses by hand forever.
        #
        # Re-checked rather than trusted: the flag is set once at startup but
        # client.py runs for hours, and the migrator has a window between its
        # `os.rename` and `_link_legacy` where the legacy path does not exist.
        # Honouring a stale flag there would `mkdir -p` it back as a real
        # directory with a fresh token — the fork, caused by the guard against
        # it. A stat is a read, not the filesystem mutation data_dir() is
        # required to stay free of.
        if _still_on_legacy(legacy_data_dir(), default_data_dir()):
            return legacy_data_dir()
    return default_data_dir()


def server_log_path() -> Path:
    return data_dir() / "server.log"


def messages_log_path() -> Path:
    return data_dir() / "messages.log"


def token_path() -> Path:
    return data_dir() / "token"


def _identity_stem(port: int = DEFAULT_PORT, host: str | None = None) -> str:
    """Filename stem for server identity.

    Keep the default 127.0.0.1 path as `server.<port>` for compatibility with
    older installs/tests. Non-default hosts include a percent-encoded host
    component so endpoints like localhost:9473 and 127.0.0.1:9473 do not
    clobber each other in a shared data_dir.
    """
    if host is None or host == "127.0.0.1":
        return f"server.{port}"
    return f"server.{quote(host, safe='')}.{port}"


def pidfile_path(port: int = DEFAULT_PORT, host: str | None = None) -> Path:
    """Endpoint-scoped pidfile for a listener host+port."""
    return data_dir() / f"{_identity_stem(port, host)}.pid"


def pidfile_meta_path(port: int = DEFAULT_PORT, host: str | None = None) -> Path:
    return data_dir() / f"{_identity_stem(port, host)}.pid.meta"


def election_lock_path(port: int = DEFAULT_PORT, host: str | None = None) -> Path:
    """Per-endpoint advisory-lock file that serializes the server election, so
    two clients racing to start a server can't both bind() the port (which
    SO_REUSEADDR would otherwise allow before either socket listens) and spawn
    duplicate servers that clobber each other's identity."""
    return data_dir() / f"{_identity_stem(port, host)}.election.lock"


def autostart_optout_path() -> Path:
    """Records "the user turned auto-start off", outside the plugin directory.

    `auto_start.py` expresses the setting by rewriting `when` in the plugin's
    own `monitors/monitors.json`, which `/plugin update` overwrites with the
    shipped file. While the shipped default was lazy that was harmless for
    opt-outs; now that it ships `always`, an update would silently hand
    always-on monitors back to someone who had explicitly turned them off. The
    data dir survives updates, so the opt-out lives here and `client.py`
    honours it when it was started by the monitor.
    """
    return data_dir() / "autostart-off"


def clients_dir() -> Path:
    return data_dir() / "clients"


def client_lock_path(ppid: int) -> Path:
    return clients_dir() / f"{ppid}.lock"


def client_session_path(ppid: int) -> Path:
    return clients_dir() / f"{ppid}.session"


NAME_MAX_LEN = 40
# Only the range this generator itself produces. Stripping any -<digits>
# rewrote `release-2024` into `release-2`, so a session in ~/src/release-2024
# registered under a name that collides with a different repo's — and
# `send --to release-2` then reaches the wrong session.
_GENERATED_SUFFIX_RE = re.compile(r"-([2-9]|[1-9]\d)$")


def suffixed_name_candidates(name: str, count: int = 3,
                             taken: "set[str] | None" = None) -> list[str]:
    """`<stem>-2 … `, trimmed to satisfy NAME_RE, skipping names already in use.

    Two failure modes this has to avoid, both of which drop a session off the
    bus for good:

    - **Over-long.** A bare f"{name}-{i}" overflows the 40-char cap for any
      name of 39-40 chars, and `auto_name_from_cwd` truncates to exactly 40.
      The client adopts the suggestion, the server rejects it as
      `invalid_name`, and the monitor stops. Hence the trim.
    - **Non-progressing.** Trimming alone made `A38-2` suggest `A38-2` — the
      caller's own name — so the client re-sent the same name until its retry
      budget ran out. Hence stripping any existing `-N` to a stable stem, and
      never returning `name` itself or anything in `taken`.

    `taken` should be the live registry: without it the third session in a repo
    is handed `foo-2`, which is already registered, and burns a retry per hop.
    """
    taken = set(taken or ())
    stem = _GENERATED_SUFFIX_RE.sub("", name) or name
    out: list[str] = []
    i = 2
    while len(out) < count and i < 100:
        suffix = f"-{i}"
        base = stem[: NAME_MAX_LEN - len(suffix)].rstrip("-")
        candidate = f"{base}{suffix}"
        if (base and candidate != name and candidate not in taken
                and candidate not in out and validate_name(candidate)):
            out.append(candidate)
        i += 1
    return out


def validate_name(s: str) -> bool:
    if not isinstance(s, str):
        return False
    return bool(NAME_RE.match(s))


def validate_label(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if s == "":
        return True
    nfc = unicodedata.normalize("NFC", s)
    if len(nfc) > LABEL_MAX_CP:
        return False
    for ch in nfc:
        if ch == " ":
            continue
        if ch in LABEL_FORBIDDEN_CHARS:
            return False
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat[0] == "Z":
            return False
    return True


def normalize_label(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sanitize_for_stdout(s: str) -> str:
    s = ANSI_RE.sub("", s)
    out = []
    for ch in s:
        if ch == "\t":
            out.append(ch)
        elif ch == "\n" or ch == "\r":
            out.append("↵")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            out.append(ch)
    return "".join(out)


# Render-time neutralization of the header-structural characters
# (LABEL_FORBIDDEN_CHARS) to safe look-alikes, preserving readability. Labels
# that passed validate_label already exclude these (the primary defense), so
# this is belt-and-suspenders for any label that bypassed validation — e.g. a
# direct _format_msg caller, or a label persisted by an older client version
# before this reject existed.
_LABEL_STRUCTURAL = {'"': "'", "[": "(", "]": ")"}


def sanitize_label_for_display(s: str) -> str:
    """Make a peer-supplied label safe to interpolate into the single-line
    stdout notification (and the `list` table). Strips control/ANSI like
    `sanitize_for_stdout`, folds tabs to spaces (sanitize_for_stdout keeps
    them, but a tab would disrupt the `list` table's fixed-width columns), then
    neutralizes the header-structural characters so the label cannot break out
    of its field and forge a header."""
    s = sanitize_for_stdout(s).replace("\t", " ")
    return "".join(_LABEL_STRUCTURAL.get(ch, ch) for ch in s)


def truncate_for_stdout(s: str, cap: int = STDOUT_CAP) -> tuple[str, bool, int]:
    full_len = len(s)
    if full_len <= cap:
        return s, False, full_len
    return s[:cap], True, full_len


def ensure_token(path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Re-tighten perms in case the file was created with looser permissions
        # (older version, accidental chmod, restore from backup, etc.). Also
        # refuse to follow symlinks since that's a leak vector.
        try:
            st = os.lstat(str(path))
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
                raise RuntimeError(f"token path {path} is not a regular file")
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        return _read_token_with_retry(path)
    try:
        token = secrets.token_urlsafe(32)
        os.write(fd, token.encode("utf-8"))
        os.fsync(fd)
        return token
    finally:
        os.close(fd)


def _read_token_with_retry(path: Path, attempts: int = 50, delay: float = 0.005) -> str:
    """Read a token file that may currently be empty because another process
    just created it (O_CREAT|O_EXCL) and hasn't yet written the body.
    """
    import time
    for _ in range(attempts):
        try:
            content = path.read_text().strip()
            if content:
                return content
        except OSError:
            pass
        time.sleep(delay)
    raise RuntimeError(f"token file at {path} is empty after {attempts} retries")


MESSAGES_LOG_MAX_BYTES = 50 * 1024 * 1024
MESSAGES_LOG_BACKUPS = 5


def rotate_log_if_needed(path: Path, max_bytes: int, backups: int) -> None:
    """Size-based rotation. Single-writer assumption (server-only writer).
    Standard shift: drop the oldest, then path.<i-1> → path.<i> for i in
    [backups..2], then path → path.1.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    for i in range(backups, 0, -1):
        nxt = path.parent / f"{path.name}.{i}"
        prev = path if i == 1 else path.parent / f"{path.name}.{i - 1}"
        if i == backups:
            try:
                nxt.unlink()
            except OSError:
                pass
        try:
            prev.rename(nxt)
        except OSError:
            pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically write `text` to `path` with `mode` perms: write a sibling
    tempfile (fchmod + fsync), then os.replace so readers never observe a
    partial file. The parent directory must already exist (callers create it
    with the right perms via secure_dir)."""
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_pid_alive(pid: int) -> bool:
    """True if a process with `pid` is alive (or we can signal 0 to it).

    Every caller feeds this a number read off disk — a `clients/*.session`
    file or a server pidfile — so it has to survive whatever is in there.
    `os.kill` raises OverflowError, an ArithmeticError rather than an OSError,
    for a pid too large for a C int, which no `except OSError` catches: a
    hand-edited or foreign state file would take down `list.py --self`, the
    command the disconnect flow depends on.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except (OSError, OverflowError):
        return False


def write_server_identity(pid: int, host: str, port: int) -> None:
    """Write endpoint-scoped server identity files before the listener starts
    accepting clients. Default-host files keep the historical
    `server.<port>.pid` name; non-default hosts include host+port so multiple
    endpoints in the same data_dir don't clobber each other's identity."""
    pid_path = pidfile_path(port, host)
    meta_path = pidfile_meta_path(port, host)
    pid_path.write_text(str(pid))
    secure_file(pid_path)
    meta_path.write_text(json.dumps({
        "pid": pid,
        "host": host,
        "port": port,
    }))
    secure_file(meta_path)


def _pidfile_meta_matches(
    pid: int, host: str | None, port: int | None, meta_path: Path,
) -> bool:
    if host is None and port is None:
        return True
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    if meta.get("pid") != pid:
        return False
    if port is not None and meta.get("port") != port:
        return False
    if host is not None and meta.get("host") != host:
        return False
    return True


def listener_lock_held(lock_path: Path) -> bool:
    """True iff a live monitor holds this session's listener flock.

    Used with — not instead of — a liveness check on the pid in the state
    file. The flock proves *a* monitor holds this session's lock; it does not
    prove the state file was written by that monitor, because a respawned
    monitor takes the lock in run() and only writes state after the server is
    up and `hello` is answered. Requiring both keeps `list.py --self` from
    printing a dead monitor's pid, which the disconnect flow hands to `kill`.

    Read-only: no O_CREAT, so probing a session that never had a monitor does
    not litter `clients/` with lock files.
    """
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        return False  # never had a monitor
    except OSError:
        return True   # cannot tell; "held" is the non-destructive answer
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False  # we took it, so nobody was holding it
    except OSError:
        return True
    finally:
        # Closing releases the lock if we took it — this is only a probe.
        os.close(fd)


def _cmdline_port_matches(cmdline: list[str], port: int | None) -> bool:
    if port is None:
        return True
    for i, arg in enumerate(cmdline):
        if arg == "--port" and i + 1 < len(cmdline):
            try:
                return int(cmdline[i + 1]) == port
            except ValueError:
                return False
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1]) == port
            except ValueError:
                return False
    # Older/manual default-port servers may not have an explicit --port arg.
    return port == DEFAULT_PORT


def _cmdline_host_matches(cmdline: list[str], host: str | None) -> bool:
    """Symmetric to _cmdline_port_matches for --host.
    A missing --host arg implies the server's default (127.0.0.1)."""
    if host is None:
        return True
    for i, arg in enumerate(cmdline):
        if arg == "--host" and i + 1 < len(cmdline):
            return cmdline[i + 1] == host
        if arg.startswith("--host="):
            return arg.split("=", 1)[1] == host
    return host == "127.0.0.1"


def _identity_lookup_paths(
    host: str | None, port: int | None,
) -> tuple[Path, Path, bool]:
    lookup_port = port if port is not None else DEFAULT_PORT
    if host is None:
        return pidfile_path(lookup_port), pidfile_meta_path(lookup_port), False
    pid_path = pidfile_path(lookup_port, host)
    meta_path = pidfile_meta_path(lookup_port, host)
    if pid_path.exists() or meta_path.exists():
        return pid_path, meta_path, False
    # Upgrade path: previous versions wrote only server.<port>.pid(.meta),
    # even for custom --host. Keep accepting those if metadata/cmdline prove
    # the exact endpoint.
    return pidfile_path(lookup_port), pidfile_meta_path(lookup_port), True


def verify_server_identity(host: str | None = None, port: int | None = None) -> bool:
    """Defense-in-depth against port squatting: confirm the listener on our
    port is actually our server.py (not a hostile local process that bound
    9473 first to harvest tokens from connecting clients).

    If host/port are supplied, the pidfile metadata must match the exact
    listener endpoint the caller is about to dial. Otherwise a legitimate
    server on one port could make a squatter on another port look trusted.

    Fails closed:
      - missing pidfile        → False (squatter scenario)
      - unreadable / bad pid   → False
      - dead pid               → False
      - missing/mismatched endpoint metadata when host/port supplied → False
      - cmdline doesn't include bin/server.py → False
      - cmdline includes bin/server.py        → True

    Soft-trust fallback: if psutil isn't available, trust an alive pidfile
    pid (the protection degrades but doesn't break setups without psutil).

    Server-side ordering note: server.py writes the pidfile BEFORE
    `await websockets.serve()`, so by the time a client's WS upgrade
    completes, the pidfile exists. No race window for legit clients.

    Threat model caveat: a same-UID attacker can spoof the pidfile, since
    it lives in the user's data dir. This isn't a crypto guarantee — it
    closes the opportunistic squatting vector but assumes the user trusts
    same-UID code (per README threat model).
    """
    pid_path, meta_path, using_legacy_path = _identity_lookup_paths(host, port)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return False
    if not safe_pid_alive(pid):
        return False
    endpoint_check_needed = host is not None or port is not None
    meta_exists = meta_path.exists()
    meta_verified = False
    if endpoint_check_needed and meta_exists:
        if not _pidfile_meta_matches(pid, host, port, meta_path):
            return False
        meta_verified = True
    try:
        import psutil
    except ImportError:
        return not endpoint_check_needed or meta_verified
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return not endpoint_check_needed or meta_verified
    cmd = " ".join(cmdline)
    if "bin/server.py" not in cmd:
        return False
    if endpoint_check_needed and (not meta_exists or using_legacy_path):
        # Cmdline-only path: must prove BOTH port AND host. Otherwise an
        # old default-host pidfile could trust a different requested host.
        return (_cmdline_port_matches(cmdline, port)
                and _cmdline_host_matches(cmdline, host))
    return True


def auto_name_from_cwd(cwd: str = "") -> str:
    """Best-effort name derivation from the current working directory's basename.

    Returns a name matching `validate_name`, or '' if no valid name can be
    derived. Used in plugin mode where the user/agent didn't supply --name.
    """
    base = os.path.basename(cwd or os.getcwd()).lower()
    cleaned: list[str] = []
    for ch in base:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            cleaned.append(ch)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    name = "".join(cleaned).strip("-")[:40]
    if not name or not validate_name(name):
        return ""
    return name


def secure_dir(path: Path) -> bool:
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(path), 0o700)
        return True
    except OSError:
        return False


def secure_file(path: Path) -> bool:
    path = Path(path)
    try:
        os.chmod(str(path), 0o600)
        return True
    except OSError:
        return False


def find_cc_ancestor_pid() -> int:
    """Walk up the process tree, return the pid of the first Claude Code
    ancestor. Returns -1 if none found.

    Why: In real CC, the monitor (`client.py`) and helper CLIs (`send.py` etc.)
    are spawned by *different* Bash subshells. They are siblings, not
    ancestor-descendant. `os.getppid()` of one cannot reach the other. But
    both share the CC main process as a common ancestor — keying state files
    by that pid makes them mutually discoverable.

    Detection: match on `cmdline[0]` basename, NOT `psutil.Process.name()`.
    Modern CC sets its proctitle to its version string (e.g., "2.1.119"),
    which makes `p.name()` useless. The actual binary basename in
    `cmdline[0]` is reliably `claude` (or `node` for older bundles).
    """
    try:
        import psutil
    except ImportError:
        return -1

    pid = os.getppid()
    seen: set[int] = set()
    while pid and pid not in seen and pid != 1:
        seen.add(pid)
        try:
            p = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return -1
        try:
            cmd = p.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmd = []
        if cmd:
            exe = os.path.basename(cmd[0]).lower()
            if exe == "claude" or exe.startswith("claude-"):
                return pid
            # Background sessions launch the Claude binary by its versioned
            # path (e.g. ~/.local/share/claude/versions/2.1.146), whose
            # basename is a version number, not "claude". Without recognizing
            # it, the walk skips the per-session process and resolves to the
            # shared supervisor (claude daemon run), so distinct background
            # sessions collide on one ppid lock and client/helper self-detection
            # mismatches. Match the versioned path so each bg session keys on
            # its own process; intra-session dedup is preserved.
            full = cmd[0].lower()
            if "/claude/versions/" in full or "/.local/share/claude/" in full:
                return pid
            if exe in ("node", "node.exe"):
                joined = " ".join(cmd[1:]).lower()
                if (
                    "/claude" in joined
                    or "claude-code" in joined
                    or "/.claude/" in joined
                ):
                    return pid
        try:
            pid = p.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return -1
    return -1


def resolve_listener_key() -> int:
    """Pid used as the state-file key for the inter-session listener of the
    current Claude Code session. Both `client.py` (writer) and helpers
    (`send.py`/`list.py` readers) must compute this the same way.

    Resolution order:
      1. `HUBBUB_PPID_OVERRIDE` / `INTER_SESSION_PPID_OVERRIDE` (test/debug)
      2. The CC main process pid (production: a stable common ancestor)
      3. `os.getppid()` (fallback for non-CC environments)
    """
    override = env("PPID_OVERRIDE")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    cc = find_cc_ancestor_pid()
    if cc > 0:
        return cc
    return os.getppid()
