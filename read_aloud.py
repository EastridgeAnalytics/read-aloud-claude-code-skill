"""Read a local document, or the last Claude Code reply, aloud with the Kokoro voice model.

Everything runs on this PC. The reading process blocks its own network access before it loads
anything, so the text cannot leave the machine through this tool.

  read_aloud.py FILE [--voice NAME] [--speed 1.0] [--from N]   read .html, .md or .txt aloud
  read_aloud.py last                                           read the most recent reply in this session
  read_aloud.py pause | resume | toggle | faster | slower | speed 1.5 | skip | back | stop | status
  read_aloud.py FILE --save OUT.wav                            write the reading to a WAV file instead
  read_aloud.py FILE --dry-run                                 show what would be read, load nothing
  read_aloud.py --text "some words"                            speak a short piece of text
  read_aloud.py voices

Reading starts in a detached background process so the caller returns at once. --foreground blocks.
While a reading is in progress the keys in HOTKEYS work from any window.
"""
import argparse
import ctypes
import datetime
import glob
import html.parser
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "kokoro-v1.0.onnx")
VOICES = os.path.join(HERE, "models", "voices-v1.0.bin")
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "last.log")
CONFIG = os.path.join(HERE, "config.json")
CONTROL = os.path.join(HERE, "control.txt")        # commands for the reading in progress, one per line
LAST_REPLY = os.path.join(HERE, "last_reply.md")
RATE = 24000                                       # Kokoro's output sample rate
MIN_SPEED, MAX_SPEED, SPEED_STEP = 0.5, 2.0, 0.25  # 2.0 is the model's own ceiling
KEEP_BEHIND = 6                                    # pieces kept in memory for "back"
CONTROL_WORDS = {"pause", "resume", "toggle", "faster", "slower", "speed", "skip", "back"}
# (command, shown as, Windows key code); every one is Ctrl+Alt plus the key
HOTKEYS = [("pause/resume", "Ctrl+Alt+P", 0x50), ("faster", "Ctrl+Alt+=", 0xBB), ("slower", "Ctrl+Alt+-", 0xBD),
           ("skip", "Ctrl+Alt+]", 0xDD), ("back", "Ctrl+Alt+[", 0xDB), ("stop", "Ctrl+Alt+Q", 0x51)]
DEFAULTS = {"voice": "af_heart", "speed": 1.0, "lang": "en-us"}
MAX_CHARS = 280          # per synthesis call: short enough to start fast, long enough for natural phrasing
WORDS_PER_MINUTE = 165   # rough Kokoro pace at speed 1.0, used only for the time estimate

# ---------------------------------------------------------------- text extraction

BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "ul", "ol", "li", "dl", "dt",
              "dd", "table", "thead", "tbody", "tfoot", "figure", "figcaption", "blockquote", "details", "summary",
              "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "svg", "noscript", "template", "title", "nav"}   # nav: a list of page links, not prose


