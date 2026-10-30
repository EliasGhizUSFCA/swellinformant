"""README.md names only commands and scripts that exist (spec section 33: exact commands).

* every `python -m app.cli ...` the README names parses: its command, its sub-command where
  the command has them, and every `--option` written after it are in that command's help;
* every `scripts/<file>` it names exists, and the shell scripts are executable;
* every `scripts/vercel-local.sh <verb>` is a verb the script handles;
* every `npm run <script>` (and `npm test`) is a script of frontend/package.json.

The same check runs over the setup documents the README sends the owner to
(docs/API_SETUP_REQUIRED.md, docs/deploy-vercel.md, docs/WINDY_API_SETUP.md, docs/sync.md,
docs/operations.md).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
SETUP_DOCS = [
    ROOT / "docs" / name
    for name in (
        "API_SETUP_REQUIRED.md",
        "deploy-vercel.md",
        "WINDY_API_SETUP.md",
        "sync.md",
        "operations.md",
    )
]
DOCS = [README, *SETUP_DOCS]

# `python -m app.cli` and what follows it, up to the end of the inline code span, the line,
# a pipe or a shell separator.
CLI_RE = re.compile(r"python -m app\.cli((?:[ \t]+[^\s`|;&)]+)*)")
WORD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
OPTION_RE = re.compile(r"(--[a-z][a-z0-9-]*)")
SCRIPT_RE = re.compile(r"scripts/([A-Za-z0-9_-]+\.(?:sh|py))\b")
VERCEL_LOCAL_RE = re.compile(r"scripts/vercel-local\.sh ([a-z][a-z-]*)")
NPM_RE = re.compile(r"\bnpm run ([a-z][a-z:-]*)")


def command_text(text: str) -> str:
    """``text`` with every command on one line: prose paragraphs are joined (an inline code
    span wrapped over two lines becomes one), fenced code keeps its lines but joins a
    shell continuation (a line ending in a backslash) with the next."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("\n")
        elif in_fence:
            stripped = line.rstrip()
            out.append(stripped[:-1] + " " if stripped.endswith("\\") else line + "\n")
        else:
            out.append(line + " " if line.strip() else "\n")
    return "".join(out)


def cli_invocations(text: str) -> list[tuple[list[str], list[str]]]:
    """(command words, --options) of every `python -m app.cli` in ``text``."""
    found = []
    for match in CLI_RE.finditer(command_text(text)):
        tokens = match.group(1).split()
        words: list[str] = []
        for token in tokens:
            if len(words) == 2 or not WORD_RE.match(token):
                break
            words.append(token)
        options = [opt for token in tokens for opt in OPTION_RE.findall(token)]
        if words:
            found.append((words, options))
    return found


def help_text(words: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """The --help output of ``words``; argparse exits 0 after printing it."""
    with pytest.raises(SystemExit) as exc:
        cli_main([*words, "--help"])
    assert exc.value.code == 0, f"python -m app.cli {' '.join(words)} is not a command"
    return capsys.readouterr().out


def subcommands(help_out: str) -> set[str]:
    """The sub-command choices of a command's help ({a,b,c}), or none."""
    usage = help_out.split("\n\n", 1)[0]
    choices = re.search(r"\{([a-z0-9,-]+)\}", usage)
    return set(choices.group(1).split(",")) if choices else set()


def test_the_readme_names_the_cli_commands_it_documents() -> None:
    commands = {tuple(words[:1]) for words, _ in cli_invocations(README.read_text())}
    for expected in ("init-db", "migrate", "pipeline", "sync", "forecast", "check-config"):
        assert (expected,) in commands, expected


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_cli_command_and_option_named_exists(
    doc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invocations = cli_invocations(doc.read_text())
    assert invocations or doc != README
    checked: dict[tuple[str, ...], str] = {}
    for words, options in invocations:
        group_help = checked.get((words[0],)) or help_text(words[:1], capsys)
        checked[(words[0],)] = group_help
        target = words[:1]
        subs = subcommands(group_help)
        if subs:
            assert len(words) > 1 and words[1] in subs, (
                f"{doc.name}: python -m app.cli {' '.join(words)}: not one of {sorted(subs)}"
            )
            target = words
        key = tuple(target)
        out = checked.get(key) or help_text(target, capsys)
        checked[key] = out
        for option in options:
            assert re.search(rf"{re.escape(option)}\b", out), (
                f"{doc.name}: python -m app.cli {' '.join(target)} has no option {option}"
            )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_script_named_exists(doc: Path) -> None:
    for name in sorted(set(SCRIPT_RE.findall(doc.read_text()))):
        path = ROOT / "scripts" / name
        assert path.is_file(), f"{doc.name} names scripts/{name}, which does not exist"
        if name.endswith(".sh"):
            assert os.access(path, os.X_OK), f"scripts/{name} is not executable"


def test_every_vercel_local_verb_named_is_handled() -> None:
    script = (ROOT / "scripts" / "vercel-local.sh").read_text()
    handled = set(re.findall(r"^\s*([a-z][a-z-]*)\)\s+cmd_", script, flags=re.M))
    assert {"up", "sync", "smoke", "down"} <= handled
    for doc in [*DOCS, ROOT / "docs" / "local-vercel-stack.md"]:
        for verb in set(VERCEL_LOCAL_RE.findall(doc.read_text())):
            assert verb in handled, f"{doc.name}: scripts/vercel-local.sh {verb} is not a verb"


def test_every_npm_script_named_exists() -> None:
    scripts = json.loads((ROOT / "frontend" / "package.json").read_text())["scripts"]
    text = README.read_text()
    named = set(NPM_RE.findall(text))
    assert {"build", "dev"} <= named
    for name in named:
        assert name in scripts, f"README names npm run {name}, not in frontend/package.json"
    if "npm test" in text:
        assert "test" in scripts
