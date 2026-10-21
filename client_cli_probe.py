"""Check actual terminal entry points; never supply account credentials."""
import json
import os
import pathlib
import platform
import re
import signal
import subprocess
import time
from private_desktop import ROOT, INSTALL, install_client

report = {"scope": "fresh Mac, help/version arguments only, no account login",
          "macos": platform.mac_ver()[0], "architecture": platform.machine(),
          "run_id": os.getenv("GITHUB_RUN_ID"), "cases": []}

def app_pids(app):
    out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True).stdout.decode("utf-8", "replace")
    return [int(parts[0]) for line in out.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2 and parts[1].startswith(str(app) + "/")]

def stop_app(app):
    for pid in app_pids(app):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1)

def main():
    install_client()
    report["installer"] = INSTALL.copy()
    if INSTALL.get("state") != "launched":
        return
    app = ROOT / "AliSupplier.app"
    binary = app / "Contents/MacOS/AliSupplier"
    if not binary.exists():
        raise RuntimeError("Expected application executable missing")
    for argument in ("--help", "-h", "--version"):
        stop_app(app)
        process = subprocess.Popen([str(binary), argument], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=3)
        raw = stdout + b"\n" + stderr
        options = sorted(set(re.findall(rb"(?m)^\s*(-{1,2}[A-Za-z][A-Za-z0-9_-]{0,60})(?=\s|=|$)", raw)))
        report["cases"].append({"argument": argument, "returncode": process.returncode,
            "timed_out_after_seconds": 8 if timed_out else None,
            "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
            "usage_header": bool(re.search(rb"(?im)^\s*usage\s*[: ]", raw)),
            "option_names": [x.decode("ascii") for x in options],
            "raw_output_published": False})
        stop_app(app)
    report["account_authentication_attempted"] = False

try:
    main()
except Exception as exc:
    report["error_type"] = type(exc).__name__
finally:
    pathlib.Path("evidence").mkdir(exist_ok=True)
    pathlib.Path("evidence/cli-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