class _HtmlToBlocks(html.parser.HTMLParser):
    """Turn a page into a list of (kind, text) blocks: headings, paragraphs, list items and table rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf, self.kind = [], [], "p"
        self.skip, self.in_title, self.title = 0, False, ""
        self.row, self.cell, self.row_is_header, self.in_cell = None, [], True, False

    def _flush(self, kind=None):
        text = " ".join("".join(self.buf).split())
        self.buf = []
        if text:
            self.blocks.append((kind or self.kind, text))
        self.kind = "p"

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "pre":
            self._flush()
            self.blocks.append(("p", "Code block skipped."))
            self.skip += 1
        elif tag == "tr":
            self._flush()
            self.row, self.row_is_header = [], True
        elif tag in ("td", "th"):
            self.cell, self.in_cell = [], True
            if tag == "td":
                self.row_is_header = False
        elif tag == "cite":
            (self.cell if self.in_cell else self.buf).append(" ")
        elif tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADINGS:
                self.kind = "h"
            elif tag == "li":
                self.kind = "li"

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP_TAGS or (tag == "pre" and self.skip):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("td", "th") and self.in_cell:
            text = " ".join("".join(self.cell).split())
            if self.row is not None and text:
                self.row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            if self.row:
                if self.row_is_header:
                    self.blocks.append(("row", "Table columns: " + ", ".join(self.row) + "."))
                else:
                    self.blocks.append(("row", "; ".join(self.row) + "."))
            self.row = None
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip:
            return
        (self.cell if self.in_cell else self.buf).append(data)


def blocks_from_html(text):
    parser = _HtmlToBlocks()
    parser.feed(text)
    parser._flush()
    title = " ".join(parser.title.split())
    if title and not any(k == "h" for k, _ in parser.blocks[:6]):
        parser.blocks.insert(0, ("h", title))
    return parser.blocks


def blocks_from_markdown(text):
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)           # front matter
    text = re.sub(r"```.*?```", "\n\nCode block skipped.\n\n", text, flags=re.S)
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l.rstrip() for l in para.splitlines() if l.strip()]
        if not lines:
            continue
        if all(l.lstrip().startswith("|") for l in lines):                        # table
            for l in lines:
                cells = [c.strip() for c in l.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    continue
                blocks.append(("row", "; ".join(c for c in cells if c) + "."))
            continue
        items, plain = [], []
        for l in lines:
            m = re.match(r"\s*(#{1,6})\s+(.*)", l)
            if m:
                blocks.append(("h", m.group(2)))
                continue
            m = re.match(r"\s*(?:[-*+]|\d+[.)])\s+(.*)", l)
            if m:
                if plain:
                    blocks.append(("p", " ".join(plain))); plain = []
                items.append(m.group(1))
            elif items and l.startswith((" ", "\t")):
                items[-1] += " " + l.strip()
            else:
                plain.append(re.sub(r"^\s*>\s?", "", l).strip())
        if plain:
            blocks.append(("p", " ".join(plain)))
        blocks.extend(("li", i) for i in items)
    out = []
    for kind, t in blocks:
        t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)                           # images
        t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)                            # links
        t = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", t)                   # [[wiki links]]
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"(\*\*|__|\*|_|`|~~)", "", t)
        out.append((kind, t))
    return out


def blocks_from_file(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if ext in (".html", ".htm"):
        return blocks_from_html(text)
    if ext in (".md", ".markdown"):
        return blocks_from_markdown(text)
    if ext in (".txt", ".text", ".log", ""):
        return [("p", " ".join(p.split())) for p in re.split(r"\n\s*\n", text) if p.strip()]
    raise SystemExit(f"read-aloud can read .html, .md and .txt files; {ext} is not supported yet.")


REPLACEMENTS = [(r"https?://\S+", " link "), ("&", " and "), ("→", " to "), ("·", ", "), ("•", ", "),
                (r"\s[–—]\s", ", "), ("[–—]", ", "), ("[“”]", '"'), ("[‘’]", "'"),
                ("…", "..."), (r"(?<![\w/.:])/(?=[a-z][\w-]*)", " slash "), (r"\s*/\s*", " / "), ("�", " "), (r"\bvs\.?(?=\s)", "versus"), ("%", " percent")]
FILE_LIKE = re.compile(r"\S*[\\/_]\S*\.(?:html?|md|py|json|csv|xlsx?|pdf|txt|tsx?|jsx?|ya?ml|png|zip)\b", re.I)


def speakable(text):
    text = FILE_LIKE.sub(" a file ", text)
    for pattern, repl in REPLACEMENTS:
        text = re.sub(pattern, repl, text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Sk", "Cf", "Co", "Cn"))
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = text.rstrip(":;, ")
    if text and text[-1] not in ".!?":
        text += "."
    return text


def chunks_from_blocks(blocks):
    """Split blocks into pieces of at most MAX_CHARS, each with the pause (seconds) to leave after it."""
    out = []
    for kind, raw in blocks:
        text = speakable(raw)
        if not re.search(r"[A-Za-z0-9]", text):
            continue
        sentences = re.split(r'(?<=[.!?])["\')]?\s+(?=["\'(A-Z0-9])', text)
        pieces, cur = [], ""
        for s in sentences:
            while len(s) > MAX_CHARS:                                             # a very long sentence: cut at a comma
                cut = max(s.rfind(", ", 0, MAX_CHARS), s.rfind("; ", 0, MAX_CHARS), s.rfind(" ", 0, MAX_CHARS))
                cut = cut if cut > 40 else MAX_CHARS
                pieces.append((cur + " " + s[:cut + 1]).strip() if cur else s[:cut + 1].strip()); cur = ""
                s = s[cut + 1:].strip()
            if cur and len(cur) + 1 + len(s) > MAX_CHARS:
                pieces.append(cur); cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            pieces.append(cur)
        end_pause = {"h": 0.55, "row": 0.25, "li": 0.3}.get(kind, 0.4)
        for i, p in enumerate(pieces):
            out.append((p, end_pause if i == len(pieces) - 1 else 0.12))
    return out


def minutes_for(chunks, speed):
    words = sum(len(c.split()) for c, _ in chunks)
    return words / (WORDS_PER_MINUTE * speed)

# ---------------------------------------------------------------- the last reply in this Claude Code session


def _entry_kind(entry):
    """Classify one transcript line: a finished piece of reply text, a tool call, a tool result, or a prompt."""
    if entry.get("isSidechain"):
        return None, ""
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") == "assistant" and isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_use" for b in blocks):
            return "tool", ""
        text = "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return ("text", text) if text else ("thinking", "")
    if entry.get("type") == "user":
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "result", ""
        return "prompt", ""
    return None, ""


def _reply_from(path, tail_bytes):
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - tail_bytes) if tail_bytes else 0)
        raw = fh.read().decode("utf-8", errors="replace")
    lines = raw.split("\n")
    if tail_bytes and size > tail_bytes:
        lines = lines[1:]                      # the first line of a tail read is cut off
    kinds = []
    for line in lines:
        try:
            kind, text = _entry_kind(json.loads(line))
        except ValueError:
            continue
        if kind:
            kinds.append((kind, text))
    end = len(kinds)
    if kinds and kinds[-1][0] != "text":       # a tool is running right now, so the newest text belongs to this turn
        while end and kinds[end - 1][0] != "prompt":
            end -= 1
        while end and kinds[end - 1][0] == "prompt":
            end -= 1
    i = end - 1
    while i >= 0 and kinds[i][0] != "text":    # a turn that ended on a tool call: go back to the nearest text
        i -= 1
    parts = []
    while i >= 0 and kinds[i][0] in ("text", "thinking"):
        if kinds[i][0] == "text":
            parts.append(kinds[i][1])
        i -= 1
    return "\n\n".join(reversed(parts)).strip()


def last_reply_text():
    """The most recent finished reply in the current Claude Code session, read from its local transcript."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    paths = glob.glob(os.path.join(base, "*", session + ".jsonl")) if session else []
    if not paths:
        paths = sorted(glob.glob(os.path.join(base, "*", "*.jsonl")), key=os.path.getmtime)[-1:]
    if not paths:
        raise SystemExit("No Claude Code session transcript was found.")
    for tail_bytes in (4_000_000, None):
        text = _reply_from(paths[0], tail_bytes)
        if text:
            return re.split(r"\n\s*\**Sources:?\**\s*\n", text)[0]      # a list of links is not worth hearing
    raise SystemExit("No reply was found in this session yet.")

