FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd -m -r -s /bin/false appuser

# Copy project files
COPY pyproject.toml requirements.lock ./
COPY swarmtrader/ swarmtrader/

# Install package with pinned deps
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir .

# Switch to non-root user
USER appuser

# Railway injects $PORT at runtime
EXPOSE ${PORT:-8080}

# Health check (deep check for production)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health?deep=1 || exit 1

ENTRYPOINT ["python", "-m", "swarmtrader.main"]
# Bind to 0.0.0.0 inside container so host can reach it
CMD ["mock", "31536000", "--web", "--web-host", "0.0.0.0", "--pairs", "ETHUSD", "BTCUSD"]
