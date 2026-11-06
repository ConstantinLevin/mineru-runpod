"""Worker-process behaviors that aren't request-shape semantics.

- Cumulative refresh counters (REFRESH_WORKER_AFTER_JOBS / _PAGES)
- Concurrency modifier env-var parsing (MINERU_MAX_CONCURRENCY)
- SIGTERM shutdown event
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import handler
from worker import lifecycle


def test_erste_bereitschaft_bleibt_auch_bei_null_erhalten(monkeypatch):
    """Wiederaufnahme oder erneutes Markieren darf die erste Bereitschaft nicht ersetzen."""
    now = [50.0]
    monkeypatch.setattr(lifecycle, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(lifecycle, "_process_started_monotonic", 50.0)
    monkeypatch.setattr(lifecycle, "_ready_at_ms", None)
    lifecycle.mark_ready()
    first = lifecycle.process_debug()
    now[0] = 150.0
    lifecycle.mark_ready()
    second = lifecycle.process_debug()
    assert first["ready_at_ms"] == second["ready_at_ms"] == 0
    assert (first["uptime_ms"], second["uptime_ms"]) == (0, 100000)


@pytest.mark.parametrize(
    ("warmup_status", "bereitschaft", "dauer", "eintritt"),
    [
        ("ok", 66000, 2000, [73000, 75000]),
        ("error", 66000, 2000, [73000, 75000]),
        ("skipped", 64000, None, [71000, 73000]),
    ],
)
def test_bereitschaft_nach_vorbereitung_vor_sdk_schleife(
    fake_mineru, monkeypatch, tmp_path, warmup_status, bereitschaft, dauer, eintritt,
):
    """Warmup-Dauer oder erste Jobannahme waeren beide die falsche Messstelle."""
    from runpod.serverless.modules import rp_fitness, rp_ping, rp_scale
    from worker import warmup

    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(lifecycle, "time", clock)
    monkeypatch.setattr(warmup, "time", clock)
    monkeypatch.setattr(lifecycle, "_process_started_monotonic", 50.0)
    monkeypatch.setattr(lifecycle, "_ready_at_ms", None)
    monkeypatch.setattr(lifecycle, "_completed_jobs", 0)
    monkeypatch.setattr(warmup, "_duration_ms", None)
    monkeypatch.setattr(warmup, "_status", "not_run")
    monkeypatch.delenv("MINERU_SKIP_WARMUP", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    if warmup_status == "skipped":
        monkeypatch.setenv("MINERU_SKIP_WARMUP", "1")
    elif warmup_status == "error":
        fake_mineru.state["error"] = RuntimeError("synthetischer Warmup-Fehler")
    fixture = tmp_path / "synthetisch.pdf"
    fixture.write_bytes(b"%PDF-1.4\nsynthetisch")
    monkeypatch.setattr(warmup, "WARMUP_FIXTURE_PATH", fixture)
    parser = sys.modules["mineru.parser"]
    parse_async = parser.parse_async

    async def parse_mit_dauer(*args, **kwargs):
        now[0] += 2.0
        return await parse_async(*args, **kwargs)

    monkeypatch.setattr(parser, "parse_async", parse_mit_dauer)

    async def fitness():
        assert lifecycle.process_debug()["ready_at_ms"] is None
        now[0] += 3.0

    class Heartbeat:
        def start_ping(self):
            assert lifecycle.process_debug()["ready_at_ms"] is None
            now[0] += 4.0

    def signal_vorbereiten(*args):
        assert lifecycle.process_debug()["ready_at_ms"] is None
        now[0] += 1.0

    responses = []

    class JobScaler:
        def __init__(self, config):
            assert lifecycle.process_debug()["ready_at_ms"] is None
            now[0] += 5.0
            self.config = config

        async def run(self):
            process = lifecycle.process_debug()
            assert process["ready_at_ms"] == bereitschaft
            assert process["uptime_ms"] == bereitschaft
            assert process["warmup_ms"] == dauer
            assert process["warmup_status"] == warmup_status
            fake_mineru.state["error"] = None
            now[0] += 7.0
            for _ in range(2):
                responses.append(await self.config["handler"]({
                    "input": {"file_b64": "JVBERi0xLjQK", "transport": "inline"},
                }))

    monkeypatch.setattr(rp_fitness, "run_fitness_checks", fitness)
    monkeypatch.setattr(rp_ping, "Heartbeat", Heartbeat)
    monkeypatch.setattr(rp_scale, "JobScaler", JobScaler)
    monkeypatch.setattr(handler, "signal", SimpleNamespace(
        signal=signal_vorbereiten, SIGTERM=15, SIGINT=2,
    ))
    handler._bootstrap_main()
    assert [r["ok"] for r in responses] == [True, True]
    assert [r["debug"]["process"]["ready_at_ms"] for r in responses] == [bereitschaft, bereitschaft]
    assert [r["debug"]["process"]["uptime_ms"] for r in responses] == eintritt
    assert [r["debug"]["process"]["jobs_completed"] for r in responses] == [0, 1]


@pytest.fixture(autouse=True)
def reset_counters(monkeypatch):
    """Each test starts with clean counters and no thresholds set."""
    lifecycle._jobs_processed = 0
    lifecycle._pages_processed_total = 0
    monkeypatch.delenv("REFRESH_WORKER_AFTER_JOBS", raising=False)
    monkeypatch.delenv("REFRESH_WORKER_AFTER_PAGES", raising=False)
    yield


# -----------------------------------------------------------------------------
# Refresh counters
# -----------------------------------------------------------------------------

def test_refresh_disabled_by_default():
    # With no thresholds set, every job returns None (no recycle).
    for _ in range(5):
        assert handler._record_job(10) is None


def test_refresh_jobs_threshold_crosses(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "3")
    assert handler._record_job(0) is None
    assert handler._record_job(0) is None
    # Third job crosses the jobs threshold — reason identifies which one.
    assert handler._record_job(0) == "jobs_threshold"
    assert lifecycle._jobs_processed == 3


def test_refresh_pages_threshold_crosses(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "50")
    assert handler._record_job(20) is None
    assert handler._record_job(20) is None  # 40 cumulative
    # 60 cumulative crosses 50 — pages reason wins.
    assert handler._record_job(20) == "pages_threshold"
    assert lifecycle._pages_processed_total == 60


def test_refresh_unbounded_jobs_do_not_count_pages(monkeypatch):
    # End-page=-1 jobs pass pages=0; jobs counter still increments.
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "10")
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "2")
    assert handler._record_job(0) is None
    assert handler._record_job(0) == "jobs_threshold"  # job count 2 crosses


def test_refresh_either_threshold_trips(monkeypatch):
    # If BOTH thresholds are set, whichever trips first wins.
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "100")
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "5")
    assert handler._record_job(3) is None       # pages 3
    assert handler._record_job(3) == "pages_threshold"  # pages 6, crosses


def test_refresh_jobs_reason_wins_when_both_trip_same_job(monkeypatch):
    """When jobs AND pages thresholds both trip on the same call, jobs
    wins deterministically — order matches the env-var documentation."""
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "1")
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "1")
    assert handler._record_job(5) == "jobs_threshold"


def test_refresh_malformed_env_var_treated_as_disabled(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "not-a-number")
    assert handler._record_job(0) is None


def test_refresh_worker_signal_via_full_handler_path(monkeypatch):
    """End-to-end: set threshold=1, run handler, confirm refresh_worker key."""
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "1")

    async def fake_run(file_bytes, *, work_dir, **kwargs):
        out = work_dir / "fake-out"
        out.mkdir()
        (out / "markdown.md").write_text("# fake\n", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    result = asyncio.run(handler.handler({
        "input": {"file_b64": "JVBERi0xLjQK", "basename": "test"}  # %PDF-1.4
    }))
    assert result.get("ok") is True
    assert result.get("refresh_worker") is True


# -----------------------------------------------------------------------------
# Concurrency modifier
# -----------------------------------------------------------------------------

def test_concurrency_default_is_one(monkeypatch):
    monkeypatch.delenv("MINERU_MAX_CONCURRENCY", raising=False)
    assert handler._concurrency_modifier(0) == 1


def test_concurrency_from_env(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "3")
    assert handler._concurrency_modifier(0) == 3


def test_concurrency_clamps_to_one(monkeypatch):
    # Zero / negative makes no sense for a serverless worker; coerce to 1.
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "0")
    assert handler._concurrency_modifier(0) == 1
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "-5")
    assert handler._concurrency_modifier(0) == 1


def test_concurrency_malformed_env_var_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "auto")
    assert handler._concurrency_modifier(0) == 1


# -----------------------------------------------------------------------------
# SIGTERM shutdown breadcrumb
# -----------------------------------------------------------------------------

def test_note_shutdown_drains_instead_of_raising(monkeypatch):
    """SIGTERM mid-job must NOT fail the job: RunPod treats a handler error
    as terminal (FAILED, no retry), so the guard logs a breadcrumb and lets
    the accepted job drain to completion."""
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        handler._logging, "warning", lambda msg, **kw: warnings.append((msg, kw))
    )
    handler._shutting_down.set()
    try:
        handler._note_shutdown("parse")  # must not raise
    finally:
        handler._shutting_down.clear()
    assert warnings, "expected a drain breadcrumb to be logged"
    # Loose on the prose, strict on the structured field: `phase` is what the
    # handler passes deliberately and what a regression would silently break.
    assert "continuing to drain" in warnings[0][0]
    assert warnings[0][1].get("phase") == "parse"


def test_note_shutdown_is_noop_when_clear(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(
        handler._logging, "warning", lambda msg, **kw: warnings.append(msg)
    )
    handler._shutting_down.clear()
    handler._note_shutdown("fetch_input")  # should not raise
    assert not warnings


def test_on_sigterm_sets_event():
    handler._shutting_down.clear()
    try:
        handler._on_sigterm(15, None)
        assert handler._shutting_down.is_set()
    finally:
        handler._shutting_down.clear()


# -----------------------------------------------------------------------------
# Egress sizing helper (feeds the bytes_out_total / output_size_bytes metrics)
# -----------------------------------------------------------------------------

def test_measure_output_bytes_tarball():
    """Tarball: the base64 string IS the payload; len() is exact.
    The helper reads through `response["results"][0]` per the unified shape.
    """
    response = {"results": [{"tarball_b64": "QUFB" * 100}]}  # 400-char b64 string
    assert handler._measure_output_bytes(response, "tarball_b64") == 400


def test_measure_output_bytes_inline_sums_markdown_and_images():
    """Inline: dominant fields are the two markdown texts + b64 image strings."""
    response = {"results": [{
        "markdown": "# hello\n",  # 8 utf-8 bytes
        "markdown_full": "# hello\n\n1\n",  # 11 utf-8 bytes
        "images": {"page1.png": "A" * 200, "page2.png": "B" * 300},
        # The JSON files are ignored — their overhead is an estimate's error, not
        # worth re-serializing a whole response to measure.
        "structured_content": {"pages": [{"page_idx": 0, "blocks": []}]},
    }]}
    assert handler._measure_output_bytes(response, "inline") == 8 + 11 + 500


def test_measure_output_bytes_s3_uses_bucket_bytes():
    """S3: package_s3 records the uploaded tarball size in bucket_bytes."""
    response = {"results": [{
        "tarball_url": "https://example.com/x.tar.gz",
        "bucket_bytes": 1024 * 1024,
    }]}
    assert handler._measure_output_bytes(response, "s3") == 1024 * 1024


def test_measure_output_bytes_empty_response_returns_zero():
    """A response missing the expected fields shouldn't produce a misleading
    zero histogram sample — the caller skips the record on out_bytes <= 0.
    Failure responses also have no `results` key and must return 0.
    """
    assert handler._measure_output_bytes({}, "tarball_b64") == 0
    assert handler._measure_output_bytes({}, "inline") == 0
    assert handler._measure_output_bytes({}, "s3") == 0
    assert handler._measure_output_bytes({"results": []}, "tarball_b64") == 0
    assert handler._measure_output_bytes({"results": [{}]}, "tarball_b64") == 0
    assert handler._measure_output_bytes({"random": "shape"}, "unknown") == 0


def test_measure_output_bytes_inline_handles_unicode():
    """Multi-byte chars count as utf-8 bytes, not codepoints."""
    response = {"results": [{"markdown": "héllo", "images": {}}]}
    # 'h' + 'é' (2 bytes) + 'l' + 'l' + 'o' = 6 utf-8 bytes
    assert handler._measure_output_bytes(response, "inline") == 6
