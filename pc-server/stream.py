"""What the PC knows about a Steam Link VR stream, for the headset's Performance tab
(GET /stream on the PC app): live from the PC's own SteamVR compositor (OpenVR, sampled
once a second), the game's framerate against the target, its GPU and CPU milliseconds per
frame, dropped and reprojected frames, and motion smoothing (SteamVR's counterpart of SSW).
Only while SteamVR runs on the PC; this never starts it.

Steam Link's own encode / network / decode breakdown isn't published anywhere live (only in
its client's debug window and in timing snapshots it writes after a hitch), so the headset
measures the links itself instead.

  python stream.py        print one answer (a check while streaming)
"""
import json
import sys
import threading
import time

SAMPLE_S = 1.0
RETRY_S = 10.0


# ---------------------------------------------------------------- the PC's compositor

class PcMonitor(threading.Thread):
    """latest: {"app", "fps", "target_hz", "gpu_ms", "cpu_ms", "dropped", "reprojected",
    "motion_enabled", "motion_forced", "motion_active", "async"}, or None while SteamVR isn't
    running on the PC (or no frames are coming)."""

    def __init__(self):
        super().__init__(daemon=True, name="pc-monitor")
        self.latest = None

    def run(self):
        while True:
            try:
                import openvr
            except ImportError:
                print("stream: no pyopenvr; PC frame timing off", file=sys.stderr, flush=True)
                return
            try:
                system = openvr.init(openvr.VRApplication_Background)  # never starts SteamVR
            except openvr.error_code.InitError:
                time.sleep(RETRY_S)
                continue
            try:
                self._watch(openvr, system)
            except openvr.error_code.OpenVRError as e:
                print(f"stream: {e!r}", file=sys.stderr, flush=True)
            finally:
                self.latest = None
                try:
                    openvr.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(RETRY_S)

    def _watch(self, openvr, system):
        comp, apps = openvr.VRCompositor(), openvr.VRApplications()
        size = openvr.sizeof(openvr.Compositor_CumulativeStats)
        prev, prev_t = comp.getCumulativeStats(size), time.monotonic()
        ev = openvr.VREvent_t()
        while True:
            time.sleep(SAMPLE_S)
            while system.pollNextEvent(ev):
                if ev.eventType == openvr.VREvent_Quit:
                    system.acknowledgeQuit_Exiting()
                    return
            cur, now = comp.getCumulativeStats(size), time.monotonic()
            frames, dt = cur.m_nNumFrameSubmits - prev.m_nNumFrameSubmits, now - prev_t
            if frames <= 0 or dt <= 0 or cur.m_nPid != prev.m_nPid:
                self.latest = None if frames <= 0 else self.latest
                prev, prev_t = cur, now
                continue
            ok, f = comp.getFrameTiming(0)
            flags = f.m_nReprojectionFlags if ok else 0
            pid = apps.getCurrentSceneProcessId()
            self.latest = {
                "app": apps.getApplicationKeyByProcessId(pid) if pid else None,
                "fps": round(frames / dt, 1),
                "target_hz": system.getFloatTrackedDeviceProperty(openvr.k_unTrackedDeviceIndex_Hmd,
                                                                   openvr.Prop_DisplayFrequency_Float),
                "gpu_ms": round((cur.m_flSumApplicationGPUTimeMS - prev.m_flSumApplicationGPUTimeMS) / frames, 2),
                "cpu_ms": round((cur.m_flSumApplicationCPUTimeMS - prev.m_flSumApplicationCPUTimeMS) / frames, 2),
                "dropped": round((cur.m_nNumDroppedFrames - prev.m_nNumDroppedFrames) / dt, 1),
                "reprojected": round((cur.m_nNumReprojectedFrames - prev.m_nNumReprojectedFrames) / dt, 1),
                "async": bool(flags & openvr.VRCompositor_ReprojectionAsync),
                "motion_active": bool(flags & openvr.VRCompositor_ReprojectionMotion),
                "motion_enabled": bool(flags & openvr.VRCompositor_ReprojectionMotion_Enabled),
                "motion_forced": bool(flags & openvr.VRCompositor_ReprojectionMotion_ForcedOn),
            }
            prev, prev_t = cur, now


MONITOR = PcMonitor()


def snapshot():
    return {"pc": MONITOR.latest}


if __name__ == "__main__":
    MONITOR.start()
    time.sleep(2.5)
    print(json.dumps(snapshot(), indent=1))
