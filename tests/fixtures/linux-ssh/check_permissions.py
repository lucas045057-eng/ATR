import json
import os

assert os.getuid() == 10002 and os.getgid() == 10002
paths = ["/srv/atr-fixture", "/srv/atr-fixture/app.conf", "/etc/atr/policy.json",
         "/etc/atr/client.pub", "/opt/atr/ssh_gateway.py"]
for path in paths:
    assert not os.access(path, os.W_OK), path
try:
    descriptor = os.open("/srv/atr-fixture/app.conf", os.O_WRONLY)
except PermissionError:
    pass
else:
    os.close(descriptor)
    raise AssertionError("Fixture reader unexpectedly has write access")
print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "write_denied_paths": len(paths), "actual_write_open_denied": True}))
