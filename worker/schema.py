"""Input schema (rp_validator) + cross-field validation."""

from __future__ import annotations

import os
from typing import Any

from runpod.serverless.utils.rp_validator import validate

from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.transport import net as _net
from runpod_doc_worker.transport import package as _package

from worker.harness import MANIFEST

# Both come from where the behaviour is rather than being restated here: a
# transport this worker accepts but the harness cannot pack, or a format the
# schema admits but no artifact produces, would be a contract that validates
# and then fails.
VALID_TRANSPORTS = _package.VALID_TRANSPORTS

# Declaration order in the manifest is the canonical output order — used as
# the default when `formats` is omitted, and as the iteration order for
# deduplication.
VALID_FORMATS: tuple[str, ...] = tuple(_artifacts.keys(MANIFEST))

# How deeply MinerU 4 reads a PDF or image, cheapest first. `advanced` is the
# default of this worker: it is the only tier that reads with the full model and
# the only one where `image_analysis` has an effect.
VALID_TIERS = ("flash", "basic", "standard", "advanced")

# Where the text of a page comes from. `ocr` forces the model to read it, which is
# this worker's default because on damaged material the text layer of a PDF is the
# damage; `txt` takes the text layer as it is (exact on clean digital PDFs); `auto`
# lets MinerU choose per file.
VALID_OCR_MODES = ("auto", "txt", "ocr")

# Fields MinerU 3.4 took and MinerU 4 does not. A job that sends one is turned
# away with what takes its place, rather than read with a setting the caller
# believed they had chosen. rp_validator would reject them too, but only as
# "unexpected input", which leaves a caller to guess where the setting went.
REMOVED_FIELDS: dict[str, str] = {
    "backend": (
        "MinerU 4 has no backends. Use `tier` (flash, basic, standard or "
        "advanced) for how deeply it reads and `ocr_mode` (auto, txt or ocr) "
        "for where the text comes from; the old vlm and hybrid backends "
        "correspond to tier=advanced"
    ),
    "effort": (
        "Use `tier` and `image_analysis`: the old effort=high is "
        "image_analysis=true, which acts in tier=advanced"
    ),
    "lang": "There is no replacement: MinerU 4 recognises the language itself",
    "formula_enable": (
        "There is no replacement: MinerU 4 has no switch for formulas"
    ),
    "table_enable": "There is no replacement: MinerU 4 has no switch for tables",
    "server_url": (
        "There is no replacement: it belonged to the *-http-client backends, "
        "and MinerU 4 reads inside the worker"
    ),
}

# `basename` labels the response entry. MinerU 4 names its own output files, so
# it no longer reaches the filesystem; 128 characters is still far above any real
# document name, and the bound keeps the label from growing a response.
MAX_BASENAME_LEN = 128

# Archive container for the archive transports (tarball_b64, s3). The default
# preserves historical behavior (.tar.gz); "zip" exists for callers that need a
# real .zip — e.g. the MinerU-API compat client matching the cloud API's
# `full_zip_url`. No-op for the inline transport.
VALID_ARCHIVE_FORMATS = {"tar.gz", "zip"}


# rp_validator's `constraints` lambdas are silently ignored on some versions
# — we declare them anyway for documentation but never rely on them.
# Cross-field rules and per-field bounds are re-checked manually in
# validate_input() below.
INPUT_SCHEMA: dict[str, dict[str, Any]] = {
    "file_url":       {"type": str,  "required": False, "default": None},
    "file_b64":       {"type": str,  "required": False, "default": None},
    "volume_path":    {"type": str,  "required": False, "default": None},
    # When `probe` is true the handler skips MinerU entirely and returns a
    # filesystem dump of /runpod-volume + relevant env vars. Used to debug
    # RunPod Cached Models setup.
    "probe":          {"type": bool, "required": False, "default": False},
    "start_page":     {"type": int,  "required": False, "default": 0},
    "end_page":       {"type": int,  "required": False, "default": -1},
    "tier":           {"type": str,  "required": False, "default": "advanced"},
    "ocr_mode":       {"type": str,  "required": False, "default": "ocr"},
    "image_analysis": {"type": bool, "required": False, "default": True},
    "transport":      {"type": str,  "required": False, "default": "tarball_b64"},
    "formats":        {"type": list, "required": False, "default": list(VALID_FORMATS)},
    "basename":       {"type": str,  "required": False, "default": "doc"},
    "archive_format": {"type": str,  "required": False, "default": "tar.gz"},
}


def _fail(msg: str) -> None:
    raise ValueError(f"input validation failed: {msg}")


def _max_pages_per_job() -> int:
    """Largest page range a single job may ask for; 0 means no ceiling.

    Read per job (like the refresh thresholds in handler.py) so an operator
    can tune it without a redeploy. Off by default: the endpoint's execution
    timeout is the backstop that has always applied, and a ceiling here is
    for operators who would rather a too-large request be turned away up
    front than spend GPU minutes on it.
    """
    try:
        return max(0, int(os.environ.get("MINERU_MAX_PAGES_PER_JOB", "0")))
    except ValueError:
        return 0


