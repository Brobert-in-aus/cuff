"""Wrist-watch style panel placement and glance fade for an OpenVR overlay.

Clean-room implementation (MIT). Plain Python, no numpy.

Conventions
-----------
* Pose: 3x4 row-major list [[r00, r01, r02, tx], [r10, r11, r12, ty],
  [r20, r21, r22, tz]]. Rotation columns are local X, Y, Z in the parent
  frame; column 3 is the origin. parent = R @ local + t. Metres.
* Left controller "tip" frame: origin at the front tip, -Z forward (pointing
  direction), +Y up out of the top of the controller, +X toward the user's
  right. The wrist lies behind the grip, along +Z and a little below (-Y).
  The back / outer side of the left wrist faces roughly -X and +Y.
* Overlay: visible face along its own +Z, image x along +X, image up along +Y.

Panel orientation in the tip frame
----------------------------------
Start from a "base" orientation and roll it about the forearm axis:

  base:  panel +X = tip -Z   (text runs along the forearm toward the hand)
         panel +Z = tip +Y   (face points straight up out of the controller)
         panel +Y = tip -X   (follows from Y = Z x X, right-handed)

  then:  rotate by WRIST_ROLL_DEG about the tip Z axis (the forearm axis),
         which swings the face normal from +Y toward -X, onto the back of the
         left wrist; then optionally tilt by PANEL_TILT_DEG about the panel's
         own X axis (the forearm direction) for fine trimming, and
         PANEL_YAW_DEG about the panel's own Z axis to spin the image.

Because panel X is along the forearm (toward the hand) and panel Z faces the
viewer, X x Y = Z forces panel Y to be "up" for the viewer whenever the user
looks at the face with the hand on their right, i.e. the classic left-wrist
watch glance with the forearm across the chest.
"""

import math

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Physical width of the panel. 0.11 m: a bit larger than a big smartwatch so
# text stays legible at ~35-45 cm reading distance, but still narrower than
# the back of a typical adult wrist-to-mid-forearm span, so it does not
# overhang onto the hand.
PANEL_WIDTH = 0.11

# Centre of the wrist (the bone axis) in the tip frame, metres.
# The spec says the wrist is 10-16 cm back along +Z and a few cm below.
# 0.13 m is the middle of that range; -0.03 m puts it slightly under the top
# of the controller, where the forearm axis passes when gripping the handle.
WRIST_CENTRE = (0.0, -0.03, 0.13)

# Approximate wrist radius measured toward its back/outer side. An adult
# wrist is ~5-6 cm across, so ~2.8 cm from the axis to the dorsal skin.
WRIST_RADIUS = 0.028

# Gap between skin and panel so the panel floats "just above" the skin and
# does not z-fight with a hand model. 5 mm.
SKIN_GAP = 0.005

# Roll of the face normal about the forearm, measured from tip +Y toward
# tip -X. 0 deg = face straight up out of the controller top, 90 deg = face
# straight out to the left. The spec says the back/outer side of the left
# wrist faces "roughly -X and +Y"; with a natural grip the thumb is on top,
# so the back of the hand is mostly outward (-X) and somewhat up. 60 deg
# gives a normal 30 deg above horizontal-outward: (-0.866, 0.5, 0).
WRIST_ROLL_DEG = 60.0

# Trims. Tilt pitches the face about the forearm direction (panel X) -
# positive turns the face toward panel +Y, so the top edge leans away from
# the viewer, and the panel moves round the wrist with it to stay off the
# skin. 30 deg was chosen in the headset: the top tipped away reads better.
# Yaw spins the image within its own plane.
PANEL_TILT_DEG = 30.0
PANEL_YAW_DEG = 0.0

