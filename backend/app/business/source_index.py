from __future__ import annotations

import hashlib
import json

from app.business.models import CodeContext, CodeSearch
from app.business.service import BusinessService
from app.context.budget import ContextBudgetManager
from app.tools.redaction import redact_condition, redact_source_text


class SourceIndex:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def search(self, business_id, request: CodeSearch, budget_id=None):
        BusinessService(self.db, self.settings).get(business_id)
        conn = self.db.connect()
        try:
            # Quote user input as literal FTS text; never pass FTS operators from the caller.
            query = '"' + request.keyword.replace('"', '""') + '"'
            rows = conn.execute(
                """SELECT f.id AS file_id,f.path,f.content_digest,s.module,s.symbols_json
                FROM source_search q JOIN source_index s ON s.file_id=q.file_id JOIN artifact_files f ON f.id=s.file_id
                WHERE source_search MATCH ? AND f.business_id=? AND f.active=1
                AND (? IS NULL OR s.module=?) ORDER BY rank LIMIT ?""",
                (
                    query,
                    business_id,
                    request.module,
                    request.module,
                    min(request.limit, self.settings.code_search_limit),
                ),
            ).fetchall()
            result = [
                {**dict(r), "symbols": json.loads(r["symbols_json"]), "symbols_json": None}
                for r in rows
            ]
            if budget_id:
                ContextBudgetManager(self.db, self.settings).retrieval(
                    budget_id, len(json.dumps(result).encode())
                )
            return result
        finally:
            conn.close()

    def context(self, business_id, request: CodeContext, budget_id=None):
        service = BusinessService(self.db, self.settings)
        profile = service.get(business_id, request.profile_version)
        if request.file_id not in {r["id"] for r in profile["source_references"]}:
            raise KeyError("源码不属于指定业务画像版本")
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT f.*,s.symbols_json FROM artifact_files f JOIN source_index s ON s.file_id=f.id WHERE f.id=? AND f.business_id=?",
                (request.file_id, business_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise KeyError("没有可读取的源码索引")
        root = service.root / business_id / row["artifact_id"] / "files"
        path = root / row["path"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() for p in path.parents if p != service.root)
        ):
            raise ValueError("源码路径超出受控资料目录")
        with path.open("rb") as stream:
            raw = stream.read(self.settings.artifact_read_bytes + 1)
        if len(raw) > self.settings.artifact_read_bytes:
            raise ValueError("源码超过单文件读取上限")
        if hashlib.sha256(raw).hexdigest() != row["content_digest"]:
            raise ValueError("源码版本摘要不匹配，请重新导入")
        start = request.start_line
        if request.symbol:
            matches = [s for s in json.loads(row['symbols_json']) if s['name'] == request.symbol]
            if not matches:
                raise KeyError("符号不在此文件索引中")
            if len(matches) != 1:
                raise ValueError('符号存在多个声明，请使用来源中的准确行号')
            symbol = matches[0]
            start = symbol["start"]
        # Preserve literal context even when the requested window starts inside a secret.
        lines = redact_source_text(raw.decode("utf-8-sig")).splitlines()
        parsed=json.loads(row['parse_json'])
        for branch in parsed.get('branches',[]):
            # Old parser receipts have broader references; conservatively hide their literals too.
            for reference in branch.get('condition_source_refs',branch.get('source_refs',[])):
                first,last=reference['start_line']-1,min(len(lines),reference['end_line'])
                block=redact_condition('\n'.join(lines[first:last])).split('\n')
                lines[first:last]=block if len(block)==last-first else ['[已隐藏条件文本，原行号保留]']*(last-first)
        for operation in parsed.get('data_operations',[]):
            if operation.get('sensitive'):
                for reference in operation.get('source_refs',[]):
                    for line in range(reference['start_line']-1,min(len(lines),reference['end_line'])):
                        lines[line]='[已隐藏涉及凭据的数据操作，原行号保留]'
        text = "\n".join(lines[start - 1 : start - 1 + request.max_lines])
        if budget_id:
            ContextBudgetManager(self.db, self.settings).retrieval(budget_id, len(text.encode()))
        return {
            "file_id": request.file_id,
            "path": row["path"],
            "source_digest": row["content_digest"],
            "profile_version": profile["profile_version"],
            "start_line": start,
            "end_line": min(len(lines), start - 1 + request.max_lines),
            "text": text,
            "bytes": len(text.encode()),
            "redacted": True,
        }
