# ── stablecoin_arbi runtime image (one image, many commands via entrypoint) ──
FROM python:3.12-slim

WORKDIR /app

# Install the package + deps first (layer-cached unless deps change)
COPY pyproject.toml requirements.txt ./
COPY src/ src/
COPY config/ config/
RUN pip install --no-cache-dir -e .

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Make path resolution explicit regardless of install mode / cwd
ENV SCA_CONFIG=/app/config/strategy.yaml \
    SCA_DATA_DIR=/app/data \
    SCA_OUT_DIR=/app/out \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["dryrun"]
