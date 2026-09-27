# syntax=docker/dockerfile:1.7
# One image for every Python process: api, engine, init job and load generator.

ARG PYTHON_VERSION=3.14.7

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS build
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
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra tracing --extra load

FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
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
WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "perimeter.api"]
