# Roadmap

Planned work, roughly in order. Nothing here is started unless it says so.

## Cuff for PC as a desktop app

Today Cuff for PC is a Python server plus PowerShell scripts (`install.ps1`, `pair.ps1`).
It needs uv, and is set up from a console. It should become an ordinary Windows app:

- **Install:** a normal installer, with nothing else to install first (no uv or console).
  It starts at logon and updates itself.
- **A tray icon and a window** showing:
  - whether a headset is connected, and over which link (Wi-Fi or the dongle);
  - the paired headsets, each with an Unpair button (instead of editing `paired.json`);
  - dictation status (model loaded, GPU or CPU), and the log.
- **Pairing in the app:** a Pair button opens the window that takes the code from the wrist,
  in place of `pair.ps1`.
- **Send keyboard input to the Frame:** a "Type on the Frame" mode, where what you type on
  the PC keyboard goes to whatever has focus in the headset. Useful for logins, search boxes
  and chat in games, where the SteamVR keyboard is slow.
- **Send the clipboard to the Frame:** a button (and a hotkey) that types the PC's
  clipboard text into the focused field on the Frame.

### How keyboard and clipboard could work

- **Getting there:** the headset already reaches the PC with its pairing token. It would
  hold one connection open to the PC, a WebSocket on the existing port, and the PC sends
  text and keys down it. The headset opens no port of its own.
- **Text:** it goes in the way dictation types today: Steam's
  `SteamClient.Input.ControllerKeyboardSendText` (`cuff/steam.py`), into the focused window.
- **Keys:** Enter, Backspace, Tab and the arrows need a separate path, still to find. A
  Steam input call would be first choice; `uinput` needs root on SteamOS.
- **You can see it:** the wrist shows "Typing from <PC>" while it's active, and Settings >
  Alerts gets a switch to turn remote typing off.
- **Only paired PCs** can send. Cuff never logs what's typed.

## Smaller items

- **Too-weak battery packs:** an alert when a pack can't keep up from the moment it's
  plugged in (the battery keeps falling while "plugged in"), not only when it runs flat.
