"""Windows integrations with explicit executables and private clipboard formats."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import struct

from .paths import is_local_path


def system_executable(name: str) -> str:
    if os.name != "nt":
        raise OSError("Windows integration is unavailable.")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Windows folder is unavailable.")
    base = Path(buffer.value)
    choices = {
        "explorer": base / "explorer.exe",
        "powershell": base / "System32/WindowsPowerShell/v1.0/powershell.exe",
    }
    if name not in choices:
        raise ValueError("Unsupported Windows integration.")
    path = choices[name]
    if not is_local_path(path) or not path.is_file():
        raise OSError("Windows integration is unavailable.")
    return str(path)


def child_environment() -> dict:
    """Do not pass credentials, decoder overrides, FFREPORT or profile config to FFmpeg."""
    if os.name != "nt":
        return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Windows folder is unavailable.")
    base = Path(buffer.value)
    result = {"SystemRoot": str(base), "WINDIR": str(base),
              "PATH": str(base / "System32") + ";" + str(base)}
    from .paths import temp_dir
    temporary = temp_dir()
    result.update(TEMP=str(temporary), TMP=str(temporary))
    return result


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class ProcessGuard:
    """Bound each Windows decoder's memory and children; closing kills remaining work.

    This constrains resources. It does not make native decoders a security sandbox.
    """
    MEMORY_BYTES = 2 * 1024 * 1024 * 1024

    def __init__(self, process, *, memory_bytes: int | None = None):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.windll.kernel32
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("Decoder resource protection is unavailable.")
        self.handle = handle
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x100 | 0x8  # KILL_ON_CLOSE | PROCESS_MEMORY | ACTIVE_PROCESS
        limits.basic.active_processes = 1
        limits.process_memory = memory_bytes or self.MEMORY_BYTES
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
            self.close()
            raise OSError("Decoder resource protection is unavailable.")

    def close(self):
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def copy_file(path: Path, owner: int) -> None:
    """Publish CF_HDROP with Windows cloud/history exclusion flags.

    Windows owns each allocation after SetClipboardData succeeds. No shell or
    command string is used, and file bytes are not put into clipboard history.
    """
    path = Path(path)
    if os.name != "nt" or not is_local_path(path) or path.suffix.lower() != ".gif" or not path.is_file():
        raise OSError("A local GIF is required.")
    user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.restype = wintypes.HGLOBAL
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    user.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user.RegisterClipboardFormatW.restype = wintypes.UINT
    allocations = []
    opened = False
    try:
        # Allocate and register everything before replacing the old clipboard.
        formats = [(user.RegisterClipboardFormatW(name), struct.pack("<I", 0))
                   for name in ("CanUploadToCloudClipboard", "CanIncludeInClipboardHistory")]
        payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (str(path.absolute()) + "\0\0").encode("utf-16-le")
        formats.append((15, payload))  # CF_HDROP
        for format_id, data in formats:
            if not format_id:
                raise OSError("Clipboard format is unavailable.")
            handle = kernel.GlobalAlloc(0x42, len(data))  # MOVEABLE | ZEROINIT
            if not handle:
                raise OSError("Clipboard allocation failed.")
            allocations.append([format_id, handle])
            address = kernel.GlobalLock(handle)
            if not address:
                raise OSError("Clipboard allocation could not be locked.")
            try:
                ctypes.memmove(address, data, len(data))
            finally:
                kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(owner):
            raise OSError("Clipboard is busy. Try again.")
        opened = True
        if not user.EmptyClipboard():
            raise OSError("Clipboard is unavailable.")
        # Exclusion flags first: never publish the file if they cannot be set.
        for entry in allocations:
            format_id, handle = entry
            if not user.SetClipboardData(format_id, handle):
                raise OSError("Clipboard is unavailable.")
            entry[1] = None
    finally:
        if opened:
            user.CloseClipboard()
        for _, handle in allocations:
            if handle:
                kernel.GlobalFree(handle)
