"""Trusted subprocess entry point with a hard parent timeout; does not import project code."""

import json
import sys
from pathlib import Path

from app.business.ingestion import unpack
from app.business.static import inspect_file


def main():
    from app.business.resources import apply_memory_limit

    apply_memory_limit()
    source, dest, filename, limits_json, manifest, cache_file = sys.argv[1:]
    limits = json.loads(limits_json)
    rows = unpack(Path(source), Path(dest), filename, limits)
    cache = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    for row in rows:
        cached = cache.get(row["path"] + ":" + row["content_digest"])
        row["analysis"] = (
            cached
            if cached is not None
            else inspect_file(Path(dest) / row["path"], row["path"], limits["read_bytes"])
        )
    Path(manifest).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
