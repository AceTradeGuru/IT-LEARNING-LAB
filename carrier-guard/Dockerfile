# CarrierGuard production image.
FROM python:3.13-slim

# Fail fast and log immediately — buffered stdout means a crash loop shows you
# an empty log, which is the worst possible time to be missing output.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, as their own layer: application edits then rebuild in
# seconds instead of reinstalling the world on every deploy.
COPY requirements.lock.txt .
RUN pip install --no-deps -r requirements.lock.txt

COPY . .

# Run as a non-root user. A web process that can rewrite its own source is one
# file-write bug away from remote code execution that survives a restart.
RUN useradd --create-home --uid 10001 carrierguard \
    && chown -R carrierguard:carrierguard /app
USER carrierguard

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["./entrypoint.sh"]
