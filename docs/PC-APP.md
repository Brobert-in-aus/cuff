# Cuff for PC: the desktop app

Cuff for PC is one Python process on Windows: the server the headset talks to (dictation,
stream stats, pairing), a tray icon, and a window. From the window it also sets up the
headset: installing and updating Cuff, its recordings and screenshots, every Cuff setting, and typing on it
from the PC keyboard.

Built 2026-10-26. The Windows installer is written and staged, but not yet compiled (it needs
Inno Setup; see "The installer").

## Running it

- **At logon:** the "Cuff for PC" scheduled task runs `run-server.ps1`. It runs
  `bootstrap.py`, which installs the Python packages the first time and after an update, then
  `app.py` (the server, then the tray icon). `install-autostart.ps1` sets it up, with a Start
  menu shortcut, **Cuff for PC**; the installer does the same.
- **The window** is the app page, `http://127.0.0.1:8765/app`, in Microsoft Edge's app mode:
  its own window, no tabs or address bar, with its own profile
  (`%LOCALAPPDATA%\cuff-pc\window`). Without Edge, the default browser opens it.
- **The tray icon** is a cuff, with a green dot while a headset has been heard from in the
  last minute. Its menu: Open Cuff for PC (or double-click), Pair a headset, Recordings,
  Type on the Frame, Quit. Closing the window leaves the app running.
- **Ctrl+Alt+V** anywhere types the PC's clipboard on the headset, with a notification.
- **One copy:** a named mutex (`Local\CuffForPC-<port>`, per Windows session) is held while it
  runs. Starting it again (the Start menu shortcut, the logon task, by hand) opens the running
  one's window instead, waiting for it to answer if it's still loading the speech model; if
  the running one goes away meanwhile, the new one carries on. The window opens by itself
  only on the first run.
- **Without a desktop** (no tray possible), it runs as the server alone.

## The pages

- **Overview:** whether the headset is around and over which link (the dongle or your
  network), dictation (model, GPU or CPU), the recordings, this app's version with
  **Update** when a newer one is out, and the paired headsets (Unpair takes two clicks).
  Pairing by code, as `pair.ps1` did, is here too.
- **Headset setup:**
  - **Connect** to the headset by its address. The first time, its password (see "Before
    the first install" on the page: turning SSH on in Desktop Mode). A headset whose SSH
    identity the app doesn't know shows its fingerprint and **Trust this headset** (two
    clicks) instead of connecting.
  - **On the headset:** what's installed, and which version (the installer's `--status`:
    the installed copy of `install.sh`, or `~/src/cuff`'s on an older install). A check or
    install that fails or is cancelled keeps what was known.
  - **Install or update:** Cuff, plus the recorder, the panel helper and mpv if ticked,
    and "Pair with this PC". It shows the installer's output as it runs. The installer's
    questions come up as Yes and No buttons, and its password prompts (the panel helper's
    sudo) as a password field. **Uninstall…** takes two clicks.
