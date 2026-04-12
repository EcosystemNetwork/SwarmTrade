FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY swarmtrader/ swarmtrader/

# Install package
RUN pip install --no-cache-dir .

# Railway injects $PORT at runtime
EXPOSE ${PORT:-8080}

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

ENTRYPOINT ["python", "-m", "swarmtrader.main"]
# Run indefinitely (31536000s = 1 year), web dashboard on, use $PORT
CMD ["mock", "31536000", "--web", "--pairs", "ETHUSD", "BTCUSD"]
