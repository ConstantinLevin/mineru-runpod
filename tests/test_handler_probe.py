"""Handler-side unit tests. Exercise the parts that don't need a GPU or MinerU. -- probe."""

from __future__ import annotations

import asyncio
import sys

import pytest

import handler
from worker import parse


def test_handler_probe_mode_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MINERU_DISABLE_PROBE", "1")
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert result["ok"] is False
    assert "probe is disabled" in result["error"]
    assert "probe" not in result


def test_the_refusal_names_the_knob(monkeypatch):
    """A caller reading this is usually the operator who can act on it."""
    monkeypatch.setenv("MINERU_DISABLE_PROBE", "1")
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert "MINERU_DISABLE_PROBE" in result["error"]


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " 1 "])
def test_every_affirmative_spelling_turns_the_probe_off(monkeypatch, value):
    monkeypatch.setenv("MINERU_DISABLE_PROBE", value)
    assert handler._probe_allowed() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_an_explicit_negative_leaves_the_probe_on(monkeypatch, value):
    monkeypatch.setenv("MINERU_DISABLE_PROBE", value)
    assert handler._probe_allowed() is True


@pytest.mark.parametrize("value", ["maybe", "typo", "disabled", "2"])
def test_an_unrecognised_value_denies(monkeypatch, value):
    """An operator who typed something into this variable meant to turn the
    probe off. A typo must not be what publishes a filesystem dump."""
    monkeypatch.setenv("MINERU_DISABLE_PROBE", value)
    assert handler._probe_allowed() is False


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_value_is_not_a_setting(monkeypatch, value):
    monkeypatch.setenv("MINERU_DISABLE_PROBE", value)
    assert handler._probe_allowed() is True


def test_handler_probe_mode_enabled_by_default(monkeypatch):
    """Unset means available, which is what this endpoint has always done."""
    monkeypatch.delenv("MINERU_DISABLE_PROBE", raising=False)
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert result["ok"] is True


def test_handler_probe_mode_returns_filesystem_dump():
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert result["ok"] is True
    assert "probe" in result
    assert "env" in result["probe"]
    assert "paths" in result["probe"]
    assert "models_found" in result["probe"]
    # Surfaces the MinerU availability flag so a busted import doesn't hide
    # behind a happy ok=true probe response.
    assert "mineru_available" in result
    assert isinstance(result["mineru_available"], bool)


def test_the_probe_says_why_mineru_cannot_be_imported(monkeypatch):
    """`mineru_available: false` alone leaves the operator to guess which import broke."""
    monkeypatch.setattr(parse, "MINERU_AVAILABLE", False)
    monkeypatch.setattr(parse, "MINERU_IMPORT_ERROR", "ModuleNotFoundError: No module named 'docvortex'")
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert result["mineru_import_error"] == "ModuleNotFoundError: No module named 'docvortex'"


def test_the_probe_does_not_report_models_missing_from_the_hf_cache(monkeypatch, tmp_path):
    """MinerU 4 keeps its baked models in $MINERU_HOME/models, never in the Hugging
    Face cache, so asking the harness to resolve them there would report a missing
    model on every healthy worker."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    result = asyncio.run(handler.handler({"input": {"probe": True}}))
    assert result["ok"] is True
    assert result["probe"]["resolution_attempts"] == []


def test_the_probe_reports_where_mineru_looks_for_its_models(monkeypatch):
    monkeypatch.setenv("MINERU_HOME", "/opt/mineru")
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "local")
    env = asyncio.run(handler.handler({"input": {"probe": True}}))["probe"]["env"]
    assert env["MINERU_HOME"] == "/opt/mineru"
    assert env["MINERU_MODEL_SOURCE"] == "local"


# -----------------------------------------------------------------------------
# The version a worker reports: the installed distribution's, not an attribute
# -----------------------------------------------------------------------------


def test_the_reported_version_is_the_installed_one(monkeypatch):
    monkeypatch.setattr(parse.metadata, "version", lambda name: "4.0.12" if name == "mineru" else "x")
    assert parse.installed_version() == "4.0.12"


def test_the_version_is_unknown_only_when_the_package_has_none(monkeypatch):
    def missing(name):
        raise parse.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(parse.metadata, "version", missing)
    assert parse.installed_version() == "unknown"


def test_the_version_survives_a_parser_that_cannot_be_imported(monkeypatch):
    """A broken dependency used to replace the version with an error string in
    every response, which hides the one fact that says what was deployed."""
    monkeypatch.setattr(parse.metadata, "version", lambda name: "4.0.12")
    monkeypatch.setitem(sys.modules, "mineru.parser", None)  # makes the import raise
    available, version, import_error = parse.probe_mineru()
    assert available is False
    assert version == "4.0.12"
    assert "mineru.parser" in import_error
