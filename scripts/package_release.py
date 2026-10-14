"""Package committed source and exact running Docker images, without private runtime data."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

INVENTORY = r'''
import importlib.metadata, json, platform, subprocess
from pathlib import Path
packages=sorted([{'name':d.metadata['Name'],'version':d.version} for d in importlib.metadata.distributions()],key=lambda d:d['name'].lower())
print(json.dumps({'python_version':platform.python_version(),'node_version':subprocess.check_output(['node','--version'],text=True).strip(),'python_packages':packages}))
'''

LOAD_PS = r'''param([string]$Context = 'default')
$ErrorActionPreference = 'Stop'
$bundleRoot = $PSScriptRoot
$manifest = Get-Content -Raw -LiteralPath (Join-Path $bundleRoot 'manifest.json') | ConvertFrom-Json
foreach ($file in $manifest.files) {
    if ([IO.Path]::IsPathRooted($file.path) -or $file.path.Split('/') -contains '..') { throw 'Unsafe bundle path' }
    $path = Join-Path $bundleRoot $file.path
    if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Bundle symlink refused' }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) { throw ('Checksum mismatch: ' + $file.path) }
}
$engine = docker --context $Context info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') { throw 'A running Linux Docker engine is required' }
foreach ($service in @('api','web')) {
    $image = $manifest.images.$service
    $current = docker --context $Context image inspect --format '{{.Id}}' $image.tag 2>$null
    if ($LASTEXITCODE -eq 0 -and $current -ne $image.id) { throw 'Existing image tag belongs to another image' }
}
docker --context $Context load --input (Join-Path $bundleRoot 'images.tar')
if ($LASTEXITCODE -ne 0) { throw 'Image import failed; original containers were not changed' }
foreach ($service in @('api','web')) {
    $image = $manifest.images.$service
    $actual = docker --context $Context image inspect --format '{{.Id}}' $image.tag
    if ($LASTEXITCODE -ne 0 -or $actual -ne $image.id) { throw 'Imported image identity mismatch' }
}
Write-Output 'Images verified and loaded. Follow README.zh-CN.md to back up and update the existing deployment.'
'''

LOAD_SH = r'''#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
context="${1:-default}"
sha256sum -c SHA256SUMS
test "$(docker --context "$context" info --format '{{.OSType}}')" = linux
while read -r tag expected; do
    actual=$(docker --context "$context" image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)
    test -z "$actual" || test "$actual" = "$expected"
done < images.expected
docker --context "$context" load --input images.tar
while read -r tag expected; do
    test "$(docker --context "$context" image inspect --format '{{.Id}}' "$tag")" = "$expected"
done < images.expected
echo 'Images verified and loaded. Follow README.zh-CN.md to back up and update the existing deployment.'
'''


def command(arguments, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, **kwargs).stdout


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def package(repo: Path, output: Path, context: str, api_sha: str, web_sha: str):
    repo = repo.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise ValueError('A fresh output path is required; existing deliverables are preserved')
    git = ['git', '-C', str(repo)]
    head = command([*git, 'rev-parse', 'HEAD'], text=True).strip()
    command([*git, 'diff', '--exit-code', 'HEAD'])
    api_sha = command([*git, 'rev-parse', api_sha+'^{commit}'], text=True).strip()
    web_sha = command([*git, 'rev-parse', web_sha+'^{commit}'], text=True).strip()
    command([*git, 'diff', '--exit-code', api_sha, head, '--', 'backend', 'pyproject.toml', 'uv.lock', 'third_party'])
    command([*git, 'diff', '--exit-code', web_sha, head, '--', 'frontend'])
    docker = ['docker', '--context', context]
    images = {}
    for service, revision in [('api', api_sha), ('web', web_sha)]:
        container = 'atr-runtime-'+service+'-1'
        value = json.loads(command([*docker, 'inspect', container], text=True))[0]
        if value['Config']['Labels'].get('com.docker.compose.project') != 'atr-runtime':
            raise ValueError('Original atr-runtime deployment required')
        image_id = value['Image']
        metadata = json.loads(command([*docker, 'image', 'inspect', image_id], text=True))[0]
        if (metadata['Os'], metadata['Architecture']) != ('linux', 'amd64'):
            raise ValueError('This bundle requires Linux amd64 images')
        tag = f'atr-runtime-{service}:ab-{revision[:7]}-{image_id[7:19]}'
        existing = subprocess.run([*docker, 'image', 'inspect', '--format', '{{.Id}}', tag], capture_output=True, text=True)
        if existing.returncode == 0 and existing.stdout.strip() != image_id:
            raise ValueError('Release tag already belongs to a different image')
        command([*docker, 'tag', image_id, tag])
        images[service] = {'id':image_id, 'tag':tag, 'application_sha':revision, 'platform':'linux/amd64'}
    inventory = json.loads(command([*docker, 'exec', '-i', 'atr-runtime-api-1', 'python', '-'], input=INVENTORY.encode()))
    with tempfile.TemporaryDirectory(prefix='atr-offline-', dir=output.parent) as folder:
        root = Path(folder)
        source = root/'source.tar.gz'
        command([*git, 'archive', '--format=tar.gz', '--output='+str(source), head])
        with tarfile.open(source, 'r:gz') as archive:
            if any(member.issym() or member.islnk() or Path(member.name).name == '.env'
                   or 'runtime.sqlite3' in member.name or '.pytest_tmp' in member.name
                   for member in archive.getmembers()):
                raise ValueError('Private data or unreviewed links in committed source')
        command([*docker, 'save', '--output', str(root/'images.tar'), *(image['tag'] for image in images.values())])
        (root/'README.zh-CN.md').write_bytes((repo/'docs/ab-v1/install-upgrade-rollback.md').read_bytes())
        (root/'runtime-inventory.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')
        (root/'load-images.ps1').write_text(LOAD_PS,encoding='utf-8')
        (root/'load-images.sh').write_text(LOAD_SH,encoding='utf-8')
        (root/'images.expected').write_text(''.join(image['tag']+' '+image['id']+'\n' for image in images.values()),encoding='ascii')
        (root/'compose.images.yaml').write_text('services:\n'+''.join(f"  {service}:\n    image: {image['tag']}\n    pull_policy: never\n" for service,image in images.items()),encoding='ascii')
        notices = root/'runtime-notices'
        notices.mkdir()
        command([*docker, 'cp', 'atr-runtime-api-1:/usr/share/doc', str(notices/'debian')])
        command([*docker, 'cp', 'atr-runtime-api-1:/usr/local/lib/python3.12/LICENSE.txt', str(notices/'python-LICENSE.txt')])
        command([*docker, 'cp', 'atr-runtime-api-1:/app/third_party/licenses/node/LICENSE', str(notices/'node-LICENSE.txt')])
        command([*docker, 'cp', 'atr-runtime-web-1:/usr/share/licenses', str(notices/'alpine')])
        command([*docker, 'cp', 'atr-runtime-web-1:/usr/share/doc', str(notices/'nginx-docs')])
        # Package only regular upstream notices; do not follow OS documentation symlinks.
        files = sorted(path for path in root.rglob('*') if path.is_file() and not path.is_symlink())
        (root/'SHA256SUMS').write_text(''.join(sha(path)+'  '+path.relative_to(root).as_posix()+'\n' for path in files),encoding='utf-8')
        files.append(root/'SHA256SUMS')
        manifest = {'schema_version':'atr-docker-offline-1','acceptance_status':'INCOMPLETE_PREVIEW',
                    'source_sha':head,'created_at':datetime.now(timezone.utc).isoformat(),'images':images,
                    'clean_windows_install':'NOT_RUN','clean_ubuntu_install':'NOT_RUN',
                    'native_host_runtime_included':False,'docker_linux_amd64_runtime_included':True,
                    'private_runtime_data_included':False,
                    'files':[{'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)} for path in files]}
        (root/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        partial = output.with_suffix(output.suffix+'.partial')
        if partial.exists():
            raise ValueError('Incomplete previous package preserved; choose a fresh output path')
        with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in [*files, root/'manifest.json']:
                archive.write(path, path.relative_to(root).as_posix())
        partial.replace(output)
    return {'path':str(output),'sha256':sha(output),'bytes':output.stat().st_size,'source_sha':head,'images':images,'acceptance_status':'INCOMPLETE_PREVIEW'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--context',required=True)
    parser.add_argument('--api-source-sha',required=True)
    parser.add_argument('--web-source-sha',required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.repo,args.output,args.context,args.api_source_sha,args.web_source_sha)))


if __name__ == '__main__':
    main()
