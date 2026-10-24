"""Handler-side unit tests. Exercise the parts that don't need a GPU or MinerU. -- envelope."""

from __future__ import annotations

import asyncio
import base64

import pytest
from runpod_doc_worker.transport import io as worker_io

import handler


def test_resolve_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(handler._resolve_input_bytes({}))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(handler._resolve_input_bytes({"file_url": "x", "file_b64": "y"}))


def test_resolve_b64_roundtrip():
    payload = base64.b64encode(b"%PDF-1.4 inline").decode("ascii")
    raw, src = asyncio.run(handler._resolve_input_bytes({"file_b64": payload}))
    assert raw == b"%PDF-1.4 inline"
    assert src == "b64"


def test_resolve_b64_rejects_oversized_payload():
    too_big = base64.b64encode(b"x" * (handler.MAX_INLINE_FILE_MB * 1024 * 1024 + 1)).decode("ascii")
    with pytest.raises(ValueError, match="inline file too large"):
        asyncio.run(handler._resolve_input_bytes({"file_b64": too_big}))


def test_resolve_volume_path_reads_file(tmp_path, monkeypatch):
    # Point the input roots at the pytest tmp dir: its location differs by
    # platform, so pinning it here keeps the test deterministic everywhere.
    monkeypatch.setenv("MINERU_VOLUME_ROOTS", str(tmp_path))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 volume")
    raw, src = asyncio.run(handler._resolve_input_bytes({"volume_path": str(pdf)}))
    assert raw == b"%PDF-1.4 volume"
    assert src.startswith("volume:")


def test_resolve_volume_path_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_VOLUME_ROOTS", str(tmp_path))
    missing = tmp_path / "nope.pdf"
    with pytest.raises(ValueError, match="volume_path not found"):
        asyncio.run(handler._resolve_input_bytes({"volume_path": str(missing)}))


@pytest.mark.parametrize(
    ("source_label", "expected"),
    [
        ("b64", "b64"),
        ("url:https://example.com/report.pdf?token=secret", "url"),
        ("volume:/customer/acme/report.pdf", "volume"),
        ("other:customer-data", "unknown"),
        ("", "unknown"),
    ],
)
def test_telemetry_source_kind_is_bounded(source_label, expected):
    assert worker_io.telemetry_source_kind(source_label) == expected


def test_detect_format_pdf():
    assert handler._detect_format(b"%PDF-1.4\nfoo") == "pdf"


def test_detect_format_image_png():
    assert handler._detect_format(b"\x89PNG\r\n\x1a\nfoo") == "image"


def test_detect_format_image_jpeg():
    assert handler._detect_format(b"\xff\xd8\xff\xe0junk") == "image"


def test_detect_format_ooxml():
    # DOCX/PPTX/XLSX all start with the ZIP magic.
    assert handler._detect_format(b"PK\x03\x04rest") == "ooxml"


def test_detect_format_unknown():
    assert handler._detect_format(b"not a real file") == "unknown"
    assert handler._detect_format(b"") == "unknown"


def test_handler_returns_error_on_bad_input():
    result = asyncio.run(handler.handler({"input": {}}))  # no source provided
    # RunPod-convention: top-level `error` key marks the job FAILED.
    assert "error" in result
    assert result["ok"] is False
    assert "exactly one" in result["error"]
    # Even on error, the metadata fields are present.
    assert "mineru_version" in result
    assert "elapsed_seconds" in result


def test_handler_rejects_bad_basename():
    result = asyncio.run(handler.handler({"input": {"file_b64": "AA==", "basename": "../bad"}}))
    assert "error" in result
    assert result["ok"] is False
    # rp_validator reports its own message; we just check it's about input.
    assert "input validation" in result["error"].lower() or "basename" in result["error"].lower()


def test_handler_failure_response_reports_compacted_text(monkeypatch):
    """Both text fields of a failure response go through the same reduction.

    The reduction itself is the harness's; what this asserts is that the
    handler routes `error` and `traceback` through it, so a signed URL a
    caller passed in does not come back in the response it gets.
    """
    async def boom(*args, **kwargs):
        raise RuntimeError(
            "GET failed for 'https://files.example.com/doc.pdf?sig=abcdef123456'"
        )

    monkeypatch.setattr(worker_io, "resolve_input_bytes", boom)
    result = asyncio.run(handler.handler({
        "id": "compact-test",
        "input": {"file_b64": "JVBERi0xLjQK"},
    }))

    assert result["ok"] is False
    assert "sig=" not in result["error"]
    assert "sig=" not in result["traceback"]
    assert "https://files.example.com/doc.pdf" in result["error"]


def test_validate_input_rejects_overlong_basename():
    with pytest.raises(ValueError, match="basename must be at most"):
        handler._validate_input({"file_b64": "AA==", "basename": "d" * 129})


