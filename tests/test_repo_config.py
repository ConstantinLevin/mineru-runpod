"""Repository configuration that is easy to get subtly wrong.

Config is not covered by any other test here, and its failures are quiet: nothing
breaks, a job still runs, and the only symptom is something that should have
happened not happening. A security update that is never opened looks exactly like
a repository with no vulnerabilities.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _dependabot() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )


def test_npm_security_updates_reach_lockfile_only_vulnerabilities() -> None:
    """`allow: dependency-type: direct` narrows security updates as well as
    version ones — `allow` carries the security shield in GitHub's options
    reference — so it excluded any CVE reachable only through package-lock.json.
    In an npm tree that is most of them.

    Removing it loses nothing: the documented default for version updates is
    already "all dependencies explicitly defined in a manifest", which is what the
    restriction specified. The npm-security-updates group in the same file only
    means something once transitive vulnerabilities can reach it.
    """
    npm = [u for u in _dependabot()["updates"] if u["package-ecosystem"] == "npm"]
    assert npm, "the npm ecosystem must be configured"
    for entry in npm:
        allowed = entry.get("allow") or []
        assert not any(rule.get("dependency-type") == "direct" for rule in allowed), (
            "this excludes lockfile-only vulnerabilities from security updates"
        )


def test_the_npm_security_group_still_exists() -> None:
    """The thing the above enables. Without it the fix is half a fix."""
    npm = [u for u in _dependabot()["updates"] if u["package-ecosystem"] == "npm"][0]
    groups = npm.get("groups") or {}
    assert any(g.get("applies-to") == "security-updates" for g in groups.values())


def test_github_actions_keeps_its_restriction() -> None:
    """Actions have no transitive dependencies, so there the restriction is inert
    rather than harmful — and removing it would be churn, not a fix.
    """
    actions = [
        u for u in _dependabot()["updates"]
        if u["package-ecosystem"] == "github-actions"
    ]
    assert actions
    assert any(
        rule.get("dependency-type") == "direct"
        for entry in actions
        for rule in (entry.get("allow") or [])
    )


# -----------------------------------------------------------------------------
# The build step that pins the baked model revisions (scripts/verify_model_revisions.py)
#
# MinerU's downloader fetches whatever a repository's main branch is at build time
# and takes no revision. The Dockerfile names the commit it expects and this script
# fails the build when what was fetched is something else. The Dockerfile only ever
# runs it against a good download, so what it does with a bad one is asserted here.
# -----------------------------------------------------------------------------

EXPECTED = "2b3afb86f4d23fa623f6b2f4b2279ed4ef93a89d"
OTHER = "08aaea840498d49ce16247b6263196cf02814885"
REPO = "MinerU-4_models_torch"


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location(
        "verify_model_revisions", REPO_ROOT / "scripts" / "verify_model_revisions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download(models: Path, repo: str, relative: str, commit: str) -> None:
    """What huggingface_hub leaves for one file of a `local_dir` download: the file
    and, beside it, a record of the commit it came from, the etag and a timestamp."""
    file = models / repo / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"weights")
    meta = models / repo / ".cache" / "huggingface" / "download" / f"{relative}.metadata"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(f"{commit}\n\"etag\"\n1790000000.0\n", encoding="utf-8")


def test_a_repository_at_the_expected_commit_passes(verify, tmp_path) -> None:
    _download(tmp_path, REPO, "Layout/model.onnx", EXPECTED)
    _download(tmp_path, REPO, "OCR/det.pth", EXPECTED)
    assert verify.check(tmp_path, {REPO: EXPECTED}) == []


def test_a_repository_at_another_commit_is_reported_with_both(verify, tmp_path) -> None:
    _download(tmp_path, REPO, "Layout/model.onnx", OTHER)
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert REPO in problem
    assert EXPECTED in problem and OTHER in problem


def test_one_file_from_another_commit_fails_the_whole_repository(verify, tmp_path) -> None:
    _download(tmp_path, REPO, "a.bin", EXPECTED)
    _download(tmp_path, REPO, "b.bin", OTHER)
    assert len(verify.check(tmp_path, {REPO: EXPECTED})) == 1


def test_a_repository_with_no_download_record_is_a_failure_not_a_pass(verify, tmp_path) -> None:
    """Nothing to compare is not the same as nothing differing: a layout change in
    the downloader would otherwise turn this check into one that always passes."""
    (tmp_path / REPO).mkdir()
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert "no download record" in problem


def test_a_file_without_a_download_record_is_a_failure_not_a_pass(verify, tmp_path) -> None:
    """The commits of the files that have a record all match, but one file has none,
    so nothing says where it came from. Judging only the recorded ones would let a
    repository pass with files of an unknown revision in it."""
    _download(tmp_path, REPO, "Layout/model.onnx", EXPECTED)
    (tmp_path / REPO / "OCR").mkdir()
    (tmp_path / REPO / "OCR" / "det.pth").write_bytes(b"weights")
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert REPO in problem
    assert "OCR/det.pth" in problem


def test_a_missing_repository_directory_is_reported(verify, tmp_path) -> None:
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert REPO in problem


def test_main_exits_nonzero_on_a_mismatch_and_zero_otherwise(verify, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    _download(tmp_path / "models", REPO, "a.bin", OTHER)
    assert verify.main([f"{REPO}={EXPECTED}"]) == 1
    assert EXPECTED in capsys.readouterr().err

    assert verify.main([f"{REPO}={OTHER}"]) == 0


def test_an_argument_that_is_not_name_equals_commit_is_refused(verify) -> None:
    with pytest.raises(SystemExit):
        verify.main([REPO])

