"""OpenVR: the arm64 library shim, pose maths, hand tip poses and double-buffered layers."""
import ctypes
import json
import math
import os
import sys
import time

from PIL import Image

from . import ui
from .config import PROFILE, RUN_DIR

OPENVR_LIB = PROFILE["openvr_lib"]

# pyopenvr only bundles x86 libraries. Hand it SteamVR's arm64 one instead.
_load = ctypes.cdll.LoadLibrary
ctypes.cdll.LoadLibrary = lambda name: _load(OPENVR_LIB if "libopenvr_api" in str(name) else name)
import openvr  # noqa: E402
ctypes.cdll.LoadLibrary = _load

# The panel's size and placement are placement.py's (the settings page changes them live).

# The poke point sits this far ahead of the right tip (along its aim, -Z), so the hand
# holding the controller doesn't get in the way of touching the panel.
POKE_REACH = 0.06

# Touch, in metres from the panel surface (+ is in front). Hysteresis stops chatter.
TOUCH_PRESS, TOUCH_RELEASE, TOUCH_HOVER, TOUCH_BEHIND = 0.004, 0.012, 0.04, -0.03
HOVER_EXIT, EDGE_EXIT = 0.055, 0.005  # the sphere's hover colour holds until this far out
TOUCH_MIN_ALPHA = 0.6  # only a panel you're clearly looking at takes touches

TIP_DIAMETER = 0.012
TIP_PX = 64


# --- 3x4 affine maths (row-major, rotation in the 3x3, translation in column 3) ---

def quat_to_mat(q, t):
    x, y, z, w = q
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), t[0]],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), t[1]],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), t[2]],
    ]


def mul(a, b):
    out = [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(4)] for r in range(3)]
    for r in range(3):
        out[r][3] += a[r][3]
    return out


def translation(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z]]


def origin(m):
    return (m[0][3], m[1][3], m[2][3])


def axis_z(m):
    return (m[0][2], m[1][2], m[2][2])


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    n = math.sqrt(dot(a, a)) or 1.0
    return tuple(x / n for x in a)


def to_local(m, p):
    """p in m's frame (m is rigid, so the inverse rotation is the transpose)."""
    d = sub(p, origin(m))
    return tuple(sum(m[r][c] * d[r] for r in range(3)) for c in range(3))


def from_hmd(m34):
    return [[m34.m[r][c] for c in range(4)] for r in range(3)]


def to_hmd(m):
    out = openvr.HmdMatrix34_t()
    for r in range(3):
        for c in range(4):
            out.m[r][c] = m[r][c]
    return out


def billboard(pos, eye):
    """Pose at pos whose +Z (an overlay's visible side) points at eye."""
    z = norm(sub(eye, pos))
    up = (0.0, 1.0, 0.0) if abs(z[1]) < 0.99 else (1.0, 0.0, 0.0)
    x = norm(cross(up, z))
    y = cross(z, x)
    return [[x[i], y[i], z[i], pos[i]] for i in range(3)]


def tip_texture(tint):
    """A shaded disc that reads as a sphere when billboarded."""
    buf = bytearray(TIP_PX * TIP_PX * 4)
    c = (TIP_PX - 1) / 2
    for y in range(TIP_PX):
        for x in range(TIP_PX):
            dx, dy = (x - c) / c, (y - c) / c
            d2 = dx * dx + dy * dy
            if d2 > 1:
                continue
            light = max(0.0, dot((dx, dy, math.sqrt(1 - d2)), norm((-0.4, -0.5, 0.75))))
            shade = 0.35 + 0.65 * light
            edge = min(1.0, (1 - math.sqrt(d2)) * c / 1.5)  # anti-aliased rim
            i = (y * TIP_PX + x) * 4
            buf[i:i + 4] = bytes([min(255, int(t * shade)) for t in tint] + [int(255 * edge)])
    return Image.frombytes("RGBA", (TIP_PX, TIP_PX), bytes(buf))


GLOW_PX = 96
GLOWS = {"white": (235, 240, 255), "orange": (255, 150, 40), "red": (255, 55, 45)}


