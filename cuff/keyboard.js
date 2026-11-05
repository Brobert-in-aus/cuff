// Cuff's dictation key on Steam's on-screen keyboard: injected into the "SteamVR - Keyboard"
// CEF page by keyboard.py. It sits over the left end of the space bar, the size of the key
// beside it, and calls window.__cuffDictate (a debugger binding) when pressed; keyboard.py
// toggles dictation and calls window.__cuffMic.set({phase, ready}) to show its state.
// It only relies on things Steam keeps stable: the page title and data-key attributes.
(() => {
  window.__cuffMicCleanup?.();
  document.getElementById("cuff-mic")?.remove();

  const css = document.createElement("style");
  css.id = "cuff-mic-style";
  css.textContent = `
    #cuff-mic { position: fixed; display: none; align-items: center; justify-content: center; z-index: 99998;
      box-sizing: border-box; cursor: pointer; user-select: none; transition: background-color 80ms; }
    #cuff-mic svg { width: 20px; height: 20px; }
    #cuff-mic.hover { background-color: rgb(26, 159, 255) !important; color: #fff !important; }
    #cuff-mic.listening { background-color: rgb(214, 54, 54) !important; color: #fff !important; }
    #cuff-mic.transcribing { background-color: rgb(232, 163, 61) !important; color: #fff !important; }
    #cuff-mic.off { opacity: 0.35; }
    #cuff-mic.down { background-color: #fff !important; color: rgb(26, 159, 255) !important; }`;
  document.getElementById("cuff-mic-style")?.remove();
  document.head.appendChild(css);

  const key = document.createElement("div");
  key.id = "cuff-mic";
  key.title = "Dictate";
  key.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/>
    <path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 18v4"/><path d="M8 22h8"/></svg>`;
  document.body.appendChild(key);

  // Over the space bar's left end, as wide as the key to its left (Steam's own switch key).
  function place() {
    const space = document.querySelector('[data-key=" "]');
    if (!space) { key.style.display = "none"; return; }
    const s = space.getBoundingClientRect();
    const beside = document.querySelector('[data-key="SwitchKeys_Steam"]')?.getBoundingClientRect();
    const w = Math.round(Math.min(beside ? beside.width : 73, s.width / 4));
    const face = space.firstElementChild || space;
    const look = getComputedStyle(face);
    Object.assign(key.style, {
      display: "flex", left: s.left + "px", top: s.top + "px", width: w - 1 + "px", height: s.height + "px",
      backgroundColor: look.backgroundColor, color: look.color, borderRadius: look.borderRadius,
    });
  }
  place();
  const placer = setInterval(place, 500); // Steam (or another add-on) may resize its rows

  const press = (e) => {
    e.preventDefault(); e.stopImmediatePropagation();
    key.classList.add("down"); setTimeout(() => key.classList.remove("down"), 120);
    try { window.__cuffDictate(""); } catch (err) { console.error("cuff mic", err); }
  };
  key.addEventListener("pointerdown", press, true);
  // Keep the press to ourselves: Steam mustn't see it as a space.
  const swallow = (e) => { if (e.target === key || key.contains(e.target)) { e.preventDefault(); e.stopImmediatePropagation(); } };
  const swallowed = ["pointerup", "mousedown", "mouseup", "click", "touchstart", "touchend"];
  for (const t of swallowed) window.addEventListener(t, swallow, true);

  // Hover: the laser may not give CSS :hover, so follow the pointer ourselves.
  let hoverTimer = 0;
  const onMove = (e) => {
    const r = key.getBoundingClientRect();
    key.classList.toggle("hover", e.clientX >= r.left && e.clientX < r.right && e.clientY >= r.top && e.clientY < r.bottom);
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => key.classList.remove("hover"), 1500);
  };
  for (const t of ["pointermove", "mousemove"]) window.addEventListener(t, onMove, true);

  window.__cuffMic = {
    set({ phase, ready }) {
      key.classList.toggle("listening", phase === "listening");
      key.classList.toggle("transcribing", phase === "transcribing");
      key.classList.toggle("off", !ready);
      key.title = phase === "listening" ? "Listening: tap to stop" : phase === "transcribing" ? "Transcribing" : "Dictate";
    },
  };
  window.__cuffMicCleanup = () => {
    clearInterval(placer); clearTimeout(hoverTimer);
    for (const t of swallowed) window.removeEventListener(t, swallow, true);
    for (const t of ["pointermove", "mousemove"]) window.removeEventListener(t, onMove, true);
    key.remove(); css.remove();
  };
  return "cuff mic key installed";
})()
