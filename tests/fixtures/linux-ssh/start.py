"""Local acceptance fixture only; no Docker socket or production account."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

Path("/etc/atr/policy.json").write_text(json.dumps({
    "allowed_paths": ["/srv/atr-fixture"], "services": [], "containers": [],
    "docker_reads_enabled": False,
}))
for name in ("host_key", "client.pub"):
    destination = Path("/etc/atr") / name
    shutil.copyfile(Path("/keys") / name, destination)
    destination.chmod(0o600 if name == "host_key" else 0o644)

for port, command in (
    (2222, "/usr/local/bin/python3 /opt/atr/ssh_gateway.py --policy /etc/atr/policy.json"),
    (2223, "/usr/local/bin/python3 /opt/atr/transport.py timeout"),
    (2224, "/usr/local/bin/python3 /opt/atr/transport.py disconnect"),
):
    config = Path(f"/etc/atr/sshd-{port}.conf")
    config.write_text(f"""Port {port}
ListenAddress 0.0.0.0
HostKey /etc/atr/host_key
PidFile /run/sshd-{port}.pid
AuthorizedKeysFile /etc/atr/client.pub
AllowUsers atrreader
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
UsePAM no
StrictModes yes
PermitTTY no
DisableForwarding yes
PermitUserEnvironment no
LogLevel VERBOSE
ForceCommand {command}
""")
    subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)])

child = os.fork()
if child == 0:
    os.setgid(10002)
    os.setuid(10002)
    noise = [subprocess.Popen(["/usr/bin/sleep", "900"]) for _ in range(550)]
    subprocess.Popen(["/usr/local/bin/catalog-api", "900"])
    while True:
        time.sleep(10)
else:
    while True:
        time.sleep(10)
