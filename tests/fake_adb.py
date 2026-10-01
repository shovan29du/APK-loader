#!/usr/bin/env python3
"""Stand-in for adb: enough behaviour to exercise subprocess + streaming code paths."""
import os
import sys
import time

a = sys.argv[1:]
dev = ""
if a[:1] == ["-s"]:
    dev, a = a[1], a[2:]
log = os.environ.get("FAKE_ADB_LOG")
if log and dev:
    with open(log, "a") as f:
        f.write(f"serial:{dev}\n")
if log:
    with open(log, "a") as f:
        f.write(" ".join(a) + "\n")
out = sys.stdout.buffer
if a[:1] == ["push"]:
    print("1 file pushed")
    sys.exit(0)
if a[:2] == ["forward", "tcp:0"]:
    print(os.environ.get("FAKE_SCRCPY_PORT", "0"))
    sys.exit(0)
if a[:1] == ["forward"]:
    sys.exit(0)
if a[:1] == ["shell"] and len(a) == 2 and a[1].startswith("CLASSPATH="):   # scrcpy-server "running"
    time.sleep(600)
    sys.exit(0)
if a == ["shell"]:                      # persistent shell: log every stdin line
    for line in sys.stdin:
        if log:
            with open(log, "a") as f:
                f.write("stdin: " + line.strip() + "\n")
    sys.exit(0)
if a[:1] == ["devices"]:
    print("List of devices attached")
    print("emulator-5554          device product:sdk model:Pixel_5 device:generic transport_id:1")
    print("192.168.1.20:5555      device product:x model:Pixel_8 device:y transport_id:2")
elif a[:1] == ["pair"]:
    print("Successfully paired to " + a[1])
elif a[:1] == ["connect"] or a[:1] == ["get-state"]:
    print("device")
elif a[:2] == ["shell", "dumpsys window displays | grep -m1 'cur='"]:
    print("  init=1080x2400 420dpi cur=1080x2400 app=1080x2400 rng=1080x1008-2400x2328")
elif a[:2] == ["shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -3"]:
    print("  mCurrentFocus=Window{1a2b u0 com.example.app/com.example.app.MainActivity}")
elif len(a) == 2 and a[0] == "shell" and a[1].startswith("dumpsys package "):
    print("    versionCode=42 minSdk=24 targetSdk=34\n    versionName=2.5.1\n    firstInstallTime=2026-09-01 10:00:00\n    lastUpdateTime=2026-09-20 11:30:00\n    installerPackageName=com.android.vending")
elif a[:2] == ["shell", "dumpsys battery"]:
    print("Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  status: 3\n  level: 42\n  scale: 100")
elif a[:4] == ["shell", "dumpsys", "battery", "set"]:
    pass
elif a[:2] == ["shell", "dumpsys battery reset"] or a[:4] == ["shell", "dumpsys", "battery", "reset"]:
    pass
elif a[:1] == ["shell"] and len(a) == 2 and a[1].startswith("run-as ") and "echo ok" in a[1]:
    print("ok")
elif a[:1] == ["shell"] and len(a) == 2 and (a[1].startswith("run-as ") or a[1].startswith("[ -d ") or "tar -cf" in a[1]):
    pass   # export: tar / existence-check commands succeed silently (no file actually produced in this fake)
elif a[:1] == ["shell"] and len(a) == 2 and a[1].startswith("rm -f /data/local/tmp/apkloader-export"):
    pass
elif a[:3] == ["shell", "pm", "clear"]:
    print("Success")
elif a[:2] == ["shell", "getevent"]:
    print('add device 1: /dev/input/event2\n  name:     "virtio_input_multi_touch_1"\n  events:\n    ABS (0003): ABS_MT_SLOT : value 0, min 0, max 9, fuzz 0\n'
          '                ABS_MT_POSITION_X : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0\n'
          '                ABS_MT_POSITION_Y : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0\n  input props:\n    INPUT_PROP_DIRECT')
elif a[:2] == ["shell", "sendevent /dev/input/event2 0 0 0 && echo OK"]:
    print("OK")
elif a[:4] == ["shell", "settings", "get", "system"]:
    print("0")
elif a[:2] == ["shell", "wm"] and a[2] == "size":
    print("Physical size: 1080x2400")
elif a[:1] == ["exec-out"] and a[1] == "screencap":
    out.write(b"\x89PNG\r\n\x1a\nFAKE")
elif a[:1] == ["exec-out"] and a[1] == "screenrecord":
    for nal in (b"\x00\x00\x00\x01\x67\x64\x00\x1f", b"\x00\x00\x00\x01\x68\xee\x3c\x80",
                b"\x00\x00\x00\x01\x65" + b"\x88" * 40, b"\x00\x00\x00\x01\x41" + b"\x9a" * 20):
        out.write(nal)
        out.flush()
        time.sleep(0.01)
    time.sleep(0.3)
elif a[:1] == ["install"]:
    print("Success")
elif a[:3] == ["shell", "pm", "list"]:
    print("package:com.fake.app versionCode:7")
elif a[:2] == ["shell", "monkey"]:
    print("Events injected: 1")
out.flush()
