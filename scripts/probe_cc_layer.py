#!/usr/bin/env python3
"""On-demand probe of the Claude Code layer: `make probe-cc`.

The half of #34 that pytest cannot have. `tests/test_cc_harness.py` spawns the
command CC would spawn and proves a monitor registers, a message is delivered
and the first line fits the clip — all without a `claude` binary, because
`.github/workflows/ci.yml` has none and `conftest.pytest_sessionfinish` fails
the run on any skip. What it cannot do is put a *real session* on the other
end: whether the SKILL.md connect step registers a monitor from inside
`claude -p`, and whether a 500-UTF-16-unit first line reaches the agent whole.

**This is a report, not a build to fix.** It is never referenced from
`ci.yml`, never a pytest test, and never run unattended: each case spends the
operator's credential, and an overnight loop already lost a night to the
account's weekly limit once. A missing prerequisite is exit 2 with a reason,
never a skip — a live-model check that could not run must not look like one
that passed.

Two cases:

- **plugin-dir** — `claude -p --plugin-dir <repo>`, the `/hubbub:talk` form.
- **standalone** — no `--plugin-dir`, the `/talk` form, which requires the
  documented `ln -s <repo>/skills/talk ~/.claude/skills/talk` install. The
  script creates and removes nothing under `~/.claude`.

The measurement is #38's number observed from the receiving side. The sender
registers under the maximal name and label the real CLI accepts (40 ASCII
characters; 60 × U+1F600, two UTF-16 units each), which renders a 220-unit
header and leaves `_format_msg` a 279-unit body, so the whole first line is
exactly 500 units — deterministically, which is why the body is a sentinel
ruler rather than free text. A 500-character verbatim echo is the one thing a
model turn cannot be trusted to reproduce; `|0270|` present and `|0280|`
absent is something it can.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "skills" / "talk" / "bin"
sys.path.insert(0, str(REPO / "skills" / "talk"))
from bin import shared  # noqa: E402  (after the sys.path insert, by design)

# The maximal sender, reachable through the real CLI: NAME_RE allows 40
# characters, validate_label allows 60 code points. Together they render the
# widest header `_format_msg` can produce, which is the one the 500-unit
# measurement turns on.
SENDER_NAME = ("probe-sender-" + "x" * 40)[:40]
SENDER_LABEL = "\U0001F600" * 60

RULER_LEN = 1000
BODY_UNITS = 279          # what a 220-unit header leaves under the 500 clip
LAST_SENTINEL = "|0270|"  # occupies 270-275, the last one wholly inside it
ABSENT_SENTINEL = "|0280|"

# Bounds, in the order they can fire. The ceiling sits deliberately *behind*
# the wait: it counts from the model's last turn, which precedes the send,
# while WAIT_AFTER_SEND counts from the send. At 180000 the two race and CC's
# dropped partial result can pre-empt the script's own FAIL line.
BG_WAIT_CEILING_MS = 240000
WAIT_AFTER_SEND = 180.0
BACKSTOP_AFTER_SEND = 120.0
# Generous on purpose. It covers session start, the skill expansion and
# however many turns the model takes before it calls `Monitor()` — and a
# `SessionStart` hook that injects a large preamble (the operator's own
# settings can, and this machine's does) makes the model's first turn slower
# and occasionally spends a turn elsewhere first. Measured: a bare
# `claude -p --plugin-dir <repo>` round-trip is ~6 s here, so 150 s is roughly
# twenty times the floor; a run that misses it has a real problem, not a slow
# machine.
REGISTER_TIMEOUT = 150.0

HEADER_RE = re.compile(
    r'\[hubbub msg=[0-9a-f]{8} from="' + re.escape(SENDER_NAME) +
    r'" sid=[0-9a-f]{8} "' + re.escape(SENDER_LABEL) + r'" truncated=1000\]'
)

# One prompt shape, two skill spellings. Three things are load-bearing:
#
# - The **disconnect comes before the report**, because the model's last text
#   is what the stream's `result` line carries.
# - It names `TaskList`/`TaskStop` rather than saying `/hubbub:talk
#   disconnect`. A *leading* slash command is prompt expansion and costs no
#   permission, but the same text mid-prompt is literal, and a model that
#   answers it by reaching for the `Skill` tool is denied — `--allowedTools`
#   is exactly SKILL.md L14's list and nothing wider. (A denial is still
#   distinguishable: it lands as `permission_denials` and its own FAIL line.)
# - It forbids reading `messages.log`. `Bash` is pre-approved, the log holds
#   the untruncated 1000 characters, and a log-reading model would report a
#   sentinel past |0270| and no truncation — a false PASS dressed as a FAIL.
#
# Observed on 2.1.271, both real runs, and recorded rather than worked around:
# inside `claude -p` a `TaskList()` call returns **no tasks** even with a live
# `Monitor()` watch, and the model falls back to the id `Monitor()` handed it.
# SKILL.md's disconnect flow (L418-421) is `TaskList()` then `TaskStop(<id>)`,
# so under `-p` its first step comes back empty. The run still ends correctly,
# and the prompt deliberately does not depend on `TaskList()` finding
# anything — but a future change here should not start depending on it either.
PROMPT = (
    "/{skill} connect {name}. Then wait for one message to arrive on the bus. "
    "When it arrives, first end the monitor: call TaskList(), find the task "
    'described "hubbub messages", and call TaskStop() on its id. Then, as '
    "your FINAL message, report exactly three things: (1) the notification's "
    'header, from the leading "[hubbub" up to and including the closing "]", '
    "exactly as you received it; (2) the last complete |NNNN| sentinel marker "
    "you can see in the message body; (3) whether Claude Code appended a "
    "truncation suffix to the end of that line — answer item 3 with the "
    f"single token {{present}} if it did and {{absent}} if it did not, and do "
    "not write the suffix out. Report only what the notification line itself "
    "showed — do not read messages.log or any other file, and do not use the "
    "bus to ask anyone anything."
)

# Item 3 is a yes/no question, and the obvious spelling of it cannot be
# graded: asking the model whether the line ended with "...(truncated)" makes
# it quote that string in *either* answer, so `"(truncated)" in target` fails
# a run that passed. It did, on the first real run — the model wrote "No — the
# line ended with |0270|... (three dots, no `(truncated)` suffix)" and the
# probe reported the line as clipped. Two disjoint tokens the model never has
# a reason to write except as an answer are gradeable; a natural-language
# negation is not.
SUFFIX_PRESENT = "SUFFIX-PRESENT"
SUFFIX_ABSENT = "SUFFIX-ABSENT"

# Exactly SKILL.md L14's `allowed-tools` list and nothing wider: a `-p` run
# has no interactive permission host, so an unlisted tool is denied silently
# and surfaces only as `permission_denials` on the result line.
#
# **Passed as ONE argv token, `--allowedTools=…`, and that is not cosmetic.**
# The flag is variadic — it accepts a space-separated list as well as a
# comma-separated one — so in the two-token spelling
# (`--allowedTools", "Monitor,Bash,…", "<prompt>"`) it greedily swallows the
# positional prompt that follows it, and CC exits with `Error: Input must be
# provided either through stdin or as a prompt argument when using --print`
# before the session starts. Cost one paid run to find; the `=` form and a
# `--` separator both fix it, and this one is a single token.
ALLOWED_TOOLS_ARG = "--allowedTools=Monitor,Bash,TaskList,TaskStop"
MAX_TURNS = "8"


def die(reason: str) -> None:
    """A missing prerequisite: exit 2 with one line, never a skip."""
    print(f"probe-cc: {reason}", file=sys.stderr)
    raise SystemExit(2)


def redact(text: str) -> str:
    """Strip the operator's `$HOME` out of anything destined for a PR paste.

    The assertion target is all main-conversation assistant prose, not one
    field, and model narration quotes `$HOME`-rooted paths
    (`~/.claude/data/hubbub/venv`, `~/.claude/skills/talk`) readily. This repo
    is public.
    """
    home = str(Path.home())
    return text.replace(home, "~") if home and home != "/" else text


def build_ruler() -> str:
    """A 0-based sentinel ruler: `|NNNN|` occupies characters NNNN..NNNN+5.

    `|0000|` at 0-5, `|0010|` at 10-15, … `|0990|` at 990-995, four filler
    characters between each. A 279-character body (characters 0..278)
    therefore ends with `|0270|` complete, `|0280|` absent, and three filler
    characters at the cut — so "the last complete sentinel" has exactly one
    right answer.
    """
    ruler = "".join(f"|{i:04d}|...." for i in range(0, RULER_LEN, 10))
    assert len(ruler) == RULER_LEN, len(ruler)
    assert ruler[270:276] == LAST_SENTINEL, ruler[270:276]
    assert ruler[:BODY_UNITS].endswith("|0270|..."), ruler[:BODY_UNITS][-12:]
    assert ABSENT_SENTINEL not in ruler[:BODY_UNITS]
    return ruler


def verify_arithmetic() -> None:
    """Re-derive 220 / 279 / 500 from the shipped constants, before spending
    anything.

    The sentinel assertion is only meaningful while `|0270|` really is the
    last complete marker inside the delivered body, and that follows from
    `NOTIFICATION_CLIP`, `STDOUT_CAP` and the header's rendered width. If any
    of them moves, this says so in a line that costs no credential, instead of
    reporting a false `FAIL … sentinel not reported` after two paid runs.
    """
    header = (f'[hubbub msg={"0" * 8} from="{SENDER_NAME}" sid={"0" * 8} '
              f'"{SENDER_LABEL}" truncated={RULER_LEN}]')
    header_units = shared.utf16_len(header)
    budget = min(shared.STDOUT_CAP,
                 max(0, shared.NOTIFICATION_CLIP - shared.utf16_len(header + " ")))
    if header_units != 220 or budget != BODY_UNITS:
        die(f"the header arithmetic moved: header {header_units} units "
            f"(expected 220), body budget {budget} (expected {BODY_UNITS}). "
            f"Re-derive LAST_SENTINEL before running.")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def bootstrap_interpreter() -> str:
    """The interpreter the session's `Monitor()` shell will actually end up on.

    Mirrors `client.py:11-23` rather than guessing: inside `claude -p` that
    shell runs a bare `python3` under the operator's `$HOME`, and the
    bootstrap re-execs into `~/.claude/data/hubbub/venv/bin/python` when it
    exists (falling back to the pre-rename `inter-session` one). Checking the
    wrong interpreter here would let the run fail for a reason unrelated to
    delivery — the session would print `dependencies missing` and the case
    would look like a delivery failure.
    """
    data = Path.home() / ".claude" / "data"
    for candidate in (data / "hubbub" / "venv" / "bin" / "python",
                      data / "inter-session" / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("python3") or ""


def check_preconditions(cases: list[str]) -> None:
    if shutil.which("claude") is None:
        die("claude is not on PATH")
    python = bootstrap_interpreter()
    probe = subprocess.run([python, "-c", "import websockets, psutil"],
                           capture_output=True) if python else None
    if probe is None or probe.returncode != 0:
        die("runtime deps missing — run /hubbub:talk install-deps")
    if "standalone" in cases:
        # Checked up front rather than at the case, so a missing standalone
        # install costs no credential: the ticket puts this check in step 5,
        # but spending a paid run and *then* exiting 2 is strictly worse.
        link = Path.home() / ".claude" / "skills" / "talk"
        if not link.exists() or link.resolve() != (REPO / "skills" / "talk").resolve():
            die("standalone skill not installed at ~/.claude/skills/talk")


def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except OSError:
            pass
        time.sleep(interval)
    return False


def parse_stream(path: Path) -> tuple[str, dict | None]:
    """Return (assertion target, the trailing `result` object).

    The target is the concatenated `text` blocks of every `assistant` message
    whose `parent_tool_use_id` is null — main conversation, subagent messages
    excluded — plus the `result` line's own `result` string.

    Never `result` alone. It holds only the *final* assistant text, and a
    watch-driven run has more than one assistant turn (the model answers the
    connect, then the delivery, then the disconnect), so a report given one
    turn early would vanish from `result` and print a header-mismatch FAIL for
    an intact header. The prompt order puts the report in `result` on the
    common path; the concatenation makes the assertion hold when it does not.
    """
    chunks: list[str] = []
    result: dict | None = None
    if not path.exists():
        return "", None
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "assistant" and obj.get("parent_tool_use_id") is None:
            for block in obj.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
        elif obj.get("type") == "result":
            result = obj
    if result is not None:
        # `.get(..., "")`, not `[...]`: an error-subtype result line (a spent
        # turn budget, for one) may carry no `result` string at all.
        chunks.append(result.get("result") or "")
    return "\n".join(chunks), result


def run_case(case: str, skill: str, name: str, tmpdir: Path, port: int,
             sender_env: dict, ruler: str, backstop: bool) -> list[str]:
    """Run one case and return its verdict lines (`PASS …` / `FAIL …`)."""
    fails: list[str] = []
    stream_path = tmpdir / f"{case}.stream.jsonl"

    env = dict(os.environ)
    env["HUBBUB_DATA_DIR"] = str(tmpdir)
    env["HUBBUB_PORT"] = str(port)
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = str(BG_WAIT_CEILING_MS)
    # Never exported to `claude`: the in-session monitor would key on the
    # override instead of the `claude` pid, collide with the sender's own
    # `clients/<n>.lock`, and the registration wait below could never complete.
    env.pop("HUBBUB_PPID_OVERRIDE", None)
    env.pop("INTER_SESSION_PPID_OVERRIDE", None)

    argv = ["claude", "-p"]
    if case == "plugin-dir":
        argv += ["--plugin-dir", str(REPO)]
    argv += ["--max-turns", MAX_TURNS,
             "--output-format", "stream-json", "--verbose",
             ALLOWED_TOOLS_ARG,
             PROMPT.format(skill=skill, name=name,
                           present=SUFFIX_PRESENT, absent=SUFFIX_ABSENT)]

    print(f"  running {case}: {redact(' '.join(argv[:6]))} …", flush=True)
    proc = None
    # stdout and stderr go to FILES, not PIPEs. `proc.wait()` on a PIPE'd
    # stdout is the documented subprocess deadlock, and `--verbose stream-json`
    # makes it likely rather than latent: the child emits the system init plus
    # every message *during* the run, 64 KiB is easily reached, and the
    # resulting hang would print this script's own `did not exit within 180 s`
    # line — exactly the misdiagnosis the FAIL taxonomy exists to prevent. The
    # same argument applies to a stderr nobody drains, so it gets a file too.
    err_path = tmpdir / f"{case}.stderr.txt"
    with open(stream_path, "w") as stream, open(err_path, "w") as errfile:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=stream, stderr=errfile,
                                    text=True, cwd=str(REPO), env=env)

            session_file = tmpdir / "clients" / f"{proc.pid}.session"

            def registered() -> bool:
                if not session_file.exists():
                    return False
                return json.loads(session_file.read_text()).get("name") == name

            def settled() -> bool:
                # Short-circuit on a dead child. `claude` exiting before the
                # monitor registers is a real outcome — a rejected argv, a
                # refused credential, a model that answered without calling
                # `Monitor()` — and sitting out the full window for a process
                # that is already gone turns a two-second diagnosis into a
                # two-and-a-half-minute one. The FAIL line is the same; only
                # the waiting is wasted.
                return registered() or proc.poll() is not None

            if not wait_for(settled, REGISTER_TIMEOUT) or not registered():
                # Its own FAIL line: "the monitor never started in the child"
                # is this probe's most likely real failure, and without this
                # it would fall through to the send and surface as the 180 s
                # timeout, which reads as "claude did not exit" instead.
                stray = _stray_session(tmpdir, name, proc.pid)
                why = (f"claude exited {proc.poll()} first"
                       if proc.poll() is not None
                       else f"after {REGISTER_TIMEOUT:.0f} s")
                fails.append(
                    f"FAIL {case}: {name} never registered (no "
                    f"clients/{proc.pid}.session; {why}){stray}")
                # End the run and flush, *then* say what the session was
                # doing. `claude` block-buffers a non-tty stdout, so the
                # stream file is nearly empty while it runs — reading it
                # before the child exits shows session startup and nothing
                # else, which is how this failure first read as "stuck in a
                # SessionStart hook" when it was not.
                _end_child(proc)
                _diagnose(case, stream_path, err_path)
                return fails

            _record_monitor_env(case, session_file)

            send = subprocess.run(
                [sys.executable, str(BIN_DIR / "send.py"),
                 "--to", name, "--text", ruler],
                capture_output=True, text=True, env=sender_env, timeout=30)
            if send.returncode != 0:
                fails.append(f"FAIL {case}: send.py failed: "
                             f"{redact(send.stderr.strip())}")
                return fails
            sent_at = time.monotonic()

            # The run ends by itself: the prompt's TaskStop ends the monitor,
            # the watch ends, the model writes its report, CC emits `result`
            # and exits 0. The backstop covers a model that skipped the
            # disconnect — SIGTERM to the *listener*, never to `claude`, which
            # would exit 143 with no `result` at all.
            rc = None
            backstopped = False
            while True:
                try:
                    rc = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    pass
                elapsed = time.monotonic() - sent_at
                if elapsed > WAIT_AFTER_SEND:
                    break
                if backstop and not backstopped and elapsed > BACKSTOP_AFTER_SEND:
                    backstopped = True
                    _sigterm_listener(session_file)

            if rc is None:
                # Its own line, distinct from every assertion on the output.
                # The 240 s ceiling sits behind this wait on purpose, so this
                # line — not CC's dropped partial result — is what a hung run
                # produces.
                fails.append(f"FAIL {case}: claude did not exit within "
                             f"{WAIT_AFTER_SEND:.0f} s of the send")
                _end_child(proc)
                _diagnose(case, stream_path, err_path)
                return fails
        finally:
            # Only now: every path that reaches here has already reported a
            # FAIL, so there is no result left to protect.
            _end_child(proc)

    target, result = parse_stream(stream_path)
    fails.extend(_assert_case(case, target, result, rc, backstopped))
    if not fails:
        return [f"PASS {case}: header intact, 500 units delivered"]
    return fails


def _end_child(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _diagnose(case: str, stream_path: Path, err_path: Path) -> None:
    """Say what the session was actually doing when a FAIL is not about the
    delivered line.

    Without this the registration FAIL is a dead end: the whole point of the
    line is to distinguish "the session never joined the bus" from "`claude`
    did not exit", and neither is actionable without knowing which tools the
    turn budget went on.
    """
    target, result = parse_stream(stream_path)
    tools: list[str] = []
    if stream_path.exists():
        for line in stream_path.read_text(errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "assistant":
                for block in obj.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools.append(block.get("name", "?"))
    print(f"  diag {case}: tool calls = {tools or '(none)'}", flush=True)
    if result is not None:
        print(f"  diag {case}: result subtype={result.get('subtype')} "
              f"turns={result.get('num_turns')} "
              f"denials={result.get('permission_denials')}", flush=True)
    if target.strip():
        print(f"  diag {case}: last assistant text = "
              f"{redact(target.strip()[-500:])!r}", flush=True)
    try:
        err = err_path.read_text(errors="replace").strip()
    except OSError:
        err = ""
    if err:
        print(f"  diag {case}: stderr tail = {redact(err[-400:])!r}", flush=True)
    print(f"  diag {case}: full stream at {stream_path.name} "
          f"(kept, see the path printed at the end)", flush=True)


def _record_monitor_env(case: str, session_file: Path) -> None:
    """Record which `CLAUDE_*` names the in-session monitor actually received.

    **Read what this measures, and what it does not.** This is the monitor the
    *agent* started with `Monitor()` from SKILL.md's connect step, so it
    inherits the environment of a `Monitor()` shell — the route the skill
    actually uses. It is **not** the plugin-monitor route from
    `monitors/monitors.json`, which CC runs only in interactive sessions and
    which `docs/guides/release-checklist.md` exists to measure by hand.

    Recorded rather than asserted. CLAUDE.md's dated `2.1.233` measurement
    says a live monitor had no `CLAUDE_PLUGIN_*` at all; the current
    plugins-reference says monitors receive `CLAUDE_PLUGIN_ROOT`,
    `CLAUDE_PLUGIN_DATA` and `CLAUDE_PROJECT_DIR`. The two disagree, the
    answer decides whether #28 has a config-file bridge available, and this is
    one of the two places it can be observed at all. Names only, with the
    three plugin-path values `$HOME`-redacted: an environment dump belongs in
    nobody's PR.
    """
    if not sys.platform.startswith("linux"):
        print(f"  env {case}: /proc unavailable on {sys.platform}; "
              f"see docs/guides/release-checklist.md", flush=True)
        return
    try:
        pid = int(json.loads(session_file.read_text())["listener_pid"])
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"  env {case}: could not read the monitor's environ ({e})",
              flush=True)
        return
    pairs = [entry.split("=", 1) for entry in raw.split("\0") if "=" in entry]
    claude = {k: v for k, v in pairs if k.startswith("CLAUDE_")}
    print(f"  env {case}: {len(pairs)} variables, "
          f"{len(claude)} of them CLAUDE_*", flush=True)
    if not claude:
        print("  env: no CLAUDE_* names at all", flush=True)
    for key in sorted(claude):
        shown = (f" = {redact(claude[key])}"
                 if key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA",
                            "CLAUDE_PROJECT_DIR") else "")
        # Marked, because it changes what the name means. Running this script
        # from inside a Claude Code session leaks that session's own CLAUDE_*
        # names down the whole chain, and an unmarked list would read as "CC
        # gave these to the monitor" when the honest reading is "these were
        # already here". Only an `(inherited)`-free name is evidence about
        # what CC exports.
        inherited = " (inherited from this shell)" if key in os.environ else ""
        print(f"  env: {key}{shown}{inherited}", flush=True)


def _stray_session(tmpdir: Path, name: str, claude_pid: int) -> str:
    """If the monitor registered under some *other* key, say which.

    The state file is keyed by the CC *ancestor* pid, and inside `claude -p`
    that ancestor should be the `claude` child this script spawned — so the
    file appearing under exactly that pid is `find_cc_ancestor_pid` observed
    directly. When it lands elsewhere that is a finding about the walk, not a
    delivery failure, and a bare timeout would hide it.
    """
    clients = tmpdir / "clients"
    if not clients.is_dir():
        return "; no clients/ directory at all"
    for path in clients.glob("*.session"):
        try:
            if json.loads(path.read_text()).get("name") == name:
                return (f"; it registered as {path.name} instead — "
                        f"find_cc_ancestor_pid did not land on {claude_pid}")
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def _sigterm_listener(session_file: Path) -> None:
    try:
        pid = int(json.loads(session_file.read_text())["listener_pid"])
        os.kill(pid, signal.SIGTERM)
        print("  backstop: SIGTERM to the in-session monitor "
              f"(listener_pid={pid})", flush=True)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass


def _assert_case(case, target, result, rc, backstopped) -> list[str]:
    fails = []
    if result is None:
        return [f"FAIL {case}: no result (exit {rc})"]
    denials = result.get("permission_denials") or []
    if denials:
        names = ", ".join(
            sorted({d.get("tool_name", "?") for d in denials
                    if isinstance(d, dict)})) or str(denials)
        fails.append(f"FAIL {case}: permission denied: {names}")
    if result.get("subtype") == "error_max_turns":
        # Its own line. Without it a spent budget surfaces as a header
        # mismatch — the reading reserved for a damaged SEC-002 marker.
        fails.append(f"FAIL {case}: turn budget exhausted before the report")
    if fails:
        return fails

    if not HEADER_RE.search(target):
        fails.append(f"FAIL {case}: header not reported intact "
                     f"(SEC-002 authority marker); see the stream")
    if LAST_SENTINEL not in target:
        fails.append(f"FAIL {case}: sentinel {LAST_SENTINEL} not reported")
    elif ABSENT_SENTINEL in target:
        fails.append(f"FAIL {case}: {ABSENT_SENTINEL} reported — more than "
                     f"{BODY_UNITS} characters arrived, or messages.log was read")
    if SUFFIX_PRESENT in target:
        fails.append(f"FAIL {case}: a truncation suffix was reported — "
                     f"Claude Code clipped the line below {BODY_UNITS + 221} "
                     f"units")
    elif SUFFIX_ABSENT not in target:
        # Unanswered is its own outcome, never silently a pass: the whole
        # point of the 500-unit measurement is item 3.
        fails.append(f"FAIL {case}: item 3 unanswered — neither "
                     f"{SUFFIX_ABSENT} nor {SUFFIX_PRESENT} in the report")
    # Recorded for #43, never asserted: whether the header line and the `cont`
    # pointer batch into one notification or arrive as two is that ticket's
    # question, and the answer is allowed to differ by CC version.
    print(f"  note {case}: cont line "
          f"{'was' if 'cont]' in target else 'was NOT'} reported alongside "
          f"the header (#43, observation only)", flush=True)
    if rc != 0:
        fails.append(f"FAIL {case}: claude exited {rc}")
    if backstopped:
        # Not a failure: the run was bounded as designed. Worth saying,
        # because it means the model did not end its own watch.
        print(f"  note {case}: ended via the listener backstop, not the "
              f"prompt's TaskStop", flush=True)
    return fails


def start_sender(tmpdir: Path, port: int) -> tuple[subprocess.Popen, dict]:
    ppid = 990001
    env = dict(os.environ)
    env.update({
        "HUBBUB_DATA_DIR": str(tmpdir),
        "HUBBUB_PORT": str(port),
        # In this dict only — see the note in run_case().
        "HUBBUB_PPID_OVERRIDE": str(ppid),
    })
    proc = subprocess.Popen(
        [sys.executable, str(BIN_DIR / "client.py"),
         "--name", SENDER_NAME, "--label", SENDER_LABEL],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, cwd=str(tmpdir), env=env)
    session = tmpdir / "clients" / f"{ppid}.session"
    if not wait_for(lambda: session.exists()
                    and json.loads(session.read_text()).get("session_id"), 30.0):
        proc.terminate()
        die("the probe's own sender never registered — is the bus port free?")
    return proc, env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", choices=("plugin-dir", "standalone", "all"),
                    default="all")
    ap.add_argument("--no-backstop", action="store_true",
                    help="Do not SIGTERM the in-session monitor 120 s after "
                         "the send. Exists only so the timeout FAIL line can "
                         "be exercised by hand; the Makefile never passes it.")
    args = ap.parse_args()

    cases = (["plugin-dir", "standalone"] if args.case == "all"
             else [args.case])
    check_preconditions(cases)
    verify_arithmetic()

    print(f"probe-cc: {len(cases)} case(s): {', '.join(cases)}")
    print("  expected wall time: 40-90 s per case when the model ends its own "
          "watch, 120-180 s via the backstop.")
    print("  this spends the operator's Claude credential, one session per "
          "case. Never run it unattended.")

    ruler = build_ruler()
    port = free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="probe-cc-"))
    sender = None
    lines: list[str] = []
    try:
        sender, sender_env = start_sender(tmpdir, port)
        for case in cases:
            skill, name = (("hubbub:talk", "probe-b") if case == "plugin-dir"
                           else ("talk", "probe-s"))
            lines.extend(run_case(case, skill, name, tmpdir, port, sender_env,
                                  ruler, backstop=not args.no_backstop))
    finally:
        if sender is not None:
            sender.terminate()
            try:
                sender.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sender.kill()
        for pid_path in tmpdir.glob("server.*.pid"):
            try:
                os.kill(int(pid_path.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        streams = sorted(tmpdir.glob("*.stream.jsonl"))
        # A fresh 0700 directory per run, never a fixed name under the shared
        # temp dir: these streams are the model's raw output, unredacted (only
        # the printed summary goes through redact()), and a predictable path is
        # both world-readable by default and pre-creatable by another local user
        # as a symlink we would then copy through.
        kept = None
        if streams:
            kept = Path(tempfile.mkdtemp(prefix="probe-cc-streams-"))
            for s in streams:
                shutil.copy2(s, kept / s.name)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if streams:
            print(f"\nstreams kept for inspection: {kept}")

    print()
    for line in lines:
        print(redact(line))
    return 1 if any(line.startswith("FAIL") for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
