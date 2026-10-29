# =============================================================================
# OSS Scanner image (https://github.com/anthropics/oss-scanner)
# =============================================================================
# Not a runtime image: the production build is ../Dockerfile. This image holds
# the full source tree with every dependency the backend, gateway and frontend
# test suites need, because the scanner audits the built image with no network
# access. Build context is the repository root; Dockerfile.dockerignore next to
# this file replaces the root .dockerignore, which drops docs/ and .github/ that
# the scanner should still be able to read.
#
# node:24 matches CI's Node version; bookworm's system Python is 3.11, which is
# what CI and the production image use.
# =============================================================================
FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-dev \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . /src

# Backend and gateway get separate virtualenvs, as in CI: the gateway pins its
# own hashed lockfile that need not agree with the backend's ranges. The ML
# extras (torch) are left out; the suite runs on its mock embedding paths.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r backend/requirements-dev.txt \
    && python3 -m venv /opt/gateway-venv \
    && /opt/gateway-venv/bin/pip install --no-cache-dir \
        -r services/mcp_oauth_gateway/requirements-dev.txt

RUN npm ci --no-audit --no-fund

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/src \
    PYTHONDONTWRITEBYTECODE=1

# Smoke-check that the suites run offline-ready; a failure here must not block
# the image, since the scanner only needs the environment.
RUN (pytest backend/ -q -x -p no:cacheprovider || true) \
    && (cd services/mcp_oauth_gateway \
        && /opt/gateway-venv/bin/pytest test_oauth_flow.py test_upstream_auth.py test_lockfiles.py -q -p no:cacheprovider \
        || true) \
    && (npm run test:unit || true)
