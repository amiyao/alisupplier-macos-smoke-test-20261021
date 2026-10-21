"""Check actual terminal entry points; never supply account credentials."""
import json
import hashlib
import os
import pathlib
import platform
import plistlib
import re
import signal
import sys
import subprocess
import time
ROOT = pathlib.Path(os.environ['RUNNER_TEMP']) / 'client-cli-probe'
ROOT.mkdir(mode=0o700,exist_ok=True)
INSTALL = {}

def checked(args,timeout=90):
    p=subprocess.run(args,capture_output=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError(pathlib.Path(args[0]).name+':exit='+str(p.returncode))
    return p.stdout

def install_client():
    mount=None
    try:
        INSTALL['stage']='download'
        dmg=ROOT/'official.dmg'
        p=subprocess.run(['curl','-fsSL','--connect-timeout','30','--max-time','240',
            '-w','%{http_code}','-o',str(dmg),
            'https://alisupplierdownload.alibaba.com/download/AlibabaSupplier(11.45.90EM).dmg'],
            capture_output=True,timeout=255)
        INSTALL.update(download_exit=p.returncode,http_status=p.stdout.decode('ascii','replace')[-3:])
        if p.returncode:
            raise RuntimeError('official_download_failed')
        INSTALL['stage']='hash'
        digest=hashlib.sha256(dmg.read_bytes()).hexdigest()
        INSTALL['sha256']=digest
        if digest!='345a05146106abc6dbc5564f319ebb7d5e49ad86d0380db45c779f439f297474':
            raise RuntimeError('installer_hash_mismatch')
        INSTALL['stage']='mount_copy'
        info=plistlib.loads(checked(['hdiutil','attach','-readonly','-nobrowse','-plist',str(dmg)]))
        source=next(a for e in info['system-entities'] if 'mount-point' in e
                    for a in pathlib.Path(e['mount-point']).glob('*.app'))
        mount=source.parent
        app=ROOT/source.name
        checked(['ditto',str(source),str(app)],120)
        INSTALL['stage']='signature'
        checked(['codesign','--verify','--deep','--strict',str(app)])
        checked(['spctl','--assess','--type','execute',str(app)])
        INSTALL['stage']='launch'
        checked(['open',str(app)],30)
        INSTALL.update(state='launched',stage='ready')
    except Exception as exc:
        INSTALL.update(state='failed',error_type=type(exc).__name__,
                       reason=str(exc) if type(exc) is RuntimeError else 'operation_failed')
    finally:
        if mount:
            subprocess.run(['hdiutil','detach',str(mount)],capture_output=True,timeout=40)

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
    if len(report["cases"]) != 3:
        sys.exit(1)

