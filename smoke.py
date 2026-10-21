"""No-login smoke test. Publishes metadata only, never raw application logs."""
import hashlib
import json
import os
import pathlib
import platform
import plistlib
import signal
import subprocess
import time

URL = "https://alisupplierdownload.alibaba.com/download/AlibabaSupplier(11.45.90EM).dmg"
SHA256 = "345a05146106abc6dbc5564f319ebb7d5e49ad86d0380db45c779f439f297474"
OUT = pathlib.Path("evidence")
OUT.mkdir(exist_ok=True)
R = {"schema": 1, "scope": "fresh runner; no login; no UI interaction; no messages",
     "message_capture_verified": False, "checks": {}, "samples": []}
APP = None
MOUNT = None
ROOTS = [pathlib.Path.home() / p for p in (
    "Library/Application Support/AliSupplier",
    "Library/Logs/AliSupplier",
    "Library/Containers/com.alibaba.AliSupplierMac/Data/Library/Application Support/AliSupplier",
    "Library/Containers/com.alibaba.AliSupplierMac/Data/Library/Logs")]
MARKERS = ["convCode:", "lastMessageSummary", "sendTime", "msgType",
           "messageId", "contactOwnerId", "contactCid", "latestBuyerMessage"]

def run(args, timeout=60):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, stdin=subprocess.DEVNULL)

def record_command(name, args, timeout=60):
    p = run(args, timeout)
    # Do not forward arbitrary app stdout or error output to public CI.
    R["checks"][name] = {"returncode": p.returncode,
                         "stdout_bytes": len(p.stdout), "stderr_bytes": len(p.stderr)}
    return p

def processes():
    p = run(["ps", "-axo", "pid=,comm="])
    found = []
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and APP and str(APP) + "/" in parts[1]:
            found.append({"pid": int(parts[0]), "executable": pathlib.Path(parts[1]).name})
    return found

def logs():
    results = []
    for root in ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if len(results) >= 100:
                break
            if p.is_symlink() or not p.is_file():
                continue
            if not (p.name.endswith(".log") or p.name.endswith(".log.old")):
                continue
            item = {"path": "~/" + str(p.relative_to(pathlib.Path.home()))}
            try:
                stat = p.stat()
                item.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                # Bounded reads; hash is for exactly the sampled bytes.
                with p.open("rb") as f:
                    data = f.read(8 * 1024 * 1024)
                item.update(python_readable=True, bytes_read=len(data),
                            truncated=stat.st_size > len(data),
                            sampled_sha256=hashlib.sha256(data).hexdigest())
                try:
                    text = data.decode("utf-8")
                    item["utf8_valid"] = True
                except UnicodeDecodeError:
                    text = data.decode("utf-8", "replace")
                    item["utf8_valid"] = False
                item["line_count"] = len(text.splitlines())
                item["marker_counts"] = {m: text.count(m) for m in MARKERS}
            except OSError as e:
                item.update(python_readable=False, error_type=type(e).__name__,
                            errno=e.errno)
            results.append(item)
    return results

