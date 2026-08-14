import fcntl
import json
import os
import stat
import time
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from bin import shared


class TestValidateName:
    def test_simple_ascii(self):
        assert shared.validate_name("auth-refactor")
        assert shared.validate_name("a")
        assert shared.validate_name("a1b2")
        assert shared.validate_name("payments-debug-v2")

    def test_max_length(self):
        assert shared.validate_name("a" * 40)
        assert not shared.validate_name("a" * 41)

    def test_must_start_alnum(self):
        assert not shared.validate_name("-foo")
        assert not shared.validate_name("_foo")

    def test_no_uppercase(self):
        assert not shared.validate_name("Auth-refactor")
        assert not shared.validate_name("AUTH")

    def test_no_underscore(self):
        # Strict: name regex is [a-z0-9][a-z0-9-]{0,39}, no underscore.
        assert not shared.validate_name("auth_refactor")

    def test_no_unicode(self):
        assert not shared.validate_name("重构")
        assert not shared.validate_name("réviser")

    def test_no_spaces_or_punct(self):
        assert not shared.validate_name("auth refactor")
        assert not shared.validate_name("auth.refactor")
        assert not shared.validate_name("auth/refactor")
        assert not shared.validate_name('auth"x')

    def test_empty_invalid_for_addressing(self):
        # Empty allowed at hello-time but never as an addressing target.
        assert not shared.validate_name("")


class TestValidateLabel:
    def test_ascii_label(self):
        assert shared.validate_label("auth-refactor")
        assert shared.validate_label("Auth Refactor v2")

    def test_unicode_label(self):
        assert shared.validate_label("重构-认证")
        assert shared.validate_label("réviser")
        assert shared.validate_label("Payments 🐛 v2")

    def test_max_length(self):
        assert shared.validate_label("a" * 60)
        assert not shared.validate_label("a" * 61)

    def test_nfc_normalization_within_length(self):
        # NFD form expands to more codepoints; we measure NFC length.
        nfd = "é" * 30  # 60 codepoints in NFD; NFC = "é" * 30 = 30 cp
        assert shared.validate_label(nfd)

    def test_rejects_bidi_controls(self):
        for ch in ("‮", "‭", "⁦", "⁧"):  # RLO/LRO/LRI/RLI
            assert not shared.validate_label(f"a{ch}b")

    def test_rejects_zwj_zwnj(self):
        assert not shared.validate_label("a‍b")  # ZWJ
        assert not shared.validate_label("a‌b")  # ZWNJ

    def test_rejects_nbsp_and_other_z_separators(self):
        assert not shared.validate_label("a b")  # NBSP
        assert not shared.validate_label("a b")  # line separator
        assert not shared.validate_label("a b")  # em space (Zs but not 0x20)
        # ASCII 0x20 (regular space) is allowed (see test_ascii_label).

    def test_rejects_control_chars(self):
        assert not shared.validate_label("a\nb")
        assert not shared.validate_label("a\x1bb")
        assert not shared.validate_label("a\tb")  # tab is Cc

    def test_rejects_notification_header_structural_chars(self):
        # SEC-001: a peer label containing the notification header's structural
        # characters could reconstruct/corrupt a `[inter-session … from="…"]`
        # header on any surface that reflects it (notification line, `list`
        # table, messages.log). Reject them at the boundary.
        for ch in ('"', "[", "]"):
            assert not shared.validate_label(f"a{ch}b")
        assert not shared.validate_label('] [inter-session from="ceo')

    def test_empty_label_allowed(self):
        # Empty label is a valid sentinel meaning "no label".
        assert shared.validate_label("")


class TestSanitizeForStdout:
    def test_passes_plain_text(self):
        assert shared.sanitize_for_stdout("hello world") == "hello world"

    def test_strips_ansi(self):
        assert shared.sanitize_for_stdout("\x1b[31mred\x1b[0m") == "red"

    def test_replaces_newlines(self):
        assert shared.sanitize_for_stdout("a\nb\rc") == "a↵b↵c"

    def test_strips_other_control(self):
        assert shared.sanitize_for_stdout("a\x07b\x08c") == "abc"

    def test_preserves_tab(self):
        assert shared.sanitize_for_stdout("a\tb") == "a\tb"

    def test_handles_unicode(self):
        assert shared.sanitize_for_stdout("重构") == "重构"


class TestTruncateForStdout:
    def test_under_cap_unchanged(self):
        assert shared.truncate_for_stdout("hello", cap=100) == ("hello", False, 5)

    def test_over_cap_truncates(self):
        s = "x" * 1000
        truncated, was_truncated, full_len = shared.truncate_for_stdout(s, cap=100)
        assert was_truncated
        assert full_len == 1000
        assert len(truncated) <= 100


class TestAtomicToken:
    def test_creates_token_if_missing(self, tmp_path):
        token_path = tmp_path / "token"
        t1 = shared.ensure_token(token_path)
        assert token_path.exists()
        assert len(t1) >= 32

    def test_returns_existing_token(self, tmp_path):
        token_path = tmp_path / "token"
        t1 = shared.ensure_token(token_path)
        t2 = shared.ensure_token(token_path)
        assert t1 == t2

    def test_token_file_mode(self, tmp_path):
        token_path = tmp_path / "token"
        shared.ensure_token(token_path)
        mode = stat.S_IMODE(os.stat(token_path).st_mode)
        assert mode == 0o600

    def test_concurrent_create(self, tmp_path):
        token_path = tmp_path / "token"
        results = []
        errors = []

        def worker():
            try:
                results.append(shared.ensure_token(token_path))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(set(results)) == 1

    def test_repairs_loose_perms_on_existing(self, tmp_path):
        """Round-6 fix: if the token file exists with looser perms, ensure_token
        re-tightens to 0600 instead of trusting the leaky state."""
        token_path = tmp_path / "token"
        token_path.write_text("preexisting-token-value")
        os.chmod(str(token_path), 0o644)
        result = shared.ensure_token(token_path)
        assert result == "preexisting-token-value"
        assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600

    def test_rejects_symlink(self, tmp_path):
        """A symlinked token path is a privilege-escalation vector
        (could redirect to /tmp/world-readable). Reject it."""
        target = tmp_path / "real-token"
        target.write_text("anything")
        link = tmp_path / "token"
        link.symlink_to(target)
        with pytest.raises(RuntimeError):
            shared.ensure_token(link)


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "f.json"
        shared.atomic_write_text(p, '{"a": 1}')
        assert p.read_text() == '{"a": 1}'

    def test_mode_0600_by_default(self, tmp_path):
        p = tmp_path / "f"
        shared.atomic_write_text(p, "x")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600

    def test_overwrites_atomically(self, tmp_path):
        p = tmp_path / "f"
        shared.atomic_write_text(p, "first")
        shared.atomic_write_text(p, "second")
        assert p.read_text() == "second"
        # No tempfiles left behind.
        assert [x.name for x in tmp_path.iterdir()] == ["f"]

    def test_custom_mode(self, tmp_path):
        p = tmp_path / "f"
        shared.atomic_write_text(p, "x", mode=0o644)
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o644


