"""Tests for bin/auto_start.py — the /hubbub:talk auto-start helper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "talk" / "bin" / "auto_start.py"
ALWAYS = "always"
LAZY = "on-skill-invoke:talk"


@pytest.fixture
def fake_plugin_root(tmp_path: Path) -> Path:
    monitors_dir = tmp_path / "monitors"
    monitors_dir.mkdir()
    (monitors_dir / "monitors.json").write_text(json.dumps([
        {
            "name": "hubbub-client",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/skills/talk/bin/client.py",
            "description": "hubbub messages",
            "when": LAZY,
        }
    ], indent=2) + "\n")
    return tmp_path


def _run(args: list[str], plugin_root: Path | None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin"}
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


class TestStatus:
    def test_lazy_default(self, fake_plugin_root: Path):
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 0
        assert "OFF" in r.stdout
        assert LAZY in r.stdout

    def test_after_set_on(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "ON" in r.stdout

    def test_custom_value(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = "on-skill-invoke:other-skill"
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        assert "CUSTOM" in r.stdout


class TestSet:
    def test_on_writes_always(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == ALWAYS

    def test_off_writes_lazy(self, fake_plugin_root: Path):
        # First flip to ON so we can verify OFF is a real change
        _run(["--on"], fake_plugin_root)
        r = _run(["--off"], fake_plugin_root)
        assert r.returncode == 0
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_no_change_when_already_target(self, fake_plugin_root: Path):
        r = _run(["--off"], fake_plugin_root)
        assert r.returncode == 0
        assert "no change" in r.stdout
        # File contents preserved
        data = json.loads((fake_plugin_root / "monitors" / "monitors.json").read_text())
        assert data[0]["when"] == LAZY

    def test_preserves_other_monitor_fields(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        before = json.loads(m.read_text())[0]
        _run(["--on"], fake_plugin_root)
        after = json.loads(m.read_text())[0]
        for k in ("name", "command", "description"):
            assert before[k] == after[k]

    def test_reload_instruction_printed(self, fake_plugin_root: Path):
        r = _run(["--on"], fake_plugin_root)
        assert "/reload-plugins" in r.stdout or "new Claude Code session" in r.stdout


class TestErrors:
    def test_no_env_falls_back_to_script_relative(self):
        """Without CLAUDE_PLUGIN_ROOT, the script self-locates from
        __file__ (bin/auto_start.py → repo root → monitors/monitors.json).
        It should succeed against the real repo."""
        r = _run(["--status"], plugin_root=None)
        assert r.returncode == 0
        assert "auto-start:" in r.stdout

    def test_env_root_takes_precedence(self, fake_plugin_root: Path):
        """When CLAUDE_PLUGIN_ROOT IS set, prefer it over script-relative."""
        m = fake_plugin_root / "monitors" / "monitors.json"
        data = json.loads(m.read_text())
        data[0]["when"] = ALWAYS  # set the fake to ALWAYS
        m.write_text(json.dumps(data) + "\n")
        r = _run(["--status"], fake_plugin_root)
        # Should reflect the fake's value, not the real repo's.
        assert "ON" in r.stdout

    def test_missing_monitor_entry(self, fake_plugin_root: Path):
        m = fake_plugin_root / "monitors" / "monitors.json"
        m.write_text(json.dumps([{"name": "other-monitor", "when": "always"}]))
        r = _run(["--status"], fake_plugin_root)
        assert r.returncode == 2
        assert "hubbub-client" in r.stderr

    def test_requires_one_of(self, fake_plugin_root: Path):
        # No flags → argparse should fail (mutually exclusive group, required=True)
        r = _run([], fake_plugin_root)
        assert r.returncode != 0
