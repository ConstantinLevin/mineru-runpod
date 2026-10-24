"""Schema validation for the orthogonal `transport` + `formats` job-input fields."""

from __future__ import annotations


import pytest

from worker.schema import VALID_FORMATS, validate_input


# -----------------------------------------------------------------------------
# transport
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("transport", ["tarball_b64", "inline", "s3"])
def test_transport_each_valid_value_accepted(transport):
    cleaned = validate_input({"file_b64": "AA==", "transport": transport})
    assert cleaned["transport"] == transport


def test_transport_default_is_tarball_b64():
    cleaned = validate_input({"file_b64": "AA=="})
    assert cleaned["transport"] == "tarball_b64"


def test_transport_invalid_value_rejected():
    with pytest.raises(ValueError, match="transport must be one of"):
        validate_input({"file_b64": "AA==", "transport": "carrier-pigeon"})


# -----------------------------------------------------------------------------
# archive_format
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["tar.gz", "zip"])
def test_archive_format_each_valid_value_accepted(fmt):
    cleaned = validate_input({"file_b64": "AA==", "archive_format": fmt})
    assert cleaned["archive_format"] == fmt


def test_archive_format_default_is_tar_gz():
    cleaned = validate_input({"file_b64": "AA=="})
    assert cleaned["archive_format"] == "tar.gz"


def test_archive_format_invalid_value_rejected():
    with pytest.raises(ValueError, match="archive_format must be one of"):
        validate_input({"file_b64": "AA==", "archive_format": "rar"})


# -----------------------------------------------------------------------------
# formats
# -----------------------------------------------------------------------------

def test_formats_default_is_all_four():
    cleaned = validate_input({"file_b64": "AA=="})
    assert cleaned["formats"] == list(VALID_FORMATS)


def test_formats_single_member_subset_accepted():
    cleaned = validate_input({"file_b64": "AA==", "formats": ["markdown"]})
    assert cleaned["formats"] == ["markdown"]


def test_formats_two_member_subset_accepted():
    cleaned = validate_input({"file_b64": "AA==", "formats": ["markdown", "images"]})
    assert cleaned["formats"] == ["markdown", "images"]


def test_formats_name_minerus_files_and_the_full_mode_markdown():
    sent = [
        "images", "model_output", "structured_content", "middle_json",
        "markdown_full", "markdown",
    ]
    cleaned = validate_input({"file_b64": "AA==", "formats": sent})
    # Order is preserved as the caller sent it; canonical order is NOT forced.
    assert cleaned["formats"] == sent


@pytest.mark.parametrize("old", ["middle", "content_list"])
def test_a_format_key_from_mineru_3_is_rejected(old):
    with pytest.raises(ValueError, match="formats entry"):
        validate_input({"file_b64": "AA==", "formats": ["markdown", old]})


def test_formats_default_to_every_artifact_in_declaration_order():
    assert validate_input({"file_b64": "AA=="})["formats"] == [
        "markdown", "markdown_full", "middle_json", "structured_content",
        "model_output", "images",
    ]


def test_formats_empty_list_rejected():
    with pytest.raises(ValueError, match="formats must not be empty"):
        validate_input({"file_b64": "AA==", "formats": []})


def test_formats_unknown_member_rejected():
    with pytest.raises(ValueError, match="formats entry"):
        validate_input({"file_b64": "AA==", "formats": ["markdown", "pdf"]})


def test_formats_non_string_member_rejected():
    with pytest.raises(ValueError, match="must be strings"):
        validate_input({"file_b64": "AA==", "formats": ["markdown", 7]})


def test_formats_non_list_rejected():
    # rp_validator catches the type mismatch first (its own error message),
    # then our normalize step would also reject — either way, ValueError fires.
    with pytest.raises(ValueError, match="formats"):
        validate_input({"file_b64": "AA==", "formats": "markdown"})


def test_formats_duplicates_collapsed_preserving_order():
    cleaned = validate_input({
        "file_b64": "AA==",
        "formats": ["markdown", "images", "markdown", "images", "middle_json"],
    })
    assert cleaned["formats"] == ["markdown", "images", "middle_json"]


# -----------------------------------------------------------------------------
# legacy `return` field — rejected by rp_validator as an unknown field
# -----------------------------------------------------------------------------

def test_legacy_return_field_rejected_as_unknown():
    """The old `return` field is gone. rp_validator rejects unknown keys, so
    callers using the old API name get a clear ``Unexpected input`` error
    rather than mysteriously receiving a default-transport response.
    Pre-1.0 cutover — no silent fallback, no migration alias.
    """
    with pytest.raises(ValueError, match="Unexpected input.*return"):
        validate_input({"file_b64": "AA==", "return": "inline"})


# -----------------------------------------------------------------------------
# which files are read, and how they reach MinerU 4 (stand-in: `fake_mineru`)
# -----------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n fake"
DOCX = b"PK\x03\x04 fake docx"


def test_an_image_goes_to_mineru_as_the_file_it_is(fake_mineru, run_job):
    fake_mineru.state["suffix"] = "png"
    assert run_job(PNG)["ok"] is True
    (call,) = fake_mineru.calls
    assert call["path"].suffix == ".png"
    assert call["bytes"] == PNG, "no conversion to PDF on the worker's side"
    assert call["tier"] == "advanced"


def test_a_whole_document_format_is_read_in_tier_flash(fake_mineru, run_job):
    fake_mineru.state["suffix"] = "docx"
    assert run_job(DOCX, tier="advanced")["ok"] is True
    (call,) = fake_mineru.calls
    assert call["path"].suffix == ".docx"
    assert call["tier"] == "flash"


def test_a_file_mineru_does_not_read_is_turned_away_before_any_parse(fake_mineru, run_job):
    fake_mineru.state["suffix"] = "txt"
    result = run_job(b"just text")
    assert result["ok"] is False
    assert "txt" in result["error"]
    assert fake_mineru.calls == []