def _normalize_formats(raw: Any) -> list[str]:
    """Validate + dedupe a `formats` list. Returns a list with first-seen order.

    rp_validator catches outer-type mismatches (e.g. ``formats: "markdown"``)
    before this function runs; the per-element and emptiness checks below
    are what we actually rely on. Duplicates collapse. Empty list is rejected —
    callers asking for nothing would get no useful response.
    """
    if raw is None:
        return list(VALID_FORMATS)
    if not isinstance(raw, list):
        # Defensive: rp_validator already rejects non-list values, but keep
        # this in case the SDK behavior changes.
        _fail(f"formats must be a list of strings; got {type(raw).__name__}")
    if not raw:
        _fail("formats must not be empty; omit the field to get all formats")
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            _fail(f"formats entries must be strings; got {type(item).__name__}")
        if item not in VALID_FORMATS:
            _fail(
                f"formats entry {item!r} not one of {list(VALID_FORMATS)}"
            )
        if item not in seen:
            seen.append(item)
    return seen


def validate_input(job_input: dict) -> dict:
    """Run rp_validator over the schema and enforce cross-field rules.

    Returns the cleaned input dict with defaults applied. Raises ValueError
    with an ``input validation failed: ...`` prefix on any rejection.
    """
    removed = [
        f"{field} was removed in MinerU 4: {REMOVED_FIELDS[field]}"
        for field in REMOVED_FIELDS
        if field in job_input
    ]
    if removed:
        _fail("; ".join(removed))

    result = validate(job_input, INPUT_SCHEMA)
    if result.get("errors"):
        _fail("; ".join(result["errors"]))

    cleaned = result["validated_input"]

    basename = cleaned.get("basename") or "doc"
    if not basename or not all(c.isalnum() or c in "-_" for c in basename):
        _fail(f"basename must be alphanumeric (with - or _); got {basename!r}")
    if len(basename) > MAX_BASENAME_LEN:
        _fail(
            f"basename must be at most {MAX_BASENAME_LEN} characters; "
            f"got {len(basename)}"
        )
    cleaned["basename"] = basename

    # Write `transport` and the reading settings back so downstream code can read them
    # with `cleaned[...]` (rather than `.get(...) or default`) regardless of
    # whether rp_validator's `default` mechanism populated the key.
    transport = cleaned.get("transport") or "tarball_b64"
    if transport not in VALID_TRANSPORTS:
        _fail(f"transport must be one of {sorted(VALID_TRANSPORTS)}; got {transport!r}")
    cleaned["transport"] = transport

    # `archive_format` selects the container for the archive transports
    # (tarball_b64 / s3). Inline ignores it. Default keeps the .tar.gz behavior.
    archive_format = cleaned.get("archive_format") or "tar.gz"
    if archive_format not in VALID_ARCHIVE_FORMATS:
        _fail(
            f"archive_format must be one of {sorted(VALID_ARCHIVE_FORMATS)}; "
            f"got {archive_format!r}"
        )
    cleaned["archive_format"] = archive_format

    cleaned["formats"] = _normalize_formats(cleaned.get("formats"))

    tier = cleaned["tier"]
    if tier not in VALID_TIERS:
        _fail(f"tier must be one of {list(VALID_TIERS)}; got {tier!r}")

    ocr_mode = cleaned["ocr_mode"]
    if ocr_mode not in VALID_OCR_MODES:
        _fail(f"ocr_mode must be one of {list(VALID_OCR_MODES)}; got {ocr_mode!r}")

    # rp_validator skips its own type check when the value is already an
    # instance of the declared default's type, and `isinstance(True, int)` is
    # True — so `start_page: true` reached this resolver and was used as page 1.
    # Driven off INPUT_SCHEMA rather than a list of field names: a later int
    # field would otherwise arrive unguarded, and silently, because nothing
    # ties a hand-written list back to the declared types.
    for field, spec in INPUT_SCHEMA.items():
        if spec["type"] is int and isinstance(cleaned.get(field), bool):
            _fail(
                f"{field} must be an integer, not a boolean; "
                f"got {cleaned[field]!r}"
            )

    start_page = cleaned.get("start_page", 0) or 0
    if start_page < 0:
        _fail(f"start_page must be >= 0; got {start_page!r}")

    # end_page is 0-based and inclusive; any negative value is the "to the end
    # of the document" sentinel (-1 is the documented spelling). A bounded
    # range that ends before it starts is a caller mistake — MinerU would
    # return an empty parse and the caller would have nothing to go on.
    end_page = cleaned.get("end_page")
    if end_page is not None and end_page >= 0:
        if end_page < start_page:
            _fail(
                f"end_page must be >= start_page when set; "
                f"got start_page={start_page}, end_page={end_page}"
            )
        # Only an explicit range has a page count at this point — an
        # open-ended one isn't known until MinerU opens the document, so the
        # ceiling can't speak to it. Operators who set it are expected to pair
        # it with callers that request ranges (see the scaling guide).
        ceiling = _max_pages_per_job()
        requested = end_page - start_page + 1
        if ceiling and requested > ceiling:
            _fail(
                f"requested page range is {requested} pages; this endpoint "
                f"allows at most {ceiling} per job "
                f"(MINERU_MAX_PAGES_PER_JOB)"
            )

    # XOR over the three transports. The handler also relies on this — only
    # one of file_url/file_b64/volume_path may be set per job.
    sources = [k for k in ("file_url", "file_b64", "volume_path") if cleaned.get(k)]
    if len(sources) != 1:
        _fail(
            f"must provide exactly one of file_url / file_b64 / volume_path "
            f"(got {sources!r})"
        )

    if file_url := cleaned.get("file_url"):
        try:
            _net.require_http_url(file_url, field="file_url")
        except ValueError as e:
            _fail(str(e))

    return cleaned
