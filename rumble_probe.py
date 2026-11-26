"""
Rumble probe: checks whether your pad responds to the DragonRise-style raw vibration
command (the one used by livetech_bridge.py).

Setup:  pip install hidapi
Run:    python rumble_probe.py                 (defaults: VID 0x0079, PID 0x0006)
        python rumble_probe.py 0x0079 0x0006   (or your pad's VID/PID)

Close x360ce, PCSX2, gamepad tester pages and the Windows properties window first.
Only try pads whose VID/PID you know. This sends raw USB data to the device.
"""
import sys
import time
import hid

VID = int(sys.argv[1], 0) if len(sys.argv) > 2 else 0x0079
PID = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x0006


def send(dev, report_id, data):
    payload = bytes([report_id]) + bytes(data) + bytes(8 - len(data))
    try:
        return dev.write(payload)
    except Exception as e:
        return f"error: {e}"


def play(dev, rid, weak, strong):
    return (send(dev, rid, [0x51, 0x00, weak, 0x00, strong, 0x00, 0x00]),
            send(dev, rid, [0xFA, 0xFE, 0x00, 0x00, 0x00, 0x00, 0x00]))


def stop(dev, rid):
    send(dev, rid, [0xF3, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def main():
    infos = hid.enumerate(VID, PID)
    if not infos:
        print(f"No HID device with VID_{VID:04X} PID_{PID:04X} found. Check it is plugged in and OK in Device Manager.")
        return
    print(f"Found VID_{VID:04X} PID_{PID:04X}:", infos[0].get("product_string"))
    dev = hid.device()
    dev.open_path(infos[0]["path"])

    for rid in (0x00, 0x01):
        for name, weak, strong in (("small motor", 0xFF, 0x00),
                                   ("big motor", 0x00, 0xFF),
                                   ("both motors", 0xFF, 0xFF)):
            print(f"[report id {rid}] {name} for 1.5s ... ", end="", flush=True)
            res = play(dev, rid, weak, strong)
            time.sleep(1.5)
            stop(dev, rid)
            print("sent", res)
            time.sleep(0.8)
        if input(f"Did ANY vibration happen with report id {rid}? (y/n) ").strip().lower() == "y":
            print(f"\nGood: report id {rid} works. Set REPORT_ID = {rid:#04x} in livetech_bridge.py.")
            break
    stop(dev, 0x00)
    stop(dev, 0x01)
    dev.close()


if __name__ == "__main__":
    main()
