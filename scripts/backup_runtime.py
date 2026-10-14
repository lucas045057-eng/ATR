"""Private, consistent runtime backup. Run on the original data volume, never publish it."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

PRIVATE_FILES = ('model-configuration.enc', 'model-text-proof.json', 'model-vision-proof.json')


def encode_blob(value):
    if isinstance(value, bytes):
        return {'$sqlite_blob_base64': base64.b64encode(value).decode('ascii')}
    raise TypeError(f'Unsupported SQLite value: {type(value).__name__}')


def encode_rows(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, default=encode_blob, **kwargs)


def private_signature(data):
    signature = {}
    for name in PRIVATE_FILES:
        path = data / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Private configuration path is not a regular file')
        signature[name] = sha(path) if path.is_file() else None
    return signature


def sha(path):
    checksum=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            checksum.update(block)
    return checksum.hexdigest()


def backup(data: Path, destination: Path, application_sha: str, compare: Path | None=None):
    if len(application_sha) not in {7,40} or any(c not in '0123456789abcdef' for c in application_sha):
        raise ValueError('Exact application SHA required')
    data=data.resolve(strict=True)
    database=data/'runtime.sqlite3'
    artifacts=data/'artifacts'
    destination=destination.resolve()
    if destination==data or destination.is_relative_to(artifacts.resolve()):
        raise ValueError('Backup destination cannot overwrite runtime or artifact files')
    if destination.exists() or database.is_symlink() or artifacts.is_symlink():
        raise ValueError('Fresh destination and regular original paths required')
    compare = compare.resolve(strict=True) if compare else None
    compared_sha = sha(compare) if compare else None
    with sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=10) as lock:
        lock.execute('BEGIN IMMEDIATE')
        if lock.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','waiting_input')").fetchone()[0]:
            raise ValueError('Wait for active work or end waiting investigations before backup')
        if lock.execute("SELECT COUNT(*) FROM diagnosis_runs WHERE status IN ('queued','running','waiting_input')").fetchone()[0]:
            raise ValueError('Active investigation prevents consistent backup')
        configuration_before = private_signature(data)
        destination.mkdir(parents=True,mode=0o700,exist_ok=False)
        os.chmod(destination,0o700)
        snapshot=destination/'runtime.sqlite3'
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        os.chmod(snapshot,0o600)
        entries=[]
        artifact_paths = sorted(artifacts.rglob('*'))
        archive_path = destination/'artifacts.tar'
        with tarfile.open(archive_path,'w') as archive:
            os.chmod(archive_path, 0o600)
            for path in artifact_paths:
                if path.is_symlink() or not path.resolve().is_relative_to(artifacts.resolve()):
                    raise ValueError('Artifact symlink or escaped path prevents backup')
                if not path.is_file():
                    if not path.is_dir():
                        raise ValueError('Artifact path is not a regular file or directory')
                    continue
                relative=path.relative_to(data).as_posix()
                entries.append({'path':relative,'bytes':path.stat().st_size,'sha256':sha(path)})
                archive.add(path,arcname=relative,recursive=False)
                if sha(path) != entries[-1]['sha256']:
                    raise ValueError('Artifact changed during backup; retry when idle')
        private_files={}
        for name in PRIVATE_FILES:
            source=data/name
            if source.is_symlink():
                raise ValueError('Private configuration path is not a regular file')
            if source.is_file():
                shutil.copyfile(source,destination/name)
                os.chmod(destination/name,0o600)
                private_files[name]=sha(destination/name)
        if private_signature(data) != configuration_before or any(
            digest != configuration_before[name] for name, digest in private_files.items()
        ):
            raise ValueError('Model configuration changed during backup; retry when idle')
        if sorted(artifacts.rglob('*')) != artifact_paths or any(
            not (data / entry['path']).is_file() or sha(data / entry['path']) != entry['sha256']
            for entry in entries
        ):
            raise ValueError('Artifacts changed during backup; retry when idle')
        exported={}
        with sqlite3.connect(snapshot) as current:
            current.row_factory=sqlite3.Row
            tables=[row[0] for row in current.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            counts={}
            previous=sqlite3.connect(compare.as_uri()+'?mode=ro',uri=True) if compare else None
            try:
                if previous:
                    previous.execute('BEGIN')
                old_tables={r[0] for r in previous.execute("SELECT name FROM sqlite_master WHERE type='table'")} if previous else set()
                for table in tables:
                    quoted='"'+table.replace('"','""')+'"'
                    rows=[dict(row) for row in current.execute('SELECT * FROM '+quoted)]
                    counts[table]=len(rows)
                    if previous:
                        previous.row_factory=sqlite3.Row
                        old=[dict(row) for row in previous.execute('SELECT * FROM '+quoted)] if table in old_tables else []
                        encoded={encode_rows(row,sort_keys=True) for row in old}
                        changed=[row for row in rows if encode_rows(row,sort_keys=True) not in encoded]
                        if changed:
                            exported[table]=changed
            finally:
                if previous:
                    previous.close()
            schema=[dict(row) for row in current.execute('SELECT * FROM schema_migrations')]
        if compare:
            if sha(compare) != compared_sha:
                raise ValueError('Comparison backup changed during export')
            (destination/'new-records.json').write_text(encode_rows(exported),encoding='utf-8')
            os.chmod(destination/'new-records.json',0o600)
        manifest={'schema_version':'atr-private-backup-1','application_sha':application_sha,
                  'created_at':datetime.now(timezone.utc).isoformat(),'schema_migrations':schema,
                  'database_sha256':sha(snapshot),'artifacts':entries,'artifacts_tar_sha256':sha(destination/'artifacts.tar'),
                  'private_configuration_files':private_files,'counts':counts,
                  'comparison_database_sha256':compared_sha, 'blob_json_encoding':'$sqlite_blob_base64',
                  'new_records_sha256':sha(destination/'new-records.json') if compare else None,
                  'new_or_changed_record_counts':{table:len(rows) for table,rows in exported.items()}}
        if private_signature(data) != configuration_before:
            raise ValueError('Model configuration changed before backup completed')
        manifest_path = destination/'manifest.json.tmp'
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        os.chmod(manifest_path,0o600)
        for path in destination.iterdir():
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        manifest_path.replace(destination/'manifest.json')
        directory = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        lock.rollback()
    return {'destination':str(destination),'database_sha256':manifest['database_sha256'],
            'artifact_file_count':len(entries),'new_or_changed_record_counts':manifest['new_or_changed_record_counts']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--application-sha',required=True)
    parser.add_argument('--compare',type=Path)
    args=parser.parse_args()
    print(json.dumps(backup(args.data,args.destination,args.application_sha,args.compare)))


if __name__=='__main__':
    main()
