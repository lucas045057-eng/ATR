FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn[standard]>=0.32,<1" "pymysql>=1.1,<2"
COPY app.py /app/app.py
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
