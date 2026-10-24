"""The adapter between a job and MinerU 4's `parse_async`.

MinerU is not installed where these tests run, so a stand-in package is put in
`sys.modules`. It reproduces the surface of MinerU 4.0.12 that the adapter touches
(`mineru.parser.parse_async`, `ParseResult.save` / `from_json` / `markdown`,
`FileBasedDataWriter`, `guess_suffix_by_bytes`, the file-type tables and
`RenderMode`) and records what the adapter handed it. What is asserted is what
MinerU would have received and what the output directory holds afterwards -- the
two things a caller can see.
"""

from __future__ import annotations

import asyncio
import enum
import json
import sys
import types
from pathlib import Path

import pytest
from runpod_doc_worker.contract import degraded

from worker import parse

PDF = b"%PDF-1.7 fake"
PNG = b"\x89PNG\r\n\x1a\n fake"
DOCX = b"PK\x03\x04 fake docx"


class _RenderMode(enum.Enum):
    DEFAULT = "default"
    FULL = "full"


class _FakeParseResult:
    """What `ParseResult` does for the adapter: save itself, then be re-read."""

    def __init__(self, middle: dict) -> None:
        self.middle = middle

    def save(self, writer) -> None:
        writer.write_string("markdown.md", "# body\n")
        writer.write_string("middle_json.json", json.dumps(self.middle))
        writer.write_string("structured_content.json", "{}")
        writer.write("images/page_0_image_1.jpg", b"\xff\xd8jpeg")

    @staticmethod
    def from_json(text: str) -> "_FakeParseResult":
        return _FakeParseResult(json.loads(text))

    def markdown(self, *, mode=None, **_unused) -> str:
        return f"rendered:{mode.value if mode else 'none'}"


class _Writer:
    def __init__(self, parent_dir: str = "") -> None:
        self.parent = Path(parent_dir)

    def write(self, path: str, data: bytes) -> None:
        target = self.parent / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def write_string(self, path: str, data: str) -> None:
        self.write(path, data.encode("utf-8"))


@pytest.fixture
def mineru(monkeypatch):
    """A stand-in `mineru` package; `calls` records every `parse_async` call."""
    calls: list[dict] = []
    state = {"suffix": "pdf", "tier": "advanced", "parse_mode": "ocr"}

    async def parse_async(path, **kwargs):
        calls.append({"path": Path(path), "bytes": Path(path).read_bytes(), **kwargs})
        return _FakeParseResult(
            {"extensions": {"mineru": {"tier": state["tier"], "parse_mode": state["parse_mode"]}}}
        )

    def module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    pdf = frozenset({"pdf"})
    images = frozenset({"png", "jpg", "tiff"})
    whole = frozenset({"docx", "html", "epub"})
    module("mineru")
    module("mineru.parser", parse_async=parse_async)
    module("mineru.parser.base", ParseResult=_FakeParseResult)
    module("mineru.parser.writer", FileBasedDataWriter=_Writer)
    module(
        "mineru.parser.file_type",
        guess_suffix_by_bytes=lambda data, file_path=None: state["suffix"],
    )
    module(
        "mineru.filetypes",
        PARSEABLE_EXTENSIONS=pdf | images | whole,
        batch_effective_parse_tier=lambda tier, ext: "flash" if ext in whole else tier,
    )
    module("mineru.render", )
    module("mineru.render.contracts", RenderMode=_RenderMode)
    monkeypatch.setattr(parse, "MINERU_AVAILABLE", True)
    return types.SimpleNamespace(calls=calls, state=state)


def _run(file_bytes=PDF, tmp_path=None, **overrides):
    kwargs = dict(
        start_page=0,
        end_page=None,
        tier="advanced",
        ocr_mode="ocr",
        image_analysis=True,
    )
    kwargs.update(overrides)
    return asyncio.run(parse.run_mineru(file_bytes, work_dir=tmp_path, **kwargs))


def test_a_pdf_is_read_with_the_jobs_settings(mineru, tmp_path):
    _run(PDF, tmp_path, tier="standard", ocr_mode="txt", image_analysis=False)
    (call,) = mineru.calls
    assert call["path"].suffix == ".pdf"
    assert call["bytes"] == PDF
    assert (call["tier"], call["ocr_mode"], call["image_analysis"]) == ("standard", "txt", False)


def test_an_image_goes_to_mineru_as_the_file_it_is(mineru, tmp_path):
    mineru.state["suffix"] = "png"
    _run(PNG, tmp_path)
    (call,) = mineru.calls
    assert call["path"].suffix == ".png"
    assert call["bytes"] == PNG, "no conversion to PDF on the worker's side"
    assert call["tier"] == "advanced"


