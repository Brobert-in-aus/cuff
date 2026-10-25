"""Cuff: a standalone OpenVR overlay on the left wrist.

It sits on the back of the left wrist (an offset from the left hand's tip pose) and fades in
as it turns to face the HMD (wrist.py). The status bar and quick-action row are fixed;
below them each charm is a tab. A small sphere marks a poke point 60 mm ahead of the right
controller's tip; touching the panel with it presses buttons.

  python -m cuff                         run until SteamVR quits (the systemd service restarts it)
  python -m cuff --debug                 also print poses, alpha and touch depth twice a second
  python -m cuff render X.png [ID|none]  draw a charm's page (none: collapsed) without SteamVR
"""
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

from . import charm as charms
from . import config, ui
from . import chime
from .dictation import Dictation
from .quick import Quick
from .pc import Pc
from .services import Services

STATE_FILE = os.path.join(config.CACHE_DIR, "state.json")
SCREENSHOT_DIR = os.path.expanduser(config.PROFILE["screenshot_dir"])
SCREENSHOT_DELAY_S = 3.0

TIP_DIAMETER = 0.012
# The alert glows: the bright core is the touch sphere's size, the halo around it fades out.
ORB_WIDTH = TIP_DIAMETER / 0.42  # the core twice the touch sphere's size
ORB_S, ORB_PULSE_S, ORB_MAX, ORB_SPACING = 8.0, 1.2, 4, 0.036
CONTROLLER_BODY = 0.04  # the controller's middle, this far back from its tip pose (+Z)
ORB_BEHIND = 0.07  # the glows' distance past the controller's middle, away from the head

GHOST_COLOUR, GHOST_ALPHA = (52, 132, 255, 255), 0.2  # the placement preview: flat blue, 20 %
PANEL_DIM = 0.5  # the panel's opacity while the preview is behind it, so the preview shows through
GHOST_SWITCH_M = 0.005  # how much nearer or farther the preview must be to switch sides
# Draw order (each layer takes two slots, for its double buffer): the preview first when
# it's behind the panel, else last.
SORT_GHOST_BEHIND, SORT_PANEL, SORT_MARQUEE, SORT_TIP, SORT_GHOST_FRONT = 0, 1, 2, 3, 4
PASSTHROUGH_CONFIRM_S = 4.0
POWER_EVERY_S = 2.0  # how often the headset's power state is read for the charging alerts
POWER_SETTLE_S = 5.0  # a change in it must hold this long to alert (so blips don't)
FULL_PCT = 95  # at or above this, charging stopping is the battery being full, not a weak pack
BATTERY_REARM = 5  # percent above a threshold before its low battery alert can fire again  # SteamVR's Room View setting must follow a press within this


BUDGET_S = 0.05  # a redraw slower than this counts against the charm on show
SLOW_STREAK, SLOW_PAUSE_S = 3, 1.0  # after this many slow redraws in a row, redraw at most once a second


class Renderer(threading.Thread):
    """Redraws the panel off the overlay loop whenever something changes.

    Charm code never runs on the overlay loop: this thread also works out the open charm's
    hit boxes and scrolling line, and presses go to the Presses worker. A charm whose page
    keeps taking over BUDGET_S to draw is logged and its redraws are spaced out."""

    def __init__(self, stt, quick, tabs, services):
        super().__init__(daemon=True, name="render")
        self.stt, self.quick, self.tabs, self.services = stt, quick, tabs, services
        self.page = load_page(tabs)  # the open charm's id, or None with every tab closed
        self.tab_page = ui.tab_page_of(self.tab_list(), self.page)  # which tabs the row shows
        self.pressed = None
        self.status = None  # set by the overlay loop
        self.dirty = threading.Event()
        self.frame = None  # latest PIL image, picked up by the overlay loop
        self.hits = ui.buttons(self.tab_list(), None, self.tab_page)  # for the frame on show
        self.marquee = None  # (text, box) of the open charm's scrolling line, or None
        self.lock = threading.Lock()
        self.slow, self.slow_logged = 0, 0.0
        self.dirty.set()

    @property
    def charm(self):
        return self.tabs.get(self.page) if self.page else None

    def tab_list(self):
        return [(i, c.label) for i, c in self.tabs.items()]

    def run(self):
        while True:
            self.dirty.wait()
            self.dirty.clear()
            charm, tabs, tab_page = self.charm, self.tab_list(), self.tab_page
            started = time.monotonic()
            badges = {}
            for charm_id, c in self.tabs.items():
                c.ctx.shown = c is charm
                badge = charms.call(c, "badge")
                if badge and badge is not charms.FAILED:
                    badges[charm_id] = badge
            img = ui.render(self.status, self.quick.state, self.stt.state, self.pressed, tabs,
                            self.page, charm, title=False, tab_page=tab_page,
                            badges=badges)
            hits = ui.buttons(tabs, charm, tab_page)
            spec = charms.call(charm, "marquee") if charm is not None else None
            took = time.monotonic() - started
            with self.lock:
                self.frame, self.hits = img, hits
                self.marquee = None if spec is charms.FAILED or not spec else (spec[0], tuple(spec[1]))
            self._budget(charm, took)

    def _budget(self, charm, took):
        if took <= BUDGET_S or charm is None:
            self.slow = 0
            return
        self.slow += 1
        now = time.monotonic()
        if now - self.slow_logged > 60:
            self.slow_logged = now
            print(f"charm {charm.ctx.id}: redraw took {took * 1000:.0f} ms (budget {BUDGET_S * 1000:.0f} ms)",
                  file=sys.stderr, flush=True)
        if self.slow >= SLOW_STREAK:
            time.sleep(SLOW_PAUSE_S)

    def take(self):
        with self.lock:
            frame, self.frame = self.frame, None
        return frame