# Glance fade thresholds on cos(angle between panel normal and the
# direction from the panel to the eyes).
#   FADE_START_COS = 0.30 -> ~72.5 deg off-axis: still essentially edge-on,
#                    so alpha is 0 for sideways/away poses.
#   FADE_FULL_COS  = 0.75 -> ~41 deg off-axis: a relaxed glance rarely gets
#                    closer than 20-35 deg to dead-on, so the panel is fully
#                    opaque well before it is square to the eyes.
# With the smoothstep ramp, alpha = 0.6 (the touch threshold) is reached at
# cos ~0.55 (~57 deg), leaving ~16 deg of rotation between "touchable" and
# "fully opaque" and ~15 deg of visible-but-not-touchable fade-in.
FADE_START_COS = 0.30
FADE_FULL_COS = 0.75

# ---------------------------------------------------------------------------
# Small maths helpers
# ---------------------------------------------------------------------------


def _norm(v):
    """Return v normalised; a zero vector is returned unchanged (as zeros)."""
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _axis_angle(axis, deg):
    """3x3 rotation (list of rows) for a right-handed turn of deg about axis."""
    x, y, z = _norm(axis)
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    t = 1.0 - c
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def _mat3_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _mat3_vec(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2]
                 for i in range(3))


def _from_columns(x, y, z):
    return [[x[0], y[0], z[0]], [x[1], y[1], z[1]], [x[2], y[2], z[2]]]


def _pose(rot, t):
    return [[rot[i][0], rot[i][1], rot[i][2], float(t[i])] for i in range(3)]


def _pose_mul(a, b):
    """Compose 3x4 poses: result maps b-local -> a-parent (a @ b)."""
    ra = [row[:3] for row in a]
    rb = [row[:3] for row in b]
    r = _mat3_mul(ra, rb)
    t = _mat3_vec(ra, (b[0][3], b[1][3], b[2][3]))
    return _pose(r, (t[0] + a[0][3], t[1] + a[1][3], t[2] + a[2][3]))


def _col(pose, j):
    return (pose[0][j], pose[1][j], pose[2][j])


def _smoothstep(e0, e1, x):
    if e1 == e0:
        return 1.0 if x >= e1 else 0.0
    t = (x - e0) / (e1 - e0)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# Derived placement
# ---------------------------------------------------------------------------

# Base orientation (columns = panel X, Y, Z in tip frame), see module doc.
_BASE_ROT = _from_columns((0.0, 0.0, -1.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0))


def _panel_rotation(roll=None, tilt=None, yaw=None):
    """The panel's rotation for these angles (degrees); the module constants by default."""
    roll = WRIST_ROLL_DEG if roll is None else roll
    tilt = PANEL_TILT_DEG if tilt is None else tilt
    yaw = PANEL_YAW_DEG if yaw is None else yaw
    r = _mat3_mul(_axis_angle((0.0, 0.0, 1.0), roll), _BASE_ROT)
    # Local trims are applied on the right (about the panel's own axes).
    r = _mat3_mul(r, _axis_angle((1.0, 0.0, 0.0), -tilt))
    r = _mat3_mul(r, _axis_angle((0.0, 0.0, 1.0), yaw))
    return r


# Panel rotation in the tip frame (3x3, rows). Recomputed by panel_offset()
# so edits to the constants above at runtime take effect.
PANEL_ROTATION = _panel_rotation()

# Panel centre in the tip frame: wrist axis pushed out along the face
# normal by the wrist radius plus the skin gap. With the defaults this is
# about (-0.0286, -0.0135, 0.130).
PANEL_TRANSLATION = tuple(
    WRIST_CENTRE[i] + (WRIST_RADIUS + SKIN_GAP) * _col(_pose(PANEL_ROTATION, (0, 0, 0)), 2)[i]
    for i in range(3))


def panel_offset(overrides=None) -> list:
    """3x4 pose of the panel centre expressed in the left controller tip frame.

    Rebuilt from the tunable constants on every call so live tweaks apply.
    overrides: {constant name: value} to use instead of the module's, for one call
    (the settings page previews a placement this way without changing the module).
    """
    o = overrides or {}
    centre = o.get("WRIST_CENTRE", WRIST_CENTRE)
    rot = _panel_rotation(o.get("WRIST_ROLL_DEG"), o.get("PANEL_TILT_DEG"), o.get("PANEL_YAW_DEG"))
    normal = (rot[0][2], rot[1][2], rot[2][2])
    d = o.get("WRIST_RADIUS", WRIST_RADIUS) + o.get("SKIN_GAP", SKIN_GAP)
    t = tuple(centre[i] + d * normal[i] for i in range(3))
    return _pose(rot, t)


