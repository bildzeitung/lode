"""Tests for lode.redact — redact-before-index / redact-before-egress (lode-fk8.2).

Asserts the acceptance criteria: known secret patterns from the high-precision
seed set are stripped from egress payloads (before-egress) and kept out of the
index (before-index), while ordinary prose passes through untouched (precision).
"""

import pytest
from pydantic import ValidationError

from lode.config import Settings, load_settings
from lode.redact import (
    REDACTION_MARKER,
    redact,
    redact_before_egress,
    redact_before_egress_counting,
    redact_before_index,
    redact_counting,
)

# One representative live secret per seed pattern (synthetic, not real creds).
SEED_SECRETS = [
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "ghp_" + "a" * 36,
    "gho_" + "b" * 36,
    "github_pat_" + "C" * 82,
    "xoxb-1234567890-abcdEFGHijklMNOP",
    "sk_live_" + "0" * 24,
    "rk_live_" + "Z" * 24,
    "AIza" + "a" * 35,
    "sk-ant-api03-" + "x" * 20,
]

# High-precision means these must NOT trip the seed set (no false positives).
INNOCENT_PROSE = [
    "The deploy went fine; rerun the migration tomorrow.",
    "Meeting notes: discuss AKIA naming convention for buckets.",
    "ghp_ is the GitHub classic token prefix (just three chars here).",
    "We use Slack; ping me on xox.",
    "email me at sam@example.com about the sk_live rollout plan",
]


@pytest.mark.parametrize("secret", SEED_SECRETS)
def test_each_seed_secret_is_redacted_before_egress(secret: str) -> None:
    out = redact_before_egress(f"key: {secret} done")
    assert secret not in out
    assert REDACTION_MARKER in out


@pytest.mark.parametrize("secret", SEED_SECRETS)
def test_each_seed_secret_is_redacted_before_index(secret: str) -> None:
    out = redact_before_index(f"key: {secret} done")
    assert secret not in out
    assert REDACTION_MARKER in out


@pytest.mark.parametrize("prose", INNOCENT_PROSE)
def test_innocent_prose_passes_through_unchanged(prose: str) -> None:
    assert redact_before_egress(prose) == prose
    assert redact_before_index(prose) == prose


def test_multiple_secrets_in_one_payload_all_redacted() -> None:
    text = "aws=AKIAIOSFODNN7EXAMPLE\ngh=ghp_" + "z" * 36 + "\ntrailing prose stays"
    out = redact_before_egress(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_" + "z" * 36 not in out
    assert "trailing prose stays" in out
    assert out.count(REDACTION_MARKER) == 2


def test_redaction_is_idempotent() -> None:
    text = "secret AKIAIOSFODNN7EXAMPLE here"
    once = redact_before_egress(text)
    assert redact_before_egress(once) == once


def test_empty_pattern_set_returns_text_unchanged() -> None:
    text = "AKIAIOSFODNN7EXAMPLE stays because no patterns"
    assert redact(text, []) == text


def test_custom_settings_pattern_set_is_used() -> None:
    settings = Settings(
        redact_before_egress_patterns=[r"TOPSECRET-\d+"],
        redact_before_index_patterns=[],
    )
    assert (
        redact_before_egress("x TOPSECRET-42 y", settings) == f"x {REDACTION_MARKER} y"
    )
    # Index set is empty here, so an egress-only pattern is left intact for index.
    assert redact_before_index("x TOPSECRET-42 y", settings) == "x TOPSECRET-42 y"


def test_index_and_egress_seeds_match_by_default() -> None:
    # Both controls ship seeded identically (docs/configuration.md).
    s = load_settings()
    assert s.redact_before_index_patterns == s.redact_before_egress_patterns


def test_invalid_redaction_regex_fails_at_config_load() -> None:
    with pytest.raises(ValidationError):
        load_settings(redact_before_egress_patterns=["(unbalanced"])


# --- counting twins (lode-az0.4): the egress audit log records how many spans
# were stripped per sent target, so redaction must report a count, not just text.


def test_redact_counting_reports_text_and_count() -> None:
    text = "a AKIAIOSFODNN7EXAMPLE b AKIAIOSFODNN7EXAMPLE c"
    out, count = redact_counting(text, [r"AKIA[0-9A-Z]{16}"])
    assert count == 2
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert out.count(REDACTION_MARKER) == 2


def test_redact_counting_zero_when_nothing_matches() -> None:
    out, count = redact_counting("clean prose", [r"AKIA[0-9A-Z]{16}"])
    assert (out, count) == ("clean prose", 0)


def test_redact_delegates_to_counting() -> None:
    # redact() is the text-only twin of redact_counting() — same substitution.
    patterns = [r"AKIA[0-9A-Z]{16}"]
    text = "key AKIAIOSFODNN7EXAMPLE end"
    assert redact(text, patterns) == redact_counting(text, patterns)[0]


def test_redact_before_egress_counting_uses_egress_pattern_set() -> None:
    settings = Settings(
        redact_before_egress_patterns=[r"TOPSECRET-\d+"],
        redact_before_index_patterns=[],
    )
    out, count = redact_before_egress_counting("x TOPSECRET-42 y", settings)
    assert (out, count) == (f"x {REDACTION_MARKER} y", 1)
    assert redact_before_egress_counting("clean", settings) == ("clean", 0)
