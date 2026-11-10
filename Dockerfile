FROM python:3.13-slim

# Same uv as local development, so uv.lock is read the same way.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend ./backend
COPY frontend ./frontend
COPY config.json ./

# Run as a normal user; data/ holds notify state and is a volume (see docker-compose.yml).
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown app /app/data
USER app

EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/status', timeout=4)"]

# 0.0.0.0 inside the container; docker-compose.yml decides what the host exposes.
CMD ["/app/.venv/bin/python", "-m", "backend.monitor.server", "--host", "0.0.0.0", "--port", "8770"]
