"""What this worker tells the harness about itself.

`runpod-doc-worker` is engine-agnostic: it fetches input bytes, checks
outbound targets, packs responses, emits structured logs and probes the
model cache without knowing what parses the document. The handful of values
it cannot derive — what this worker's operator-facing env vars are called,
where a `volume_path` input may live, what MinerU's weights look like on
disk — are declared here, once, at import.

Importing this module installs the config. `worker/__init__.py` imports it,
so any `from worker import ...` (including `import handler`) configures the
harness before a job can reach it. The harness reads the active config at
call time rather than at import, so ordering beyond that does not matter.

`env_prefix` is why adopting the harness did not disturb a running
endpoint: MINERU_ALLOW_LOCAL_FETCH and MINERU_VOLUME_ROOTS keep the
spellings `.runpod/hub.json` documents them under, because the prefix comes
from here rather than from the package.

MINERU_DISABLE_PROBE is not in that list because the harness does not read
it at all. Who may ask this endpoint for its filesystem layout is decided in
`handler.py`, under a name this repo owns — see `_probe_allowed` there.
"""

from __future__ import annotations

from typing import Any

from runpod_doc_worker import config
from runpod_doc_worker.transport import response_size as _response_size
from runpod_doc_worker.config import DEFAULT_VOLUME_ROOTS
from runpod_doc_worker.contract.artifacts import Artifact


def _telemetry_mirror(level: str, msg: str, fields: dict[str, Any]) -> None:
    """Second sink for log records: the OTLP/HTTP logs exporter.

    Registered as `log_mirror`, so the harness calls this after it has
    written the stdout line. Dormant unless OTEL_EXPORTER_OTLP_ENDPOINT is
    set — `is_enabled()` is what makes the no-telemetry path free.

    The import is local because `worker.telemetry` reaches back into
    `worker.parse` and the harness's redaction, and because a module-level
    import here would run at config time on a path that may never export a
    single record.
    """
    from worker import telemetry as _telemetry  # noqa: PLC0415

    if _telemetry.is_enabled():
        _telemetry.emit_log(level, msg, fields)


config.configure(
    config.WorkerConfig(
        env_prefix="MINERU",
        logger_name="mineru-worker",
        # `/worker` is where the image bakes the Hub validator's fixture
        # (Dockerfile: COPY .runpod/test-fixture.pdf /worker/test-fixture.pdf),
        # which the boot warmup parses. MINERU_VOLUME_ROOTS still replaces
        # the whole list at runtime.
        volume_roots=DEFAULT_VOLUME_ROOTS + ("/worker",),
        # Every response's `debug.model_dir` is a directory in the Hugging Face
        # cache whose name matches this, or null when there is none. It is found
        # by name, so it does not say which weights MinerU loaded: MinerU 4 loads
        # its baked models from its own directory ($MINERU_HOME/models, where
        # `mineru-kit models download` writes them and the build checks their
        # revisions), which the harness does not look into. The image has such a
        # cache directory all the same, and responses of MinerU 4.0.12 report it:
        # /root/.cache/huggingface/hub/models--opendatalab--MinerU2.5-Pro-2605-1.2B.
        model_globs=("models--opendatalab--MinerU*",),
        # The `probe: true` response resolves these in the Hugging Face cache and
        # reports a model that is not complete there as an issue. That cache is not
        # where MinerU 4 loads from, so whether an entry there is complete says
        # nothing about the worker; none are named.
        probe_model_ids=(),
        # Things an operator can get wrong in a way that looks like a missing
        # model. The probe echoes their values: where MinerU looks for its models
        # (MINERU_HOME), that it loads them offline (MINERU_MODEL_SOURCE), which
        # engines it was told to use, and the VRAM it plans with.
        probe_env_keys=(
            "MINERU_HOME",
            "MINERU_MODEL_SOURCE",
            "MINERU_MODEL_SMALL_BACKEND",
            "MINERU_MODEL_VLM_ENGINE",
            "MINERU_VIRTUAL_VRAM_SIZE",
        ),
        log_mirror=_telemetry_mirror,
    )
)


# What MinerU 4 writes into the output directory, and what each file becomes in
# an inline response. Declaration order is the canonical output order: it is
# what `formats` defaults to and what worker.schema validates against, so a
# key added here reaches the input contract with it.
#
# MinerU 4 names its own files (`ParseResult.save`), so no pattern carries the
# job's `basename`; it only labels the response entry now. `markdown_full.md` is
# not MinerU's: the worker renders it from the saved middle JSON in full mode,
# which keeps the headers, footers, page numbers and margin text the default
# markdown leaves out. `model_output.json` exists only when MinerU
# has raw model output to keep.
#
# markdown is required: it is what a caller asked for, and an empty string in
# its place is worth less to them than a failed job they can retry. The others
# are worth shipping without -- a corrupt structured_content still leaves a
# usable document, and one unreadable image should not cost a caller the other
# forty. Those degrade instead, and say so in the response.

# Refuse a response the RunPod gateway would discard rather than have it dropped.
# Over ~20 MB the gateway returns COMPLETED with no `output` key at all, so the
# caller receives None having paid for the parse -- see the harness's packaging
# guide. Off by default there, because a library should not start raising from a
# version bump; on here, because the alternative is a silent failure.
#
# BULKY_ARTIFACT names the `formats` key of the heaviest optional output so the
# refusal can suggest the smallest `formats` change that would fit. For MinerU 4
# that is the middle JSON on the strength of what it holds (every block with its
# spans); which optional file is heaviest on the heaviest scans is still to be
# measured.
_response_size.ENFORCE_RESPONSE_CAP = True
_response_size.BULKY_ARTIFACT = "middle_json"

# The two files the worker itself touches in MinerU's output directory: it reads
# the middle JSON back, and writes the full-mode markdown beside it.
MIDDLE_JSON_FILE = "middle_json.json"
MARKDOWN_FULL_FILE = "markdown_full.md"

MANIFEST = (
    Artifact("markdown", ("markdown.md",), kind="text", required=True),
    Artifact("markdown_full", (MARKDOWN_FULL_FILE,), kind="text"),
    Artifact("middle_json", (MIDDLE_JSON_FILE,), kind="json"),
    Artifact("structured_content", ("structured_content.json",), kind="json"),
    Artifact("model_output", ("model_output.json",), kind="json"),
    Artifact("images", ("images/*",), kind="b64map"),
)
