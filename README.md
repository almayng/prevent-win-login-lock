# Auto Mouse Mover

A Windows utility that prevents screen lock and system sleep during long idle periods. Instead of constantly moving the cursor (which can cause pixel burn-in on OLED/IPS displays), the program:

- periodically sends a harmless activity signal (pressing `Shift`);
- after prolonged user inactivity, covers all monitors with a black fullscreen overlay;
- optionally requires a password to dismiss the screen protection.

After launch, the program minimizes to the system tray. To exit, use the **Exit** item in the tray icon menu.

## Requirements

- Windows 10/11
- Python 3.11+
- Dependencies from the project's virtual environment

## Installation

```powershell
cd c:\work\Pycharm\auto-mouse-mover
python -m venv venv
venv\Scripts\pip install pyautogui pynput pystray pillow psutil pycaw
```

> **Note.** To detect Windows screen lock, you can optionally install `pywin32`. Without it, the program will still run, but the "screen locked by the system" check will be unavailable.

## Running

```powershell
venv\Scripts\python.exe mmover.py
```

On first run, a `config.json` file is automatically created next to the script if it does not already exist.

## How It Works

1. The program tracks real mouse and keyboard activity.
2. If the user is inactive for longer than `idle_time_threshold` seconds, a keep-alive signal (`Shift`) is sent so the system does not lock or sleep.
3. While an active, unmuted Windows audio session is detected (for example, a video or an online meeting), the black screen is not shown.
4. If idle time exceeds `display_protection_threshold` seconds and no media is playing, a black screen is shown over all monitors — this protects the panel from static images.
5. Any mouse movement or key press dismisses the black screen **only if password protection is disabled**.
6. If a password is enabled, you must enter it in the form on the black screen and press Enter to unlock.

## Configuration

All settings are stored in the `config.json` file next to `mmover.py`.

### Example Configuration

```json
{
  "idle_time_threshold": 20,
  "display_protection_threshold": 60,
  "media_detection_enabled": true,
  "password_protection_enabled": false,
  "password_prompt_timeout": 15,
  "password_salt": "",
  "password_hash": ""
}
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---:|---|
| `idle_time_threshold` | number (sec) | `20` | How many seconds of idle time before sending a keep-alive signal to prevent system lock. |
| `display_protection_threshold` | number (sec) | `60` | How many seconds of idle time before showing a black screen on all monitors. Must be **greater than or equal to** `idle_time_threshold`. |
| `media_detection_enabled` | `true` / `false` | `true` | Do not show the black screen while Windows has an active, unmuted audio session. Requires `pycaw`. |
| `password_protection_enabled` | `true` / `false` | `false` | Enable or disable password requirement to dismiss the black screen. |
| `password_prompt_timeout` | number (sec) | `15` | How many seconds to show the password input field after an unlock attempt. When the timeout expires, the screen becomes fully black again. |
| `password_salt` | string | `""` | Service field: salt for password verification. Filled automatically when setting a password. **Do not edit manually.** |
| `password_hash` | string | `""` | Service field: password hash (PBKDF2-SHA256). Filled automatically. **Do not edit manually.** |

### Recommended Values

```json
{
  "idle_time_threshold": 20,
  "display_protection_threshold": 60
}
```

- `idle_time_threshold` — set it slightly below the Windows screen lock timeout.
- `display_protection_threshold` — the higher the value, the less often the black screen appears, but static images may remain on the monitor longer.

Example for quick testing (debugging only):

```json
{
  "idle_time_threshold": 2,
  "display_protection_threshold": 5
}
```

Restart the program after changing `config.json`.

## Password Protection

The password protects dismissal of the **program's black screen**, not Windows lock. This is local protection against accidental or intentional removal of the overlay without entering a password.

The password is **not stored in plain text**. Only a cryptographic hash (PBKDF2-SHA256 with salt) is saved in the config.

### Set or Change Password

```powershell
venv\Scripts\python.exe mmover.py --set-password
```

The command will prompt for the password twice and automatically:

- generate a salt;
- save the hash in `config.json`;
- enable `"password_protection_enabled": true`.

### Disable Password Protection

**Method 1 — via command:**

```powershell
venv\Scripts\python.exe mmover.py --disable-password
```

**Method 2 — via config:**

```json
{
  "password_protection_enabled": false
}
```

The `password_salt` and `password_hash` fields can be left as-is — they simply won't be used while protection is disabled.

### Unlocking with Password

1. Wait for the black screen to appear (without an input field).
2. Move the mouse or press any key — this counts as an unlock attempt.
3. A password input field will appear. Enter the password and press **Enter**.
4. If the password is not entered within `password_prompt_timeout` seconds, the field is hidden again and the screen remains black.

With an incorrect password, the screen stays covered, but the input field remains available until the timeout expires.

## Controls

| Action | How to Perform |
|---|---|
| Start | `venv\Scripts\python.exe mmover.py` |
| Exit | Right-click tray icon → **Exit** |
| Dismiss black screen without password | Move mouse or press any key |
| Dismiss black screen with password | Move mouse or press key → enter password → Enter |

## Limitations

- Password protection does not replace Windows system lock (`Win + L`). The process can be terminated from Task Manager under the same user account.
- The keep-alive signal (`Shift`) should not interfere with work, but in rare cases it may reach the active window if it has focus.
- The black screen covers all monitors, including those positioned to the left or above the primary display.

## Verification

The project includes helper scripts for automated testing:

```powershell
venv\Scripts\python.exe test_mmover.py
venv\Scripts\python.exe smoke_test_mmover.py
```

## Project Structure

```
auto-mouse-mover/
├── mmover.py              # main script
├── config.json            # settings
├── test_mmover.py         # logic unit tests
├── smoke_test_mmover.py   # short live smoke test
└── venv/                  # Python virtual environment
```
