from __future__ import annotations

import os

import pymysql
from fastapi import FastAPI, HTTPException

app = FastAPI(title="System A Catalog API")


def connect():
    return pymysql.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"], connect_timeout=3, read_timeout=3, write_timeout=3,
        cursorclass=pymysql.cursors.DictCursor,
    )


@app.on_event("startup")
def verify_dependency_at_startup() -> None:
    try:
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except Exception as exc:
        print(f"startup dependency check failed: {type(exc).__name__}: {exc}", flush=True)
        raise RuntimeError("required datastore unavailable at startup") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ready", "service": "python_api"}


@app.get("/record")
def read_seed_record() -> dict[str, str]:
    try:
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT item_key, display_name FROM catalog_items WHERE item_key=%s", ("demo-1",))
                row = cursor.fetchone()
    except Exception as exc:
        print(f"read-only record query failed: {type(exc).__name__}", flush=True)
        raise HTTPException(status_code=503, detail="catalog dependency unavailable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="seed record not found")
    return {"item_key": row["item_key"], "display_name": row["display_name"]}
