"""Fixtures for the session-coord suite.

A naming constraint worth knowing before you add a file here. `tools` now carries two test
suites under one rootdir, and pytest's default import mode resolves a test module by its
BASENAME -- so `tests/sessioncoord/test_cli.py` and `tests/prmerge/test_cli.py` cannot both
exist, and `pytest tests` fails to collect with "import file mismatch". That is why this
suite's CLI tests live in `test_commands.py`. Keep new basenames distinct from `tests/prmerge`
until somebody sets `--import-mode=importlib` repo-wide, which would fix it properly but also
changes how the other suite's `from fakes import ...` resolves -- not a change to make behind
its author's back.


The sys.path insert is deliberate and belongs here rather than in ``pytest.ini``. The repo's
``pytest.ini`` currently says ``pythonpath = src``, which names a directory that has never
existed in this repo -- so ``prmerge`` and ``sessioncoord`` are importable only because
``python -m pytest`` happens to put the invocation directory on the path. Doing it here means
this suite passes under a bare ``pytest tests/sessioncoord`` too, instead of only under the one
invocation somebody happened to type. Reported to the session that owns ``pytest.ini``; when
that line becomes ``pythonpath = .`` this block is redundant and can go.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_tmux(monkeypatch):
    """`cli._live` reads the REAL tmux server unless replaced, so every test replaces it.

    Without this a test's labels would depend on which panes happen to exist on the box that
    runs it. None means tmux could not be asked, so stored windows stand; an empty table would
    mean no panes exist, which makes every record stale. A test about live windows sets its own.
    """
    from sessioncoord import cli
    monkeypatch.setattr(cli, "_live_table", lambda: None)
