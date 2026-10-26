# Backend Dockerfile — NL-to-Data Insight Platform
#
# Multi-stage build:
#   builder — installs Python dependencies into /install
#   runtime — copies installed packages + source; adds Node.js + mermaid-cli
#
# The runtime image runs as a non-root user (appuser) for security.

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps needed by some packages (pyhive, cryptography, sasl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsasl2-dev \
    libkrb5-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime SASL/Kerberos libraries + Node.js LTS (for mermaid-cli)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsasl2-modules \
    nodejs \
    npm \
    && npm install -g @mermaid-js/mermaid-cli \
    && mmdc --version \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app
RUN mkdir -p /tmp/nl_insight_outputs && chown appuser:appuser /tmp/nl_insight_outputs

# Copy application source
COPY --chown=appuser:appuser app/ ./app/

USER appuser

# Expose port used by uvicorn
EXPOSE 8000

# Health check — hits the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
