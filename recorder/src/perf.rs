//! Samples what the recording costs, once a second, into a CSV next to the
//! video: the recorder's own CPU, GPU and memory, the whole headset's, and
//! how the capture itself is keeping up. Cheap enough to leave on.
//!
//! A recorder that runs for its replay buffer (Cuff keeps one up all day)
//! has no video to put the CSV next to, so it goes in the state folder
//! instead: one per start, of which only the newest few are kept, each
//! capped in size (past the cap it starts over, so it holds the latest
//! stretch).

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

const INTERVAL: Duration = Duration::from_secs(1);
/// How often (in samples) to look for processes that opened the GPU since
/// last time. Walking every process's fds is the priciest thing in here.
const RESCAN_EVERY: u32 = 30;

/// Bumped by the capture loop.
#[derive(Default)]
pub struct Counters {
    pub frames: AtomicU64,
    pub dropped: AtomicU64,
    /// Shader time from GPU timestamps, in nanoseconds.
    pub shader_ns: AtomicU64,
}

#[derive(Default, Clone, Copy)]
struct Stat {
    sum: f64,
    max: f64,
}

impl Stat {
    fn add(&mut self, v: f64) {
        self.sum += v;
        self.max = self.max.max(v);
    }
}

#[derive(Default)]
pub struct Summary {
    samples: u32,
    rec_cpu: Stat,
    rec_gpu: Stat,
    rec_mem: Stat,
    sys_cpu: Stat,
    sys_gpu: Stat,
    shader_ms: Stat,
}

impl Summary {
    /// One line for the log, which the dashboard tab also reads back.
    pub fn line(&self) -> String {
        let n = self.samples.max(1) as f64;
        format!(
            "perf summary: recorder cpu {:.1}% of a core (max {:.1}), gpu {:.1}% (max {:.1}), {:.0} MB ram, shader {:.2} ms/frame; whole headset cpu {:.0}%, gpu {:.0}%",
            self.rec_cpu.sum / n,
            self.rec_cpu.max,
            self.rec_gpu.sum / n,
            self.rec_gpu.max,
            self.rec_mem.max,
            self.shader_ms.sum / n,
            self.sys_cpu.sum / n,
            self.sys_gpu.sum / n,
        )
    }
}

pub struct PerfLog {
    stop: Arc<AtomicBool>,
    thread: JoinHandle<Summary>,
    pub path: PathBuf,
}

/// Where the CSV for a recording goes: `clip.mp4` -> `clip.perf.csv`.
pub fn csv_path(video: &Path) -> PathBuf {
    video.with_extension("perf.csv")
}

/// Session CSVs kept in the state folder, the new one included.
pub const SESSION_LOGS_KEPT: usize = 5;
/// Biggest a session CSV gets (a line a second: about 8 hours).
pub const SESSION_LOG_MAX_BYTES: u64 = 2 << 20;

/// Where a session's CSV goes when there's no recording to put it next to:
/// ~/.local/state/framecorder/<name>.perf.csv, with all but the newest few
/// older ones deleted.
pub fn session_csv(name: &str) -> Result<PathBuf> {
    let home = std::env::var_os("HOME").context("HOME isn't set")?;
    let dir = PathBuf::from(home).join(".local/state/framecorder");
    fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    prune(&dir, SESSION_LOGS_KEPT - 1);
    Ok(csv_path(&dir.join(name)))
}

/// Deletes all but the newest `keep` CSVs in `dir`.
fn prune(dir: &Path, keep: usize) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut logs: Vec<_> = entries
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().ends_with(".perf.csv"))
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .collect();
    logs.sort();
    let excess = logs.len().saturating_sub(keep);
    for (_, path) in logs.into_iter().take(excess) {
        let _ = fs::remove_file(path);
    }
}

/// Starts sampling into the CSV at `path`, starting it over whenever it
/// would grow past `max_bytes`.
pub fn start(path: PathBuf, max_bytes: Option<u64>, counters: Arc<Counters>) -> Result<PerfLog> {
    let file = File::create(&path).with_context(|| format!("creating {}", path.display()))?;
    let stop = Arc::new(AtomicBool::new(false));
    let thread = std::thread::Builder::new().name("perf".into()).spawn({
        let stop = stop.clone();
        move || sample(Csv { out: BufWriter::new(file), written: 0, max_bytes }, counters, &stop)
    })?;
    Ok(PerfLog { stop, thread, path })
}

const HEADER: &str =
    "time_s,fps,dropped,rec_cpu_pct,rec_gpu_pct,shader_ms_per_frame,rec_rss_mb,rec_anon_mb,sys_cpu_pct,sys_gpu_pct,gpu_mhz,sys_mem_used_mb";

/// The CSV file, kept under its size cap.
struct Csv {
    out: BufWriter<File>,
    written: u64,
    max_bytes: Option<u64>,
}

