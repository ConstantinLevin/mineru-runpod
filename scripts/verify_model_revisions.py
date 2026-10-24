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