class TestSecureDir:
    def test_creates_with_0700(self, tmp_path):
        d = tmp_path / "data"
        ok = shared.secure_dir(d)
        assert ok
        assert d.is_dir()
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700

    def test_idempotent(self, tmp_path):
        d = tmp_path / "data"
        shared.secure_dir(d)
        ok = shared.secure_dir(d)
        assert ok

    def test_tightens_loose_perms(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir(mode=0o755)
        shared.secure_dir(d)
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


class TestSecureFile:
    def test_creates_with_0600(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("data")
        ok = shared.secure_file(f)
        assert ok
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


class TestRoleEnum:
    def test_values(self):
        assert shared.Role.AGENT.value == "agent"
        assert shared.Role.CONTROL.value == "control"

    def test_from_string(self):
        assert shared.Role("agent") == shared.Role.AGENT
        assert shared.Role("control") == shared.Role.CONTROL


class TestProtocolConstants:
    def test_caps_match_plan(self):
        assert shared.WS_FRAME_CAP == 16 * 1024 * 1024
        assert shared.TEXT_CAP == 10 * 1024 * 1024
        assert shared.BROADCAST_TEXT_CAP == 256 * 1024
        assert shared.STDOUT_CAP == 400
        assert shared.TEXT_CAP < shared.WS_FRAME_CAP

    def test_default_port(self):
        assert isinstance(shared.DEFAULT_PORT, int)
        assert 1024 < shared.DEFAULT_PORT < 65535

    def test_error_codes(self):
        # Stable error code strings the protocol uses
        codes = {
            shared.ErrorCode.INVALID_NAME,
            shared.ErrorCode.INVALID_LABEL,
            shared.ErrorCode.INVALID_PAYLOAD,
            shared.ErrorCode.NAME_TAKEN,
            shared.ErrorCode.UNKNOWN_PEER,
            shared.ErrorCode.AMBIGUOUS,
            shared.ErrorCode.TEXT_TOO_LONG,
            shared.ErrorCode.UNAUTHORIZED,
            shared.ErrorCode.RATE_LIMITED,
            shared.ErrorCode.HOP_LIMIT,
            shared.ErrorCode.UNKNOWN_OP,
        }
        assert all(isinstance(c, str) for c in codes)
        assert len(codes) == 11


class TestPaths:
    def test_data_dir_respects_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_path / "x"))
        # Reload the module path resolver
        path = shared.data_dir()
        assert path == tmp_path / "x"

    def test_data_dir_respects_new_env_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUBBUB_DATA_DIR", str(tmp_path / "new"))
        assert shared.data_dir() == tmp_path / "new"

    def test_new_env_name_wins_over_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUBBUB_DATA_DIR", str(tmp_path / "new"))
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_path / "old"))
        assert shared.data_dir() == tmp_path / "new"

    def test_default_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INTER_SESSION_DATA_DIR", raising=False)
        monkeypatch.delenv("HUBBUB_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        path = shared.data_dir()
        assert str(path).endswith(".claude/data/hubbub")

    def test_data_dir_has_no_filesystem_side_effects(self, tmp_path, monkeypatch):
        """`data_dir()` must stay a pure resolver. It used to run the legacy
        migration inline, which meant any test that resolved the default path
        without the `tmp_data_dir` fixture silently moved the developer's real
        ~/.claude/data/inter-session — it did, once, during 0.2.0."""
        monkeypatch.delenv("INTER_SESSION_DATA_DIR", raising=False)
        monkeypatch.delenv("HUBBUB_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        legacy = tmp_path / ".claude" / "data" / "inter-session"
        legacy.mkdir(parents=True)
        shared.data_dir()
        assert not legacy.is_symlink()
        assert not (tmp_path / ".claude" / "data" / "hubbub").exists()


class TestLegacyDataDirMigration:
    """`inter-session` → `hubbub` must move the directory *and* leave a symlink,
    so older builds still running on this machine keep resolving to the same
    token / election lock / pidfile instead of forking the namespace."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INTER_SESSION_DATA_DIR", raising=False)
        monkeypatch.delenv("HUBBUB_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        self.legacy = tmp_path / ".claude" / "data" / "inter-session"
        self.new = tmp_path / ".claude" / "data" / "hubbub"

    def test_migrates_and_leaves_symlink(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("secret")
        shared.migrate_legacy_data_dir()
        assert shared.data_dir() == self.new
        assert (self.new / "token").read_text() == "secret"
        assert self.legacy.is_symlink()
        # An older build reading the hardcoded legacy path sees the same file.
        assert (self.legacy / "token").read_text() == "secret"

    def test_running_client_keeps_its_flock(self):
        """A held flock survives the move: `os.rename` keeps the inode, so a
        pre-migration monitor and a post-migration one still contend."""
        self.legacy.mkdir(parents=True)
        lock = self.legacy / "9473.election.lock"
        lock.touch()
        held = open(lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            shared.migrate_legacy_data_dir()
            contender = open(self.new / "9473.election.lock", "w")
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                contender.close()
        finally:
            held.close()

    def test_noop_when_already_migrated(self):
        self.new.mkdir(parents=True)
        (self.new / "token").write_text("current")
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("stale")
        shared.migrate_legacy_data_dir()
        assert shared.data_dir() == self.new
        assert (self.new / "token").read_text() == "current"
        # Legacy left exactly as found — never clobbered, never deleted.
        assert not self.legacy.is_symlink()
        assert (self.legacy / "token").read_text() == "stale"

    def test_noop_on_fresh_install(self):
        shared.migrate_legacy_data_dir()
        assert shared.data_dir() == self.new
        assert not self.legacy.exists()

    def test_second_call_does_not_re_migrate(self):
        self.legacy.mkdir(parents=True)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        # Symlink present: a re-run must not try to rename it onto itself.
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert self.legacy.resolve() == self.new

    def test_migrates_when_install_deps_created_the_new_dir_first(self):
        """`install-deps` is a standalone command, so <new>/venv can exist
        before any monitor has migrated anything. A "new exists → bail" guard
        would strand the token in the legacy dir forever."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "clients").mkdir()
        (self.new / "venv" / "bin").mkdir(parents=True)
        shared.migrate_legacy_data_dir()
        assert (self.new / "token").read_text() == "live"
        assert (self.new / "clients").is_dir()
        assert (self.new / "venv" / "bin").is_dir()
        assert self.legacy.is_symlink()

    def test_repairs_symlink_lost_after_a_partial_migration(self):
        """rename() succeeded, symlink_to() didn't. Left alone, an older build
        recreates the legacy path as a real dir with its own token."""
        self.new.mkdir(parents=True)
        (self.new / shared.MIGRATION_MARKER).touch()
        (self.new / "token").write_text("live")
        assert not self.legacy.exists()
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.legacy / "token").read_text() == "live"

    def test_fresh_install_gets_no_legacy_symlink(self):
        """No marker means this machine never had an inter-session dir; don't
        conjure one up just to have something to point at."""
        self.new.mkdir(parents=True)
        shared.migrate_legacy_data_dir()
        assert not self.legacy.exists()

    def test_colliding_venv_does_not_block_the_migration(self, capsys):
        """The regression that mattered: EVERY pre-rename install that ever ran
        install-deps has inter-session/venv, and install-deps on the new build
        creates hubbub/venv. Treating that pair as a conflict refused the
        migration on exactly the machines it exists for."""
        (self.legacy / "venv" / "bin").mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "clients").mkdir()
        (self.new / "venv" / "bin").mkdir(parents=True)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"
        assert (self.new / "clients").is_dir()
        # The new venv is the one the bootstrap will find; the stale one is
        # set aside rather than deleted.
        assert (self.new / "venv" / "bin").is_dir()
        aside = self.legacy.with_name("inter-session.pre-rename")
        assert (aside / "venv").is_dir()
        assert "safe to delete" in capsys.readouterr().err

    def test_symlink_still_lands_when_legacy_is_repopulated_mid_move(self, monkeypatch):
        """An old build still running can recreate clients/ between the scan
        and the rmdir. Leaving `legacy` a real directory is the one outcome
        that forks the namespace, so the remainder gets set aside instead."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        self.new.mkdir(parents=True)
        real_rename = os.rename
        state = {"done": False}

        def racing_rename(src, dst):
            real_rename(src, dst)
            if not state["done"]:
                state["done"] = True
                (self.legacy / "clients").mkdir()  # the old monitor comes back

        monkeypatch.setattr(shared.os, "rename", racing_rename)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"

    def test_backfills_marker_for_installs_migrated_before_it_existed(self):
        """0.2.0's first cut symlinked without writing a marker. Those installs
        would return here forever, so a later lost symlink could never be
        repaired."""
        self.new.mkdir(parents=True)
        self.legacy.symlink_to(self.new)
        assert not (self.new / shared.MIGRATION_MARKER).exists()
        shared.migrate_legacy_data_dir()
        assert (self.new / shared.MIGRATION_MARKER).is_file()

    def test_foreign_symlink_is_left_alone(self, tmp_path, capsys):
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        self.legacy.symlink_to(elsewhere)
        shared.migrate_legacy_data_dir()
        assert self.legacy.resolve() == elsewhere.resolve()
        assert not (self.new / shared.MIGRATION_MARKER).exists()
        assert "leaving it alone" in capsys.readouterr().err

    def test_losing_the_race_is_silent(self, monkeypatch, capsys):
        """Concurrent starts are normal (a monitor and a list.py). A lost race
        is judged by the end state, not by the errno we happened to catch: the
        winner leaves a complete migration behind, so there is nothing to
        report even though our own syscall failed."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")

        def peer_wins(*a, **k):
            # The winner completes while we are mid-syscall.
            if not self.new.exists():
                self.new.mkdir(parents=True)
                (self.new / shared.MIGRATION_MARKER).touch()
                for e in list(self.legacy.iterdir()):
                    e.replace(self.new / e.name)
                self.legacy.rmdir()
                self.legacy.symlink_to(self.new)
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(shared.os, "rename", peer_wins)
        shared.migrate_legacy_data_dir()
        assert capsys.readouterr().err == ""
        assert self.legacy.is_symlink()

    def test_a_real_failure_is_still_reported(self, monkeypatch, capsys):
        """The flip side: an error that leaves nothing migrated must not be
        waved through as a lost race."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")

        def vanish(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(shared.os, "rename", vanish)
        shared.migrate_legacy_data_dir()
        assert "could not migrate" in capsys.readouterr().err

    def test_live_state_collision_still_refuses(self, capsys):
        """Two real tokens means the fork already happened; picking a winner
        would destroy one side's bus."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("legacy-token")
        self.new.mkdir(parents=True)
        (self.new / "token").write_text("new-token")
        shared.migrate_legacy_data_dir()
        assert (self.legacy / "token").read_text() == "legacy-token"
        assert (self.new / "token").read_text() == "new-token"
        assert not self.legacy.is_symlink()
        assert "cannot merge" in capsys.readouterr().err

    def test_peer_finishing_mid_check_is_not_reported_as_a_conflict(self, monkeypatch):
        """is_symlink() then is_dir(): a peer completing the migration between
        the two makes is_dir() true *through the new symlink*, so src and dst
        are the same directory and every entry collides with itself. With
        when: "always" every session opens into this window."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "clients").mkdir()
        real_is_dir = Path.is_dir

        def peer_wins_first(p):
            if p == self.legacy and not self.legacy.is_symlink():
                os.rename(self.legacy, self.new)
                self.legacy.symlink_to(self.new)
            return real_is_dir(p)

        monkeypatch.setattr(Path, "is_dir", peer_wins_first)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"

    def test_peer_creating_the_symlink_first_is_not_a_failure(self, monkeypatch, capsys):
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        real_symlink_to = Path.symlink_to

        def taken(p, target, **kw):
            real_symlink_to(p, target, **kw)
            raise FileExistsError(17, "File exists")

        monkeypatch.setattr(Path, "symlink_to", taken)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert capsys.readouterr().err == ""

    def _refuse(self, monkeypatch, name: str):
        real_rename = os.rename

        def refuse_one(src, dst):
            if Path(src).name == name:
                raise PermissionError(13, "Permission denied")
            return real_rename(src, dst)

        monkeypatch.setattr(shared.os, "rename", refuse_one)

    def test_disposable_entry_stuck_still_reaches_the_symlink(self, monkeypatch, capsys):
        """A regenerable entry that won't move must not abort before the
        symlink: a half-drained legacy dir left as a real directory is the
        split namespace this all exists to prevent."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "venv").mkdir()
        self.new.mkdir(parents=True)
        self._refuse(monkeypatch, "venv")
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"
        assert "venv" in capsys.readouterr().err

    def test_stuck_live_state_rolls_back_what_already_moved(
            self, monkeypatch, capsys):
        """Bailing out *after* moving some live state is the same fork by
        another route: token stuck behind while clients/ and the pidfile have
        gone leaves no symlink and no marker, so the next client mints a fresh
        token and connected peers get `unauthorized`."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "clients").mkdir()
        (self.legacy / "messages.log").write_text("log")
        (self.legacy / "token").write_text("live")
        self.new.mkdir(parents=True)
        self._refuse(monkeypatch, "token")
        shared.migrate_legacy_data_dir()
        assert not self.legacy.is_symlink()
        # Everything is back where it started — no half-migrated split.
        assert (self.legacy / "token").read_text() == "live"
        assert (self.legacy / "clients").is_dir()
        assert (self.legacy / "messages.log").read_text() == "log"
        assert not (self.new / "clients").exists()
        assert not (self.new / "messages.log").exists()
        assert not (self.new / shared.MIGRATION_MARKER).exists()

    def test_entry_appearing_mid_drain_is_not_clobbered(self, monkeypatch, capsys):
        """`collisions` is computed a loop earlier. With when: "always" a peer
        can reach ensure_token() in that window, and os.rename would silently
        replace the live token with the legacy one."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "clients").mkdir()
        (self.legacy / "token").write_text("legacy")
        self.new.mkdir(parents=True)
        real_rename = os.rename

        def peer_writes_token(src, dst):
            out = real_rename(src, dst)
            if Path(src).name == "clients":
                (self.new / "token").write_text("live")  # a peer got there
            return out

        monkeypatch.setattr(shared.os, "rename", peer_writes_token)
        shared.migrate_legacy_data_dir()
        assert (self.new / "token").read_text() == "live", (
            "the legacy token overwrote the one a peer had just minted"
        )
        assert not self.legacy.is_symlink()

    def test_taken_aside_name_does_not_strand_live_state(self, monkeypatch, capsys):
        """The aside slot can already be occupied by an earlier migration — the
        user is told it is safe to delete, not required to. Returning False
        there after live state has moved would leave hubbub/ holding the token
        and inter-session/ a real directory an older build repopulates."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "venv").mkdir()
        self.new.mkdir(parents=True)
        (self.new / "venv").mkdir()          # forces the set-aside path
        for suffix in ("", *(f"-{n}" for n in range(2, 20))):
            (self.legacy.with_name(f"inter-session.pre-rename{suffix}")).mkdir()
        shared.migrate_legacy_data_dir()
        # Nothing half-done: the token is still where it started.
        assert (self.legacy / "token").read_text() == "live"
        assert not (self.new / "token").exists()
        assert not self.legacy.is_symlink()
        assert "could not finish draining" in capsys.readouterr().err

    def test_dangling_symlink_is_not_a_free_aside_name(self, capsys):
        """Path.exists() follows symlinks, so a dangling one in the aside slot
        reports False and gets handed back as free — then the rename onto it
        fails with ENOTDIR, after the live state has already moved."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "venv").mkdir()
        self.new.mkdir(parents=True)
        (self.new / "venv").mkdir()          # forces the set-aside path
        dangling = self.legacy.with_name("inter-session.pre-rename")
        dangling.symlink_to(self.legacy.with_name("gone"))
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"
        # Skipped the dangling slot rather than trying to rename onto it.
        assert (self.legacy.with_name("inter-session.pre-rename-2") / "venv").is_dir()

    def test_unwritable_parent_rolls_back_rather_than_half_migrating(self, capsys):
        """`src/token -> dst/token` needs no write on the parent, but renaming
        `src` aside does. Failing there after the live move is the same fork."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "venv").mkdir()
        self.new.mkdir(parents=True)
        (self.new / "venv").mkdir()
        parent = self.legacy.parent
        parent.chmod(0o500)
        try:
            shared.migrate_legacy_data_dir()
        finally:
            parent.chmod(0o700)
        assert not self.legacy.is_symlink()
        assert (self.legacy / "token").read_text() == "live"
        assert not (self.new / "token").exists()
        assert "could not finish draining" in capsys.readouterr().err

    def test_aside_falls_back_to_a_free_name(self, capsys):
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "venv").mkdir()
        self.new.mkdir(parents=True)
        (self.new / "venv").mkdir()
        self.legacy.with_name("inter-session.pre-rename").mkdir()
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert (self.new / "token").read_text() == "live"
        assert (self.legacy.with_name("inter-session.pre-rename-2") / "venv").is_dir()

    def test_peer_winning_mid_drain_is_not_reported_as_a_conflict(self, monkeypatch, capsys):
        """Losing this race is routine now that every session open and every
        helper CLI runs the migration."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        self.new.mkdir(parents=True)

        def peer_finishes(*a, **k):
            for e in list(self.legacy.iterdir()):
                e.replace(self.new / e.name)
            (self.new / shared.MIGRATION_MARKER).touch()
            self.legacy.rmdir()
            self.legacy.symlink_to(self.new)
            raise FileExistsError(17, "File exists")

        monkeypatch.setattr(shared.os, "rename", peer_finishes)
        shared.migrate_legacy_data_dir()
        assert capsys.readouterr().err == ""
        assert self.legacy.is_symlink()

    def test_migrated_dir_is_locked_down(self):
        """`uv venv` creates hubbub/ with the default umask, and the bearer
        token ends up inside it."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        self.new.mkdir(parents=True, mode=0o755)
        shared.migrate_legacy_data_dir()
        assert self.legacy.is_symlink()
        assert stat.S_IMODE(os.stat(self.new).st_mode) == 0o700

    def test_stuck_live_state_aborts_rather_than_relocating_the_token(
            self, monkeypatch, capsys):
        """The opposite call for live state. Setting the token aside and
        symlinking anyway moves it out of the live path — the next client mints
        a fresh one and every peer still holding the old one gets
        `unauthorized`. Stopping leaves the imperfect state instead of actively
        breaking a running bus."""
        self.legacy.mkdir(parents=True)
        (self.legacy / "token").write_text("live")
        (self.legacy / "clients").mkdir()
        self.new.mkdir(parents=True)
        self._refuse(monkeypatch, "token")
        shared.migrate_legacy_data_dir()
        assert not self.legacy.is_symlink()
        assert (self.legacy / "token").read_text() == "live"
        assert not (self.legacy.with_name("inter-session.pre-rename")).exists()
        assert "must not be relocated" in capsys.readouterr().err

    def test_env_override_suppresses_migration(self, tmp_path, monkeypatch):
        """An explicit data dir means the caller owns the layout; don't go
        touching the legacy path behind its back."""
        monkeypatch.setenv("HUBBUB_DATA_DIR", str(tmp_path / "explicit"))
        shared.migrate_legacy_data_dir()
        assert not self.legacy.is_symlink()
        assert not self.new.exists()


class TestEnvAliases:
    def test_prefers_new_prefix(self, monkeypatch):
        monkeypatch.setenv("HUBBUB_PORT", "1111")
        monkeypatch.setenv("INTER_SESSION_PORT", "2222")
        assert shared.env("PORT") == "1111"

    def test_falls_back_to_legacy_prefix(self, monkeypatch):
        monkeypatch.delenv("HUBBUB_PORT", raising=False)
        monkeypatch.setenv("INTER_SESSION_PORT", "2222")
        assert shared.env("PORT") == "2222"

    def test_default_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("HUBBUB_PORT", raising=False)
        monkeypatch.delenv("INTER_SESSION_PORT", raising=False)
        assert shared.env("PORT", "9473") == "9473"


class TestResolveListenerKey:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("INTER_SESSION_PPID_OVERRIDE", "12345")
        assert shared.resolve_listener_key() == 12345

    def test_falls_back_to_getppid_outside_cc(self, monkeypatch):
        monkeypatch.delenv("INTER_SESSION_PPID_OVERRIDE", raising=False)
        key = shared.resolve_listener_key()
        # In pytest there's no CC ancestor; falls back to getppid().
        # Either a CC ancestor pid (>0) or getppid() (>0).
        assert isinstance(key, int) and key > 0

    def test_invalid_override_falls_back(self, monkeypatch):
        monkeypatch.setenv("INTER_SESSION_PPID_OVERRIDE", "not-a-number")
        key = shared.resolve_listener_key()
        assert isinstance(key, int) and key > 0


class TestFindCcAncestorPid:
    def test_returns_int(self):
        # In test env there's no CC ancestor; -1 is expected.
        # In real CC, returns a positive pid.
        result = shared.find_cc_ancestor_pid()
        assert isinstance(result, int)
        assert result == -1 or result > 0


class TestFindCcAncestorPidMatching:
    """Verify which ancestor pid the walk stops at for different launcher
    layouts. Uses a fake process tree via monkeypatched psutil so the test
    is isolated from the host machine's real ancestry."""

    @staticmethod
    def _fake_process_cls(tree):
        """Return a fake `psutil.Process` bound to `tree` (pid → {cmdline, ppid})."""
        import psutil

        class _FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                if pid not in tree:
                    raise psutil.NoSuchProcess(pid)
                self._info = tree[pid]

            def cmdline(self):
                return list(self._info["cmdline"])

            def ppid(self):
                return self._info["ppid"]

        return _FakeProcess

    def test_foreground_symlinked_claude_still_matches(self, monkeypatch):
        """argv[0]='claude' (the ~/.local/bin/claude symlink path) continues
        to match. Guards against the bg-versioned-path patch accidentally
        regressing the common foreground case."""
        import psutil
        tree = {100: {"cmdline": ["claude", "--effort", "max"], "ppid": 1}}
        monkeypatch.setattr(os, "getppid", lambda: 100)
        monkeypatch.setattr(psutil, "Process", self._fake_process_cls(tree))
        assert shared.find_cc_ancestor_pid() == 100

    def test_background_versioned_path_returns_per_session_pid(self, monkeypatch):
        """Regression: in CC's background mode (Agent View / `claude --bg`)
        the per-session spare is launched by its resolved versioned path
        (e.g. ~/.local/share/claude/versions/2.1.146 --bg-spare), whose
        basename is a version number, not `claude`. The walk must recognize
        the versioned path and stop at the per-session pid (100) instead of
        continuing up to the shared supervisor (50), which would collide
        across distinct bg sessions on the ppid flock."""
        import psutil
        tree = {
            100: {"cmdline": ["/Users/x/.local/share/claude/versions/2.1.146",
                              "--bg-spare"], "ppid": 50},
            50:  {"cmdline": ["claude", "daemon", "run"], "ppid": 1},
        }
        monkeypatch.setattr(os, "getppid", lambda: 100)
        monkeypatch.setattr(psutil, "Process", self._fake_process_cls(tree))
        assert shared.find_cc_ancestor_pid() == 100

    def test_distinct_bg_sessions_resolve_to_distinct_pids(self, monkeypatch):
        """The core invariant: two background sessions share a supervisor
        ancestor but each has its own bg-spare. They must resolve to
        DIFFERENT pids so they don't collide on the ppid dedup flock —
        otherwise the second `/hubbub:talk connect` exits with `another
        monitor for this session is already running`."""
        import psutil
        tree = {
            100: {"cmdline": ["/Users/x/.local/share/claude/versions/2.1.146",
                              "--bg-spare", "A"], "ppid": 50},
            200: {"cmdline": ["/Users/x/.local/share/claude/versions/2.1.146",
                              "--bg-spare", "B"], "ppid": 50},
            50:  {"cmdline": ["claude", "daemon", "run"], "ppid": 1},
        }
        monkeypatch.setattr(psutil, "Process", self._fake_process_cls(tree))

        monkeypatch.setattr(os, "getppid", lambda: 100)
        a = shared.find_cc_ancestor_pid()
        monkeypatch.setattr(os, "getppid", lambda: 200)
        b = shared.find_cc_ancestor_pid()

        assert a == 100
        assert b == 200
        assert a != b


