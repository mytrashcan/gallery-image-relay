"""Serialize launchd arguments as data, including XML metacharacters in paths."""
import os
import plistlib
import sys
from pathlib import Path


def write_plist(path, label, root, log, args, environment):
    """Write to an operator-selected CLI destination, never an HTTP input.

    dcselfie.sh supplies a fixed service label under ~/Library/LaunchAgents.
    Absolute destinations outside the checkout are intentional.
    """
    payload = {"Label": label, "ProgramArguments": args, "EnvironmentVariables": environment,
               "WorkingDirectory": root, "RunAtLoad": True, "KeepAlive": True,
               "ThrottleInterval": 30, "ExitTimeOut": 110, "Umask": 0o077,
               "StandardOutPath": log, "StandardErrorPath": log,
               "SoftResourceLimits": {"Core": 0}}
    with Path(path).open("wb") as stream:
        plistlib.dump(payload, stream)
    os.chmod(path, 0o600)


if __name__ == "__main__":
    path, label, root, log, *args = sys.argv[1:]
    env = {"PYTHONUNBUFFERED": "1"}
    if label.endswith("crawler"):
        env["WEB_GALLERY"] = "1"
    elif label.endswith("web"):
        env["WEB_HOST"] = "127.0.0.1"
    write_plist(path, label, root, log, args, env)
