//! The replay buffer clips come from: the last few seconds of encoded video
//! and audio, kept in memory. Nothing gets re-encoded, a clip is just these
//! packets written into a file, starting at a keyframe.
//!
//! What's kept is decided by time, on one timeline for both streams (audio
//! samples), not by the newest video frame: while the panels are dark no
//! video comes, but the mic keeps sending, and that has to age out too.
//! Video stops for real now and then (the headset taken off, SteamVR's
//! headset view going quiet), and a clip never reaches back across such a
//! gap: one asked for after it starts at the first keyframe after the gap,
//! instead of at one from before it with the whole gap in between.

use std::collections::VecDeque;

use crate::audio::RATE;
use crate::writer::Packet;

/// Video frames further apart than this have a gap between them.
pub const GAP_SECS: i64 = 1;

pub struct Ring {
    /// How much to keep, in audio samples.
    keep: i64,
    fps: i64,
    /// Never hold more than this, whatever the timestamps say.
    max_bytes: usize,
    packets: VecDeque<Packet>,
    /// Timeline positions of the keyframes still in `packets`.
    keyframes: VecDeque<i64>,
    /// Gaps in the video that a clip might still reach back to: the last
    /// frame before each and the first one after (video frames).
    gaps: VecDeque<(i64, i64)>,
    newest_video: Option<i64>,
    /// The furthest either stream has got, in audio samples.
    now: i64,
    bytes: usize,
}

/// Video frame `pts` as an audio sample position.
pub fn audio_pos(pts: i64, fps: i64) -> i64 {
    pts * RATE as i64 / fps.max(1)
}

impl Ring {
    pub fn new(secs: u32, fps: u32, max_bytes: usize) -> Self {
        Self {
            keep: secs as i64 * RATE as i64,
            fps: fps as i64,
            max_bytes,
            packets: VecDeque::new(),
            keyframes: VecDeque::new(),
            gaps: VecDeque::new(),
            newest_video: None,
            now: 0,
            bytes: 0,
        }
    }

    pub fn bytes(&self) -> usize {
        self.bytes
    }

    pub fn push(&mut self, p: Packet) {
        let end = if p.stream == 0 {
            if let Some(prev) = self.newest_video {
                if p.pts - prev > GAP_SECS * self.fps {
                    self.gaps.push_back((prev, p.pts));
                }
            }
            if p.key {
                self.keyframes.push_back(p.pts);
            }
            self.newest_video = Some(self.newest_video.map_or(p.pts, |n| n.max(p.pts)));
            audio_pos(p.pts + 1, self.fps)
        } else {
            p.pts + p.duration
        };
        self.now = self.now.max(end);
        self.bytes += p.data.len();
        self.packets.push_back(p);
        self.trim();
    }

    /// The keyframe a clip of `span` audio samples ending now starts at: the
    /// last one far enough back, so clips are never shorter than asked, or
    /// the oldest one there is. Never one from before a gap in the video
    /// that began before the clip would: then it's the first keyframe after
    /// that gap. None when there's no video in that span at all.
    fn start_for(&self, span: i64) -> Option<i64> {
        let newest = self.newest_video?;
        let want = self.now - span;
        if audio_pos(newest + 1, self.fps) <= want {
            return None;
        }
        let floor = self
            .gaps
            .iter()
            .rev()
            .find(|&&(before, _)| audio_pos(before + 1, self.fps) <= want)
            .map(|&(_, after)| after);
        let allowed = |k: i64| floor.is_none_or(|f| k >= f);
        self.keyframes
            .iter()
            .rev()
            .find(|&&k| audio_pos(k, self.fps) <= want && allowed(k))
            .or_else(|| self.keyframes.iter().find(|&&k| allowed(k)))
            .copied()
    }

