"""Symbol im Infobereich der Taskleiste (System Tray) – reines Win32 über ctypes.

Linksklick/Doppelklick öffnet die Oberfläche, Rechtsklick zeigt ein Menü
(Oberfläche öffnen · Alle Server stoppen · Manager beenden). Der Tooltip zeigt,
wie viele Server laufen. Nur unter Windows; anderswo ist `available` False.
"""
from __future__ import annotations

import ctypes
import logging
import pathlib
import sys
import threading

log = logging.getLogger("mcsm.tray")
available = sys.platform == "win32"

ID_OPEN, ID_STOP, ID_QUIT = 1, 2, 3
CLASS_NAME = "MCSMTrayWindow"

if available:
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    WM_NULL, WM_DESTROY, WM_CLOSE, WM_COMMAND = 0x0000, 0x0002, 0x0010, 0x0111
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
    NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
    WM_TRAY = 0x0400 + 1
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
    NIIF_INFO, NIIF_USER, NIIF_LARGE_ICON = 0x1, 0x4, 0x20
    NOTIFYICON_VERSION_4 = 4
    MF_STRING, MF_SEPARATOR = 0x0, 0x800
    TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x0100, 0x0002, 0x0020
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
    SM_CXSMICON, SM_CXICON = 49, 11

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                    ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                    ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                    ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wt.HICON)]

    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wt.ATOM
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.AppendMenuW.restype = wt.BOOL
    user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.LoadImageW.restype = wt.HANDLE
    user32.DestroyIcon.argtypes = [wt.HICON]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowW.restype = wt.HWND
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wt.BOOL
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wt.HMODULE


class Tray:
    """Tray-Symbol mit eigener Nachrichtenschleife in einem Hintergrund-Thread."""

    def __init__(self, icon: pathlib.Path, tip: str, on_open, on_stop_all, on_quit) -> None:
        self.icon_path = pathlib.Path(icon)
        self.tip = tip
        self.on_open, self.on_stop_all, self.on_quit = on_open, on_stop_all, on_quit
        self.hwnd = None
        self.nid = None
        self.hicon_small = None
        self.hicon_big = None
        self.ready = threading.Event()
        self.ok = False
        self._proc = None                 # Referenz auf den ctypes-Callback (sonst räumt ihn der GC ab)
        self._thread: threading.Thread | None = None

    # -- Lebenszyklus
    def start(self, timeout: float = 5.0) -> bool:
        if not available:
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self.ready.wait(timeout)
        return self.ok

    def stop(self) -> None:
        if available and self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if self._thread:
                self._thread.join(3)

    # -- Anzeige
    def set_tip(self, text: str) -> None:
        if not (available and self.ok and self.nid):
            return
        self.nid.uFlags = NIF_TIP | NIF_SHOWTIP
        self.nid.szTip = text[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def notify(self, title: str, text: str) -> None:
        """Sprechblase neben dem Symbol."""
        if not (available and self.ok and self.nid):
            return
        self.nid.uFlags = NIF_INFO | NIF_SHOWTIP
        self.nid.szInfoTitle = title[:63]
        self.nid.szInfo = text[:255]
        self.nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON if self.hicon_big else NIIF_INFO
        self.nid.hBalloonIcon = self.hicon_big or None
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    # -- Intern
    def _run(self) -> None:
        try:
            self._create()
        except Exception:                             # noqa: BLE001 – Tray ist Komfort, nie Pflicht
            log.exception("Tray-Symbol konnte nicht angelegt werden")
            self.ready.set()
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._destroy()

    def _create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):                  # ERROR_CLASS_ALREADY_EXISTS
                raise OSError(f"RegisterClassW fehlgeschlagen ({err})")
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, "Minecraft Server Manager", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW fehlgeschlagen")

        small = user32.GetSystemMetrics(SM_CXSMICON) or 16
        big = user32.GetSystemMetrics(SM_CXICON) or 32
        if self.icon_path.exists():
            self.hicon_small = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, small, small, LR_LOADFROMFILE)
            self.hicon_big = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, big, big, LR_LOADFROMFILE)

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon_small or None
        nid.szTip = self.tip[:127]
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("Shell_NotifyIcon(NIM_ADD) fehlgeschlagen")
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self.nid = nid
        self.ok = True
        self.ready.set()
        log.info("Tray-Symbol aktiv")

    def _destroy(self) -> None:
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
        for h in (self.hicon_small, self.hicon_big):
            if h:
                user32.DestroyIcon(h)
        self.hicon_small = self.hicon_big = None
        self.ok = False

    def _menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN, "Oberfläche öffnen")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, ID_STOP, "Alle Server stoppen")
        user32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "Manager beenden")
        user32.SetMenuDefaultItem(hmenu, ID_OPEN, 0)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)       # sonst schließt sich das Menü nicht beim Wegklicken
        cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd:
            self._dispatch(cmd)

    def _dispatch(self, cmd: int) -> None:
        handler = {ID_OPEN: self.on_open, ID_STOP: self.on_stop_all, ID_QUIT: self.on_quit}.get(cmd)
        if handler:
            # Nie in der Nachrichtenschleife blockieren (Server stoppen kann Sekunden dauern).
            threading.Thread(target=handler, daemon=True).start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT):
                self._dispatch(ID_OPEN)
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if msg == WM_COMMAND:
            self._dispatch(wparam & 0xFFFF)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def find_window():
    """HWND eines laufenden Tray-Fensters (für Tests/Steuerung aus anderen Prozessen)."""
    if not available:
        return None
    return user32.FindWindowW(CLASS_NAME, None) or None
