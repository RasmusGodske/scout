# scout as a service: the HTTP API on port 8000.
#
#   docker build -t scout .
#   docker run -p 8000:8000 -e OPENROUTER_API_KEY=... -e SCOUT_API_TOKEN=... -v scout-data:/app/data scout
#
# SCOUT_API_TOKEN set   -> every /v1 call needs `Authorization: Bearer <token>`
# SCOUT_API_TOKEN unset -> the API is open (for local or isolated networks only)
#
# The route cache and the Jev usage log live in /app/data; mount a volume there to keep them.
# Settings come from /app/scout.toml; mount your own over it to change them.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.5.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first, from the lock file, so code changes don't reinstall them.
RUN pip install "poetry==${POETRY_VERSION}"
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

# Chromium's headless shell and its system libraries, from the locked Playwright version.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY scout.toml ./
RUN poetry install --only-root --no-interaction --no-ansi

RUN useradd --create-home --uid 10001 scout \
    && mkdir -p /app/data \
    && chown -R scout:scout /app/data
USER scout
VOLUME ["/app/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["scout", "serve", "--host", "0.0.0.0", "--port", "8000"]
