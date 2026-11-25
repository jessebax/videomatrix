import ctypes
from ctypes import wintypes
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
import winreg

import numpy as np
import pyvirtualcam
import SpoutGL
from SpoutGL.enums import GL_RGB

try:
    import NDIlib as ndi
except (ImportError, OSError) as exc:
    ndi = None
    NDI_IMPORT_ERROR = exc
else:
    NDI_IMPORT_ERROR = None

# --- Win32 API Setup for Background Window Capture (PrintWindow) ---
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

# Make process DPI-aware so window coordinates & pixel buffers match 1:1
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0
PREVIEW_WIDTH = 220
PREVIEW_HEIGHT = 110
STREAM_FPS = 60
SPI_GETWORKAREA = 0x0030
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
BROWSER_PROCESS_NAMES = {
    "arc.exe",
    "brave.exe",
    "chrome.exe",
    "chromium.exe",
    "firefox.exe",
    "iexplore.exe",
    "librewolf.exe",
    "msedge.exe",
    "opera.exe",
    "opera_gx.exe",
    "safari.exe",
    "tor.exe",
    "vivaldi.exe",
    "waterfox.exe",
    "zen.exe",
}
SETTINGS_PROCESS_NAMES = {
    "systemsettings.exe",
    "systemsettingsadminflows.exe",
}
VIDEO_INPUT_DEVICE_CATEGORY = (
    r"SOFTWARE\Classes\CLSID"
    r"\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\Instance"
)
UNITY_CAPTURE_FILTER_CLSID_PREFIX = (
    "5c2cd55c-92ad-4999-8666-912bd3e700"
)


def list_unity_capture_devices():
    is_64_bit = ctypes.sizeof(ctypes.c_void_p) == 8
    registry_view = (
        winreg.KEY_WOW64_64KEY if is_64_bit else winreg.KEY_WOW64_32KEY
    )
    first_device_suffix = 0x10 if is_64_bit else 0x20
    access = winreg.KEY_READ | registry_view

    try:
        category_key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, VIDEO_INPUT_DEVICE_CATEGORY, 0, access
        )
    except FileNotFoundError:
        return []

    names = set()
    with category_key:
        subkey_count = winreg.QueryInfoKey(category_key)[0]
        for index in range(subkey_count):
            clsid = winreg.EnumKey(category_key, index).strip("{}").lower()
            if not clsid.startswith(UNITY_CAPTURE_FILTER_CLSID_PREFIX):
                continue
            suffix = int(clsid[-2:], 16)
            if suffix != first_device_suffix and suffix < first_device_suffix + 2:
                continue

            try:
                device_key = winreg.OpenKey(category_key, "{" + clsid + "}")
            except FileNotFoundError:
                continue
            with device_key:
                try:
                    name, _ = winreg.QueryValueEx(device_key, "FriendlyName")
                except FileNotFoundError:
                    continue
                names.add(name)

    return sorted(names, key=str.casefold)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


