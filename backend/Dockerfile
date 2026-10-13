FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y libseccomp2 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 runtime && mkdir -p /data && chown runtime:runtime /data
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-dev --no-install-project
COPY backend/app /app/app
ENV PATH="/app/.venv/bin:${PATH}"
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
