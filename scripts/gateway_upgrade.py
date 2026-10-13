"""Explicit administrator maintenance over an existing trusted OpenSSH alias."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

REMOTE = r'''
import hashlib,json,os,pwd,shutil,stat,subprocess,sys
from pathlib import Path
r=json.loads(sys.stdin.buffer.read(256001))
assert os.getuid()==0 and r['mode'] in {'check','apply','rollback'}
gateway=Path('/usr/local/libexec/atr-read-v1/ssh_gateway.py')
helper=gateway.with_name('remote_reads.py')
policy=Path('/etc/atr-read-v1/policy.json')
base=Path('/var/backups/atr-read-v1')
assert r['backup_id'].startswith('ab-') and '/' not in r['backup_id']
backup=base/r['backup_id']
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def verify_parent(path):
 for parent in [path.parent,*path.parent.parents]:
  info=parent.lstat()
  assert stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
for path in (gateway,policy):
 verify_parent(path)
 assert stat.S_ISREG(path.lstat().st_mode) and path.stat().st_uid==0
def replace(path,raw,mode,gid=0):
 temp=path.with_name(path.name+'.'+r['backup_id']+'.tmp')
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 try:
  with os.fdopen(fd,'wb') as stream:
   stream.write(raw);stream.flush();os.fsync(stream.fileno())
  os.chown(temp,0,gid);os.chmod(temp,mode);os.replace(temp,path)
  directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try: os.fsync(directory)
  finally: os.close(directory)
 except BaseException:
  raise
def services_snapshot():
 config=json.loads(policy.read_text())
 rows=[]
 for cid in config.get('containers',[]):
  p=subprocess.run(['/usr/bin/docker','inspect','--format','{{json .State.Status}} {{json .State.StartedAt}} {{json .Name}}',cid],capture_output=True,timeout=8,check=True)
  rows.append({'id':cid,'state_metadata':p.stdout.decode().strip()})
 return rows
def probe(path):
 config=json.loads(policy.read_text())
 tools=[('capabilities',{})]+[('container.status',{'container_id':v}) for v in config.get('containers',[])]+[('health.get',{'endpoint_id':v['endpoint_id']}) for v in config.get('health_endpoints',[])]
 results=[]
 for tool,args in tools:
  p=subprocess.run(['/usr/sbin/runuser','-u','atr-read-v1','--','/usr/bin/python3',str(path),'--policy',str(policy)],input=json.dumps({'tool_id':tool,'arguments':args,'protocol':'atr-read-ab-v1'}).encode(),capture_output=True,env={'PATH':'/usr/bin:/bin','SSH_ORIGINAL_COMMAND':'atr-read-v1'},timeout=10,check=True)
  row=json.loads(p.stdout)
  assert row.get('protocol')=='atr-read-ab-v1' if tool not in {'capabilities'} else row.get('schema_version')==1
  if tool=='health.get': assert row['status_code']==200 and row['observation_location']=='remote_ssh'
  results.append({'tool_id':tool,'state':row.get('state'),'status_code':row.get('status_code'),'protocol':row.get('protocol'),'startup_cycle':row.get('startup_cycle')})
 return results
if r['mode']=='rollback':
 manifest=json.loads((backup/'manifest.json').read_text())
 assert sha(gateway)==manifest['after']['gateway'] and sha(policy)==manifest['after']['policy'] and sha(helper)==manifest['after']['helper'], 'Administrator drift; rollback refused'
 before=services_snapshot()
 restore=[(gateway,'gateway'),(policy,'policy')]+([(helper,'helper')] if 'helper' in manifest['before'] else [])
 for path,name in restore:
  entry=manifest['before'][name]
  raw=(backup/(name+'.original')).read_bytes()
  assert hashlib.sha256(raw).hexdigest()==entry['sha256']
  replace(path,raw,entry['mode'],entry['gid'])
 # Pre-helper historical manifests restore the legacy gateway; its unused helper remains recoverable.
 after=services_snapshot()
 assert before==after
 print(json.dumps({'mode':'rollback','backup':str(backup),'gateway_sha256':sha(gateway),'helper_sha256':sha(helper),'policy_sha256':sha(policy),'business_services_unchanged':True,'containers':after}))
 sys.exit(0)
assert sha(gateway)==r['expected_gateway_sha'] and sha(policy)==r['expected_policy_sha'], 'Original deployment drift'
payloads={name:content.encode() for name,content in r['files'].items()}
assert set(payloads)=={'gateway','helper'}
for name,raw in payloads.items():
 assert hashlib.sha256(raw).hexdigest()==r['hashes'][name]
 compile(raw,name,'exec')
old_config=json.loads(policy.read_text())
new_config={**old_config,'health_endpoints':r['health_endpoints']}
policy_bytes=(json.dumps(new_config,indent=2)+'\n').encode()
before=services_snapshot()
report={'mode':r['mode'],'backup':str(backup),'source_hashes':r['hashes'],'original_gateway_sha256':sha(gateway),'original_policy_sha256':sha(policy),'containers':before}
if r['mode']=='check':
 report['server_written']=False
 print(json.dumps(report));sys.exit(0)
assert not backup.exists()
base.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(base,0o700)
verify_parent(backup)
backup.mkdir(mode=0o700)
original={}
for path,name in ((gateway,'gateway'),(policy,'policy')):
 info=path.stat(); original[name]={'sha256':sha(path),'mode':stat.S_IMODE(info.st_mode),'gid':info.st_gid}
 shutil.copyfile(path,backup/(name+'.original'))
if helper.exists():
 assert stat.S_ISREG(helper.lstat().st_mode) and helper.stat().st_uid==0
 info=helper.stat();original['helper']={'sha256':sha(helper),'mode':stat.S_IMODE(info.st_mode),'gid':info.st_gid}
 shutil.copyfile(helper,backup/'helper.original')
manifest={'before':original,'after':{'gateway':r['hashes']['gateway'],'helper':r['hashes']['helper'],'policy':hashlib.sha256(policy_bytes).hexdigest()},'containers':before}
(backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
try:
 replace(helper,payloads['helper'],0o755)
 replace(policy,policy_bytes,original['policy']['mode'],original['policy']['gid'])
 replace(gateway,payloads['gateway'],original['gateway']['mode'],original['gateway']['gid'])
 results=probe(gateway)
 after=services_snapshot()
 assert before==after
 report.update(server_written=True,business_services_unchanged=True,probes=results,applied=manifest['after'])
except BaseException as error:
 restore=[(gateway,'gateway'),(policy,'policy')]+([(helper,'helper')] if 'helper' in original else [])
 for path,name in restore:
  entry=original[name];replace(path,(backup/(name+'.original')).read_bytes(),entry['mode'],entry['gid'])
 print(json.dumps({'mode':'apply','status':'failed_rolled_back','error_type':type(error).__name__,'backup':str(backup),'business_services_unchanged':before==services_snapshot()}))
 sys.exit(1)
print(json.dumps(report))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'apply', 'rollback'])
    parser.add_argument('--ssh-alias', required=True)
    parser.add_argument('--backup-id', required=True)
    parser.add_argument('--expected-gateway-sha', default='')
    parser.add_argument('--expected-policy-sha', default='')
    parser.add_argument('--endpoints-file', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    options = parser.parse_args()
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', options.ssh_alias)
            or not re.fullmatch(r'ab-[A-Za-z0-9_-]{1,60}', options.backup_id)):
        parser.error('Invalid fixed SSH alias or backup identity')
    request = {key: getattr(options, key) for key in ('mode', 'backup_id', 'expected_gateway_sha', 'expected_policy_sha')}
    if options.mode != 'rollback':
        if any(not re.fullmatch(r'[a-f0-9]{64}', request[key]) for key in ('expected_gateway_sha', 'expected_policy_sha')):
            parser.error('Exact current deployment hashes are required')
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root/'backend'))
        request['files'] = {'gateway': (root/'scripts/ssh_gateway.py').read_text(encoding='utf-8'),
                            'helper': (root/'backend/app/connectors/remote_reads.py').read_text(encoding='utf-8')}
        request['hashes'] = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in request['files'].items()}
        request['health_endpoints'] = json.loads(options.endpoints_file.read_text(encoding='utf-8')) if options.endpoints_file else []
        from urllib.parse import urlsplit

        from app.connectors.models import HealthEndpointSelection
        request['health_endpoints'] = HealthEndpointSelection(health_endpoints=request['health_endpoints']).model_dump()['health_endpoints']
        if any(urlsplit(row['url']).hostname not in {'localhost', '127.0.0.1', '::1'} for row in request['health_endpoints']):
            parser.error('This maintenance installer only accepts reviewed remote loopback endpoints')
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
                             options.ssh_alias, shlex.join(['/usr/bin/python3', '-c', REMOTE])],
                            input=json.dumps(request).encode(), capture_output=True, timeout=60)
    if result.stdout:
        report = json.loads(result.stdout)
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report))
    else:
        print(json.dumps({'status': 'maintenance_failed', 'exit_code': result.returncode,
                          'stderr_bytes': len(result.stderr)}))
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
