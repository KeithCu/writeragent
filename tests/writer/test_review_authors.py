# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import threading
from typing import Any
import pytest

from plugin.writer import review_authors


class FakeAuthorAccess:
    def __init__(
        self,
        store: dict[str, str],
        commits: list[dict[str, str]] | None = None,
        fail_commit_value: str | None = None,
    ) -> None:
        self.store = store
        self.staged = dict(store)
        self.commits = commits if commits is not None else []
        self.fail_commit_value = fail_commit_value

    def getPropertyValue(self, name: str) -> str:  # noqa: N802 -- UNO API
        return self.store.get(name, "")

    def setPropertyValue(self, name: str, value: Any) -> None:  # noqa: N802 -- UNO API
        self.staged[name] = str(value)

    def commitChanges(self) -> None:  # noqa: N802 -- UNO API
        if self.fail_commit_value is not None and self.staged.get("givenname") == self.fail_commit_value:
            raise RuntimeError("commit failed")
        self.store.clear()
        self.store.update(self.staged)
        self.commits.append(dict(self.store))


@pytest.fixture
def clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_authors, "_state", threading.local())


@pytest.fixture
def fake_profile(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[dict[str, str]]]:
    store = {"givenname": "OriginalGiven", "sn": "OriginalSn"}
    commits: list[dict[str, str]] = []

    def _fake_access(_ctx: Any) -> FakeAuthorAccess:
        return FakeAuthorAccess(store, commits)

    monkeypatch.setattr(review_authors, "_author_access", _fake_access)
    return store, commits


def test_begin_and_end_normal_cycle(clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]) -> None:
    store, commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx)
    assert prior == ("OriginalGiven", "OriginalSn")
    assert store["givenname"] == "WriterAgent"
    assert store["sn"] == ""
    assert getattr(review_authors._state, "last", None) == "WriterAgent"

    review_authors.end(ctx, prior)
    assert store["givenname"] == "OriginalGiven"
    assert store["sn"] == "OriginalSn"
    assert getattr(review_authors._state, "last", None) is None


def test_custom_insert_and_delete_authors(clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]) -> None:
    store, _commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx, insert_author="CustomInsert", delete_author="CustomDelete")
    assert prior == ("OriginalGiven", "OriginalSn")
    assert store["givenname"] == "CustomInsert"

    with review_authors.deletion_author():
        assert store["givenname"] == "CustomDelete"

    assert store["givenname"] == "CustomInsert"
    review_authors.end(ctx, prior)
    assert store["givenname"] == "OriginalGiven"


def test_deletion_author_swaps_and_restores(clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]) -> None:
    store, _commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx)
    assert store["givenname"] == "WriterAgent"

    with review_authors.deletion_author():
        assert store["givenname"] == "WriterAgent (deletions)"
        assert getattr(review_authors._state, "last", None) == "WriterAgent (deletions)"

    assert store["givenname"] == "WriterAgent"
    assert getattr(review_authors._state, "last", None) == "WriterAgent"

    review_authors.end(ctx, prior)


def test_begin_failed_returns_none_and_deletion_author_no_writes(
    clean_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _failing_access(_ctx: Any) -> Any:
        raise RuntimeError("cannot read profile")

    monkeypatch.setattr(review_authors, "_author_access", _failing_access)

    ctx = object()
    prior = review_authors.begin(ctx)
    assert prior is None
    assert getattr(review_authors._state, "ctx", None) is None

    # deletion_author should be inert (no swaps, runs body)
    ran = False
    with review_authors.deletion_author():
        ran = True
    assert ran


def test_deletion_author_failed_swap_runs_body_and_does_not_restore(
    clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, commits = fake_profile
    ctx = object()

    review_authors.begin(ctx)
    initial_commits = len(commits)

    # Monkeypatch _set_office_author to fail during deletion_author
    call_count = 0

    def _failing_set(c: Any, given: str) -> bool:
        nonlocal call_count
        call_count += 1
        return False

    monkeypatch.setattr(review_authors, "_set_office_author", _failing_set)

    ran = False
    with review_authors.deletion_author():
        ran = True
    assert ran
    # _set_office_author was called once for delete, returned False, so body yielded and finally was not reached
    assert call_count == 1
    assert len(commits) == initial_commits


def test_deletion_author_failed_restore_is_swallowed(
    clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _commits = fake_profile
    ctx = object()

    review_authors.begin(ctx)

    def _failing_restore_access(c: Any) -> Any:
        return FakeAuthorAccess(store, fail_commit_value="WriterAgent")

    monkeypatch.setattr(review_authors, "_author_access", _failing_restore_access)

    ran = False
    with review_authors.deletion_author():
        ran = True
        assert store["givenname"] == "WriterAgent (deletions)"
    assert ran
    # Restore failed and was caught by _set_office_author, so store is still deletions
    assert store["givenname"] == "WriterAgent (deletions)"


def test_end_none_prior_never_calls_author_access(clean_state: None, monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _failing_access(_ctx: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(review_authors, "_author_access", _failing_access)
    review_authors.end(object(), None)
    assert not called


def test_end_does_not_overwrite_external_author_change(
    clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]
) -> None:
    store, commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx)
    assert prior == ("OriginalGiven", "OriginalSn")
    assert store["givenname"] == "WriterAgent"

    # External change during edit
    store["givenname"] = "Alice"
    store["sn"] = "Smith"
    commit_count_before_end = len(commits)

    review_authors.end(ctx, prior)
    # No new commitChanges should have been made
    assert len(commits) == commit_count_before_end
    assert store["givenname"] == "Alice"
    assert store["sn"] == "Smith"


def test_end_restores_after_debug_hook_set_office_author(
    clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]
) -> None:
    store, _commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx)
    assert prior == ("OriginalGiven", "OriginalSn")

    # Debug hook writes "Test User"
    assert review_authors._set_office_author(ctx, "Test User") is True
    assert store["givenname"] == "Test User"
    assert getattr(review_authors._state, "last", None) == "Test User"

    # end() must recognize "Test User" as our own last write and restore prior
    review_authors.end(ctx, prior)
    assert store["givenname"] == "OriginalGiven"
    assert store["sn"] == "OriginalSn"


def test_end_current_equals_prior_does_no_write(
    clean_state: None, fake_profile: tuple[dict[str, str], list[dict[str, str]]]
) -> None:
    store, commits = fake_profile
    ctx = object()

    prior = review_authors.begin(ctx)
    # External process reverted to prior
    store["givenname"] = "OriginalGiven"
    store["sn"] = "OriginalSn"
    commit_count_before_end = len(commits)

    review_authors.end(ctx, prior)
    assert len(commits) == commit_count_before_end
