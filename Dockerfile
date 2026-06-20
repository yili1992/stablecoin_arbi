# ── stablecoin_arbi runtime image (one image, many commands via entrypoint) ──
FROM python:3.12-slim

WORKDIR /app

# 1) DEPENDENCY LAYER FIRST — cached across rebuilds unless requirements.txt changes.
#    A code-only change does NOT invalidate this layer, so the heavy ccxt/numpy/pandas
#    wheel download (slow on a thin link) runs ONCE, not on every build.
#    requirements.txt MUST mirror pyproject [project.dependencies]: the --no-deps install
#    below trusts it to be complete, so a drift here would SILENTLY omit a runtime dep.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 2) SOURCE LAST — a code change re-runs only these cheap layers. --no-deps skips
#    dependency resolution entirely (already satisfied above), so the editable install
#    just registers the package (~instant), with no network round-trip.
COPY pyproject.toml ./
COPY src/ src/
COPY config/ config/
RUN pip install --no-cache-dir --no-deps -e .

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Make path resolution explicit regardless of install mode / cwd
ENV SCA_CONFIG=/app/config/strategy.yaml \
    SCA_DATA_DIR=/app/data \
    SCA_OUT_DIR=/app/out \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["dryrun"]
