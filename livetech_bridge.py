"""
Generic USB pad (DragonRise-style, VID 0x0079 / PID 0x0006) -> virtual Xbox 360 pad,
with working vibration.

The pad is found by its USB VID/PID, NOT by its name, so it works whatever the pad calls
itself. Pads with a different VID/PID are ignored, so your other controllers are never touched.

The virtual Xbox pad needs the ViGEmBus driver. On start this program checks whether it is
installed. If it is, it is left alone. If not, the program asks your permission, downloads the
official installer and installs it (Windows will show an administrator prompt).

Usage:
  python livetech_bridge.py                run the bridge (leave window open, close it to quit)
  python livetech_bridge.py --list         list all controllers with VID/PID
  python livetech_bridge.py --show         print axes/buttons (to fix the mapping)
  python livetech_bridge.py --rumble       buzz both motors for 2 seconds (also: --rumble big, --rumble small)
  python livetech_bridge.py --stop         switch the motors off
  python livetech_bridge.py --reset-pad    restart the pad's USB device (run as admin)
  python livetech_bridge.py --min 160      weakest vibration sent, 0-255 (default 160; 0 = pass through unchanged)
  python livetech_bridge.py --hold 170     minimum time a vibration peak stays on, in ms (default 170; 0 = off)
  python livetech_bridge.py --debug        run the bridge and print the vibration values games send
  python livetech_bridge.py --write-config  create livetech_config.json (edit buttons/axes in Notepad)
  python livetech_bridge.py --no-driver-install   never offer to install ViGEmBus

Close x360ce first (it would create a second virtual pad).
UNTESTED on pads other than the author's. The code is short: it reads your pad, creates a
virtual Xbox pad, and writes vibration to your pad.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"   # keep reading when unfocused
os.environ["SDL_VIDEODRIVER"] = "dummy"

import hid
import pygame

vg = None   # vgamepad is imported later, only after the ViGEmBus driver is confirmed present
B = None

# ----------------------------- CONFIG ---------------------------------
TARGET_VID = 0x0079
TARGET_PID = 0x0006
REPORT_ID = 0x00            # the report id that worked in rumble_probe.py
SWAP_MOTORS = False         # set True if big/small feel reversed

LEFT_X, LEFT_Y = 0, 1       # axis numbers (use --show to check)
RIGHT_X, RIGHT_Y = 2, 3
INVERT_Y = True             # pygame Y is down-positive; Xbox is up-positive
DEADZONE = 0.12
MIN_STRENGTH = [160, 160]   # [big, small]: any vibration weaker than this (but not zero) is raised to it.
                            # Cheap pads often can't spin their motors at low values. 0 = off.
HOLD_MS = 170               # keep a vibration peak on for at least this many ms, because games often send
                            # pulses only ~16 ms long that cheap pads can't feel. 0 = off.
DEBUG = False               # print the vibration values the game sends (use --debug)

# pygame button index -> Xbox button name. Typical generic PS2-style layout; adjust via --show.
BUTTON_MAP_NAMES = {
    0: "XUSB_GAMEPAD_Y",               # Triangle
    1: "XUSB_GAMEPAD_B",               # Circle
    2: "XUSB_GAMEPAD_A",               # Cross
    3: "XUSB_GAMEPAD_X",               # Square
    6: "XUSB_GAMEPAD_LEFT_SHOULDER",   # L1
    7: "XUSB_GAMEPAD_RIGHT_SHOULDER",  # R1
    8: "XUSB_GAMEPAD_BACK",            # Select
    9: "XUSB_GAMEPAD_START",           # Start
    10: "XUSB_GAMEPAD_LEFT_THUMB",     # L3
    11: "XUSB_GAMEPAD_RIGHT_THUMB",    # R3
}
LEFT_TRIGGER_BUTTON = 4     # L2
RIGHT_TRIGGER_BUTTON = 5    # R2

# ViGEmBus installer (the project is retired but still works on Windows 10/11).
# URL is the official GitHub release asset. If it ever fails, the program shows the releases page.
VIGEM_RELEASES_PAGE = "https://github.com/nefarius/ViGEmBus/releases"
VIGEM_URL = "https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe"
VIGEM_SHA256 = ""   # paste the SHA-256 of the official installer here to enforce a check
# ----------------------------------------------------------------------

BUTTON_MAP = {}
rumble_state = {"large": 0, "small": 0}
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
stop_event = threading.Event()   # set when the user closes the window / presses Ctrl+C
done_event = threading.Event()   # set when cleanup (motors off) has finished
_ctrl_handler_ref = None


def install_close_handler():
    """Make closing the console window (X button), Ctrl+C, logoff or shutdown stop the
    bridge cleanly (motors switched off) instead of killing it mid-vibration."""
    global _ctrl_handler_ref
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    HandlerType = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(ctrl_type):
        stop_event.set()
        done_event.wait(4)   # Windows gives about 5 s after the X button
        return True

    _ctrl_handler_ref = HandlerType(handler)   # keep a reference so it is not garbage collected
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler_ref, True)


def stamp():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


def pause_if_frozen(msg="Press Enter to close this window..."):
    """When running as a double-clicked exe the window would vanish; let the user read it."""
    if getattr(sys, "frozen", False):
        try:
            input(msg)
        except Exception:
            pass


# ------------------------------ config file ---------------------------
CONFIG_NAME = "livetech_config.json"


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), CONFIG_NAME)


def load_config():
    """Optional livetech_config.json next to the exe/script overrides the defaults above,
    so exe users can fix their button layout without any programming."""
    global SWAP_MOTORS, REPORT_ID, LEFT_X, LEFT_Y, RIGHT_X, RIGHT_Y, INVERT_Y, DEADZONE
    global LEFT_TRIGGER_BUTTON, RIGHT_TRIGGER_BUTTON, BUTTON_MAP_NAMES, HOLD_MS, MIN_STRENGTH
    path = config_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        SWAP_MOTORS = bool(c.get("swap_motors", SWAP_MOTORS))
        REPORT_ID = int(c.get("report_id", REPORT_ID))
        LEFT_X = int(c.get("left_x", LEFT_X))
        LEFT_Y = int(c.get("left_y", LEFT_Y))
        RIGHT_X = int(c.get("right_x", RIGHT_X))
        RIGHT_Y = int(c.get("right_y", RIGHT_Y))
        INVERT_Y = bool(c.get("invert_y", INVERT_Y))
        DEADZONE = float(c.get("deadzone", DEADZONE))
        HOLD_MS = int(c.get("rumble_hold_ms", HOLD_MS))
        MIN_STRENGTH = [int(c.get("rumble_min_big", MIN_STRENGTH[0])),
                        int(c.get("rumble_min_small", MIN_STRENGTH[1]))]
        LEFT_TRIGGER_BUTTON = int(c.get("left_trigger_button", LEFT_TRIGGER_BUTTON))
        RIGHT_TRIGGER_BUTTON = int(c.get("right_trigger_button", RIGHT_TRIGGER_BUTTON))
        if "button_map" in c:
            BUTTON_MAP_NAMES = {int(k): str(v) for k, v in c["button_map"].items()}
        print("Loaded settings from", path)
    except Exception as e:
        print(f"Could not read {path}: {e}\nUsing default settings.")


def write_config_template():
    path = config_path()
    if os.path.exists(path):
        print(path, "already exists. Delete it first if you want a fresh template.")
        return
    data = {
        "swap_motors": SWAP_MOTORS,
        "report_id": REPORT_ID,
        "left_x": LEFT_X, "left_y": LEFT_Y, "right_x": RIGHT_X, "right_y": RIGHT_Y,
        "invert_y": INVERT_Y,
        "deadzone": DEADZONE,
        "rumble_hold_ms": HOLD_MS,
        "rumble_min_big": MIN_STRENGTH[0],
        "rumble_min_small": MIN_STRENGTH[1],
        "left_trigger_button": LEFT_TRIGGER_BUTTON,
        "right_trigger_button": RIGHT_TRIGGER_BUTTON,
        "button_map": {str(k): v for k, v in BUTTON_MAP_NAMES.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print("Wrote", path)
    print("Edit it with Notepad. Use --show to find your pad's button numbers.")
    print("Valid Xbox button names: XUSB_GAMEPAD_A, _B, _X, _Y, _LEFT_SHOULDER, _RIGHT_SHOULDER,")
    print("  _BACK, _START, _LEFT_THUMB, _RIGHT_THUMB, _GUIDE")


# ------------------------------ ViGEmBus ------------------------------
def vigem_installed():
    try:
        r = subprocess.run(["sc", "query", "ViGEmBus"], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        return r.returncode == 0 and "RUNNING" in r.stdout
    except Exception:
        return False


def install_vigem():
    """Download the official installer and run it silently with an administrator prompt."""
    path = os.path.join(tempfile.gettempdir(), "ViGEmBus_setup.exe")
    print("Downloading ViGEmBus installer from the official GitHub release...")
    try:
        urllib.request.urlretrieve(VIGEM_URL, path)
    except Exception as e:
        print("Download failed:", e)
        print("Please install it manually from:", VIGEM_RELEASES_PAGE)
        return False

    digest = hashlib.sha256(open(path, "rb").read()).hexdigest().upper()
    print("SHA-256 of downloaded installer:", digest)
    if VIGEM_SHA256 and digest != VIGEM_SHA256.upper():
        print("Checksum does NOT match the expected value. Not installing.")
        os.remove(path)
        return False

    print("Installing (a Windows administrator prompt will appear - choose Yes)...")
    ps = (f"$p = Start-Process -FilePath '{path}' -ArgumentList '/exenoui','/qn','/norestart' "
          "-Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW)
    except Exception as e:
        print("Could not start the installer:", e)
        return False
    print("Installer finished with code", r.returncode)
    time.sleep(2)
    return vigem_installed()


def ensure_vigem(allow_install):
    if vigem_installed():
        return True
    print("\nThe ViGEmBus driver (creates the virtual Xbox controller) is not installed.")
    if not allow_install:
        print("Install it from:", VIGEM_RELEASES_PAGE)
        return False
    ans = input("Download and install it now? [y/N] ").strip().lower()
    if ans != "y":
        print("Skipped. Install it manually from:", VIGEM_RELEASES_PAGE)
        return False
    if install_vigem():
        print("ViGEmBus installed.")
        return True
    print("ViGEmBus still not detected. If it was just installed, restart Windows and run this again.")
    return False


def load_vgamepad():
    global vg, B, BUTTON_MAP
    import vgamepad as _vg
    vg = _vg
    B = vg.XUSB_BUTTON
    BUTTON_MAP = {}
    for idx, name in BUTTON_MAP_NAMES.items():
        if not hasattr(B, name):
            raise SystemExit(f"Unknown Xbox button name '{name}' in settings. "
                             "Use names like XUSB_GAMEPAD_A, XUSB_GAMEPAD_LEFT_SHOULDER.")
        BUTTON_MAP[idx] = getattr(B, name)


# ---------------------------- identification --------------------------
def guid_vid_pid(guid):
    """SDL joystick GUID on Windows: 4 bytes bus/crc, VID (2 bytes LE), 2 zero bytes,
    PID (2 bytes LE), ... Returns (vid, pid) or None."""
    g = (guid or "").lower()
    if len(g) != 32:
        return None
    try:
        return int(g[10:12] + g[8:10], 16), int(g[18:20] + g[16:18], 16)
    except ValueError:
        return None


def list_joysticks():
    out = []
    for i in range(pygame.joystick.get_count()):
        js = pygame.joystick.Joystick(i)
        out.append((js, js.get_name(), guid_vid_pid(js.get_guid())))
    return out


def find_joystick(vid, pid, index):
    matches = [js for js, _, vp in list_joysticks() if vp == (vid, pid)]
    if len(matches) > index:
        js = matches[index]
        js.init()
        return js
    return None


# ------------------------------ Windows PnP ---------------------------
def pnp_devices(vid, pid):
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like "
          f"'USB\\VID_{vid:04X}&PID_{pid:04X}*' }} | "
          "Select-Object Status,Class,FriendlyName,InstanceId | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW)
        data = json.loads(r.stdout) if r.stdout.strip() else []
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def explain_missing(vid, pid):
    print(f"\nPad with VID_{vid:04X} / PID_{pid:04X} is not ready.")
    devs = pnp_devices(vid, pid)
    if not devs:
        print("  Windows does not list it at all. Check the cable, try a rear USB port (no hub).")
    for d in devs:
        print(f"  Windows sees: {d.get('FriendlyName')}  status={d.get('Status')}  class={d.get('Class')}")
        if d.get("Status") != "OK":
            print("  -> Windows reports a problem with this USB device. Unplug it, wait 5 s, plug it into a")
            print("     different (rear, USB 2.0) port. Or run with --reset-pad as administrator.")
    print("  Also make sure x360ce and other controller tools are closed. Waiting for the pad...\n")


def reset_pad(vid, pid):
    devs = pnp_devices(vid, pid)
    if not devs:
        print("No matching USB device found to reset.")
        return
    for d in devs:
        iid = d.get("InstanceId")
        print("Restarting", iid)
        r = subprocess.run(["pnputil", "/restart-device", iid], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        print(r.stdout.strip() or r.stderr.strip())
    print("If it said 'access denied', run as Administrator and try again.")


def wait_for_pad(vid, pid, index, timeout=90):
    start = time.time()
    last_msg = 0
    while not stop_event.is_set():
        pygame.joystick.quit()
        pygame.joystick.init()
        js = find_joystick(vid, pid, index)
        if js is not None and hid.enumerate(vid, pid):
            return js
        if time.time() - last_msg > 15:
            explain_missing(vid, pid)
            last_msg = time.time()
        if time.time() - start > timeout:
            return None
        time.sleep(2)
    return None


# ------------------------------ vibration -----------------------------
def open_hid(vid, pid):
    infos = hid.enumerate(vid, pid)
    if not infos:
        raise RuntimeError("HID interface not found")
    dev = hid.device()
    dev.open_path(infos[0]["path"])
    return dev


def hid_send(dev, data):
    payload = bytes([REPORT_ID]) + bytes(data) + bytes(8 - len(data))
    try:
        dev.write(payload)
    except Exception as e:
        print("HID write error:", e)


def set_motors(dev, large, small):
    strong, weak = (small, large) if SWAP_MOTORS else (large, small)
    if strong == 0 and weak == 0:
        hid_send(dev, [0xF3, 0, 0, 0, 0, 0, 0])
    else:
        hid_send(dev, [0x51, 0x00, weak, 0x00, strong, 0x00, 0x00])
        hid_send(dev, [0xFA, 0xFE, 0, 0, 0, 0, 0])


# ------------------------------- bridge -------------------------------
def dz(v):
    return 0.0 if abs(v) < DEADZONE else v


def update_pad(pad, js):
    lx, ly = dz(js.get_axis(LEFT_X)), dz(js.get_axis(LEFT_Y))
    rx, ry = dz(js.get_axis(RIGHT_X)), dz(js.get_axis(RIGHT_Y))
    if INVERT_Y:
        ly, ry = -ly, -ry
    pad.left_joystick_float(x_value_float=lx, y_value_float=ly)
    pad.right_joystick_float(x_value_float=rx, y_value_float=ry)

    for idx, xbtn in BUTTON_MAP.items():
        if idx < js.get_numbuttons():
            (pad.press_button if js.get_button(idx) else pad.release_button)(button=xbtn)

    pad.left_trigger_float(value_float=1.0 if js.get_button(LEFT_TRIGGER_BUTTON) else 0.0)
    pad.right_trigger_float(value_float=1.0 if js.get_button(RIGHT_TRIGGER_BUTTON) else 0.0)

    if js.get_numhats():
        hx, hy = js.get_hat(0)
        for flag, on in ((B.XUSB_GAMEPAD_DPAD_UP, hy == 1), (B.XUSB_GAMEPAD_DPAD_DOWN, hy == -1),
                         (B.XUSB_GAMEPAD_DPAD_LEFT, hx == -1), (B.XUSB_GAMEPAD_DPAD_RIGHT, hx == 1)):
            (pad.press_button if on else pad.release_button)(button=flag)
    pad.update()


def run_bridge(args):
    if not ensure_vigem(not args.no_driver_install):
        return 1
    load_vgamepad()

    js = wait_for_pad(args.vid, args.pid, args.index)
    if js is None:
        print("Pad never became ready. Giving up.")
        return 1
    print(f"Using pad: {js.get_name()} (VID_{args.vid:04X} PID_{args.pid:04X})")
    dev = open_hid(args.vid, args.pid)

    pad = vg.VX360Gamepad()

    def on_rumble(client, target, large_motor, small_motor, led_number, user_data):
        rumble_state["large"] = large_motor
        rumble_state["small"] = small_motor
        if DEBUG:
            print(f"{stamp()} [game vibration] large={large_motor} small={small_motor}", flush=True)

    pad.register_notification(callback_function=on_rumble)
    print("Virtual Xbox 360 pad created. Bridge running.")
    print("Leave this window open (minimize it) while you play. Close the window to quit.")

    last = (0, 0)
    out = [0, 0]              # what is actually sent to the pad (large, small)
    hold_until = [0.0, 0.0]
    try:
        while not stop_event.is_set():
            lost = False
            for ev in pygame.event.get():
                if ev.type == pygame.JOYDEVICEREMOVED:
                    lost = True
            if lost:
                print("A controller was removed. Re-checking for the pad...")
                pad.reset()
                pad.update()
                try:
                    dev.close()
                except Exception:
                    pass
                js = wait_for_pad(args.vid, args.pid, args.index)
                if js is None:
                    print("Pad did not come back. Stopping.")
                    return 1
                dev = open_hid(args.vid, args.pid)
                last = (0, 0)
                out = [0, 0]
                print("Pad is back.")
                continue

            update_pad(pad, js)

            now = time.time()
            for i, key in enumerate(("large", "small")):
                g = rumble_state[key]
                if 0 < g < MIN_STRENGTH[i]:
                    g = MIN_STRENGTH[i]             # raise weak requests the pad can't feel
                if g > out[i]:                      # stronger request: apply now, start hold timer
                    out[i] = g
                    hold_until[i] = now + HOLD_MS / 1000.0
                elif g < out[i] and now >= hold_until[i]:
                    out[i] = g                      # hold over: follow the game again
            cur = (out[0], out[1])
            if cur != last:
                if DEBUG:
                    print(f"{stamp()} [sent to pad]      large={cur[0]} small={cur[1]}", flush=True)
                set_motors(dev, *cur)
                last = cur

            time.sleep(0.004)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            set_motors(dev, 0, 0)
            dev.close()
        except Exception:
            pass
        print("Stopped.")
        done_event.set()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--rumble", nargs="?", const="both", choices=["both", "big", "small"],
                    help="buzz the motors for 2 seconds: both (default), big, or small")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--reset-pad", action="store_true")
    ap.add_argument("--hold", type=int, metavar="MS",
                    help="keep each vibration peak on for at least MS milliseconds (default 170, 0 = off)")
    ap.add_argument("--min", type=int, metavar="N", dest="min_strength",
                    help="raise any non-zero vibration weaker than N (0-255) up to N, for both motors (default 160, 0 = off)")
    ap.add_argument("--debug", action="store_true",
                    help="print the vibration values the game sends (to find out why a game doesn't vibrate)")
    ap.add_argument("--write-config", action="store_true",
                    help="create livetech_config.json next to the program, to edit buttons/axes")
    ap.add_argument("--no-driver-install", action="store_true", help="never offer to install ViGEmBus")
    ap.add_argument("--vid", type=lambda x: int(x, 0), default=TARGET_VID, help="USB vendor id, e.g. 0x0079")
    ap.add_argument("--pid", type=lambda x: int(x, 0), default=TARGET_PID, help="USB product id, e.g. 0x0006")
    ap.add_argument("--index", type=int, default=0, help="which matching pad if you have several")
    args = ap.parse_args()

    install_close_handler()
    if args.write_config:
        write_config_template()
        return 0
    load_config()
    global DEBUG, HOLD_MS, MIN_STRENGTH
    DEBUG = args.debug
    if args.hold is not None:
        HOLD_MS = args.hold
    if args.min_strength is not None:
        MIN_STRENGTH = [args.min_strength, args.min_strength]
    pygame.init()
    pygame.joystick.init()

    if args.reset_pad:
        reset_pad(args.vid, args.pid)
        return 0

    if args.list:
        print("Controllers seen by SDL (name, VID, PID):")
        for _, name, vp in list_joysticks():
            print("  ", name, "-", f"VID_{vp[0]:04X} PID_{vp[1]:04X}" if vp else "VID/PID unknown")
        print("\nHID devices matching target:", [(d["product_string"], d["path"]) for d in hid.enumerate(args.vid, args.pid)])
        return 0

    if args.show:
        js = wait_for_pad(args.vid, args.pid, args.index, timeout=30)
        if js is None:
            return 1
        print("Move sticks / press buttons. Ctrl+C to stop.\n")
        try:
            while True:
                pygame.event.pump()
                axes = [round(js.get_axis(i), 2) for i in range(js.get_numaxes())]
                btns = [i for i in range(js.get_numbuttons()) if js.get_button(i)]
                hats = [js.get_hat(i) for i in range(js.get_numhats())]
                print(f"\raxes={axes} buttons={btns} hat={hats}        ", end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            return 0

    if args.stop or args.rumble:
        dev = open_hid(args.vid, args.pid)
        if args.rumble:
            big, small = {"both": (255, 255), "big": (255, 0), "small": (0, 255)}[args.rumble]
            print(f"Motor test ({args.rumble}) for 2s...")
            set_motors(dev, big, small)
            time.sleep(2)
        set_motors(dev, 0, 0)
        dev.close()
        return 0

    try:
        return run_bridge(args)
    finally:
        done_event.set()


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
    if code:
        pause_if_frozen()
    sys.exit(code)