def main():
    global APP, MOUNT
    if platform.system() != "Darwin" or os.getenv("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Only execute on an isolated GitHub macOS runner")
    R["environment"] = {"macos": platform.mac_ver()[0], "architecture": platform.machine(),
                        "python": platform.python_version(), "uid": os.getuid(),
                        "run_id": os.getenv("GITHUB_RUN_ID"),
                        "runner_image": os.getenv("ImageOS"),
                        "runner_image_version": os.getenv("ImageVersion")}
    tmp = pathlib.Path(os.environ["RUNNER_TEMP"]) / "alisupplier-smoke"
    tmp.mkdir(exist_ok=False)
    dmg = tmp / "official.dmg"
    p = record_command("download", ["curl", "--fail", "--location", "--silent",
        "--show-error", "--max-time", "300", "--output", str(dmg), URL], 320)
    if p.returncode != 0:
        raise RuntimeError("Download failed")
    actual = hashlib.sha256(dmg.read_bytes()).hexdigest()
    R["installer"] = {"url": URL, "sha256": actual, "bytes": dmg.stat().st_size}
    if actual != SHA256:
        raise RuntimeError("Installer hash differs from inspected official package")
    p = record_command("mount", ["hdiutil", "attach", "-readonly", "-nobrowse",
                                "-plist", str(dmg)], 90)
    if p.returncode:
        raise RuntimeError("DMG mount failed")
    mount_info = plistlib.loads(p.stdout)
    mounts = [pathlib.Path(e["mount-point"]) for e in mount_info["system-entities"]
              if "mount-point" in e]
    source = next((a for m in mounts for a in m.glob("*.app")), None)
    if source is None:
        raise RuntimeError("No top-level application bundle")
    MOUNT = source.parent
    APP = tmp / source.name
    p = record_command("copy", ["ditto", str(source), str(APP)], 120)
    if p.returncode:
        raise RuntimeError("Application copy failed")
    info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
    R["bundle"] = {k: info.get(k) for k in ("CFBundleIdentifier",
                  "CFBundleShortVersionString", "CFBundleVersion", "CFBundleExecutable")}
    p = record_command("codesign_verify", ["codesign", "--verify", "--deep", "--strict",
                                          str(APP)], 90)
    if p.returncode:
        raise RuntimeError("Code signature verification failed; not bypassed")
    p = record_command("gatekeeper_assess", ["spctl", "--assess", "--type", "execute",
                                           str(APP)], 90)
    if p.returncode:
        raise RuntimeError("Gatekeeper did not approve; not bypassed")
    R["before_logs"] = logs()
    R["launch_time_unix"] = time.time()
    p = record_command("open", ["open", "-n", str(APP)], 30)
    if p.returncode:
        raise RuntimeError("LaunchServices returned an error")
    started = time.monotonic()
    for target in (5, 20, 45, 90):
        time.sleep(max(0, target - (time.monotonic() - started)))
        R["samples"].append({"elapsed_seconds": round(time.monotonic()-started, 1),
                              "processes": processes(), "logs": logs()})
    # AppKit metadata only: no UI interaction, window titles, images, or login state.
    swift = tmp / "observe.swift"
    swift.write_text('''import AppKit
import CoreGraphics
import Foundation
let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.alibaba.AliSupplierMac")
let wins = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
let records: [[String: Any]] = apps.map { app in
  ["pid": Int(app.processIdentifier), "finished_launching": app.isFinishedLaunching,
   "window_count": wins.filter { ($0[kCGWindowOwnerPID as String] as? Int) == Int(app.processIdentifier) }.count]
}
let data = try JSONSerialization.data(withJSONObject: records, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
''', encoding="utf-8")
    p = record_command("gui_metadata", ["swift", str(swift)], 90)
    if p.returncode == 0:
        R["gui_applications"] = json.loads(p.stdout)
    R["after_logs"] = logs()
    before = {x["path"]: x for x in R["before_logs"]}
    for item in R["after_logs"]:
        old = before.get(item["path"])
        item["new_since_launch"] = old is None
        item["changed_since_launch"] = old is None or (
            item.get("size"), item.get("mtime_ns")) != (old.get("size"), old.get("mtime_ns"))
    R["process_alive_at_90_seconds"] = bool(R["samples"][-1]["processes"])
    R["new_readable_nonempty_log"] = any(
        x.get("changed_since_launch") and x.get("python_readable") and x.get("bytes_read",0)>0
        for x in R["after_logs"])
    R["smoke_passed"] = R["process_alive_at_90_seconds"] and R["new_readable_nonempty_log"]

try:
    main()
except Exception as e:
    # Exceptions may contain paths/output; report type and controlled RuntimeError only.
    R["error_type"] = type(e).__name__
    if type(e) is RuntimeError:
        R["error"] = str(e)
finally:
    if APP:
        for proc in processes():
            try:
                os.kill(proc["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    if MOUNT:
        try:
            record_command("unmount", ["hdiutil", "detach", str(MOUNT)], 30)
        except Exception:
            R["unmount_failed"] = True
    payload = json.dumps(R, ensure_ascii=False, indent=2)
    (OUT / "report.json").write_text(payload + "\n", encoding="utf-8")
    print("SANITIZED_REPORT_BEGIN")
    print(payload)
    print("SANITIZED_REPORT_END")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("# No-login macOS smoke test\n\n")
            f.write("- Smoke passed: " + str(R.get("smoke_passed", False)) + "\n")
            f.write("- Real message capture verified: **False** (no login)\n")
            f.write("- Raw app logs and screenshots are not uploaded.\n")
    raise SystemExit(0 if R.get("smoke_passed") else 1)