- **Recordings:** the headset's clips and recordings, newest first, each with a thumbnail,
  length, size, and Download or Play (in the PC's own player, once downloaded) and Delete
  (two clicks; it warns when a video isn't on the PC yet).
  - **Download all new**, and the download folder (default `Videos\Cuff`; **Change…** opens
    Windows' folder picker).
  - **Download new ones automatically:** clips, recordings, screenshots, or any of them,
    checked every minute while the headset is connected, once a file hasn't changed for 20 s
    (so not a video being recorded). **Then delete them from the headset** deletes only what
    the automatic download fetched, after checking each download's size.
  - **New ones show up by themselves:** when the headset saves a screenshot, clip or
    recording, it sends Cuff for PC a signed nudge (`POST /recordings/changed`), and the list
    is read again straight away, so the page shows it without Refresh. With automatic
    downloads on, it's fetched then too: at once if it's settled, else once `SETTLED_S`
    (20 s) has passed. A headset older than v1.2 doesn't nudge; opening Recordings lists them.
  - **Videos | Screenshots:** a switch at the top shows one list at a time, with each one's
    count; the last choice is remembered. The Overview's counts link to each list
    (`#videos`, `#screenshots`).
  - **Screenshots:** the screenshot button's pictures, newest first, each
    with a thumbnail (the one-eye preview), its time and size, Download or Open (in the PC's
    own viewer, once downloaded) and Delete (two clicks, both files of the pair). Clicking
    the thumbnail shows it larger, one eye or both. **Download all new** fetches every one not
    on the PC yet.
- **Cuff settings:** everything Cuff's Settings tab and charms set on the wrist, read from
  the headset: wrist placement (sliders), the quick row (six slots), alerts and low
  battery levels, Record's settings, charms on or off, and the tab order. Changes collect
  until **Save to headset**. Most take effect at once. Charms and the tab order need a
  Cuff restart, which the page offers; it warns if a recording seems to be going, as a
  restart would stop it.
- **Connection:** how well the headset and the PC reach each other, measured from both ends
  every 15 s (a sample starts every 15 s, however long the last took), each link graded good, fair or poor by its weakest figure (signal under
  -60 dBm, a round trip over 8 ms or any loss makes it fair; under -72 dBm, over 30 ms or 5 %
  loss, poor).
  - **The dongle:**
    - **Figures:** round trip, jitter, loss and the signal at the headset.
    - **Radio:** band, channel and width.
    - **Rates:** what the headset sends and receives at (width, MCS, streams).
    - **The PC's adapter:** its signal and rate, and its USB speed (USB 2 is too slow for
      a full stream).
    - **Delivery:** how many of the headset's transmissions needed a retry, and how many
      failed, since the last sample (iw's own counts run from when the dongle joined).
  - **Your network:**
    - **Figures:** the same four, pinged at the headset's own Wi-Fi address (a name like
      frame.local can resolve to its dongle address instead).
    - **Details:** the network, band, and its rates.
  - **The last 10 minutes:** round trip and signal for both links.
  - **The Steam Link stream:** while SteamVR streams, its own view of the link (from the
    driver log: `linklog.py`). That's its bandwidth estimate, what the link carried, the video
    bitrate, and per minute the held frames, stalls, bitrate step-downs and bad-link events.
  - **Overview** sums it up under Headset: "dongle good, 2 ms · network good, 3 ms".
- **Type on the Frame:** what you type goes to whatever has focus in the headset.
  - **Type live:** click the box and type; letters go in bursts as you type, and Enter,
    Backspace, Tab, Esc, Delete, the arrows, Home and End as keys. Pasting sends the text.
    It shows only how many keys went, not what.
  - **Send text:** write it first, then send it in one go, optionally followed by Enter.
  - **Send the clipboard** (or Ctrl+Alt+V anywhere).
  - The headset can refuse all of it: Cuff settings > Typing from the PC.
- **Log:** Cuff for PC's log.

## How it works

| File | Role |
| --- | --- |
| `pc-server/app.py` | The desktop app: the server on a thread, the tray (`pystray`), the window (Edge `--app`), one copy |
| `pc-server/server.py` | The HTTP server: the headset's API, and `/app/*` for the page |
| `pc-server/appapi.py` | The page's API, and the guard on it (below) |
| `pc-server/app.html` | The page: plain HTML and JavaScript, no build step; polls `/app/state` every 2 s |
| `pc-server/frame.py` | The SSH link to the headset (`paramiko`): connecting, installing, pairing, settings |
| `pc-server/videos.py` | Recordings and screenshots: listing, thumbnails, downloads, automatic downloads |
| `pc-server/linkquality.py` | The Connection page: the PC's Wi-Fi adapters (Windows' WLAN API), the headset's radios (`iw` over SSH), round trips (Windows' ICMP API), and the stream's link (`stream.py`). No `netsh` or `ping` output is read: their labels follow the display language |
| `cuff/remote.py` | On the headset: `python -m cuff.remote dump / apply / videos`, run by `frame.py` |