def view_alpha(panel, hmd) -> float:
    """Opacity 0..1 from how squarely the panel faces the HMD.

    panel, hmd: 3x4 world poses. Uses the panel's +Z (face normal) and the
    unit vector from the panel centre to the HMD origin.
    """
    normal = _norm(_col(panel, 2))
    to_eye = _norm((hmd[0][3] - panel[0][3],
                    hmd[1][3] - panel[1][3],
                    hmd[2][3] - panel[2][3]))
    if normal == (0.0, 0.0, 0.0) or to_eye == (0.0, 0.0, 0.0):
        return 0.0  # degenerate: coincident points or broken pose
    c = _dot(normal, to_eye)
    a = _smoothstep(FADE_START_COS, FADE_FULL_COS, c)
    return 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def _fmt(v):
        return "(" + ", ".join("%+.3f" % x for x in v) + ")"

    off = panel_offset()
    print("panel offset in tip frame:")
    for row in off:
        print("   ", ["%+.4f" % x for x in row])

    # HMD standing at origin, eyes 1.6 m up, looking along world -Z.
    hmd = _pose(_from_columns((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0.0, 1.6, 0.0))
    eye = (0.0, 1.6, 0.0)

    def tip_pose_for_glance(wrist_world, err_deg=0.0):
        """Left forearm across the chest, hand to the user's right (+X),
        wrist turned so the panel normal points at the eyes (optionally
        err_deg off, rolled about the forearm)."""
        z_w = (-1.0, 0.0, 0.0)  # tip +Z points back toward the elbow
        u = _norm(tuple(eye[i] - wrist_world[i] for i in range(3)))
        u = _norm(tuple(u[i] - _dot(u, z_w) * z_w[i] for i in range(3)))
        if err_deg:
            u = _mat3_vec(_axis_angle(z_w, err_deg), u)
        w = _cross(u, z_w)
        s = math.sin(math.radians(WRIST_ROLL_DEG))
        c = math.cos(math.radians(WRIST_ROLL_DEG))
        x = tuple(-s * u[i] + c * w[i] for i in range(3))
        y = tuple(c * u[i] + s * w[i] for i in range(3))
        rot = _from_columns(x, y, z_w)
        wc = _mat3_vec(rot, WRIST_CENTRE)
        return _pose(rot, tuple(wrist_world[i] - wc[i] for i in range(3)))

    def report(name, tip):
        panel = _pose_mul(tip, off)
        print("%-44s alpha=%.3f  normal=%s  img-x=%s  img-up=%s" % (
            name, view_alpha(panel, hmd), _fmt(_col(panel, 2)),
            _fmt(_col(panel, 0)), _fmt(_col(panel, 1))))

    wrist_a = (-0.05, 1.25, -0.30)
    report("(a) arm raised, wrist turned to face", tip_pose_for_glance(wrist_a))
    report("(a') same, 25 deg short of square", tip_pose_for_glance(wrist_a, 25))
    report("(a'') same, 50 deg short of square", tip_pose_for_glance(wrist_a, 50))

    # (b) arm hanging at the left side, controller pointing forward and
    # pitched 45 deg down, top of controller up/forward.
    pitch = _axis_angle((1, 0, 0), -45.0)
    report("(b) arm down by the side", _pose(pitch, (-0.25, 0.85, -0.15)))
    # (b') arm by the side, controller level and pointing forward.
    report("(b') arm by side, controller level", _pose(
        _from_columns((1, 0, 0), (0, 1, 0), (0, 0, 1)), (-0.25, 0.9, -0.2)))

    # Degenerate: panel exactly at the HMD position.
    print("coincident points alpha =", view_alpha(
        _pose(_from_columns((1, 0, 0), (0, 1, 0), (0, 0, 1)), eye), hmd))
