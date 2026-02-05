# =============================================================================
# CommunityOverview - Production Dockerfile
# =============================================================================
# Multi-stage build for optimal image size and security
# Compatible with: Docker, Podman, Cloud Run, App Platform, Fly.io, etc.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Frontend Builder
# -----------------------------------------------------------------------------
FROM node:18-alpine AS builder

WORKDIR /app

# Copy package files first for better layer caching
COPY package.json package-lock.json ./

# Copy source code for workspaces
COPY packages ./packages
COPY frontend ./frontend

# Install dependencies
RUN npm ci --ignore-scripts

# Build web and widget
RUN npm run build:web && npm run build:widget

# -----------------------------------------------------------------------------
# Stage 2: Python Runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim

# Labels for container metadata (OCI standard)
LABEL org.opencontainers.image.title="CommunityOverview"
LABEL org.opencontainers.image.description="Community Knowledge Graph with MCP support"
LABEL org.opencontainers.image.source="https://github.com/jakobengdahl/CommunityOverview"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install system dependencies
# curl: for healthchecks
# build-essential: for some Python packages that need compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
# Using UID 1000 for compatibility with most systems
RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser

# Copy backend requirements and install dependencies
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend code and config
COPY backend ./backend
COPY config ./config

# Copy built frontend assets from builder stage
COPY --from=builder /app/frontend/web/dist ./static/web
COPY --from=builder /app/frontend/widget/dist ./static/widget

# Create data directory and set permissions
RUN mkdir -p /data \
    && chown -R appuser:appgroup /app /data

# Environment Variables
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_STATIC_PATH=/app/static/web \
    WIDGET_STATIC_PATH=/app/static/widget \
    GRAPH_FILE=/data/graph.json \
    PORT=8000 \
    HOST=0.0.0.0

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start server
# Using exec form for proper signal handling
CMD ["uvicorn", "backend.api_host.server:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
