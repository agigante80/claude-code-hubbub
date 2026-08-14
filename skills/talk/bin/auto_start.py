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


def _set_optout(off: bool) -> None:
    """Mirror the setting into the data dir, which `/plugin update` cannot
    overwrite. See shared.autostart_optout_path for why the plugin file alone
    is not enough."""
    path = shared.autostart_optout_path()
    if off:
        # secure_dir, not a bare mkdir: on a fresh machine whose first hubbub
        # command is `auto-start off` this creates the data dir, and the
        # bearer token gets minted inside it later.
        shared.secure_dir(path.parent)
        path.touch()
    else:
        path.unlink(missing_ok=True)


def cmd_status() -> int:
    path = _resolve_monitors_path()
    entry = _find_entry(_load(path))
    when = entry.get("when", "always")
    if when == ALWAYS:
        label = "ON  (auto-start at every session)"
    elif when == LAZY:
        label = "OFF (lazy: starts on first /hubbub:talk invocation)"
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
    # Durable flag first. _resolve_monitors_path() exits when the plugin file
    # is missing and _atomic_write fails on a read-only plugin dir — and a
    # plugin directory in flux is precisely the case this flag exists for, so
    # gating it behind that write would drop the setting exactly when it
    # matters most.
    _set_optout(target == LAZY)
    path = _resolve_monitors_path()
    monitors = _load(path)
    entry = _find_entry(monitors)
    prev = entry.get("when", "always")
    if prev == target:
        # The durable flag was already reasserted above, which matters: a
        # plugin update can restore the shipped `when` while leaving the
        # data-dir opt-out stale, or vice versa.
        print(f"auto-start: already {target!r}; no change")
        return 0
    entry["when"] = target
    _atomic_write(path, json.dumps(monitors, indent=2) + "\n")
    print(f"auto-start: {prev!r} -> {target!r}")
    print("Reload to apply: /reload-plugins (or open a new Claude Code session).")
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
