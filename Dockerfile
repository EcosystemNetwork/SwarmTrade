FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY swarmtrader/ swarmtrader/
COPY tests/ tests/

# Install package
RUN pip install --no-cache-dir -e ".[dev]"

# Default port for web dashboard
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["python", "-m", "swarmtrader.main"]
CMD ["mock", "300", "--web"]