class Presses(threading.Thread):
    """Runs charms' press() one at a time, so a slow one can't stall the overlay loop."""

    def __init__(self, renderer):
        super().__init__(daemon=True, name="presses")
        self.renderer, self.queue = renderer, queue.Queue()

    def press(self, charm, button_id):
        self.queue.put((charm, button_id))

    def run(self):
        while True:
            charm, button_id = self.queue.get()
            charms.call(charm, "press", button_id)
            self.renderer.dirty.set()


class PowerWatch:
    """Notices the headset's charging stopping, from the status bar's readings: charging to
    plugged-in-but-draining (the battery pack is flat or too weak), or from either to on
    battery (the external power is gone). A state must hold for POWER_SETTLE_S to count, so a
    blip doesn't alert; it's read every POWER_EVERY_S. At FULL_PCT or more, charging stopping is
    the battery topping up, so it doesn't alert. Settings > Alerts switches it off."""

    def __init__(self):
        self.state, self.pending = None, None  # the settled state; (new state, since)

    def update(self, status, now):
        """The alert's text when charging has just stopped, else None."""
        head = next(((lv, st) for lb, lv, st in status["batteries"] if lb == "H"), None)
        if head is None:
            return None
        level, state = head
        if self.state is None:
            self.state = state
            return None
        if state == self.state:
            self.pending = None
            return None
        if self.pending is None or self.pending[0] != state:
            self.pending = (state, now)
            return None
        if now - self.pending[1] < POWER_SETTLE_S:
            return None
        was, self.state, self.pending = self.state, state, None
        pct = f" ({round(level * 100)}%)" if level is not None else ""
        if was == "charging" and state == "plugged":
            if level is not None and level * 100 >= FULL_PCT:
                return None  # topped up: a full battery stops charging, and that's fine
            return f"Headset not charging{pct}"
        if was in ("charging", "plugged") and state == "battery":
            return f"External power lost{pct}"
        return None


class BatteryWatch:
    """Low battery alerts, from the status bar's readings: an alert when a device drops to one
    of its levels while not charging; it re-arms once the level is back BATTERY_REARM above
    it, or charging. Each device's switch and two levels are in Settings > Alerts > Battery
    (services.Prefs): reaching the higher level glows orange, the lower one red."""

    NAMES = {"H": "Headset", "L": "Left controller", "R": "Right controller", "E": "Headphones"}
    KIND = {"H": "headset", "L": "controllers", "R": "controllers", "E": "headphones"}

    def __init__(self, prefs):
        self.prefs = prefs
        self.fired = {}  # device label -> levels already alerted

    def update(self, status):
        """[(text, glow)] for the devices that just reached a level."""
        out = []
        for label, level, state in status["batteries"]:
            if level is None or label not in self.NAMES:
                continue
            pct = round(level * 100)
            fired = self.fired.setdefault(label, set())
            if state == "charging":
                fired.clear()
                continue
            fired.difference_update({t for t in fired if pct >= t + BATTERY_REARM})
            device = self.prefs.device(self.KIND[label])
            levels = sorted((lv for lv in device["levels"] if lv), reverse=True)
            crossed = [lv for lv in levels if pct <= lv and lv not in fired]
            if not crossed:
                continue
            fired.update(crossed)  # a jump past both counts as both
            if not device["on"]:
                continue
            low = min(crossed)
            red = len(levels) > 1 and low == levels[-1]
            out.append((f"{self.NAMES[label]} battery {pct}%" + (": nearly flat" if red else ""),
                        "red" if red else "orange"))
        return out


