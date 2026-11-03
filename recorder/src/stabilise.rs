//! Stabilisation for the cropped eye views (16:9, 1:1, 9:16).
//!
//! The crop normally follows the head exactly, so every small head movement
//! shakes the video. The panels show more than the crop, so instead the crop
//! is turned against the head's motion: the video's camera follows a smoothed
//! copy of the head's orientation, and each frame the lookup grid is rebuilt
//! on the GPU for the difference (shaders/stabilise.comp, submitted with the
//! conversion; the CPU only works out the rotation).
//!
//! - The correction is `head^T * smoothed`: a direction in the video's frame,
//!   taken to the head's. Its twist about the view axis is dropped, so the
//!   video stays level with the head as it always was; only jitter in where
//!   it points is smoothed.
//! - It's held within the margin the narrower crop leaves (lut::Stable::margin),
//!   by pulling the smoothed orientation along, so an edge never shows black.
//! - Smoothing is an exponential slerp with time constant `tau`: higher is
//!   steadier, but lags a deliberate turn more.
//!
//! At startup the GPU's grid is checked twice against the CPU's
//! (lut::Stable::fill): with no rotation it has to match the ordinary grid,
//! and for a test rotation it has to match the CPU's rebuild.

use std::time::Instant;

use anyhow::Result;

use crate::gpu::Gpu;
use crate::lut::Stable;
use cuff_recorder::openvr::OpenVr;

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum Strength {
    Off,
    Light,
    Strong,
}

impl Strength {
    /// How much of the widest clean view the crop keeps, leaving the rest to move in.
    pub fn fov_scale(self) -> f64 {
        match self {
            Strength::Off => 1.0,
            Strength::Light => 0.88,
            Strength::Strong => 0.78,
        }
    }

    /// Smoothing time constant, seconds.
    fn tau(self) -> f64 {
        match self {
            Strength::Off => 0.0,
            Strength::Light => 0.12,
            Strength::Strong => 0.35,
        }
    }
}

/// Keeps a little of the margin spare, for the corners (which a turn moves
/// further than the edges' middles) and the twist that's left in.
const MARGIN_USE: f64 = 0.85;

pub struct Stabiliser {
    vr: OpenVr,
    tau: f64,
    max_angle: f64,
    predict: f32,
    smoothed: Option<Quat>,
    last: Option<Instant>,
    /// CPU time spent on stabilisation since the last status line.
    pub busy: std::time::Duration,
}

impl Stabiliser {
    /// Sets up the GPU pass. `base` is the grid the GPU started with, for the checks.
    pub fn new(stable: Stable, base: &[[f32; 2]], strength: Strength, predict: f32, gpu: &mut Gpu) -> Result<Self> {
        let vr = OpenVr::connect()?;
        gpu.enable_stabilise(&stable.rays(), &stable.inverse(), &stable.constants())?;

        // With no rotation, the GPU's grid has to be the one built at startup.
        let unturned = gpu.stabilised_grid(base.len())?;
        report("with no rotation, the GPU's grid matches the startup one", worst_px(base, &unturned));
        // A test turn (2° yaw, 1° pitch): the GPU has to agree with the CPU.
        let turn = Quat::from_axis([0.0, 1.0, 0.0], 2.0).mul(Quat::from_axis([1.0, 0.0, 0.0], 1.0)).to_matrix();
        gpu.set_rotation(&turn);
        let gpu_turned = gpu.stabilised_grid(base.len())?;
        let mut cpu_turned = vec![[0.0f32; 2]; base.len()];
        stable.fill(&turn, &mut cpu_turned);
        report("for a test turn, the GPU's grid matches the CPU's", worst_px(&cpu_turned, &gpu_turned));
        gpu.set_rotation(&IDENTITY);

        let max_angle = stable.margin() * MARGIN_USE;
        log::info!(
            "stabilising ({strength:?}): up to {:.1}° of correction, smoothing over {:.0} ms",
            max_angle.to_degrees(),
            strength.tau() * 1e3
        );
        Ok(Self { vr, tau: strength.tau(), max_angle, predict, smoothed: None, last: None, busy: std::time::Duration::ZERO })
    }

