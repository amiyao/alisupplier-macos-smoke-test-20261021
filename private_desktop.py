"""Short-lived, key-only SSH desktop transport for manual Mac compatibility testing."""
import base64
import hashlib
import json
import os
import pathlib
import plistlib
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(os.environ["RUNNER_TEMP"]) / "private-desktop"
ROOT.mkdir(mode=0o700, exist_ok=True)
os.chmod(ROOT, 0o700)
PROCS = []
INSTALL = {"state": "pending"}
KICK = "/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart"
PUBLIC_SSH = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBqGYDti8Igcd2xUaIx5I7CiYNjYmWYYzNx4hx5AiLj/"

def run(args, timeout=120):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError("command_failed:" + pathlib.Path(args[0]).name)
    return p.stdout

def spawn(args, name):
    f = (ROOT / (name + ".private.log")).open("wb")
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT)
    f.close()
    PROCS.append(p)
    return p

def wait_port(port):
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), 1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError("local_service_unavailable:" + str(port))

def download(url, target):
    run(["curl", "-fsSL", "--connect-timeout", "20", "--retry", "2", "--max-time", "90", "--retry-max-time", "280", "-o", str(target), url], 330)

def metadata():
    roots = [pathlib.Path.home() / v for v in (
        "Library/Application Support/AliSupplier", "Library/Logs/AliSupplier",
        "Library/Containers/com.alibaba.AliSupplierMac/Data/Library/Application Support/AliSupplier",
        "Library/Containers/com.alibaba.AliSupplierMac/Data/Library/Logs")]
    rows = []
    markers = ["convCode:", "lastMessageSummary", "sendTime", "msgType", "messageId",
               "contactOwnerId", "contactCid", "latestBuyerMessage"]
    for root_index, root in enumerate(roots):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if len(rows) >= 100:
                break
            if path.is_symlink() or not path.is_file() or not path.name.endswith((".log", ".log.old")):
                continue
            name = path.name if path.name in ("app.log", "AliRender.log", "app.log.old", "AliRender.log.old", "debug.log") else "other.log"
            row = {"root_index": root_index, "file": name}
            try:
                size = path.stat().st_size
                with path.open("rb") as stream:
                    data = stream.read(8*1024*1024)
                try:
                    txt = data.decode("utf-8")
                    valid = True
                except UnicodeDecodeError:
                    txt = data.decode("utf-8", "replace")
                    valid = False
                row.update(python_readable=True, size=size, utf8_valid=valid,
                           truncated=size > len(data), markers={m: txt.count(m) for m in markers})
            except OSError:
                row["python_readable"] = False
            rows.append(row)
    return {"timestamp": int(time.time()), "installer": INSTALL.copy(), "raw_contents_included": False, "files": rows}

def install_client():
    try:
        INSTALL["state"] = "downloading"
        dmg = ROOT / "official.dmg"
        download("https://alisupplierdownload.alibaba.com/download/AlibabaSupplier(11.45.90EM).dmg", dmg)
        if hashlib.sha256(dmg.read_bytes()).hexdigest() != "345a05146106abc6dbc5564f319ebb7d5e49ad86d0380db45c779f439f297474":
            raise RuntimeError("installer_hash_mismatch")
        INSTALL["state"] = "verified_installing"
        mounts = plistlib.loads(run(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(dmg)]))
        app = next(a for e in mounts["system-entities"] if "mount-point" in e
                   for a in pathlib.Path(e["mount-point"]).glob("*.app"))
        dest = ROOT / app.name
        run(["ditto", str(app), str(dest)])
        run(["hdiutil", "detach", str(app.parent)])
        run(["codesign", "--verify", "--deep", "--strict", str(dest)])
        run(["spctl", "--assess", "--type", "execute", str(dest)])
        run(["open", str(dest)])
        INSTALL["state"] = "launched"
    except Exception as exc:
        INSTALL.update(state="failed", error_type=type(exc).__name__)

