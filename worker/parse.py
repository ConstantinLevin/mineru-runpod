"""MinerU lazy import + the single async parse entry point."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import Any

from runpod_doc_worker.contract import degraded as _degraded
from runpod_doc_worker.obs import logging as _logging

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


def _page_range(start_page: int, end_page: int | None) -> str:
    """The job's 0-based inclusive window as MinerU's 1-based `page_range`.

    The whole document is no range at all: MinerU rejects any range for a file
    that is not a PDF, so an unbounded job must not send one. "r1" is MinerU's
    spelling of the last page.
    """
    if end_page is None:
        return "" if start_page == 0 else f"{start_page + 1}-r1"
    return f"{start_page + 1}-{end_page + 1}"


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
            f"images, Office, OpenDocument, RTF, EPUB, OFD, HTML and CSV/TSV files."
        )
    source = work_dir / "input" / f"document.{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(file_bytes)

    result = await _parse_async(
        source,
        # Everything that is not a PDF or an image is read whole, without models,
        # and MinerU asks for the tier to say so.
        tier=filetypes.batch_effective_parse_tier(tier, suffix),
        ocr_mode=ocr_mode,
        image_analysis=image_analysis,
        page_range=_page_range(start_page, end_page),
    )

    output_dir = work_dir / "output"
    result.save(FileBasedDataWriter(str(output_dir)))

    # The default markdown leaves out headers, footers, page numbers, margin text
    # and page footnotes; the full mode keeps them. Rendered from what was just
    # saved, so its image links resolve to the files beside it. This file is the
    # worker's, not MinerU's, and MinerU's files are already on disk: if the render
    # fails, the parse is not lost, and the response says the file is missing.
    try:
        saved = ParseResult.from_json((output_dir / "middle_json.json").read_text(encoding="utf-8"))
        full = saved.markdown(mode=RenderMode.FULL)
        (output_dir / "markdown_full.md").write_text(full, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - whatever MinerU's renderer raises
        _logging.warning("full-mode markdown could not be rendered", error=repr(exc))
        # A write that failed midway must not leave a file that reads as the document.
        # Best effort: if the removal fails too, the loss noted below still tells the
        # caller not to trust that file, and the parse is returned all the same.
        try:
            (output_dir / "markdown_full.md").unlink(missing_ok=True)
        except OSError as unlink_exc:
            _logging.warning("half-written full-mode markdown could not be removed", error=repr(unlink_exc))
        (lost if lost is not None else _degraded.Report()).note(
            reason=_degraded.UNREADABLE,
            file="markdown_full.md",
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
        middle = json.loads((output_dir / "middle_json.json").read_text(encoding="utf-8"))
        noted = middle["extensions"]["mineru"]
        tier, ocr_mode = noted["tier"], noted["parse_mode"]
    except (OSError, ValueError, KeyError, TypeError):
        return unknown
    return {
        "tier": tier,
        "ocr_mode": ocr_mode,
        "image_analysis": bool(image_analysis and tier == "advanced"),
    }