def load_page(tabs):
    """The tab that was open last time, so the panel reopens on it."""
    try:
        with open(STATE_FILE) as f:
            page = json.load(f).get("page")
    except (OSError, ValueError):
        page = None
    if page is None or page in tabs:
        return page
    return next(iter(tabs), None)


def save_page(page):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"page": page}, f)
    except OSError as e:
        print(f"state: {e!r}", file=sys.stderr, flush=True)


def wait_for_steamvr():
    # VR_Init would start SteamVR itself; only attach to one that's already running.
    while subprocess.run(["pgrep", "-x", "vrserver"], capture_output=True).returncode != 0:
        time.sleep(5)


def main(debug=False):
    wait_for_steamvr()
    from . import vr
    from .status import headset_power, read_status
    from .vr import (Hands, Layer, battery_icon, billboard, from_hmd, glow_texture, mul, openvr, origin, quat_to_mat, tip_texture,
                     to_local, translation)
    from .placement import PLACEMENT
    from .wrist import view_alpha

    system = openvr.init(openvr.VRApplication_Overlay)
    ov = openvr.VROverlay()
    os.makedirs(config.RUN_DIR, exist_ok=True)
    hands = Hands(system)
    print(f"cuff: OpenVR up ({vr.OPENVR_LIB})", flush=True)

    renderer = None

    def changed():
        if renderer:
            renderer.dirty.set()

    pc = Pc()
    pc.start()
    services = Services(changed, pc)
    pc.notify = lambda text, haptic: services.toasts.push("pairing", text, haptic)
    pc.pin = lambda text: services.toasts.pin("pairing", text) if text else services.toasts.unpin("pairing")
    stt, quick = Dictation(changed, pc), Quick(changed)
    tabs = charms.load(changed, services)
    renderer = Renderer(stt, quick, tabs, services)
    presses = Presses(renderer)
    presses.start()
    quick.start()
    for c in tabs.values():
        charms.call(c, "start")
    renderer.start()

    # The panel's size and placement (the settings page's Position sliders): `width` in
    # metres, `mpp` metres per logical pixel, `watch_offset` from the left tip pose.
    # Re-read whenever PLACEMENT.version changes (an Apply).
    width = PLACEMENT.applied["size"]
    mpp = width / ui.W
    watch_offset = PLACEMENT.offset(PLACEMENT.applied)
    geometry_version = PLACEMENT.version
    # The hand wearing the panel, and the one touching it (Settings > Place > Hand).
    wear = PLACEMENT.applied["hand"]
    poker = "right" if wear == "left" else "left"

    # Sort orders, drawn first to last: the panel, its scrolling line, the touch point. The
    # Frame's compositor depth-tests overlays against each other (measured with screenshots):
    # wherever two overlap, the nearer one hides the farther one, however translucent it is,
    # and blending only happens when the see-through one is nearer *and* drawn later. A
    # farther overlay drawn first does show through a nearer translucent one.
    panel = Layer(ov, "cuff.panel", "Cuff", width, sort=SORT_PANEL, anchor_top=True)
    # While the settings page has unapplied placement changes, a flat blue panel shape shows
    # where the panel will go; the panel itself stays put until Apply. So it always shows:
    # nearer than the panel, it's drawn last and tints it; farther, it's drawn first and the
    # panel fades to PANEL_DIM so the preview shows through it.
    ghost = Layer(ov, "cuff.ghost", "Cuff placement preview", width, sort=SORT_GHOST_FRONT, anchor_top=True)
    ghost.upload(ui.silhouette(GHOST_COLOUR))
    ghost_values, ghost_offset, ghost_behind = None, None, False
    marquees = {}  # box -> its Layer, one per box used

    def marquee_offset(box):
        """A scrolling line's centre in the panel's frame, for the current width."""
        x0, y0, x1, y1 = box
        return quat_to_mat((0, 0, 0, 1), (((x0 + x1) / 2 / ui.W - 0.5) * width,
                                          (0.5 - (y0 + y1) / 2 / ui.H) * mpp * ui.H_OPEN, 0.0005))

    def marquee_layer(box):
        if box not in marquees:
            x0, _, x1, _ = box
            marquees[box] = Layer(ov, f"cuff.marquee.{len(marquees)}", "Cuff scrolling text",
                                  (x1 - x0) / ui.W * width, sort=SORT_MARQUEE)
        return marquees[box]

    marquee, marquee_pos, marquee_at = None, None, 0.0
    tip = Layer(ov, "cuff.tip", "Cuff touch point", TIP_DIAMETER, sort=SORT_TIP)
    # A toast's own row, just above the panel's top edge; and for an alert while the panel
    # isn't on show, a row of glows on the far side of the controller (below).
    toast = Layer(ov, "cuff.toast", "Cuff notification", width, sort=SORT_MARQUEE)
    toast_text = None
    # Each alert while the panel is turned away adds a glow to a row on the far side of the
    # controller (white, or a battery's orange or red with its icon), until the panel is looked
    # at or ORB_S passes.
    orbs = [Layer(ov, f"cuff.orb.{i}", "Cuff notification", ORB_WIDTH, sort=SORT_TIP) for i in range(ORB_MAX)]
    orb_looks = [None] * ORB_MAX  # what each orb layer shows: (glow, icon)
    glow_cache = {}
    lit = []  # [(glow, icon, until)], oldest first
    seen_alerts = 0
    BATTERY_ICON = battery_icon()

    power, batteries = PowerWatch(), BatteryWatch(services.prefs)
    tips = {"idle": tip_texture((235, 235, 245)), "hover": tip_texture((150, 230, 255)),
            "press": tip_texture((30, 215, 96))}
    tip_look = None

    poke_offset = quat_to_mat((0, 0, 0, 1), (0, 0, -vr.POKE_REACH))
    poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
    standing = openvr.TrackingUniverseStanding
    touching = hovering = armed = False
    last_debug = last_status = last_error = last_roomview = last_window = last_frames = last_power = 0.0
    settings = openvr.VRSettings()
    compositor, apps = openvr.VRCompositor(), openvr.VRApplications()

    roomview = vr.RoomView(settings, compositor, config.PROFILE["compositor_log"])
    roomview_on = roomview.shown

    # Passthrough goes through SteamVR's undocumented mailbox, so it's checked twice: the
    # mailbox must open at startup, and after a press SteamVR's own Room View setting must
    # follow within PASSTHROUGH_CONFIRM_S. Either failing greys the button out for this run.
    mailbox = vr.Mailbox(system)
    quick.set_passthrough(roomview_on(), ok=mailbox.ok)
    passthrough_wanted = [None, 0.0]  # (state asked for, deadline) while a press is unconfirmed

    def toggle_passthrough():
        if not quick.state["passthrough_ok"]:
            return
        want = not roomview_on()
        # Both messages, as the dashboard sends: plain is Room View itself, _forsceneapps
        # shows it over a running game.
        sent = all(mailbox.send("vrcompositor_systemlayer", {"type": kind, "enabled": want})
                   for kind in ("set_camera_roomview_enabled", "set_camera_roomview_enabled_forsceneapps"))
        if not sent:
            quick.set_passthrough(roomview_on(), ok=False)
            return
        passthrough_wanted[:] = [want, time.monotonic() + PASSTHROUGH_CONFIRM_S]
        quick.set_passthrough(want)  # answer at once; the setting confirms

    shots = openvr.VRScreenshots()
    shot_at = [None]  # when the pending screenshot is due

    def take_screenshot():
        """A stereo screenshot via OpenVR (vrcmd --screenshot does nothing on the Frame), taken
        SCREENSHOT_DELAY_S after the tap so the panel can be turned out of view. The wearing
        hand buzzes as it's taken."""
        hands.pulse(wear)
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        base = os.path.join(SCREENSHOT_DIR, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shots.takeStereoScreenshot(base + "-preview", base + "-vr")
            print(f"screenshot: {base}-vr.png", flush=True)
        except openvr.error_code.OpenVRError as e:
            print(f"screenshot: {e!r}", file=sys.stderr, flush=True)

    keyboard_open = [False]

    def window_overlay():
        """The window the keyboard should type into: a visible gamescope window (games, the
        desktop), else a visible Steam menu, else None. Like the "show keyboard" button SteamVR
        draws under them, the keyboard then belongs to that overlay, and its owner types."""
        for key in [f"valve.steam.desktopgame.{n}" for n in range(8)] + ["valve.steam.gamepadui.main"]:
            try:
                h = ov.findOverlay(key)
            except openvr.error_code.OverlayError:
                continue
            if ov.isOverlayVisible(h):
                return h
        return None

    def toggle_keyboard():
        if keyboard_open[0]:
            ov.hideKeyboard()
            return
        target = window_overlay()
        if target is None:  # the button is greyed out then; nothing to type into
            return
        ov.showKeyboardForOverlay(target, openvr.k_EGamepadTextInputModeNormal,
                                  openvr.k_EGamepadTextInputLineModeSingleLine,
                                  openvr.KeyboardFlag_Minimal, "", 1024, "", 0)

    def press(name, px, py):
        print(f"press {name} at ({px:.0f}, {py:.0f}) on {renderer.page}", flush=True)
        hands.pulse(poker)
        if name == "q:dictation":
            stt.toggle()
        elif name == "q:screenshot":
            shot_at[0] = time.monotonic() + SCREENSHOT_DELAY_S
        elif name == "q:keyboard":
            toggle_keyboard()
        elif name == "q:passthrough":
            toggle_passthrough()
        elif name.startswith("q:"):
            quick.act(name[2:])
        elif name.startswith("tab:"):
            # Tapping the open tab closes it; any other tab opens that charm.
            renderer.page = None if renderer.page == name[4:] else name[4:]
            save_page(renderer.page)
        elif name in ("tabs:prev", "tabs:next"):
            step = 1 if name == "tabs:next" else -1
            renderer.tab_page = min(max(renderer.tab_page + step, 0), ui.tab_pages(renderer.tab_list()) - 1)
        elif name.startswith("c:") and renderer.charm is not None:
            presses.press(renderer.charm, name[2:])
        # After the state change, so the redraw can't pick up the old page.
        renderer.pressed = name
        renderer.dirty.set()

    try:
        while True:
            try:
                try:
                    ov.waitFrameSync(100)
                except openvr.error_code.OverlayError:
                    time.sleep(1 / 90)
                ev = openvr.VREvent_t()
                while system.pollNextEvent(ev):
                    if ev.eventType in (openvr.VREvent_KeyboardOpened_Global, openvr.VREvent_KeyboardClosed_Global):
                        # Whoever opened it (us, a window's control bar, Steam), so the button
                        # always knows the real state.
                        keyboard_open[0] = ev.eventType == openvr.VREvent_KeyboardOpened_Global
                        quick.set_keyboard(keyboard_open[0])
                    if ev.eventType == openvr.VREvent_CameraSettingsHaveChanged:
                        last_roomview = 0.0  # re-read the passthrough state now
                    if ev.eventType == openvr.VREvent_Quit:
                        system.acknowledgeQuit_Exiting()
                        return

                # Status bar: re-read every 10 s (2 s while a battery hasn't reported yet, as the
                # controllers often haven't at startup) and at the minute; redraw only on a change.
                now = time.monotonic()
                if shot_at[0] is not None:
                    if now >= shot_at[0]:
                        shot_at[0] = None
                        quick.set_countdown(None)
                        take_screenshot()
                    else:
                        quick.set_countdown(math.ceil(shot_at[0] - now))
                want, deadline = passthrough_wanted
                if now - last_roomview > (0.25 if want is not None else 2):
                    last_roomview = now
                    shown = roomview_on()
                    if want is not None and shown == want:
                        passthrough_wanted[0] = None  # confirmed
                    elif want is not None and now > deadline:
                        passthrough_wanted[0] = None
                        print(f"passthrough: SteamVR didn't follow the mailbox message within "
                              f"{PASSTHROUGH_CONFIRM_S:.0f} s; greying the button out", file=sys.stderr, flush=True)
                        quick.set_passthrough(shown, ok=False)
                    elif want is None:
                        if shown != quick.state["passthrough"]:
                            print(f"passthrough: SteamVR reports {'on' if shown else 'off'}", flush=True)
                        quick.set_passthrough(shown)
                missing = renderer.status is None or any(lv is None for _, lv, _ in renderer.status["batteries"])
                # Earbuds report their battery only now and then, so take a new level at once.
                earbuds = None if renderer.status is None else next(
                    (lv for label, lv, _ in renderer.status["batteries"] if label == "E"), None)
                if (now - last_status > (2 if missing else 10) or quick.state.get("earbuds") != earbuds
                        or not renderer.status["time"].startswith(time.strftime("%-I:%M "))):
                    last_status = now
                    status = read_status(system, hands, quick)
                    if status != renderer.status:
                        renderer.status = status
                        renderer.dirty.set()
                    for text, glow in batteries.update(status):
                        print(f"battery: {text}", flush=True)
                        services.toasts.push(f"battery:{text.split(' battery')[0]}", text, haptic=True,
                                             glow=glow, icon=BATTERY_ICON)

                # The headset's power on its own, every POWER_EVERY_S (a few sysfs reads), so a pack
                # running flat or a cable pulled alerts within seconds, not at the next status read.
                if now - last_power > POWER_EVERY_S:
                    last_power = now
                    alert = power.update({"batteries": [("H", *headset_power(system))]}, now)
                    if alert and services.prefs.get("power"):
                        print(f"power: {alert}", flush=True)
                        services.toasts.push("cuff", alert, haptic=True, glow="orange", icon=BATTERY_ICON)

                # The keyboard button is greyed out while there's no window for it to type into.
                if now - last_window > 0.5:
                    last_window = now
                    quick.set_keyboard_target(window_overlay() is not None)

                # A settings Apply: the panel's new size and place.
                if PLACEMENT.version != geometry_version:
                    geometry_version = PLACEMENT.version
                    width = PLACEMENT.applied["size"]
                    mpp = width / ui.W
                    watch_offset = PLACEMENT.offset(PLACEMENT.applied)
                    wear = PLACEMENT.applied["hand"]
                    poker = "right" if wear == "left" else "left"
                    panel.set_width(width)
                    toast.set_width(width)
                    for box, layer in marquees.items():
                        layer.set_width((box[2] - box[0]) / ui.W * width)
                    print(f"placement: applied {PLACEMENT.applied}", flush=True)

                # Frame statistics for charms (Performance), once a second.
                if now - last_frames >= 1.0:
                    last_frames = now
                    try:
                        services.frames.sample(compositor, system, openvr, apps)
                    except (openvr.error_code.OpenVRError, AttributeError, TypeError) as e:
                        if now - last_error > 60:
                            last_error = now
                            print(f"frame stats: {e!r}", file=sys.stderr, flush=True)
                # A toast's buzz, on the wrist the panel is on.
                for _ in range(services.toasts.due_buzz(now)):
                    hands.pulse(wear)

                frame = renderer.take()
                if frame is not None:
                    panel.upload(frame)

                system.getDeviceToAbsoluteTrackingPose(standing, 0.011, poses)
                hand = hands.poses(poses)
                poke = origin(mul(hand[poker], poke_offset)) if poker in hand else None
                hmd_pose = poses[openvr.k_unTrackedDeviceIndex_Hmd]
                hmd = from_hmd(hmd_pose.mDeviceToAbsoluteTracking) if hmd_pose.bPoseIsValid else None

                alpha, world, dev, rel = 0.0, None, None, None
                # Hidden while SteamVR's keyboard is open: other apps' controller poses are blanked
                # then (see Hands.poses), so the panel can neither follow the wrist nor be touched.
                if hmd and wear in hand and not keyboard_open[0]:
                    world = mul(hand[wear], watch_offset)
                    alpha = view_alpha(world, hmd)
                    # The layers on the wrist ride the controller (the compositor moves them with
                    # its own latest pose); world is still what the fade and touch work from.
                    dev, rel = hands.attach(wear, watch_offset)

                # The placement preview: the pending values on the same wrist, while the
                # settings tab is open with changes not yet applied. It's drawn before or after
                # the panel by which is nearer the HMD (with hysteresis, so it can't flicker at
                # the crossing); while it's behind, the panel fades so it shows through.
                pending = PLACEMENT.pending
                previewing = renderer.page == ui.SETTINGS_TAB and pending != PLACEMENT.applied
                if previewing and pending is not ghost_values:
                    ghost_values = pending
                    ghost_offset = PLACEMENT.offset(pending)
                    ghost.set_width(pending["size"])
                ghost_on = previewing and world is not None and alpha > 0
                ghost_hand = pending.get("hand", wear)  # the preview shows on the wrist it'll be on
                ghost_on = ghost_on and ghost_hand in hand
                ghost_world = mul(hand[ghost_hand], ghost_offset) if ghost_on else None
                if ghost_on:
                    eye = origin(hmd)
                    gap = (math.dist(eye, origin(ghost_world)) - math.dist(eye, origin(world)))
                    if ghost_behind and gap < -GHOST_SWITCH_M:
                        ghost_behind = False
                    elif not ghost_behind and gap > GHOST_SWITCH_M:
                        ghost_behind = True
                    ghost.set_sort(SORT_GHOST_BEHIND if ghost_behind else SORT_GHOST_FRONT)
                panel_alpha = alpha * PANEL_DIM if ghost_on and ghost_behind else alpha
                panel.place(rel, panel_alpha, alpha > 0, device=dev)
                panel.tick()
                ghost_dev, ghost_rel = hands.attach(ghost_hand, ghost_offset) if ghost_on else (None, None)
                ghost.place(ghost_rel, GHOST_ALPHA, ghost_on, device=ghost_dev)
                ghost.tick()

                # The open charm's scrolling line: its own layer, scrolled at up to 20 fps while visible.
                spec = renderer.marquee  # worked out with the last redraw, on the render thread
                if spec:
                    text, box = spec
                    layer = marquee_layer(box)
                    if marquee is None or marquee.title != text or marquee.box != box:
                        marquee, marquee_pos = ui.Marquee(text, box), None
                    if alpha > 0 and now - marquee_at >= 0.05:
                        pos = marquee.offset(now)
                        if pos != marquee_pos:
                            layer.upload(marquee.frame(pos))
                            marquee_pos, marquee_at = pos, now
                for box, layer in marquees.items():
                    on = alpha > 0 and spec is not None and box == spec[1]
                    layer.place(mul(rel, marquee_offset(box)) if rel else None, panel_alpha, on, device=dev)
                    layer.tick()

                # Touch: the poke point in panel space (x right, y up, z out of the face, metres).
                look, local = "idle", None
                if world and poke and alpha >= vr.TOUCH_MIN_ALPHA:
                    # The panel as shown: its current height, shifted down from the frame when collapsed.
                    cur_h = panel.front_height or ui.H_OPEN
                    height_m = cur_h * mpp
                    local = to_local(mul(world, translation(0, panel.offset(panel.front), 0)), poke)
                    inside = abs(local[0]) <= width / 2 and abs(local[1]) <= height_m / 2
                    depth = local[2]
                    # A press needs the sphere to come from the front: armed while it is
                    # inside the edges and in front of the press depth, so sliding in from
                    # the side or from behind the panel doesn't hit whatever is at the edge.
                    if touching and (not inside or depth > vr.TOUCH_RELEASE):
                        touching = False
                        renderer.pressed = None
                        renderer.dirty.set()
                    elif not touching and inside and armed and vr.TOUCH_BEHIND < depth < vr.TOUCH_PRESS:
                        touching, armed = True, False
                        px = (local[0] / width + 0.5) * ui.W
                        py = (0.5 - local[1] / height_m) * cur_h
                        with renderer.lock:
                            hits = renderer.hits
                        name = next((k for k, (x0, y0, x1, y1) in hits.items()
                                     if x0 <= px < x1 and y0 <= py < y1), None)
                        if name:
                            press(name, px, py)
                    armed = inside and vr.TOUCH_PRESS <= depth < vr.HOVER_EXIT
                    # Hover with hysteresis: enter within TOUCH_HOVER and inside the edges,
                    # leave only past HOVER_EXIT or EDGE_EXIT beyond them.
                    near = (abs(local[0]) <= width / 2 + vr.EDGE_EXIT
                            and abs(local[1]) <= height_m / 2 + vr.EDGE_EXIT and vr.TOUCH_BEHIND < depth)
                    if hovering:
                        hovering = near and depth < vr.HOVER_EXIT
                    else:
                        hovering = inside and vr.TOUCH_BEHIND < depth < vr.TOUCH_HOVER
                    look = "press" if touching else "hover" if hovering else "idle"
                else:
                    hovering = armed = False
                    if touching:
                        touching = False
                        renderer.pressed = None
                        renderer.dirty.set()

                tip_on = alpha > 0 and poke is not None
                if tip_on and look != tip_look:
                    tip.upload(tips[look])
                    tip_look = look
                tip.place(billboard(poke, origin(hmd)) if tip_on else None, alpha, tip_on)
                tip.tick()

                # Notifications: the toast's row above the panel, while it shows.
                current = services.toasts.current()
                if current and current[1] != toast_text:
                    toast_text = current[1]
                    toast.upload(ui.toast_image(toast_text))
                elif not current:
                    toast_text = None
                toast_on = bool(current) and alpha > 0
                above = (ui.H_OPEN / 2 + 3 + ui.TOAST_H / 2) * mpp  # its centre, up from the frame's
                toast.place(mul(rel, translation(0, above, 0)) if toast_on else None, alpha, toast_on, device=dev)
                toast.tick()
                # An alert: the chime, and a glow on the hand while the panel is turned away.
                alerts = list(services.toasts.alerts)
                fresh = min(len(alerts), services.toasts.alert_count - seen_alerts)
                new, seen_alerts = alerts[len(alerts) - fresh:], services.toasts.alert_count
                if new and services.prefs.get("sound"):
                    chime.play()  # once for a burst of alerts
                for glow, icon in new:
                    if alpha == 0 and services.prefs.get("orb"):
                        lit.append((glow, icon, now + ORB_S))
                if alpha > 0:
                    lit.clear()  # seen
                lit[:] = [o for o in lit if o[2] > now][-ORB_MAX:]
                # The row sits on the far side of the wearing hand's controller from the head: ORB_BEHIND past
                # its body along the line from the eyes, running sideways across the view.
                row = None
                if hmd is not None and wear in hand:
                    body = origin(mul(hand[wear], translation(0, 0, CONTROLLER_BODY)))
                    away = vr.norm(vr.sub(body, origin(hmd)))
                    side = vr.norm(vr.cross((0.0, 1.0, 0.0), away))
                    row = (tuple(b + a * ORB_BEHIND for b, a in zip(body, away)), side)
                for i, layer in enumerate(orbs):
                    if row is None or i >= len(lit):
                        layer.place(None, 0.0, False)
                        layer.tick()
                        continue
                    glow, icon, until = lit[i]
                    look = (glow, id(icon) if icon is not None else None)
                    if orb_looks[i] != look:
                        if look not in glow_cache:
                            glow_cache[look] = glow_texture(glow, icon)
                        layer.upload(glow_cache[look])
                        orb_looks[i] = look
                    centre, side = row
                    along = (i - (len(lit) - 1) / 2) * ORB_SPACING
                    pos = tuple(c + s * along for c, s in zip(centre, side))
                    pulse = 0.65 + 0.35 * math.sin((now + i * 0.3) * 2 * math.pi / ORB_PULSE_S) ** 2
                    layer.place(billboard(pos, origin(hmd)), pulse, True)
                    layer.tick()

                now = time.monotonic()
                if debug and now - last_debug > 0.5:
                    last_debug = now
                    fmt = lambda m: "(%.3f %.3f %.3f)" % origin(m) if m else "-"  # noqa: E731
                    loc = "(%.3f %.3f %.3f)" % local if local else "-"
                    print(f"hmd {fmt(hmd)} left {fmt(hand.get('left'))} right {fmt(hand.get('right'))} "
                          f"alpha {alpha:.2f} tip-in-panel {loc} {look}", flush=True)
            except openvr.error_code.OpenVRError as e:
                # A transient OpenVR failure skips this frame instead of taking the panel down.
                now = time.monotonic()
                if now - last_error > 60:
                    last_error = now
                    print(f"frame: {e!r}", file=sys.stderr, flush=True)
    finally:
        for layer in [tip, ghost, toast, *orbs] + list(marquees.values()) + [panel]:
            layer.destroy()
        openvr.shutdown()


def render_to(path, page=None):
    """Draw the panel with sample content, for checking the layout off-headset."""
    services = Services(lambda: None)
    tabs = charms.load(lambda: None, services)
    page = page or next(iter(tabs), None)
    page = None if page == "none" else page
    if page is not None and page not in tabs:
        sys.exit(f"render: no charm {page!r} (loaded: {', '.join(tabs) or 'none'})")
    for c in tabs.values():  # every charm's sample state, so the other tabs show their badges
        charms.call(c, "sample")
    stt = {"phase": "listening", "text": "", "configured": True}
    status = {"time": "7:52 pm", "batteries": [("H", 0.53, "plugged"), ("L", 0.86, "charging"), ("R", 0.15, "battery")]}
    quick = {"mic_muted": True, "passthrough": True, "stt_up": True}
    pressed = "q:recenter"
    if page:
        own = charms.call(tabs[page], "buttons")
        if own is not charms.FAILED and own:
            pressed = "c:" + next(iter(own))
    tab_list = [(i, c.label) for i, c in tabs.items()]
    badges = {i: b for i, c in tabs.items() if (b := charms.call(c, "badge")) and b is not charms.FAILED}
    ui.render(status, quick, stt, pressed, tab_list, page, tabs.get(page),
              tab_page=ui.tab_page_of(tab_list, page), badges=badges).save(path)


def cli():
    args = sys.argv[1:]
    if args[:1] == ["render"] and len(args) >= 2:
        render_to(args[1], *args[2:3])
        return
    try:
        main(debug="--debug" in args)
    except KeyboardInterrupt:
        pass