def main():
    if sys.platform != "darwin" or os.getenv("GITHUB_ACTIONS") != "true":
        raise RuntimeError("disposable_mac_runner_required")
    stage = "dependencies"
    try:
        run(["brew", "install", "bore-cli"], 420)
        env = ROOT / "venv"
        run([sys.executable, "-m", "venv", str(env)])
        py = str(env / "bin/python")
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "websockify==0.13.0"], 180)
        archive = ROOT / "novnc.tar.gz"
        download("https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz", archive)
        run(["tar", "-xzf", str(archive), "-C", str(ROOT)])
        web = ROOT / "noVNC-1.7.0"
        threading.Thread(target=install_client, daemon=True).start()
        stage = "desktop"
        password = secrets.token_hex(4)
        run(["sudo", KICK, "-activate", "-configure", "-access", "-on", "-users", "runner",
             "-privs", "-ControlObserve", "-clientopts", "-setvnclegacy", "-vnclegacy", "yes",
             "-setvncpw", "-vncpw", password])
        wait_port(5900)
        (web / "session.json").write_text(json.dumps({"password": password}))
        (web / "index.html").write_text('''<!doctype html><meta charset="utf-8">
<title>Temporary Mac test</title><style>body{margin:0;background:#222;color:white;font:15px sans-serif}header{padding:10px;display:flex;gap:18px;align-items:center}#screen{height:calc(100vh - 50px)}a{color:#9cf}button{padding:5px}</style>
<header><b>Mac 临时测试</b><span id="status">正在连接…</span><button id="disconnect">断开画面</button><a href="metadata.json" target="_blank">查看日志检测结果</a><span>请勿发送消息</span></header><div id="screen"></div>
<script type="module">
import RFB from './core/rfb.js';
const c=await(await fetch('./session.json',{cache:'no-store'})).json();
const r=new RFB(document.getElementById('screen'),'ws://'+location.host+'/websockify',{credentials:{password:c.password},shared:true});
r.scaleViewport=true;r.resizeSession=false;
r.addEventListener('connect',()=>document.getElementById('status').textContent='已连接');
r.addEventListener('disconnect',e=>document.getElementById('status').textContent=e.detail.clean?'已断开':'连接失败');
r.addEventListener('securityfailure',e=>document.getElementById('status').textContent='认证失败：'+e.detail.reason);
document.getElementById('disconnect').onclick=()=>r.disconnect();
</script>''', encoding="utf-8")
        spawn([str(env / "bin/websockify"), "--web", str(web), "127.0.0.1:6080", "127.0.0.1:5900"], "websockify")
        wait_port(6080)
        stage = "ssh"
        hostkey = ROOT / "hostkey"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)])
        auth = ROOT / "authorized_keys"
        auth.write_text('restrict,port-forwarding,permitopen="127.0.0.1:6080" ' + PUBLIC_SSH + '\n')
        auth.chmod(0o600)
        conf = ROOT / "sshd_config"
        conf.write_text(f'''Port 22022
ListenAddress 127.0.0.1
HostKey {hostkey}
PidFile {ROOT / "sshd.pid"}
AuthorizedKeysFile {auth}
AllowUsers runner
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM yes
StrictModes yes
AllowTcpForwarding local
PermitOpen 127.0.0.1:6080
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PermitUserRC no
GatewayPorts no
MaxSessions 0
LogLevel ERROR
''')
        run(["sudo", "/usr/sbin/sshd", "-t", "-f", str(conf)])
        spawn(["sudo", "/usr/sbin/sshd", "-D", "-e", "-f", str(conf)], "sshd")
        wait_port(22022)
        stage = "relay"
        bore = spawn(["bore", "local", "22022", "--to", "bore.pub"], "bore")
        port = None
        for _ in range(45):
            text = (ROOT / "bore.private.log").read_text(errors="replace")
            found = re.search(r"bore\.pub:(\d+)", text)
            if found:
                port = int(found.group(1))
                break
            if bore.poll() is not None:
                raise RuntimeError("relay_exit")
            time.sleep(1)
        if port is None:
            raise RuntimeError("relay_timeout")
        stage = "handoff"
        # Host public key is delivered through authenticated GitHub, never trust-on-first-use.
        handoff = {"host": "bore.pub", "port": port, "user": "runner",
                   "hostkey": ' '.join((ROOT / "hostkey.pub").read_text().split()[:2]),
                   "expires": int(time.time()) + 1200}
        from seal_connection import seal
        packet = seal(pathlib.Path("session-public.pem"), handoff)
        (ROOT / "connection.encrypted.json").write_text(json.dumps(packet))
        (ROOT / "ready").write_text("ready")
        print("Private desktop transport ready; authentication key stays on user's PC.", flush=True)
        stage = "session"
        for _ in range(80):
            (web / "metadata.json").write_text(json.dumps(metadata()), encoding="utf-8")
            if any(p.poll() is not None for p in PROCS):
                raise RuntimeError("session_service_exit")
            time.sleep(15)
    except Exception as exc:
        (ROOT / "failed.json").write_text(json.dumps({"stage": stage, "error_type": type(exc).__name__, "reason": str(exc) if type(exc) is RuntimeError else "operation_failed"}))
        print("Session stopped at stage=" + stage + "; error_type=" + type(exc).__name__, flush=True)
    finally:
        for p in reversed(PROCS):
            if p.poll() is None:
                p.terminate()
        p = subprocess.run(["sudo", KICK, "-deactivate", "-stop"], capture_output=True, timeout=40)
        (ROOT / "finished.json").write_text(json.dumps({"desktop_stop_exit": p.returncode}))

if __name__ == "__main__":
    main()

