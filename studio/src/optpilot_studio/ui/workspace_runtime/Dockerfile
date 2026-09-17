ARG BASE_IMAGE=ghcr.io/coder/code-server:4.137.0@sha256:57ac684d44deb6fa94317b3e8f3e128dd7fb897fffd95b4efcd16f56ce607971
ARG NODE_VERSION=22.19.0
ARG UV_VERSION=0.12.15
ARG TARGETARCH
FROM ${BASE_IMAGE}

ARG NODE_VERSION=22.19.0
ARG NODE_ARM64_SHA256=0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc
ARG NODE_X64_SHA256=c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2
ARG UV_VERSION=0.12.15
ARG UV_ARM64_SHA256=0e9a3499b0587d449c9ff684c0160da607826e4af1cee220bc87f378702d3e08
ARG UV_X64_SHA256=f97935763c04be3e692460a7aaeaaab8fc3b78fcf8b389da820b38ae7423a638
ARG OPTPILOT_RUNTIME_REVISION=unprepared
ARG TARGETARCH

LABEL io.optpilot.workspace-runtime.revision="${OPTPILOT_RUNTIME_REVISION}"

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        python-is-python3 \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        ripgrep \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) node_arch="x64"; node_sha256="${NODE_X64_SHA256}" ;; \
        arm64|aarch64) node_arch="arm64"; node_sha256="${NODE_ARM64_SHA256}" ;; \
        *) echo "Unsupported Node architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -o /tmp/node.tar.xz; \
    echo "${node_sha256}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) uv_arch="x86_64"; uv_sha256="${UV_X64_SHA256}" ;; \
        arm64|aarch64) uv_arch="aarch64"; uv_sha256="${UV_ARM64_SHA256}" ;; \
        *) echo "Unsupported uv architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_arch}-unknown-linux-gnu.tar.gz" -o /tmp/uv.tar.gz; \
    echo "${uv_sha256}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp; \
    install -m 0755 "/tmp/uv-${uv_arch}-unknown-linux-gnu/uv" /usr/local/bin/uv; \
    install -m 0755 "/tmp/uv-${uv_arch}-unknown-linux-gnu/uvx" /usr/local/bin/uvx; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${uv_arch}-unknown-linux-gnu"

RUN code-server --version \
    && python --version \
    && python -m pip --version \
    && node --version \
    && npm --version \
    && uv --version \
    && git --version \
    && rg --version

USER coder

ENV PATH="/home/coder/.local/bin:/usr/local/bin:${PATH}"
