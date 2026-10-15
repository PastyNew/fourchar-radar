# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build the wheel in an isolated env so the runtime image carries no build deps.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip build \
 && python -m build --wheel --outdir /dist

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="fourchar-radar" \
      org.opencontainers.image.description="Radar for free 4-character Discord usernames" \
      org.opencontainers.image.source="https://github.com/your-org/fourchar-radar" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RADAR_DB_PATH=/app/state/radar.db

# curl is used by the HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl /tmp/

RUN python -m pip install --no-cache-dir /tmp/*.whl \
 && rm -f /tmp/*.whl

# Run unprivileged. The state directory is the only writable path.
RUN useradd --create-home --uid 10001 radar \
 && mkdir -p /app/state \
 && chown -R radar:radar /app

USER radar
WORKDIR /app
VOLUME ["/app/state"]

# A scan can sit idle for a long time inside a throttle penalty; the default
# compose timeout of 10s would kill the container during that.
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS "file:///app/state/radar.db" >/dev/null 2>&1 || \
      python -c "import pathlib,sqlite3,sys; sqlite3.connect('/app/state/radar.db').execute('select 1'); sys.exit(0)"

ENTRYPOINT ["fourchar-radar"]
CMD ["run", "--watch"]
