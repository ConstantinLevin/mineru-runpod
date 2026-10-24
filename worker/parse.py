"""MinerU lazy import + the single async parse entry point."""

from __future__ import annotations

import json
import os
from importlib import metadata
from pathlib import Path
from typing import Any

from runpod_doc_worker.contract import degraded as _degraded
from runpod_doc_worker.obs import logging as _logging
from runpod_doc_worker.obs import redact as _redact

from worker import harness as _harness

# MinerU's heavy imports run lazily inside run_mineru so the handler module
# itself imports on a CPU-only test machine (CI exercises input validation
# and packaging without needing a GPU). Module-level only does a soft probe
# so we can report MINERU_VERSION even if the dep failed to install.


def installed_version() -> str:
    """The version of the MinerU package that is installed, or "unknown".

    Read from the installed distribution's metadata. It used to be read from
    `mineru.__version__`, an attribute the package does not carry, so every
    response said "unknown" whatever was running.
    """
    try:
        return metadata.version("mineru")
    except metadata.PackageNotFoundError:
        return "unknown"


def probe_mineru() -> tuple[bool, str, str | None]:
    """Whether MinerU's parser imports, which version is installed, and why not.

    The version is the installed distribution's whether or not the import works:
    a broken dependency must not replace it in every response, since it is the one
    fact that says what was deployed. The import error is kept apart for the
    message of the job that cannot run and for the probe.
    """
    version = installed_version()
    try:
        from mineru.parser import parse_async as _smoke  # noqa: F401, PLC0415
    except Exception as exc:  # noqa: BLE001 - any failure to import is the answer
        return False, version, f"{type(exc).__name__}: {exc}"
    return True, version, None


MINERU_AVAILABLE, MINERU_VERSION, MINERU_IMPORT_ERROR = probe_mineru()


def _first_and_last_page(start_page: int, end_page: int | None) -> tuple[int, int | None]:
    """The job's 0-based inclusive window as 1-based first and last pages; no last page is the end."""
    return start_page + 1, None if end_page is None else end_page + 1


def _page_range(start_page: int, end_page: int | None) -> str:
    """The job's 0-based inclusive window as MinerU's 1-based `page_range`.

    The whole document is no range at all, and "r1" is MinerU's spelling of the
    last page. Whether a file takes a range is decided by the caller.
    """
    first, last = _first_and_last_page(start_page, end_page)
    if last is None:
        return "" if first == 1 else f"{first}-r1"
    return f"{first}-{last}"


def _window_in_words(start_page: int, end_page: int | None) -> str:
    """The job's window in its own fields, and in pages counted from 1 as the compat client sends them."""
    first, last = _first_and_last_page(start_page, end_page)
    if last is None:
        return f"start_page={start_page} (page {first} to the end, counting from 1)"
    return f"start_page={start_page}, end_page={end_page} (pages {first}-{last}, counting from 1)"