# ---------------------------------------------------------------- state, controls, status


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return dict(default)


def save_json(path, **fields):
    data = load_json(path, {})
    data.update(fields)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def save_state(**fields):
    save_json(STATE, **fields)


def reader_alive(pid):
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return "python" in out.lower()


def reading_now():
    state = load_json(STATE, {})
    return state if state.get("status") == "reading" and reader_alive(state.get("pid")) else None


def describe(state):
    where = f"piece {state.get('chunk')} of {state.get('total')}"
    return f"{'Paused at' if state.get('paused') else 'Reading'} {where}, speed {state.get('speed')}: {state.get('file')}"


def stop(quiet=False):
    state = reading_now()
    if not state:
        if not quiet:
            print("Nothing is being read right now.")
        return
    with open(CONTROL, "a", encoding="utf-8") as fh:
        fh.write("stop\n")
    for _ in range(15):                        # give the reader a moment to close the sound device itself
        if not reader_alive(state["pid"]):
            break
        time.sleep(0.1)
    else:
        subprocess.run(["taskkill", "/PID", str(state["pid"]), "/F"], capture_output=True)
    save_state(status="stopped", paused=False)
    if not quiet:
        print(f"Stopped at piece {state.get('chunk')} of {state.get('total')}: {state.get('file')}")