@pytest.mark.parametrize(
    ("start", "end", "page_range"),
    [
        (0, None, ""),        # the whole document is no range at all
        (0, 4, "1-5"),        # 0-based inclusive becomes 1-based inclusive
        (2, 2, "3-3"),
        (3, None, "4-r1"),    # open-ended: to MinerU's "last page"
    ],
)
def test_the_page_window_becomes_a_one_based_page_range(mineru, tmp_path, start, end, page_range):
    _run(PDF, tmp_path, start_page=start, end_page=end)
    assert mineru.calls[0]["page_range"] == page_range


def test_a_whole_document_format_is_read_in_tier_flash(mineru, tmp_path):
    mineru.state["suffix"] = "docx"
    _run(DOCX, tmp_path, tier="advanced")
    (call,) = mineru.calls
    assert call["path"].suffix == ".docx"
    assert call["tier"] == "flash"


def test_a_file_mineru_does_not_read_is_turned_away_before_any_parse(mineru, tmp_path):
    mineru.state["suffix"] = "txt"
    with pytest.raises(ValueError, match="txt"):
        _run(b"just text", tmp_path)
    assert mineru.calls == []


def test_the_result_is_saved_in_minerus_own_layout(mineru, tmp_path):
    out = _run(PDF, tmp_path)
    assert (out / "markdown.md").read_text() == "# body\n"
    assert json.loads((out / "middle_json.json").read_text())["extensions"]
    assert (out / "structured_content.json").is_file()
    assert (out / "images" / "page_0_image_1.jpg").read_bytes() == b"\xff\xd8jpeg"


def test_the_full_mode_markdown_is_written_beside_the_default_one(mineru, tmp_path):
    out = _run(PDF, tmp_path)
    assert (out / "markdown_full.md").read_text() == "rendered:full"


def test_the_settings_mineru_noted_are_the_ones_reported(mineru, tmp_path):
    mineru.state.update(tier="flash", parse_mode="txt")
    out = _run(PDF, tmp_path, tier="advanced", ocr_mode="auto")
    reported = parse.reported_settings(out, image_analysis=True)
    assert reported == {"tier": "flash", "ocr_mode": "txt", "image_analysis": False}


def test_image_analysis_is_reported_as_it_acted(mineru, tmp_path):
    out = _run(PDF, tmp_path, tier="advanced", image_analysis=True)
    assert parse.reported_settings(out, image_analysis=True)["image_analysis"] is True
    assert parse.reported_settings(out, image_analysis=False)["image_analysis"] is False


def test_settings_mineru_did_not_note_are_reported_as_unknown(tmp_path):
    assert parse.reported_settings(tmp_path, image_analysis=True) == {
        "tier": None,
        "ocr_mode": None,
        "image_analysis": None,
    }


def test_the_reported_version_is_the_installed_one(monkeypatch):
    monkeypatch.setattr(parse.metadata, "version", lambda name: "4.0.12" if name == "mineru" else "x")
    assert parse.installed_version() == "4.0.12"


def test_the_version_is_unknown_only_when_the_package_has_none(monkeypatch):
    def missing(name):
        raise parse.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(parse.metadata, "version", missing)
    assert parse.installed_version() == "unknown"


def test_a_full_mode_render_that_fails_costs_that_file_not_the_parse(mineru, tmp_path, monkeypatch):
    """`markdown_full` is built by the worker, after MinerU has saved the result it
    was paid for. If rendering it fails, the saved files still ship, and the loss
    is reported rather than dropped."""
    def broken(self, *, mode=None, **_unused):
        if mode is not None and mode.value == "full":
            raise RuntimeError("cannot render this document in full mode")
        return "default"

    monkeypatch.setattr(_FakeParseResult, "markdown", broken)
    lost = degraded.Report()
    out = _run(PDF, tmp_path, lost=lost)

    assert (out / "markdown.md").read_text() == "# body\n"
    assert not (out / "markdown_full.md").exists()
    entry = lost.entry()
    assert entry["count"] == 1
    (item,) = entry["items"]
    assert (item["artifact"], item["file"], item["reason"], item["error_type"]) == (
        "markdown_full", "markdown_full.md", "unreadable", "RuntimeError",
    )


def test_the_version_survives_a_parser_that_cannot_be_imported(monkeypatch):
    """The version is the installed distribution's, whether or not the parser then
    imports. A broken dependency used to replace it with an error string in every
    response, which hides the one fact that says what was deployed."""
    monkeypatch.setattr(parse.metadata, "version", lambda name: "4.0.12")
    monkeypatch.setitem(sys.modules, "mineru.parser", None)  # makes the import raise
    available, version, import_error = parse.probe_mineru()
    assert available is False
    assert version == "4.0.12"
    assert "mineru.parser" in import_error


def test_a_parse_without_an_importable_mineru_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(parse, "MINERU_AVAILABLE", False)
    monkeypatch.setattr(parse, "MINERU_IMPORT_ERROR", "ModuleNotFoundError: no module named docvortex")
    with pytest.raises(RuntimeError, match="docvortex"):
        _run(PDF, tmp_path)
