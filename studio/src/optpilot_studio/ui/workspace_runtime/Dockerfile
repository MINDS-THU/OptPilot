ARG BASE_IMAGE=ghcr.io/coder/code-server:latest
ARG NODE_VERSION=22.12.0
ARG TARGETARCH
FROM ${BASE_IMAGE}

ARG NODE_VERSION=22.12.0
ARG TARGETARCH

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
        amd64) node_arch="x64" ;; \
        arm64|aarch64) node_arch="arm64" ;; \
        *) echo "Unsupported Node architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

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
