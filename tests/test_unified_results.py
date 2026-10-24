"""Unified `results: [...]` response shape.

Exercises the harness's package_results_entry with this worker's manifest
(no GPU, no MinerU) plus the handler-level shape assertions. What is being
asserted is the contract callers see: which keys an entry carries per
transport, and which ones a `formats` filter leaves out.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.transport import package

import handler
from worker.harness import MANIFEST


# -----------------------------------------------------------------------------
# Fixture
# -----------------------------------------------------------------------------

def _seed_mineru_output(dir_: Path, basename: str | None = None) -> None:
    """The files MinerU 4's `ParseResult.save` writes, plus the full-mode markdown.

    `basename` is accepted and unused: MinerU 4 names its files itself, so the
    caller's basename no longer reaches the output directory.
    """
    (dir_ / "markdown.md").write_text("# heading\n\nbody\n", encoding="utf-8")
    (dir_ / "markdown_full.md").write_text("# heading\n\nbody\n\n1\n", encoding="utf-8")
    (dir_ / "middle_json.json").write_text(json.dumps({"k": 1}), encoding="utf-8")
    (dir_ / "structured_content.json").write_text(
        json.dumps({"blocks": [{"type": "text", "text": "body"}]}), encoding="utf-8"
    )
    (dir_ / "model_output.json").write_text(json.dumps({"raw": True}), encoding="utf-8")
    (dir_ / "images").mkdir()
    (dir_ / "images" / "fig1.png").write_bytes(b"\x89PNG fake")


# -----------------------------------------------------------------------------
# package_results_entry — per-transport assembly
# -----------------------------------------------------------------------------

def test_tarball_entry_carries_basename_source_pages_and_tarball(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    entry = package.package_results_entry(
        transport="tarball_b64",
        formats=["markdown"],  # no-op for tarball
        output_dir=out,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        metadata={"pages_requested": 10},
    )
    assert entry["basename"] == "doc"
    assert entry["source"] == "b64"
    assert entry["pages_requested"] == 10
    assert isinstance(entry["tarball_b64"], str) and entry["tarball_b64"]
    # tarball is self-contained — formats filter does NOT remove files inside
    raw = base64.b64decode(entry["tarball_b64"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {
        "markdown.md",
        "markdown_full.md",
        "middle_json.json",
        "structured_content.json",
        "model_output.json",
        "images/fig1.png",
    } <= names
    # No inline-format keys leak into a tarball entry.
    assert "markdown" not in entry
    assert "structured_content" not in entry


def test_tarball_entry_zip_archive_format(tmp_path):
    """archive_format='zip' produces a base64 .zip (still under tarball_b64)."""
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    entry = package.package_results_entry(
        transport="tarball_b64",
        formats=["markdown"],
        output_dir=out,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        metadata={"pages_requested": 10},
        archive_format="zip",
    )
    raw = base64.b64decode(entry["tarball_b64"])
    assert raw[:4] == b"PK\x03\x04"  # zip local-file-header magic
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
    assert "markdown.md" in names
    assert "markdown_full.md" in names
    assert "images/fig1.png" in names


def test_inline_entry_with_all_formats(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    entry = package.package_results_entry(
        transport="inline",
        formats=[
            "markdown", "markdown_full", "middle_json", "structured_content",
            "model_output", "images",
        ],
        output_dir=out,
        basename="doc",
        source="url:https://x/doc.pdf",
        manifest=MANIFEST,
        metadata={"pages_requested": -1},
    )
    assert entry["basename"] == "doc"
    assert entry["source"] == "url:https://x/doc.pdf"
    assert entry["markdown"].startswith("# heading")
    assert entry["markdown_full"].endswith("1\n")
    assert entry["structured_content"]["blocks"][0]["text"] == "body"
    assert entry["middle_json"] == {"k": 1}
    assert entry["model_output"] == {"raw": True}
    assert "fig1.png" in entry["images"]
    assert "tarball_b64" not in entry
    assert "tarball_url" not in entry


def test_inline_entry_with_markdown_only_omits_other_keys(tmp_path):
    """formats=['markdown'] produces ONLY the markdown key in the entry —
    all the others are absent, not present-as-empty.
    """
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    entry = package.package_results_entry(
        transport="inline",
        formats=["markdown"],
        output_dir=out,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        metadata={"pages_requested": 5},
    )
    assert "markdown" in entry
    for other in ("markdown_full", "middle_json", "structured_content", "model_output", "images"):
        assert other not in entry
    assert "markdown" in entry


def test_inline_entry_with_two_formats(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    entry = package.package_results_entry(
        transport="inline",
        formats=["structured_content", "images"],
        output_dir=out,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        metadata={"pages_requested": 5},
    )
    assert "markdown" not in entry
    assert "middle_json" not in entry
    assert "structured_content" in entry
    assert "images" in entry


def test_inline_entry_without_images_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "markdown.md").write_text("# md", encoding="utf-8")
    # No images dir — `images` should be present-as-empty when requested.
    entry = package.package_results_entry(
        transport="inline",
        formats=["markdown", "images"],
        output_dir=out,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        metadata={"pages_requested": 1},
    )
    assert entry["markdown"] == "# md"
    assert entry["images"] == {}


# -----------------------------------------------------------------------------
# Handler-level shape — error path has no results
# -----------------------------------------------------------------------------

def test_handler_failure_response_has_no_results_key():
    """A validation failure must produce error+ok=false, NOT an empty results list."""
    result = asyncio.run(handler.handler({"input": {}}))  # missing source
    assert result["ok"] is False
    assert "error" in result
    assert "results" not in result


def test_handler_success_wraps_entry_in_results_list(monkeypatch):
    """Full handler path: a fake MinerU parse produces one entry in the results list."""

    async def fake_run(file_bytes, *, work_dir, **kwargs):  # noqa: ARG001
        out = work_dir / "fake-out"
        out.mkdir()
        (out / "markdown.md").write_text("# fake\n", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    result = asyncio.run(handler.handler({
        "input": {"file_b64": "JVBERi0xLjQK", "basename": "doc"}  # %PDF-1.4
    }))
    assert result["ok"] is True
    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["basename"] == "doc"
    assert entry["source"] == "b64"
    assert "tarball_b64" in entry  # default transport
    # Top-level keys job-scoped, NOT per-file:
    assert "elapsed_seconds" in result
    assert "mineru_version" in result
    assert "debug" in result
    # Per-entry keys NOT mirrored at top level:
    assert "basename" not in result
    assert "source" not in result
    assert "tarball_b64" not in result
    assert "pages_processed" not in result  # alias dropped per the pre-1.0 cutover


def test_handler_success_with_inline_filtered_formats(monkeypatch):
    """End-to-end: transport=inline + formats=['markdown'] returns ONLY markdown."""

    async def fake_run(file_bytes, *, work_dir, **kwargs):  # noqa: ARG001
        out = work_dir / "fake-out"
        out.mkdir()
        (out / "markdown.md").write_text("# fake\n", encoding="utf-8")
        (out / "structured_content.json").write_text("{}", encoding="utf-8")
        (out / "middle_json.json").write_text("{}", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    result = asyncio.run(handler.handler({
        "input": {
            "file_b64": "JVBERi0xLjQK",
            "basename": "doc",
            "transport": "inline",
            "formats": ["markdown"],
        }
    }))
    assert result["ok"] is True
    entry = result["results"][0]
    assert entry["markdown"] == "# fake\n"
    assert "structured_content" not in entry
    assert "middle_json" not in entry
    assert "images" not in entry


# -----------------------------------------------------------------------------
# What the response says about how the document was read
# -----------------------------------------------------------------------------

def _stub_run(monkeypatch, *, noted=None, record=None):
    """A stand-in MinerU run; `noted` is what MinerU writes into the middle JSON."""

    async def fake_run(file_bytes, *, work_dir, **kwargs):  # noqa: ARG001
        if record is not None:
            record.update(kwargs)
        out = work_dir / "fake-out"
        out.mkdir()
        (out / "markdown.md").write_text("# fake\n", encoding="utf-8")
        if noted is not None:
            (out / "middle_json.json").write_text(
                json.dumps({"extensions": {"mineru": noted}}), encoding="utf-8"
            )
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)


def test_the_job_settings_reach_the_parse(monkeypatch):
    seen = {}
    _stub_run(monkeypatch, record=seen)
    asyncio.run(handler.handler({
        "input": {
            "file_b64": "JVBERi0xLjQK",
            "tier": "standard",
            "ocr_mode": "txt",
            "image_analysis": False,
            "start_page": 2,
            "end_page": 4,
        }
    }))
    assert seen["tier"] == "standard"
    assert seen["ocr_mode"] == "txt"
    assert seen["image_analysis"] is False
    assert (seen["start_page"], seen["end_page"]) == (2, 4)


def test_without_settings_the_parse_gets_the_defaults(monkeypatch):
    seen = {}
    _stub_run(monkeypatch, record=seen)
    asyncio.run(handler.handler({"input": {"file_b64": "JVBERi0xLjQK"}}))
    assert (seen["tier"], seen["ocr_mode"], seen["image_analysis"]) == ("advanced", "ocr", True)
    assert seen["end_page"] is None


def test_the_response_names_the_settings_mineru_noted_not_the_ones_asked_for(monkeypatch):
    _stub_run(monkeypatch, noted={"tier": "advanced", "parse_mode": "txt"})
    result = asyncio.run(handler.handler({
        "input": {"file_b64": "JVBERi0xLjQK", "tier": "advanced", "ocr_mode": "auto"}
    }))
    assert result["tier"] == "advanced"
    assert result["ocr_mode"] == "txt"       # auto, resolved by MinerU
    assert result["image_analysis"] is True  # asked for and acting in advanced


def test_image_analysis_is_reported_off_outside_advanced(monkeypatch):
    _stub_run(monkeypatch, noted={"tier": "standard", "parse_mode": "ocr"})
    result = asyncio.run(handler.handler({
        "input": {"file_b64": "JVBERi0xLjQK", "tier": "standard", "image_analysis": True}
    }))
    assert result["tier"] == "standard"
    assert result["image_analysis"] is False


def test_the_response_reports_the_installed_mineru_version(monkeypatch):
    monkeypatch.setattr("worker.parse.MINERU_VERSION", "4.0.12")
    _stub_run(monkeypatch, noted={"tier": "advanced", "parse_mode": "ocr"})
    result = asyncio.run(handler.handler({"input": {"file_b64": "JVBERi0xLjQK"}}))
    assert result["mineru_version"] == "4.0.12"


def test_a_failed_job_reports_the_version_too(monkeypatch):
    monkeypatch.setattr("worker.parse.MINERU_VERSION", "4.0.12")
    result = asyncio.run(handler.handler({"input": {}}))
    assert result["ok"] is False
    assert result["mineru_version"] == "4.0.12"


def test_bytes_the_coarse_check_does_not_know_are_left_to_mineru(monkeypatch):
    """Only MinerU can say an HTML or RTF file is one it reads; the worker's own
    magic-number check knows PDF, images and ZIP containers and nothing else."""
    seen = {}
    _stub_run(monkeypatch, record=seen)
    html = base64.b64encode(b"<html><body>hi</body></html>").decode("ascii")
    result = asyncio.run(handler.handler({"input": {"file_b64": html}}))
    assert result["ok"] is True
    assert seen["tier"] == "advanced"
