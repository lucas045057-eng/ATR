#!/usr/local/bin/python3
"""Isolated sudo protocol fixture. Container metadata is synthetic; no Docker daemon."""
import os
import sys

if os.geteuid() != 0 or sys.argv[1:] != ["list"]:
    sys.exit(1)
print("NAMES\tIMAGE\tSTATUS")
print("catalog-api\tcatalog:fixture\tUp 2 hours")
print("unrelated-backup\tbackup:fixture\tExited (0) 1 day ago")
