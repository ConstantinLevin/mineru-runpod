"""Handler-side unit tests. Exercise the parts that don't need a GPU or MinerU. -- packaging."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tarfile
from pathlib import Path

import pytest

import handler
from worker import envelope


def _seed_mineru_output(dir_: Path, basename: str) -> None:
    """MinerU 4's saved files; `basename` is unused, MinerU names its own."""
    (dir_ / "markdown.md").write_text("# heading\n\nbody\n", encoding="utf-8")
    (dir_ / "markdown_full.md").write_text("# heading\n\nbody\n\n1\n", encoding="utf-8")
    (dir_ / "structured_content.json").write_text(
        json.dumps({"blocks": [{"type": "text", "text": "body"}]}), encoding="utf-8"
    )
    (dir_ / "middle_json.json").write_text(json.dumps({"k": 1}), encoding="utf-8")
    (dir_ / "images").mkdir()
    (dir_ / "images" / "fig1.png").write_bytes(b"\x89PNG fake")


def test_package_tarball_includes_all_artefacts(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")

    encoded = handler._package_tarball(out)
    raw = base64.b64decode(encoded)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {"markdown.md", "markdown_full.md", "structured_content.json", "middle_json.json"} <= names
    assert "images/fig1.png" in names or "images" in names


def test_package_inline_returns_full_payload(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")

    pkg = envelope._package_inline(out, "doc")
    assert pkg["markdown"].startswith("# heading")
    assert pkg["markdown_full"].endswith("1\n")
    assert pkg["structured_content"]["blocks"][0]["text"] == "body"
    assert pkg["middle_json"]["k"] == 1
    assert pkg["model_output"] == {}, "absent: MinerU wrote no raw model output"
    assert "fig1.png" in pkg["images"]
    assert base64.b64decode(pkg["images"]["fig1.png"]) == b"\x89PNG fake"


def test_validate_input_rejects_invalid_transport_value():
    with pytest.raises(ValueError, match="input validation"):
        handler._validate_input({"file_b64": "AA==", "transport": "tarball-xml"})


def test_validate_input_defaults_applied():
    cleaned = handler._validate_input({"file_b64": "AA=="})
    assert cleaned["start_page"] == 0
    assert cleaned["end_page"] == -1
    assert cleaned["tier"] == "advanced"
    assert cleaned["ocr_mode"] == "ocr"
    assert cleaned["image_analysis"] is True
    assert cleaned["transport"] == "tarball_b64"
    assert cleaned["formats"] == [
        "markdown", "markdown_full", "middle_json", "structured_content",
        "model_output", "images",
    ]
    assert cleaned["basename"] == "doc"


def test_validate_input_accepts_s3_transport():
    cleaned = handler._validate_input({"file_b64": "AA==", "transport": "s3"})
    assert cleaned["transport"] == "s3"


def test_package_s3_requires_bucket_env_vars(tmp_path, monkeypatch):
    # Strip any leaked credentials from the test process.
    for var in (
        "BUCKET_ENDPOINT_URL",
        "BUCKET_NAME",
        "BUCKET_ACCESS_KEY_ID",
        "BUCKET_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    with pytest.raises(ValueError, match="BUCKET_"):
        handler._package_s3(out, "doc")


def test_package_s3_complains_about_each_missing_env_var(tmp_path, monkeypatch):
    # All four names should be mentioned in the error.
    for var in (
        "BUCKET_ENDPOINT_URL",
        "BUCKET_NAME",
        "BUCKET_ACCESS_KEY_ID",
        "BUCKET_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    with pytest.raises(ValueError) as excinfo:
        handler._package_s3(out, "doc")
    msg = str(excinfo.value)
    assert "BUCKET_ENDPOINT_URL" in msg
    assert "BUCKET_NAME" in msg
    assert "BUCKET_ACCESS_KEY_ID" in msg
    assert "BUCKET_SECRET_ACCESS_KEY" in msg


def test_package_s3_signs_with_the_configured_lifetime(tmp_path, monkeypatch):
    """The knob has to reach the signing call and the reported value, not just
    resolve correctly on its own."""
    # package_s3 imports boto3 lazily, so the patch goes on the module itself.
    import boto3  # noqa: PLC0415

    recorded = {}

    class _FakeS3:
        def put_object(self, **kwargs):
            recorded["key"] = kwargs["Key"]

        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
            recorded["op"] = op
            recorded["expires_in"] = ExpiresIn
            return f"https://bucket.example/{Params['Key']}?signature=x"

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeS3())
    for var, val in (
        ("BUCKET_ENDPOINT_URL", "https://bucket.example"),
        ("BUCKET_NAME", "parses"),
        ("BUCKET_ACCESS_KEY_ID", "id"),
        ("BUCKET_SECRET_ACCESS_KEY", "secret"),
        ("BUCKET_PRESIGN_TTL_SECONDS", "900"),
    ):
        monkeypatch.setenv(var, val)

    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    result = handler._package_s3(out, "doc")

    assert recorded["expires_in"] == 900
    assert result["tarball_url_expires_in"] == 900
    assert result["bucket_key"] == recorded["key"]
    assert result["bucket_bytes"] > 0


def test_build_tarball_bytes_roundtrip(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    data = handler._build_tarball_bytes(out)
    # Should be valid gzip-tar with the seeded files inside.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "markdown.md" in names
    assert "structured_content.json" in names


def test_build_zip_bytes_roundtrip(tmp_path):
    import zipfile  # noqa: PLC0415

    out = tmp_path / "out"
    out.mkdir()
    _seed_mineru_output(out, "doc")
    data = handler._build_zip_bytes(out)
    assert data[:4] == b"PK\x03\x04"  # zip magic
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "markdown.md" in names
    assert "structured_content.json" in names
    assert "images/fig1.png" in names


# -----------------------------------------------------------------------------
# What the worker's own parse code leaves in the output directory
# (stand-in package: `fake_mineru`, conftest.py)
# -----------------------------------------------------------------------------


def _read_inline(**job_input) -> dict:
    payload = base64.b64encode(b"%PDF-1.7 fake").decode("ascii")
    return asyncio.run(handler.handler({
        "input": {"file_b64": payload, "transport": "inline", **job_input}
    }))


def test_the_result_is_saved_in_minerus_own_layout(fake_mineru):
    entry = _read_inline()["results"][0]
    assert entry["markdown"] == "# body\n"
    assert entry["middle_json"]["extensions"]
    assert entry["structured_content"] == {}
    assert base64.b64decode(entry["images"]["page_0_image_1.jpg"]) == b"\xff\xd8jpeg"


def test_the_full_mode_markdown_is_written_beside_the_default_one(fake_mineru):
    entry = _read_inline()["results"][0]
    assert entry["markdown_full"] == "rendered:full"


# -----------------------------------------------------------------------------
# The full-mode markdown is the worker's own file
# -----------------------------------------------------------------------------

def test_a_full_mode_render_that_fails_costs_that_file_not_the_parse(fake_mineru, monkeypatch):
    """`markdown_full` is built by the worker after MinerU has saved the result it
    was paid for. If rendering it fails, the saved files still ship and the loss is
    reported rather than dropped."""
    def broken(self, *, mode=None, **_unused):
        if mode is not None and mode.value == "full":
            raise RuntimeError("cannot render this document in full mode")
        return "default"

    monkeypatch.setattr(fake_mineru.result_type, "markdown", broken)
    result = _read_inline()

    assert result["ok"] is True
    entry = result["results"][0]
    assert entry["markdown"] == "# body\n"
    assert entry["markdown_full"] == ""  # nothing of it ships
    assert entry["degraded"]["count"] == 1
    (item,) = entry["degraded"]["items"]
    assert (item["artifact"], item["file"], item["reason"], item["error_type"]) == (
        "markdown_full", "markdown_full.md", "unreadable", "RuntimeError",
    )


def test_a_full_mode_file_that_fails_midway_is_not_left_behind(fake_mineru, monkeypatch):
    """A half-written markdown_full.md would ship and read as the document."""
    real_write = Path.write_text

    def failing_write(self, data, *args, **kwargs):
        if self.name == "markdown_full.md":
            real_write(self, "# h", *args, **kwargs)
            raise OSError("no space left on device")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    entry = _read_inline()["results"][0]
    assert entry["markdown_full"] == ""  # nothing of it ships
    assert entry["degraded"]["count"] == 1


def test_a_cleanup_that_cannot_run_does_not_turn_the_loss_into_a_failed_job(fake_mineru, monkeypatch):
    """Removing the half-written file is best effort. If the removal itself raises,
    the parse MinerU already paid for must still be returned."""
    real_unlink = Path.unlink

    def broken_render(self, *, mode=None, **_unused):
        raise RuntimeError("cannot render")

    def refusing_unlink(self, *args, **kwargs):
        if self.name == "markdown_full.md":
            raise PermissionError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(fake_mineru.result_type, "markdown", broken_render)
    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    result = _read_inline()
    assert result["ok"] is True
    assert result["results"][0]["markdown"] == "# body\n"
    assert result["results"][0]["degraded"]["count"] == 1
