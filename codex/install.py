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
        ("SessionStart", "session-coord-codex-register", None, 15,
         "session-coord: register this session"),
        ("UserPromptSubmit", "session-coord-mail", None, 20,
         "session-coord: deliver peer mail"),
        ("PreToolUse", "session-coord-gate", "", 15,
         "session-coord: refuse writes in a shared tree"),
        ("SessionEnd", "session-coord-codex-end", None, 3,
         "session-coord: remove presence"),
    )
    lines = [START]
    for event, script, matcher, timeout, status_message in entries:
        lines += [f"[[hooks.{event}]]"]
        if matcher is not None:
            lines += [f"matcher = {_toml_string(matcher)}"]
        lines += [f"[[hooks.{event}.hooks]]", 'type = "command"',
                  f"command = {_toml_string(shlex.quote(str(hooks / script)))}",
                  f"timeout = {timeout}",
                  f"statusMessage = {_toml_string(status_message)}", ""]
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
        for part in ("sessioncoord", "bin", "hooks"):
            shutil.copytree(ROOT / part, staged / part,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for directory in (path for path in staged.rglob("*") if path.is_dir()):
            target_dir = runtime / directory.relative_to(staged)
            if target_dir.is_symlink():
                raise ValueError(f"runtime directory {target_dir} is a symlink; refusing to "
                                 "write through it")
        runtime.mkdir(parents=True, exist_ok=True)
        skill = skills / "session-coord"
        skill.mkdir(parents=True, exist_ok=True)
        for name in ("SKILL.md", "DESIGN.md"):
            shutil.copy2(ROOT / "skills" / "session-coord" / name, skill / name)
        # Replace files one at a time. An interrupted reinstall may leave mixed versions,
        # but no hook path disappears between two directory renames.
        for source in sorted(staged.rglob("*")):
            if source.is_file():
                target = runtime / source.relative_to(staged)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
        if not config.exists() or config.read_text() != new:
            temporary = config.with_name(config.name + ".session-coord-tmp")
            temporary.write_text(new)
            os.replace(temporary, config)


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
