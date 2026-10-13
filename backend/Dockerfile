FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN useradd --create-home --uid 10001 runtime && mkdir -p /data && chown runtime:runtime /data
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-dev --no-install-project
RUN python -c "import ctypes; ctypes.CDLL('libseccomp.so.2')" \
    || (sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
        && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 update \
        && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 install --no-install-recommends -y libseccomp2 \
        && rm -rf /var/lib/apt/lists/*)
COPY backend/app /app/app
COPY third_party/licenses /app/third_party/licenses
ENV PATH="/app/.venv/bin:${PATH}"
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
