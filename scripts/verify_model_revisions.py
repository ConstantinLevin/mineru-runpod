"""Fail the image build when a baked model repository is not at the expected commit.

    verify_model_revisions.py NAME=COMMIT [NAME=COMMIT ...]

MinerU's `mineru-kit models download` takes no revision: it fetches whatever a
repository's main branch is at build time. The Dockerfile names the commit it was
checked against, and this compares it with what was actually fetched, which
Hugging Face records beside every file of a `local_dir` download
(`.cache/huggingface/download/<file>.metadata`, whose first line is the commit).

A rebuild after a repository moved therefore stops here instead of silently
producing a different engine. Moving the pin is a deliberate edit.

Models are looked for under $MINERU_HOME/models, which is where MinerU puts them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _commits(repo_dir: Path) -> set[str]:
    records = (repo_dir / ".cache" / "huggingface" / "download").rglob("*.metadata")
    commits = set()
    for record in records:
        lines = record.read_text(encoding="utf-8").splitlines()
        if lines:
            commits.add(lines[0].strip())
    return commits


# MinerU touches this in the repository root, or in each required directory, once the
# download is done (`mineru/model/download.py`, MODEL_COMPLETE_MARKER); `mineru-kit
# models verify` reads it as "ready". It is MinerU's own file, not a download.
_MINERU_MARKER = ".mineru_complete"


def _unrecorded(repo_dir: Path) -> list[str]:
    """Downloaded files that have no record of the commit they came from."""
    records = repo_dir / ".cache" / "huggingface" / "download"
    missing = []
    for file in sorted(repo_dir.rglob("*")):
        relative = file.relative_to(repo_dir)
        if not file.is_file() or relative.parts[0] == ".cache" or file.name == _MINERU_MARKER:
            continue
        if not (records / f"{relative.as_posix()}.metadata").is_file():
            missing.append(relative.as_posix())
    return missing


def check(models: Path, expected: dict[str, str]) -> list[str]:
    """What is wrong with each repository, one sentence per repository."""
    problems = []
    for name, commit in expected.items():
        repo_dir = models / name
        if not repo_dir.is_dir():
            problems.append(f"{name}: not found under {models}")
            continue
        found = _commits(repo_dir)
        if not found:
            problems.append(
                f"{name}: no download record under {repo_dir}, so its commit cannot be "
                f"compared with the expected {commit}"
            )
        elif found != {commit}:
            problems.append(
                f"{name}: expected commit {commit}, found {', '.join(sorted(found))}"
            )
        elif unrecorded := _unrecorded(repo_dir):
            # Every recorded file matches, but a file with no record says nothing
            # about where it came from: judging only the recorded ones would pass it.
            problems.append(
                f"{name}: {len(unrecorded)} downloaded file(s) have no download record "
                f"(e.g. {unrecorded[0]}), so their commit cannot be compared with the expected {commit}"
            )
    return problems


def main(argv: list[str]) -> int:
    expected = {}
    for arg in argv:
        name, separator, commit = arg.partition("=")
        if not separator or not name or not commit:
            raise SystemExit(f"expected NAME=COMMIT, got {arg!r}")
        expected[name] = commit

    home = Path(os.environ.get("MINERU_HOME", "~/.mineru")).expanduser()
    problems = check(home / "models", expected)
    for problem in problems:
        print(f"model revision check failed: {problem}", file=sys.stderr)
    if not problems:
        print(f"model revisions verified: {', '.join(expected)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