def get_process_name(hwnd):
    """Returns the executable name (e.g. 'waaavepool.exe') owning hwnd."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "unknown.exe"

    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return f"PID:{pid.value}"

    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if hasattr(kernel32, "QueryFullProcessImageNameW"):
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        if psapi.GetModuleBaseNameW(hproc, None, buf, 512):
            return buf.value
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(hproc)

    return f"PID:{pid.value}"


def get_window_title_safe(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value.strip()


def get_window_class_name(hwnd):
    buff = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buff, 256)
    return buff.value.strip()


def get_window_client_size(hwnd, include_minimized=False):
    """Returns even (width, height) for the interior client area of hwnd."""
    if not user32.IsWindow(hwnd) or (user32.IsIconic(hwnd) and not include_minimized):
        return None

    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 32 or height < 32:
        return None

    # Ensure even dimensions (required by virtual camera drivers)
    width = width - (width % 2)
    height = height - (height % 2)
    if width <= 0 or height <= 0:
        return None

    return width, height


def list_all_capturable_windows(include_hidden=False, include_minimized=False):
    """
    Enumerates top-level windows and larger child windows, including titleless
    OpenGL/SDL/GLFW/openFrameworks output windows.
    """
    found = {}

    def record_hwnd(hwnd, is_child=False):
        if hwnd in found:
            return
        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        if (not visible and not include_hidden) or (
            minimized and not include_minimized
        ):
            return

        size = get_window_client_size(hwnd, include_minimized=include_minimized)
        if not size:
            return
        width, height = size

        title = get_window_title_safe(hwnd)
        cls_name = get_window_class_name(hwnd)
        proc_name = get_process_name(hwnd)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if title in ("Program Manager", "Windows Input Experience") or cls_name in (
            "Progman",
            "WorkerW",
            "Shell_TrayWnd",
        ):
            return

        display_title = title if title else f"[Untitled {cls_name}]"
        prefix = "  └─ " if is_child else ""
        label = f"{prefix}[{proc_name}] {display_title}  ({width}x{height})  HWND:{hex(hwnd)}"
        found[hwnd] = {
            "hwnd": hwnd,
            "proc": proc_name,
            "title": display_title,
            "class": cls_name,
            "width": width,
            "height": height,
            "label": label,
            "pid": pid.value,
            "visible": visible,
            "minimized": minimized,
        }

    def enum_child_cb(child_hwnd, lparam):
        size = get_window_client_size(
            child_hwnd, include_minimized=include_minimized
        )
        if size and size[0] >= 200 and size[1] >= 150:
            record_hwnd(child_hwnd, is_child=True)
        return True

    def enum_top_cb(hwnd, lparam):
        record_hwnd(hwnd, is_child=False)
        user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_child_cb), 0)
        return True

    user32.EnumWindows(WNDENUMPROC(enum_top_cb), 0)
    return list(found.values())


class Win32WindowCapturer:
    """
    Captures the interior client area of a specific HWND directly from Windows'
    compositor surface via PrintWindow(..., PW_CLIENTONLY | PW_RENDERFULLCONTENT).
    Overlapping windows on top of the target window are never included.
    """

    def __init__(self, hwnd, width, height):
        self.hwnd = hwnd
        self.width = width
        self.height = height

        self.hwnd_dc = user32.GetDC(hwnd)
        self.mem_dc = gdi32.CreateCompatibleDC(self.hwnd_dc)
        self.bitmap = gdi32.CreateCompatibleBitmap(self.hwnd_dc, width, height)
        self.old_obj = gdi32.SelectObject(self.mem_dc, self.bitmap)

        self.bmi = BITMAPINFO()
        self.bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.bmi.bmiHeader.biWidth = width
        # Negative height specifies a top-down DIB so rows are ordered top-to-bottom
        self.bmi.bmiHeader.biHeight = -height
        self.bmi.bmiHeader.biPlanes = 1
        self.bmi.bmiHeader.biBitCount = 32
        self.bmi.bmiHeader.biCompression = BI_RGB

        self.buffer = np.empty((height, width, 4), dtype=np.uint8)

    def grab_rgb(self):
        if not user32.IsWindow(self.hwnd) or user32.IsIconic(self.hwnd):
            return None

        # PW_CLIENTONLY (1) | PW_RENDERFULLCONTENT (2) captures OpenGL/DirectX window content cleanly
        ok = user32.PrintWindow(self.hwnd, self.mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(self.hwnd, self.mem_dc, PW_CLIENTONLY)
        if not ok:
            return None

        lines = gdi32.GetDIBits(
            self.mem_dc,
            self.bitmap,
            0,
            self.height,
            self.buffer.ctypes.data_as(ctypes.c_void_p),
            ctypes.byref(self.bmi),
            DIB_RGB_COLORS,
        )
        if lines != self.height:
            return None

        # Convert BGRA -> contiguous RGB
        return np.ascontiguousarray(self.buffer[:, :, 2::-1])

    def close(self):
        try:
            if self.mem_dc and self.old_obj:
                gdi32.SelectObject(self.mem_dc, self.old_obj)
            if self.bitmap:
                gdi32.DeleteObject(self.bitmap)
            if self.mem_dc:
                gdi32.DeleteDC(self.mem_dc)
            if self.hwnd_dc:
                user32.ReleaseDC(self.hwnd, self.hwnd_dc)
        except Exception:
            pass
        self.mem_dc = None
        self.bitmap = None
        self.hwnd_dc = None


class WindowBroadcasterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Matrix")
        work_area = wintypes.RECT()
        if not user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(work_area), 0
        ):
            raise ctypes.WinError()
        self.work_area = (
            work_area.left,
            work_area.top,
            work_area.right,
            work_area.bottom,
        )
        work_width = work_area.right - work_area.left
        work_height = work_area.bottom - work_area.top
        self.root.geometry(
            f"{work_width // 2}x{work_height}+{work_area.left}+{work_area.top}"
        )
        self.root.resizable(True, True)
        self._work_area_binding_id = self.root.bind(
            "<Map>", self._on_window_map, add="+"
        )

        self.is_streaming = False
        self.windows_list = []
        self.selected_hwnd = None
        self.preview_cards = {}
        self.preview_cache = {}
        self.collapsed_groups = set()
        self.group_buttons = {}
        self.grid_columns = 3
        self.advanced_expanded = False
        self.preview_queue = queue.Queue()
        self.preview_stop_event = threading.Event()
        self.preview_pause_event = threading.Event()
        self.preview_state_lock = threading.Lock()
        self.preview_idle_event = threading.Event()
        self.preview_idle_event.set()
        self.preview_interval_s = 3

        self.capturer = None
        self.target_hwnd = None
        self.spout_senders = []
        self.virtual_cameras = []
        self.ndi_initialized = False
        self.ndi_sender = None
        self.ndi_video_frame = None
        self.ndi_buffer = None
        self.available_virtual_camera_devices = []
        self.stream_width = 0
        self.stream_height = 0
        self.frame_interval_ms = max(1, int(1000 / STREAM_FPS))

        self._build_ui()
        self.refresh_virtual_camera_devices()
        self.refresh_windows()
        self.preview_thread = threading.Thread(target=self._preview_worker, daemon=True)
        self.preview_thread.start()
        self.root.after(100, self._drain_preview_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_window_map(self, _event=None):
        if self._work_area_binding_id:
            self.root.unbind("<Map>", self._work_area_binding_id)
            self._work_area_binding_id = None
        self.root.after(100, self._fit_window_to_work_area)

    def _fit_window_to_work_area(self):
        left, top, right, bottom = self.work_area
        hwnd = user32.GetParent(self.root.winfo_id())
        if not hwnd:
            hwnd = self.root.winfo_id()
        # Tk geometry sizes the client area; set the decorated outer window directly.
        if not user32.SetWindowPos(
            hwnd,
            None,
            left,
            top,
            (right - left) // 2,
            bottom - top,
            SWP_NOZORDER | SWP_NOACTIVATE,
        ):
            messagebox.showerror(
                "Display Layout Error",
                f"Could not fit the window within the desktop work area: "
                f"{ctypes.WinError()}",
            )

    def _build_ui(self):
        header_frame = tk.Frame(self.root)
        header_frame.pack(fill="x", padx=12, pady=(10, 4))

        tk.Label(
            header_frame,
            text="Select a target window (isolated window capture):",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", anchor="w")

        self.refresh_btn = ttk.Button(header_frame, text="Refresh", command=self.refresh_windows)
        self.refresh_btn.pack(side="right")

        self.advanced_frame = tk.Frame(self.root, bd=1, relief="solid")
        self.advanced_frame.pack(fill="x", padx=12, pady=(0, 4))
        self.advanced_button = ttk.Button(
            self.advanced_frame,
            text="▶ Advanced",
            command=self._toggle_advanced,
        )
        self.advanced_button.pack(anchor="w", padx=4, pady=2)
        self.advanced_options_frame = tk.Frame(self.advanced_frame, padx=8, pady=4)
        self.show_hidden_var = tk.BooleanVar(value=False)
        self.show_minimized_var = tk.BooleanVar(value=False)
        self.show_explorer_var = tk.BooleanVar(value=False)
        self.show_browsers_var = tk.BooleanVar(value=False)
        self.show_settings_var = tk.BooleanVar(value=False)
        self.show_application_frame_host_var = tk.BooleanVar(value=False)
        self.show_spout_output_var = tk.BooleanVar(value=True)
        self.show_virtual_camera_output_var = tk.BooleanVar(value=True)
        self.show_ndi_output_var = tk.BooleanVar(value=False)
        self.show_hidden_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show hidden windows",
            variable=self.show_hidden_var,
            command=self.refresh_windows,
        )
        self.show_hidden_check.grid(row=0, column=0, sticky="w", padx=(0, 18))
        self.show_minimized_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show minimized windows",
            variable=self.show_minimized_var,
            command=self.refresh_windows,
        )
        self.show_minimized_check.grid(row=0, column=1, sticky="w")
        self.show_explorer_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show explorer.exe windows",
            variable=self.show_explorer_var,
            command=self.refresh_windows,
        )
        self.show_explorer_check.grid(row=1, column=0, sticky="w", padx=(0, 18))
        self.show_browsers_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show web browser windows",
            variable=self.show_browsers_var,
            command=self.refresh_windows,
        )
        self.show_browsers_check.grid(row=1, column=1, sticky="w")
        self.show_settings_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show Settings windows",
            variable=self.show_settings_var,
            command=self.refresh_windows,
        )
        self.show_settings_check.grid(row=2, column=0, sticky="w", padx=(0, 18))
        self.show_application_frame_host_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show ApplicationFrameHost.exe",
            variable=self.show_application_frame_host_var,
            command=self.refresh_windows,
        )
        self.show_application_frame_host_check.grid(row=2, column=1, sticky="w")
        self.show_spout_output_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show Spout output",
            variable=self.show_spout_output_var,
            command=self._update_output_visibility,
        )
        self.show_spout_output_check.grid(row=3, column=0, sticky="w", padx=(0, 18))
        self.show_virtual_camera_output_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show Virtual camera output",
            variable=self.show_virtual_camera_output_var,
            command=self._update_output_visibility,
        )
        self.show_virtual_camera_output_check.grid(
            row=3, column=1, sticky="w"
        )
        self.show_ndi_output_check = ttk.Checkbutton(
            self.advanced_options_frame,
            text="Show NDI output",
            variable=self.show_ndi_output_var,
            command=self._update_output_visibility,
        )
        self.show_ndi_output_check.grid(row=4, column=0, sticky="w", padx=(0, 18))

        grid_frame = tk.Frame(self.root, bd=1, relief="sunken")
        grid_frame.pack(fill="both", expand=True, padx=12, pady=(2, 6))

        self.window_canvas = tk.Canvas(grid_frame, highlightthickness=0)
        self.window_scrollbar = ttk.Scrollbar(
            grid_frame, orient="vertical", command=self.window_canvas.yview
        )
        self.window_canvas.configure(yscrollcommand=self.window_scrollbar.set)
        self.window_scrollbar.pack(side="right", fill="y")
        self.window_canvas.pack(side="left", fill="both", expand=True)
        self.window_grid = tk.Frame(self.window_canvas)
        self.window_grid_id = self.window_canvas.create_window(
            (0, 0), window=self.window_grid, anchor="nw"
        )
        self.window_grid.bind("<Configure>", self._update_grid_scrollregion)
        self.window_canvas.bind("<Configure>", self._resize_grid)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        hwnd_frame = tk.Frame(self.root)
        hwnd_frame.pack(fill="x", padx=12, pady=(0, 4))

        tk.Label(hwnd_frame, text="Or enter HWND manually (hex, e.g. 0x1a04b2):", fg="#444444").pack(side="left")
        self.manual_hwnd_var = tk.StringVar()
        self.manual_hwnd_entry = ttk.Entry(hwnd_frame, textvariable=self.manual_hwnd_var, width=16)
        self.manual_hwnd_entry.pack(side="left", padx=(6, 0))

        options_frame = tk.Frame(self.root)
        options_frame.pack(fill="x", padx=12)

        outputs_frame = tk.Frame(options_frame)
        outputs_frame.pack(fill="x")
        self.spout_outputs = []
        self.virtual_camera_outputs = []
        self.send_spout_var = tk.BooleanVar(value=True)
        self.send_virtual_camera_var = tk.BooleanVar(value=True)

        self.spout_section = tk.LabelFrame(
            outputs_frame, text="Spout senders", padx=6, pady=4
        )
        self.spout_section.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.send_spout_check = ttk.Checkbutton(
            self.spout_section,
            text="Send Spout output",
            variable=self.send_spout_var,
            command=self._update_output_controls_state,
        )
        self.send_spout_check.pack(anchor="w", pady=(0, 3))
        self.spout_rows_frame = tk.Frame(self.spout_section)
        self.spout_rows_frame.pack(fill="x")
        self.add_spout_output_btn = ttk.Button(
            self.spout_section,
            text="Add Spout sender",
            command=self._add_spout_output,
        )
        self.add_spout_output_btn.pack(anchor="w", pady=(3, 0))

        self.camera_section = tk.LabelFrame(
            outputs_frame, text="Virtual cameras", padx=6, pady=4
        )
        self.camera_section.pack(side="left", fill="both", expand=True)
        self.send_virtual_camera_check = ttk.Checkbutton(
            self.camera_section,
            text="Send virtual camera output",
            variable=self.send_virtual_camera_var,
            command=self._update_output_controls_state,
        )
        self.send_virtual_camera_check.pack(anchor="w", pady=(0, 3))
        tk.Label(
            self.camera_section,
            text="Select an installed UnityCapture device:",
            fg="#555555",
        ).pack(anchor="w")
        self.camera_device_status_label = tk.Label(
            self.camera_section, text="Checking installed devices...", fg="#555555"
        )
        self.camera_device_status_label.pack(anchor="w")
        self.virtual_camera_rows_frame = tk.Frame(self.camera_section)
        self.virtual_camera_rows_frame.pack(fill="x")
        camera_buttons_frame = tk.Frame(self.camera_section)
        camera_buttons_frame.pack(fill="x", pady=(3, 0))
        self.add_virtual_camera_btn = ttk.Button(
            camera_buttons_frame,
            text="Add virtual camera",
            command=self._add_virtual_camera_output,
        )
        self.add_virtual_camera_btn.pack(side="left")
        self.refresh_camera_devices_btn = ttk.Button(
            camera_buttons_frame,
            text="Refresh devices",
            command=self.refresh_virtual_camera_devices,
        )
        self.refresh_camera_devices_btn.pack(side="left", padx=(4, 0))

        install_camera_section = tk.LabelFrame(
            self.camera_section,
            text="Add camera (install a device)",
            padx=6,
            pady=4,
        )
        install_camera_section.pack(fill="x", pady=(6, 0))
        tk.Label(
            install_camera_section,
            text=(
                "Use UnityCapture's multiple-device installer to register "
                "additional virtual cameras. After installation, refresh the device "
                "list. Administrator approval may be required."
            ),
            fg="#555555",
            wraplength=440,
            justify="left",
        ).pack(anchor="w", fill="x")
        self.install_camera_instructions_link = self._create_hyperlink(
            install_camera_section,
            "Open installation instructions",
            self._open_camera_install_instructions,
        )

        self.ndi_section = tk.LabelFrame(
            outputs_frame, text="NDI output", padx=6, pady=4
        )
        self.ndi_section.pack(side="left", fill="both", expand=True)
        self.send_ndi_var = tk.BooleanVar(value=False)
        self.send_ndi_check = ttk.Checkbutton(
            self.ndi_section,
            text="Send NDI output",
            variable=self.send_ndi_var,
            command=self._update_output_controls_state,
        )
        self.send_ndi_check.pack(anchor="w", pady=(0, 3))
        tk.Label(
            self.ndi_section,
            text="Source name:",
            fg="#555555",
        ).pack(anchor="w")
        self.ndi_name_var = tk.StringVar(value="Video Matrix NDI")
        self.ndi_name_entry = ttk.Entry(
            self.ndi_section, textvariable=self.ndi_name_var
        )
        self.ndi_name_entry.pack(fill="x")
        self.ndi_install_instructions_link = self._create_hyperlink(
            self.ndi_section,
            "Open installation instructions",
            self._open_ndi_install_instructions,
        )

        self._add_spout_output("Waaavepool Output")
        self._add_virtual_camera_output()
        self._update_output_visibility()

        self.toggle_btn = ttk.Button(self.root, text="Start Window Broadcast", command=self.toggle_stream)
        self.toggle_btn.pack(fill="x", padx=12, pady=(7, 5), ipady=5)

        self.status_var = tk.StringVar(value="Status: Ready")
        status_label = tk.Label(self.root, textvariable=self.status_var, fg="#444444", anchor="w", wraplength=1000)
        status_label.pack(fill="x", padx=12, pady=(0, 6))

    def _create_hyperlink(self, parent, text, command):
        link = tk.Label(
            parent,
            text=text,
            fg="#0563C1",
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
        )
        link.pack(anchor="w", pady=(4, 0))
        link.bind("<Button-1>", lambda _event: command())
        link.bind(
            "<Enter>",
            lambda _event: link.configure(fg="#004A99"),
        )
        link.bind(
            "<Leave>",
            lambda _event: link.configure(fg="#0563C1"),
        )
        return link

    def _update_output_visibility(self):
        sections = (
            (self.spout_section, self.show_spout_output_var),
            (self.camera_section, self.show_virtual_camera_output_var),
            (self.ndi_section, self.show_ndi_output_var),
        )
        for section, _ in sections:
            section.pack_forget()

        visible_sections = [
            section for section, variable in sections if variable.get()
        ]
        for index, section in enumerate(visible_sections):
            section.pack(
                side="left",
                fill="both",
                expand=True,
                padx=(0, 6) if index < len(visible_sections) - 1 else 0,
            )

    def _add_spout_output(self, name=""):
        return self._add_output_row(
            self.spout_outputs, self.spout_rows_frame, name
        )

    def _add_virtual_camera_output(self, device_name=None):
        if device_name is None:
            used_devices = {
                output["variable"].get().casefold()
                for output in self.virtual_camera_outputs
                if output["variable"].get()
            }
            device_name = next(
                (
                    name
                    for name in self.available_virtual_camera_devices
                    if name.casefold() not in used_devices
                ),
                "",
            )
        return self._add_output_row(
            self.virtual_camera_outputs,
            self.virtual_camera_rows_frame,
            device_name,
            values=self.available_virtual_camera_devices,
        )

    def _add_output_row(self, outputs, parent, value, values=None):
        row = tk.Frame(parent)
        row.pack(fill="x", pady=1)
        variable = tk.StringVar(value=value)
        if values is None:
            entry = ttk.Entry(row, textvariable=variable)
            normal_state = "normal"
        else:
            entry = ttk.Combobox(
                row, textvariable=variable, values=values, state="readonly"
            )
            normal_state = "readonly"
        entry.pack(side="left", fill="x", expand=True)
        output = {
            "variable": variable,
            "entry": entry,
            "normal_state": normal_state,
            "frame": row,
        }
        remove_button = ttk.Button(
            row,
            text="Remove",
            command=lambda target=output: self._remove_output_row(outputs, target),
        )
        remove_button.pack(side="left", padx=(4, 0))
        output["remove_button"] = remove_button
        outputs.append(output)
        return output

    @staticmethod
    def _remove_output_row(outputs, output):
        outputs.remove(output)
        output["frame"].destroy()

    def refresh_virtual_camera_devices(self):
        try:
            devices = list_unity_capture_devices()
        except OSError as exc:
            self.camera_device_status_label.configure(
                text="Could not read installed UnityCapture devices."
            )
            messagebox.showerror("Camera device list error", str(exc))
            return

        self.available_virtual_camera_devices = devices
        available_devices = {device.casefold() for device in devices}
        used_devices = set()
        for output in self.virtual_camera_outputs:
            current = output["variable"].get()
            if current.casefold() not in available_devices or current.casefold() in used_devices:
                current = next(
                    (
                        device
                        for device in devices
                        if device.casefold() not in used_devices
                    ),
                    "",
                )
                output["variable"].set(current)
            output["entry"].configure(values=devices)
            if current:
                used_devices.add(current.casefold())

        if devices:
            self.camera_device_status_label.configure(
                text=f"{len(devices)} installed UnityCapture device(s) found."
            )
        else:
            self.camera_device_status_label.configure(
                text="No UnityCapture devices found. Install one, then refresh."
            )

    def _open_camera_install_instructions(self):
        url = "https://github.com/schellingb/UnityCapture#installation"
        try:
            if not webbrowser.open(url):
                messagebox.showwarning(
                    "Could not open browser",
                    f"Open the UnityCapture installation instructions manually:\n{url}",
                )
        except (webbrowser.Error, OSError) as exc:
            messagebox.showerror(
                "Could not open browser",
                f"{exc}\n\nOpen the UnityCapture installation instructions manually:\n{url}",
            )

    def _open_ndi_install_instructions(self):
        url = "https://ndi.video/for-developers/ndi-sdk/download/"
        try:
            if not webbrowser.open(url):
                messagebox.showwarning(
                    "Could not open browser",
                    f"Open the NDI SDK installation instructions manually:\n{url}",
                )
        except (webbrowser.Error, OSError) as exc:
            messagebox.showerror(
                "Could not open browser",
                f"{exc}\n\nOpen the NDI SDK installation instructions manually:\n{url}",
            )

    def _update_grid_scrollregion(self, _event=None):
        self.window_canvas.configure(scrollregion=self.window_canvas.bbox("all"))

    def _resize_grid(self, event):
        self.window_canvas.itemconfigure(self.window_grid_id, width=event.width)
        columns = max(2, min(4, event.width // 235))
        if columns != self.grid_columns:
            self.grid_columns = columns
            self._render_window_groups(self.windows_list)

    def _on_mousewheel(self, event):
        if not self.window_canvas.winfo_ismapped():
            return
        x = self.window_canvas.winfo_rootx()
        y = self.window_canvas.winfo_rooty()
        inside_canvas = (
            x <= event.x_root < x + self.window_canvas.winfo_width()
            and y <= event.y_root < y + self.window_canvas.winfo_height()
        )
        if inside_canvas:
            self.window_canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

    def _toggle_advanced(self):
        self.advanced_expanded = not self.advanced_expanded
        if self.advanced_expanded:
            self.advanced_options_frame.pack(fill="x", padx=4, pady=(0, 4))
        else:
            self.advanced_options_frame.pack_forget()
        self.advanced_button.configure(
            text="▼ Advanced" if self.advanced_expanded else "▶ Advanced"
        )

    def refresh_windows(self):
        all_wins = list_all_capturable_windows(
            include_hidden=self.show_hidden_var.get(),
            include_minimized=self.show_minimized_var.get(),
        )
        own_pid = os.getpid()
        filtered = []
        for window in all_wins:
            process_name = window["proc"].lower()
            if window["pid"] == own_pid:
                continue
            if process_name == "explorer.exe" and not self.show_explorer_var.get():
                continue
            if (
                process_name in BROWSER_PROCESS_NAMES
                and not self.show_browsers_var.get()
            ):
                continue
            is_settings_window = process_name in SETTINGS_PROCESS_NAMES or (
                process_name == "applicationframehost.exe"
                and "settings" in window["title"].lower()
            )
            if is_settings_window and not self.show_settings_var.get():
                continue
            if (
                process_name == "applicationframehost.exe"
                and not self.show_application_frame_host_var.get()
            ):
                continue
            filtered.append(window)
        previous_selection = self.selected_hwnd
        with self.preview_state_lock:
            self.windows_list = filtered

        selected_still_exists = any(w["hwnd"] == previous_selection for w in filtered)
        if not selected_still_exists:
            waaave_windows = [
                w for w in filtered
                if "waaave" in w["proc"].lower() or "waaave" in w["title"].lower()
            ]
            self.selected_hwnd = (
                waaave_windows[-1]["hwnd"] if waaave_windows else
                filtered[0]["hwnd"] if filtered else None
            )

        self._render_window_groups(filtered)
        count = len(filtered)
        self.status_var.set(
            f"Found {count} window(s). Preview refreshes every {self.preview_interval_s} seconds; "
            "previews pause while broadcasting."
        )

    def _render_window_groups(self, windows):
        for child in self.window_grid.winfo_children():
            child.destroy()
        self.preview_cards.clear()
        self.group_buttons.clear()

        by_process = {}
        for window in windows:
            process_name = window["proc"].lower()
            by_process.setdefault(process_name, []).append(window)

        if not windows:
            message = "No capturable windows found. Restore a target or enable hidden/minimized windows."
            tk.Label(self.window_grid, text=message, fg="#555555").grid(
                row=0, column=0, padx=12, pady=16, sticky="w"
            )
        else:
            row = 0
            for process_name, siblings in sorted(by_process.items()):
                proc = siblings[0]["proc"]
                process_count = len({window["pid"] for window in siblings})
                group_key = process_name
                group_frame = tk.Frame(self.window_grid, bd=1, relief="solid")
                group_frame.grid(
                    row=row, column=0, columnspan=self.grid_columns, sticky="ew",
                    padx=8, pady=3,
                )
                header = tk.Frame(group_frame)
                header.grid(
                    row=0, column=0, columnspan=self.grid_columns, sticky="ew",
                    padx=4, pady=(3, 2),
                )
                process_header = tk.Label(
                    header,
                    text=(
                        f"{proc}  -  {len(siblings)} related window(s)"
                        + (
                            f" across {process_count} processes"
                            if process_count > 1 else ""
                        )
                    ),
                    font=("Segoe UI", 9, "bold"),
                    anchor="w",
                )
                process_header.pack(side="left", fill="x", expand=True)
                collapsed = group_key in self.collapsed_groups
                group_button = ttk.Button(
                    header,
                    text="▶" if collapsed else "▼",
                    width=2,
                    command=lambda key=group_key: self._toggle_group(key),
                )
                group_button.pack(side="right")
                self.group_buttons[group_key] = group_button
                if not collapsed:
                    for index, window in enumerate(siblings):
                        column = index % self.grid_columns
                        card_row = 1 + index // self.grid_columns
                        self._create_window_card(
                            window, group_frame, card_row, column
                        )
                for column in range(self.grid_columns):
                    group_frame.grid_columnconfigure(
                        column, weight=1, uniform="window_cards"
                    )
                row += 1
            for column in range(self.grid_columns):
                self.window_grid.grid_columnconfigure(column, weight=1, uniform="window_cards")

        self.window_canvas.yview_moveto(0)

    def _toggle_group(self, group_key):
        with self.preview_state_lock:
            if group_key in self.collapsed_groups:
                self.collapsed_groups.remove(group_key)
            else:
                self.collapsed_groups.add(group_key)
            windows = list(self.windows_list)
        self._render_window_groups(windows)

    def _create_window_card(self, window, parent, row, column):
        hwnd = window["hwnd"]
        selected = hwnd == self.selected_hwnd
        card = tk.Frame(
            parent,
            bd=0,
            relief="solid",
            highlightbackground="#2e8b57" if selected else "#b8b8b8",
            highlightcolor="#2e8b57" if selected else "#b8b8b8",
            highlightthickness=2 if selected else 1,
            padx=3,
            pady=3,
        )
        card.grid(row=row, column=column, sticky="nsew", padx=3, pady=2)

        title = tk.Label(
            card,
            text=window["title"],
            font=("Segoe UI", 9, "bold"),
            wraplength=PREVIEW_WIDTH,
            justify="left",
            anchor="w",
        )
        title.pack(fill="x", pady=(0, 2))

        details = f"{window['width']}x{window['height']}  |  {window['class']}"
        tk.Label(card, text=details, fg="#555555", justify="left", anchor="w").pack(
            fill="x", pady=(0, 3)
        )

        preview_frame = tk.Frame(
            card, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT, bg="#20242a"
        )
        preview_frame.pack(fill="x")
        preview_frame.pack_propagate(False)
        preview = tk.Label(
            preview_frame,
            text="Preview paused while broadcasting"
            if self.is_streaming else
            "Hidden / minimized"
            if not window["visible"] or window["minimized"] else
            "Waiting for preview...",
            bg="#20242a",
            fg="#e8e8e8",
            wraplength=PREVIEW_WIDTH - 20,
            anchor="center",
        )
        preview.pack(fill="both", expand=True)
        cached_photo = self.preview_cache.get(hwnd)
        if cached_photo is not None:
            preview.configure(image=cached_photo, text="")
        preview.bind("<Button-1>", lambda _event, target=hwnd: self._select_window(target))

        self.preview_cards[hwnd] = {
            "card": card,
            "preview": preview,
            "photo": cached_photo,
        }

    def _select_window(self, hwnd):
        if self.is_streaming:
            return
        self.selected_hwnd = hwnd
        for target, card in self.preview_cards.items():
            selected = target == hwnd
            card["card"].configure(
                highlightbackground="#2e8b57" if selected else "#b8b8b8",
                highlightcolor="#2e8b57" if selected else "#b8b8b8",
                highlightthickness=2 if selected else 1,
            )

    def _preview_worker(self):
        while not self.preview_stop_event.wait(self.preview_interval_s):
            with self.preview_state_lock:
                windows = list(self.windows_list)
                collapsed_groups = set(self.collapsed_groups)
            for window in windows:
                if self.preview_stop_event.is_set():
                    break
                if window["proc"].lower() in collapsed_groups:
                    continue
                if not window["visible"] or window["minimized"]:
                    continue
                with self.preview_state_lock:
                    if self.preview_pause_event.is_set():
                        break
                    self.preview_idle_event.clear()
                capturer = None
                try:
                    size = get_window_client_size(window["hwnd"])
                    if size:
                        capturer = Win32WindowCapturer(window["hwnd"], size[0], size[1])
                        rgb = capturer.grab_rgb()
                        if rgb is not None:
                            height, width = rgb.shape[:2]
                            scale = min(
                                1.0, PREVIEW_WIDTH / width, PREVIEW_HEIGHT / height
                            )
                            out_width = max(1, int(width * scale))
                            out_height = max(1, int(height * scale))
                            x_idx = np.linspace(0, width - 1, out_width).astype(np.intp)
                            y_idx = np.linspace(0, height - 1, out_height).astype(np.intp)
                            thumbnail = np.ascontiguousarray(rgb[y_idx[:, None], x_idx[None, :], :])
                            ppm = (
                                f"P6 {out_width} {out_height} 255\n".encode("ascii")
                                + thumbnail.tobytes()
                            )
                            self.preview_queue.put((window["hwnd"], ppm, None))
                except Exception as exc:
                    self.preview_queue.put((window["hwnd"], None, str(exc)))
                finally:
                    if capturer is not None:
                        capturer.close()
                    with self.preview_state_lock:
                        self.preview_idle_event.set()

    def _drain_preview_queue(self):
        latest = {}
        while True:
            try:
                hwnd, ppm, error = self.preview_queue.get_nowait()
            except queue.Empty:
                break
            latest[hwnd] = (ppm, error)

        for hwnd, (ppm, error) in latest.items():
            card = self.preview_cards.get(hwnd)
            if ppm is not None:
                try:
                    photo = tk.PhotoImage(data=ppm, format="PPM")
                    self.preview_cache[hwnd] = photo
                    if card is not None:
                        card["preview"].configure(image=photo, text="")
                        card["photo"] = photo
                except tk.TclError as exc:
                    if card is not None and hwnd not in self.preview_cache:
                        card["preview"].configure(
                            image="", text=f"Preview display error: {exc}"
                        )
                        card["photo"] = None
            elif error and card is not None and hwnd not in self.preview_cache:
                card["preview"].configure(image="", text=f"Preview error: {error}")

        if not self.preview_stop_event.is_set():
            self.root.after(100, self._drain_preview_queue)

    def toggle_stream(self):
        if not self.is_streaming:
            self.start_stream()
        else:
            self.stop_stream()

    def _resolve_target_hwnd(self):
        manual_txt = self.manual_hwnd_var.get().strip()
        if manual_txt:
            try:
                hwnd = int(manual_txt, 16) if manual_txt.lower().startswith("0x") else int(manual_txt)
                if user32.IsWindow(hwnd):
                    title = get_window_title_safe(hwnd) or f"[{get_window_class_name(hwnd)}]"
                    return hwnd, f"[{get_process_name(hwnd)}] {title}"
                messagebox.showerror("Invalid HWND", f"Window handle {manual_txt} does not exist.")
                return None, None
            except ValueError:
                messagebox.showerror("Invalid HWND", "Enter a valid hex (0x1234) or integer HWND.")
                return None, None

        selected = next(
            (window for window in self.windows_list if window["hwnd"] == self.selected_hwnd),
            None,
        )
        if selected is None:
            messagebox.showwarning("No Window", "Please select a window first.")
            return None, None

        return selected["hwnd"], f"[{selected['proc']}] {selected['title']}"

    def start_stream(self):
        hwnd, display_name = self._resolve_target_hwnd()
        if not hwnd:
            return

        send_spout = self.send_spout_var.get()
        send_virtual_camera = self.send_virtual_camera_var.get()
        send_ndi = self.send_ndi_var.get()
        if not send_spout and not send_virtual_camera and not send_ndi:
            messagebox.showwarning(
                "No Output Selected",
                "Select Spout, virtual camera, or NDI output to start.",
            )
            return
        if send_ndi and ndi is None:
            messagebox.showerror(
                "NDI Unavailable",
                "NDI could not be loaded. Install the Video Matrix Python "
                "requirements and the NDI SDK, then restart Video Matrix.\n\n"
                f"Details: {NDI_IMPORT_ERROR}",
            )
            return
        ndi_name = self.ndi_name_var.get().strip() if send_ndi else ""
        if send_ndi and not ndi_name:
            messagebox.showwarning(
                "Missing NDI Name", "Enter a name for the NDI source."
            )
            return
        if send_spout and not self.spout_outputs:
            messagebox.showwarning(
                "No Spout Sender", "Add a Spout sender or turn off Spout output."
            )
            return
        if send_virtual_camera and not self.virtual_camera_outputs:
            messagebox.showwarning(
                "No Virtual Camera",
                "Add a virtual camera or turn off virtual camera output.",
            )
            return

        spout_names = (
            [output["variable"].get().strip() for output in self.spout_outputs]
            if send_spout
            else []
        )
        camera_device_names = (
            [
                output["variable"].get().strip()
                for output in self.virtual_camera_outputs
            ]
            if send_virtual_camera
            else []
        )
        if not spout_names and not camera_device_names and not ndi_name:
            messagebox.showwarning("Missing Output", "Add at least one output.")
            return
        if any(not name for name in spout_names):
            messagebox.showwarning(
                "Missing Spout Name", "Enter a name for every Spout sender."
            )
            return
        if len({name.casefold() for name in spout_names}) != len(spout_names):
            messagebox.showwarning(
                "Duplicate Spout Name", "Spout sender names must be unique."
            )
            return

        if any(not name for name in camera_device_names):
            messagebox.showwarning(
                "Missing Camera Device",
                "Select an installed UnityCapture device for every virtual camera. "
                "Refresh the device list after installing one.",
            )
            return
        if len({name.casefold() for name in camera_device_names}) != len(
            camera_device_names
        ):
            messagebox.showwarning(
                "Duplicate Camera Device",
                "Each virtual camera must use a different installed device.",
            )
            return

        size = get_window_client_size(hwnd)
        if not size:
            messagebox.showerror(
                "Invalid Window",
                "Selected window is minimized, closed, or has 0 size. Restore it and click Refresh.",
            )
            return

        with self.preview_state_lock:
            self.preview_pause_event.set()
        self.preview_idle_event.wait()

        self.target_hwnd = hwnd
        self.stream_width, self.stream_height = size
        fps = STREAM_FPS

        try:
            active_outputs = []
            self.capturer = Win32WindowCapturer(hwnd, self.stream_width, self.stream_height)

            for sender_name in spout_names:
                sender = SpoutGL.SpoutSender()
                self.spout_senders.append({"name": sender_name, "sender": sender})
                if hasattr(sender, "createOpenGL") and not sender.createOpenGL():
                    raise RuntimeError(
                        f"Could not initialize Spout sender '{sender_name}'."
                    )
                sender.setSenderName(sender_name)
                active_outputs.append(f"Spout '{sender_name}'")

            for device_name in camera_device_names:
                camera = pyvirtualcam.Camera(
                    width=self.stream_width,
                    height=self.stream_height,
                    fps=fps,
                    fmt=pyvirtualcam.PixelFormat.RGB,
                    backend="unitycapture",
                    device=device_name,
                )
                self.virtual_cameras.append(camera)
                active_outputs.append(f"Virtual camera '{camera.device}'")

            if send_ndi:
                if not ndi.initialize():
                    raise RuntimeError("NDI runtime could not be initialized.")
                self.ndi_initialized = True
                send_settings = ndi.SendCreate()
                send_settings.ndi_name = ndi_name
                send_settings.clock_video = False
                self.ndi_sender = ndi.send_create(send_settings)
                if self.ndi_sender is None:
                    raise RuntimeError("Could not create the NDI sender.")

                self.ndi_buffer = np.empty(
                    (self.stream_height, self.stream_width, 4), dtype=np.uint8
                )
                self.ndi_video_frame = ndi.VideoFrameV2()
                self.ndi_video_frame.data = self.ndi_buffer
                self.ndi_video_frame.FourCC = ndi.FOURCC_VIDEO_TYPE_RGBX
                self.ndi_video_frame.frame_rate_N = STREAM_FPS
                self.ndi_video_frame.frame_rate_D = 1
                active_outputs.append(f"NDI '{ndi_name}'")

            self.is_streaming = True
            self._set_controls_state("disabled")
            self.toggle_btn.config(text="Stop Broadcast")
            self.status_var.set(
                f"Live {display_name} ({self.stream_width}x{self.stream_height}) -> {' + '.join(active_outputs)}"
            )

            self.root.after(1, self._capture_and_send)

        except Exception as e:
            cleanup_errors = self._cleanup_resources()
            with self.preview_state_lock:
                self.preview_pause_event.clear()
            error_message = f"Startup Error: {e}"
            if cleanup_errors:
                error_message += f" | Cleanup errors: {'; '.join(cleanup_errors)}"
            self.status_var.set(error_message)

    def stop_stream(self):
        self.is_streaming = False
        cleanup_errors = self._cleanup_resources()
        with self.preview_state_lock:
            self.preview_pause_event.clear()
        self._set_controls_state("normal")
        self.toggle_btn.config(text="Start Window Broadcast")
        status = "Status: Stopped."
        if cleanup_errors:
            status += f" Cleanup errors: {'; '.join(cleanup_errors)}"
        self.status_var.set(status)
        return cleanup_errors

    def _set_controls_state(self, state):
        self.advanced_button.config(state=state)
        for check in (
            self.show_hidden_check,
            self.show_minimized_check,
            self.show_explorer_check,
            self.show_browsers_check,
            self.show_settings_check,
            self.show_application_frame_host_check,
            self.show_spout_output_check,
            self.show_virtual_camera_output_check,
            self.show_ndi_output_check,
        ):
            check.config(state=state)
        self.send_spout_check.config(state=state)
        self.send_virtual_camera_check.config(state=state)
        self.send_ndi_check.config(state=state)
        self.refresh_btn.config(state=state)
        self.refresh_camera_devices_btn.config(state=state)
        if state == "disabled":
            self._set_output_group_enabled(
                self.spout_outputs, self.add_spout_output_btn, False
            )
            self._set_output_group_enabled(
                self.virtual_camera_outputs, self.add_virtual_camera_btn, False
            )
            self.ndi_name_entry.config(state="disabled")
        else:
            self._update_output_controls_state()
        self.manual_hwnd_entry.config(state=state)

    def _set_output_group_enabled(self, outputs, add_button, enabled):
        state = "normal" if enabled else "disabled"
        add_button.config(state=state)
        for output in outputs:
            output["entry"].config(
                state=output["normal_state"] if enabled else "disabled"
            )
            output["remove_button"].config(state=state)

    def _update_output_controls_state(self):
        self._set_output_group_enabled(
            self.spout_outputs,
            self.add_spout_output_btn,
            self.send_spout_var.get(),
        )
        self._set_output_group_enabled(
            self.virtual_camera_outputs,
            self.add_virtual_camera_btn,
            self.send_virtual_camera_var.get(),
        )
        self.ndi_name_entry.config(
            state="normal" if self.send_ndi_var.get() else "disabled"
        )

    def _cleanup_resources(self):
        errors = []
        if self.capturer is not None:
            try:
                self.capturer.close()
            except Exception as exc:
                errors.append(f"window capture: {exc}")
            self.capturer = None

        for camera in self.virtual_cameras:
            try:
                camera.close()
            except Exception as exc:
                errors.append(f"virtual camera '{camera.device}': {exc}")
        self.virtual_cameras.clear()

        for output in self.spout_senders:
            sender = output["sender"]
            try:
                sender.releaseSender()
            except Exception as exc:
                errors.append(f"Spout sender '{output['name']}' release: {exc}")
            if hasattr(sender, "closeOpenGL"):
                try:
                    if not sender.closeOpenGL():
                        errors.append(
                            f"Spout sender '{output['name']}' OpenGL close failed."
                        )
                except Exception as exc:
                    errors.append(
                        f"Spout sender '{output['name']}' OpenGL close: {exc}"
                    )
        self.spout_senders.clear()

        if self.ndi_sender is not None:
            try:
                ndi.send_destroy(self.ndi_sender)
            except Exception as exc:
                errors.append(f"NDI sender release: {exc}")
            self.ndi_sender = None
        self.ndi_video_frame = None
        self.ndi_buffer = None
        if self.ndi_initialized:
            try:
                ndi.destroy()
            except Exception as exc:
                errors.append(f"NDI runtime shutdown: {exc}")
            self.ndi_initialized = False
        return errors

    def _capture_and_send(self):
        if not self.is_streaming or self.capturer is None:
            return

        t0 = time.perf_counter()

        try:
            rgb = self.capturer.grab_rgb()
            if rgb is None:
                cleanup_errors = self.stop_stream()
                status = "Stopped: Target window was minimized or closed."
                if cleanup_errors:
                    status += f" Cleanup errors: {'; '.join(cleanup_errors)}"
                self.status_var.set(status)
                return

            # Virtual camera expects top-down RGB
            for camera in self.virtual_cameras:
                camera.send(rgb)

            for output in self.spout_senders:
                sender = output["sender"]
                if not sender.sendImage(
                    rgb,
                    self.stream_width,
                    self.stream_height,
                    GL_RGB,
                    False,
                    0,
                ):
                    raise RuntimeError(
                        f"Spout sender '{output['name']}' failed to send a frame."
                    )

            if self.ndi_sender is not None:
                self.ndi_buffer[:, :, :3] = rgb
                self.ndi_buffer[:, :, 3] = 255
                ndi.send_send_video_v2(self.ndi_sender, self.ndi_video_frame)

        except Exception as e:
            cleanup_errors = self.stop_stream()
            error_message = f"Capture Error: {e}"
            if cleanup_errors:
                error_message += f" | Cleanup errors: {'; '.join(cleanup_errors)}"
            self.status_var.set(error_message)
            return

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        next_delay = max(1, self.frame_interval_ms - elapsed_ms)
        self.root.after(next_delay, self._capture_and_send)

    def _on_close(self):
        self.is_streaming = False
        self.preview_stop_event.set()
        with self.preview_state_lock:
            self.preview_pause_event.set()
        cleanup_errors = self._cleanup_resources()
        if cleanup_errors:
            messagebox.showerror("Cleanup errors", "\n".join(cleanup_errors))
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = WindowBroadcasterApp(root)
    root.mainloop()
