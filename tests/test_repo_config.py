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


def test_an_empty_download_record_is_no_record(verify, tmp_path) -> None:
    """A record cut off before its first line names no commit. Beside a valid one it
    would otherwise pass for a file whose revision nothing states."""
    _download(tmp_path, REPO, "Layout/model.onnx", EXPECTED)
    _download(tmp_path, REPO, "OCR/det.pth", EXPECTED)
    (tmp_path / REPO / ".cache" / "huggingface" / "download" / "OCR" / "det.pth.metadata").write_bytes(b"")
    problems = verify.check(tmp_path, {REPO: EXPECTED})
    assert len(problems) == 1 and "OCR/det.pth" in problems[0], problems


@pytest.mark.parametrize("marker_dir", ["", "Layout/PP-DocLayoutV2"])
def test_minerus_completion_marker_is_not_a_file_that_lacks_a_record(verify, tmp_path, marker_dir) -> None:
    """After the download MinerU touches `.mineru_complete` in the repository root, or
    in each required directory. Hugging Face never recorded it, and `mineru-kit models
    verify` needs it, so a good download carries one on every build."""
    _download(tmp_path, REPO, "Layout/PP-DocLayoutV2/model.onnx", EXPECTED)
    (tmp_path / REPO / marker_dir / ".mineru_complete").touch()
    assert verify.check(tmp_path, {REPO: EXPECTED}) == []


def test_the_marker_does_not_excuse_another_unrecorded_file(verify, tmp_path) -> None:
    _download(tmp_path, REPO, "Layout/model.onnx", EXPECTED)
    (tmp_path / REPO / ".mineru_complete").touch()
    (tmp_path / REPO / "OCR").mkdir()
    (tmp_path / REPO / "OCR" / "det.pth").write_bytes(b"weights")
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert "OCR/det.pth" in problem
    assert ".mineru_complete" not in problem


@pytest.mark.parametrize(
    ("unrecorded", "content"),
    [
        (".mineru_complete.bak", b"weights"),  # starts like the marker
        ("x.mineru_complete", b"weights"),     # ends like it
        ("mineru_complete", b"weights"),       # without the dot
        (".mineru", b"weights"),               # a prefix of it
        (".hidden", b"weights"),               # any other dotfile
        ("Layout/PP-DocLayoutV2/extra.bin", b"weights"),     # beside a marker in a subdirectory
        ("OCR/.mineru_complete/never.bin", b"weights"),      # under a directory of that name
        ("empty.bin", b""),                    # the marker is empty, an unrecorded file may be too
    ],
)
def test_only_the_marker_itself_is_skipped(verify, tmp_path, unrecorded, content) -> None:
    """The exemption is one file name. A rule any wider (every dotfile, every name that
    contains it or begins like it, everything in a directory that has one or is named
    like it, every empty file) would pass a file of unknown origin, which is what the
    check is there to stop."""
    _download(tmp_path, REPO, "Layout/PP-DocLayoutV2/model.onnx", EXPECTED)
    (tmp_path / REPO / ".mineru_complete").touch()
    (tmp_path / REPO / "Layout" / "PP-DocLayoutV2" / ".mineru_complete").touch()
    stray = tmp_path / REPO / unrecorded
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(content)
    (problem,) = verify.check(tmp_path, {REPO: EXPECTED})
    assert unrecorded in problem


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

