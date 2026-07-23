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

    return {**DEFAULT_CONFIG, **config}

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

def move_mouse_at_intervals():
    """Keep the session active without moving the user's cursor."""
    global last_keepalive_time, synthetic_input_until

    while not stop_event.is_set():
        current_time = time.monotonic()

        if (
            current_time - last_user_activity_time >= idle_time_threshold
            and current_time - last_keepalive_time >= idle_time_threshold
            and not is_screen_locked()
        ):
            # Do not alter cursor position: it can wake the display and disrupt work.
            synthetic_input_until = current_time + 1.0
            try:
                pyautogui.press('shift')  # Simulate a harmless keypress
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

class ScreenProtector:
    """Covers all monitors with black while the computer is unattended."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = tk.Toplevel(self.root, bg="black")
        self.overlay.withdraw()
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.configure(cursor="none")
        self.visible = False
        self.password_form_visible = False
        self.password_form_hide_at = 0.0
        self.password_required = self.password_is_configured()
        self.password_var = tk.StringVar()
        self.message_var = tk.StringVar(value="Enter password to unlock")

        if self.password_required:
            self.create_password_form()
            self.bind_unlock_attempt_handlers()

        # Virtual desktop bounds cover every monitor, including monitors left
        # or above the primary display.
        self.x = self.root.winfo_vrootx()
        self.y = self.root.winfo_vrooty()
        self.width = self.root.winfo_vrootwidth()
        self.height = self.root.winfo_vrootheight()
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
        self.password_panel = tk.Frame(self.overlay, bg="black")
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
        for sequence in ("<KeyPress>", "<Button>", "<Motion>"):
            self.overlay.bind(sequence, self.on_unlock_attempt, add="+")

    def on_unlock_attempt(self, event=None):
        if self.visible and self.password_required and not self.password_form_visible:
            self.show_password_form()
        return "break"

    def extend_password_form_timeout(self):
        self.password_form_hide_at = time.monotonic() + password_prompt_timeout

    def on_password_form_activity(self, event=None):
        if self.password_form_visible:
            self.extend_password_form_timeout()

    def show_password_form(self):
        if self.password_form_visible:
            self.extend_password_form_timeout()
            return

        self.password_panel.place(relx=0.5, rely=0.5, anchor="center")
        self.password_form_visible = True
        self.password_var.set("")
        self.message_var.set("Enter password to unlock")
        self.overlay.configure(cursor="")
        self.extend_password_form_timeout()
        self.overlay.after(50, self.password_entry.focus_force)

    def hide_password_form(self):
        if not self.password_form_visible:
            return

        self.password_panel.place_forget()
        self.password_form_visible = False
        self.password_var.set("")
        self.message_var.set("Enter password to unlock")
        self.overlay.configure(cursor="none")

    def show(self):
        self.overlay.geometry(f"{self.width}x{self.height}{self.x:+d}{self.y:+d}")
        if self.password_required:
            password_protection_active.set()
            self.hide_password_form()
        self.overlay.deiconify()
        self.overlay.lift()
        self.visible = True

    def hide(self):
        if self.password_required:
            self.hide_password_form()
        self.overlay.withdraw()
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