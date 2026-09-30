#!/usr/bin/env python3
"""Stand-in for adb: enough behaviour to exercise subprocess + streaming code paths."""
import os
import sys
import time

a = sys.argv[1:]
if a[:1] == ["-s"]:
    a = a[2:]
log = os.environ.get("FAKE_ADB_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(a) + "\n")
out = sys.stdout.buffer
if a[:1] == ["connect"] or a[:1] == ["get-state"]:
    print("device")
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