impl Csv {
    fn line(&mut self, line: &str) {
        let len = line.len() as u64 + 1;
        if self.written > 0 && self.max_bytes.is_some_and(|max| self.written + len > max) {
            // Start over rather than stop: the latest stretch is the useful one.
            let _ = self.out.flush();
            let file = self.out.get_mut();
            let _ = file.set_len(0);
            let _ = file.seek(SeekFrom::Start(0));
            self.written = 0;
            self.line(HEADER);
        }
        let _ = writeln!(self.out, "{line}");
        let _ = self.out.flush();
        self.written += len;
    }
}

impl PerfLog {
    pub fn finish(self) -> Summary {
        self.stop.store(true, Ordering::SeqCst);
        self.thread.join().unwrap_or_default()
    }
}

struct Sample {
    at: Instant,
    frames: u64,
    dropped: u64,
    shader_ns: u64,
    rec_ticks: u64,
    rec_gpu_ns: u64,
    sys_busy: u64,
    sys_total: u64,
    sys_gpu_ns: u64,
}

fn sample(mut out: Csv, counters: Arc<Counters>, stop: &AtomicBool) -> Summary {
    out.line(HEADER);
    let tick_hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
    let gpu_freq = gpu_freq_path();
    let mut gpu_clients = drm_clients();
    let start = Instant::now();
    let mut summary = Summary::default();
    let mut last = read(&counters, &gpu_clients);
    let mut rounds = 0u32;

    while !stop.load(Ordering::Relaxed) {
        // Sleep in small steps so stopping doesn't wait a whole interval.
        let wake = Instant::now() + INTERVAL;
        while Instant::now() < wake && !stop.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_millis(100));
        }
        rounds += 1;
        if rounds.is_multiple_of(RESCAN_EVERY) {
            gpu_clients = drm_clients();
        }

        let now = read(&counters, &gpu_clients);
        let dt = now.at.duration_since(last.at).as_secs_f64().max(1e-3);
        let frames = now.frames.saturating_sub(last.frames);
        let fps = frames as f64 / dt;
        let dropped = now.dropped.saturating_sub(last.dropped);
        let rec_cpu = now.rec_ticks.saturating_sub(last.rec_ticks) as f64 / tick_hz / dt * 100.0;
        let rec_gpu = now.rec_gpu_ns.saturating_sub(last.rec_gpu_ns) as f64 / 1e9 / dt * 100.0;
        let shader_ms = now.shader_ns.saturating_sub(last.shader_ns) as f64 / 1e6 / frames.max(1) as f64;
        let sys_total = now.sys_total.saturating_sub(last.sys_total).max(1) as f64;
        let sys_cpu = now.sys_busy.saturating_sub(last.sys_busy) as f64 / sys_total * 100.0;
        // Clients coming and going can make the sum step backwards; clamp.
        let sys_gpu = (now.sys_gpu_ns.saturating_sub(last.sys_gpu_ns) as f64 / 1e9 / dt * 100.0).min(100.0);
        let (rss, anon) = own_memory();
        let mhz = gpu_freq.as_ref().and_then(|p| read_u64(p)).map_or(0, |hz| hz / 1_000_000);
        let mem_used = system_memory_used();

        out.line(&format!(
            "{:.1},{:.1},{},{:.2},{:.2},{:.3},{:.1},{:.1},{:.1},{:.1},{},{:.0}",
            start.elapsed().as_secs_f64(),
            fps,
            dropped,
            rec_cpu,
            rec_gpu,
            shader_ms,
            rss,
            anon,
            sys_cpu,
            sys_gpu,
            mhz,
            mem_used
        ));

        summary.samples += 1;
        summary.rec_cpu.add(rec_cpu);
        summary.rec_gpu.add(rec_gpu);
        summary.rec_mem.add(rss);
        summary.sys_cpu.add(sys_cpu);
        summary.sys_gpu.add(sys_gpu);
        summary.shader_ms.add(shader_ms);
        last = now;
    }
    summary
}

fn read(counters: &Counters, gpu_clients: &[PathBuf]) -> Sample {
    let (sys_busy, sys_total) = system_cpu();
    Sample {
        at: Instant::now(),
        frames: counters.frames.load(Ordering::Relaxed),
        dropped: counters.dropped.load(Ordering::Relaxed),
        shader_ns: counters.shader_ns.load(Ordering::Relaxed),
        rec_ticks: own_cpu_ticks(),
        rec_gpu_ns: own_drm_clients().iter().filter_map(|p| gpu_ns(p)).sum(),
        sys_busy,
        sys_total,
        sys_gpu_ns: gpu_clients.iter().filter_map(|p| gpu_ns(p)).sum(),
    }
}

fn read_u64(path: &Path) -> Option<u64> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

