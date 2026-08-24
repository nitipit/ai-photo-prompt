# syntax=docker/dockerfile:1
# Fedora 44 digest is resolved from the x86_64 Fedora 44 release image.
FROM registry.fedoraproject.org/fedora:44@sha256:e82672761671216fdd87c14d90379ad4368ab0200f072f9dffa5bd0459302f33 AS builder

ARG PI_VERSION=0.84.3
ARG CODEX_VERSION=0.147.0
ARG UV_VERSION=0.8.17
ARG DENO_VERSION=2.7.14
WORKDIR /app
ENV PATH="/opt/venv/bin:/opt/deno/node_modules/.bin:${PATH}"

RUN dnf install -y --setopt=install_weak_deps=False \
      gcc gcc-c++ make nodejs npm python3 python3-devel python3-pip \
    && dnf clean all \
    && rm -rf /var/cache/dnf
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "uv==${UV_VERSION}"
RUN npm install --prefix /opt/deno "deno@${DENO_VERSION}"

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY design ./design
COPY deno.json deno.lock package.json ./
RUN UV_PROJECT_ENVIRONMENT=/opt/venv /opt/venv/bin/uv sync --locked --no-dev
RUN deno task app:build
RUN npm install --prefix /opt/pi "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm install --prefix /opt/codex "@openai/codex@${CODEX_VERSION}"

FROM registry.fedoraproject.org/fedora:44@sha256:e82672761671216fdd87c14d90379ad4368ab0200f072f9dffa5bd0459302f33 AS runtime

WORKDIR /app
ENV PATH="/opt/venv/bin:/opt/pi/node_modules/.bin:/opt/codex/node_modules/.bin:/usr/local/bin:${PATH}" \
    CODEX_HOME=/home/photo-prompt/.pi/codex \
    PHOTO_PROMPT_CONFIG=/etc/photo-prompt/app.toml \
    PYTHONUNBUFFERED=1

RUN dnf install -y --setopt=install_weak_deps=False python3 nodejs \
    && dnf clean all \
    && rm -rf /var/cache/dnf \
    && useradd --system --uid 10001 --home-dir /home/photo-prompt --create-home --shell /sbin/nologin photo-prompt \
    && mkdir -p /etc/photo-prompt /var/lib/photo-prompt/state /var/lib/photo-prompt/generated /home/photo-prompt/.pi/codex /run/photo-prompt/pi-rpc \
    && chown -R photo-prompt:photo-prompt /var/lib/photo-prompt /home/photo-prompt /run/photo-prompt

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/pi /opt/pi
COPY --from=builder /opt/codex /opt/codex
COPY src ./src
COPY --from=builder /app/dist ./dist
COPY conf/app.sample.toml ./conf/app.sample.toml
COPY deploy/codex-bridge.ts ./deploy/codex-bridge.ts

# The active TOML is mounted read-only by Quadlet; no credentials are copied.
RUN chown -R photo-prompt:photo-prompt /app /etc/photo-prompt
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import json,urllib.request; value=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)); raise SystemExit(0 if value.get('ready') is True else 1)"]

ENTRYPOINT ["/opt/venv/bin/uvicorn", "app.server:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