def control(word, value=None):
    """Send pause, resume, toggle, faster, slower, speed, skip or back to the reading in progress."""
    if word == "speed":
        try:
            value = min(MAX_SPEED, max(MIN_SPEED, float(value)))
        except (TypeError, ValueError):
            raise SystemExit(f"Give a speed between {MIN_SPEED} and {MAX_SPEED}, for example: speed 1.5")
    if not reading_now():
        if word == "speed":
            save_json(CONFIG, speed=value)
            return print(f"Nothing is being read. Default speed is now {value}.")
        return print("Nothing is being read right now.")
    with open(CONTROL, "a", encoding="utf-8") as fh:
        fh.write(f"{word} {value if value is not None else ''}\n")
    time.sleep(0.6)
    print(describe(load_json(STATE, {})))


def status():
    state = reading_now()
    if not state:
        last = load_json(STATE, {})
        return print(f"Not reading. Last: {last.get('status', 'never run')} {last.get('file', '')}".strip())
    print(describe(state))
    keys = [label for label, ok in (state.get("hotkeys") or {}).items() if ok]
    if keys:
        print("Keys while reading: " + ", ".join(f"{label} {name}" for name, label, _ in HOTKEYS if label in keys))

# ---------------------------------------------------------------- sound output and hotkeys (Windows built-ins)


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD), ("dwBytesRecorded", wintypes.DWORD),
                ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class Speaker:
    """Plays 16-bit mono sound through the Windows wave-out device, which can pause and resume mid-sentence."""
    DONE = 0x1

    def __init__(self, rate):
        self.mm = mm = ctypes.WinDLL("winmm")
        mm.waveOutOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_uint, ctypes.POINTER(WAVEFORMATEX),
                                   ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        for fn in (mm.waveOutPrepareHeader, mm.waveOutWrite, mm.waveOutUnprepareHeader):
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        for fn in (mm.waveOutPause, mm.waveOutRestart, mm.waveOutReset, mm.waveOutClose):
            fn.argtypes = [wintypes.HANDLE]
        fmt = WAVEFORMATEX(1, 1, rate, rate * 2, 2, 16, 0)
        self.h = wintypes.HANDLE()
        if mm.waveOutOpen(ctypes.byref(self.h), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0):
            raise SystemExit("Could not open a sound output device.")

    def play(self, pcm, tick):
        """Play one piece. tick() is called about 50 times a second and may return 'cut' or 'stop'."""
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        hdr = WAVEHDR(lpData=ctypes.cast(buf, ctypes.c_void_p), dwBufferLength=len(pcm))
        size = ctypes.sizeof(hdr)
        if self.mm.waveOutPrepareHeader(self.h, ctypes.byref(hdr), size) or self.mm.waveOutWrite(self.h, ctypes.byref(hdr), size):
            raise SystemExit("The sound device refused the audio.")
        action = None
        while not hdr.dwFlags & self.DONE:
            time.sleep(0.02)
            if action is None:
                action = tick()
                if action:
                    self.cut()
        self.mm.waveOutUnprepareHeader(self.h, ctypes.byref(hdr), size)
        return action

    def pause(self):
        self.mm.waveOutPause(self.h)

    def resume(self):
        self.mm.waveOutRestart(self.h)

    def cut(self):
        self.mm.waveOutReset(self.h)
        self.mm.waveOutRestart(self.h)

    def close(self):
        self.mm.waveOutReset(self.h)
        self.mm.waveOutClose(self.h)


