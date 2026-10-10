"""Low-level Windows Junction and standard Python link creation."""
from __future__ import annotations

from pathlib import Path
import ctypes
import os
import struct

JUNCTION_TAG = 0xA0000003
SYMLINK_TAG = 0xA000000C


def create_junction(source: Path, target: Path) -> None:
    if os.name != 'nt':
        raise OSError('Junction 仅支持 Windows')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    ioctl.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    if str(source).startswith('\\\\'):
        raise ValueError('Junction 源不支持 UNC 网络路径，请使用目录符号链接')
    substitute = ('\\??\\' + str(source)).encode('utf-16-le')
    printable = str(source).encode('utf-16-le')
    data = substitute + b'\x00\x00' + printable + b'\x00\x00'
    body = struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(printable)) + data
    buffer = ctypes.create_string_buffer(struct.pack('<IHH', JUNCTION_TAG, len(body), 0) + body)
    target.mkdir()
    handle = create(str(target), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        target.rmdir()
        raise error
    try:
        returned = wintypes.DWORD()
        if not ioctl(handle, 0x000900A4, buffer, len(buffer.raw) - 1, None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        close(handle)
        handle = None
        target.rmdir()
        raise
    finally:
        if handle is not None:
            close(handle)


def create_link(source: Path, target: Path, kind: str) -> None:
    if kind == 'junction':
        create_junction(source, target)
    elif kind == 'hardlink':
        os.link(source, target)
    else:
        os.symlink(source, target, target_is_directory=(kind == 'dir_symlink'))


