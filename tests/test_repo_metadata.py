"""Static checks on the repo metadata GitHub reads from `main` (#37).

`.github/FUNDING.yml` is what renders the Sponsor button in the repository
header, and the README's closing `## Sponsor` section is the link for anyone
reading the page instead. GitHub Sponsors is the single funding route, so the
YAML must carry exactly that one entry, and the section must stay last so it
sits after the useful content rather than interrupting it.

Text asserts, not parsed YAML: PyYAML is not a dependency and a two-line file
does not justify one. The tests pin the heading position and the link, not
the paragraph wording, so a copy edit does not fail CI.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text()


class TestFundingYml:
    def test_only_github_sponsors_entry(self):
        path = REPO / ".github" / "FUNDING.yml"
        assert path.exists(), ".github/FUNDING.yml is missing (#37)"
        lines = [line.strip() for line in path.read_text().splitlines()]
        entries = [line for line in lines if line and not line.startswith("#")]
        assert entries == ["github: agigante80"], (
            f"FUNDING.yml must contain only 'github: agigante80'; found {entries!r}"
        )


class TestReadmeSponsor:
    def test_sponsor_is_last_section(self):
        headings = [line for line in README.splitlines() if line.startswith("## ")]
        assert headings[-1] == "## Sponsor", (
            f"last README section is '{headings[-1]}', expected '## Sponsor'"
        )

    def test_sponsor_section_links_to_sponsors_page(self):
        assert "\n## Sponsor\n" in README, "README has no ## Sponsor section (#37)"
        body = README.split("\n## Sponsor\n", 1)[1]
        assert body.count("https://github.com/sponsors/agigante80") == 1, (
            "the Sponsor section must link https://github.com/sponsors/agigante80 "
            f"exactly once; found {body.count('https://github.com/sponsors/agigante80')}"
        )
