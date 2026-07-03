FROM python:3.14-slim

# uv
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

WORKDIR /app

# Install dependencies first for better layer caching (uses the committed uv.lock).
# pydantic 2.12 / pydantic-core 2.41 ship cp314 wheels, so no compiler is needed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source and helper scripts.
COPY src/ ./src/
COPY scripts/ ./scripts/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8001

CMD ["uvicorn", "app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8001"]
