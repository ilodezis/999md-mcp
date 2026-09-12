FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py remote.py ./

EXPOSE 8013
CMD [".venv/bin/uvicorn", "remote:app", "--host", "0.0.0.0", "--port", "8013", "--proxy-headers", "--forwarded-allow-ips", "*"]
