"""Tests for the shared ``reject_option`` argument guard.

``reject_option`` is the single home for the option-injection guard formerly
duplicated in the git client and the ``gh`` CLI wrapper. These tests pin its
contract: safe values pass through unchanged, and an option-like value (one
starting with ``-``) raises a 400 ``invalid_argument`` error whose message
names the field.
"""

from __future__ import annotations

import pytest

from git_automation.core.errors import GitAutomationError
from git_automation.core.validation import reject_option


def test_returns_value_unchanged_for_normal_string() -> None:
    assert reject_option("origin", "remote") == "origin"


def test_returns_value_unchanged_for_slash_ref() -> None:
    assert reject_option("origin/feature", "remote ref") == "origin/feature"


def test_rejects_option_like_value() -> None:
    with pytest.raises(GitAutomationError) as exc:
        reject_option("--upload-pack=evil", "remote")
    assert exc.value.code == "invalid_argument"
    assert exc.value.status_code == 400


def test_error_message_contains_label() -> None:
    with pytest.raises(GitAutomationError) as exc:
        reject_option("-x", "branch name")
    assert "branch name" in exc.value.message


def test_error_message_is_exact() -> None:
    with pytest.raises(GitAutomationError) as exc:
        reject_option("-x", "commit")
    assert exc.value.message == "Invalid commit: must not start with '-'."
