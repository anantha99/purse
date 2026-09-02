"""Bundled seed artifacts parse and load — no database (C5.4).

Proves the shipped ``purse-save-policy`` document is a valid skill and that the
loader reads its name from the frontmatter, without needing a vault.
"""

from __future__ import annotations

from purse.skills.parse import parse_skill
from purse.skills.seed import bundled_seed_skills


def test_bundled_seed_skills_includes_a_valid_save_policy() -> None:
    seeds = dict(bundled_seed_skills())
    assert "purse-save-policy" in seeds

    parsed = parse_skill(seeds["purse-save-policy"])
    assert parsed.name == "purse-save-policy"
    assert parsed.version == "1.0.0"
    assert parsed.description
    assert parsed.body.strip()


def test_every_bundled_seed_parses_and_its_name_matches_frontmatter() -> None:
    for name, content in bundled_seed_skills():
        assert parse_skill(content).name == name
