"""The operating system's own folder picker, opened by the hub on the server computer.

A web page cannot learn the full path of a folder the user picks in the browser (only its
name), so the admin page asks the hub, which runs on that same computer, to open the
picker. The admin page is only served to the server computer itself (4.2), so only the
person sitting there can open it.

- Windows: the Explorer-style "Select folder" dialog (IFileOpenDialog, through ctypes:
  no extra package).
- macOS: Finder's "choose folder" (AppleScript, through osascript).
- Linux: zenity or kdialog when installed.

Every picker starts in the user's Downloads folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

TITLE = "Chọn thư mục phim cho House Cinema"
OK_LABEL = "Chọn thư mục này"


class PickerUnavailable(Exception):
    """No folder picker can be shown here (no desktop session, no tool): type the path."""


class PickerBusy(Exception):
    """A picker opened by an earlier click is still open."""


_open = threading.Lock()


def pick_folder(initial: Path | None = None, title: str = TITLE) -> str | None:
    """Show the picker and wait. Returns the chosen folder, or None when cancelled.
    Raises PickerBusy or PickerUnavailable."""
    initial = initial if initial is not None else downloads_folder()
    if not _open.acquire(blocking=False):
        raise PickerBusy("The folder picker is already open on the server.")
    try:
        if sys.platform == "win32":
            return _windows_pick(initial, title)
        if sys.platform == "darwin":
            return _mac_pick(initial, title)
        return _linux_pick(initial, title)
    finally:
        _open.release()


def downloads_folder() -> Path:
    """The user's Downloads folder (where Windows really keeps it, also when moved)."""
    if sys.platform == "win32":
        found = _windows_known_folder("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
        if found is not None:
            return found
    return Path.home() / "Downloads"


# ============================================================================ macOS


def _applescript_text(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mac_command(initial: Path, title: str) -> list[str]:
    choose = f"set chosen to choose folder with prompt {_applescript_text(title)}"
    if initial.is_dir():
        choose += f" default location (POSIX file {_applescript_text(str(initial))})"
    else:
        choose += " default location (path to downloads folder)"
    # "activate": the picker comes to the front instead of opening behind the browser.
    return ["osascript", "-e", "activate", "-e", choose, "-e", "POSIX path of chosen"]


def _mac_pick(initial: Path, title: str, run=subprocess.run) -> str | None:
    try:
        done = run(_mac_command(initial, title), capture_output=True, text=True)
    except OSError as exc:
        raise PickerUnavailable(f"osascript cannot run: {exc}") from exc
    if done.returncode != 0:
        if "-128" in done.stderr:  # "User canceled."
            return None
        raise PickerUnavailable(done.stderr.strip() or f"osascript exit code {done.returncode}")
    path = done.stdout.strip()
    return path.rstrip("/") or "/"


# ============================================================================ Linux


def _linux_pick(initial: Path, title: str, run=subprocess.run) -> str | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise PickerUnavailable("No desktop session on the server.")
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={initial}/"]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", str(initial), "--title", title]
    else:
        raise PickerUnavailable("No folder picker installed (zenity or kdialog).")
    done = run(cmd, capture_output=True, text=True)
    if done.returncode == 1:
        return None
    if done.returncode != 0:
        raise PickerUnavailable(done.stderr.strip() or f"exit code {done.returncode}")
    return done.stdout.strip() or None


# ============================================================================ Windows

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _ole32 = ctypes.WinDLL("ole32")
    _shell32 = ctypes.WinDLL("shell32")
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32")

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(text: str) -> _GUID:
        g = _GUID()
        _ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    _ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                              ctypes.POINTER(ctypes.c_void_p)]
    _shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    _shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                     ctypes.POINTER(ctypes.c_void_p)]
    _shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumThreadWindows.argtypes = [wintypes.DWORD, _EnumProc, wintypes.LPARAM]

    _CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
    _IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
    _IID_IShellItem = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
    _FOS_PICKFOLDERS, _FOS_FORCEFILESYSTEM, _FOS_PATHMUSTEXIST = 0x20, 0x40, 0x800
    _SIGDN_FILESYSPATH = 0x80058000
    _CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
    # vtable slots (IUnknown 0-2, IModalWindow::Show 3, then IFileDialog in order)
    _RELEASE, _SHOW, _SET_OPTIONS, _GET_OPTIONS, _SET_FOLDER = 2, 3, 9, 10, 12
    _SET_TITLE, _SET_OK_LABEL, _GET_RESULT, _GET_DISPLAY_NAME = 17, 18, 20, 5

    def _call(obj: ctypes.c_void_p, slot: int, argtypes: list, *args) -> int:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
        return fn(obj, *args) & 0xFFFFFFFF

    def _check(hr: int, what: str) -> None:
        if hr & 0x80000000:
            raise PickerUnavailable(f"{what} failed (0x{hr:08X})")

    def _windows_known_folder(guid_text: str) -> Path | None:
        out = ctypes.c_void_p()
        if _shell32.SHGetKnownFolderPath(ctypes.byref(_guid(guid_text)), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(ctypes.wstring_at(out.value))
        finally:
            _ole32.CoTaskMemFree(out)

    def _front_owner() -> int | None:
        """A tiny topmost window, brought to the front, to own the dialog. The hub runs in the
        background (tray app): without this, Windows opens the picker behind the browser."""
        x = _user32.GetSystemMetrics(0) // 2 - 300  # SM_CXSCREEN: the dialog centres on its owner
        y = _user32.GetSystemMetrics(1) // 2 - 220
        hwnd = _user32.CreateWindowExW(0x8 | 0x80, "STATIC", TITLE, 0x80000000, x, y, 0, 0,
                                       None, None, None, None)  # TOPMOST|TOOLWINDOW, POPUP
        if not hwnd:
            return None
        me = _kernel32.GetCurrentThreadId()
        front = _user32.GetForegroundWindow()
        other = _user32.GetWindowThreadProcessId(front, None) if front else 0
        attached = bool(other and other != me and _user32.AttachThreadInput(me, other, True))
        _user32.ShowWindow(hwnd, 5)  # SW_SHOW (0 x 0: nothing to see)
        _user32.SetForegroundWindow(hwnd)
        if attached:
            _user32.AttachThreadInput(me, other, False)
        return hwnd

    def _bring_to_front(thread_id: int, stop: threading.Event) -> None:
        """Once the dialog of ``thread_id`` shows, make it the foreground window. Windows only
        lets the process the user last used take the foreground; joining the input of the
        current foreground thread (the browser) for a moment lifts that rule."""
        def find() -> int | None:
            found: list[int] = []

            def cb(hwnd, _):
                name = ctypes.create_unicode_buffer(16)
                _user32.GetClassNameW(hwnd, name, 16)
                if name.value == "#32770" and _user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                    return False
                return True
            _user32.EnumThreadWindows(thread_id, _EnumProc(cb), 0)
            return found[0] if found else None

        for _ in range(100):  # up to 5 s
            if stop.wait(0.05):
                return
            dialog = find()
            if dialog:
                front = _user32.GetForegroundWindow()
                if front == dialog:
                    return
                other = _user32.GetWindowThreadProcessId(front, None) if front else 0
                attached = bool(other and other != thread_id and _user32.AttachThreadInput(thread_id, other, True))
                _user32.BringWindowToTop(dialog)
                _user32.SetForegroundWindow(dialog)
                if attached:
                    _user32.AttachThreadInput(thread_id, other, False)
                return

    def _windows_dialog(initial: Path, title: str) -> str | None:
        hr = _ole32.CoInitializeEx(None, 0x2) & 0xFFFFFFFF  # apartment threaded, as the dialog needs
        if hr & 0x80000000:
            raise PickerUnavailable(f"CoInitializeEx failed (0x{hr:08X})")
        owner = None
        try:
            dialog = ctypes.c_void_p()
            _check(_ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_FileOpenDialog)), None, 1,
                                           ctypes.byref(_guid(_IID_IFileOpenDialog)), ctypes.byref(dialog)) & 0xFFFFFFFF,
                   "Creating the folder dialog")
            try:
                options = wintypes.DWORD()
                _call(dialog, _GET_OPTIONS, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options))
                _call(dialog, _SET_OPTIONS, [wintypes.DWORD],
                      options.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST)
                _call(dialog, _SET_TITLE, [ctypes.c_wchar_p], title)
                _call(dialog, _SET_OK_LABEL, [ctypes.c_wchar_p], OK_LABEL)
                if initial.is_dir():
                    item = ctypes.c_void_p()
                    if _shell32.SHCreateItemFromParsingName(str(initial), None, ctypes.byref(_guid(_IID_IShellItem)),
                                                            ctypes.byref(item)) == 0:
                        _call(dialog, _SET_FOLDER, [ctypes.c_void_p], item)  # always start here
                        _call(item, _RELEASE, [])
                owner = _front_owner()
                shown = threading.Event()
                threading.Thread(target=_bring_to_front, args=(_kernel32.GetCurrentThreadId(), shown),
                                 name="folder-picker-front", daemon=True).start()
                try:
                    hr = _call(dialog, _SHOW, [wintypes.HWND], owner)
                finally:
                    shown.set()
                if hr == _CANCELLED:
                    return None
                _check(hr, "Showing the folder dialog")
                result = ctypes.c_void_p()
                _check(_call(dialog, _GET_RESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(result)),
                       "Reading the chosen folder")
                try:
                    text = ctypes.c_void_p()
                    _check(_call(result, _GET_DISPLAY_NAME, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                                 _SIGDN_FILESYSPATH, ctypes.byref(text)), "Reading the folder path")
                    try:
                        return ctypes.wstring_at(text.value)
                    finally:
                        _ole32.CoTaskMemFree(text)
                finally:
                    _call(result, _RELEASE, [])
            finally:
                _call(dialog, _RELEASE, [])
        finally:
            if owner:
                _user32.DestroyWindow(owner)
            _ole32.CoUninitialize()

    def _windows_pick(initial: Path, title: str) -> str | None:
        # A thread of its own: COM must be "apartment threaded" there, and the web server's
        # worker threads may already be set up otherwise.
        box: dict = {}

        def run():
            try:
                box["path"] = _windows_dialog(initial, title)
            except Exception as exc:  # reported to the page, never crashes the hub
                box["error"] = exc

        t = threading.Thread(target=run, name="folder-picker", daemon=True)
        t.start()
        t.join()
        if "error" in box:
            err = box["error"]
            raise err if isinstance(err, PickerUnavailable) else PickerUnavailable(str(err))
        return box.get("path")
else:
    def _windows_known_folder(guid_text: str) -> Path | None:
        return None

    def _windows_pick(initial: Path, title: str) -> str | None:
        raise PickerUnavailable("Not Windows")
