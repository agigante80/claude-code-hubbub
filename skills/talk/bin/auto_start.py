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


def _resolve_monitors_path() -> Path:
    # monitors.json lives at <plugin-root>/monitors/monitors.json.
    # This script lives at <plugin-root>/skills/talk/bin/auto_start.py,
    # so the plugin root is FOUR parents up from this file.
    #
    # Resolution order:
    #   1. CLAUDE_PLUGIN_ROOT env var (override; rarely set in subprocesses
    #      because ${CLAUDE_PLUGIN_ROOT} in CC manifests is a substitution
    #      token, not an exported env var).
    #   2. Script-relative: walk up four parents.
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parents[3])

    for root in candidates:
        p = root / "monitors" / "monitors.json"
        if p.is_file():
            return p

    sys.stderr.write(
        "auto_start: could not locate monitors.json. Searched: "
        f"{[str(c / 'monitors' / 'monitors.json') for c in candidates]}\n"
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
    if when == ALWAYS:
        label = "ON  (auto-start at every session)"
    elif when == LAZY:
        label = "OFF (no auto-start; /hubbub:talk connect still works)"
    else:
        label = f"CUSTOM ({when})"
    optout = shared.autostart_optout_path()
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
    if _userconfig_says_off():
        # Third source of truth, and the only one this command cannot change.
        # Reporting it matters more than it looks: without this line, a user
        # who chose "no" at install and later runs `auto-start on` gets a
        # success message and a monitor that keeps exiting, with nothing
        # anywhere connecting the two.
        print("  plugin config: auto_start is set to false")
        if not optout.exists():
            print("  note: the monitor exits at session open because of that "
                  "setting, whatever `when` says. This command cannot change "
                  "it — use /plugin to edit the hubbub config.")
    return 0


def _userconfig_says_off() -> bool:
    """Is the `auto_start` userConfig explicitly false in this process's env?

    Only meaningful when CC injected it, which it does for the monitor and for
    skill-invoked commands in an installed plugin. Absent in `--plugin-dir`
    local-dev mode, where CC does not prompt for userConfig at all — so absence
    must read as "no opinion", never as "off".
    """
    v = os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_START")
    return v is not None and v.strip().lower() in {"0", "false", "no", "off"}


def cmd_set(target: str) -> int:
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
        # On needs all three: a lingering flag would exit the monitor CC just
        # spawned, and so would a userConfig `auto_start: false` — which this
        # command has no way to change, so claiming success would be a lie.
        effective = ((not optout_present) and when_now == ALWAYS
                     and not _userconfig_says_off())

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
        if target == ALWAYS and _userconfig_says_off():
            print("  reason: the plugin's auto_start config is false, which "
                  "this command cannot change. Edit it with /plugin — the "
                  "manifest and the saved setting are already correct.")
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
    args = parser.parse_args(argv)
    shared.migrate_legacy_data_dir()

    if args.status:
        return cmd_status()
    if args.on:
        return cmd_set(ALWAYS)
    if args.off:
        return cmd_set(LAZY)
    return 2  # unreachable due to required=True


if __name__ == "__main__":
    raise SystemExit(main())
