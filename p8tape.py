#!/usr/bin/env python3
"""
p8tape - load PICO-8 carts from cassette tape, ZX Spectrum style.

  python3 p8tape.py
        -> opens the loader window: pick a sound input or a WAV file, then
           press "Boot PICO-8 loader".

  python3 p8tape.py encode mygame.p8.png --screen title.png
        -> mygame.wav: a loading picture followed by the game. Record it onto tape.

  python3 p8tape.py play
        -> starts PICO-8 running loader.p8 and feeds it the tape from your
           sound card's input. The loading picture builds up on screen as the
           tape plays, then the game starts.

  python3 p8tape.py play --wav mygame.wav
        -> the same, playing a WAV file instead of a real tape (good for testing).

  python3 p8tape.py decode recording.wav
        -> just save the cart (and a PNG of the loading picture) from a WAV.

Requirements: numpy and pillow; live tape input also needs sounddevice.
    pip install numpy pillow sounddevice

How it works
------------
Tape signal: every bit is one sine cycle, 0 = short (~2400 Hz), 1 = long (~1200 Hz).
A tape is: a header block (~5 s pilot, just the name, shown as "Found: name"),
a pause (3 s by default), the optional picture block, then the cart block.
Each block is a pilot tone (~2 s, or ~5 s for the header), a sync word, then:
    "P8T2" | type (1 = picture, 2 = cart, 3 = header) | name length | name | size (4) | CRC32
    payload | CRC32
The picture payload is: mode (5), x, y, w, h, then the black-and-white outline
(rows in the Spectrum's interleaved order) interleaved with the full colours, so
the outline draws slowly through the whole load while the colour arrives out of
sight. A short final section then sweeps the colour down the screen while the
data is still playing, like the Spectrum's colour data.

Link to PICO-8: p8tape starts PICO-8 itself with `-p tape` and pipes 285-byte
packets into its standard input (each byte sent as two characters '@'..'O' so
nothing gets mangled on Windows). The cart reads them with serial(0x804, ...)
and asks for the next packet by writing byte 0xF7 to its standard output.
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
import zlib
from collections import deque

import numpy as np

FROZEN = getattr(sys, "frozen", False)      # running as a packaged program
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

if FROZEN and sys.platform.startswith("linux"):
    # the packaged Linux program carries its own PortAudio sound library
    import ctypes.util
    _pa = os.path.join(BUNDLE, "libportaudio.so.2")
    if os.path.exists(_pa):
        _find = ctypes.util.find_library
        ctypes.util.find_library = lambda name: _pa if name == "portaudio" else _find(name)

RATE = 44100
F0 = 2400            # bit 0
F1 = 1200            # bit 1
PILOT_BITS = 4800    # ~2 seconds of pilot tone
PILOT_MIN = 512      # steady cycles needed before we believe it's a pilot

# Tape speeds: every tone is scaled by the factor. The loader measures the
# pilot tone, so it loads any of these without being told which.
SPEEDS = {
    "slow": (0.5, "Slow"),
    "spectrum": (0.85, "Spectrum pitch"),
    "standard": (1.0, "Standard"),
    "turbo": (2.0, "Turbo"),
}
SPEED_MIN, SPEED_MAX = 0.5, 2.0


def speed_baud(factor):
    """Average bits per second at a speed (half 0s, half 1s)."""
    n0, n1 = round(RATE / (F0 * factor)), round(RATE / (F1 * factor))
    return 2 * RATE / (n0 + n1)


def speed_label(name):
    factor, title = SPEEDS[name]
    return f"{title} - about {round(speed_baud(factor), -1):,.0f} baud"
SYNC = 0xD35C
MAGIC = b"P8T2"
MAX_SIZE = 1 << 20
NAME_RE = re.compile(r"^[a-z0-9-]{1,24}\.p8(\.png)?$")

T_SCREEN, T_CART, T_HEADER = 1, 2, 3
HEADER_PILOT_BITS = 12000   # ~5 s, like the Spectrum's longer header pilot
# Loading pictures are always the full 128x128 screen. While loading, the
# loader draws its raster bars over the outer 4 px on every side, so keep
# anything important inside the middle 120x120.

PAL = [(0, 0, 0), (29, 43, 83), (126, 37, 83), (0, 135, 81), (171, 82, 54),
       (95, 87, 79), (194, 195, 199), (255, 241, 232), (255, 0, 77),
       (255, 163, 0), (255, 236, 39), (0, 228, 54), (41, 173, 255),
       (131, 118, 156), (255, 119, 168), (255, 204, 170)]


# ---------------------------------------------------------------- names

def tape_name(path):
    """mygame.p8.png -> 'mygame.p8.png', made safe for PICO-8."""
    base = os.path.basename(path).lower()
    for ext in (".p8.png", ".p8"):
        if base.endswith(ext):
            stem = base[: -len(ext)]
            break
    else:
        sys.exit(f"{path}: expected a .p8 or .p8.png cart")
    stem = re.sub(r"[^a-z0-9-]+", "-", stem).strip("-")[:24] or "cart"
    return stem + ext


def stem_of(name):
    return name.split(".")[0]


# ---------------------------------------------------------------- loading picture

def row_order(h):
    """The Spectrum's interleaved row order: thirds, then pixel line, then cell row."""
    out = []
    for third in range((h + 63) // 64):
        for line in range(8):
            for cell in range(8):
                r = third * 64 + cell * 8 + line
                if r < h:
                    out.append(r)
    return out


def cart_label(path):
    """The 128x128 label picture stored in a .p8.png or .p8 cart, or None."""
    from PIL import Image
    pal = np.array(PAL)
    if path.lower().endswith(".png"):
        img = Image.open(path).convert("RGB")
        if img.size != (160, 205):
            return None
        a = np.asarray(img).astype(int)[24:152, 16:144]
        # the cart's data is hidden in the low bits, so snap back to the palette
        idx = ((a[:, :, None, :] - pal[None, None]) ** 2).sum(-1).argmin(-1)
        return Image.fromarray(pal[idx].astype(np.uint8))
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if "__label__" not in lines:
        return None
    rows = lines[lines.index("__label__") + 1:][:128]
    if len(rows) < 128:
        return None
    idx = np.array([[int(c, 32) % 16 if c.isalnum() else 0 for c in r[:128].ljust(128, "0")]
                    for r in rows])
    return Image.fromarray(pal[idx].astype(np.uint8))


def make_screen(path):
    """Picture payload: mode 5, x, y, w, h, then
    1. for each of the h rows: one row of the black-and-white outline (w/8
       bytes, taken in the Spectrum's interleaved row order) followed by one
       row of the real colours (w/2 bytes, top to bottom). So the outline draws
       slowly through the whole load while the colour arrives out of sight.
       Outline "ink" is any pixel clearly lighter or darker than the background.
    2. a short sweep section (3 bytes per row): as it arrives, the loader
       sweeps the colour down the screen, like the Spectrum's colour data.
    (Modes 3 and 4 are older tape formats the loader still understands.)"""
    from PIL import Image
    x, y, w, h = 0, 0, 128, 128
    img = (path if isinstance(path, Image.Image) else Image.open(path)).convert("RGB")
    s = max(w / img.width, h / img.height)          # only if it isn't 128x128 already
    img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))),
                     Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    img = img.crop((left, top, left + w, top + h))
    px = np.asarray(img, np.float32)
    pal = np.array(PAL, np.float32)
    idx = ((px[:, :, None, :] - pal[None, None, :, :]) ** 2).sum(-1).argmin(-1).astype(np.uint8)
    # ink = pixels clearly lighter or darker than the background colour
    # (so faint shadows and outlines don't turn everything into black blobs)
    lum = (pal @ np.array([0.299, 0.587, 0.114], np.float32)) / 255
    background = np.bincount(idx.ravel(), minlength=16).argmax()
    ink = (np.abs(lum[idx] - lum[background]) > 0.25).astype(np.uint8)
    out = bytearray([5, x, y, w, h])
    for i, r in enumerate(row_order(h)):
        out += np.packbits(ink[r]).tobytes()                    # outline row
        out += bytes(idx[i, 0::2] | (idx[i, 1::2] << 4))        # colour row i
    out += bytes((n // 3) & 255 for n in range(h * 3))    # sweep section
    return bytes(out)


def render_screen(payload, upto=None):
    """Draw a picture payload the way the loader cart does (optionally only the
    first `upto` bytes, to see it part-loaded); returns a PIL image."""
    from PIL import Image
    mode, x, y, w, h = payload[:5]
    data = payload[5:upto]
    scr = np.full((128, 128), 0, np.uint8)
    rows, bpr, bw = row_order(h), w // 8, w // 2

    def outline(r, c, v):
        for b in range(8):
            scr[y + r, x + c * 8 + b] = 7 if v & (128 >> b) else 0

    def colour_row(r, row_bytes):
        for c, v in enumerate(row_bytes):
            scr[y + r, x + c * 2], scr[y + r, x + c * 2 + 1] = v & 15, v >> 4

    if mode == 5:
        chunk = bpr + bw
        col = {}
        for k, v in enumerate(data):
            if k < h * chunk:
                i, o = divmod(k, chunk)
                if o < bpr:
                    outline(rows[i], o, v)
                else:
                    col[i * bw + o - bpr] = v
            else:
                r = (k - h * chunk) // 3              # sweep: reveal row r
                if (k - h * chunk) % 3 == 0 and r < h:
                    colour_row(r, [col.get(r * bw + c, 0) for c in range(bw)])
        return Image.fromarray(np.array(PAL, np.uint8)[scr])

    nb = bpr * h
    for k, v in enumerate(data):
        if k < nb:
            outline(rows[k // bpr], k % bpr, v)
        else:
            j = k - nb
            if mode == 3:                       # one 8x8 square at a time
                cell, within = divmod(j, 32)
                cy, cx = divmod(cell, w // 8)
                yy, bx = divmod(within, 4)
                row, c = cy * 8 + yy, cx * 8 + bx * 2
            else:                               # row by row, top to bottom
                row, c = j // bw, (j % bw) * 2
            scr[y + row, x + c] = v & 15
            scr[y + row, x + c + 1] = v >> 4
    return Image.fromarray(np.array(PAL, np.uint8)[scr])


# ---------------------------------------------------------------- tape encoding

def _cycle(freq):
    n = round(RATE / freq)
    return np.sin(2 * np.pi * np.arange(n) / n).astype(np.float32)


def encode_block(btype, name, data, pilot=PILOT_BITS, factor=1.0):
    nb = name.encode("ascii")
    header = MAGIC + bytes([btype, len(nb)]) + nb + struct.pack(">I", len(data))
    payload = (header + struct.pack(">I", zlib.crc32(header))
               + data + struct.pack(">I", zlib.crc32(data)))
    bits = np.concatenate([
        np.zeros(int(pilot * factor), np.uint8),     # same length in seconds
        np.unpackbits(np.array([SYNC >> 8, SYNC & 0xFF], np.uint8)),
        np.unpackbits(np.frombuffer(payload, np.uint8)),
        np.zeros(200, np.uint8),
    ])
    c0, c1 = _cycle(F0 * factor), _cycle(F1 * factor)
    return np.concatenate([c1 if b else c0 for b in bits]) * 0.8


def write_wav(path, samples, rate=RATE):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:
        a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        a = v.astype(np.float32) / (1 << 23)
    elif width == 4:
        a = np.frombuffer(raw, "<i4").astype(np.float32) / (1 << 31)
    else:
        sys.exit(f"{path}: unsupported sample width")
    return rate, a.reshape(-1, ch).mean(axis=1)


# ---------------------------------------------------------------- tape decoding

class _Bits:
    """Turns cycle lengths into bits, finds the sync word and reads a block.

    The decoder runs two of these: one timing cycles between rising edges and
    one between falling edges, so it doesn't matter if the tape deck turns the
    signal upside down."""

    def __init__(self, dec):
        self.d = dec
        self.since = 0
        self.idle()

    def idle(self):
        self.state = "idle"
        self.zeros = 0

    def period(self, p):
        d = self.d
        if d.active is not None and d.active is not self:
            return                          # the other polarity is reading
        if p > d.pmax:
            if self.state == "data" and d.active is self:
                d.finish(self, "error", msg="signal lost")
            elif self.state == "data":
                self.idle()                 # wasn't ours anyway
                d.lost()
            elif self.state == "pilot":
                self.idle()
                d.lost()
            else:
                self.zeros = 0
            return
        if self.state == "idle":
            # a pilot is a long run of cycles that are all the same length
            if self.zeros and abs(p - self.avg) <= 0.2 * self.avg:
                self.zeros += 1
                self.avg += (p - self.avg) / min(self.zeros, 64)
            else:
                self.zeros, self.avg = 1, float(p)
            if self.zeros >= PILOT_MIN:
                lo = d.rate / (F0 * SPEED_MAX * 1.3)
                hi = d.rate / (F0 * SPEED_MIN / 1.3)
                if lo <= self.avg <= hi:
                    self.state, self.shift, self.after, self.blur = "pilot", 0, 0, 0
                    d.pilot(d.lock_p0 if d.in_pilot else self.avg)
                else:
                    self.zeros = 0
            return
        if d.split is None:
            return
        bit = 1 if p > d.split else 0
        if d.active is None and abs(p - d.split) < d.split * 0.12:
            # a cycle halfway between short and long: this reader is timing
            # from the middle of each cycle, so it's out of step
            self.blur += 1
        if self.state == "pilot":
            self.shift = ((self.shift << 1) | bit) & 0xFFFF
            if bit or self.after:
                self.after += 1
                if self.shift == SYNC:
                    # both polarities may read on until one sees "P8T2"
                    self.state, self.phase = "data", "header"
                    self.buf, self.cur, self.nbits = bytearray(), 0, 0
                elif self.after >= 16:
                    self.idle()
                    d.lost()
        else:
            self.cur = (self.cur << 1) | bit
            self.nbits += 1
            if self.nbits == 8:
                self.buf.append(self.cur)
                self.cur = self.nbits = 0
                self.byte()

    def byte(self):
        b, d = self.buf, self.d
        if self.phase == "header":
            if len(b) == 4:
                if bytes(b) != MAGIC or self.blur > 3:
                    self.idle()             # out of step: let the other one read it
                    return d.lost()
                d.claim(self)               # this one's in step: it reads the block
            if len(b) >= 6:
                hlen = 6 + b[5] + 4
                if len(b) == hlen + 4:
                    if struct.unpack(">I", b[hlen:])[0] != zlib.crc32(bytes(b[:hlen])):
                        return d.finish(self, "error", msg="header checksum failed")
                    btype = b[4]
                    name = bytes(b[6:6 + b[5]]).decode("ascii", "replace")
                    size = struct.unpack(">I", b[hlen - 4:hlen])[0]
                    if btype not in (T_SCREEN, T_CART, T_HEADER) or not NAME_RE.match(name) \
                            or size > MAX_SIZE:
                        return d.finish(self, "error", msg="bad header")
                    self.btype, self.name, self.size, self.start = btype, name, size, len(b)
                    self.phase = "body"
                    d.emit("header", type=btype, name=name, size=size)
        else:
            got = len(b) - self.start
            if got <= self.size:
                d.emit("byte", offset=got - 1, value=b[-1])
            elif got == self.size + 4:
                data = bytes(b[self.start:self.start + self.size])
                if struct.unpack(">I", b[-4:])[0] == zlib.crc32(data):
                    d.finish(self, "done", type=self.btype, name=self.name, data=data)
                else:
                    d.finish(self, "error", msg="checksum failed")


class Decoder:
    """Streaming decoder: feed() it audio, it calls emit(kind, **info).

    Events: pilot, lost, header(type, name, size), byte(offset, value),
            done(type, name, data), error(msg)
    """

    def __init__(self, rate, emit):
        self.emit = emit
        self.rate = rate
        self.px = self.py = self.env = self.l1 = self.l2 = 0.0
        self.high = False
        self.lock_p0 = None
        self.active = None
        self.in_pilot = False
        self.rise, self.fall = _Bits(self), _Bits(self)
        self.wide()

    def set_filter(self, hz):
        hz = min(hz, self.rate * 0.45)
        self.lpa = 1 - float(np.exp(-2 * np.pi * hz / self.rate))

    def wide(self):
        """Settings while listening for a pilot tone of any speed."""
        r = self.rate
        self.split = None
        self.pmin = r / (F0 * SPEED_MAX) * 0.5     # shorter = glitch, ignore
        self.pmax = int(r / (F1 * SPEED_MIN) * 1.7)  # longer = gap / dropout
        self.set_filter(F0 * SPEED_MAX * 2.5)

    def lock(self, p0):
        """Pilot found, its cycles p0 samples long: tune to this speed."""
        self.split = p0 * 1.5                       # 0 = p0, 1 = 2 * p0
        self.pmin = p0 * 0.5
        self.pmax = int(p0 * 2 * 1.7)
        self.set_filter(self.rate / p0 * 2.5)       # drop the hiss above the tones

    # coordination between the two polarities
    def pilot(self, p0):
        if not self.in_pilot and self.active is None:
            self.in_pilot = True
            self.lock_p0 = p0
            self.lock(p0)
            self.emit("pilot")

    def lost(self):
        if self.active is None and self.rise.state == self.fall.state == "idle" \
                and self.in_pilot:
            self.in_pilot = False
            self.wide()
            self.emit("lost")

    def claim(self, bits):
        self.active = bits
        other = self.fall if bits is self.rise else self.rise
        other.idle()

    def finish(self, bits, kind, **kw):
        self.active = None
        self.in_pilot = False
        self.rise.idle()
        self.fall.idle()
        self.wide()
        self.emit(kind, **kw)

    def feed(self, samples):
        a = 0.995                          # DC-blocking high-pass
        px, py, env, high = self.px, self.py, self.env, self.high
        l1, l2, k = self.l1, self.l2, self.lpa
        rise, fall = self.rise, self.fall
        sr, sf = rise.since, fall.since
        pmin, gap = self.pmin, self.pmax + 1
        for x in np.asarray(samples, np.float32).tolist():
            l1 += k * (x - l1)             # two-pole low-pass: drop tape hiss
            l2 += k * (l1 - l2)
            y = a * (py + l2 - px)
            px, py = l2, y
            ay = y if y > 0 else -y
            env = ay if ay > env else env * 0.9995
            sr += 1
            sf += 1
            h = env * 0.25                 # Schmitt trigger thresholds
            if high:
                if y < -h:
                    high = False           # falling edge
                    if sf >= pmin:
                        fall.period(sf)
                        sf = 0
                        pmin, gap, k = self.pmin, self.pmax + 1, self.lpa
            elif y > h and env > 0.005:
                high = True                # rising edge
                if sr >= pmin:
                    rise.period(sr)
                    sr = 0
                    pmin, gap, k = self.pmin, self.pmax + 1, self.lpa
            if sr == gap:
                rise.period(sr)            # silence / dropout
                pmin, gap, k = self.pmin, self.pmax + 1, self.lpa
            if sf == gap:
                fall.period(sf)
                pmin, gap, k = self.pmin, self.pmax + 1, self.lpa
        rise.since, fall.since = sr, sf
        self.px, self.py, self.env, self.high = px, py, env, high
        self.l1, self.l2 = l1, l2


def save_cart(outdir, name, data):
    path = os.path.join(outdir, "tape_" + name)
    for other in (".p8", ".p8.png"):             # don't leave a stale twin
        o = os.path.join(outdir, "tape_" + stem_of(name) + other)
        if o != path and os.path.exists(o):
            os.remove(o)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    return path


# ---------------------------------------------------------------- link to PICO-8

ST_IDLE, ST_PILOT, ST_DATA, ST_ERROR, ST_DONE = range(5)
K_STATUS, K_SCREEN, K_NAME = range(3)
PKT_DATA, PKT_AUDIO = 36, 240
PKT = 8 + PKT_DATA + 1 + PKT_AUDIO   # 285 bytes
PCM_RATE = 5512.5                    # PICO-8's raw audio output rate
LOADER_VERSION = 10  # must match the "p8tape loader version" line in loader.p8
REQUEST = 0xF7      # cart wants the next packet
START = 0xF8        # player pressed X: start the WAV


class ConsoleUI:
    """Where messages go when running from the command line."""

    def log(self, msg):
        print("\r\033[K" + msg, flush=True)

    def progress(self, what, pct):
        print(f"\r  {what} [{'#' * (pct // 4):<25}] {pct:3d}%", end="", flush=True)

    def level(self, peak):
        pass

    def finished(self):
        print("\r\033[KPICO-8 closed")


class Feeder:
    """Turns decoder events into the packets the loader cart reads.

    Packet (285 bytes): 0xA5, kind, tape state, percent, block type,
    data length, data offset (2 bytes, little-endian), 36 data bytes,
    audio length, then up to 240 bytes of tape audio (8-bit, 5512 Hz) for
    the loader to play through PICO-8's raw audio output.
    """

    def __init__(self, outdir, ui):
        self.outdir, self.ui = outdir, ui
        self.lock = threading.Lock()
        self.state, self.pct, self.block, self.size = ST_IDLE, 0, 0, 1
        self.names = deque()
        self.screen, self.sent = bytearray(), 0
        self.last_pct = -1
        self.audio = bytearray()

    def add_audio(self, pcm):
        with self.lock:
            self.audio += pcm
            if len(self.audio) > 2000:          # never fall more than ~1/3 s behind
                del self.audio[:len(self.audio) - 1000]

    def say(self, msg):
        self.ui.log(msg)

    def event(self, kind, **kw):
        with self.lock:
            if kind == "pilot":
                self.state = ST_PILOT
                self.say("tone found...")
            elif kind == "lost":
                self.state = ST_IDLE
            elif kind == "header":
                self.state, self.block, self.size, self.pct = ST_DATA, kw["type"], kw["size"], 0
                self.names.append(bytes([kw["type"]]) + kw["name"].encode())
                if self.block == T_SCREEN:
                    self.screen, self.sent = bytearray(), 0
                if self.block == T_HEADER:
                    self.say(f"Found: {stem_of(kw['name'])}")
                else:
                    what = "picture" if self.block == T_SCREEN else "cart"
                    self.say(f"loading {what}: {kw['name']} ({kw['size']} bytes)")
            elif kind == "byte":
                if self.block == T_SCREEN:
                    self.screen.append(kw["value"])
                self.pct = min(100, (kw["offset"] + 1) * 100 // max(1, self.size))
                if self.pct != self.last_pct:
                    self.last_pct = self.pct
                    what = {T_SCREEN: "picture", T_CART: "cart"}.get(self.block, "header")
                    self.ui.progress(what, self.pct)
            elif kind == "done":
                if kw["type"] == T_CART:
                    path = save_cart(self.outdir, kw["name"], kw["data"])
                    self.say(f"saved {path} - starting it")
                    self.state = ST_DONE
                else:
                    if kw["type"] == T_SCREEN:
                        self.say("picture loaded")
                    self.state = ST_IDLE
            elif kind == "error":
                self.state = ST_ERROR
                self.say(f"R Tape loading error: {kw['msg']}")

    def packet(self):
        with self.lock:
            kind, off, data = K_STATUS, 0, b""
            if self.names:
                kind, data = K_NAME, self.names.popleft()
            elif self.sent < len(self.screen):
                kind, off = K_SCREEN, self.sent
                data = bytes(self.screen[off:off + PKT_DATA])
                self.sent += len(data)
            state = self.state
            if state == ST_DONE and (self.names or self.sent < len(self.screen)):
                state = ST_DATA                 # finish sending first
            hdr = bytes([0xA5, kind, state, self.pct, self.block, len(data),
                         off & 255, off >> 8])
            audio = bytes(self.audio[:PKT_AUDIO])
            del self.audio[:len(audio)]
        return (hdr + data.ljust(PKT_DATA, b"\0") + bytes([len(audio)]) + audio).ljust(PKT, b"\0")


_NIBBLES = [bytes((0x40 + (x >> 4), 0x40 + (x & 15))) for x in range(256)]


def encode_packet(pkt):
    return b"".join(_NIBBLES[x] for x in pkt)


class PcmMaker:
    """Turns tape audio into PICO-8's raw audio format: 8-bit, 5512.5 Hz."""

    def __init__(self, rate):
        self.step = rate / PCM_RATE
        self.taps = max(1, round(self.step))       # simple low-pass before thinning out
        self.tail = np.zeros(self.taps - 1, np.float32)
        self.buf = np.zeros(0, np.float32)
        self.pos = 0.0
        self.peak = 0.1

    def convert(self, block):
        x = np.concatenate([self.tail, np.asarray(block, np.float32)])
        if self.taps > 1:
            self.tail = x[-(self.taps - 1):]
        smooth = np.convolve(x, np.ones(self.taps, np.float32) / self.taps, "valid")
        buf = np.concatenate([self.buf, smooth])
        idx = np.arange(self.pos, buf.size - 1, self.step)
        nxt = idx[-1] + self.step if idx.size else self.pos
        drop = min(int(nxt), buf.size)
        self.buf, self.pos = buf[drop:], nxt - drop
        if not idx.size:
            return b""
        out = np.interp(idx, np.arange(buf.size), buf)
        # keep the volume steady whatever the tape level
        self.peak = max(0.02, self.peak * 0.97, float(np.abs(out).max()))
        return np.clip(out / self.peak * 50 + 128, 0, 255).astype(np.uint8).tobytes()


class Link:
    """Sends a packet whenever the cart asks for one (it writes 0xF7)."""

    def __init__(self, proc, feeder, ui):
        self.proc, self.feeder, self.ui = proc, feeder, ui
        self.wlock = threading.Lock()
        self.requests = 0
        self.push = False
        self.alive = True
        self.start = threading.Event()     # set when the player presses X

    def send(self):
        data = encode_packet(self.feeder.packet())
        with self.wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.alive = False

    def read_requests(self):
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 512)
            except OSError:
                break
            if not chunk:
                break
            if START in chunk:
                self.start.set()
            rest = chunk.replace(bytes([REQUEST]), b"").replace(bytes([START]), b"")
            text = rest.decode("utf-8", "replace").strip()
            if text:                               # anything else PICO-8 prints
                self.ui.log("PICO-8: " + text)
            for _ in range(chunk.count(REQUEST)):
                self.requests += 1
                self.push = False
                self.send()

    def keepalive(self):
        # Normally the cart drives everything. If it never asks for packets
        # (it should within a few seconds), fall back to sending steadily.
        start = time.monotonic()
        while self.alive and self.proc.poll() is None:
            time.sleep(1 / 25)
            if self.requests == 0 and time.monotonic() - start > 20:
                if not self.push:
                    self.ui.log("note: PICO-8 isn't asking for data, sending it steadily instead")
                self.push = True
                self.start.set()
            if self.push:
                self.send()


def find_pico8(given=None):
    """Path to the PICO-8 program, or None if it can't be found."""
    if given:
        return given
    for n in ("pico8", "pico8.exe", "pico-8"):
        p = shutil.which(n)
        if p:
            return p
    home = os.path.expanduser("~")
    for p in (r"C:\Program Files (x86)\PICO-8\pico8.exe",
              r"C:\Program Files\PICO-8\pico8.exe",
              "/Applications/PICO-8.app/Contents/MacOS/pico8",
              os.path.join(home, "pico-8", "pico8"),
              os.path.join(home, "pico-8", "pico8_64")):
        if os.path.isfile(p):
            return p
    return None


def loader_version(path):
    """The version number written in loader.p8, or 0 if it hasn't got one."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r"p8tape loader version (\d+)", f.read(4000))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def have_sounddevice():
    try:
        import sounddevice  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


def input_devices():
    """[(index, label)] for every sound input."""
    import sounddevice as sd
    apis = sd.query_hostapis()
    return [(i, f"{d['name']}  ({apis[d['hostapi']]['name']})")
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def wav_source(path, decoder, speed, audible, start, delay, ui, stop, audio=None):
    rate, audio_data = read_wav(path)
    ui.log("press X in the loader to start the tape")
    while not start.wait(0.2):          # wait for the player to press X
        if stop.is_set():
            return
    time.sleep(delay)
    sd = None
    if audible and speed == 1:
        try:
            import sounddevice as sd
            sd.play(audio_data, rate)
        except (ImportError, OSError):
            ui.log("(can't play the sound: pip install sounddevice)")
    ui.log("playing " + os.path.basename(path))
    pcm = PcmMaker(rate) if audio else None
    chunk, t0 = 1024, time.monotonic()
    for i in range(0, audio_data.size, chunk):
        if stop.is_set():
            break
        block = audio_data[i:i + chunk]
        decoder.feed(block)
        if pcm:
            audio(pcm.convert(block))
        ui.level(float(np.abs(block).max()) if block.size else 0.0)
        wait = t0 + (i + chunk) / rate / speed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    if sd and stop.is_set():
        sd.stop()
    decoder.feed(np.zeros(rate // 10, np.float32))
    ui.level(0.0)
    ui.log("end of WAV")


def live_source(device, rate, decoder, ui, stop, audio=None):
    import queue
    import sounddevice as sd
    q = queue.Queue()
    pcm = PcmMaker(rate) if audio else None

    def callback(indata, frames, timeinfo, status):
        q.put(indata[:, 0].copy())

    try:
        with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                            device=device, callback=callback, blocksize=2048):
            ui.log("listening - press play on the tape")
            while not stop.is_set():
                try:
                    block = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                decoder.feed(block)
                if pcm:
                    audio(pcm.convert(block))
                ui.level(float(np.abs(block).max()))
    except Exception as e:                      # e.g. device busy or unplugged
        ui.log(f"sound input problem: {e}")


class LevelMonitor:
    """Shows the input level before loading, so the tape volume can be set."""

    def __init__(self, device, ui, rate=RATE):
        import sounddevice as sd
        self.stream = sd.InputStream(
            samplerate=rate, channels=1, dtype="float32", device=device, blocksize=2048,
            callback=lambda indata, f, t, s: ui.level(float(np.abs(indata).max())))
        self.stream.start()

    def close(self):
        self.stream.stop()
        self.stream.close()


class Session:
    """One run of PICO-8 + loader.p8, fed from a sound input or a WAV file."""

    def __init__(self, ui):
        self.ui = ui
        self.proc = None
        self.stop_evt = threading.Event()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, pico8, loader, wav=None, device=None, audible=False,
              delay=1.0, speed=1.0, rate=RATE, sound=True):
        """sound: play the tape audio inside PICO-8."""
        loader = os.path.abspath(loader)
        root = os.path.dirname(loader)
        self.stop_evt.clear()
        feeder = Feeder(root, self.ui)
        decoder = Decoder(read_wav(wav)[0] if wav else rate, feeder.event)
        self.proc = subprocess.Popen([pico8, "-root_path", root, "-run", loader, "-p", "tape"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        link = Link(self.proc, feeder, self.ui)
        link.send()                       # keep one packet ahead of the cart
        link.send()
        threading.Thread(target=link.read_requests, daemon=True).start()
        threading.Thread(target=link.keepalive, daemon=True).start()
        if wav:
            target, args = wav_source, (wav, decoder, speed, audible, link.start, delay,
                                        self.ui, self.stop_evt,
                                        feeder.add_audio if sound else None)
        else:
            target, args = live_source, (device, rate, decoder, self.ui, self.stop_evt,
                                         feeder.add_audio if sound else None)
        threading.Thread(target=target, args=args, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        self.proc.wait()
        self.stop_evt.set()
        self.ui.finished()

    def stop(self):
        self.stop_evt.set()
        if self.running():
            self.proc.terminate()


# ---------------------------------------------------------------- commands

def build_tape(cart, screen=None, pause=3.0, speed="standard"):
    """Tape audio for a cart, with an optional loading picture (a path or a
    PIL image). Returns (samples, list of info lines)."""
    with open(cart, "rb") as f:
        data = f.read()
    name = tape_name(cart)
    factor = SPEEDS[speed][0]
    gap = np.zeros(RATE, np.float32)
    info = []
    # Spectrum-style header first: long pilot, just the name, then a pause
    header = encode_block(T_HEADER, name, b"", HEADER_PILOT_BITS, factor)
    parts = [gap, header, np.zeros(int(RATE * pause), np.float32)]
    if screen is not None:
        scr = make_screen(screen)
        block = encode_block(T_SCREEN, name, scr, PILOT_BITS, factor)
        parts += [block, gap]
        info.append(f"picture: {len(scr)} bytes, {len(block) / RATE:.0f} s")
    block = encode_block(T_CART, name, data, PILOT_BITS, factor)
    parts += [block, gap]
    info.append(f"{name}: {len(data)} bytes, {len(block) / RATE:.0f} s")
    return np.concatenate(parts), info


def cmd_encode(args):
    samples, info = build_tape(args.cart, args.screen, args.pause, args.speed)
    print(speed_label(args.speed))
    for line in info:
        print(line)
    out = args.output or stem_of(tape_name(args.cart)) + ".wav"
    write_wav(out, samples)
    print(f"wrote {out} ({samples.size / RATE / 60:.1f} min) - play it into your tape deck and record")


def cmd_decode(args):
    rate, audio = read_wav(args.wav)
    os.makedirs(args.out, exist_ok=True)
    saved = []

    def event(kind, **kw):
        if kind == "header":
            what = {T_SCREEN: "picture", T_CART: "cart", T_HEADER: "header"}[kw["type"]]
            print(f"found {what}: {kw['name']}")
        elif kind == "error":
            print(f"R Tape loading error: {kw['msg']}")
        elif kind == "done" and kw["type"] != T_HEADER:
            if kw["type"] == T_CART:
                saved.append(save_cart(args.out, kw["name"], kw["data"]))
            else:
                p = os.path.join(args.out, "tape_" + stem_of(kw["name"]) + "-picture.png")
                render_screen(kw["data"]).resize((512, 512), 0).save(p)
                saved.append(p)
            print(f"  saved {saved[-1]}")

    dec = Decoder(rate, event)
    for i in range(0, audio.size, 8192):
        dec.feed(audio[i:i + 8192])
    dec.feed(np.zeros(rate // 10, np.float32))
    print(f"{len(saved)} file(s) saved")


def cmd_play(args):
    loader = os.path.abspath(args.loader or default_loader())
    if not os.path.isfile(loader):
        sys.exit(f"can't find {loader} - put loader.p8 next to p8tape.py or use --loader")
    if args.device == "list":
        for i, label in input_devices():
            print(f"{i:3d}  {label}")
        return
    if loader_version(loader) != LOADER_VERSION:
        print(f"warning: {loader} is out of date - use the loader.p8 that came "
              "with this p8tape.py, or loading pictures will come out wrong")
    if not args.wav and not have_sounddevice():
        sys.exit("listening to a tape needs sounddevice:  pip install sounddevice")
    pico8 = find_pico8(args.pico8)
    if not pico8:
        sys.exit("couldn't find PICO-8 - tell me where it is with --pico8 PATH")
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    session = Session(ConsoleUI())
    session.start(pico8, loader, wav=args.wav, device=device, audible=args.audible,
                  sound=not args.silent,
                  delay=args.delay, speed=args.speed, rate=args.rate)
    try:
        while session.running():
            time.sleep(0.2)
    except KeyboardInterrupt:
        session.stop()
    time.sleep(0.3)


# ---------------------------------------------------------------- window

def app_dir():
    """Per-user folder for settings, the loader cart and loaded carts."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "p8tape")
    return os.path.join(os.path.expanduser("~"), ".config", "p8tape")


def default_loader():
    """loader.p8 to use. The packaged program carries its own copy and keeps
    an up-to-date one in the user's p8tape folder."""
    if not FROZEN:
        return os.path.join(BUNDLE, "loader.p8")
    target = os.path.join(app_dir(), "loader.p8")
    if loader_version(target) != LOADER_VERSION:
        os.makedirs(app_dir(), exist_ok=True)
        shutil.copyfile(os.path.join(BUNDLE, "loader.p8"), target)
    return target


SETTINGS = os.path.join(app_dir(), "settings.json")


def load_settings():
    import json
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    import json
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def run_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        sys.exit("the window needs Tkinter:  sudo apt install python3-tk")
    import queue
    import math

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = load_settings()
    events = queue.Queue()
    sound_ok = have_sounddevice()

    class WindowUI:                       # called from other threads: queue it
        def log(self, msg):
            events.put(("log", msg))

        def progress(self, what, pct):
            events.put(("progress", (what, pct)))

        def level(self, peak):
            events.put(("level", peak))

        def finished(self):
            events.put(("finished", None))

    ui = WindowUI()
    session = Session(ui)
    monitor = [None]

    win = tk.Tk()
    win.title("P8 Tape")
    win.minsize(560, 0)
    tabs = ttk.Notebook(win)
    tabs.pack(fill="both", expand=True, padx=6, pady=6)
    make_tab = ttk.Frame(tabs, padding=12)
    main = ttk.Frame(tabs, padding=12)
    tabs.add(make_tab, text="Make a tape")
    tabs.add(main, text="Load a tape")
    main.columnconfigure(1, weight=1)

    # --- PICO-8 and loader
    pico8_var = tk.StringVar(value=cfg.get("pico8") or find_pico8() or "")
    saved_loader = cfg.get("loader")
    if not saved_loader or loader_version(saved_loader) != LOADER_VERSION:
        saved_loader = default_loader()        # missing or out of date
    loader_var = tk.StringVar(value=saved_loader)

    def browse_into(var, title, types):
        start = os.path.dirname(var.get()) if var.get() else os.path.expanduser("~")
        p = filedialog.askopenfilename(title=title, initialdir=start, filetypes=types)
        if p:
            var.set(p)

    ttk.Label(main, text="PICO-8 program:").grid(row=0, column=0, sticky="w", pady=2)
    ttk.Entry(main, textvariable=pico8_var).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(main, text="Browse…", command=lambda: browse_into(
        pico8_var, "Find the PICO-8 program", [("All files", "*")])).grid(row=0, column=2)
    ttk.Label(main, text="Loader cart:").grid(row=1, column=0, sticky="w", pady=2)
    ttk.Entry(main, textvariable=loader_var).grid(row=1, column=1, sticky="ew", padx=6)
    ttk.Button(main, text="Browse…", command=lambda: browse_into(
        loader_var, "Find loader.p8", [("PICO-8 carts", "*.p8"), ("All files", "*")])
    ).grid(row=1, column=2)

    # --- where the tape comes from
    src_box = ttk.LabelFrame(main, text="Load from", padding=10)
    src_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 6))
    src_box.columnconfigure(1, weight=1)
    source_var = tk.StringVar(value=cfg.get("source", "tape" if sound_ok else "wav"))

    tape_rb = ttk.Radiobutton(src_box, text="Tape (sound input):", value="tape",
                              variable=source_var)
    tape_rb.grid(row=0, column=0, sticky="w")
    devices = input_devices() if sound_ok else []
    device_var = tk.StringVar()
    device_box = ttk.Combobox(src_box, textvariable=device_var, state="readonly",
                              values=[label for _, label in devices])
    device_box.grid(row=0, column=1, sticky="ew", padx=6)

    def pick_default_device():
        labels = [label for _, label in devices]
        if cfg.get("device") in labels:
            device_var.set(cfg["device"])
        elif devices:
            try:
                import sounddevice as sd
                d = sd.default.device[0]
                device_var.set(next(lab for i, lab in devices if i == d))
            except Exception:
                device_var.set(labels[0])

    def refresh_devices():
        nonlocal devices
        stop_monitor()
        devices = input_devices() if sound_ok else []
        device_box["values"] = [label for _, label in devices]
        if device_var.get() not in device_box["values"]:
            device_var.set("")
            pick_default_device()
        start_monitor()

    ttk.Button(src_box, text="Refresh", command=refresh_devices).grid(row=0, column=2)
    pick_default_device()

    meter_row = ttk.Frame(src_box)
    meter_row.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(4, 8))
    meter_row.columnconfigure(1, weight=1)
    ttk.Label(meter_row, text="Level:").grid(row=0, column=0, padx=(0, 6))
    meter = ttk.Progressbar(meter_row, maximum=100)
    meter.grid(row=0, column=1, sticky="ew")
    meter_text = ttk.Label(meter_row, width=16)
    meter_text.grid(row=0, column=2, padx=(6, 0))
    if not sound_ok:
        tape_rb.state(["disabled"])
        device_box.state(["disabled"])
        meter_text.config(text="needs sounddevice")

    ttk.Radiobutton(src_box, text="WAV file:", value="wav",
                    variable=source_var).grid(row=2, column=0, sticky="w")
    wav_var = tk.StringVar(value=cfg.get("wav", ""))
    ttk.Entry(src_box, textvariable=wav_var).grid(row=2, column=1, sticky="ew", padx=6)

    def browse_wav():
        browse_into(wav_var, "Choose a tape WAV", [("WAV files", "*.wav"), ("All files", "*")])
        source_var.set("wav")

    ttk.Button(src_box, text="Browse…", command=browse_wav).grid(row=2, column=2)
    sound_var = tk.BooleanVar(value=cfg.get("sound", True))
    ttk.Checkbutton(src_box, text="Tape sound in PICO-8", variable=sound_var).grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # --- boot button, status, log
    boot = ttk.Button(main, text="Boot PICO-8 loader")
    boot.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4), ipady=6)
    hint = ttk.Label(main, foreground="#555")
    hint.grid(row=4, column=0, columnspan=3, sticky="w")
    status = ttk.Label(main, text="Ready")
    status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 2))
    load_bar = ttk.Progressbar(main, maximum=100)
    load_bar.grid(row=6, column=0, columnspan=3, sticky="ew")
    log_frame = ttk.Frame(main)
    log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
    main.rowconfigure(7, weight=1)
    log = tk.Text(log_frame, height=9, width=60, state="disabled", wrap="word",
                  font=("TkFixedFont", 9))
    sb = ttk.Scrollbar(log_frame, command=log.yview)
    log.config(yscrollcommand=sb.set)
    log.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def add_log(msg):
        log.config(state="normal")
        log.insert("end", msg + "\n")
        log.see("end")
        log.config(state="disabled")

    def update_hint(*_):
        if session.running():
            return
        if source_var.get() == "tape":
            hint.config(text="Boot the loader, press X on its title screen, then press play on the tape.")
        else:
            hint.config(text="Boot the loader and press X on its title screen to start the WAV.")

    # --- level meter while not loading
    def selected_device():
        label = device_var.get()
        return next((i for i, lab in devices if lab == label), None)

    def stop_monitor():
        if monitor[0]:
            try:
                monitor[0].close()
            except Exception:
                pass
            monitor[0] = None
        meter["value"] = 0

    def start_monitor(*_):
        stop_monitor()
        if not sound_ok or session.running() or source_var.get() != "tape":
            meter_text.config(text="" if sound_ok else "needs sounddevice")
            return
        dev = selected_device()
        if dev is None:
            return
        try:
            monitor[0] = LevelMonitor(dev, ui)
        except Exception as e:
            meter_text.config(text="can't open input")
            add_log(f"can't open that input: {e}")

    def on_source_change(*_):
        update_hint()
        start_monitor()

    source_var.trace_add("write", on_source_change)
    device_box.bind("<<ComboboxSelected>>", start_monitor)

    # --- boot / close
    def remember():
        cfg.update(pico8=pico8_var.get(), loader=loader_var.get(), source=source_var.get(),
                   device=device_var.get(), wav=wav_var.get(), sound=sound_var.get())
        save_settings(cfg)

    def on_boot():
        if session.running():
            session.stop()
            return
        pico8, loader = pico8_var.get().strip(), loader_var.get().strip()
        if not pico8 or not os.path.isfile(pico8):
            messagebox.showerror("P8 Tape", "Please choose the PICO-8 program first.")
            return
        if not os.path.isfile(loader):
            messagebox.showerror("P8 Tape", "Can't find the loader cart (loader.p8).")
            return
        wav, device = None, None
        if source_var.get() == "wav":
            wav = wav_var.get().strip()
            if not os.path.isfile(wav):
                messagebox.showerror("P8 Tape", "Please choose a WAV file to load.")
                return
        else:
            device = selected_device()
            if device is None:
                messagebox.showerror("P8 Tape", "Please choose a sound input.")
                return
        if loader_version(loader) != LOADER_VERSION and not messagebox.askyesno(
                "P8 Tape", "This loader.p8 is out of date, so loading pictures will come "
                           "out wrong.\n\nReplace it with the loader.p8 that came with this "
                           "version of p8tape.py.\n\nBoot it anyway?"):
            return
        remember()
        stop_monitor()
        try:
            session.start(pico8, loader, wav=wav, device=device,
                          sound=sound_var.get(),
                          speed=float(os.environ.get("P8TAPE_SPEED", "1")))
        except Exception as e:
            messagebox.showerror("P8 Tape", f"Couldn't start PICO-8:\n{e}")
            start_monitor()
            return
        boot.config(text="Close PICO-8")
        status.config(text="PICO-8 running - press X on the loader's title screen")
        load_bar["value"] = 0
        add_log("booted PICO-8 loader")

    boot.config(command=on_boot)

    # ================= make a tape =================
    import base64
    import io
    from PIL import Image, ImageDraw

    mk = make_tab
    mk.columnconfigure(1, weight=1)
    cart_var = tk.StringVar(value=cfg.get("cart", ""))
    pic_var = tk.StringVar()
    pause_var = tk.StringVar(value=str(cfg.get("pause", 3)))
    out_var = tk.StringVar()
    picture = [None]                    # the PIL image to use, or None

    ttk.Label(mk, text="Cart:").grid(row=0, column=0, sticky="w", pady=2)
    ttk.Entry(mk, textvariable=cart_var).grid(row=0, column=1, sticky="ew", padx=6)

    def browse_cart():
        browse_into(cart_var, "Choose a PICO-8 cart",
                    [("PICO-8 carts", "*.p8 *.png"), ("All files", "*")])
        cart_chosen()

    ttk.Button(mk, text="Browse…", command=browse_cart).grid(row=0, column=2)

    pic_box = ttk.LabelFrame(mk, text="Loading picture (optional)", padding=10)
    pic_box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 6))
    pic_box.columnconfigure(1, weight=1)
    # a blank 256x256 image keeps the preview that size in pixels (a Label
    # with only text would measure width/height in characters)
    blank = tk.PhotoImage(width=256, height=256)
    preview = tk.Label(pic_box, image=blank, bg="#1d2b53", fg="#c2c3c7",
                       text="no picture", compound="center", bd=0)
    preview.grid(row=0, column=0, rowspan=5, padx=(0, 12))
    pic_label = ttk.Label(pic_box, textvariable=pic_var, wraplength=230)
    pic_label.grid(row=0, column=1, columnspan=2, sticky="nw")
    pic_buttons = ttk.Frame(pic_box)
    pic_buttons.grid(row=1, column=1, columnspan=2, sticky="nw", pady=6)
    ttk.Label(pic_box, foreground="#555", wraplength=230, justify="left",
              text="128×128 works best. The raster bars cover the outer 4 pixels "
                   "(shown striped), so keep anything important inside them.").grid(
        row=2, column=1, columnspan=2, sticky="nw")

    def show_picture():
        img = picture[0]
        if img is None:
            preview.config(image=blank, text="no picture")
            preview.image = blank
            return
        img = img.convert("RGB")
        s = max(128 / img.width, 128 / img.height)
        img = img.resize((max(128, round(img.width * s)), max(128, round(img.height * s))))
        l, t = (img.width - 128) // 2, (img.height - 128) // 2
        img = img.crop((l, t, l + 128, t + 128)).resize((256, 256), Image.NEAREST)
        d = ImageDraw.Draw(img)                    # mark the border the bars cover
        for y in range(0, 256, 4):                 # border = 4 px, so 8 px here
            colour = (41, 173, 255) if (y // 4) % 2 else (255, 0, 77)
            if y < 8 or y >= 248:
                d.rectangle((0, y, 255, y + 1), fill=colour)
            else:
                d.rectangle((0, y, 7, y + 1), fill=colour)
                d.rectangle((248, y, 255, y + 1), fill=colour)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        preview.config(image=photo, text="")
        preview.image = photo

    def choose_picture():
        p = filedialog.askopenfilename(
            title="Choose a loading picture",
            initialdir=os.path.dirname(cart_var.get()) or os.path.expanduser("~"),
            filetypes=[("Pictures", "*.png *.gif *.jpg *.jpeg *.bmp"), ("All files", "*")])
        if p:
            try:
                picture[0] = Image.open(p)
                picture[0].load()
                pic_var.set(os.path.basename(p))
            except Exception as e:
                messagebox.showerror("P8 Tape", f"Can't open that picture:\n{e}")
            show_picture()

    def use_label():
        cart = cart_var.get().strip()
        if not os.path.isfile(cart):
            messagebox.showerror("P8 Tape", "Choose the cart first.")
            return
        img = cart_label(cart)
        if img is None:
            messagebox.showinfo("P8 Tape", "That cart doesn't have a label picture.")
            return
        picture[0] = img
        pic_var.set("the cart's label")
        show_picture()

    def no_picture():
        picture[0] = None
        pic_var.set("")
        show_picture()

    ttk.Button(pic_buttons, text="Choose picture…", command=choose_picture).pack(anchor="w", fill="x")
    ttk.Button(pic_buttons, text="Use the cart's label", command=use_label).pack(anchor="w", fill="x", pady=4)
    ttk.Button(pic_buttons, text="No picture", command=no_picture).pack(anchor="w", fill="x")

    opts = ttk.Frame(mk)
    opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
    opts.columnconfigure(1, weight=1)
    ttk.Label(opts, text="Pause after \"Found:\"").grid(row=0, column=0, sticky="w")
    pf = ttk.Frame(opts)
    pf.grid(row=0, column=1, sticky="w", padx=6)
    ttk.Spinbox(pf, from_=0, to=30, increment=0.5, width=5, textvariable=pause_var).pack(side="left")
    ttk.Label(pf, text="seconds").pack(side="left", padx=4)
    ttk.Label(opts, text="Speed:").grid(row=1, column=0, sticky="w", pady=(6, 0))
    speed_names = list(SPEEDS)
    speed_labels = [speed_label(s) for s in speed_names]
    speed_var = tk.StringVar(value=speed_label(cfg.get("speed", "standard")
                                               if cfg.get("speed") in SPEEDS else "standard"))
    ttk.Combobox(opts, textvariable=speed_var, values=speed_labels, state="readonly",
                 width=30).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
    ttk.Label(opts, text="Save tape as:").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(opts, textvariable=out_var).grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))

    def browse_out():
        start = out_var.get() or cart_var.get()
        p = filedialog.asksaveasfilename(
            title="Save the tape as", defaultextension=".wav",
            initialdir=os.path.dirname(start) or os.path.expanduser("~"),
            initialfile=os.path.basename(out_var.get()) if out_var.get() else "",
            filetypes=[("WAV files", "*.wav")])
        if p:
            out_var.set(p)

    ttk.Button(opts, text="Browse…", command=browse_out).grid(row=2, column=2, pady=(6, 0))

    def cart_chosen():
        cart = cart_var.get().strip()
        if os.path.isfile(cart):
            try:
                out_var.set(os.path.join(os.path.dirname(cart), stem_of(tape_name(cart)) + ".wav"))
            except SystemExit:
                pass

    make_btn = ttk.Button(mk, text="Make tape")
    make_btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 4), ipady=6)
    make_status = ttk.Label(mk, text="")
    make_status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
    load_now = ttk.Button(mk, text="Load it now →")
    made = [None]

    def on_make():
        cart, out = cart_var.get().strip(), out_var.get().strip()
        if not os.path.isfile(cart):
            messagebox.showerror("P8 Tape", "Choose a cart (.p8 or .p8.png) first.")
            return
        low = cart.lower()
        if not (low.endswith(".p8") or low.endswith(".p8.png")):
            messagebox.showerror("P8 Tape", "The cart needs to be a .p8 or .p8.png file.")
            return
        if not out:
            messagebox.showerror("P8 Tape", "Choose where to save the tape.")
            return
        try:
            pause = max(0.0, float(pause_var.get()))
        except ValueError:
            messagebox.showerror("P8 Tape", "The pause needs to be a number of seconds.")
            return
        speed = speed_names[speed_labels.index(speed_var.get())]
        cfg.update(cart=cart, pause=pause, speed=speed)
        save_settings(cfg)
        make_btn.state(["disabled"])
        load_now.grid_remove()
        make_status.config(text="Making the tape…")
        pic = picture[0]

        def work():
            try:
                samples, info = build_tape(cart, pic, pause, speed)
                write_wav(out, samples)
                events.put(("made", (out, info, samples.size / RATE)))
            except BaseException as e:
                events.put(("make_failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def go_load():
        if made[0]:
            wav_var.set(made[0])
            source_var.set("wav")
        tabs.select(main)

    make_btn.config(command=on_make)
    load_now.config(command=go_load)
    cart_chosen()

    # --- handle messages from the loading threads
    peak = [0.0]

    def poll():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == "log":
                    add_log(val)
                    if val.startswith(("Found:", "loading", "R Tape", "saved", "tone")):
                        status.config(text=val)
                elif kind == "progress":
                    load_bar["value"] = val[1]
                    status.config(text=f"Loading {val[0]}: {val[1]}%")
                elif kind == "level":
                    peak[0] = max(peak[0], val)
                elif kind == "made":
                    out, info, secs = val
                    made[0] = out
                    make_btn.state(["!disabled"])
                    m, s = divmod(round(secs), 60)
                    make_status.config(text=f"Saved {os.path.basename(out)} - {m}:{s:02d} long "
                                            f"({'; '.join(info)})")
                    load_now.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
                elif kind == "make_failed":
                    make_btn.state(["!disabled"])
                    make_status.config(text="")
                    messagebox.showerror("P8 Tape", f"Couldn't make the tape:\n{val}")
                elif kind == "finished":
                    boot.config(text="Boot PICO-8 loader")
                    status.config(text="PICO-8 closed")
                    add_log("PICO-8 closed")
                    update_hint()
                    start_monitor()
        except queue.Empty:
            pass
        db = 20 * math.log10(max(peak[0], 1e-6))
        meter["value"] = max(0, min(100, (db + 60) / 60 * 100))
        if monitor[0] or session.running():
            meter_text.config(text="no signal" if db < -50 else "too quiet" if db < -30
                              else "clipping - lower it" if db > -1 else "good")
        peak[0] *= 0.6                     # let the meter fall back smoothly
        win.after(60, poll)

    def on_close():
        remember()
        stop_monitor()
        session.stop()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)
    update_hint()
    start_monitor()
    poll()
    win.mainloop()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Load PICO-8 carts from cassette tape.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="turn a cart (and loading picture) into a WAV for tape")
    e.add_argument("cart", help=".p8 or .p8.png file")
    e.add_argument("--screen", help="128x128 loading picture; the raster bars cover "
                   "the outer 4 px on every side")
    e.add_argument("--pause", type=float, default=3.0,
                   help="seconds of silence after the Found: header (default 3)")
    e.add_argument("--speed", choices=list(SPEEDS), default="standard",
                   help="tape speed: " + ", ".join(speed_label(s) for s in SPEEDS))
    e.add_argument("-o", "--output", help="output WAV file")
    e.set_defaults(func=cmd_encode)

    pl = sub.add_parser("play", help="start PICO-8 and load from tape")
    pl.add_argument("--loader", default=None, help="loader.p8 to use (normally automatic)")
    pl.add_argument("--pico8", help="path to the PICO-8 program")
    pl.add_argument("--device", help="sound input device number or name ('list' to see them)")
    pl.add_argument("--rate", type=int, default=RATE)
    pl.add_argument("--wav", help="play this WAV instead of listening to a tape")
    pl.add_argument("--silent", action="store_true",
                    help="don't play the tape sound in PICO-8")
    pl.add_argument("--audible", action="store_true", help=argparse.SUPPRESS)
    pl.add_argument("--delay", type=float, default=1.0,
                    help="seconds between pressing X and the WAV starting")
    pl.add_argument("--speed", type=float, default=1.0, help=argparse.SUPPRESS)
    pl.set_defaults(func=cmd_play)

    d = sub.add_parser("decode", help="save the cart and picture from a WAV")
    d.add_argument("wav")
    d.add_argument("--out", default=".")
    d.set_defaults(func=cmd_decode)

    g = sub.add_parser("gui", help="open the loader window (the default)")
    g.set_defaults(func=lambda a: run_gui())

    if len(sys.argv) == 1:
        return run_gui()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