/// utime + stime of every thread in this process.
fn own_cpu_ticks() -> u64 {
    let Ok(stat) = fs::read_to_string("/proc/self/stat") else { return 0 };
    // Fields after the ")" that closes the command name; utime and stime are 14 and 15.
    let rest = stat.rsplit_once(')').map_or("", |(_, r)| r);
    let fields: Vec<&str> = rest.split_whitespace().collect();
    let get = |i: usize| fields.get(i).and_then(|f| f.parse::<u64>().ok()).unwrap_or(0);
    get(11) + get(12)
}

/// Resident and anonymous (really ours, not shared libraries) memory in MB.
fn own_memory() -> (f64, f64) {
    let Ok(status) = fs::read_to_string("/proc/self/status") else { return (0.0, 0.0) };
    let field = |name: &str| {
        status
            .lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
            .map_or(0.0, |kb| kb / 1024.0)
    };
    (field("VmRSS:"), field("RssAnon:"))
}

/// (busy, total) jiffies across all CPUs.
fn system_cpu() -> (u64, u64) {
    let Ok(stat) = fs::read_to_string("/proc/stat") else { return (0, 0) };
    let Some(line) = stat.lines().next() else { return (0, 0) };
    let v: Vec<u64> = line.split_whitespace().skip(1).filter_map(|f| f.parse().ok()).collect();
    let total: u64 = v.iter().take(8).sum();
    let idle = v.get(3).copied().unwrap_or(0) + v.get(4).copied().unwrap_or(0);
    (total.saturating_sub(idle), total)
}

fn system_memory_used() -> f64 {
    let Ok(info) = fs::read_to_string("/proc/meminfo") else { return 0.0 };
    let field = |name: &str| {
        info.lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(0.0)
    };
    (field("MemTotal:") - field("MemAvailable:")) / 1024.0
}

fn gpu_freq_path() -> Option<PathBuf> {
    fs::read_dir("/sys/class/devfreq").ok()?.flatten().find_map(|e| {
        let name = e.file_name().to_string_lossy().into_owned();
        name.contains("gpu").then(|| e.path().join("cur_freq"))
    })
}

/// GPU time a DRM client has used so far, from its fdinfo.
fn gpu_ns(fdinfo: &Path) -> Option<u64> {
    let text = fs::read_to_string(fdinfo).ok()?;
    text.lines()
        .find(|l| l.starts_with("drm-engine-gpu:"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|v| v.parse().ok())
}

fn is_drm(fd_link: &Path) -> bool {
    fs::read_link(fd_link).is_ok_and(|t| t.starts_with("/dev/dri/"))
}

fn drm_fdinfos(pid_dir: &Path) -> Vec<PathBuf> {
    let Ok(fds) = fs::read_dir(pid_dir.join("fd")) else { return Vec::new() };
    fds.flatten()
        .filter(|e| is_drm(&e.path()))
        .map(|e| pid_dir.join("fdinfo").join(e.file_name()))
        .collect()
}

fn own_drm_clients() -> Vec<PathBuf> {
    drm_fdinfos(Path::new("/proc/self"))
}

/// One fdinfo per GPU client on the system. Several fds can share a client,
/// so they're told apart by client id to avoid counting anything twice.
fn drm_clients() -> Vec<PathBuf> {
    let Ok(procs) = fs::read_dir("/proc") else { return Vec::new() };
    let mut by_id: HashMap<String, PathBuf> = HashMap::new();
    for p in procs.flatten() {
        if !p.file_name().to_string_lossy().bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        for info in drm_fdinfos(&p.path()) {
            let Ok(text) = fs::read_to_string(&info) else { continue };
            if let Some(id) = text.lines().find(|l| l.starts_with("drm-client-id:")) {
                by_id.entry(id.to_string()).or_insert(info);
            }
        }
    }
    by_id.into_values().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("framecorder-perf-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn only_the_newest_logs_are_kept() {
        let dir = tempdir("prune");
        for i in 0..8 {
            let path = dir.join(format!("{i}.perf.csv"));
            fs::write(&path, "x").unwrap();
            let t = std::time::SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000 + i);
            File::options().write(true).open(&path).unwrap().set_modified(t).unwrap();
        }
        fs::write(dir.join("keep.mp4"), "x").unwrap();
        prune(&dir, 4);
        let mut left: Vec<String> = fs::read_dir(&dir).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left, ["4.perf.csv", "5.perf.csv", "6.perf.csv", "7.perf.csv", "keep.mp4"]);
    }

    #[test]
    fn a_capped_log_starts_over() {
        let dir = tempdir("cap");
        let path = dir.join("a.perf.csv");
        let mut csv = Csv { out: BufWriter::new(File::create(&path).unwrap()), written: 0, max_bytes: Some(HEADER.len() as u64 + 40) };
        csv.line(HEADER);
        for i in 0..10 {
            csv.line(&format!("{i},row"));
        }
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.len() as u64 <= HEADER.len() as u64 + 40, "{text:?}");
        assert!(text.starts_with(HEADER));
        assert!(text.ends_with("9,row\n"));
    }
}
