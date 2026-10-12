"""`--repo` is a hint about which repo, not a literal state key.

Found by the session that built `tagged`, in its own code first: passing `--repo "$PWD"` from
inside a worktree keyed state on the WORKTREE rather than on the repository. The session then
registered on a private board of one, saw no peers, and was invisible to them — silent
isolation reached by using the tool exactly as documented, with no error and nothing on the
board to suggest anything was wrong.

That is the worst failure shape this package has: not a refusal, not a wrong answer, but a
confident empty board.
"""

from __future__ import annotations

import pytest
from sessioncoord import cli


class Args:
    def __init__(self, repo=None, repo_of=None):
        self.repo = repo
        self.repo_of = repo_of


def test_an_explicit_worktree_path_resolves_to_the_repository(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root",
                        lambda p, run=None: "/repo" if "/repo" in p else None)
    assert cli._resolve_repo(Args(repo="/repo/.worktrees/mine")) == "/repo"


def test_an_explicit_repo_root_is_unchanged(monkeypatch):
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: "/repo")
    assert cli._resolve_repo(Args(repo="/repo")) == "/repo"


def test_a_path_outside_any_repo_falls_back_to_itself(monkeypatch):
    # Tests pass synthetic roots like /repo that git has never heard of; resolving to None and
    # returning that would break every caller rather than isolating one.
    monkeypatch.setattr(cli.gitio, "repo_root", lambda p, run=None: None)
    assert cli._resolve_repo(Args(repo="/not/a/repo")) == "/not/a/repo"