class TestAutoNameFromCwd:
    def test_simple_basename(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        d = tmp_path / "auth-refactor"
        d.mkdir()
        monkeypatch.chdir(d)
        assert shared.auto_name_from_cwd() == "auth-refactor"

    def test_uppercase_lowered(self, tmp_path, monkeypatch):
        d = tmp_path / "MyProject"
        d.mkdir()
        monkeypatch.chdir(d)
        assert shared.auto_name_from_cwd() == "myproject"

    def test_punctuation_replaced_with_dash(self, tmp_path, monkeypatch):
        d = tmp_path / "foo.bar_baz"
        d.mkdir()
        monkeypatch.chdir(d)
        assert shared.auto_name_from_cwd() == "foo-bar-baz"

    def test_unicode_dropped(self, tmp_path, monkeypatch):
        d = tmp_path / "プロジェクト-X"
        d.mkdir()
        monkeypatch.chdir(d)
        # Non-ASCII chars dropped, only "-x" remains -> starts with non-alnum -> ""
        # OR truncates to "x". Either way it must be valid name or empty.
        result = shared.auto_name_from_cwd()
        assert result == "" or shared.validate_name(result)

    def test_unrelated_explicit_cwd(self):
        # Explicit cwd works without monkeypatching
        result = shared.auto_name_from_cwd("/path/to/payments-debug")
        assert result == "payments-debug"

    def test_truncates_to_40_chars(self, tmp_path, monkeypatch):
        long_name = "a" * 100
        d = tmp_path / long_name
        d.mkdir()
        monkeypatch.chdir(d)
        result = shared.auto_name_from_cwd()
        assert len(result) <= 40
        if result:
            assert shared.validate_name(result)


class TestVerifyServerIdentity:
    """Round-7/12: refuse to send the token to a process that isn't our
    server.py. Round-12 made this fail-closed when pidfile missing/dead
    (the original conservative-true was the squatter loophole)."""

    def test_no_pidfile_fails_closed(self, tmp_data_dir, monkeypatch):
        """The squatter scenario: no real server, just something on port 9473.
        Without a pidfile we can't verify; fail closed."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        assert not shared.verify_server_identity()

    def test_dead_pid_fails_closed(self, tmp_data_dir, monkeypatch):
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.pidfile_path().write_text("999999\n")
        assert not shared.verify_server_identity()

    def test_unreadable_pidfile_fails_closed(self, tmp_data_dir, monkeypatch):
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.pidfile_path().write_text("not-a-number")
        assert not shared.verify_server_identity()

    def test_alive_non_server_pid_returns_false(self, tmp_data_dir, monkeypatch):
        """An alive process whose cmdline ISN'T bin/server.py fails."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.pidfile_path().write_text(f"{os.getpid()}\n")
        assert not shared.verify_server_identity()

    def test_endpoint_metadata_must_match_when_port_supplied(self, tmp_data_dir, monkeypatch):
        """Round-13: a pidfile for a real server on port A must not verify a
        different listener on port B."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.write_server_identity(os.getpid(), "127.0.0.1", 1111)

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def cmdline(self):
                return ["/usr/bin/python", "/repo/bin/server.py"]

        fake_psutil = types.SimpleNamespace(
            Process=FakeProcess,
            NoSuchProcess=RuntimeError,
            AccessDenied=PermissionError,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        assert shared.verify_server_identity("127.0.0.1", 1111)
        assert not shared.verify_server_identity("127.0.0.1", 2222)
        assert not shared.verify_server_identity("localhost", 1111)

    def test_multiple_ports_do_not_clobber(self, tmp_data_dir, monkeypatch):
        """Round-14 fix: two servers in the same data_dir on different ports
        write per-port pidfiles. Either listener verifies independently."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.write_server_identity(os.getpid(), "127.0.0.1", 1111)
        shared.write_server_identity(os.getpid(), "127.0.0.1", 2222)
        # Both pidfiles exist independently.
        assert shared.pidfile_path(1111).exists()
        assert shared.pidfile_path(2222).exists()
        assert shared.pidfile_meta_path(1111).exists()
        assert shared.pidfile_meta_path(2222).exists()
        # Helpers verifying port 1111 read 1111's metadata; verifying 2222
        # reads 2222's metadata. No cross-clobber. (Identity check still
        # requires bin/server.py in cmdline, which our pytest pid won't have,
        # so verify_server_identity() returns False here — but we're testing
        # that the FILES exist independently, not that the cmdline check
        # passes.)
        assert shared.pidfile_path(1111).read_text().strip() == str(os.getpid())
        assert shared.pidfile_path(2222).read_text().strip() == str(os.getpid())

    def test_multiple_hosts_same_port_do_not_clobber(self, tmp_data_dir, monkeypatch):
        """Round-19: endpoint identity must include host as well as port.
        Otherwise a custom-host listener on the same port overwrites the
        default-host listener's pid/meta and makes legitimate clients fail
        identity verification."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        shared.write_server_identity(os.getpid(), "127.0.0.1", 1111)
        shared.write_server_identity(os.getpid(), "localhost", 1111)

        assert shared.pidfile_path(1111, "127.0.0.1").exists()
        assert shared.pidfile_path(1111, "localhost").exists()
        assert shared.pidfile_meta_path(1111, "127.0.0.1").exists()
        assert shared.pidfile_meta_path(1111, "localhost").exists()

        default_meta = json.loads(shared.pidfile_meta_path(1111, "127.0.0.1").read_text())
        localhost_meta = json.loads(shared.pidfile_meta_path(1111, "localhost").read_text())
        assert default_meta["host"] == "127.0.0.1"
        assert localhost_meta["host"] == "localhost"

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def cmdline(self):
                return ["/usr/bin/python", "/repo/bin/server.py", "--port", "1111"]

        fake_psutil = types.SimpleNamespace(
            Process=FakeProcess,
            NoSuchProcess=RuntimeError,
            AccessDenied=PermissionError,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        assert shared.verify_server_identity("127.0.0.1", 1111)
        assert shared.verify_server_identity("localhost", 1111)

    def test_legacy_pidfile_must_match_host_too(self, tmp_data_dir, monkeypatch):
        """Round-20 fix: when falling back to legacy server.<port>.pid (no
        host-scoped file, no .meta), require BOTH cmdline --port AND --host
        to match. An old default-host pidfile must NOT validate a request
        for a different host."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        # Legacy: server.9473.pid only, no metadata, no host-scoped file.
        shared.pidfile_path(9473).write_text(f"{os.getpid()}\n")

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
            def cmdline(self):
                # Default-host server, no explicit --host arg
                return ["/usr/bin/python", "/repo/bin/server.py", "--port", "9473"]

        fake_psutil = types.SimpleNamespace(
            Process=FakeProcess,
            NoSuchProcess=RuntimeError,
            AccessDenied=PermissionError,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        # The legacy pidfile validates the default host
        assert shared.verify_server_identity("127.0.0.1", 9473)
        # …but MUST NOT validate a different host using the same pidfile
        assert not shared.verify_server_identity("evil.example", 9473)
        assert not shared.verify_server_identity("localhost", 9473)

    def test_missing_endpoint_metadata_falls_back_to_cmdline_port(self, tmp_data_dir, monkeypatch):
        """Upgrade path: a server from the previous version has only the plain
        pidfile. Accept it only if psutil cmdline proves bin/server.py + port."""
        monkeypatch.setenv("INTER_SESSION_DATA_DIR", str(tmp_data_dir))
        shared.secure_dir(tmp_data_dir)
        # Pidfile written for port 1111 but no .meta companion (simulates an
        # older server that didn't write metadata).
        shared.pidfile_path(1111).write_text(f"{os.getpid()}\n")

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def cmdline(self):
                return ["/usr/bin/python", "/repo/bin/server.py", "--port", "1111"]

        fake_psutil = types.SimpleNamespace(
            Process=FakeProcess,
            NoSuchProcess=RuntimeError,
            AccessDenied=PermissionError,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        assert shared.verify_server_identity("127.0.0.1", 1111)
        assert not shared.verify_server_identity("127.0.0.1", 2222)


class TestSafePidAlive:
    def test_self_pid_alive(self):
        assert shared.safe_pid_alive(os.getpid())

    def test_pid_zero_dead(self):
        assert not shared.safe_pid_alive(0)

    def test_negative_pid_dead(self):
        assert not shared.safe_pid_alive(-1)

    def test_unlikely_pid_dead(self):
        # Pid 999999 unlikely to exist
        assert not shared.safe_pid_alive(999999)


class TestRotateLog:
    def test_under_threshold_noop(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("hello\n")
        shared.rotate_log_if_needed(log, max_bytes=100, backups=3)
        assert log.exists()
        assert (tmp_path / "x.log.1").exists() is False

    def test_over_threshold_rotates(self, tmp_path):
        log = tmp_path / "x.log"
        log.write_text("X" * 200)
        shared.rotate_log_if_needed(log, max_bytes=100, backups=3)
        # Original moved to .1; original gone (until next writer creates it)
        assert (tmp_path / "x.log.1").exists()

    def test_keeps_backups_count(self, tmp_path):
        log = tmp_path / "x.log"
        for i in range(5):
            log.write_text("X" * 200)
            shared.rotate_log_if_needed(log, max_bytes=100, backups=3)
            log.touch()  # ensure file exists for next write
        # Should not have x.log.4 or higher
        assert not (tmp_path / "x.log.4").exists()
        assert not (tmp_path / "x.log.5").exists()


class TestValidatorsTypeSafe:
    """Regression: validate_name/validate_label used to raise TypeError on
    non-string input (e.g., None, int). Now they return False."""

    def test_validate_name_non_string(self):
        assert not shared.validate_name(None)
        assert not shared.validate_name(123)
        assert not shared.validate_name(["a"])
        assert not shared.validate_name({"x": 1})

    def test_validate_label_non_string(self):
        assert not shared.validate_label(None)
        assert not shared.validate_label(123)
        assert not shared.validate_label(["x"])


class TestRotateLogRetention:
    """Regression: rotation used to overwrite path.1 instead of shifting
    to path.2 → path.3 etc. Now backups are preserved up to N."""

    def test_rotation_shifts_backups(self, tmp_path):
        log = tmp_path / "x.log"
        # Create initial content
        log.write_text("v0\n" * 50)  # well over our threshold
        shared.rotate_log_if_needed(log, max_bytes=10, backups=3)
        # Now: log.1 has v0, log gone
        assert (tmp_path / "x.log.1").exists()
        assert not log.exists()
        # Write second batch
        log.write_text("v1\n" * 50)
        shared.rotate_log_if_needed(log, max_bytes=10, backups=3)
        # Now: log.2 has v0, log.1 has v1
        assert "v0" in (tmp_path / "x.log.2").read_text()
        assert "v1" in (tmp_path / "x.log.1").read_text()
        # Third batch
        log.write_text("v2\n" * 50)
        shared.rotate_log_if_needed(log, max_bytes=10, backups=3)
        # log.3=v0, log.2=v1, log.1=v2
        assert "v0" in (tmp_path / "x.log.3").read_text()
        assert "v1" in (tmp_path / "x.log.2").read_text()
        assert "v2" in (tmp_path / "x.log.1").read_text()
        # Fourth batch — oldest (v0) drops
        log.write_text("v3\n" * 50)
        shared.rotate_log_if_needed(log, max_bytes=10, backups=3)
        # log.3=v1, log.2=v2, log.1=v3, no .4
        assert not (tmp_path / "x.log.4").exists()
        assert "v1" in (tmp_path / "x.log.3").read_text()
        assert "v2" in (tmp_path / "x.log.2").read_text()
        assert "v3" in (tmp_path / "x.log.1").read_text()


class TestListenerLockHeld:
    """The staleness authority for `list.py --self`, which prints a pid the
    disconnect flow tells an agent to kill. A cmdline substring check could not
    tell one session's monitor from another's, so a reused pid belonging to a
    *live peer's* client.py would have been offered as the kill target."""

    def test_false_when_nobody_holds_it(self, tmp_path):
        lock = tmp_path / "1.lock"
        lock.touch()
        assert shared.listener_lock_held(lock) is False

    def test_missing_lock_file_is_not_held(self, tmp_path):
        assert shared.listener_lock_held(tmp_path / "never-existed.lock") is False

    def test_probing_creates_nothing(self, tmp_path):
        """A read-only query must not litter clients/ with lock files — and
        O_CREAT on a full or read-only filesystem would fail into the
        catch-all, reporting a session with no monitor as connected."""
        shared.listener_lock_held(tmp_path / "absent.lock")
        assert list(tmp_path.iterdir()) == []

    def test_true_while_a_holder_lives(self, tmp_path):
        lock = tmp_path / "1.lock"
        held = open(lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert shared.listener_lock_held(lock) is True
        finally:
            held.close()

    def test_released_when_the_holder_dies(self, tmp_path):
        """The kernel drops the lock however the process exits — which is why
        this beats a pid check for a monitor that was SIGKILLed."""
        lock = tmp_path / "1.lock"
        proc = subprocess.Popen([
            sys.executable, "-c",
            f"import fcntl,time;f=open({str(lock)!r},'w');"
            f"fcntl.flock(f,fcntl.LOCK_EX);time.sleep(30)",
        ])
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not shared.listener_lock_held(lock):
                time.sleep(0.05)
            assert shared.listener_lock_held(lock) is True
            proc.kill()
            proc.wait(timeout=5)
            deadline = time.time() + 5
            while time.time() < deadline and shared.listener_lock_held(lock):
                time.sleep(0.05)
            assert shared.listener_lock_held(lock) is False
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_probing_does_not_keep_the_lock(self, tmp_path):
        lock = tmp_path / "1.lock"
        assert shared.listener_lock_held(lock) is False
        assert shared.listener_lock_held(lock) is False


class TestSafePidAliveSurvivesJunk:
    """Every caller feeds this a number read off disk — a clients/*.session
    file or a server pidfile — so it has to survive whatever is in there.
    os.kill raises OverflowError, which is an ArithmeticError and so slips
    past `except OSError`, taking down list.py --self with it."""

    def test_pid_too_large_for_a_c_int(self):
        assert shared.safe_pid_alive(10 ** 30) is False

    def test_non_numeric(self):
        assert shared.safe_pid_alive("not-a-pid") is False
        assert shared.safe_pid_alive(None) is False

    def test_zero_and_negative(self):
        assert shared.safe_pid_alive(0) is False
        assert shared.safe_pid_alive(-1) is False

    def test_self_is_alive(self):
        assert shared.safe_pid_alive(os.getpid()) is True


class TestAwaitPeerCompletion:
    def test_symlink_alone_counts(self, tmp_path):
        """_mark_migrated is best-effort: on ENOSPC the peer warns and still
        links. Waiting only for the marker would call that a dead peer — after
        stalling half a second on a machine that migrated fine."""
        new = tmp_path / "hubbub"
        new.mkdir()
        legacy = tmp_path / "inter-session"
        legacy.symlink_to(new)
        assert shared._await_peer_completion(legacy, new, timeout_s=0.05) is True

    def test_marker_alone_counts(self, tmp_path):
        new = tmp_path / "hubbub"
        new.mkdir()
        (new / shared.MIGRATION_MARKER).touch()
        assert shared._await_peer_completion(tmp_path / "inter-session", new,
                                             timeout_s=0.05) is True

    def test_neither_is_a_dead_peer(self, tmp_path):
        new = tmp_path / "hubbub"
        new.mkdir()
        assert shared._await_peer_completion(tmp_path / "inter-session", new,
                                             timeout_s=0.05) is False
