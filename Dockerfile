# Build Stage
FROM node:18-alpine AS builder

WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Copy source code for workspaces
COPY packages ./packages
COPY frontend ./frontend

# Install dependencies
RUN npm ci

# Build web and widget
RUN npm run build:web
RUN npm run build:widget

# Runtime Stage
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements
COPY backend/requirements.txt backend/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend code and config
COPY backend ./backend
COPY config ./config

# Copy built frontend assets from builder
COPY --from=builder /app/frontend/web/dist ./static/web
COPY --from=builder /app/frontend/widget/dist ./static/widget

# Create directory for data persistence
RUN mkdir -p /data

# Environment Variables
ENV PYTHONPATH=/app
ENV WEB_STATIC_PATH=/app/static/web
ENV WIDGET_STATIC_PATH=/app/static/widget
ENV GRAPH_FILE=/data/graph.json
ENV PORT=8000
ENV HOST=0.0.0.0

# Expose port
EXPOSE 8000

# Start server
CMD ["uvicorn", "backend.api_host.server:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