def test_validate_input_accepts_basename_at_the_limit():
    cleaned = handler._validate_input({"file_b64": "AA==", "basename": "d" * 128})
    assert cleaned["basename"] == "d" * 128


def test_validate_input_backfills_an_empty_basename():
    cleaned = handler._validate_input({"file_b64": "AA==", "basename": ""})
    assert cleaned["basename"] == "doc"



def test_validate_input_rejects_end_page_before_start_page():
    with pytest.raises(ValueError, match="end_page must be >= start_page"):
        handler._validate_input({"file_b64": "AA==", "start_page": 10, "end_page": 4})


def test_validate_input_allows_a_single_page_range():
    cleaned = handler._validate_input(
        {"file_b64": "AA==", "start_page": 4, "end_page": 4}
    )
    assert (cleaned["start_page"], cleaned["end_page"]) == (4, 4)


def test_validate_input_keeps_the_open_ended_range():
    cleaned = handler._validate_input({"file_b64": "AA==", "start_page": 10})
    assert cleaned["end_page"] == -1


def test_page_ceiling_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MINERU_MAX_PAGES_PER_JOB", raising=False)
    cleaned = handler._validate_input(
        {"file_b64": "AA==", "start_page": 0, "end_page": 4999}
    )
    assert cleaned["end_page"] == 4999