- **The SSH link:**
  - **Keys:** the app has its own key (`%LOCALAPPDATA%\cuff-pc\frame_ed25519`, made on first
    use). The password typed the first time adds that key to the headset's
    `~/.ssh/authorized_keys` and is never stored. A key of your own in `~\.ssh` (or your SSH
    agent) works too, but only with a headset the app already knows: it's never offered to
    an unknown host.
  - **Host key:** the headset's identity. It's pinned in `frame.json` (`host_keys`, with the
    headset's name) at the first connection that works, and holds whatever address the
    headset has. A host key that isn't pinned is refused before any password or key is
    sent, so something else answering at the headset's address (say, ARP spoofing on the
    network) never gets the password. The page shows the refused key's fingerprint
    (`SHA256:...`, as `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` shows it on the
    headset) and **Trust this headset**, for a reinstalled or second headset. Only the very
    first headset is trusted on first use. Keys from the older `frame_known_hosts` are
    carried over once.
  - **Staying connected:** it reconnects every minute while the headset is about.
  - **One owner:** `frame.py` owns the SSH connection; `videos.py` and `linkquality.py` use
    its `run()`, `sftp()` and `channel()`, which fail cleanly (`ConnectionError`) when it's
    gone. Connecting and Disconnect are serialised: a reconnect under way when you click
    Disconnect is dropped, not left connected.
- **Installing:**
  - **Upload:** the app uploads its own copy of Cuff (the folder above `pc-server`, without
    `.git`, build output or media, plus a `VERSION` file) to `~/src/cuff`, replacing it.
  - **Run:** `install.sh --here` runs there on a terminal (a pty). An unfinished last line
    that stops for 0.4 s is a question: `[Y/n]` gives buttons, "password:" a hidden field,
    any other ending in `:` or `?` a text field. The terminal echoes answers, except
    passwords; nothing typed is logged.
- **Pairing over SSH:** the SSH login already proves it's your headset, so no code is
  needed.
  - **Token:** the app makes a token (`Pairing.add`) and writes it to the headset's
    `~/.config/cuff/pc-pairing.json` under the PC's name, then restarts Cuff. The headset
    binds it to this PC's id the first time this PC proves it holds it (docs/INTERNALS.md).
  - **Already paired:** a headset that already holds a working token for this PC is left
    alone.
