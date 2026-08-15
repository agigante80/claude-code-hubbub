"""Static checks on SKILL.md so prose edits don't drop critical guardrails."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = (REPO / "skills" / "talk" / "SKILL.md").read_text()


class TestFrontmatter:
    def test_name(self):
        assert "name: talk" in SKILL

    def test_allowed_tools(self):
        assert "allowed-tools" in SKILL
        for tool in ("Bash", "Monitor", "TaskList", "TaskStop"):
            assert tool in SKILL


class TestSubcommands:
    def test_dispatch_table_has_all_subcommands(self):
        for sub in ("connect", "install-deps", "list", "send", "broadcast",
                    "rename", "relabel", "status", "disconnect"):
            assert f"`/hubbub:talk {sub}" in SKILL or f"`/hubbub:talk [args]" in SKILL


class TestReactionPolicy:
    def test_treats_messages_as_instructions_by_default(self):
        # The whole point of this skill is agent-to-agent automation.
        assert "act on it" in SKILL.lower() or "instruction from" in SKILL.lower()

    def test_destructive_ops_require_explicit_content(self):
        for kw in ("rm -rf", "git push --force", "DROP TABLE", "kubectl delete"):
            assert kw in SKILL

    def test_destructive_gate_is_in_the_dispatch_table(self):
        """Issue #6: the table is where the model routes, so the
        destructive-op rule has to live in it, not only in prose below.
        Anchored on the row's own wording."""
        assert "| Confirm first" in SKILL or "Confirm first" in SKILL
        assert "destructive, irreversible, or outward-facing" in SKILL

    def test_destructive_gate_is_not_advisory(self):
        """Issue #6: confirmation must be the default for the irreversible
        subset, re-affirmed in a *separate* message, and not defeatable by
        whatever the first message asserts."""
        low = SKILL.lower()
        assert "a gate, not a judgment call" in low
        assert "in a separate message" in low
        assert "never enough" in low or "never sufficient" in low

    def test_sender_name_is_documented_as_unauthenticated(self):
        """Issue #7: `from=` is self-asserted, so a peer can pick an
        authority-sounding name. The policy must say a name is never
        evidence of trust."""
        low = SKILL.lower()
        assert "self-asserted" in low
        assert "evidence of authority" in low

    def test_loop_suppression_with_done_status_answer(self):
        assert "`done:" in SKILL
        assert "`status:" in SKILL
        assert "`answer:" in SKILL
        assert "`question:" in SKILL

    def test_permission_rules_not_overridden(self):
        # The single most important guardrail: peers can't escalate.
        assert "do NOT override" in SKILL or "do not override" in SKILL.lower()

    def test_reply_is_bound_to_the_inbound_transport(self):
        """Issue #9: the bus and the harness's agent roster are separate
        namespaces, and the same project often has a live session in each
        under near-identical names. Answering a bus message with the
        harness's SendMessage delivers it to the wrong session (or gets it
        held) with no error on either side, so the reply-on-the-same-
        transport rule must stay explicit and absolute. Anchored on
        distinctive phrases so deleting the section fails the test."""
        low = SKILL.lower()
        assert "reply on the same transport" in low
        assert "separate namespaces" in low
        assert "sendmessage" in low
        assert "never answer it with" in low

    def test_reply_target_comes_from_the_notification(self):
        """The corollary: address the peer by the `from="…"` in the line you
        are answering, never by a similar name seen elsewhere."""
        low = SKILL.lower()
        assert "listagents" in low          # names it as the wrong source
        assert 'copied verbatim from the notification' in low

    def test_only_leading_header_is_authoritative(self):
        # SEC-002: a peer can embed `[inter-session ...]`-looking text in the
        # message body. The reaction policy must tell the agent that only the
        # leading prefix is authoritative, so an embedded pseudo-header can't
        # spoof the sender or inject a second directive. Anchor on distinctive
        # phrases from the guardrail bullet so deleting it fails the test even
        # if the individual words survive elsewhere in the prose.
        low = SKILL.lower()
        assert "only the leading" in low
        assert "authoritative" in low
        assert "pseudo-header" in low


