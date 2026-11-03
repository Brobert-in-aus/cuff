"""Which USB speed the Steam Frame's wireless dongle (Valve 28DE:2432) negotiated with the PC.
On USB 2 it carries only about 240-320 Mbit/s of stream, whatever the Wi-Fi rate says, and
Windows' own driver settings can claim USB 3 when it isn't.

Walks the USB hubs and asks each port what's plugged in (the hub IOCTLs need no admin).
The EX query can say High-Speed for a SuperSpeed device; the V2 query's operating flags settle
it. Ported from SteamFrame Advanced Options (MIT,
github.com/elliotttate/SteamFrame-Advanced-Options, UsbDongleConnection.cs).

  python usbdongle.py     print each dongle's speed
"""
import ctypes
import os
import struct
import sys
import uuid
from ctypes import wintypes

VID, PID = 0x28DE, 0x2432
HUB_INTERFACE = uuid.UUID("f18a0e88-c30c-11d0-8815-00a0c906bed8")
GET_NODE_INFORMATION = 0x220408
GET_CONNECTION_INFORMATION_EX = 0x220448
GET_CONNECTION_INFORMATION_EX_V2 = 0x22045C
CONNECTION_SIZE = 35  # USB_NODE_CONNECTION_INFORMATION_EX before its pipe list (packed to 1)
SPEEDS = {0: "USB 1", 1: "USB 1", 2: "USB 2", 3: "USB 3"}


class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]


class InterfaceData(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("guid", GUID), ("flags", wintypes.DWORD), ("reserved", ctypes.c_void_p)]


def _api():
    setup, k32 = ctypes.WinDLL("setupapi", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                  wintypes.DWORD, ctypes.POINTER(InterfaceData)]
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(InterfaceData), ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return setup, k32


def _query(k32, hub, code, buf, minimum):
    got = wintypes.DWORD()
    return bool(k32.DeviceIoControl(hub, code, buf, len(buf), buf, len(buf), ctypes.byref(got), None)) and got.value >= minimum


def _connection(k32, hub, port):
    buf = ctypes.create_string_buffer(CONNECTION_SIZE + 32 * 11)  # room for the variable pipe list
    struct.pack_into("<I", buf, 0, port)
    return buf.raw if _query(k32, hub, GET_CONNECTION_INFORMATION_EX, buf, CONNECTION_SIZE) else None


def _is_dongle(raw):
    return (raw and struct.unpack_from("<I", raw, 31)[0] == 1  # DeviceConnected
            and raw[4] == 18 and raw[5] == 1  # a device descriptor
            and struct.unpack_from("<HH", raw, 12) == (VID, PID))


def _speed(speed, flags):
    if flags is not None and flags & 0b101:  # operating at SuperSpeed (1) or SuperSpeedPlus (4)
        return "USB 3"
    if speed == 2 and flags is None:
        return None  # EX alone can't tell High-Speed from SuperSpeed
    return SPEEDS.get(speed)


def dongles():
    """Each connected dongle's speed ("USB 3", "USB 2", "USB 1", or None if Windows couldn't
    say); [] with none plugged in, None off Windows or if the hubs couldn't be read."""
    if os.name != "nt":
        return None
    setup, k32 = _api()
    guid = GUID((ctypes.c_ubyte * 16)(*HUB_INTERFACE.bytes_le))
    devices = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    if not devices or devices == ctypes.c_void_p(-1).value:
        return None
    out = []
    try:
        index = 0
        while True:
            data = InterfaceData(size=ctypes.sizeof(InterfaceData))
            if not setup.SetupDiEnumDeviceInterfaces(devices, None, ctypes.byref(guid), index, ctypes.byref(data)):
                break
            index += 1
            need = wintypes.DWORD()
            setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            if not 6 <= need.value <= 65536:
                continue
            detail = ctypes.create_string_buffer(need.value)
            struct.pack_into("<I", detail, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if not setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(data), detail, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            hub = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights: the IOCTLs are FILE_ANY_ACCESS
            if hub in (None, wintypes.HANDLE(-1).value):
                continue
            try:
                info = ctypes.create_string_buffer(76)
                if not _query(k32, hub, GET_NODE_INFORMATION, info, 7):
                    continue
                for port in range(1, info.raw[6] + 1):  # bNumberOfPorts
                    raw = _connection(k32, hub, port)
                    if not _is_dongle(raw):
                        continue
                    v2 = ctypes.create_string_buffer(16)
                    struct.pack_into("<III", v2, 0, port, 16, 7)  # we understand USB 1, 2 and 3
                    flags = struct.unpack_from("<I", v2.raw, 12)[0] if _query(
                        k32, hub, GET_CONNECTION_INFORMATION_EX_V2, v2, 16) else None
                    again = _connection(k32, hub, port)  # unplugged in between? don't mix two devices
                    if _is_dongle(again):
                        out.append(_speed(again[23], flags))
            finally:
                k32.CloseHandle(hub)
    finally:
        setup.SetupDiDestroyDeviceInfoList(devices)
    return out


if __name__ == "__main__":
    print(dongles(), file=sys.stdout)