def glow_texture(colour, icon=None):
    """A soft glow: a bright core fading out smoothly, for the alert orbs. icon: an RGBA image
    for its middle (drawn at ICON_SHARE of the glow's width), or None."""
    tint = GLOWS.get(colour, GLOWS["white"])
    buf = bytearray(GLOW_PX * GLOW_PX * 4)
    c = (GLOW_PX - 1) / 2
    for y in range(GLOW_PX):
        for x in range(GLOW_PX):
            r = math.hypot(x - c, y - c) / c  # 0 at the centre, 1 at the edge
            if r >= 1:
                continue
            core = max(0.0, 1 - r / 0.42)  # the bright middle, about the touch sphere's size
            halo = math.exp(-((r / 0.55) ** 2) * 2.2) * (1 - r)  # fades to nothing at the edge
            a = min(1.0, 0.9 * core + halo)
            white = core ** 2 * 0.6  # the very centre whitens, like a light
            px = [min(255, int(t * (1 - white) + 255 * white)) for t in tint]
            i = (y * GLOW_PX + x) * 4
            buf[i:i + 4] = bytes(px + [int(255 * a)])
    img = Image.frombytes("RGBA", (GLOW_PX, GLOW_PX), bytes(buf))
    if icon is not None:
        side = round(GLOW_PX * ICON_SHARE)
        pic = icon.convert("RGBA")
        pic.thumbnail((side, side), Image.LANCZOS)
        img.alpha_composite(pic, ((GLOW_PX - pic.width) // 2, (GLOW_PX - pic.height) // 2))
    return img


ICON_SHARE = 0.4


def battery_icon(px=64):
    """The core's low battery alerts' icon: a battery's outline in dark ink."""
    from PIL import ImageDraw
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (40, 24, 10, 240)
    w, h = px * 0.78, px * 0.44
    x0, y0 = (px - w) / 2 - px * 0.04, (px - h) / 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=px * 0.08, outline=ink, width=max(2, px // 12))
    d.rectangle((x0 + w, px / 2 - h * 0.22, x0 + w + px * 0.08, px / 2 + h * 0.22), fill=ink)
    d.rectangle((x0 + px * 0.12, y0 + px * 0.12, x0 + w * 0.32, y0 + h - px * 0.12), fill=ink)
    return img


# --- OpenVR ---

class Hands:
    """Tip poses of both controllers: the raw device pose times the render model's `tip`
    component, which is what WayVR's /user/hand/*/pose/tip resolves to.

    IVRInput pose actions would be simpler, but SteamVR leaves them inactive for an
    overlay that isn't a Steam app (ours is system.generated.python3.14), even though its
    bindings load. Raw device poses always come through.
    """

    ROLES = {"left": openvr.TrackedControllerRole_LeftHand, "right": openvr.TrackedControllerRole_RightHand}

    def __init__(self, system):
        self.system = system
        self.models = openvr.VRRenderModels()
        vin = openvr.VRInput()
        self.paths = {h: vin.getInputSourceHandle(f"/user/hand/{h}") for h in self.ROLES}
        self.tips = {}  # device index -> tip offset (3x4), or None if the model has none

    def index(self, hand):
        i = self.system.getTrackedDeviceIndexForControllerRole(self.ROLES[hand])
        return i if i != openvr.k_unTrackedDeviceIndexInvalid else None

    def _tip(self, i, hand):
        if i not in self.tips:
            try:
                model = self.system.getStringTrackedDeviceProperty(i, openvr.Prop_RenderModelName_String)
                ok, st = self.models.getComponentStateForDevicePath(
                    model, "tip", self.paths[hand], openvr.RenderModel_ControllerMode_State_t())
                self.tips[i] = from_hmd(st.mTrackingToComponentLocal) if ok else None
            except openvr.error_code.OpenVRError:
                self.tips[i] = None
            print(f"hands: device {i} tip offset {self.tips[i]}", flush=True)
        return self.tips[i]

    def poses(self, raw):
        """While SteamVR's dashboard (and so its keyboard) is up, other apps get controller
        poses blanked (bPoseIsValid 0, an identity matrix, though eTrackingResult stays
        Running_OK), apparently so typing can't be read from hand motion. Then there are none."""
        out = {}
        for hand in self.ROLES:
            i = self.index(hand)
            if i is None or not raw[i].bPoseIsValid:
                continue
            m = from_hmd(raw[i].mDeviceToAbsoluteTracking)
            tip = self._tip(i, hand)
            out[hand] = mul(m, tip) if tip else m
        return out

    def attach(self, hand, offset):
        """(device index, pose relative to that controller) for a layer at `offset` from the
        hand's tip, for Layer.place(..., device=): the compositor then moves it with the
        controller every frame, even on a frame this loop runs late."""
        i = self.index(hand)
        tip = self._tip(i, hand) if i is not None else None
        return i, (mul(tip, offset) if tip else offset)

    def pulse(self, hand):
        i = self.index(hand)
        if i is not None:
            self.system.triggerHapticPulse(i, 0, 20000)


class Layer:
    """An overlay fed from PNG files, double-buffered.

    SetOverlayRaw would avoid the PNG, but SteamVR accepts only ~200 raw uploads per
    process (visible or hidden overlays alike); after that every call fails with
    RequestFailed for good. SetOverlayFromFile has no such limit (600 of 600 in a test).

    SetOverlayFromFile loads asynchronously: the overlay has no texture until the new one
    arrives, a few frames later, and showing it then blinks. So each image goes to the twin
    behind: it is shown one sort order below the front (and covered by it) while it loads,
    then raised once its texture reports ready or after SWAP_MAX frames, whichever is first;
    the old front is hidden a frame later. While the layer isn't visible there is nothing to
    blink, so the swap is immediate. Images that arrive mid-swap wait; only the latest is kept.
    (A wait for a *hidden* twin to report ready proved unreliable: in the panel process it
    sometimes never did, while another process saw the texture loaded.)
    Every image gets a new file name, unique across runs (pid + serial): SteamVR caches
    SetOverlayFromFile by path, so reloading a path kept showing the first image, and after a
    restart the reused names showed the previous run's (old layout's) images. Only the last KEEP_FILES files
    per layer are kept, so none is deleted while the compositor might still read it.
    """

    SWAP_MIN, SWAP_MAX = 2, 12
    RETRY_FRAMES = 30
    KEEP_FILES = 6
    _last_error = 0.0

    def __init__(self, ov, key, name, width, sort=0, anchor_top=False):
        """anchor_top: images of different heights keep their top edge where an H_OPEN image's
        top is, so a collapsed panel shrinks from the bottom (the panel; the others are fixed)."""
        self.ov = ov
        self.anchor_top = anchor_top
        self.sizes = [None, None]  # each twin's image size in pixels
        self.handles = [ov.createOverlay(f"{key}.{i}", name) for i in "ab"]
        self.key, self.serial, self.files = key, 0, []  # files: recent uploads, oldest first
        for old in os.listdir(RUN_DIR):  # left over from a previous run
            if old.startswith(f"{key}."):
                os.remove(os.path.join(RUN_DIR, old))
        self.sort = sort * 2  # front at sort + 1, the loading twin just below it
        self.set_width(width)
        for h in self.handles:
            # Stay up while the dashboard (the Steam menu) is open.
            ov.setOverlayFlag(h, openvr.VROverlayFlags_VisibleInDashboard, True)
        self.front = 0
        self._order()
        self.loaded = False  # the front has an image
        self.pending = None  # PIL image waiting for the back buffer
        self.loading = None  # (frames waited, image) while the back's texture loads
        self.retiring = None  # old front, hidden on the next tick
        self.retry = 0  # frames to wait after a failed upload
        self.first_wait = None  # frames waited for the very first image to load
        self.visible = False
        self.shown = [False, False]

    def set_sort(self, sort):
        """Move the layer in the draw order (the placement preview flips sides)."""
        if sort * 2 != self.sort:
            self.sort = sort * 2
            self._order()

    def set_width(self, width):
        """Physical width in metres (the panel's can change with the settings page)."""
        self.mpp = width / ui.W  # metres per logical pixel, for top anchoring
        for h in self.handles:
            self.ov.setOverlayWidthInMeters(h, width)

    def upload(self, img):
        self.pending = img

    def _load(self, i, img):
        """Write img to twin i's file and load it; on failure keep it for a retry."""
        try:
            self.serial += 1
            # The pid keeps names unique across runs too: SteamVR's by-path cache outlives us.
            path = os.path.join(RUN_DIR, f"{self.key}.{os.getpid()}.{self.serial}.png")
            img.save(path, compress_level=1)
            self.files.append(path)
            while len(self.files) > self.KEEP_FILES:
                os.remove(self.files.pop(0))
            self.ov.setOverlayFromFile(self.handles[i], path)
            self.sizes[i] = img.size
            return True
        except (openvr.error_code.OverlayError, OSError) as e:
            now = time.monotonic()
            if now - Layer._last_error > 60:
                Layer._last_error = now
                print(f"layer: loading {self.key} failed ({e!r}), retrying", file=sys.stderr, flush=True)
            if self.pending is None:
                self.pending = img
            self.loading, self.retry = None, self.RETRY_FRAMES
            return False

    def offset(self, i):
        """Twin i's shift from the frame, in metres along the panel's y (top anchoring)."""
        if not self.anchor_top or not self.sizes[i]:
            return 0.0
        return (ui.H_OPEN - self.sizes[i][1] / ui.SCALE) / 2 * self.mpp

    @property
    def front_height(self):
        """Logical height of the image being shown, or None before the first one."""
        size = self.sizes[self.front]
        return size[1] / ui.SCALE if size else None

    def place(self, frame, alpha, visible, device=None):
        """Call once per frame, before tick(). frame: a 3x4 pose for the layer's centre (for an
        anchored layer, the centre an H_OPEN image would have): in the standing space, or with
        `device`, relative to that tracked device (Hands.attach), so it rides the controller."""
        self.visible = visible and self.loaded
        if visible:
            for i, h in enumerate(self.handles):
                m = mul(frame, translation(0, self.offset(i), 0)) if self.anchor_top else frame
                if device is None:
                    self.ov.setOverlayTransformAbsolute(h, openvr.TrackingUniverseStanding, to_hmd(m))
                else:
                    self.ov.setOverlayTransformTrackedDeviceRelative(h, device, to_hmd(m))
                self.ov.setOverlayAlpha(h, alpha)

    def _order(self):
        self.ov.setOverlaySortOrder(self.handles[self.front], self.sort + 1)
        self.ov.setOverlaySortOrder(self.handles[1 - self.front], self.sort)

    def _swap(self):
        self.retiring, self.front = self.front, 1 - self.front
        self._order()

    def _ready(self, i):
        """Twin i has a texture (False while a SetOverlayFromFile load is in flight)."""
        try:
            self.ov.getOverlayTextureSize(self.handles[i])
            return True
        except openvr.error_code.OverlayError:
            return False

    def tick(self):
        back = 1 - self.front
        if self.first_wait is not None:
            # The first image went straight to the front; show the layer only once it has
            # loaded (or SWAP_MAX frames on), so it doesn't appear blank or half-drawn.
            self.first_wait += 1
            if self.first_wait >= self.SWAP_MAX or (self.first_wait >= self.SWAP_MIN and self._ready(self.front)):
                self.first_wait, self.loaded = None, True
        elif self.retiring is not None:
            self.retiring = None
        elif self.loading is not None:
            waited, _ = self.loading
            if not self.visible or waited >= self.SWAP_MAX or (waited >= self.SWAP_MIN and self._ready(back)):
                self.loading = None
                self._swap()
            else:
                self.loading = (waited + 1, None)
        elif self.retry > 0:
            self.retry -= 1
        elif self.pending is not None:
            img, self.pending = self.pending, None
            if not self.loaded:  # first image: nothing to flicker against, but wait for it
                if self._load(self.front, img):
                    self.first_wait = 0
            elif self._load(back, img):
                self.loading = (0, img)
        want = [False, False]
        if self.visible:
            want[self.front] = True
            if self.retiring is not None:
                want[self.retiring] = True
            if self.loading is not None:  # loads underneath the front
                want[1 - self.front] = True
        for i, h in enumerate(self.handles):
            if want[i] != self.shown[i]:
                (self.ov.showOverlay if want[i] else self.ov.hideOverlay)(h)
                self.shown[i] = want[i]

    def destroy(self):
        for h in self.handles:
            self.ov.destroyOverlay(h)


class RoomView:
    """Whether Room View is showing.

    With no game running, SteamVR keeps it in camera/enableConstructRoomView (absent when off).
    In a game (a scene app, Steam Link's vrlink included) that setting stays as it was while
    the dashboard and the mailbox turn Room View on and off over the game, so the compositor's
    own log is read instead: its system layer logs "room view for scene apps was enabled" and
    "room view was turned off". The log is one file per SteamVR run; on opening, the last such
    line in it gives the state so far."""

    ON, OFF = "room view for scene apps was enabled", "room view was turned off"

    def __init__(self, settings, compositor, log_path):
        self.settings, self.compositor = settings, compositor
        self.path = os.path.expanduser(log_path)
        self.in_game = False  # the log's state, from its latest line
        self.log, self.ino = None, None
        self._open()

    def _open(self):
        """(Re)open the log and read it through: it only covers this SteamVR run."""
        self.in_game = False
        if self.log is not None:
            self.log.close()
        try:
            self.log = open(self.path, errors="replace")
            self.ino = os.fstat(self.log.fileno()).st_ino
        except OSError:
            self.log = None
            return
        self._read()

    def _read(self):
        for line in self.log.readlines():
            if self.ON in line:
                self.in_game = True
            elif self.OFF in line:
                self.in_game = False

    def _setting(self):
        try:
            return bool(self.settings.getBool("camera", "enableConstructRoomView"))
        except openvr.error_code.OpenVRError:
            return False

    def _game(self):
        try:
            return bool(self.compositor.getCurrentSceneFocusProcess())
        except (AttributeError, openvr.error_code.OpenVRError):
            return False

    def shown(self):
        """Call a few times a second: it also follows the log."""
        try:
            if self.log is None or os.stat(self.path).st_ino != self.ino:
                self._open()  # a new SteamVR run starts a new log
        except OSError:
            pass
        if self.log is not None:
            self._read()
        return self.in_game if self._game() else self._setting()


class Mailbox:
    """SteamVR's own message mailboxes (vrclient's IVRMailbox_001), the route its dashboard
    uses for Room View. Undocumented, so it's opened once and checked: ok is False when the
    interface isn't there or registering fails, and callers grey out what depends on it.

    The vtable layout (0 register, 1 unregister, 2 send) was found by probing SteamVR 2.18.4
    on the Steam Frame: register(name, &handle) and send(handle, mailbox, json) return 0."""

    TESTED_RUNTIME = "2.18.4"
    _REGISTER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64))
    _SEND = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p)

    def __init__(self, system):
        self.ok, self.reason = False, ""
        try:
            runtime = system.getRuntimeVersion()
        except (AttributeError, openvr.error_code.OpenVRError):
            runtime = "unknown"
        try:
            lib = ctypes.CDLL(OPENVR_LIB)
            lib.VR_GetGenericInterface.restype = ctypes.c_void_p
            lib.VR_GetGenericInterface.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
            err = ctypes.c_int(0)
            self.obj = lib.VR_GetGenericInterface(b"IVRMailbox_001", ctypes.byref(err))
            if not self.obj:
                raise OSError(f"no IVRMailbox_001 (error {err.value})")
            vtable = ctypes.cast(self.obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            self._send = self._SEND(vtable[2])
            self.handle = ctypes.c_uint64(0)
            r = self._REGISTER(vtable[0])(self.obj, f"cuff_{os.getpid()}".encode(), ctypes.byref(self.handle))
            if r != 0:
                raise OSError(f"register returned {r}")
            self.ok = True
        except OSError as e:
            self.reason = str(e)
        note = "" if runtime == self.TESTED_RUNTIME else f" (tested on {self.TESTED_RUNTIME})"
        print(f"mailbox: SteamVR {runtime}{note}: {'ok' if self.ok else 'unavailable, ' + self.reason}",
              file=sys.stdout if self.ok else sys.stderr, flush=True)

    def send(self, mailbox, message):
        """True if SteamVR accepted it."""
        if not self.ok:
            return False
        r = self._send(self.obj, self.handle.value, mailbox.encode(), json.dumps(message).encode())
        if r != 0:
            print(f"mailbox: send to {mailbox} returned {r}", file=sys.stderr, flush=True)
        return r == 0
