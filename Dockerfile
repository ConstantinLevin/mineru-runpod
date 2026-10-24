# MinerU on RunPod Serverless -- generic document parsing worker.
# MinerU 4.0.x runtime, models of MinerU's own `standard` set baked in.
#
# Base image: vllm/vllm-openai, the one MinerU's own Dockerfile for 4.0.x builds
# on. It bundles CUDA + a working vLLM, which MinerU 4 reuses instead of
# installing its own: MinerU 4.0.x accepts vllm>=0.19.1,<0.29.0 and needs
# transformers>=5.10.1, which the `torch` extra below brings. The previous base
# (vLLM 0.11.2) pins transformers<5 and cannot run MinerU 4.
#
# At runtime: handler.py listens for RunPod jobs, downloads/decodes the input
# document, calls MinerU's async parse, and returns the result as a base64 tarball.
#
# Model weights are baked into the image at build time, exactly the set MinerU
# calls `standard` (`mineru-kit models download --tier standard`, the way MinerU's
# own Dockerfile does it). That one set covers every tier a job can ask for:
#   - flash reads text layers without models, and scans with the small local models
#   - basic uses only the small-model bundle
#   - standard and advanced use the bundle plus the VLM
# so nothing else is baked, and a smaller image shortens every cold start. No tier
# calls a service outside the container.

# The base image, by tag and by digest. The digest is what makes a rebuild reproduce
# the engine: a tag can be re-pushed. Moving it is a deliberate edit, together with
# the CUDA list in .runpod/hub.json and .runpod/tests.json -- this tag is a CUDA 13.0
# image. The `v0.21.0-cu129` tag is MinerU's documented variant for CUDA 12.9 hosts.
ARG VLLM_IMAGE=vllm/vllm-openai:v0.21.0@sha256:a230095847e93bd4df9888b33dab956fa9504537b828a23657d2b26fed57b5c9
FROM ${VLLM_IMAGE}

# HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1 force the HuggingFace libs to read from
# cache only, and MINERU_MODEL_SOURCE=local (set below, after the download) makes
# MinerU itself load the baked models and fail on a missing one instead of
# downloading. Together they keep the worker from reaching out to fetch weights
# at runtime: fail-fast against a misconfigured endpoint.
#
# MinerU's LLM-aided post-processing (title levelling and cross-page table cell
# merging) is off by default, but its default endpoint is an external service
# (dashscope.aliyuncs.com), so both features are pinned off here and a later change
# of MinerU's defaults cannot switch on a call that sends text to a third party.
#
# MinerU's usage telemetry belongs to its document-library server and its CLI
# (mineru.doclib, mineru.cli). This worker calls only mineru.parser.parse_async,
# which does not import them, so nothing is sent; re-check that when MinerU is
# bumped.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    MINERU_HOME=/opt/mineru \
    MINERU_MODEL_SMALL_BACKEND=torch \
    MINERU_MODEL_VLM_ENGINE=vllm \
    MINERU_LLM_AIDED_FEATURES_TITLE_LEVELING=false \
    MINERU_LLM_AIDED_FEATURES_CROSS_PAGE_TABLE_CELL_MERGE=false

# vllm-openai inherits an entrypoint that launches the OpenAI server. Override
# it so our handler can be the process.
ENTRYPOINT []

# System deps. The base image already has CUDA + Python; MinerU's own Dockerfile
# adds only libgl1 (OpenCV). libglib2.0-0 and ca-certificates are kept from before.
# poppler-utils went with MinerU 3.4: MinerU 4 renders PDFs with pypdfium2.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

# Install uv (10x+ faster than pip on resolution-heavy installs like the MinerU
# stack, which churns through pydantic / opencv / numpy version conflicts with
# the base image). Negligible image size (~10 MB) in exchange for a meaningful
# build-time win. The base image's Python is marked externally managed (PEP 668),
# as MinerU's own Dockerfile also has to acknowledge.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir --break-system-packages uv

# Install MinerU + RunPod worker SDK. requirements.txt pins MinerU, mineru-vl-utils,
# docvortex and the RunPod SDK exactly; `mineru[torch]` pulls the Torch small
# models' runtime and transformers 5 and leaves vLLM to the base image.
COPY requirements.txt /worker/requirements.txt
RUN uv pip install --system --break-system-packages --no-cache -r requirements.txt

# Bake MinerU's `standard` model set into $MINERU_HOME/models. Runs AFTER the
# install so mineru-kit exists, and BEFORE the code COPY so iterating on handler
# code doesn't bust this layer.
#
# The two --small-backend / --vlm-engine options name what the runtime will use
# (they match the ENV above), so a build node without a GPU still fetches the right
# weights. HF_XET_HIGH_PERFORMANCE=1 tells the Xet backend (a dependency of
# huggingface-hub) to saturate the build node's bandwidth. HF_HUB_OFFLINE and
# TRANSFORMERS_OFFLINE are "0" inline for this step only -- the image-wide ENV
# keeps them at "1" so runtime stays offline.
#
# MinerU's downloader takes no revision, so the model revisions are pinned by
# verifying instead: the next step fails the build if what was fetched is not the
# commit named here. A moved repository then stops the build rather than changing
# the engine unnoticed; bump the two values together with a check of the outputs.
# hadolint ignore=DL3059
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_XET_HIGH_PERFORMANCE=1 \
    mineru-kit models download --tier standard --small-backend torch --vlm-engine vllm -s huggingface

ARG MINERU_SMALL_MODELS_REVISION=2b3afb86f4d23fa623f6b2f4b2279ed4ef93a89d
ARG MINERU_VLM_REVISION=08aaea840498d49ce16247b6263196cf02814885
COPY scripts/verify_model_revisions.py /tmp/verify_model_revisions.py
RUN python3 /tmp/verify_model_revisions.py \
        "MinerU-4_models_torch=${MINERU_SMALL_MODELS_REVISION}" \
        "MinerU2.5-Pro-2605-1.2B=${MINERU_VLM_REVISION}" \
    && rm /tmp/verify_model_revisions.py \
    && mineru-kit models verify --tier standard --small-backend torch --vlm-engine vllm

# From here on MinerU loads the baked models and never downloads.
ENV MINERU_MODEL_SOURCE=local

# Copy the worker code last so iterating on it doesn't bust the pip or
# model layers. handler.py is the entry point; the worker/ package
# holds the MinerU-specific modules it imports (harness, schema, parse,
# telemetry, warmup) — the engine-agnostic half came from the
# runpod-doc-worker pin in requirements.txt. Both must land at /worker/ so
# `from worker import ...` resolves from the script's directory.
COPY handler.py /worker/handler.py
COPY worker /worker/worker

# Tiny fixture PDF used by local smoke input and optional Hub tests. It is
# copied into /worker/test-fixture.pdf so validations can round-trip a real
# document without adding meaningful image size.
COPY .runpod/test-fixture.pdf /worker/test-fixture.pdf

# RunPod's serverless runtime invokes Python directly. `python3` is what
# vllm/vllm-openai ships on PATH; `python` is not always aliased.
CMD ["python3", "-u", "handler.py"]
