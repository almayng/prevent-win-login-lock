import ctypes
import sys

# pyautogui calls SetProcessDPIAware() on import and locks the process to the
# primary DPI. On a mixed-DPI setup that shrinks the second monitor overlay.
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

import pyautogui
import threading
import time
import tkinter as tk
import argparse
import base64
import getpass
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import psutil
from pynput.mouse import Listener as MouseListener
from pynput.keyboard import Listener as KeyboardListener
from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageDraw

# Windows: reset idle timers so the session does not lock or sleep.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
MONITORINFOF_PRIMARY = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _INPUT(ctypes.Structure):
    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT),)

    _anonymous_ = ("_input",)
    _fields_ = (("type", ctypes.c_ulong), ("_input", _INPUTUNION))

CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CONFIG = {
    "idle_time_threshold": 20,
    "display_protection_threshold": 60,
    "media_detection_enabled": True,
    "password_protection_enabled": False,
    "password_prompt_timeout": 15,
    "password_salt": "",
    "password_hash": "",
}

def load_config():
    """Load settings and create a disabled password configuration if needed."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return DEFAULT_CONFIG.copy()

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read configuration file {CONFIG_PATH}: {error}") from error

    merged = {**DEFAULT_CONFIG, **config}
    idle = float(merged["idle_time_threshold"])
    display = float(merged["display_protection_threshold"])
    if display < idle:
        # Keep-alive must start before the black overlay; otherwise Windows can
        # lock while the protector is already covering the screen.
        print(
            "Warning: display_protection_threshold "
            f"({display}) < idle_time_threshold ({idle}). "
            f"Raising display_protection_threshold to {idle}."
        )
        merged["display_protection_threshold"] = idle
    return merged

def save_config(config):
    """Save configuration with restrictive permissions where supported."""
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

def password_digest(password, salt):
    """Derive a password verifier; the original password is never stored."""
    return base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    ).decode("ascii")

def set_password(config):
    """Ask for and store a new password verifier in the configuration."""
    password = getpass.getpass("New protection password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if not password:
        raise ValueError("Password must not be empty.")
    if password != confirmation:
        raise ValueError("Passwords do not match.")

    salt = secrets.token_bytes(16)
    config["password_salt"] = base64.b64encode(salt).decode("ascii")
    config["password_hash"] = password_digest(password, salt)
    config["password_protection_enabled"] = True
    save_config(config)

def parse_arguments():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--set-password",
        action="store_true",
        help="Set or change the screen protector password.",
    )
    actions.add_argument(
        "--disable-password",
        action="store_true",
        help="Disable password protection in config.json.",
    )
    return parser.parse_args()

# Global control variables
stop_event = threading.Event()
user_activity_event = threading.Event()
unlock_attempt_event = threading.Event()
password_protection_active = threading.Event()
last_user_activity_time = time.monotonic()
last_keepalive_time = last_user_activity_time
synthetic_input_until = 0.0
config = DEFAULT_CONFIG.copy()
idle_time_threshold = config["idle_time_threshold"]
display_protection_threshold = config["display_protection_threshold"]
password_prompt_timeout = config["password_prompt_timeout"]
media_detection_enabled = config["media_detection_enabled"]
_media_check_time = 0.0
_media_playing = False

def is_media_playing():
    """Return whether Windows has an active, unmuted audio session."""
    global _media_check_time, _media_playing

    if not media_detection_enabled or not psutil.WINDOWS:
        return False

    current_time = time.monotonic()
    if current_time - _media_check_time < 2.0:
        return _media_playing

    _media_check_time = current_time
    try:
        from pycaw.pycaw import AudioUtilities

        # AudioSessionStateActive has the numeric value 1. Avoid importing the
        # enum because its location differs between pycaw versions.
        _media_playing = any(
            session.State == 1 and not session.SimpleAudioVolume.GetMute()
            for session in AudioUtilities.GetAllSessions()
        )
    except (ImportError, OSError):
        _media_playing = False
    except Exception as error:
        # Audio sessions may disappear while they are being enumerated.
        print(f"Unable to check media playback: {error}")
        _media_playing = False

    return _media_playing

def is_screen_locked():
    """Check if the screen is locked (Windows/Linux)."""
    try:
        if psutil.WINDOWS:
            import win32gui, win32process
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name().lower() == "logonui.exe"
        elif psutil.LINUX:
            with open("/proc/uptime", "r") as f:
                idle_time = float(f.read().split()[0])
            return idle_time > 300
    except Exception:
        return False

def prevent_system_idle():
    """Tell Windows the session is still in use (display + system)."""
    if not psutil.WINDOWS:
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception as ex:
        print(f"Unable to set execution state: {ex}")


def jiggle_mouse():
    """Reset the Windows last-input timer so messengers stay Available.

    ``pyautogui.moveTo`` uses ``SetCursorPos``, which moves the cursor but does
    not count as input. ``SendInput`` does, and that is what GetLastInputInfo,
    the screensaver, and presence status look at.
    """
    if not psutil.WINDOWS:
        x, y = pyautogui.position()
        pyautogui.moveTo(x + 1, y)
        pyautogui.moveTo(x, y)
        return

    events = (_INPUT * 2)()
    events[0].type = INPUT_MOUSE
    events[0].mi = _MOUSEINPUT(1, 0, 0, MOUSEEVENTF_MOVE, 0, None)
    events[1].type = INPUT_MOUSE
    events[1].mi = _MOUSEINPUT(-1, 0, 0, MOUSEEVENTF_MOVE, 0, None)
    sent = ctypes.windll.user32.SendInput(2, ctypes.byref(events), ctypes.sizeof(_INPUT))
    if sent != 2:
        raise OSError(f"SendInput sent {sent} of 2 mouse events")


def move_mouse_at_intervals():
    """Keep the session active so Windows does not lock during idle."""
    global last_keepalive_time, synthetic_input_until

    while not stop_event.is_set():
        current_time = time.monotonic()

        if (
            current_time - last_user_activity_time >= idle_time_threshold
            and current_time - last_keepalive_time >= idle_time_threshold
            and not is_screen_locked()
        ):
            # Ignore the synthetic move in activity listeners so the black
            # overlay is not dismissed and the idle clock is not reset wrongly.
            synthetic_input_until = current_time + 1.0
            try:
                prevent_system_idle()
                jiggle_mouse()
            except Exception as ex:
                print(f"Unable to simulate input: {ex}")
            last_keepalive_time = current_time

        stop_event.wait(0.5)  # Check every 0.5 seconds

def on_mouse_activity(x, y):
    """Record real mouse activity and request restoring the desktop."""
    record_user_activity()

def on_keyboard_activity(key):
    """Record real keyboard activity and request restoring the desktop."""
    record_user_activity()

def record_user_activity():
    """Ignore events generated by the keep-alive keypress itself."""
    global last_user_activity_time, last_keepalive_time

    if time.monotonic() < synthetic_input_until:
        return

    if password_protection_active.is_set():
        unlock_attempt_event.set()
        return

    last_user_activity_time = time.monotonic()
    last_keepalive_time = last_user_activity_time
    user_activity_event.set()

def stop_program(icon, item):
    """Stop the program."""
    stop_event.set()
    icon.stop()

def create_image():
    """Create a tray icon image."""
    image = Image.new("RGB", (64, 64), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, 48, 48), fill=(0, 0, 255))
    return image

def setup_tray():
    """Set up the system tray icon."""
    icon_image = create_image()
    menu = Menu(MenuItem("Exit", stop_program))
    icon = Icon("Auto Mouse Mover", icon_image, "Mouse Mover", menu)
    threading.Thread(target=icon.run, daemon=True).start()

def enable_per_monitor_dpi():
    """Use physical pixels so mixed-DPI monitors get exact overlay placement."""
    if not psutil.WINDOWS:
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception as error:
            print(f"Unable to set DPI awareness: {error}")


def list_windows_monitors():
    """Return monitor rectangles in physical pixels from the Win32 API."""
    monitors = []

    def callback(hmon, _hdc, _lprect, _lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                {
                    "x": int(rect.left),
                    "y": int(rect.top),
                    "width": int(rect.right - rect.left),
                    "height": int(rect.bottom - rect.top),
                    "is_primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                }
            )
        return 1

    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_RECT),
        ctypes.c_void_p,
    )
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, enum_proc(callback), 0)
    return [monitor for monitor in monitors if monitor["width"] and monitor["height"]]


def get_monitor_geometries(root):
    """Return one geometry dict per monitor covering the whole desktop.

    On Windows a single Tk window sized to ``winfo_vrootwidth`` only spans the
    primary display, so a secondary monitor stayed partly uncovered. Querying
    every monitor lets us place a dedicated black overlay on each one. Falls
    back to Tk's virtual-root bounds when monitor enumeration is unavailable.
    """
    monitors = []
    if psutil.WINDOWS:
        try:
            monitors = list_windows_monitors()
        except Exception as error:
            print(f"Unable to enumerate monitors via Win32 ({error}).")

    if not monitors:
        try:
            from screeninfo import get_monitors

            for monitor in get_monitors():
                if monitor.width and monitor.height:
                    monitors.append(
                        {
                            "x": int(monitor.x),
                            "y": int(monitor.y),
                            "width": int(monitor.width),
                            "height": int(monitor.height),
                            "is_primary": bool(monitor.is_primary),
                        }
                    )
        except Exception as error:
            # Any failure (missing package, headless server, driver quirk) must not
            # crash the protector; fall back to the single virtual-desktop overlay.
            print(f"Unable to enumerate monitors ({error}); using virtual desktop bounds.")

    if not monitors:
        monitors.append(
            {
                "x": root.winfo_vrootx(),
                "y": root.winfo_vrooty(),
                "width": root.winfo_vrootwidth(),
                "height": root.winfo_vrootheight(),
                "is_primary": True,
            }
        )
    return monitors


def overlay_placement(monitor):
    """Return x, y, width, height for the protector on this monitor.

    Only the primary display is inset by 1px so Windows 11 does not treat the
    overlay as a fullscreen app (Do Not Disturb / Zzz). Other monitors keep
    their exact bounds.
    """
    x = int(monitor["x"])
    y = int(monitor["y"])
    width = int(monitor["width"])
    height = int(monitor["height"])
    if monitor.get("is_primary", True):
        width = max(1, width - 1)
        height = max(1, height - 1)
    return x, y, width, height


def overlay_geometry(monitor):
    x, y, width, height = overlay_placement(monitor)
    return f"{width}x{height}{x:+d}{y:+d}"


def overlay_hwnd(overlay):
    hwnd = int(overlay.winfo_id())
    return ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def suppress_overlay_activation(overlay):
    """Keep the overlay off the foreground/Alt-Tab fullscreen heuristics."""
    if not psutil.WINDOWS:
        return
    try:
        overlay.update_idletasks()
        hwnd = overlay_hwnd(overlay)
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )
    except Exception as error:
        print(f"Unable to adjust overlay window style: {error}")


def place_overlay(overlay, monitor):
    """Place the overlay on the monitor using physical Win32 coordinates.

    Tk ``geometry`` cannot reliably position overrideredirect windows on a
    mixed-DPI secondary display, which left a gap at the top and right.
    """
    x, y, width, height = overlay_placement(monitor)
    overlay.geometry(f"{width}x{height}{x:+d}{y:+d}")
    if not psutil.WINDOWS:
        return
    try:
        overlay.update_idletasks()
        ctypes.windll.user32.SetWindowPos(
            overlay_hwnd(overlay),
            ctypes.c_void_p(HWND_TOPMOST),
            x,
            y,
            width,
            height,
            SWP_NOACTIVATE,
        )
    except Exception as error:
        print(f"Unable to place overlay: {error}")


class ScreenProtector:
    """Covers every monitor with black while the computer is unattended."""

    def __init__(self):
        enable_per_monitor_dpi()
        self.root = tk.Tk()
        self.root.withdraw()
        self.monitors = get_monitor_geometries(self.root)

        # One full-screen black overlay per monitor. A single window cannot
        # reliably span every display on Windows, which left secondary monitors
        # partly visible, so each monitor gets its own dedicated overlay.
        self.overlays = []
        for monitor in self.monitors:
            overlay = tk.Toplevel(self.root, bg="black")
            overlay.withdraw()
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.configure(cursor="none")
            overlay.monitor = monitor
            suppress_overlay_activation(overlay)
            self.overlays.append(overlay)

        self.visible = False
        self.password_form_visible = False
        self.password_form_hide_at = 0.0
        self.password_required = self.password_is_configured()
        self.password_var = tk.StringVar()
        self.message_var = tk.StringVar(value="Enter password to unlock")
        self.form_window = None

        if self.password_required:
            self.create_password_form()
            self.bind_unlock_attempt_handlers()

        print(f"Screen protector covering {len(self.overlays)} monitor(s):")
        for monitor in self.monitors:
            print(
                f"  {monitor['width']}x{monitor['height']} at "
                f"({monitor['x']}, {monitor['y']})"
                + (" [primary]" if monitor["is_primary"] else "")
            )

        self.root.after(200, self.update)

    def password_is_configured(self):
        """Enable the prompt only when the complete verifier is present."""
        if not config["password_protection_enabled"]:
            return False

        try:
            base64.b64decode(config["password_salt"], validate=True)
            return bool(config["password_hash"])
        except (TypeError, ValueError):
            return False

    def create_password_form(self):
        # The form lives in its own top-level window so it can be centered on
        # whichever monitor the user is interacting with, instead of being
        # pinned to one overlay.
        self.form_window = tk.Toplevel(self.root, bg="black")
        self.form_window.withdraw()
        self.form_window.overrideredirect(True)
        self.form_window.attributes("-topmost", True)
        self.password_panel = tk.Frame(self.form_window, bg="black")
        self.password_panel.pack(padx=48, pady=32)
        tk.Label(
            self.password_panel,
            textvariable=self.message_var,
            bg="black",
            fg="white",
            font=("Segoe UI", 14),
        ).pack(pady=(0, 12))
        self.password_entry = tk.Entry(
            self.password_panel,
            textvariable=self.password_var,
            show="•",
            width=28,
            justify="center",
            font=("Segoe UI", 14),
        )
        self.password_entry.pack()
        self.password_entry.bind("<Return>", self.try_unlock)
        self.password_entry.bind("<KeyRelease>", self.on_password_form_activity)

    def bind_unlock_attempt_handlers(self):
        """Show the password form only after an explicit unlock attempt."""
        for overlay in self.overlays:
            for sequence in ("<KeyPress>", "<Button>", "<Motion>"):
                overlay.bind(sequence, self.on_unlock_attempt, add="+")

    def on_unlock_attempt(self, event=None):
        if self.visible and self.password_required and not self.password_form_visible:
            self.show_password_form()
        return "break"

    def extend_password_form_timeout(self):
        self.password_form_hide_at = time.monotonic() + password_prompt_timeout

    def on_password_form_activity(self, event=None):
        if self.password_form_visible:
            self.extend_password_form_timeout()

    def monitor_under_pointer(self):
        """Return the monitor holding the cursor, else the primary monitor."""
        try:
            pointer_x = self.root.winfo_pointerx()
            pointer_y = self.root.winfo_pointery()
        except tk.TclError:
            pointer_x = pointer_y = None

        if pointer_x is not None:
            for monitor in self.monitors:
                if (
                    monitor["x"] <= pointer_x < monitor["x"] + monitor["width"]
                    and monitor["y"] <= pointer_y < monitor["y"] + monitor["height"]
                ):
                    return monitor

        for monitor in self.monitors:
            if monitor["is_primary"]:
                return monitor
        return self.monitors[0]

    def set_overlay_cursor(self, cursor):
        for overlay in self.overlays:
            overlay.configure(cursor=cursor)

    def show_password_form(self):
        if self.password_form_visible:
            self.extend_password_form_timeout()
            return

        monitor = self.monitor_under_pointer()
        self.form_window.update_idletasks()
        form_width = self.form_window.winfo_reqwidth()
        form_height = self.form_window.winfo_reqheight()
        form_x = monitor["x"] + (monitor["width"] - form_width) // 2
        form_y = monitor["y"] + (monitor["height"] - form_height) // 2
        self.form_window.geometry(f"{form_width}x{form_height}{form_x:+d}{form_y:+d}")

        self.password_form_visible = True
        self.password_var.set("")
        self.message_var.set("Enter password to unlock")
        self.set_overlay_cursor("")
        self.form_window.deiconify()
        self.form_window.lift()
        self.extend_password_form_timeout()
        self.form_window.after(50, self.password_entry.focus_force)

    def hide_password_form(self):
        if not self.password_form_visible:
            return

        self.form_window.withdraw()
        self.password_form_visible = False
        self.password_var.set("")
        self.message_var.set("Enter password to unlock")
        self.set_overlay_cursor("none")

    def show(self):
        for overlay in self.overlays:
            overlay.deiconify()
            overlay.attributes("-topmost", True)
            place_overlay(overlay, overlay.monitor)
        if self.password_required:
            password_protection_active.set()
            self.hide_password_form()
        self.visible = True

    def hide(self):
        if self.password_required:
            self.hide_password_form()
        for overlay in self.overlays:
            overlay.withdraw()
        password_protection_active.clear()
        self.visible = False

    def try_unlock(self, event=None):
        """Hide the overlay only when the entered password is valid."""
        try:
            salt = base64.b64decode(config["password_salt"], validate=True)
            actual_digest = password_digest(self.password_var.get(), salt)
        except (TypeError, ValueError):
            self.message_var.set("Password configuration error")
            return "break"

        if hmac.compare_digest(actual_digest, config["password_hash"]):
            global last_user_activity_time, last_keepalive_time
            last_user_activity_time = time.monotonic()
            last_keepalive_time = last_user_activity_time
            self.hide()
        else:
            self.password_var.set("")
            self.message_var.set("Incorrect password. Try again")
            self.extend_password_form_timeout()
            self.password_entry.focus_force()
        return "break"

    def update(self):
        if stop_event.is_set():
            self.root.quit()
            return

        if unlock_attempt_event.is_set():
            unlock_attempt_event.clear()
            if self.visible and self.password_required:
                if self.password_form_visible:
                    self.extend_password_form_timeout()
                else:
                    self.show_password_form()

        if (
            self.password_form_visible
            and time.monotonic() >= self.password_form_hide_at
        ):
            self.hide_password_form()

        if user_activity_event.is_set():
            user_activity_event.clear()
            if self.visible and not self.password_required:
                self.hide()
        elif (
            not self.visible
            and time.monotonic() - last_user_activity_time >= display_protection_threshold
            and not is_media_playing()
        ):
            self.show()

        self.root.after(200, self.update)

if __name__ == "__main__":
    arguments = parse_arguments()
    config = load_config()

    if arguments.set_password:
        try:
            set_password(config)
            print(f"Password protection enabled in {CONFIG_PATH}.")
        except ValueError as error:
            raise SystemExit(f"Password was not changed: {error}") from error
        raise SystemExit()

    if arguments.disable_password:
        config["password_protection_enabled"] = False
        save_config(config)
        print(f"Password protection disabled in {CONFIG_PATH}.")
        raise SystemExit()

    enable_per_monitor_dpi()
    idle_time_threshold = float(config["idle_time_threshold"])
    display_protection_threshold = float(config["display_protection_threshold"])
    password_prompt_timeout = float(config["password_prompt_timeout"])
    media_detection_enabled = bool(config["media_detection_enabled"])
    setup_tray()

    # Start mouse listener
    mouse_listener = MouseListener(on_move=on_mouse_activity)

    mouse_listener.start()

    # Start keyboard listener
    keyboard_listener = KeyboardListener(on_press=on_keyboard_activity)
    keyboard_listener.start()

    # Start mouse mover thread
    mover_thread = threading.Thread(target=move_mouse_at_intervals, daemon=True)
    mover_thread.start()

    try:
        ScreenProtector().root.mainloop()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        mouse_listener.stop()
        keyboard_listener.stop()