def test_page_ceiling_rejects_a_larger_range(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_PAGES_PER_JOB", "50")
    with pytest.raises(ValueError, match="allows at most 50"):
        handler._validate_input({"file_b64": "AA==", "start_page": 0, "end_page": 50})


def test_page_ceiling_allows_a_range_at_the_limit(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_PAGES_PER_JOB", "50")
    cleaned = handler._validate_input(
        {"file_b64": "AA==", "start_page": 10, "end_page": 59}
    )
    assert cleaned["end_page"] == 59


def test_page_ceiling_does_not_apply_to_an_open_ended_range(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_PAGES_PER_JOB", "5")
    cleaned = handler._validate_input({"file_b64": "AA=="})
    assert cleaned["end_page"] == -1


def test_page_ceiling_ignores_a_malformed_value(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_PAGES_PER_JOB", "fifty")
    cleaned = handler._validate_input(
        {"file_b64": "AA==", "start_page": 0, "end_page": 999}
    )
    assert cleaned["end_page"] == 999


def test_resolve_b64_rejects_oversized_encoded_payload():
    # Rejected on the encoded length, before the decoded copy is allocated.
    # One character past the harness's own bound, rather than a copy of its
    # arithmetic that would stop describing it the moment the headroom moved.
    too_big = "A" * (worker_io.max_inline_b64_chars() + 1)
    with pytest.raises(ValueError, match="inline file too large"):
        asyncio.run(handler._resolve_input_bytes({"file_b64": too_big}))


def test_validate_input_rejects_non_http_file_url():
    with pytest.raises(ValueError, match="file_url must be an http"):
        handler._validate_input({"file_url": "file:///etc/hosts"})



@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, 3600),        # unset → the default hour
        ("", 3600),          # blank → the default hour
        ("900", 900),
        ("30", 60),          # below the floor → clamped up
        ("999999999", 604800),  # above the signing maximum → clamped down
        ("soon", 3600),      # unparseable → the default, not a failed job
    ],
)
def test_presign_ttl_resolution(monkeypatch, env_value, expected):
    from runpod_doc_worker.transport import package as worker_package

    if env_value is None:
        monkeypatch.delenv("BUCKET_PRESIGN_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("BUCKET_PRESIGN_TTL_SECONDS", env_value)
    assert worker_package.presign_ttl_seconds() == expected


# -----------------------------------------------------------------------------
# Job contract: how the document is read (MinerU 4)
# -----------------------------------------------------------------------------


def test_the_reading_defaults_are_advanced_forced_ocr_and_image_analysis():
    cleaned = handler._validate_input({"file_b64": "AA=="})
    assert cleaned["tier"] == "advanced"
    assert cleaned["ocr_mode"] == "ocr"
    assert cleaned["image_analysis"] is True


@pytest.mark.parametrize("tier", ["flash", "basic", "standard", "advanced"])
def test_every_tier_is_accepted(tier):
    assert handler._validate_input({"file_b64": "AA==", "tier": tier})["tier"] == tier


@pytest.mark.parametrize("ocr_mode", ["auto", "txt", "ocr"])
def test_every_ocr_mode_is_accepted(ocr_mode):
    cleaned = handler._validate_input({"file_b64": "AA==", "ocr_mode": ocr_mode})
    assert cleaned["ocr_mode"] == ocr_mode


def test_an_unknown_tier_is_rejected_with_the_valid_ones():
    with pytest.raises(ValueError, match="tier must be one of") as caught:
        handler._validate_input({"file_b64": "AA==", "tier": "ultra"})
    assert "advanced" in str(caught.value)


def test_an_unknown_ocr_mode_is_rejected_with_the_valid_ones():
    with pytest.raises(ValueError, match="ocr_mode must be one of") as caught:
        handler._validate_input({"file_b64": "AA==", "ocr_mode": "vlm"})
    assert "auto" in str(caught.value)


def test_image_analysis_can_be_switched_off():
    cleaned = handler._validate_input({"file_b64": "AA==", "image_analysis": False})
    assert cleaned["image_analysis"] is False


# A job that still sends a MinerU 3.4 field is turned away rather than silently
# read with something else: the caller believed they chose how it is read.
@pytest.mark.parametrize(
    ("field", "value", "replacement"),
    [
        ("backend", "vlm-auto-engine", "tier"),
        ("backend", "pipeline", "ocr_mode"),
        ("effort", "high", "image_analysis"),
        ("lang", "ch", "no replacement"),
        ("formula_enable", False, "no replacement"),
        ("table_enable", False, "no replacement"),
        ("server_url", "http://10.1.2.3:8000", "no replacement"),
    ],
)
def test_a_removed_field_is_rejected_and_names_what_replaces_it(field, value, replacement):
    with pytest.raises(ValueError, match=f"{field} was removed") as caught:
        handler._validate_input({"file_b64": "AA==", field: value})
    assert replacement in str(caught.value)


def test_every_removed_field_is_named_at_once():
    with pytest.raises(ValueError) as caught:
        handler._validate_input(
            {"file_b64": "AA==", "backend": "pipeline", "lang": "en", "table_enable": True}
        )
    message = str(caught.value)
    assert "backend was removed" in message
    assert "lang was removed" in message
    assert "table_enable was removed" in message


def test_the_handler_reports_a_removed_field_as_a_failed_job():
    result = asyncio.run(
        handler.handler({"input": {"file_b64": "JVBERi0xLjQK", "backend": "pipeline"}})
    )
    assert result["ok"] is False
    assert "backend was removed" in result["error"]


@pytest.mark.parametrize("field", ["tier", "ocr_mode"])
def test_an_empty_reading_setting_is_rejected_not_defaulted(field):
    """Defaults are for a field that is absent. An empty string is a caller
    mistake that would otherwise be read with a setting they never named."""
    with pytest.raises(ValueError, match=f"{field} must be one of"):
        handler._validate_input({"file_b64": "AA==", field: ""})


def test_the_backend_message_says_where_each_old_backend_went():
    with pytest.raises(ValueError) as caught:
        handler._validate_input({"file_b64": "AA==", "backend": "pipeline"})
    message = str(caught.value)
    assert "pipeline" in message and "tier=basic" in message
    assert "tier=advanced" in message


# What MinerU 4 receives for a job. The stand-in package is `fake_mineru`
# (conftest.py); the worker's own parse code runs for real against it.
PDF = b"%PDF-1.7 fake"


def _read(data: bytes = PDF, **job_input) -> dict:
    payload = base64.b64encode(data).decode("ascii")
    return asyncio.run(handler.handler({"input": {"file_b64": payload, **job_input}}))


def test_a_pdf_is_read_with_the_jobs_settings(fake_mineru):
    assert _read(tier="standard", ocr_mode="txt", image_analysis=False)["ok"] is True
    (call,) = fake_mineru.calls
    assert call["path"].suffix == ".pdf"
    assert call["bytes"] == PDF
    assert (call["tier"], call["ocr_mode"], call["image_analysis"]) == ("standard", "txt", False)


@pytest.mark.parametrize(
    ("job_pages", "page_range"),
    [
        ({}, ""),                                  # the whole document is no range at all
        ({"start_page": 0, "end_page": 4}, "1-5"),  # 0-based inclusive -> 1-based inclusive
        ({"start_page": 2, "end_page": 2}, "3-3"),
        ({"start_page": 3}, "4-r1"),               # open-ended: to MinerU's "last page"
    ],
)
def test_the_page_window_becomes_a_one_based_page_range(fake_mineru, job_pages, page_range):
    _read(**job_pages)
    assert fake_mineru.calls[0]["page_range"] == page_range


def test_a_parse_without_an_importable_mineru_says_why(monkeypatch):
    monkeypatch.setattr("worker.parse.MINERU_AVAILABLE", False)
    monkeypatch.setattr(
        "worker.parse.MINERU_IMPORT_ERROR", "ModuleNotFoundError: no module named docvortex"
    )
    result = _read()
    assert result["ok"] is False
    assert "docvortex" in result["error"]
