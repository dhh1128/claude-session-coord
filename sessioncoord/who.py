"""Who the human is, as sessions should name them in what they print.

The package was written for one person and said his name in every refusal and remedy. Set
`SESSION_COORD_USER` in the `env` block of Claude Code's settings.json so hooks and the Bash
tool both see it; unset, everything says "the user".
"""

import os


def user() -> str:
    return os.environ.get("SESSION_COORD_USER", "").strip() or "the user"