def start_hotkeys(commands):
    """Register the reading keys for as long as this process lives. Windows reports only these exact
    combinations to us; nothing else that is typed is seen."""
    result, registered = {}, threading.Event()

    def loop():
        user32 = ctypes.WinDLL("user32")
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        ctrl_alt_norepeat = 0x0002 | 0x0001 | 0x4000
        for n, (name, label, key) in enumerate(HOTKEYS, 1):
            result[label] = bool(user32.RegisterHotKey(None, n, ctrl_alt_norepeat, key))
        result["thread"] = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        registered.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and 1 <= msg.wParam <= len(HOTKEYS):
                commands.put((HOTKEYS[msg.wParam - 1][0].replace("pause/resume", "toggle"), None))

    threading.Thread(target=loop, daemon=True).start()
    registered.wait(2)
    return result

# ---------------------------------------------------------------- speech


def block_network():
    """Make this process unable to open a network connection, before any third-party code loads."""
    import socket

    def refused(*args, **kwargs):
        raise OSError("read-aloud runs with its network access switched off")

    class NoNetworkSocket(socket.socket):       # still a class, so modules that subclass socket (ssl) import
        def __init__(self, *args, **kwargs):
            refused()

    socket.socket = NoNetworkSocket
    socket.create_connection = refused
    socket.getaddrinfo = refused


def load_voice_model(voice=None):
    block_network()
    import numpy as np
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(MODEL, VOICES)
    if voice and voice not in kokoro.get_voices():
        raise SystemExit(f"Voice {voice} not found. Run: read-aloud voices")
    return kokoro, np


