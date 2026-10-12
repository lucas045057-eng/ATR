"""Disposable loopback OpenSSH gateway. Never used on a production server."""

import shutil
import subprocess
from pathlib import Path

for name in ("host_key", "client.pub", "policy.json"):
    target = Path("/etc/atr") / name
    shutil.copyfile(Path("/fixture") / name, target)
    target.chmod(0o600 if name == "host_key" else 0o444)
Path("/srv/final-reviewed").mkdir(exist_ok=True)
shutil.copyfile("/fixture/deployment.reviewed.json", "/srv/final-reviewed/deployment.reviewed.json")
Path("/srv/final-reviewed/deployment.reviewed.json").chmod(0o444)
Path("/srv/final-reviewed").chmod(0o555)
config = Path("/etc/atr/sshd-final.conf")
config.write_text("""Port 2222
ListenAddress 0.0.0.0
HostKey /etc/atr/host_key
AllowUsers atrreader
PermitRootLogin no
AuthorizedKeysFile /etc/atr/client.pub
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
UsePAM no
StrictModes yes
PermitTTY no
DisableForwarding yes
PermitUserEnvironment no
PerSourcePenalties no
Subsystem sftp internal-sftp
ForceCommand sudo -n /usr/local/bin/python3 /opt/atr/ssh_gateway.py --policy /etc/atr/policy.json
""")
subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(config)], check=True)
subprocess.run(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)], check=True)
