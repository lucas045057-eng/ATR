import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_release_module():
    spec = importlib.util.spec_from_file_location("atr_build_release", ROOT / "scripts" / "build_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_release_module()


def write(root: Path, name: str, content: str | bytes = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def release_root(tmp_path):
    files = {
        ".env": "SECRET=1",
        ".env.example": "COLLECTOR_TOKEN=\n",
        "data/runtime.sqlite3": b"db",
        "output/old.zip": b"zip",
        "orbital/state.json": "{}",
        "targets/.gitkeep": "",
        "targets/private.txt": "secret",
        "frontend/dist/index.html": "<div id=\"root\"></div>",
        "frontend/dist/build.json": json.dumps(
            {"version": "2.2.0", "commit": "", "built_at": "2026-01-01T00:00:00Z", "source": "npm run build"}
        ),
        "frontend/src/main.tsx": "export {}",
        "frontend/tsconfig.tsbuildinfo": "{}",
        "backend/app/main.py": "app = None",
        "node_modules/pkg/index.js": "module.exports = {}",
        "dist/old.zip": b"zip",
        "install.ps1": "Write-Host hi",
        "install.sh": "#!/bin/sh\n",
        "pyproject.toml": "[project]\nname='atr'\n",
        "uv.lock": "",
        "scripts/atr.ps1": "Write-Host hi",
        "scripts/atr": "#!/bin/sh\n",
        "README.md": "# ATR",
    }
    for name, content in files.items():
        write(tmp_path, name, content)
    return tmp_path


def test_worktree_collection_respects_exclusions(release_root):
    entries = release.collect_worktree_files(release_root)
    names = set(entries)
    assert "frontend/dist/index.html" in names
    assert "frontend/dist/build.json" in names
    assert "backend/app/main.py" in names
    assert "targets/.gitkeep" in names
    assert "install.ps1" in names and "install.sh" in names
    assert ".env" not in names
    assert ".env.example" in names
    assert not any(name.startswith(("data/", "output/", "orbital/", "dist/", "node_modules/")) for name in names)
    assert "targets/private.txt" not in names
    assert "frontend/tsconfig.tsbuildinfo" not in names


def test_build_release_fallback_packages_dist_and_installers(release_root, tmp_path):
    output = tmp_path / "release.zip"
    result = release.build_release(root=release_root, output=output, day="20260101")
    assert result["files"] > 0
    assert result["source"] == "worktree"
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "frontend/dist/index.html" in names
        assert "frontend/dist/build.json" in names
        assert "install.ps1" in names
        assert "install.sh" in names
        assert "targets/.gitkeep" in names
        assert ".env" not in names
        assert "data/runtime.sqlite3" not in names
        assert "output/old.zip" not in names
        assert "dist/old.zip" not in names
        for name in ("install.sh", "scripts/atr"):
            assert (archive.getinfo(name).external_attr >> 16) & 0o777 == 0o755


def test_check_dist_rejects_stale_commit(release_root, capsys):
    write(
        release_root,
        "frontend/dist/build.json",
        json.dumps({"version": "2.2.0", "commit": "a" * 40, "built_at": "2026-01-01T00:00:00Z"}),
    )
    with pytest.raises(release.ReleaseError, match="不一致"):
        release.check_dist(release_root, head="b" * 40)
    release.check_dist(release_root, allow_stale_dist=True, head="b" * 40)
    assert "allow-stale-dist" in capsys.readouterr().out


def test_check_dist_tolerates_missing_manifest_but_requires_index(release_root, capsys):
    (release_root / "frontend" / "dist" / "build.json").unlink()
    release.check_dist(release_root, head="b" * 40)
    assert "build.json" in capsys.readouterr().out
    (release_root / "frontend" / "dist" / "index.html").unlink()
    with pytest.raises(release.ReleaseError, match="index.html"):
        release.check_dist(release_root, head="b" * 40)