    /// Before each frame: the rotation for the head's latest orientation,
    /// which the next `convert` rebuilds the grid with.
    pub fn update(&mut self, gpu: &mut Gpu) {
        let started = Instant::now();
        let dt = self.last.map_or(0.0, |t| t.elapsed().as_secs_f64());
        self.last = Some(started);
        let rot = match self.vr.head_rotation(self.predict) {
            Some(m) => {
                let head = Quat::from_matrix(&m);
                let (smoothed, correction) = step(self.smoothed, head, dt, self.tau, self.max_angle);
                self.smoothed = Some(smoothed);
                correction.to_matrix()
            }
            None => {
                // Not tracking (off-head, standby): follow the head again from scratch.
                self.smoothed = None;
                IDENTITY
            }
        };
        gpu.set_rotation(&rot);
        self.busy += started.elapsed();
    }
}

/// The largest difference between two grids, in panel pixels (the scanout
/// is two 2160x2160 eyes side by side). Points off the panel are skipped.
fn worst_px(a: &[[f32; 2]], b: &[[f32; 2]]) -> f64 {
    a.iter()
        .zip(b)
        .filter(|(a, b)| a[0] >= 0.0 && b[0] >= 0.0)
        .map(|(a, b)| (((a[0] - b[0]) as f64 * 4320.0).powi(2) + ((a[1] - b[1]) as f64 * 2160.0).powi(2)).sqrt())
        .fold(0.0, f64::max)
}

fn report(what: &str, px: f64) {
    if px > 0.5 {
        log::warn!("stabilisation check failed: {what} only within {px:.2} panel px");
    } else {
        log::info!("stabilisation check: {what} within {px:.3} panel px");
    }
}

const IDENTITY: [[f64; 3]; 3] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];

/// One frame of smoothing: the new smoothed orientation, and the correction
/// (video frame -> head frame) to render with.
fn step(smoothed: Option<Quat>, head: Quat, dt: f64, tau: f64, max_angle: f64) -> (Quat, Quat) {
    let mut s = match smoothed {
        Some(s) if tau > 0.0 => s.slerp(head, 1.0 - (-dt / tau).exp()),
        _ => head,
    };
    let mut c = swing(head.conj().mul(s));
    let angle = c.angle();
    if angle > max_angle {
        // Too far: drag the smoothed view along so the crop stays inside.
        s = head.slerp(s, max_angle / angle);
        c = swing(head.conj().mul(s));
    }
    (s, c)
}

