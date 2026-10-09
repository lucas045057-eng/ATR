"""Server-owned delay/abrupt-output fixture to test actual SSH transport errors."""

import os
import sys
import time

if os.environ.get("SSH_ORIGINAL_COMMAND") != "atr-read-v1":
    raise PermissionError("Fixed entry required")
sys.stdin.buffer.read(4097)
if sys.argv[1] == "timeout":
    time.sleep(15)
    print('{"text":"isolated delayed response"}')
else:
    os._exit(0)
