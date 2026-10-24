"""The build step that pins the baked model revisions.

MinerU's downloader fetches whatever a repository's main branch is at build time
and takes no revision. The Dockerfile therefore names the commit it expects and
this script fails the build when what was fetched is something else, so a moved
repository stops a rebuild instead of changing the engine without anyone noticing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_model_revisions.py"
EXPECTED = "2b3afb86f4d23fa623f6b2f4b2279ed4ef93a89d"
OTHER = "08aaea840498d49ce16247b6263196cf02814885"


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_model_revisions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download(models: Path, repo: str, relative: str, commit: str) -> None:
    """What huggingface_hub leaves for one file of a `local_dir` download: the
    commit it came from, the etag and a timestamp, one per line."""
    meta = models / repo / ".cache" / "huggingface" / "download" / f"{relative}.metadata"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(f"{commit}\n\"etag\"\n1790000000.0\n", encoding="utf-8")


def test_a_repository_at_the_expected_commit_passes(verify, tmp_path):
    _download(tmp_path, "MinerU-4_models_torch", "Layout/model.onnx", EXPECTED)
    _download(tmp_path, "MinerU-4_models_torch", "OCR/det.pth", EXPECTED)
    assert verify.check(tmp_path, {"MinerU-4_models_torch": EXPECTED}) == []


def test_a_repository_at_another_commit_is_reported_with_both(verify, tmp_path):
    _download(tmp_path, "MinerU-4_models_torch", "Layout/model.onnx", OTHER)
    (problem,) = verify.check(tmp_path, {"MinerU-4_models_torch": EXPECTED})
    assert "MinerU-4_models_torch" in problem
    assert EXPECTED in problem and OTHER in problem


def test_one_file_from_another_commit_fails_the_whole_repository(verify, tmp_path):
    _download(tmp_path, "MinerU-4_models_torch", "a.bin", EXPECTED)
    _download(tmp_path, "MinerU-4_models_torch", "b.bin", OTHER)
    assert len(verify.check(tmp_path, {"MinerU-4_models_torch": EXPECTED})) == 1


def test_a_repository_with_no_download_record_is_a_failure_not_a_pass(verify, tmp_path):
    """Nothing to compare is not the same as nothing differing: a layout change in
    the downloader would otherwise turn this check into one that always passes."""
    (tmp_path / "MinerU-4_models_torch").mkdir()
    (problem,) = verify.check(tmp_path, {"MinerU-4_models_torch": EXPECTED})
    assert "no download record" in problem


def test_a_missing_repository_directory_is_reported(verify, tmp_path):
    (problem,) = verify.check(tmp_path, {"MinerU-4_models_torch": EXPECTED})
    assert "MinerU-4_models_torch" in problem


def test_main_exits_nonzero_on_a_mismatch_and_zero_otherwise(verify, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    _download(tmp_path / "models", "MinerU-4_models_torch", "a.bin", OTHER)
    assert verify.main([f"MinerU-4_models_torch={EXPECTED}"]) == 1
    assert EXPECTED in capsys.readouterr().err

    assert verify.main([f"MinerU-4_models_torch={OTHER}"]) == 0


def test_an_argument_that_is_not_name_equals_commit_is_refused(verify):
    with pytest.raises(SystemExit):
        verify.main(["MinerU-4_models_torch"])