/// The part of a rotation that isn't a twist about the view axis (z).
fn swing(q: Quat) -> Quat {
    let twist = Quat { w: q.w, x: 0.0, y: 0.0, z: q.z }.normalized();
    q.mul(twist.conj()).normalized()
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct Quat {
    w: f64,
    x: f64,
    y: f64,
    z: f64,
}

impl Quat {
    fn from_axis(ax: [f64; 3], deg: f64) -> Self {
        let (s, c) = (deg.to_radians() / 2.0).sin_cos();
        Quat { w: c, x: ax[0] * s, y: ax[1] * s, z: ax[2] * s }
    }

    fn from_matrix(m: &[[f64; 3]; 3]) -> Self {
        let trace = m[0][0] + m[1][1] + m[2][2];
        let q = if trace > 0.0 {
            let s = (trace + 1.0).sqrt() * 2.0;
            Quat { w: 0.25 * s, x: (m[2][1] - m[1][2]) / s, y: (m[0][2] - m[2][0]) / s, z: (m[1][0] - m[0][1]) / s }
        } else if m[0][0] > m[1][1] && m[0][0] > m[2][2] {
            let s = (1.0 + m[0][0] - m[1][1] - m[2][2]).sqrt() * 2.0;
            Quat { w: (m[2][1] - m[1][2]) / s, x: 0.25 * s, y: (m[0][1] + m[1][0]) / s, z: (m[0][2] + m[2][0]) / s }
        } else if m[1][1] > m[2][2] {
            let s = (1.0 + m[1][1] - m[0][0] - m[2][2]).sqrt() * 2.0;
            Quat { w: (m[0][2] - m[2][0]) / s, x: (m[0][1] + m[1][0]) / s, y: 0.25 * s, z: (m[1][2] + m[2][1]) / s }
        } else {
            let s = (1.0 + m[2][2] - m[0][0] - m[1][1]).sqrt() * 2.0;
            Quat { w: (m[1][0] - m[0][1]) / s, x: (m[0][2] + m[2][0]) / s, y: (m[1][2] + m[2][1]) / s, z: 0.25 * s }
        };
        q.normalized()
    }

    fn to_matrix(self) -> [[f64; 3]; 3] {
        let Quat { w, x, y, z } = self;
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)],
            [2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)],
            [2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)],
        ]
    }

    fn mul(self, o: Quat) -> Quat {
        Quat {
            w: self.w * o.w - self.x * o.x - self.y * o.y - self.z * o.z,
            x: self.w * o.x + self.x * o.w + self.y * o.z - self.z * o.y,
            y: self.w * o.y - self.x * o.z + self.y * o.w + self.z * o.x,
            z: self.w * o.z + self.x * o.y - self.y * o.x + self.z * o.w,
        }
    }

    fn conj(self) -> Quat {
        Quat { w: self.w, x: -self.x, y: -self.y, z: -self.z }
    }

    fn normalized(self) -> Quat {
        let n = (self.w * self.w + self.x * self.x + self.y * self.y + self.z * self.z).sqrt();
        if n < 1e-12 {
            return Quat { w: 1.0, x: 0.0, y: 0.0, z: 0.0 };
        }
        Quat { w: self.w / n, x: self.x / n, y: self.y / n, z: self.z / n }
    }

    /// Rotation angle, radians.
    fn angle(self) -> f64 {
        2.0 * self.w.abs().min(1.0).acos()
    }

    fn slerp(self, to: Quat, t: f64) -> Quat {
        let mut dot = self.w * to.w + self.x * to.x + self.y * to.y + self.z * to.z;
        // q and -q are the same rotation; take the short way round.
        let to = if dot < 0.0 {
            dot = -dot;
            Quat { w: -to.w, x: -to.x, y: -to.y, z: -to.z }
        } else {
            to
        };
        if dot > 0.9995 {
            return Quat {
                w: self.w + (to.w - self.w) * t,
                x: self.x + (to.x - self.x) * t,
                y: self.y + (to.y - self.y) * t,
                z: self.z + (to.z - self.z) * t,
            }
            .normalized();
        }
        let theta = dot.acos();
        let (a, b) = (((1.0 - t) * theta).sin() / theta.sin(), (t * theta).sin() / theta.sin());
        Quat { w: a * self.w + b * to.w, x: a * self.x + b * to.x, y: a * self.y + b * to.y, z: a * self.z + b * to.z }
            .normalized()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn axis(ax: [f64; 3], deg: f64) -> Quat {
        let (s, c) = (deg.to_radians() / 2.0).sin_cos();
        Quat { w: c, x: ax[0] * s, y: ax[1] * s, z: ax[2] * s }
    }

    fn close(a: Quat, b: Quat) -> bool {
        // q and -q are the same rotation.
        let d = (a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z).abs();
        d > 1.0 - 1e-9
    }

    #[test]
    fn matrix_round_trip() {
        for q in [axis([0.0, 1.0, 0.0], 30.0), axis([1.0, 0.0, 0.0], -75.0), axis([0.0, 0.0, 1.0], 170.0)] {
            assert!(close(Quat::from_matrix(&q.to_matrix()), q));
        }
    }

    #[test]
    fn still_head_needs_no_correction() {
        let head = axis([0.0, 1.0, 0.0], 40.0);
        let (s, c) = step(Some(head), head, 0.011, 0.35, 0.2);
        assert!(close(s, head));
        assert!(c.angle() < 1e-9);
    }

    #[test]
    fn a_quick_turn_is_held_back_then_followed() {
        let start = Quat { w: 1.0, x: 0.0, y: 0.0, z: 0.0 };
        let head = axis([0.0, 1.0, 0.0], 3.0); // a 3° yaw jolt
        let (s, c) = step(Some(start), head, 0.011, 0.35, 0.2);
        // One frame later the video has barely moved, so the correction is
        // most of the jolt, the other way.
        assert!(c.angle().to_degrees() > 2.8 && c.angle().to_degrees() < 3.0);
        // And it keeps easing in.
        let (s2, c2) = step(Some(s), head, 1.0, 0.35, 0.2);
        assert!(c2.angle() < c.angle());
        assert!(s2.mul(head.conj()).angle() < s.mul(head.conj()).angle());
    }

    #[test]
    fn the_correction_stays_within_the_margin() {
        let start = Quat { w: 1.0, x: 0.0, y: 0.0, z: 0.0 };
        let head = axis([0.0, 1.0, 0.0], 30.0);
        let (_, c) = step(Some(start), head, 0.011, 0.35, 5f64.to_radians());
        assert!(c.angle().to_degrees() <= 5.0 + 1e-6);
    }

    #[test]
    fn twist_about_the_view_axis_is_left_alone() {
        let roll = axis([0.0, 0.0, 1.0], 10.0);
        assert!(swing(roll).angle() < 1e-9);
        let yaw_and_roll = axis([0.0, 1.0, 0.0], 4.0).mul(roll);
        let s = swing(yaw_and_roll);
        assert!((s.angle().to_degrees() - 4.0).abs() < 0.05);
    }
}
