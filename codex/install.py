#!/usr/bin/env python3
"""Install the session-coord runtime, skill and Codex hooks without rewriting other config."""

import argparse
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "# BEGIN session-coord Codex hooks"
END = "# END session-coord Codex hooks"


def _toml_string(value: str) -> str:
    # JSON basic-string escapes are also TOML basic-string escapes, including newlines.
    # JSON leaves DEL literal; TOML forbids it in a basic string.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def hook_block(runtime: Path) -> str:
    hooks = runtime.expanduser().resolve() / "hooks"
    entries = (
        ("SessionStart", "session-coord-codex-register", None, 15),
        ("UserPromptSubmit", "session-coord-mail", None, 20),
        ("PreToolUse", "session-coord-gate", "", 15),
        ("SessionEnd", "session-coord-codex-end", None, 3),
    )
    lines = [START]
    for event, script, matcher, timeout in entries:
        lines += [f"[[hooks.{event}]]"]
        if matcher is not None:
            lines += [f"matcher = {_toml_string(matcher)}"]
        lines += [f"[[hooks.{event}.hooks]]", 'type = "command"',
                  f"command = {_toml_string(shlex.quote(str(hooks / script)))}",
                  f"timeout = {timeout}", ""]
    lines += [END]
    return "\n".join(lines) + "\n"


def install(config: Path, skills: Path, runtime: Path) -> None:
    runtime = runtime.expanduser().resolve()
    config.parent.mkdir(parents=True, exist_ok=True)
    old = config.read_text() if config.exists() else ""
    block = hook_block(runtime)
    if START in old or END in old:
        if old.count(START) != 1 or old.count(END) != 1 or old.index(START) > old.index(END):
            raise ValueError("config has an incomplete session-coord block; repair it before reinstalling")
        old = old[:old.index(START)] + old[old.index(END) + len(END):].lstrip("\n")
    new = old.rstrip() + ("\n\n" if old.strip() else "") + block
    runtime.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{runtime.name}-install-", dir=runtime.parent) as tmp:
        staged = Path(tmp) / "runtime"
        backup = Path(tmp) / "previous"
        if runtime.exists():
            shutil.copytree(runtime, staged)
        else:
            staged.mkdir()
        for part in ("sessioncoord", "bin", "hooks"):
            target = staged / part
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(ROOT / part, target,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        skill = skills / "session-coord"
        skill.mkdir(parents=True, exist_ok=True)
        for name in ("SKILL.md", "DESIGN.md"):
            shutil.copy2(ROOT / "skills" / "session-coord" / name, skill / name)
        had_runtime = runtime.exists()
        if had_runtime:
            os.replace(runtime, backup)
        try:
            os.replace(staged, runtime)
            if not config.exists() or config.read_text() != new:
                temporary = config.with_name(config.name + ".session-coord-tmp")
                temporary.write_text(new)
                os.replace(temporary, config)
        except Exception:
            if runtime.exists():
                os.replace(runtime, Path(tmp) / "failed")
            if had_runtime:
                os.replace(backup, runtime)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path.home() / ".codex"
    parser.add_argument("--config", type=Path, default=home / "config.toml")
    parser.add_argument("--skills", type=Path, default=home / "skills")
    parser.add_argument("--runtime", type=Path, default=home / "session-coord")
    args = parser.parse_args()
    install(args.config.expanduser(), args.skills.expanduser(), args.runtime.expanduser())
    print(f"Installed session-coord Codex hooks in {args.config}; review them with /hooks in Codex")


if __name__ == "__main__":
    main()
