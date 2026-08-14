"""Toggle the hubbub plugin monitor's auto-start behavior.

Edits the `when` field of the hubbub-client monitor in the
plugin's `monitors/monitors.json` atomically. The script self-locates
relative to its own path (no env var needed); CLAUDE_PLUGIN_ROOT is
honored as an override if set.

Modes:
  always                          start at every CC session open
  on-skill-invoke:talk           start when /hubbub:talk is first invoked

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
    return 0


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

    # From here the two halves are applied independently. Neither is allowed to
    # abort the other: the durable flag is what survives `/plugin update`, and
    # the manifest is what CC actually reads at session open. Ordering them
    # differently only chooses which one gets dropped when the other fails.
    optout_ok, optout_changed = _set_optout(target == LAZY)

    when_now, manifest_ok = prev, True
    if prev != target:
        entry["when"] = target
        try:
            _atomic_write(path, json.dumps(monitors, indent=2) + "\n")
            when_now = target
        except OSError as e:
            manifest_ok = False
            print(f"auto-start: could not update {path}: {e}", file=sys.stderr)

    if target == LAZY:
        # Either half alone is enough to be off: the flag makes client.py exit
        # at once even when CC still starts it.
        effective = optout_ok or when_now == LAZY
    else:
        # On needs both: a lingering flag would exit the monitor CC just spawned.
        effective = optout_ok and when_now == ALWAYS

    if when_now != prev:
        print(f"auto-start: {prev!r} -> {target!r}")
        print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
    elif optout_changed:
        # Not "no change": the durable flag moved even though `when` already
        # matched — the state after a `/plugin update` restored the shipped
        # value while leaving the data-dir flag behind.
        print(f"auto-start: {target!r}; saved setting updated")
        # This is the post-/plugin-update shape, and the monitor CC started
        # for the current session has already exited on the stale opt-out —
        # so a reload is needed here just as much as on a `when` change.
        print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
    elif manifest_ok and optout_ok:
        print(f"auto-start: already {target!r}; no change")
    elif effective:
        # One half failed while the other already matched. Name the half that
        # actually carries the setting — inferring it from manifest_ok alone
        # claimed "in effect via the saved setting" when the saved setting was
        # exactly what had failed to write.
        via = "the plugin manifest" if when_now == target else "the saved setting"
        print(f"auto-start: {target!r} in effect via {via}; "
              f"the other half could not be written (see stderr)")
    else:
        # Nothing landed. SKILL.md tells the agent to surface this output, so
        # silence plus exit 1 would read as an unexplained failure.
        print(f"auto-start: {target!r} NOT applied; neither the plugin "
              f"manifest nor the saved setting could be written (see stderr)")

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
