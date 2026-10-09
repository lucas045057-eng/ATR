FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn[standard]>=0.32,<1" "docker>=7.1,<8"
COPY collector/app /app/app
# A Unix Docker socket is commonly owned by root:docker. This dedicated service
# needs socket access; keep it isolated from the backend and host-published ports.
USER root
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
