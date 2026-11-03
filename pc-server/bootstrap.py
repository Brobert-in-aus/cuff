"""First-run setup for an installed Cuff for PC: `uv sync`, with a window showing progress.

run-server.ps1 runs this (standard library only, through `uv run --no-project`) when the
app's packages aren't installed yet or uv.lock has changed since (an update). The first time
that's about 2.5 GB, mostly NVIDIA's CUDA libraries for dictation, so it takes a while; a
quick sync (an update that changed little) never shows the window.

Exit status 0 once the packages match uv.lock (recorded in .venv/.cuff-synced), else 1.
"""
import hashlib
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(HERE, ".venv", ".cuff-synced")
SHOW_AFTER_S = 1.5
LOG = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "cuff-pc", "setup.log")


def lock_hash():
    with open(os.path.join(HERE, "uv.lock"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def synced():
    try:
        with open(MARKER) as f:
            return f.read().strip() == lock_hash()
    except OSError:
        return False


def uv():
    bundled = os.path.join(os.path.dirname(HERE), "uv.exe")
    return bundled if os.path.exists(bundled) else "uv"


def main():
    if synced():
        return 0
    lines = queue.Queue()
    result = {}

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log = open(LOG, "a", encoding="utf-8")
    log.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} uv sync in {HERE}\n")

    def sync():
        try:
            proc = subprocess.Popen([uv(), "sync", "--frozen", "--project", HERE], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                    errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in proc.stdout:
                lines.put(line.rstrip())
                log.write(line)
            result["code"] = proc.wait()
        except OSError as e:
            log.write(f"couldn't run uv: {e!r}\n")
            result["code"] = -1

    worker = threading.Thread(target=sync, daemon=True)
    worker.start()
    worker.join(SHOW_AFTER_S)
    if worker.is_alive():
        window(worker, lines, result)
    worker.join()
    log.write(f"exit {result.get('code')}\n")
    log.close()
    if result.get("code") == 0:
        os.makedirs(os.path.dirname(MARKER), exist_ok=True)
        with open(MARKER, "w") as f:
            f.write(lock_hash())
        return 0
    return 1


def window(worker, lines, result):
    import tkinter
    from tkinter import ttk
    root = tkinter.Tk()
    root.title("Cuff for PC")
    root.geometry("520x300")
    root.resizable(False, False)
    tkinter.Label(root, text="Setting up Cuff for PC", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
    tkinter.Label(root, text="Downloading what it needs, about 2.5 GB the first time (mostly for dictation on "
                             "your graphics card). This happens once.", wraplength=488, justify="left",
                  font=("Segoe UI", 9)).pack(anchor="w", padx=16)
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=16, pady=10)
    bar.start(12)
    log = tkinter.Text(root, height=8, font=("Consolas", 8), relief="flat", background="#f3f4f8")
    log.pack(fill="both", expand=True, padx=16, pady=(0, 14))
    started = time.monotonic()

    def poll():
        while not lines.empty():
            log.insert("end", lines.get() + "\n")
            log.see("end")
        if worker.is_alive():
            root.title(f"Cuff for PC: setting up ({int(time.monotonic() - started)} s)")
            root.after(200, poll)
        elif result.get("code") == 0:
            root.destroy()
        else:
            bar.stop()
            log.insert("end", "\nSetup didn't finish. Check the internet connection, then start Cuff for PC "
                              "again from the Start menu.\n")
            log.see("end")

    root.after(200, poll)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
