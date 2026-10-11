"""Local acceptance fixture only; no Docker socket or production account."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

quoted_file = Path("/srv/atr-fixture/shell'$(id);.conf")
quoted_file.write_text("SAFE_SYNTHETIC_FILE\n")
quoted_file.chmod(0o444)

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
    # Fixture-only: negative cases must not trigger OpenSSH 10 source penalty bans.
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
PerSourcePenalties no
ForceCommand {command}
""")
    subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)])

# Test-only password hash; the plaintext never enters a Docker env or file.
password_hash = Path("/keys/password_hash")
if password_hash.is_file():
    subprocess.run(["chpasswd", "-e"], input="atrpassword:" + password_hash.read_text().strip() + "\n",
                   text=True, check=True)
    config = Path("/etc/atr/sshd-password.conf")
    config.write_text("""Port 2225
ListenAddress 0.0.0.0
HostKey /etc/atr/host_key
PidFile /run/sshd-password.pid
AllowUsers atrpassword
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication no
AuthorizedKeysFile none
UsePAM no
PermitEmptyPasswords no
StrictModes yes
PermitUserEnvironment no
LogLevel VERBOSE
PerSourcePenalties no
Subsystem sftp internal-sftp
Match User atrpassword
    PasswordAuthentication yes
    AuthenticationMethods password
    ForceCommand /usr/local/bin/python3 /opt/atr/ssh_gateway.py --policy /etc/atr/policy.json
    PermitTTY no
    DisableForwarding yes
""")
    subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config)], check=True)
    subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)])

# Ordinary accounts exist only inside this isolated fixture. No production account changes.
if password_hash.is_file():
    for user in ("atrstandard", "atrrisk"):
        subprocess.run(["chpasswd", "-e"], input=user + ":" + password_hash.read_text().strip() + "\n", text=True, check=True)
    for port, forced in ((2226, ""), (2227, "ForceCommand /usr/bin/sleep 15\n"),
                         (2228, "ForceCommand /usr/bin/seq 1 100000\n")):
        config = Path(f"/etc/atr/sshd-standard-{port}.conf")
        config.write_text(f"""Port {port}
ListenAddress 0.0.0.0
HostKey /etc/atr/host_key
PidFile /run/sshd-standard-{port}.pid
AllowUsers atrstandard atrrisk
PermitRootLogin no
PasswordAuthentication yes
PubkeyAuthentication yes
KbdInteractiveAuthentication no
AuthorizedKeysFile /etc/atr/client.pub
UsePAM no
PermitEmptyPasswords no
StrictModes yes
PermitUserEnvironment no
PermitTTY no
DisableForwarding yes
LogLevel VERBOSE
PerSourcePenalties no
{forced}""")
        subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config)], check=True)
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
