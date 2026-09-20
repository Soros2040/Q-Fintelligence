ARG NODE_IMAGE=dockerproxy.net/library/node:24-bookworm-slim
ARG CADDY_IMAGE=docker.m.daocloud.io/library/caddy:2-alpine

FROM ${NODE_IMAGE} AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN sed -i \
        -e 's|http://deb.debian.org/debian-security|http://mirrors.cloud.tencent.com/debian-security|g' \
        -e 's|http://deb.debian.org/debian|http://mirrors.cloud.tencent.com/debian|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        python3 \
        python3-pip \
        python3-venv \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM runtime-base AS builder

ARG NPM_REGISTRY=https://mirrors.tencentyun.com/npm
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY package.json package-lock.json ./
COPY apps/control-plane/package.json apps/control-plane/package.json
COPY apps/web/package.json apps/web/package.json
COPY packages/contracts/package.json packages/contracts/package.json

RUN npm config set registry "${NPM_REGISTRY}" \
    && npm ci

COPY pyproject.toml uv.lock ./
COPY workers ./workers

RUN sed -i \
        -e 's|https://pypi.org/simple|https://pypi.tuna.tsinghua.edu.cn/simple|g' \
        -e 's|https://files.pythonhosted.org/packages/|https://pypi.tuna.tsinghua.edu.cn/packages/|g' \
        uv.lock \
    && python3 -m pip config set global.index-url "${PIP_INDEX_URL}" \
    && python3 -m pip config set global.trusted-host "${PIP_TRUSTED_HOST}" \
    && python3 -m pip install --break-system-packages --no-cache-dir uv==0.11.29 \
    && uv sync --frozen --no-dev --default-index "${PIP_INDEX_URL}" --allow-insecure-host "${PIP_TRUSTED_HOST}"

COPY apps ./apps
COPY packages ./packages
COPY infra ./infra
COPY scripts ./scripts
COPY tsconfig.json .nvmrc .python-version ./

RUN npm run build \
    && npm prune --omit=dev \
    && find /app -type f -name '*.map' -delete

FROM runtime-base AS app

ENV NODE_ENV=production \
    QF_PROCESS_NAMESPACE=qfintelligence \
    QF_API_HOST=127.0.0.1 \
    QF_API_PORT=27872 \
    QF_WEB_PORT=27871 \
    QF_STATE_ROOT=.local/state \
    QF_SQLITE_PATH=.local/state/qfintelligence.sqlite3 \
    QF_ARTIFACT_ROOT=.local/artifacts \
    QF_AGENT_MODE=openhands \
    QF_OPENHANDS_SESSION_ROOT=.local/openhands-sessions \
    QF_OPENHANDS_SIDECAR_PATH=workers/openhands_sidecar/run-isolated.sh \
    QF_SYSTEM_PROMPT_VERSION=qf-openhands-runtime.v1 \
    HARDWARE_MODE=READ_ONLY \
    HARDWARE_TARGET=tianyan176 \
    MAX_NEW_HARDWARE_JOBS=0 \
    SHOTS_PER_JOB=0 \
    GIT_ACTION=NONE

COPY --from=builder --chown=node:node /app /app

RUN mkdir -p /app/.local \
    && chown -R node:node /app/.local

USER node

EXPOSE 27872

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD node -e "fetch('http://127.0.0.1:27872/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "apps/control-plane/dist/server.js"]

FROM ${CADDY_IMAGE} AS web

COPY --from=builder /app/apps/web/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=5 \
    CMD wget -q --spider http://127.0.0.1:27871/ || exit 1