- **Settings:** `cuff.remote dump` lists every setting with its type, choices, current value
  and default, in sections. `apply` takes `{section: {key: value}}`, writes the files Cuff
  uses (`placement.json`, `quick.json`, `alerts.json`, `charms.json`, a charm's own), then
  sends Cuff `SIGHUP`.
  - **Taking them up:** Cuff then reloads placement, alerts and the quick row, and calls
    each charm's `reload()`.
  - **Charm settings:** a charm's settings appear when its `charm.toml` names an options
    module (`options = "options"`, Record's `options.py`: `schema()` and `apply()`), or
    from its `[settings]` tables.
  - **Order:** the tab order is saved in `charms.json` and wins over `config.toml`'s `tabs`.
- **Recordings:**
  - **Listing:** `cuff.remote videos` lists `~/Videos/framecorder` and its `clips/`.
  - **Length and thumbnail:** the headset's `ffprobe` and `ffmpeg` give each video's length
    and a 320 px JPEG, once, kept in `%LOCALAPPDATA%\cuff-pc\thumbs`.
  - **Downloads:** they go over SFTP to `name.part`, then are renamed, keeping the video's
    date. A file there at the same size counts as downloaded.
  - **Screenshots:** `cuff.remote videos` also lists the screenshot folder (the profile's
    `screenshot_dir`) as pairs: `<time>-vr.png` (both eyes, side by side) and
    `<time>-preview.png` (one). A headset on v1.0.0, whose `cuff.remote` doesn't list them,
    has `~/Pictures/Frame screenshots` listed over SFTP instead. A download is both files, in
    the download folder's `screenshots\`. Thumbnails are the preview, shrunk to 480 px here
    (Pillow) and kept with the videos' ones; the larger view is the PNG itself, from the
    downloaded copy if there is one. Only names like `<time>-vr.png` and `<time>-preview.png`
    (letters, digits, `.`, `-`, `_`) are served, downloaded or deleted, and only in that
    folder. `/app/videos/screenshot`, an `<img>` source, takes the thumbnail key like
    `/app/videos/thumb`.
- **Heard from:** a paired headset says hello (a signed `GET /health`, which also checks the
  address is really this PC) once a minute, so the app and tray know it's about. Its address says the link: the Frame's own access
  point (10.35.78.0/24, which the dongle joins) is "over the dongle"; anything else is your
  network.
- **Typing** (`typing_link.py` on the PC, `cuff/pc.py` and `cuff/steam.py` on the headset):
  - **Queue:** the PC keeps what's sent for 20 s at most, so text typed while the headset
    is away doesn't turn up later somewhere unexpected.
  - **Announcing it:** while the Type page is open, or for 15 s after anything's sent, the
    announcement says `"typing": true`.
  - **Fetching:** the paired headset then long-polls `GET /typing/next?after=<seq>&run=<id>`
    (held up to 25 s), so it opens no port. It starts with whatever no headset has fetched
    yet, so a Ctrl+V sent with the window closed still arrives. The request is signed and so
    is the answer; the headset types nothing whose signature doesn't check out.
  - **Restarts:** `seq` starts from 0 again when the app restarts, so each run has a random
    id (`run`). A poll with another run's id, or a `seq` this run hasn't reached, starts over
    as a new headset does, instead of waiting for numbers the old run had reached.
  - **Typing it:** text goes in through Steam's `ControllerKeyboardSendText`, as dictation
    does. Keys go through `ControllerKeyboardSetKeyState` with USB HID key codes, as Steam's
    own on-screen keyboard does.
  - **On the wrist:** "Typing from <PC>" is pinned while text arrives, until 8 s after the
    last.
  - **Not logged:** neither side logs what's typed, only how many characters.

## Security

- **From the PC only:** every `/app` route answers only the PC itself (127.0.0.1).
- **Browser guard:** every route also needs a `Host` of 127.0.0.1 or localhost (against DNS
  rebinding). All but the page and thumbnails need the header `X-Cuff-App: 1`. Another web
  page can't send that without the browser asking first (CORS preflight, which the server
  never allows), so only the app page can drive the headset.
  - **Thumbnails** (`<img>`, which can't send a header) carry this run's random key instead,
    from `/app/state`, which only the page can read.
  - **The other local routes** (`/pair/open`, `/pair/confirm`, and `/stream`, `/transcribe`,
    `/typing/next` from the PC itself) have the same guard; `pair.ps1` sends the header.
  - **JSON** bodies must be sent as `application/json`, which a plain web form can't.
- **Secrets:** tokens never reach the page (a headset is named by a hash of its token), and
  passwords are used once and never stored or logged.

## The installer

`pc-server/installer/`: an Inno Setup script (`cuff-pc.iss`) and its build (`build.ps1`).

- **Building:** `powershell -ExecutionPolicy Bypass -File pc-server\installer\build.ps1`
  makes `installer\output\CuffForPC-Setup-<version>.exe` and its SHA-256,
  `CuffForPC-Setup-<version>.exe.sha256`. It needs Inno Setup 6
  (`winget install --id JRSoftware.InnoSetup -e`) and a committed tree.
  - **Version:** a release build is of a commit tagged exactly `vX.Y.Z`; anything else
    (untagged, commits after a tag) is refused. Test builds: `-Version vX.Y.Z-rcN` (a
    release candidate, which an installed copy counts as older than `vX.Y.Z`), or
    `-AllowDev` for any version.
  - **Staging:** `tools/publish.sh --stage` stages the public copy of the current commit,
    checked for private files, with a `VERSION` file.
  - **Adding:** it adds the `uv.exe` on PATH and an icon drawn by `app.py`.
  - **Without Inno Setup:** `-NoCompile` stops after staging.
- **Installing:** per user, into `%LOCALAPPDATA%\Programs\Cuff for PC`. It needs no admin,
  except one prompt for the firewall rules when they're missing.
  - **Setup:** it registers the logon task and a Start menu shortcut, then starts the app.
  - **First run:** `bootstrap.py` downloads the Python packages (about 2.5 GB, mostly CUDA
    for dictation), in a window with progress. Quicker syncs show no window.
  - **Nothing to install first:** uv comes with it, and uv fetches Python itself.
  - **Downloads stay in the app's folder:** `run-server.ps1` points uv's cache, the Python
    uv fetches and the speech model (Hugging Face) at `cache\` in the install folder, so
    uninstalling removes them. A git checkout uses their usual places.
  - **Not PyInstaller:** a frozen bundle with CUDA is as big, and often trips antivirus.
- **Updating:** once a day (and from Overview's **Check for updates**) the app asks GitHub
  for the public repo's latest release.
  - **Which count:** only version tags (`v1.2.0`). Builds from untagged commits never count
    as older.
  - **Which are offered:** a release carrying `CuffForPC-Setup-<tag>.exe` and
    `CuffForPC-Setup-<tag>.exe.sha256`, both named for its own tag. Without them, Overview
    says what's missing and offers nothing.
  - **Installing it:** **Update** (not while a headset job such as an install is running)
    downloads it into a folder of its own in `%TEMP%`, as `.part` until it's whole, with a
    timeout, and checks its size and SHA-256. Then it runs it silently. It stops the app,
    replaces the code (the old version's code files go first; the downloaded packages
    stay), and starts the app again. If the app is still running 5 minutes later, Overview
    says the update didn't happen, with the installer's path.
  - **Outside the logon task:** the app runs in its logon task's job object, which the
    installer stops, so the installer is started outside it (`CREATE_BREAKAWAY_FROM_JOB`).
    Where the job doesn't allow that, a one-shot scheduled task, **Cuff for PC update**,
    starts it instead (uninstalling removes that task).
  - **Releasing:** tag the commit (`v1.0.0`), build, and attach the .exe and the .exe.sha256
    to a GitHub release of that tag on the public repo.
  - **A git checkout** (no `VERSION`, a `.git`) updates with git instead.
- **Uninstalling** (Windows' Apps list) removes the app, its packages, uv's cache, the Python
  uv fetched and the speech model (`cache\`, even on a silent uninstall: they're downloads),
  the logon task and the firewall rules (with one admin prompt). It asks whether to delete `%LOCALAPPDATA%\cuff-pc`
  as well (the paired headsets, the app's SSH key, settings). A silent uninstall keeps it.
  Downloaded videos stay.
- **Starting and stopping it:**
  - **Starting:** the installer starts the app through its logon task, as at logon
    (`install-autostart.ps1 -Start`). Launched straight from Setup, its hidden console
    didn't survive Setup closing. An instance of the task still running would make Task
    Scheduler ignore the start, so after 20 s it allows a second instance for that one
    start (the app itself still runs only once).
  - **Stopping:** before replacing files (installing, updating or uninstalling), it asks
    whichever Cuff for PC holds the port to quit (`POST /app/quit`), a git checkout's
    included, then stops the task and anything still running from the install folder (not
    itself or Setup). The folder reaches PowerShell in an environment variable, so a path
    with an apostrophe or brackets works. When Setup itself is in a job object (an older
    Cuff for PC may have started it inside its task), it doesn't stop the task, which
    would end Setup too.
  - **First-run log:** first-run setup writes to `%LOCALAPPDATA%\cuff-pc\setup.log`.
- **Tested** (2026-10-26, builds v0.0.1 and v0.0.2 on the development PC):
  - **Install:** a silent install over a running git checkout's copy. It skipped the
    firewall step because the rules exist, and set up the files, `VERSION`, `uv.exe`, the
    logon task, the shortcut and the Apps entry. First-run setup from a deleted `.venv`
    worked, and so did dictation on the GPU and the reconnect to the headset.
  - **Update:** v0.0.2 over a running v0.0.1. The old copy quit, the packages were kept,
    and v0.0.2 came back up.
  - **The app's own update path:** download, size check, and a silent run. It was fed the
    v0.0.2 installer as if from GitHub, and a wrong size stops it.
  - **Silent uninstall:** it removed the folder, task, shortcut and Apps entry, and kept
    `%LOCALAPPDATA%\cuff-pc` and the firewall rules.
  - **Not tested yet:** an interactive install, which asks for admin when the firewall
    rules are missing. Also an interactive uninstall, which asks about your data and for
    admin to remove the firewall rules. Also a real GitHub release.
  - **Since changed, not yet tested installed:** the checksum, the start outside the logon
    task's job (and the one-shot task fallback), `-Start`, the app-folder `cache\` and its
    removal, and StopApp's new quoting. The update logic was tested against a local fake
    release (checksum right, wrong or missing, a misnamed asset, a stalled download, a
    double click), and StopApp's command against a folder named with an apostrophe and
    brackets.

## Still to build

- **An update notice on the wrist** when Cuff for PC carries a newer Cuff than the headset
  has. The PC side shows it already: `install.sh` keeps the installed `VERSION`, `--status`
  reports it, and Headset setup's button becomes **Update** (Overview says "update ready")
  while the headset's Cuff differs from the PC's.