    fn trim(&mut self) {
        let start = self.start_for(self.keep);
        let cutoff = self.now - self.keep;
        let (video_from, audio_from) = match start {
            Some(s) => {
                while self.keyframes.front().is_some_and(|&k| k < s) {
                    self.keyframes.pop_front();
                }
                while self.gaps.front().is_some_and(|&(_, after)| after <= s) {
                    self.gaps.pop_front();
                }
                (s, audio_pos(s, self.fps).min(cutoff))
            }
            // No video worth keeping (it stopped longer ago than the buffer
            // holds): only audio, and only the buffer's worth.
            None => {
                self.keyframes.clear();
                self.gaps.clear();
                (i64::MAX, cutoff)
            }
        };
        // Packets arrive roughly in time order, so old ones are at the front.
        while let Some(p) = self.packets.front() {
            let old = if p.stream == 0 { p.pts < video_from } else { p.pts + p.duration <= audio_from };
            if !old {
                break;
            }
            self.pop_front();
        }
        // A safety net: timestamps gone wrong mustn't be able to eat the
        // headset's memory. Normally the buffer is a fraction of this.
        while self.bytes > self.max_bytes && !self.packets.is_empty() {
            self.pop_front();
        }
    }

    fn pop_front(&mut self) {
        if let Some(p) = self.packets.pop_front() {
            self.bytes -= p.data.len();
            if p.stream == 0 && p.key && self.keyframes.front() == Some(&p.pts) {
                self.keyframes.pop_front();
            }
        }
    }