async def run_mineru(
    file_bytes: bytes,
    work_dir: Path,
    *,
    start_page: int,
    end_page: int | None,
    tier: str,
    ocr_mode: str,
    image_analysis: bool,
    lost: _degraded.Report | None = None,
) -> Path:
    """Read one document with MinerU 4 and return the directory it saved into.

    What the worker adds to MinerU's own files and cannot produce is noted in
    ``lost`` instead of failing a parse that has already been paid for.
    """
    if not MINERU_AVAILABLE:
        raise RuntimeError(f"mineru is not importable: {MINERU_IMPORT_ERROR}")
    # Late re-import keeps the static import wrapped; the binding is the real one here.
    from mineru import filetypes  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.errors import InvalidRequestError  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.parser import parse_async as _parse_async  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.parser.base import ParseResult  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.parser.file_type import guess_suffix_by_bytes  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.parser.writer import FileBasedDataWriter  # type: ignore[import-not-found]  # noqa: PLC0415
    from mineru.render.contracts import RenderMode  # type: ignore[import-not-found]  # noqa: PLC0415

    # MinerU decides what a file is from its content, not from a name: a job
    # carries only bytes, and a `basename` has no extension. The file is handed to
    # MinerU under the suffix it detected, which is what MinerU routes on. Images
    # go as the files they are -- MinerU turns them into pages itself.
    suffix = guess_suffix_by_bytes(file_bytes)
    if suffix not in filetypes.PARSEABLE_EXTENSIONS:
        raise ValueError(
            f"input is not a file type MinerU {MINERU_VERSION} reads "
            f"(detected {suffix or 'nothing recognisable'!r}). It reads PDF, "
            f"images, Office, OpenDocument, RTF, EPUB, OFD, HTML and MHTML files."
        )
    source = work_dir / "input" / f"document.{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(file_bytes)

    # MinerU rejects a range for anything that is not a PDF, an image included,
    # and reads that file whole: the window is a PDF's alone.
    page_range = _page_range(start_page, end_page) if suffix in filetypes.PAGE_RANGE_PARSE_EXTENSIONS else ""
    try:
        result = await _parse_async(
            source,
            # Everything that is not a PDF or an image is read whole, without models,
            # and MinerU asks for the tier to say so.
            tier=filetypes.batch_effective_parse_tier(tier, suffix),
            ocr_mode=ocr_mode,
            image_analysis=image_analysis,
            page_range=page_range,
        )
    except InvalidRequestError as exc:
        if exc.param != "page_range" or not page_range:
            raise
        # MinerU checks the window against the PDF and names its own 1-based range,
        # which the caller never sent: say which of their fields selects no page.
        raise ValueError(
            f"{_window_in_words(start_page, end_page)} selects no page of this PDF; "
            f"start_page must be below its page count (MinerU: {exc})"
        ) from exc

    output_dir = work_dir / "output"
    result.save(FileBasedDataWriter(str(output_dir)))

    # The default markdown leaves out headers, footers, page numbers, margin text
    # and page footnotes; the full mode keeps them. Rendered from what was just
    # saved, so its image links resolve to the files beside it. This file is the
    # worker's, not MinerU's, and MinerU's files are already on disk: if the render
    # fails, the parse is not lost, and the response says the file is missing.
    # Written beside the output directory and moved in once complete, so a write that
    # fails midway leaves nothing in the directory that is packaged; the stray file
    # goes with the job's temporary directory.
    staged = work_dir / f"{_harness.MARKDOWN_FULL_FILE}.partial"
    try:
        saved = ParseResult.from_json((output_dir / _harness.MIDDLE_JSON_FILE).read_text(encoding="utf-8"))
        full = saved.markdown(mode=RenderMode.FULL)
        staged.write_text(full, encoding="utf-8")
        os.replace(staged, output_dir / _harness.MARKDOWN_FULL_FILE)
    except Exception as exc:  # noqa: BLE001 - whatever MinerU's renderer raises
        # The renderer's exception can quote the document and URLs inside it: log it
        # through the same reduction as a failed job's error.
        _logging.warning(
            "full-mode markdown could not be rendered",
            error_type=type(exc).__name__,
            error_message=_redact.compact(str(exc)),
        )
        (lost if lost is not None else _degraded.Report()).note(
            reason=_degraded.UNREADABLE,
            file=_harness.MARKDOWN_FULL_FILE,
            artifact="markdown_full",
            error_type=type(exc).__name__,
        )
    return output_dir


def reported_settings(output_dir: Path, *, image_analysis: bool) -> dict[str, Any]:
    """How the document was read, as MinerU noted it in the result.

    MinerU records the tier and the OCR mode it actually used, not the ones that
    were asked for: `auto` becomes `txt` or `ocr`, and a Office file asked for
    `advanced` is noted as `flash`. Image analysis only acts in `advanced`, so the
    reported value follows the reported tier. A result that carries no note leaves
    all three unknown rather than guessing from the request.
    """
    unknown: dict[str, Any] = {"tier": None, "ocr_mode": None, "image_analysis": None}
    try:
        middle = json.loads((output_dir / _harness.MIDDLE_JSON_FILE).read_text(encoding="utf-8"))
        noted = middle["extensions"]["mineru"]
        tier, ocr_mode = noted["tier"], noted["parse_mode"]
    except (OSError, ValueError, KeyError, TypeError):
        return unknown
    return {
        "tier": tier,
        "ocr_mode": ocr_mode,
        "image_analysis": bool(image_analysis and tier == "advanced"),
    }
