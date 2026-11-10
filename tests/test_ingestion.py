import stat
import tarfile
import zipfile

import pytest
from app.business.ingestion import UnsafeArtifact, unpack
from app.business.static import inspect_file

LIMITS = {
    "timeout": 5,
    "depth": 16,
    "files": 2000,
    "file_bytes": 100000,
    "total_bytes": 200000,
    "read_bytes": 256000,
}


def make_zip(tmp_path, files):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return source


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "/etc/passwd",
        "C:/bad",
        "a\\..\\bad",
        "CON.txt",
        "a/../bad",
        "a/./bad",
        "evil\x01.py",
    ],
)
def test_zip_slip(tmp_path, name):
    with pytest.raises(UnsafeArtifact):
        unpack(make_zip(tmp_path, {name: "bad"}), tmp_path / "files", "s.zip", LIMITS)
    assert not (tmp_path / "outside.py").exists()


def test_links_and_bomb(tmp_path):
    source = tmp_path / "link.zip"
    with zipfile.ZipFile(source, "w") as z:
        member = zipfile.ZipInfo("link")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(member, "/etc/passwd")
    with pytest.raises(UnsafeArtifact):
        unpack(source, tmp_path / "link", "s.zip", LIMITS)
    source = tmp_path / "bomb.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("large.txt", "x" * 100000)
    with pytest.raises(UnsafeArtifact):
        unpack(source, tmp_path / "bomb", "s.zip", LIMITS)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_tar_special_files(tmp_path, kind):
    source = tmp_path / "bad.tar"
    with tarfile.open(source, "w") as t:
        member = tarfile.TarInfo("bad")
        member.type = kind
        member.linkname = "/etc/passwd"
        t.addfile(member)
    with pytest.raises(UnsafeArtifact):
        unpack(source, tmp_path / "files", "s.tar", LIMITS)


def test_complex_code_is_indexed_never_executed(tmp_path):
    source = make_zip(
        tmp_path,
        {
            "src/app.py": "raise RuntimeError('MUST NEVER EXECUTE')\nimport flask\n@app.get('/health')\ndef health(): return 'ok'",
            "web/server.js": "import express from 'express'; app.get('/record', fn)",
            "compose.yaml": "services:\n  api: {build: ., depends_on: [store]}\n  store: {image: mysql:8.4}",
        },
    )
    rows = unpack(source, tmp_path / "files", "s.zip", LIMITS)
    parsed = {
        row["path"]: inspect_file(tmp_path / "files" / row["path"], row["path"], 256000)
        for row in rows
    }
    assert parsed["src/app.py"]["routes"][0]["path"] == "/health"
    assert len(parsed["compose.yaml"]["services"]) == 2


def test_limits_and_masquerade(tmp_path):
    with pytest.raises(UnsafeArtifact):
        unpack(
            make_zip(tmp_path, {"a.py": "x", "b.py": "y"}),
            tmp_path / "many",
            "s.zip",
            {**LIMITS, "files": 1},
        )
    fake = tmp_path / "fake.py"
    fake.write_bytes(b"PK\x03\x04\x00")
    with pytest.raises(UnsafeArtifact):
        inspect_file(fake, "fake.py", 10000)
    text = tmp_path / "bad.yaml"
    text.write_text("x: &x [a]\ny: *x")
    assert inspect_file(text, "bad.yaml", 10000)["parse_state"] == "partial_parser_error"


def test_import_rejects_zip_encryption_flag_before_reading(tmp_path):
    source = make_zip(tmp_path, {'plain.txt': 'fixture'})
    data = bytearray(source.read_bytes())
    central = data.index(b'PK\x01\x02')
    data[6] |= 1
    data[central + 8] |= 1
    source.write_bytes(data)
    with pytest.raises(UnsafeArtifact, match='加密'):
        unpack(source, tmp_path / 'encrypted', 'source.zip', LIMITS)


@pytest.mark.parametrize('kind', ['zip', 'tar', 'tar.gz'])
def test_import_rejects_casefold_duplicate_archive_paths(tmp_path, kind):
    import io

    source = tmp_path / ('source.' + kind)
    if kind == 'zip':
        with zipfile.ZipFile(source, 'w') as archive:
            for name in ['source.txt', 'SOURCE.txt']:
                archive.writestr(name, 'fixture')
    else:
        with tarfile.open(source, 'w:gz' if kind == 'tar.gz' else 'w') as archive:
            for name in ['source.txt', 'SOURCE.txt']:
                member = tarfile.TarInfo(name)
                member.size = 7
                archive.addfile(member, io.BytesIO(b'fixture'))
    with pytest.raises(UnsafeArtifact, match='重复路径'):
        unpack(source, tmp_path / 'duplicate', source.name, LIMITS)


@pytest.mark.parametrize('files,limits', [
    ({'a/b/c.txt': 'x'}, {'depth': 2}),
    ({'a.txt': 'xxx'}, {'file_bytes': 2}),
    ({'a.txt': 'xx', 'b.txt': 'xx'}, {'total_bytes': 3}),
])
def test_import_archive_depth_and_byte_limits(tmp_path, files, limits):
    source = make_zip(tmp_path, files)
    with pytest.raises(UnsafeArtifact, match='超限'):
        unpack(source, tmp_path / 'limited', 'source.zip', {**LIMITS, **limits})


def test_import_archive_timeout_is_enforced_before_reading(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = make_zip(tmp_path, {'a.txt': 'fixture'})
    ticks = iter([0, 10])
    monkeypatch.setattr('app.business.ingestion.time', SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(UnsafeArtifact, match='耗时超限'):
        unpack(source, tmp_path / 'timeout', 'source.zip', LIMITS)