    /// The last `secs` seconds (a bit more, to start on a keyframe): the
    /// video frame it starts at and the packets from there on.
    pub fn snapshot(&self, secs: u32) -> Option<(i64, Vec<Packet>)> {
        let start = self.start_for((secs as i64 * RATE as i64).min(self.keep))?;
        let start_audio = audio_pos(start, self.fps);
        let packets = self
            .packets
            .iter()
            .filter(|p| if p.stream == 0 { p.pts >= start } else { p.pts >= start_audio })
            .cloned()
            .collect();
        Some((start, packets))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    const FPS: u32 = 10;
    const NO_CAP: usize = usize::MAX;

    fn video(pts: i64) -> Packet {
        // A keyframe every 2 seconds, like the encoder does.
        video_key(pts, pts % 20 == 0)
    }

    fn video_key(pts: i64, key: bool) -> Packet {
        Packet { stream: 0, pts, duration: 1, key, data: Arc::from(vec![0u8; 100]) }
    }

    fn audio(pts: i64) -> Packet {
        Packet { stream: 1, pts, duration: 1024, key: true, data: Arc::from(vec![0u8; 10]) }
    }

    /// Video frames `from..to` with audio interleaved, audio a little late.
    fn fill_range(ring: &mut Ring, next_audio: &mut i64, from: i64, to: i64) {
        for f in from..to {
            ring.push(video(f));
            while *next_audio + 1024 <= audio_pos(f, FPS as i64) - 2400 {
                ring.push(audio(*next_audio));
                *next_audio += 1024;
            }
        }
    }

    /// `secs` seconds of video with audio interleaved.
    fn fill(ring: &mut Ring, secs: i64) {
        fill_range(ring, &mut 0, 0, secs * FPS as i64);
    }

    /// Only audio (the mic, panels dark) up to sample `to`.
    fn audio_only(ring: &mut Ring, next_audio: &mut i64, to: i64) {
        while *next_audio + 1024 <= to {
            ring.push(audio(*next_audio));
            *next_audio += 1024;
        }
    }

    fn video_frames(ring: &Ring) -> Vec<i64> {
        ring.packets.iter().filter(|p| p.stream == 0).map(|p| p.pts).collect()
    }

    fn secs(s: i64) -> i64 {
        s * RATE as i64
    }

    #[test]
    fn keeps_about_what_it_should() {
        let mut ring = Ring::new(5, FPS, NO_CAP);
        fill(&mut ring, 60);
        let frames = video_frames(&ring);
        // Newest is 599; 5 s back is 550, the keyframe before that is 540.
        assert_eq!(frames.first(), Some(&540));
        assert_eq!(frames.last(), Some(&599));
        let total: usize = ring.packets.iter().map(|p| p.data.len()).sum();
        assert_eq!(ring.bytes(), total);
    }

    #[test]
    fn empty_ring_has_no_clip() {
        assert!(Ring::new(30, FPS, NO_CAP).snapshot(10).is_none());
    }

    #[test]
    fn audio_ages_out_while_the_video_is_stopped() {
        // 10 s of video, then ten minutes of only the mic.
        let mut ring = Ring::new(5, FPS, NO_CAP);
        let mut next_audio = 0;
        fill_range(&mut ring, &mut next_audio, 0, 100);
        audio_only(&mut ring, &mut next_audio, secs(600));
        // Nothing but the last 5 s of audio is left, and no video.
        assert!(video_frames(&ring).is_empty());
        let audio: Vec<i64> = ring.packets.iter().map(|p| p.pts).collect();
        assert!(audio.len() <= (secs(5) / 1024 + 2) as usize, "{} audio packets", audio.len());
        assert!(audio[0] + 2048 > secs(595), "oldest audio at {}", audio[0]);
        // With no video in the last 5 s there's nothing to clip.
        assert!(ring.snapshot(5).is_none());
    }

    #[test]
    fn clip_after_a_long_gap_starts_after_it() {
        // 20 s of video, a 60 s gap with the mic still on, then 3 s more,
        // the first frame back a keyframe (the capture loop forces one).
        let mut ring = Ring::new(30, FPS, NO_CAP);
        let mut next_audio = 0;
        fill_range(&mut ring, &mut next_audio, 0, 200);
        audio_only(&mut ring, &mut next_audio, secs(80));
        ring.push(video_key(800, true));
        fill_range(&mut ring, &mut next_audio, 801, 830);
        let (start, packets) = ring.snapshot(30).unwrap();
        // Not the keyframe at 18 s from before the gap: the one at 80 s.
        assert_eq!(start, 800);
        assert!(packets.iter().all(|p| if p.stream == 0 { p.pts >= 800 } else { p.pts >= secs(80) }));
        // And the old video has gone from memory.
        assert_eq!(video_frames(&ring).first(), Some(&800));
    }

    #[test]
    fn clip_after_a_long_gap_without_a_keyframe_waits_for_one() {
        // As above, but the first frames back aren't keyframes: the clip
        // starts at the first keyframe after the gap (82 s here, 820).
        let mut ring = Ring::new(30, FPS, NO_CAP);
        let mut next_audio = 0;
        fill_range(&mut ring, &mut next_audio, 0, 200);
        audio_only(&mut ring, &mut next_audio, secs(80));
        fill_range(&mut ring, &mut next_audio, 801, 830);
        let (start, _) = ring.snapshot(30).unwrap();
        assert_eq!(start, 820);
    }

    #[test]
    fn a_short_gap_inside_the_clip_is_kept() {
        // 20 s, a 5 s gap, 20 s more: a 30 s clip spans the gap, starting at
        // the keyframe at or before 30 s back from 45 s (14 s).
        let mut ring = Ring::new(30, FPS, NO_CAP);
        let mut next_audio = 0;
        fill_range(&mut ring, &mut next_audio, 0, 200);
        audio_only(&mut ring, &mut next_audio, secs(25));
        fill_range(&mut ring, &mut next_audio, 250, 450);
        let (start, _) = ring.snapshot(30).unwrap();
        assert_eq!(start, 140);
    }

    #[test]
    fn a_gap_just_before_the_clip_is_skipped() {
        // 20 s, a 5 s gap, then 20 s: a 15 s clip wants to start at 30 s,
        // after the gap; the newest keyframe at or before that is at 30 s.
        // A 22 s clip would want 23 s, inside the gap: it starts where the
        // video came back (25 s), not at 18 s.
        let mut ring = Ring::new(30, FPS, NO_CAP);
        let mut next_audio = 0;
        fill_range(&mut ring, &mut next_audio, 0, 200);
        audio_only(&mut ring, &mut next_audio, secs(25));
        ring.push(video_key(250, true));
        fill_range(&mut ring, &mut next_audio, 251, 450);
        assert_eq!(ring.snapshot(15).unwrap().0, 300);
        assert_eq!(ring.snapshot(22).unwrap().0, 250);
    }

    #[test]
    fn memory_is_capped() {
        // Each video frame is 100 bytes: a 1000 byte cap holds 10 or so.
        let mut ring = Ring::new(30, FPS, 1000);
        fill(&mut ring, 20);
        assert!(ring.bytes() <= 1000);
        let total: usize = ring.packets.iter().map(|p| p.data.len()).sum();
        assert_eq!(ring.bytes(), total);
        // Whatever's left still starts a clip on a keyframe (or has none).
        if let Some((start, _)) = ring.snapshot(30) {
            assert!(start % 20 == 0);
        }
    }
}
