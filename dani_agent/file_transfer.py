"""Windows file updates under exclusive handles; preserve existing file ACLs."""
import ctypes as C
from ctypes import wintypes as T
import msvcrt
import os
from pathlib import Path
from contextlib import contextmanager

FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
FILE_ATTRIBUTE_NORMAL = 0x80


@contextmanager
def shared_reader(path):
    """Read a complete version with explicit sharing; Windows may still wait for close."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [T.LPCWSTR, T.DWORD, T.DWORD, C.c_void_p, T.DWORD, T.DWORD, T.HANDLE]
    create.restype = T.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [T.HANDLE]; close.restype = T.BOOL
    handle = create(str(Path(path)), 0x80000000, 0x7, None, 3, FILE_FLAG_OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    with os.fdopen(fd,'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_nlink != 1 or getattr(info,'st_file_attributes',0) & 0x400:
            raise RuntimeError('RECOVERY_RECORD_LINK_REFUSED')
        yield stream


@contextmanager
def temporary_lock(path):
    """Exclusive CREATE_NEW lock; Windows removes it even if the worker dies."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [T.LPCWSTR, T.DWORD, T.DWORD, C.c_void_p, T.DWORD, T.DWORD, T.HANDLE]
    create.restype = T.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [T.HANDLE]
    close.restype = T.BOOL
    handle = create(str(Path(path)), 0xC0010000, 0, None, 1,
        FILE_FLAG_DELETE_ON_CLOSE | FILE_FLAG_OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        yield
    finally:
        if not close(handle):
            raise C.WinError(C.get_last_error())


def update_exact(path, expected, content):
    path = Path(path)
    new = expected is None
    if new and content is None:
        return
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [T.LPCWSTR, T.DWORD, T.DWORD, C.c_void_p, T.DWORD, T.DWORD, T.HANDLE]
    create.restype = T.HANDLE
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [T.HANDLE, C.c_int, C.c_void_p, T.DWORD]
    dispose.restype = T.BOOL
    close = kernel.CloseHandle
    close.argtypes = [T.HANDLE]
    close.restype = T.BOOL
    # Share mode zero excludes concurrent access during comparison and write.
    # OPEN_EXISTING preserves the file's ACL; CREATE_NEW cannot overwrite.
    access = 0xC0000000 | (0x10000 if new or content is None else 0)
    handle = create(str(path), access, 0, None, 1 if new else 3, FILE_FLAG_OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    with os.fdopen(fd, 'r+b', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('FILE_LINK_CHANGED_BEFORE_TRANSFER')
        actual = stream.read((len(expected) if expected is not None else 0) + 1)
        if (None if new else actual) != expected:
            raise RuntimeError('CONCURRENT_PROJECT_EDIT_NO_OVERWRITE')

        def delete_owned():
            flag = C.c_ubyte(1)
            if not dispose(handle, 4, C.byref(flag), C.sizeof(flag)):
                raise C.WinError(C.get_last_error())

        def write_bytes(value):
            stream.seek(0)
            if stream.write(value) != len(value):
                raise RuntimeError('INCOMPLETE_FILE_WRITE')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

        if content is None:
            delete_owned()
            return
        try:
            write_bytes(content)
        except Exception as exc:
            try:
                if new:
                    delete_owned()
                else:
                    write_bytes(expected)
            except Exception:
                raise RuntimeError('FILE_TRANSFER_INCOMPLETE_BACKUP_REQUIRED') from exc
            raise RuntimeError('FILE_TRANSFER_FAILED_ORIGINAL_RESTORED') from exc
