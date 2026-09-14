# Label taxonomy

Labels do two jobs: organise the tracker, and route `ticket-gate` (plugin-registered,
`forge-kit-governance`). The gate **blocks** a ticket with no area label, warns on a missing
type label, and adds its security lens on `security` or `critical`.

`.github/labels.yml` is the one declaration. Apply it with forge-kit's `sync-labels.sh`
(shipped in the `forge-host` skill of `forge-kit-devops`), never by hand — it creates what is
missing, updates what drifted, and never deletes:

```bash
S=$(ls -d ~/.claude/plugins/cache/forge-kit/forge-kit-devops/*/skills/forge-host/assets | sort -V | tail -1)
bash "$S/sync-labels.sh" --labels .github/labels.yml            # apply
bash "$S/sync-labels.sh" --labels .github/labels.yml --check    # report drift, change nothing
```

## Type labels

| Label | Use for |
|---|---|
| `bug` | Something isn't working |
| `enhancement` | New feature or request |
| `security` | Vulnerability or hardening — adds the security lens to the gate |
| `infrastructure` | Makefile, venvs, CI, plugin packaging |
| `documentation` | Docs updates or additions |
| `testing` | Tests, coverage, the test harness |

## Area labels — at least one per work ticket

These are the five names from the gate's built-in area set that describe surfaces this project
actually has. **Do not add project-specific area names** (`client`, `protocol`, …): the gate's
mechanical check tests labels against its built-in set and is not handed a project one, so a
custom area passes Step 0b and then fails check 2 on every ticket. What each built-in name
means *here*:

| Label | Means in this codebase | Typical files |
|---|---|---|
| `api` | The wire protocol — ops, frame shapes, caps, rate limits, error codes — and the CLI / stdout contracts, including the `[hubbub …]` notification prefix | `server.py::_handle_*`, `send.py`, `list.py`, `relabel.py`, `doctor.py`, `client.py::_format_msg` |
| `backend` | The long-lived processes and their state: server lifecycle, the monitor's reconnect loop, the election, the data-dir layout and its migration | `server.py`, `client.py`, `spawn.py`, `shared.py`, `discover.py`, `profile.py` |
| `components` | The Claude Code layer: the reaction policy, plugin manifests, `monitors.json`, auto-start, how a session joins the bus | `skills/talk/SKILL.md`, `.claude-plugin/*.json`, `monitors/monitors.json`, `auto_start.py` |
| `tooling` | The guards, scripts and CI that enforce the rules | `Makefile`, `tests/`, `.coveragerc`, `.github/workflows/`, `scripts/` |
| `governance` | Templates, labels, ticket standards, the roadmap, and the docs that carry them — `README.md` included | `.github/ISSUE_TEMPLATE/`, `docs/guides/`, `README.md`, `CLAUDE.md` |

Not declared, on purpose: `web`, `mobile`, `database` (no such surfaces) and `privacy` (no
personal-data processing; the `privacy-regime` lens is deliberately not installed — see rule 4
of `ticket-standards.md`). If a ticket ever needs one, declare it in `labels.yml` first.

## Priority labels

| Label | Meaning |
|---|---|
| `P0` | Critical — blocks a release, or a live bus is losing or misdelivering messages |
| `P1` | High — important for the current milestone |
| `P2` | Medium — should do, not blocking |
| `P3` | Low — nice to have |

## Special labels

| Label | Effect |
|---|---|
| `critical` | Adds the security lens and puts the critic in maximum scrutiny |