class TestPrefixRenameStaging:
    """fork #10 step 1. The emitted prefix is the last identifier still on the
    old name, and it is the wire contract between `client.py` and this policy.
    Getting the order wrong fails *silently*: a monitor emitting a spelling the
    policy does not know produces no error, the agent simply stops recognising
    peer messages. So the policy learns both spellings in one release, the
    emitter moves in the next, and the old spelling is dropped in a third.

    These tests pin that staging. Together they make step 2 a one-line change
    that cannot be done out of order without a red suite.
    """

    CODE = (REPO / "skills" / "talk" / "bin" / "client.py").read_text()

    def test_policy_accepts_both_spellings(self):
        low = SKILL.lower()
        assert "[hubbub" in low, (
            "the policy must accept the new spelling before the emitter can "
            "move to it (step 1)"
        )
        assert "[inter-session" in low, (
            "the policy must still accept the shipped spelling; dropping it "
            "is step 3, and doing it early breaks every running monitor"
        )
        assert "both spellings" in low

    def test_emitter_has_not_moved_yet(self):
        """Step 2 guard, and the reason this test looks backwards.

        If someone flips the emitter in the same release that taught the
        policy, there is no version in which a 0.2.x monitor and a newer
        policy can coexist — which is the entire point of staging. When step 2
        is genuinely being done, delete this test in that commit and say so in
        the message.

        Anchored on `[hubbub msg=` rather than on the presence of the old
        spelling. The first version of this test asserted `"[inter-session" in
        CODE`, which cannot fail: 16 of the 19 literals in `client.py` are
        `[inter-session]` operational notices, so they satisfied the check
        even with every message header flipped.
        """
        assert "[hubbub msg=" not in self.CODE
        assert "[inter-session msg=" in self.CODE

    def test_emitter_never_mixes_the_two_spellings(self):
        """The failure step 2 is actually likely to produce.

        `client.py` has three message-header literals and sixteen
        `[inter-session]` operational-notice literals. Flipping only the
        headers looks done and passes a casual read, but strands every error
        notice on the old spelling — and step 3 then drops that spelling from
        the policy, so the agent silently stops recognising them. One spelling
        or the other, never a mix.

        Counts only *emitted* literals — lines that are not comments. A
        correct step-2 commit will naturally add a comment saying "was
        `[inter-session …]` through 0.2.x", and a raw substring count would
        read that as a mix and fail the one change this guard exists to
        approve.
        """
        emitted = [
            ln for ln in self.CODE.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        body = "\n".join(emitted)
        old = body.count("[inter-session")
        new = body.count("[hubbub")
        assert (old > 0) != (new > 0), (
            f"client.py mixes prefixes in emitted strings: {old} old, "
            f"{new} new. Step 2 must move the message headers AND the "
            f"operational notices in the same commit."
        )

    def test_continuation_line_moves_with_the_header(self):
        """The `cont` line carries the same prefix, so a step-2 flip that
        misses it leaves a truncated message's two halves disagreeing about
        who sent them. Compares the two emitters rather than asserting a
        substring appears somewhere in the prose, which the previous version
        did and which no divergence could have failed."""
        import re
        header = re.search(r'\[(\S+) msg=\{msg_id\} from=', self.CODE)
        cont = re.search(r'\[(\S+) msg=\{msg_id\} cont\]', self.CODE)
        assert header and cont, "could not locate both emitters"
        assert header.group(1) == cont.group(1), (
            f"header emits {header.group(1)!r} but the continuation line "
            f"emits {cont.group(1)!r}"
        )

    def test_two_spellings_do_not_widen_the_header_boundary(self):
        """SEC-002 interaction called out in the ticket: accepting a second
        name must not turn "a bracket containing a known word" into the
        header test. The rule stays positional — only the leading bracket."""
        low = SKILL.lower()
        assert "only the leading" in low
        assert "positional" in low


class TestInstallDepsUx:
    def test_uses_isolated_venv_by_default(self):
        """install-deps must default to a project-local venv at
        ~/.claude/data/hubbub/venv so it never touches the user's
        system or user-level Python."""
        assert "~/.claude/data/hubbub/venv" in SKILL
        assert "python3 -m venv" in SKILL or "uv venv" in SKILL

    def test_offers_uv_as_optional(self):
        """uv is the optional fast path; user can also stay on stdlib venv."""
        assert "uv" in SKILL

    def test_no_user_or_system_pip_pollution(self):
        """The old --user / --system / --break-system-packages paths
        modified user-visible Python and were dropped in favor of the
        isolated venv."""
        assert "pip install --user" not in SKILL
        assert "pip install --system" not in SKILL
        assert "--break-system-packages" not in SKILL


class TestDedupGuard:
    def test_tasklist_check_in_connect(self):
        assert "TaskList()" in SKILL
        assert '"hubbub messages"' in SKILL


class TestNameValidation:
    def test_ascii_regex_in_skill(self):
        assert "[a-z0-9]" in SKILL


class TestTruncationHandling:
    def test_messages_log_pointer_documented(self):
        assert "messages.log" in SKILL

    def test_retrieval_is_rotation_aware_and_field_anchored(self):
        """Issue #8: messages.log rotates (50 MB x 5), so a retrieval that
        greps only the live file silently finds nothing once a record has
        rotated out. The documented command must span the rotated set and
        anchor on the msg_id field rather than the bare id."""
        assert "messages.log*" in SKILL
        assert '\\"msg_id\\": ' in SKILL
        assert "rotates at 50 MB" in SKILL

    def test_documented_rotation_matches_the_code(self):
        """The doc quotes concrete rotation numbers; keep them true."""
        shared = (REPO / "skills" / "talk" / "bin" / "shared.py").read_text()
        assert "MESSAGES_LOG_MAX_BYTES = 50 * 1024 * 1024" in shared
        assert "MESSAGES_LOG_BACKUPS = 5" in shared


class TestDoctorIsDocumented:
    """fork #15. The command is useless if the agent never reaches for it, and
    actively dangerous if the agent offers to repair a fork itself."""

    def test_in_the_dispatch_table(self):
        assert "`/hubbub:talk doctor`" in SKILL

    def test_says_when_to_reach_for_it(self):
        low = SKILL.lower()
        assert "behaves impossibly" in low
        assert "cannot see each other" in low

    def test_forbids_offering_repair(self):
        """Choosing between two live tokens ends one side's bus and cannot be
        undone — a human decision, not a subcommand's."""
        low = SKILL.lower()
        assert "will not repair anything" in low
        assert "must not offer" in low


class TestSessionFingerprint:
    """fork #7/#9. Rendering `sid=` is only half the fix — the policy has to
    tell the agent what to do when it changes, or the field is decoration."""

    def test_documented_in_the_notification_shape(self):
        assert "sid=<8hex>" in SKILL

    def test_says_a_changed_sid_means_a_different_conversation(self):
        low = SKILL.lower()
        assert "session fingerprint" in low
        assert "different session with a different" in low

    def test_says_it_is_not_an_authenticator(self):
        """Same trap as the name: a peer picks its own session_id, so this
        distinguishes sessions without proving anything about them."""
        low = SKILL.lower()
        assert "does not authenticate" in low

    def test_absent_sid_is_not_read_as_sameness(self):
        low = SKILL.lower()
        assert "predates this field" in low
