"""The status bar's readings: the time and batteries."""
import os
import time

from .vr import openvr


def battery(system, i):
    """(level 0-1 or None, "charging" or "battery") for a tracked device, from SteamVR."""
    if i is None:
        return None, "battery"
    try:
        if not system.getBoolTrackedDeviceProperty(i, openvr.Prop_DeviceProvidesBatteryStatus_Bool):
            return None, "battery"
        charging = system.getBoolTrackedDeviceProperty(i, openvr.Prop_DeviceIsCharging_Bool)
        return (system.getFloatTrackedDeviceProperty(i, openvr.Prop_DeviceBatteryPercentage_Float),
                "charging" if charging else "battery")
    except openvr.error_code.OpenVRError:
        return None, "battery"


POWER = "/sys/class/power_supply"


def headset_power(system):
    """(level, state) for the Frame itself, from its own power supplies: the battery's level and
    current, and whether the USB-C input is live. state is
      "charging"  current flowing into the battery
      "plugged"   USB-C power present but the battery still draining (a weak or nearly flat
                  pack can't keep up with the headset)
      "battery"   nothing plugged in (a flat pack cuts out, so this is "swap the pack")
    Falls back to SteamVR's properties if the sysfs files aren't there."""
    def read(path):
        with open(path) as f:
            return f.read().strip()
    try:
        bat = next(os.path.join(POWER, d) for d in os.listdir(POWER)
                   if read(os.path.join(POWER, d, "type")) == "Battery")
        level = int(read(os.path.join(bat, "capacity"))) / 100
        current = int(read(os.path.join(bat, "current_now")))  # microamps, + into the battery
        status = read(os.path.join(bat, "status"))
    except (OSError, ValueError, StopIteration):
        return battery(system, openvr.k_unTrackedDeviceIndex_Hmd)
    usb = False
    for d in os.listdir(POWER):
        try:
            if read(os.path.join(POWER, d, "type")) in ("USB", "Mains") and read(os.path.join(POWER, d, "online")) == "1":
                usb = True
        except OSError:
            continue
    if status in ("Charging", "Full") or current > 0:
        return level, "charging"
    return level, "plugged" if usb else "battery"


def read_status(system, hands, quick):
    """Time and batteries for the status bar: headset, left and right controllers, and (E)
    Bluetooth earbuds while connected ones report a level (quick.py polls BlueZ)."""
    batteries = [("H", *headset_power(system)),
                 ("L", *battery(system, hands.index("left"))),
                 ("R", *battery(system, hands.index("right")))]
    if quick.state.get("earbuds") is not None:
        batteries.append(("E", quick.state["earbuds"], "battery"))
    return {"time": time.strftime("%-I:%M %p").lower(), "batteries": batteries}