def pcm_bytes(np, samples, rate, pause):
    samples = np.concatenate([samples, np.zeros(int(pause * rate), dtype=samples.dtype)])
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def save_wav(chunks, voice, speed, lang, path):
    kokoro, np = load_voice_model(voice)
    t0, frames = time.time(), []
    for i, (text, pause) in enumerate(chunks, 1):
        samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        frames.append(pcm_bytes(np, samples, rate, pause))
        print(f"  {i}/{len(chunks)}", end="\r", flush=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(b"".join(frames))
    seconds = sum(len(f) for f in frames) / 2 / RATE
    print(f"Saved {seconds / 60:.1f} minutes of audio to {path} (made in {time.time() - t0:.0f}s)")


def read_chunks(chunks, voice, speed, lang, label, start=0, hotkeys=True):
    """Play the pieces in order. A second thread makes speech a few pieces ahead of the one being heard.
    Pause, resume, speed changes, skip, back and stop arrive from hotkeys or from the control file."""
    import winsound
    kokoro, np = load_voice_model(voice)
    lock, wake = threading.Lock(), threading.Event()
    ctl = {"speed": speed, "gen": 0, "pos": start, "done": False}
    ready, commands = queue.Queue(maxsize=3), queue.Queue()

    def produce():
        gen, idx = 0, start
        while not ctl["done"]:
            with lock:
                if gen != ctl["gen"]:                       # the listener jumped or changed speed: start again there
                    gen, idx = ctl["gen"], ctl["pos"]
                spd = ctl["speed"]
            if idx >= len(chunks):
                wake.wait(0.2); wake.clear()
                continue
            text, pause = chunks[idx]
            try:
                samples, rate = kokoro.create(text, voice=voice, speed=spd, lang=lang)
                item = (gen, idx, pcm_bytes(np, samples, rate, pause))
            except Exception as error:                      # hand the failure to the player instead of leaving it waiting
                item = (gen, idx, error)
            while not ctl["done"]:
                with lock:
                    if gen != ctl["gen"]:
                        break
                try:
                    ready.put(item, timeout=0.1)
                    idx += 1
                    break
                except queue.Full:
                    continue

    try:
        os.remove(CONTROL)
    except OSError:
        pass
    speaker = Speaker(RATE)
    keys = start_hotkeys(commands) if hotkeys else {}
    save_state(pid=os.getpid(), file=label, total=len(chunks), chunk=start, status="reading", paused=False,
               voice=voice, speed=speed, hotkeys={k: v for k, v in keys.items() if k != "thread"},
               hotkey_thread=keys.get("thread"), started=datetime.datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=produce, daemon=True).start()
    now = {"i": start, "next": start, "paused": False, "polled": 0.0}
    history = {}                                            # the last few pieces, so going back is instant
    t0 = time.time()

    def pending():
        out = []
        while True:
            try:
                out.append(commands.get_nowait())
            except queue.Empty:
                break
        if time.time() - now["polled"] >= 0.1:
            now["polled"] = time.time()
            taken = CONTROL + ".taken"
            try:
                os.replace(CONTROL, taken)
                with open(taken, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.split()
                        if parts:
                            out.append((parts[0], parts[1] if len(parts) > 1 else None))
                os.remove(taken)
            except OSError:
                pass
        return out

    def jump(to, new_speed=None):
        with lock:
            if new_speed is not None:
                ctl["speed"] = new_speed
            ctl["gen"] += 1
            ctl["pos"] = to
        while True:
            try:
                ready.get_nowait()
            except queue.Empty:
                break
        wake.set()
        now["next"] = to

    def tick():
        action = None
        for name, arg in pending():
            if name in ("pause", "resume", "toggle"):
                want = (not now["paused"]) if name == "toggle" else name == "pause"
                if want != now["paused"]:
                    now["paused"] = want
                    speaker.pause() if want else speaker.resume()
                    save_state(paused=want)
                    print(f"{time.time() - t0:7.1f}s {'paused' if want else 'resumed'}", flush=True)
            elif name == "stop":
                return "stop"
            elif name in ("faster", "slower", "speed"):
                step = {"faster": SPEED_STEP, "slower": -SPEED_STEP}.get(name)
                try:
                    target = ctl["speed"] + step if step else float(arg)
                except (TypeError, ValueError):
                    continue
                target = round(min(MAX_SPEED, max(MIN_SPEED, target)), 2)
                if target == ctl["speed"]:
                    winsound.MessageBeep()                  # already at the limit
                    continue
                history.clear()                             # pieces already made are at the old speed
                jump(now["i"], target)                      # say the current piece again at the new speed
                save_json(CONFIG, speed=target)             # like a podcast app: the speed you set is kept
                save_state(speed=target)
                print(f"{time.time() - t0:7.1f}s speed {target}", flush=True)
                action = "cut"
            elif name in ("skip", "back"):
                to = now["i"] + 1 if name == "skip" else max(0, now["i"] - 1)
                if to >= len(chunks):
                    winsound.MessageBeep()
                    continue
                now["next"] = to                            # the main loop finds it in memory or in the queue
                print(f"{time.time() - t0:7.1f}s {name} to piece {to + 1}", flush=True)
                action = "cut"
        if action == "cut" and now["paused"]:               # jumping while paused starts the sound again
            now["paused"] = False
            speaker.resume()
            save_state(paused=False)
        return action

    outcome = "finished"
    while now["next"] < len(chunks):
        want, action = now["next"], None
        item = history.get(want)
        while item is None:                                 # wait for the piece, still listening for commands
            action = tick()
            if action == "stop" or now["next"] != want:
                break
            try:
                gen, i, data = ready.get(timeout=0.05)
            except queue.Empty:
                continue
            with lock:
                fresh = gen == ctl["gen"]
            if not fresh:
                continue
            if isinstance(data, Exception):
                raise data
            history[i] = data
            for old in [k for k in history if k < i - KEEP_BEHIND]:
                del history[old]
            if i == want:
                item = data
            elif i > want:                                  # the piece wanted is behind what is being made
                jump(want)
        if action == "stop":
            outcome = "stopped"
            break
        if item is None:
            continue                                        # the listener moved while we waited
        now["i"] = want
        save_state(chunk=want + 1)
        print(f"{time.time() - t0:7.1f}s piece {want + 1} ({len(item) / 2 / RATE:.1f}s of audio)", flush=True)
        action = speaker.play(item, tick)
        if action == "stop":
            outcome = "stopped"
            break
        if action != "cut":
            now["next"] = want + 1
    ctl["done"] = True
    speaker.close()
    save_state(status=outcome, paused=False)
    print(f"{time.time() - t0:7.1f}s {outcome}", flush=True)


def spawn_detached(argv):
    """Start the reader as its own windowless process so it outlives the command that launched it."""
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = exe if os.path.exists(exe) else sys.executable
    cmd = [exe, os.path.abspath(__file__), *argv, "--foreground"]
    log = open(LOG, "w", encoding="utf-8")
    detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
    for flags in (detached | new_group | breakaway, detached | new_group):
        try:
            return subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags,
                                    close_fds=True, cwd=HERE)
        except OSError:
            continue
    raise SystemExit("Could not start the background reader.")

# ---------------------------------------------------------------- command line


def main():
    if os.name != "nt":
        raise SystemExit("read-aloud works on Windows only for now. macOS and Linux need a different sound and hotkey layer.")
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = {**DEFAULTS, **load_json(CONFIG, {})}
    ap = argparse.ArgumentParser(description="Read a local document, or the last Claude Code reply, aloud. Offline.")
    ap.add_argument("target", nargs="?", help="a file, 'last', or: " + ", ".join(sorted(CONTROL_WORDS | {"stop", "status", "voices"})))
    ap.add_argument("value", nargs="?", help="the number for 'speed'")
    ap.add_argument("--text")
    ap.add_argument("--voice", default=cfg["voice"])
    ap.add_argument("--speed", type=float, default=cfg["speed"])
    ap.add_argument("--from", dest="start", type=int, default=1, help="piece number to start from")
    ap.add_argument("--limit", type=int, help="read only this many pieces")
    ap.add_argument("--save", help="write a WAV file instead of playing")
    ap.add_argument("--label", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--voices", action="store_true")
    a = ap.parse_args()
    word = "stop" if a.stop else "status" if a.status else "voices" if a.voices else (a.target or "").lower()

    if word == "stop":
        return stop()
    if word == "status":
        return status()
    if word == "voices":
        kokoro, _ = load_voice_model()
        return print("\n".join(kokoro.get_voices()))
    if word in CONTROL_WORDS:
        return control(word, a.value)
    if not a.target and not a.text:
        ap.error("give a file to read, 'last' for the most recent reply, or --text")
    if not MIN_SPEED <= a.speed <= MAX_SPEED:
        raise SystemExit(f"Speed must be between {MIN_SPEED} and {MAX_SPEED}.")

    if a.text:
        blocks, label, source = [("p", a.text)], "(text)", None
    elif word == "last":
        with open(LAST_REPLY, "w", encoding="utf-8") as fh:
            fh.write(last_reply_text())
        blocks, label, source = blocks_from_file(LAST_REPLY), "the last reply", LAST_REPLY
    else:
        source = os.path.abspath(a.target)
        if not os.path.isfile(source):
            raise SystemExit(f"No such file: {source}")
        blocks, label = blocks_from_file(source), a.label or source
    chunks = chunks_from_blocks(blocks)
    if a.limit:
        chunks = chunks[:a.start - 1 + a.limit]
    if not chunks:
        raise SystemExit("Nothing readable was found.")
    start = max(0, min(a.start - 1, len(chunks) - 1))
    minutes = max(1, round(minutes_for(chunks[start:], a.speed)))
    summary = f"{len(chunks)} pieces, about {minutes} minute{'' if minutes == 1 else 's'} at speed {a.speed}"

    if a.dry_run:
        print(f"{label}\n{summary}\n")
        for i, (text, pause) in enumerate(chunks, 1):
            print(f"[{i}] {text}")
        return
    if a.save:
        return save_wav(chunks[start:], a.voice, a.speed, cfg["lang"], a.save)
    stop(quiet=True)
    if a.foreground:
        return read_chunks(chunks, a.voice, a.speed, cfg["lang"], label, start, cfg.get("hotkeys", True))

    worker = ["--text", a.text] if a.text else [source, "--label", label]   # absolute path: the worker runs elsewhere
    worker += ["--voice", a.voice, "--speed", str(a.speed), "--from", str(start + 1)]
    if a.limit:
        worker += ["--limit", str(a.limit)]
    spawn_detached(worker)
    keys = ", ".join(f"{label_} {name}" for name, label_, _ in HOTKEYS) if cfg.get("hotkeys", True) else "off"
    print(f"Reading aloud: {label}\n{summary}, voice {a.voice}.\nKeys while reading: {keys}\n"
          f"Or: read-aloud pause | resume | faster | slower | speed 1.5 | skip | back | stop")


if __name__ == "__main__":
    main()
