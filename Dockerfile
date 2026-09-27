# syntax=docker/dockerfile:1.7
# One image for every Python process: api, engine, init job and load generator.

# Base images are pinned by digest (Dependabot proposes updates): a rebuild gets the same bytes.
ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first: this layer is reused until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project --extra tracing --extra load
COPY pyproject.toml uv.lock README.md ./
COPY perimeter ./perimeter
# Always rebuild the project's own wheel: uv keys cached builds of a local project on its
# pyproject.toml, so the shared cache mount would otherwise serve a stale build of changed sources.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra tracing --extra load \
        --reinstall-package perimeter

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="perimeter" \
      org.opencontainers.image.description="Live device tracking and geofence alerting"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# /secrets is where the one-shot secrets job mounts its volume: a fresh volume inherits this owner
# and mode, so the job can populate it without root or any capability.
RUN groupadd --system --gid 10001 perimeter \
 && useradd --system --uid 10001 --gid perimeter --no-create-home --shell /usr/sbin/nologin perimeter \
 && install -d -o perimeter -g perimeter -m 0700 /secrets
COPY --from=build /opt/venv /opt/venv
COPY generator.py /app/generator.py
# The end-to-end checks run from this image too, inside the stack's network: `make smoke`.
COPY scripts/smoke.py scripts/viewers.py /app/scripts/
WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "perimeter.api"]
