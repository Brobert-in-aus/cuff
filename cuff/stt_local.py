"""On-headset speech-to-text for the dictation button: a helper process, so the model's
~1.4 GB lives only while dictation is in use.

  python -m cuff.stt_local             serve: one JSON request per stdin line, one reply per line
  python -m cuff.stt_local --download  fetch the model into the Hugging Face cache and exit

Requests are {"wav": path} (16-bit PCM WAV); replies {"text", "audio_s", "infer_ms"} or
{"error"}. It prints {"ready": true} once the model has loaded, and exits after IDLE_S
without a request (or when stdin closes), which gives the memory back.

The model is NVIDIA Parakeet TDT 0.6B v2, int8, through onnx-asr on the CPU: on the Steam
Frame about 0.3 s for 2 s of speech, and about 2 s to load once cached.
"""
import json
import select
import sys
import time
import wave

MODEL, QUANTIZATION = "nemo-parakeet-tdt-0.6b-v2", "int8"
IDLE_S = 300


def load():
    import onnx_asr
    return onnx_asr.load_model(MODEL, quantization=QUANTIZATION, providers=["CPUExecutionProvider"])


def read_wav(path):
    """Mono float32 at 16 kHz."""
    import numpy as np
    with wave.open(path) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"need 16-bit PCM WAV, got {width * 8}-bit")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        n = int(round(len(audio) * 16000 / rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def reply(obj):
    print(json.dumps(obj), flush=True)


def serve():
    started = time.perf_counter()
    model = load()
    print(f"stt_local: {MODEL} ({QUANTIZATION}) loaded in {time.perf_counter() - started:.1f} s",
          file=sys.stderr, flush=True)
    reply({"ready": True})
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], IDLE_S)
        if not ready:
            return  # idle: exit and free the model
        line = sys.stdin.readline()
        if not line:
            return
        try:
            audio = read_wav(json.loads(line)["wav"])
            t = time.perf_counter()
            text = model.recognize(audio, sample_rate=16000).strip() if len(audio) >= 1600 else ""
            reply({"text": text, "audio_s": round(len(audio) / 16000, 2),
                   "infer_ms": round((time.perf_counter() - t) * 1000)})
        except Exception as e:  # noqa: BLE001 - report it and keep serving
            reply({"error": repr(e)})


if __name__ == "__main__":
    if "--download" in sys.argv:
        load()
        print(f"stt_local: {MODEL} ({QUANTIZATION}) is cached", flush=True)
    else:
        serve()
