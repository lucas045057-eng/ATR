from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from app.common import digest, dumps
from app.runtime.processes import run_bounded


def package_root():
    for parent in Path(__file__).resolve().parents[2:4]:
        candidate=parent/'third_party'/'archify'
        if (candidate/'manifest.json').is_file():
            return candidate
    raise ValueError('缺少随包 Archify 文件')


def verify_package():
    root=package_root()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest['version']!='3.0.1':
        raise ValueError('Archify 固定版本不匹配')
    for entry in manifest['files']:
        path=(root/'3.0.1'/entry['path']).resolve()
        if not path.is_relative_to((root/'3.0.1').resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
            raise ValueError('Archify 分发文件摘要不匹配')
    return root/'3.0.1'


def render_snapshot(projected: dict, deadline: float, cache_root: Path) -> dict:
    key=digest({'projection':projected,'renderer':'archify-3.0.1'})
    directory=cache_root/key
    output=directory/'diagram.html'
    receipt=directory/'receipt.json'
    if receipt.is_file() and output.is_file():
        metadata=json.loads(receipt.read_text())
        if hashlib.sha256(output.read_bytes()).hexdigest()==metadata['html_sha256']:
            return {**metadata,'html':output.read_text(encoding='utf-8'),'cached':True}
    node=shutil.which('node')
    if not node:
        raise ValueError('本机缺少随包 Node 运行时，结构图尚未生成')
    package=verify_package()
    directory.mkdir(parents=True,exist_ok=True)
    candidate=directory/'candidate.json'
    candidate.write_text(dumps(projected['candidate']),encoding='utf-8')
    remaining=min(30,deadline-time.monotonic())
    result=run_bounded([node,str(package/'renderers'/'architecture'/'render-architecture.mjs'),str(candidate),str(output)],
                       b'{}',timeout=remaining,max_output=1024*1024)
    if result['returncode'] or not output.is_file():
        raise ValueError('结构图渲染失败；原始业务资料和来源已保留')
    raw=output.read_bytes()
    if len(raw)>8*1024*1024 or b'<svg' not in raw:
        raise ValueError('结构图输出超限或缺少有效图形')
    metadata={'snapshot_id':projected['snapshot_id'],'view':projected['view'],'renderer':'archify-3.0.1',
              'html_sha256':hashlib.sha256(raw).hexdigest(),'node_count':projected['node_count'],'edge_count':projected['edge_count']}
    receipt.write_text(dumps(metadata),encoding='utf-8')
    return {**metadata,'html':raw.decode('utf-8'),'cached':False}
