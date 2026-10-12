"""session-coord — keep several Claude sessions in one repo from tangling.

Daniel routinely runs eight to fifteen sessions at once and several may sit in one repo. On
2026-09-17 four of them were sited in `acme/infra` and the result cost him an hour of
carrying prompts between tmux windows by hand. This package is the machinery that makes that
hour unnecessary; `skills/session-coord/DESIGN.md` holds the reasoning.

Pure logic in `model`/`liveness`/`detect`/`harvest`
over plain data, effects isolated in `store`/`tmuxio`/`gitio` so the suite needs neither a live
tmux server nor fifteen running sessions.
"""

__all__ = ["model", "liveness", "detect", "store", "gitio", "tmuxio", "harvest", "asks", "cli"]
