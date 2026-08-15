"""Toggle the hubbub plugin monitor's auto-start behavior.

Edits the `when` field of the hubbub-client monitor in the
plugin's `monitors/monitors.json` atomically. The script self-locates
relative to its own path (no env var needed); CLAUDE_PLUGIN_ROOT is
honored as an override if set.

Modes:
  always (--on)                   start at every CC session open
  on-skill-invoke:talk (--off)    no auto-start at all

--off also records a durable opt-out under the data directory, which
`/plugin update` cannot overwrite and which makes a plugin-started
monitor exit immediately — so it is not a "lazy start", it is off.
Connecting by hand is unaffected: `/hubbub:talk connect` starts its own
monitor and does not consult the opt-out.

Changes take effect on `/reload-plugins` or the next CC session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Allow running as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import shared

ALWAYS = "always"
LAZY = "on-skill-invoke:talk"
MONITOR_NAME = "hubbub-client"


def _is_our_plugin_root(root: Path) -> bool:
    """Does `root` carry hubbub's own plugin manifest?

    The marker only this plugin's root has. Applied to both the inferred root
    and an explicit `CLAUDE_PLUGIN_ROOT` — "honour it or fail" and "prove it is
    ours" are not in conflict.
    """
    try:
        # `errors="replace"`, not the ambient locale, is what keeps this total:
        # UnicodeDecodeError is a ValueError raised during *decode*, so neither
        # OSError nor JSONDecodeError below would catch it, and an undecodable
        # manifest would produce a traceback instead of the clean refusal this
        # function exists to give. Do not "simplify" this to read_text().
        raw = (root / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8", errors="replace")
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("name") == "hubbub"


def _git_worktree_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` containing a `.git` entry, else None.

    The discriminator #29 needed, and it is a property of the *target* rather
    than of the path used to invoke us — which is the whole point. The
    symlink guard below compares invocation path against resolved path, so it
    cannot fire when the caller passes an already-resolved path
    (`readlink -f`, `cd -P`, or a harness that prints realpaths). At that
    point the two invocations are byte-identical and no amount of cleverness
    about the path can tell them apart.

    What *can* be told apart is what we are about to write to. Measured on
    this machine: the installed plugin at
    `~/.claude/plugins/cache/hubbub/hubbub/0.2.0` — the real target — has no
    `.git`; a development checkout does. So "am I about to dirty someone's
    tracked working tree" is answerable regardless of how we got here, and
    dirtying a checkout was the actual harm in #16.
    """
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _resolve_monitors_path() -> Path:
    """Locate *this plugin's* monitors.json, or exit 2 without writing anything.

    monitors.json lives at `<plugin-root>/monitors/monitors.json`, and this
    script at `<plugin-root>/skills/talk/bin/auto_start.py`, so the root is
    four parents up. Two separate hazards, fixed separately (fork #16):

    **An explicit `CLAUDE_PLUGIN_ROOT` is authoritative — honour it or fail.**
    It used to be the first of two *candidates*, so pointing it at a path that
    does not exist fell through to the script-relative one. During the 0.2.0
    review that silently rewrote this repo's own `monitors/monitors.json` and
    the tree had to be restored with `git checkout`. Note a marker check would
    not have caught that: the repo *is* a valid hubbub plugin root. The only
    fix is to stop treating an explicit override as a suggestion.

    **An inferred root must prove it is ours.** Four parents up from a
    standalone-skill install (`~/.claude/skills/talk/bin/auto_start.py`) is
    `~/.claude`, so a `~/.claude/monitors/monitors.json` belonging to anything
    else would be loaded and edited. `_find_entry` would then exit 2 on a
    foreign file rather than corrupt it, but the search should not be reaching
    there at all.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        root = Path(env_root)
        p = root / "monitors" / "monitors.json"
        if p.is_file() and _is_our_plugin_root(root):
            return p
        if not p.is_file():
            sys.stderr.write(
                f"auto_start: CLAUDE_PLUGIN_ROOT is set to {env_root!r} but "
                f"{p} does not exist. Refusing to fall back to a "
                f"script-relative guess — an explicit root that is wrong is a "
                f"bug to surface, not to work around, and that fallback has "
                f"silently edited the wrong manifest before.\n"
            )
        else:
            # "Honour it or fail" and "prove it is ours" are not in conflict.
            # A developer with `export CLAUDE_PLUGIN_ROOT=~/dev/other-plugin`
            # in their profile should get a loud refusal, not have us load
            # someone else's manifest and rely on `_find_entry` to bail.
            sys.stderr.write(
                f"auto_start: CLAUDE_PLUGIN_ROOT is set to {env_root!r}, which "
                f"has a monitors.json but is not a hubbub plugin root (no "
                f".claude-plugin/plugin.json naming this plugin). Refusing to "
                f"edit another plugin's manifest.\n"
            )
        sys.exit(2)

    # `Path.resolve()` follows symlinks, and the documented standalone install
    # is a symlink: `ln -s <repo>/skills/talk ~/.claude/skills/talk`. Resolving
    # through it lands on the *repo*, which is a perfectly valid hubbub plugin
    # root — so the marker check passes and `--off` rewrites the developer's
    # tracked monitors.json, dirtying their git tree. Same harm as the env-var
    # case, reached through the symlink instead. Comparing the unresolved root
    # against the resolved one detects the crossing; when they differ we are
    # reaching outside the install we were invoked from, which is the thing to
    # refuse. (Both new copytree tests are real copies, so neither could have
    # caught this.)
    resolved_root = Path(__file__).resolve().parents[3]
    literal_root = Path(os.path.abspath(__file__)).parents[3]
    crossed_symlink = literal_root.resolve() != resolved_root

    # Symlinking a live checkout's skill dir *into* an installed plugin is a
    # reasonable way to develop against a real install. There the literal root
    # is the installed plugin, carrying the manifest that genuinely should be
    # edited — so prefer it rather than refusing and leaving auto-start
    # unconfigurable from that install (fork #29).
    if crossed_symlink and _is_our_plugin_root(literal_root):
        lp = literal_root / "monitors" / "monitors.json"
        if lp.is_file():
            return lp

    root = resolved_root
    p = root / "monitors" / "monitors.json"
    if p.is_file() and _is_our_plugin_root(root) and not crossed_symlink:
        return p

    if crossed_symlink:
        # Name the *symlink*, not `parents[3]`. The link is the skill dir —
        # `~/.claude/skills/talk` for the documented install — and reporting
        # `~/.claude` instead told users their home config directory was a
        # symlink, which it is not.
        skill_dir = Path(os.path.abspath(__file__)).parents[1]
        over_there = resolved_root / "monitors" / "monitors.json"
        detail = (f"Editing {over_there} would only dirty that checkout."
                  if over_there.is_file() else
                  f"There is no plugin manifest at {resolved_root} either.")
        sys.stderr.write(
            f"auto_start: {skill_dir} is a symlink into {resolved_root}. "
            f"auto-start is a plugin feature and a symlinked skill install "
            f"governs no plugin monitor. {detail} Nothing to configure — the "
            f"skill works as normal and `/hubbub:talk connect` starts its own "
            f"monitor.\n"
        )
    elif p.is_file():
        sys.stderr.write(
            f"auto_start: found {p} but {root} is not a hubbub plugin root "
            f"(no .claude-plugin/plugin.json naming this plugin). Refusing to "
            f"edit someone else's manifest.\n"
        )
    else:
        sys.stderr.write(
            "auto_start: no hubbub plugin manifest here. auto-start is a "
            "plugin feature — it toggles the `when` field of a monitor Claude "
            "Code starts — and this looks like a standalone skill install, "
            "which has no plugin monitor to govern. There is nothing to "
            "configure; the skill works as normal and `/hubbub:talk connect` "
            "starts its own monitor.\n"
        )
    sys.exit(2)


def _load(path: Path) -> list:
    monitors = json.loads(path.read_text())
    if not isinstance(monitors, list):
        sys.stderr.write("auto_start: monitors.json must be a JSON list\n")
        sys.exit(2)
    return monitors


def _find_entry(monitors: list) -> dict:
    for m in monitors:
        if isinstance(m, dict) and m.get("name") == MONITOR_NAME:
            return m
    sys.stderr.write(
        f"auto_start: no monitor named {MONITOR_NAME!r} found in monitors.json\n"
    )
    sys.exit(2)


def _atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".monitors.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_optout(off: bool) -> tuple[bool, bool]:
    """Mirror the setting into the data dir, which `/plugin update` cannot
    overwrite. See shared.autostart_optout_path for why the plugin file alone
    is not enough.

    Returns (ok, changed). Never raises: this is one of two half-independent
    writes, and a failure here must not stop the other one from being applied.
    """
    path = shared.autostart_optout_path()
    try:
        existed = path.exists()
        if off:
            # secure_dir, not a bare mkdir: on a fresh machine whose first
            # hubbub command is `auto-start off` this creates the data dir,
            # and the bearer token gets minted inside it later.
            if not shared.secure_dir(path.parent):
                raise OSError(f"could not create {path.parent}")
            path.touch()
        else:
            path.unlink(missing_ok=True)
        return True, existed != off
    except OSError as e:
        print(f"auto-start: could not update {path}: {e}", file=sys.stderr)
        return False, False


def cmd_status() -> int:
    path = _resolve_monitors_path()
    entry = _find_entry(_load(path))
    when = entry.get("when", "always")
    optout = shared.autostart_optout_path()
    # The headline is what gets summarized — SKILL.md has the agent surface
    # this output, and a reader stops at the first line. So it must reflect
    # the *effective* state, not just `when`: printing "ON" and contradicting
    # it three lines down means the contradiction is the part that gets
    # dropped.
    forced_off = optout.exists() or _env_says_off()
    if forced_off:
        label = "OFF (forced; see below — `when` alone does not decide this)"
    elif when == ALWAYS:
        label = "ON  (auto-start at every session)"
    elif when == LAZY:
        label = "OFF (no auto-start; /hubbub:talk connect still works)"
    else:
        label = f"CUSTOM ({when})"
    print(f"auto-start: {label}")
    print(f"  when: {when}")
    print(f"  file: {path}")
    if optout.exists():
        print(f"  opt-out: {optout}")
        if when == ALWAYS:
            # A plugin update restored the shipped `always`; the durable
            # opt-out is what is actually in force.
            print("  note: monitors.json says always, but the opt-out above "
                  "wins — the monitor exits immediately at session open.")
    if _env_says_off():
        # A second source of truth this command cannot write. Reporting it
        # matters more than it looks: without this line, a user with the env
        # var exported gets a success message from `auto-start on` and a
        # monitor that keeps exiting, with nothing connecting the two.
        print(f"  env: {_env_says_off()} is set to a false value")
        if not optout.exists():
            print("  note: the monitor exits at session open because of that "
                  "variable, whatever `when` says. This command cannot change "
                  "it — unset it in the shell that starts Claude Code.")
    return 0


_FALSEY = {"0", "false", "no", "off"}
# Must stay the same key list `client.py::_autostart_wanted` consults, or this
# command reports "on" for a monitor that is exiting. Deliberately NOT
# including CLAUDE_PLUGIN_OPTION_AUTO_START: CC injects those for hooks only,
# never for monitors, so a userConfig switch would be inert (fork #22, #28).
_AUTOSTART_ENV_KEYS = ("HUBBUB_AUTO_START", "INTER_SESSION_AUTO_START")


_TRUTHY = {"1", "true", "yes", "on"}


def _env_says_off() -> str | None:
    """Name of the env key that *decides* auto-start off, else None.

    Must mirror `client.py::_env_bool`'s resolution, not just its key list.
    The rule is **first parseable value wins**, so a truthy `HUBBUB_AUTO_START`
    shadows a falsey legacy `INTER_SESSION_AUTO_START` — scanning for the first
    *falsey* key instead would disagree with the monitor.

    Concretely, that disagreement stranded upgraders: someone with
    `INTER_SESSION_AUTO_START=false` left in their profile who follows the
    current docs and exports `HUBBUB_AUTO_START=true` gets a monitor that
    stays up, while this command reported `OFF (forced)` and refused
    `auto-start on` — leaving no in-product way to clear the durable opt-out.

    Returns the key rather than a bool so the message can name it; "unset it"
    is not actionable without saying which one.
    """
    for k in _AUTOSTART_ENV_KEYS:
        v = os.environ.get(k)
        if v is None:
            continue
        v = v.strip().lower()
        if v in _FALSEY:
            return k
        if v in _TRUTHY:
            return None  # this key decides, and it decides "on"
    return None


def cmd_set(target: str, force: bool = False) -> int:
    # Locate and validate the manifest BEFORE touching durable state. These
    # three exit(2) on a missing or malformed file, and a standalone-skill copy
    # legitimately has no monitors.json — it governs no plugin monitor, so it
    # must not reach over and flip the flag a *plugin* install reads. Doing the
    # flag first meant `auto-start on` run from such a copy deleted the opt-out
    # (re-enabling the plugin's always-on monitor) and then exited 2, telling
    # the user it had failed.
    path = _resolve_monitors_path()
    monitors = _load(path)
    entry = _find_entry(monitors)
    prev = entry.get("when", "always")

    # Same principle one step further out: refuse an `--on` that cannot take
    # effect BEFORE mutating anything. Detecting it afterwards and printing
    # "NOT applied" would be a lie in the worst direction — by then
    # `_set_optout(False)` has already deleted the durable opt-out, so a
    # command that reported doing nothing has in fact destroyed the user's
    # recorded choice, and a later change to the env would silently make the
    # machine always-on with nothing left saying they had opted out.
    # The guard that does not depend on how we were invoked (fork #29). The
    # symlink check in `_resolve_monitors_path` cannot fire when the caller
    # passes an already-resolved path, and at that point nothing about the
    # path distinguishes "ran the repo's copy on purpose" from "followed a
    # symlink into it". What is still knowable is what we are about to write
    # to — and the harm in #16 was dirtying a tracked working tree.
    #
    # Safe by construction for real installs: the plugin cache carries no
    # `.git`. A development checkout does, and so does the marketplace clone,
    # where an edit would be discarded by the next update anyway.
    #
    # `--status` is unaffected; this is only on the write path.
    worktree = _git_worktree_root(path)
    if worktree is not None and not force:
        print(f"auto-start: {target!r} NOT applied — {path} is inside the git "
              f"work tree at {worktree}, so writing it would leave you with a "
              f"modified tracked file. That is how this command once dirtied "
              f"the repo it was being developed in. Nothing was modified.")
        print(f"  If this really is the plugin you want to configure "
              f"(a --plugin-dir local-dev install), re-run with --force.")
        return 1

    blocking_key = _env_says_off() if target == ALWAYS else None
    if blocking_key:
        print(f"auto-start: {target!r} NOT applied — {blocking_key} is set to "
              f"a false value in this environment, which this command cannot "
              f"change. Nothing was modified; unset it in the shell that "
              f"starts Claude Code, then re-run.")
        return 1

    # The two halves are applied independently — neither may abort the other —
    # but the *order* is not symmetric, because only one of them destroys
    # information.
    #
    # `--off` writes the flag: additive, and the fail-safe direction, so doing
    # it first means a failed manifest write still leaves the session off.
    # `--on` *deletes* the flag. Doing that first and then failing the manifest
    # write reported `NOT applied` while having already thrown away the user's
    # durable opt-out — after which the next `/plugin update` restores the
    # shipped `always` and the session goes always-on with nothing recording
    # that they had turned it off. So for `--on` the manifest write goes first
    # and the flag is only cleared once it has actually landed.
    # Two different questions, and conflating them mis-reported the exact case
    # this ordering exists for: `optout_ok` is "the write we attempted
    # succeeded" (nothing attempted counts as fine), while `optout_present` is
    # "what is actually on disk". Deriving the first from the second told the
    # user the saved setting could not be written, and to check stderr, in a
    # run that never tried to write it and printed no such error.
    optout_ok, optout_changed = (True, False)
    if target == LAZY:
        optout_ok, optout_changed = _set_optout(True)

    when_now, manifest_ok = prev, True
    if prev != target:
        entry["when"] = target
        try:
            _atomic_write(path, json.dumps(monitors, indent=2) + "\n")
            when_now = target
        except OSError as e:
            manifest_ok = False
            print(f"auto-start: could not update {path}: {e}", file=sys.stderr)

    if target != LAZY and manifest_ok:
        optout_ok, optout_changed = _set_optout(False)
    # else: the manifest never took, so the opt-out is deliberately left as it
    # was — untouched, not failed.
    optout_present = shared.autostart_optout_path().exists()

    if target == LAZY:
        # Either half alone is enough to be off: the flag makes client.py exit
        # at once even when CC still starts it.
        effective = optout_present or when_now == LAZY
    else:
        # On needs both. The env-var case is already handled by the early
        # refusal above, which returns before reaching here.
        effective = (not optout_present) and when_now == ALWAYS

    failed = ([] if manifest_ok else ["the plugin manifest"]) + \
            ([] if optout_ok else ["the saved setting"])
    # Appended to whichever line below fires. A half that failed has to be
    # visible even when the other half succeeded and the headline reads like a
    # clean result — SKILL.md tells the agent to surface stdout, and stderr
    # alone has been missed.
    detail = f"; could not write {' and '.join(failed)} (see stderr)" if failed else ""

    if not effective:
        # Checked first: any of the branches below prints a success shape, and
        # two of them add "Reload to apply". Telling the user to reload for a
        # change that is not in force is worse than saying nothing, and
        # SKILL.md has the agent surface stdout.
        print(f"auto-start: {target!r} NOT applied{detail}")
    elif when_now != prev:
        print(f"auto-start: {prev!r} -> {target!r}{detail}")
        print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
    elif optout_changed:
        # Not "no change": the durable flag moved even though `when` already
        # matched — the state after a `/plugin update` restored the shipped
        # value while leaving the data-dir flag behind.
        print(f"auto-start: {target!r}; saved setting updated{detail}")
        # This is the post-/plugin-update shape, and the monitor CC started
        # for the current session has already exited on the stale opt-out —
        # so a reload is needed here just as much as on a `when` change.
        print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
    elif not failed:
        print(f"auto-start: already {target!r}; no change")
    else:
        # In force, but one half could not be written.
        via = "the plugin manifest" if when_now == target else "the saved setting"
        print(f"auto-start: {target!r} in effect via {via}{detail}")

    if not effective:
        print(f"auto-start: could not apply {target!r}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="print current setting")
    g.add_argument("--on", action="store_true", help=f'set when="{ALWAYS}"')
    g.add_argument("--off", action="store_true", help=f'set when="{LAZY}"')
    parser.add_argument(
        "--force", action="store_true",
        help="write the manifest even when it is a tracked file in a git "
             "work tree (a --plugin-dir local-dev install)")
    args = parser.parse_args(argv)
    shared.migrate_legacy_data_dir()

    if args.status:
        return cmd_status()
    if args.on:
        return cmd_set(ALWAYS, force=args.force)
    if args.off:
        return cmd_set(LAZY, force=args.force)
    return 2  # unreachable due to required=True


if __name__ == "__main__":
    raise SystemExit(main())
