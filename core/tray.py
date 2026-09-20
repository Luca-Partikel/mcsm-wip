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
import time

log = logging.getLogger("mcsm.tray")
available = sys.platform == "win32"

ID_OPEN, ID_STOP, ID_QUIT = 1, 2, 3
CLASS_NAME = "MCSMTrayWindow"

if available:
    import ctypes.wintypes as wt

    # Eigene DLL-Handles (nicht ctypes.windll): nur so liefert ctypes.get_last_error() den echten
    # Win32-Fehlercode, und die argtypes bleiben lokal zu diesem Modul.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ERROR_CLASS_ALREADY_EXISTS = 1410

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
    BASE_FLAGS = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP   # Grundzustand des Symbols (ohne Sprechblase)
    MSGFLT_ALLOW = 1
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

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
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
    user32.UnregisterClassW.restype = wt.BOOL
    user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wt.UINT
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wt.BOOL
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    # Explorer meldet mit dieser Nachricht, dass die Taskleiste neu erzeugt wurde (Neustart, Absturz, DPI-Wechsel).
    WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")


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
        self._lock = threading.Lock()     # Shell_NotifyIcon-Aufrufe (Tooltip-Thread, Sprechblase, Neuanmeldung)
        self._abandoned = False           # start() hat aufgegeben – der Thread räumt dann selbst auf
        self._dblclick_at = 0.0           # Zeitpunkt des letzten Doppelklicks (das folgende NIN_SELECT wird ignoriert)
        self._last_open = 0.0             # Zeitpunkt des letzten „Oberfläche öffnen“ (Entprellung)

    # -- Lebenszyklus
    def start(self, timeout: float = 5.0) -> bool:
        if not available:
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout):
            self._abandoned = True
            log.warning("Tray-Symbol: Taskleiste hat nicht innerhalb von %.0f s geantwortet", timeout)
        return self.ok

    def stop(self, timeout: float = 3.0) -> None:
        if available and self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if self._thread:
                self._thread.join(timeout)

    # -- Anzeige
    def set_tip(self, text: str) -> None:
        if not (available and self.ok and self.nid):
            return
        with self._lock:
            self.nid.szTip = text[:127]               # bleibt im Grundzustand, damit ein Neu-Anmelden den Text behält
            nid = NOTIFYICONDATAW.from_buffer_copy(self.nid)
            nid.uFlags = NIF_TIP | NIF_SHOWTIP
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def notify(self, title: str, text: str) -> None:
        """Sprechblase neben dem Symbol."""
        if not (available and self.ok and self.nid):
            return
        with self._lock:
            # Kopie senden: self.nid bleibt ohne NIF_INFO/szInfo, sonst würde die Sprechblase
            # nach einem Explorer-Neustart beim Neu-Anmelden wiederholt.
            nid = NOTIFYICONDATAW.from_buffer_copy(self.nid)
            nid.uFlags = NIF_INFO | NIF_SHOWTIP
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON if self.hicon_big else NIIF_INFO
            nid.hBalloonIcon = self.hicon_big or None
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # -- Intern
    def _run(self) -> None:
        try:
            # Nur dieser Thread erzeugt Fenster: DPI-bewusst schalten, damit GetSystemMetrics die
            # skalierte Symbolgröße liefert (20/24 px bei 125/150 %) und LoadImageW den passenden Frame wählt.
            # Der Python-Prozess selbst ist DPI-unaware; sonst kämen immer 16/32 px und Explorer müsste strecken.
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        except (AttributeError, OSError):             # Windows 10 vor 1703
            pass
        try:
            self._create()
        except Exception:                             # noqa: BLE001 – Tray ist Komfort, nie Pflicht
            log.exception("Tray-Symbol konnte nicht angelegt werden")
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            self._destroy()
            self.ready.set()
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None
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
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(err)
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, "Minecraft Server Manager", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        try:                                          # nur relevant, falls erhöht gestartet: TaskbarCreated durchlassen
            user32.ChangeWindowMessageFilterEx.argtypes = [wt.HWND, wt.UINT, wt.DWORD, ctypes.c_void_p]
            user32.ChangeWindowMessageFilterEx(self.hwnd, WM_TASKBARCREATED, MSGFLT_ALLOW, None)
        except (AttributeError, OSError):
            pass

        small = user32.GetSystemMetrics(SM_CXSMICON) or 16
        big = user32.GetSystemMetrics(SM_CXICON) or 32
        if self.icon_path.exists():
            self.hicon_small = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, small, small, LR_LOADFROMFILE)
            self.hicon_big = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, big, big, LR_LOADFROMFILE)

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon_small or None
        nid.szTip = self.tip[:127]
        if not self._add_icon(nid):
            raise ctypes.WinError(ctypes.get_last_error())   # z. B. 1460 ERROR_TIMEOUT: Taskleiste antwortet nicht
        if self._abandoned:
            # start() hat schon aufgegeben – niemand würde stop() rufen, also gleich wieder abmelden.
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            raise OSError("Tray-Symbol zu spät angelegt – wieder entfernt")
        self.nid = nid
        self.ok = True
        self.ready.set()
        log.info("Tray-Symbol aktiv")

    def _add_icon(self, nid) -> bool:
        """Symbol (neu) anmelden. Laut Doku ist NIM_SETVERSION nach JEDEM NIM_ADD nötig."""
        nid.uFlags = BASE_FLAGS
        nid.szInfo = ""
        nid.szInfoTitle = ""                          # keine alte Sprechblase wiederholen
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return False
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        return True

    def _destroy(self) -> None:
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
        for h in (self.hicon_small, self.hicon_big):
            if h:
                user32.DestroyIcon(h)
        self.hicon_small = self.hicon_big = None
        self.ok = False
        # Fenster ist weg (WM_QUIT bzw. DestroyWindow) – Klasse freigeben, damit ein späterer Tray
        # im selben Prozess nicht den alten WNDPROC-Thunk wiederverwendet.
        user32.UnregisterClassW(CLASS_NAME, kernel32.GetModuleHandleW(None))

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
        if cmd == ID_OPEN:
            # Ein Doppelklick liefert erst NIN_SELECT, dann WM_LBUTTONDBLCLK – die Oberfläche soll trotzdem
            # nur einmal aufgehen (jeder Aufruf öffnet ein neues Edge-Fenster).
            now = time.monotonic()
            if now - self._last_open < 0.6:
                return
            self._last_open = now
        if handler:
            # Nie in der Nachrichtenschleife blockieren (Server stoppen kann Sekunden dauern).
            threading.Thread(target=handler, daemon=True).start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF                   # NOTIFYICON_VERSION_4: LOWORD(lParam) = Ereignis
            if event == WM_LBUTTONDBLCLK:
                # Nach dem Doppelklick schickt die Shell noch ein zweites NIN_SELECT – das ignorieren.
                # Zeitbasiert statt als Merker: bleibt das NIN_SELECT aus, würde ein Merker sonst den
                # nächsten (viel späteren) Einzelklick verschlucken.
                self._dblclick_at = time.monotonic()
                self._dispatch(ID_OPEN)
            elif event in (NIN_SELECT, NIN_KEYSELECT):   # Linksklick bzw. Leertaste/Enter
                if time.monotonic() - self._dblclick_at > 0.5:
                    self._dispatch(ID_OPEN)
            elif event == WM_CONTEXTMENU:              # Rechtsklick bzw. Shift+F10
                self._menu()
            # WM_LBUTTONUP / WM_RBUTTONUP kommen unter Version 4 ZUSÄTZLICH an -> bewusst nicht behandeln,
            # sonst wird jeder Klick doppelt ausgelöst (zwei Fenster, verschachteltes Menü).
            return 0
        if msg == WM_COMMAND:
            self._dispatch(wparam & 0xFFFF)
            return 0
        if WM_TASKBARCREATED and msg == WM_TASKBARCREATED and self.nid is not None:
            # Explorer wurde neu gestartet (Absturz, „Windows-Explorer neu starten“, DPI-Wechsel): alle
            # Symbole sind weg und müssen neu angemeldet werden. Direkt danach kann die Taskleiste noch
            # nicht bereit sein (ERROR_TIMEOUT), daher ein paar kurze Versuche.
            with self._lock:
                for attempt in range(3):
                    if self._add_icon(self.nid):
                        log.info("Tray-Symbol nach Explorer-Neustart wiederhergestellt")
                        break
                    if attempt < 2:
                        time.sleep(1)
                else:
                    log.warning("Tray-Symbol nach Explorer-Neustart nicht wiederhergestellt (%s)",
                                ctypes.get_last_error())
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def find_window(pid: int | None = None):
    """HWND eines laufenden Tray-Fensters (für Tests/Steuerung aus anderen Prozessen).
    Mit `pid` nur das Fenster dieses Prozesses – es können mehrere Manager laufen (Installation + Testkopie)."""
    if not available:
        return None
    hwnd = None
    while True:
        hwnd = user32.FindWindowExW(None, hwnd, CLASS_NAME, None)
        if not hwnd:
            return None
        if pid is None:
            return hwnd
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            return hwnd
