"""Subprocess helpers. On Windows every child process is started without a console window.

Without this, the windowless app (desktop shortcut / pythonw) makes each `adb.exe` call pop up a
black console window, and the app calls adb constantly (status, display size, input, video...).
"""
import asyncio
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


def window_kwargs(os_name: str | None = None) -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if (os_name or os.name) == "nt" else {}


def _merge(kw: dict) -> dict:
    flags = window_kwargs()
    if flags and "creationflags" in kw:
        kw = {**kw, "creationflags": kw["creationflags"] | CREATE_NO_WINDOW}
        flags = {}
    return {**kw, **flags}


async def exec_async(*args, **kw):
    return await asyncio.create_subprocess_exec(*args, **_merge(kw))


def run(*args, **kw):
    return subprocess.run(*args, **_merge(kw))


def popen(*args, **kw):
    return subprocess.Popen(*args, **_merge(kw))


def check_output(*args, **kw):
    return subprocess.check_output(*args, **_merge(kw))
