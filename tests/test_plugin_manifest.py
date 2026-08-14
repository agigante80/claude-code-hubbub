"""Static validation of the plugin manifest + monitors config."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "talk"
BIN_DIR = SKILL_DIR / "bin"


class TestPluginJson:
    def test_loads(self):
        cfg = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        assert cfg["name"] == "hubbub"
        assert cfg["monitors"] == "./monitors/monitors.json"

    def test_no_explicit_skills_key(self):
        """The current plugin schema rejects "skills": ["./"] with
        'Path escapes plugin directory'. Anthropic's official plugins omit
        the key entirely and rely on auto-discovery from skills/<name>/SKILL.md.
        """
        cfg = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        assert "skills" not in cfg, (
            'plugin.json must not declare "skills": the schema rejects '
            '["./"] and ["."]. Auto-discovery scans skills/<name>/SKILL.md.'
        )

    def test_version_matches_marketplace(self):
        """plugin.json drives installed-plugin update detection,
        marketplace.json drives the marketplace listing. A bump that lands
        in only one leaves installs silently stuck on the old version, so
        the two must always agree."""
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        entries = [p for p in market["plugins"] if p["name"] == plugin["name"]]
        assert len(entries) == 1, f"no single marketplace entry for {plugin['name']!r}"
        assert plugin["version"] == entries[0]["version"], (
            f"version drift: plugin.json {plugin['version']!r} != "
            f"marketplace.json {entries[0]['version']!r}"
        )

    def test_user_config_shape(self):
        cfg = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        uc = cfg["userConfig"]
        assert "port" in uc
        assert uc["port"]["type"] == "number"
        assert uc["port"]["default"] == 9473
        assert "idle_shutdown_minutes" in uc


class TestMonitorsJson:
    def test_loads(self):
        monitors = json.loads((REPO / "monitors" / "monitors.json").read_text())
        assert isinstance(monitors, list)
        assert len(monitors) == 1
        m = monitors[0]
        assert m["name"] == "hubbub-client"
        assert m["description"] == "hubbub messages"
        assert m["when"] in ("always", "on-skill-invoke:talk")
        assert "${CLAUDE_PLUGIN_ROOT}/skills/talk/bin/client.py" in m["command"]

    def test_default_when_is_always(self):
        """The default ships as 'always'. A bus you have to remember to join
        is a bus nobody is on when a peer wants to reach them: lazy start meant
        a session was only addressable after its own user invoked the skill,
        which is backwards for a system whose whole point is being driven from
        another session. Opt out with `/hubbub:talk auto-start off`.
        """
        m = json.loads((REPO / "monitors" / "monitors.json").read_text())[0]
        assert m["when"] == "always"

    def test_command_works_in_plugin_dir_mode(self):
        """`--plugin-dir` mode does NOT run the userConfig prompt, so the
        monitor command must not depend on `${user_config.*}` substitution.
        Defaults are read from env vars by client.py instead."""
        m = json.loads((REPO / "monitors" / "monitors.json").read_text())[0]
        assert "${user_config" not in m["command"], (
            "monitors.json must not use ${user_config.*}: it breaks --plugin-dir "
            "loading because CC doesn't prompt + substitute in that mode. "
            "Read CLAUDE_PLUGIN_OPTION_* env vars in client.py instead."
        )

    def test_command_uses_plugin_root(self):
        m = json.loads((REPO / "monitors" / "monitors.json").read_text())[0]
        assert "${CLAUDE_PLUGIN_ROOT}" in m["command"]

    def test_command_does_not_hardcode_userconfig_args(self):
        """Hardcoded `--port` / `--idle-shutdown-minutes` would override the
        argparse defaults in client.py, which read the configured values
        from CLAUDE_PLUGIN_OPTION_* env vars CC injects from userConfig.
        Hardcoding the defaults silently nullifies the user's plugin config.
        """
        m = json.loads((REPO / "monitors" / "monitors.json").read_text())[0]
        for forbidden in ("--port", "--idle-shutdown-minutes"):
            assert forbidden not in m["command"], (
                f"monitors.json must not pass {forbidden}; CC's userConfig "
                f"is delivered via CLAUDE_PLUGIN_OPTION_* env vars and a "
                f"hardcoded CLI arg would override it."
            )


class TestSkillMdLocation:
    def test_skill_md_in_skills_subdir(self):
        """Plugins use auto-discovery: skills/<name>/SKILL.md. Anything else
        breaks /reload-plugins with 'Path escapes plugin directory'."""
        assert (SKILL_DIR / "SKILL.md").is_file()

    def test_no_stray_skill_md_at_root(self):
        """If SKILL.md is at the repo root too, the duplication risks drift."""
        assert not (REPO / "SKILL.md").exists()

    def test_skill_md_has_frontmatter_name(self):
        first = (SKILL_DIR / "SKILL.md").read_text().splitlines()
        assert first[0] == "---"
        assert "name: talk" in "\n".join(first[:10])


class TestBinScriptsExist:
    """bin/ lives inside the skill directory so the skill is self-contained
    (users can copy/symlink skills/talk/ wherever and it works)."""

    def test_client_py(self):
        assert (BIN_DIR / "client.py").is_file()

    def test_server_py(self):
        assert (BIN_DIR / "server.py").is_file()

    def test_send_and_list(self):
        assert (BIN_DIR / "send.py").is_file()
        assert (BIN_DIR / "list.py").is_file()

    def test_auto_start_py(self):
        assert (BIN_DIR / "auto_start.py").is_file()

    def test_no_bin_at_repo_root(self):
        """bin/ moved into the skill dir; old top-level location is gone."""
        assert not (REPO / "bin").exists()


class TestRequirements:
    def test_runtime_deps_in_skill_dir(self):
        """Runtime deps live inside the skill dir so the skill stays
        self-contained (`<bin>/../requirements.txt` resolves to the
        right file from inside SKILL.md)."""
        req = (SKILL_DIR / "requirements.txt").read_text()
        assert "websockets" in req
        assert "psutil" in req

    def test_no_stray_requirements_at_root(self):
        assert not (REPO / "requirements.txt").exists()

    def test_dev_deps_inherit_runtime(self):
        dev = (REPO / "requirements-dev.txt").read_text()
        assert "-r skills/talk/requirements.txt" in dev
        assert "pytest" in dev


class TestDependencyGuard:
    """`import websockets` sits at module scope in every entry-point, so the
    missing-deps message has to be produced from a *caught* import error. A
    bare `import websockets` inside `if __name__ == "__main__"` is dead code —
    the module-level one already crashed with a traceback — and SKILL.md tells
    the agent to react to a string that would then never be printed."""

    ENTRY_POINTS = ("client.py", "list.py", "send.py", "relabel.py")

    def test_module_level_import_is_guarded(self):
        for name in self.ENTRY_POINTS:
            src = (REPO / "skills" / "talk" / "bin" / name).read_text()
            assert "\nimport websockets\n" not in src, (
                f"{name}: unguarded module-level `import websockets` aborts "
                f"before main() and swallows the install-deps hint"
            )
            assert "_MISSING_DEP" in src, name

    def test_psutil_is_guarded_too(self):
        """requirements.txt lists psutil, and without it resolve_listener_key
        falls back to getppid() — which the monitor and the helper CLIs, being
        siblings under different subshells, compute differently. The ppid flock
        then stops deduping and self-discovery breaks, with no error anywhere.
        A half-installed runtime has to fail loudly instead."""
        for name in self.ENTRY_POINTS:
            src = (REPO / "skills" / "talk" / "bin" / name).read_text()
            head = src.split("_MISSING_DEP = _e")[0]
            assert "import psutil" in head, (
                f"{name}: psutil missing from the guarded import block"
            )

    def test_no_dead_reimport_in_main_block(self):
        for name in self.ENTRY_POINTS:
            src = (REPO / "skills" / "talk" / "bin" / name).read_text()
            _, _, tail = src.partition('if __name__ == "__main__":')
            assert "import websockets" not in tail, (
                f"{name}: re-importing websockets in the __main__ block can "
                f"never fail — the module-level import already decided it"
            )
