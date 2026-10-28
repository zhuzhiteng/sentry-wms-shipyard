# Keep pg_dump/pg_restore version-compatible with the PostgreSQL 16 server.
# Debian's default postgresql-client can advance to a newer major and produce
# an archive that the production pg_restore 16 binary cannot read.
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY api/ops /app/ops

ENV PYTHONPATH=/app

ENTRYPOINT []
CMD ["python3", "-m", "ops.backup"]
