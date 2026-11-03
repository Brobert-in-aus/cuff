"""Probe for gaze on the Steam Frame, for showing gaze in recordings. Run it on the headset
while wearing it and looking around (development only; it changes nothing):

    ~/.local/share/cuff/venv/bin/python tools/gaze_probe.py

A. OpenVR input: SteamVR serves IVRInput_011, whose GetEyeTrackingDataRelativeToNow (slot 12
   in Valve's openvr_capi.h) reads an "eyetracking" action. The binding's device path isn't
   documented, so this tries the likely ones and prints whether gaze comes through. (SteamVR
   may keep input inactive for apps that aren't a Steam game, as it does hand poses.)
B. The eye-tracking server's shared memory, /dev/shm/eye-server.mmap (world-readable,
   undocumented, client/server with protobuf messages): snapshots of its first 4 KB a few
   times a second, then which byte offsets changed, to find the gaze fields.
"""
import ctypes
import json
import os
import tempfile
import time

LIB = "/opt/steamvr/bin/linuxarm64/libopenvr_api.so"
MMAP = "/dev/shm/eye-server.mmap"
PATHS = ["/user/eyes_ext", "/user/eyes", "/user/head"]


class Vec3(ctypes.Structure):
    _fields_ = [("v", ctypes.c_float * 3)]


class EyeData(ctypes.Structure):  # VREyeTrackingData_t
    _fields_ = [("active", ctypes.c_bool), ("valid", ctypes.c_bool), ("tracked", ctypes.c_bool),
                ("origin", Vec3), ("target", Vec3)]


class ActiveSet(ctypes.Structure):  # VRActiveActionSet_t
    _fields_ = [("set", ctypes.c_uint64), ("device", ctypes.c_uint64), ("secondary", ctypes.c_uint64),
                ("padding", ctypes.c_uint32), ("priority", ctypes.c_int32)]


def slot(table, i, restype, *argtypes):
    return ctypes.CFUNCTYPE(restype, *argtypes)(table[i])


def openvr_route(path, seconds=4.0):
    lib = ctypes.CDLL(LIB)
    lib.VR_InitInternal2.restype = ctypes.c_uint32
    lib.VR_GetGenericInterface.restype = ctypes.c_void_p
    err = ctypes.c_int(0)
    lib.VR_InitInternal2(ctypes.byref(err), 2, None)  # as an overlay app, like Cuff
    if err.value:
        return f"init failed ({err.value})"
    try:
        e = ctypes.c_int(0)
        table = ctypes.cast(lib.VR_GetGenericInterface(b"FnTable:IVRInput_011", ctypes.byref(e)),
                            ctypes.POINTER(ctypes.c_void_p))
        u64p = ctypes.POINTER(ctypes.c_uint64)
        set_manifest = slot(table, 0, ctypes.c_int, ctypes.c_char_p)
        get_set = slot(table, 1, ctypes.c_int, ctypes.c_char_p, u64p)
        get_action = slot(table, 2, ctypes.c_int, ctypes.c_char_p, u64p)
        update = slot(table, 4, ctypes.c_int, ctypes.POINTER(ActiveSet), ctypes.c_uint32, ctypes.c_uint32)
        gaze = slot(table, 12, ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_float,
                    ctypes.POINTER(EyeData), ctypes.c_uint32)

        folder = tempfile.mkdtemp(prefix="gaze-probe-")
        bindings = {"controller_type": "frame_hmd", "bindings": {"/actions/probe": {
            "eyetracking": [{"output": "/actions/probe/in/gaze", "path": path}]}}}
        manifest = {"actions": [{"name": "/actions/probe/in/gaze", "type": "eyetracking"}],
                    "action_sets": [{"name": "/actions/probe", "usage": "single"}],
                    "default_bindings": [{"controller_type": "frame_hmd", "binding_url": "bindings.json"}]}
        json.dump(bindings, open(os.path.join(folder, "bindings.json"), "w"))
        json.dump(manifest, open(os.path.join(folder, "actions.json"), "w"))
        out = [f"manifest: {set_manifest(os.path.join(folder, 'actions.json').encode())}"]
        hset, hact = ctypes.c_uint64(), ctypes.c_uint64()
        out.append(f"set: {get_set(b'/actions/probe', ctypes.byref(hset))}, "
                   f"action: {get_action(b'/actions/probe/in/gaze', ctypes.byref(hact))}")
        active = ActiveSet(set=hset.value)
        seen = {}
        end = time.time() + seconds
        while time.time() < end:
            update(ctypes.byref(active), ctypes.sizeof(ActiveSet), 1)
            d = EyeData()
            r = gaze(hact.value, 1, 0.0, ctypes.byref(d), ctypes.sizeof(EyeData))
            key = (r, d.active, d.valid, d.tracked)
            if key not in seen:
                seen[key] = [round(x, 3) for x in d.target.v]
            time.sleep(0.1)
        out += [f"result {k[0]} active={k[1]} valid={k[2]} tracked={k[3]} target={v}" for k, v in seen.items()]
        return "\n    ".join(out)
    finally:
        lib.VR_ShutdownInternal()


def mmap_route(seconds=5.0, every=0.2, size=4096):
    if not os.access(MMAP, os.R_OK):
        return f"{MMAP}: not readable"
    shots = []
    end = time.time() + seconds
    while time.time() < end:
        with open(MMAP, "rb") as f:
            shots.append(f.read(size))
        time.sleep(every)
    changed = [i for i in range(0, size, 4) if len({s[i:i + 4] for s in shots}) > 1]
    lines = [f"{len(shots)} snapshots; {len(changed)} of {size // 4} words changed"]
    for i in changed[:40]:
        vals = [int.from_bytes(s[i:i + 4], "little") for s in shots[:6]]
        floats = [round(ctypes.c_float.from_buffer_copy(s[i:i + 4]).value, 4) for s in shots[:6]]
        lines.append(f"offset {i:5d}: u32 {vals}  f32 {floats}")
    return "\n    ".join(lines)


if __name__ == "__main__":
    print("A. OpenVR eye-tracking action (IVRInput_011)")
    for p in PATHS:
        try:
            print(f"  path {p}:\n    {openvr_route(p)}")
        except Exception as e:  # noqa: BLE001 - a probe: report and carry on
            print(f"  path {p}: {e!r}")
    print("B. /dev/shm/eye-server.mmap")
    print(f"    {mmap_route()}")
