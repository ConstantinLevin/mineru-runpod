"""Shared pytest setup."""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import os
import sys
import types
from pathlib import Path

import pytest

# Make the repo root importable so `import handler` works.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests should not see any operator credentials.
for key in ("RUNPOD_API_KEY", "RUNPOD_ENDPOINT_ID", "MINERU_TEMPLATE_ID"):
    os.environ.pop(key, None)

# ...nor post anywhere. RunPod injects these two into a real worker, and the SDK
# reads them into module constants at import, so popping them here -- before any
# test imports handler -- is what makes a progress update a no-op under test. A
# test that runs the handler with a job id and no patched sender would otherwise
# reach for the network and fail somewhere unrelated to what it asserts.
for key in ("RUNPOD_WEBHOOK_POST_OUTPUT", "RUNPOD_WEBHOOK_POST_STREAM"):
    os.environ.pop(key, None)

# Install this worker's harness config for the whole session, the way
# importing handler does in production. Without it a test that reaches the
# harness directly runs against its defaults — a different env prefix and
# different input roots — and passes or fails for the wrong reason.
import worker  # noqa: E402, F401

class _RenderMode(enum.Enum):
    DEFAULT = "default"
    FULL = "full"


class _FakeParseResult:
    """What MinerU 4's `ParseResult` does for the worker: save itself, be re-read."""

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
def fake_mineru(monkeypatch):
    """A stand-in `mineru` package for tests that run the worker's own parse code.

    MinerU is not installed where the suite runs. This reproduces the surface of
    MinerU 4.0.12 that `worker.parse` touches (`parse_async`, `ParseResult.save` /
    `from_json` / `markdown`, `FileBasedDataWriter`, `guess_suffix_by_bytes`, the
    file-type tables, `RenderMode`) and records what the worker handed it.
    `calls` holds each `parse_async` call; `state` picks what the stand-in
    detects and notes (`suffix`, `tier`, `parse_mode`).
    """
    import sys  # noqa: PLC0415

    from worker import parse  # noqa: PLC0415

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
        PARSEABLE_EXTENSIONS=frozenset({"pdf", "png", "jpg", "tiff"}) | whole,
        PAGE_RANGE_PARSE_EXTENSIONS=frozenset({"pdf"}),
        batch_effective_parse_tier=lambda tier, ext: "flash" if ext in whole else tier,
    )
    module("mineru.render")
    module("mineru.render.contracts", RenderMode=_RenderMode)
    monkeypatch.setattr(parse, "MINERU_AVAILABLE", True)
    return types.SimpleNamespace(calls=calls, state=state, result_type=_FakeParseResult)


@pytest.fixture
def seed_mineru_output():
    """Write what a finished parse leaves in its output directory.

    The files MinerU 4's `ParseResult.save` writes, plus the worker's full-mode
    markdown. `model_output=False` leaves out the raw model output, which MinerU
    writes only when it has some.
    """

    def seed(dir_: Path, *, model_output: bool = True) -> None:
        (dir_ / "markdown.md").write_text("# heading\n\nbody\n", encoding="utf-8")
        (dir_ / "markdown_full.md").write_text("# heading\n\nbody\n\n1\n", encoding="utf-8")
        (dir_ / "middle_json.json").write_text(json.dumps({"k": 1}), encoding="utf-8")
        (dir_ / "structured_content.json").write_text(
            json.dumps({"blocks": [{"type": "text", "text": "body"}]}), encoding="utf-8"
        )
        if model_output:
            (dir_ / "model_output.json").write_text(json.dumps({"raw": True}), encoding="utf-8")
        (dir_ / "images").mkdir()
        (dir_ / "images" / "fig1.png").write_bytes(b"\x89PNG fake")

    return seed


@pytest.fixture
def run_job():
    """Run one job through the handler, with `data` as its `file_b64` source."""
    import handler  # noqa: PLC0415

    def run(data: bytes = b"%PDF-1.7 fake", **job_input) -> dict:
        payload = base64.b64encode(data).decode("ascii")
        return asyncio.run(handler.handler({"input": {"file_b64": payload, **job_input}}))

    return run
