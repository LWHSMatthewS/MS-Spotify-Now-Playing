# SPDX-License-Identifier: Unlicense
# Spotify Now Playing: neon purple edition, NO boot.py needed
# Adafruit Feather ESP32-S3 + 3.5" TFT FeatherWing (HX8357, 480x320)
# Based on LWHSTechnicalArts spotify.py.
#
# The board never writes to its own filesystem, so VS Code saving keeps working.
#
# TWO MODES:
#   1. SETUP MODE (SPOTIFY_REFRESH_TOKEN empty in settings.toml)
#      Paste your authorization code into the serial console. The board
#      prints a SPOTIFY_REFRESH_TOKEN line. Copy the line into settings.toml.
#   2. NORMAL MODE (SPOTIFY_REFRESH_TOKEN has a value)
#      Album art, title, artist, album, and a live progress bar.
#
# Needs on CIRCUITPY:
#   fonts/  (title.bin, artist.bin, small.bin, label.bin)
#           pinyin.bin turns Chinese song info into pinyin
#           cjk20.bin is optional: shows Chinese characters (set SHOW_PINYIN = False)
#   lib/    adafruit_hx8357.mpy, adafruit_requests.mpy,
#           adafruit_connection_manager.mpy, adafruit_binascii.mpy

import gc
import math
import os
import struct
import time

import board
import bitmaptools
import displayio
import socketpool
import ssl
import wifi
import adafruit_binascii
import adafruit_requests
import adafruit_hx8357
from fourwire import FourWire

try:
    import jpegio  # built into CircuitPython 9.2+ on most ESP32-S3 builds
except ImportError:
    jpegio = None

# ==================== CONFIGURATION ====================
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI") or "https://example.com/callback"
SPOTIFY_REFRESH_TOKEN = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip()

if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    raise RuntimeError("settings.toml needs SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET")

WIFI_NETWORKS = []
for _n in ("1", "2", "3"):
    _ssid = os.getenv("WIFI_SSID_" + _n)
    if _ssid:
        WIFI_NETWORKS.append((_ssid, os.getenv("WIFI_PASSWORD_" + _n) or ""))
if not WIFI_NETWORKS:
    raise RuntimeError("settings.toml has no WIFI_SSID_1 / WIFI_PASSWORD_1")

TOKEN_URL = "https://accounts.spotify.com/api/token"
# market=from_token drops the huge "available_markets" list from the reply.
CURRENT_TRACK_URL = "https://api.spotify.com/v1/me/player/currently-playing?market=from_token"
POLL_SECONDS = 20  # also polls right when the current song should end
ART_SWAPPED = True  # if album colors look wrong, change to False

_creds = SPOTIFY_CLIENT_ID + ":" + SPOTIFY_CLIENT_SECRET
AUTH_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Authorization": "Basic "
    + adafruit_binascii.b2a_base64(_creds.encode())[:-1].decode("ascii"),
}

# ==================== THEME ====================
BG_TOP = 0x0F0F0F
BG_BOTTOM = 0x000000
GLOW = 0x2E2E2E        # soft glow in the corner
CARD = 0x0C0C0C
CARD_EDGE = 0x2A2A2A
ACCENT = 0xB3121F      # deep red
ACCENT2 = 0xFF3B3B     # bright red
TEXT = 0xFFFFFF
TEXT2 = 0xBDBDBD
TEXT3 = 0x737373
TRACK = 0x262626
ERROR = 0xFF8A3D  # orange, stands apart from the red accent
WARN = 0xFFB35C

W, H = 480, 320
ART = 150
ART_X, ART_Y = 26, 50
TEXT_X = ART_X + ART + 26
TEXT_W = W - TEXT_X - 22
CARD_X, CARD_Y, CARD_W, CARD_H = 16, 224, 448, 80
BAR_X, BAR_Y, BAR_W, BAR_H = 44, 250, 392, 6


def mix(c1, c2, t):
    """Blend colour c1 toward c2 by t (0 = c1, 1 = c2)."""
    r = int(((c1 >> 16) & 255) * (1 - t) + ((c2 >> 16) & 255) * t)
    g = int(((c1 >> 8) & 255) * (1 - t) + ((c2 >> 8) & 255) * t)
    b = int((c1 & 255) * (1 - t) + (c2 & 255) * t)
    return (r << 16) | (g << 8) | b


# ==================== FONTS (LVGL .bin reader) ====================
class LVFont:
    """Reads LVGL binary fonts one glyph at a time, so a 6,800 character
    Chinese font costs almost no RAM."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.cache = {}
        self.subtables = []
        f = self.f
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = struct.unpack("<I", hdr[:4])[0]
            tag = hdr[4:8]
            if size == 0:
                break
            start = f.tell()
            if tag == b"head":
                d = f.read(size - 8)
                self.ascent = struct.unpack_from("<H", d, 8)[0]
                self.default_adv = struct.unpack_from("<H", d, 22)[0]
                self.loca32 = d[26] == 1
                self.adv_frac = d[28] == 1
                self.bpp = d[29]
                self.xy_bits = d[30]
                self.wh_bits = d[31]
                self.adv_bits = d[32]
            elif tag == b"cmap":
                d = f.read(size - 8)
                count = struct.unpack_from("<I", d, 0)[0]
                for i in range(count):
                    off, rstart, rlen, gstart, n, fmt = struct.unpack_from("<IIHHHB", d, 4 + 16 * i)
                    off -= 8
                    data = None
                    if fmt == 0:
                        data = bytes(d[off:off + n])
                    elif fmt == 1:
                        data = (struct.unpack_from("<%dH" % n, d, off),
                                struct.unpack_from("<%dH" % n, d, off + 2 * n))
                    elif fmt == 3:
                        data = struct.unpack_from("<%dH" % n, d, off)
                    self.subtables.append((rstart, rlen, gstart, fmt, data))
            elif tag == b"loca":
                self.glyph_count = struct.unpack("<I", f.read(4))[0]
                self.loca_start = start + 4
            elif tag == b"glyf":
                self.glyf_start = start - 8
            f.seek(start + size - 8)

    def _cid(self, cp):
        for rstart, rlen, gstart, fmt, data in self.subtables:
            if rstart <= cp < rstart + rlen:
                k = cp - rstart
                if fmt == 2:
                    return gstart + k
                if fmt == 0:
                    return gstart + data[k] if (data[k] or k == 0) else None
                keys = data if fmt == 3 else data[0]
                lo, hi = 0, len(keys) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if keys[mid] == k:
                        return gstart + (mid if fmt == 3 else data[1][mid])
                    if keys[mid] < k:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return None
        return None

    def glyph(self, cp):
        """Returns (bitmap, w, h, dx, dy, advance) or None."""
        g = self.cache.get(cp, 0)
        if g != 0:
            return g
        g = None
        cid = self._cid(cp)
        if cid and cid < self.glyph_count:
            f = self.f
            if self.loca32:
                f.seek(self.loca_start + cid * 4)
                off = struct.unpack("<I", f.read(4))[0]
            else:
                f.seek(self.loca_start + cid * 2)
                off = struct.unpack("<H", f.read(2))[0]
            f.seek(self.glyf_start + off)
            buf = f.read(256)
            pos = 0

            def bits(n):
                nonlocal pos
                v = 0
                for _ in range(n):
                    v = (v << 1) | ((buf[pos >> 3] >> (7 - (pos & 7))) & 1)
                    pos += 1
                return v

            def signed(v, n):
                return v - (1 << n) if v & (1 << (n - 1)) else v

            # monospace fonts (Chinese) store one shared width in the header
            adv = bits(self.adv_bits) if self.adv_bits else self.default_adv
            if self.adv_frac:
                adv = (adv + 8) >> 4
            dx = signed(bits(self.xy_bits), self.xy_bits)
            dy = signed(bits(self.xy_bits), self.xy_bits)
            gw = bits(self.wh_bits)
            gh = bits(self.wh_bits)
            bmp = displayio.Bitmap(max(gw, 1), max(gh, 1), 2)
            for y in range(gh):
                for x in range(gw):
                    if bits(self.bpp):
                        bmp[x, y] = 1
            g = (bmp, gw, gh, dx, dy, adv)
        if len(self.cache) > 500:
            self.cache = {}
        self.cache[cp] = g
        return g


class FontChain:
    """Tries each font in order: Latin first, then Chinese."""

    def __init__(self, *fonts):
        fonts = tuple(f for f in fonts if f)
        self.fonts = fonts
        self.ascent = fonts[0].ascent

    def glyph(self, cp):
        for font in self.fonts:
            g = font.glyph(cp)
            if g:
                return g
        return None


def text_width(font, text, tracking=0):
    w = 0
    for c in text:
        g = font.glyph(ord(c))
        if g:
            w += g[5] + tracking
    return w


def draw_text(bmp, font, text, x, baseline, tracking=0):
    for c in text:
        g = font.glyph(ord(c))
        if not g:
            continue
        gb, gw, gh, dx, dy, adv = g
        if gw and gh:
            tx = x + dx
            ty = baseline - gh - dy
            sx0, sy0 = max(0, -tx), max(0, -ty)
            sx1, sy1 = min(gw, bmp.width - tx), min(gh, bmp.height - ty)
            if sx1 > sx0 and sy1 > sy0:
                bitmaptools.blit(bmp, gb, tx + sx0, ty + sy0,
                                 x1=sx0, y1=sy0, x2=sx1, y2=sy1, skip_source_index=0)
        x += adv + tracking
    return x


def wrap(font, text, max_w, max_lines):
    """Pixel-accurate word wrap. Chinese wraps between characters.
    Adds an ellipsis when the text runs out of lines."""
    original = text.strip()
    # drop characters no font has, then tidy leftover commas and spaces
    text = "".join(c for c in text if c == " " or font.glyph(ord(c)))
    text = " ".join(text.split())
    while text and text[0] in ",;/-&. ":
        text = text[1:]
    while text and text[-1] in ",;/-& ":
        text = text[:-1]
    text = text.replace(" ,", ",").replace(",,", ",")
    if not text and original:
        text = "(unsupported characters)"
    lines = []
    while text and len(lines) < max_lines:
        w, cut, space = 0, len(text), -1
        for i, c in enumerate(text):
            g = font.glyph(ord(c))
            adv = g[5] if g else 0
            if c == " ":
                space = i
            if w + adv > max_w:
                cut = space if space > 0 else max(i, 1)
                break
            w += adv
        lines.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text and lines:
        last = lines[-1]
        ell = text_width(font, "…")
        while last and text_width(font, last) + ell > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


class TextBox:
    def __init__(self, parent, x, y, w, h, color, font, line_h=None, align="left", tracking=0):
        self.bmp = displayio.Bitmap(w, h, 2)
        self.pal = displayio.Palette(2)
        self.pal.make_transparent(0)
        self.pal[1] = color
        self.tg = displayio.TileGrid(self.bmp, pixel_shader=self.pal, x=x, y=y)
        parent.append(self.tg)
        self.font = font
        self.line_h = line_h or (font.ascent + 6)
        self.align = align
        self.tracking = tracking
        self.lines = []

    def set(self, text, max_lines=1, color=None):
        if color is not None:
            self.pal[1] = color
        lines = wrap(self.font, text, self.bmp.width, max_lines)
        if lines == self.lines:
            return len(lines)
        self.lines = lines
        self.bmp.fill(0)
        for i, line in enumerate(lines):
            x = 0
            if self.align == "right":
                x = self.bmp.width - text_width(self.font, line, self.tracking)
            draw_text(self.bmp, self.font, line, x,
                      self.font.ascent + i * self.line_h, self.tracking)
        return len(lines)


# ==================== SHAPES ====================
def rounded_tile(w, h, r, fill, back, x=0, y=0):
    """Rounded rectangle with a softened (anti-aliased) edge."""
    bmp = displayio.Bitmap(w, h, 3)
    pal = displayio.Palette(3)
    pal.make_transparent(0)
    pal[1] = fill
    pal[2] = mix(fill, back, 0.55)
    for row in range(h):
        if row < r:
            d = r - row - 0.5
        elif row >= h - r:
            d = row - (h - r) + 0.5
        else:
            d = 0
        if d > 0:
            inset = r - math.sqrt(max(0, r * r - d * d))
            full = math.ceil(inset)
            if full < w - full:
                bitmaptools.fill_region(bmp, full, row, w - full, row + 1, 1)
            soft = int(inset)
            if soft < full and soft < w - 1 - soft:
                bmp[soft, row] = 2
                bmp[w - 1 - soft, row] = 2
        else:
            bitmaptools.fill_region(bmp, 0, row, w, row + 1, 1)
    return displayio.TileGrid(bmp, pixel_shader=pal, x=x, y=y)


def background():
    shades = 40
    bmp = displayio.Bitmap(W, H, 64)
    pal = displayio.Palette(64)
    for i in range(shades):
        pal[i] = mix(BG_TOP, BG_BOTTOM, i / (shades - 1))
    band = H / shades
    for i in range(shades):
        bitmaptools.fill_region(bmp, 0, int(i * band), W, min(H, int((i + 1) * band) + 1), i)
    # aurora glow, top right
    cx, cy, levels = 420, -30, 12
    base = mix(BG_TOP, BG_BOTTOM, 0.15)
    for k in range(levels):
        idx = shades + k
        t = (k + 1) / levels
        pal[idx] = mix(base, GLOW, 0.55 * t * t)
        rad = 280 * (1 - k / levels)
        for row in range(max(0, int(cy - rad)), min(H, int(cy + rad))):
            half = math.sqrt(max(0, rad * rad - (row - cy) ** 2))
            x0, x1 = max(0, int(cx - half)), min(W, int(cx + half))
            if x1 > x0:
                bitmaptools.fill_region(bmp, x0, row, x1, row + 1, idx)
    return displayio.TileGrid(bmp, pixel_shader=pal)


# ==================== DISPLAY ====================
displayio.release_displays()
spi = board.SPI()
display_bus = FourWire(spi, command=board.D10, chip_select=board.D9, reset=None)
display = adafruit_hx8357.HX8357(display_bus, width=W, height=H)
display.auto_refresh = False

try:
    _cjk = LVFont("/fonts/cjk20.bin")
except OSError:
    _cjk = None  # no Chinese font on the board: those characters get skipped
    print("fonts/cjk20.bin not found: Chinese characters will not show")
f_title = FontChain(LVFont("/fonts/title.bin"), _cjk)
f_artist = FontChain(LVFont("/fonts/artist.bin"), _cjk)
f_small = FontChain(LVFont("/fonts/small.bin"), _cjk)
f_label = FontChain(LVFont("/fonts/label.bin"))


# ==================== PINYIN ====================
SHOW_PINYIN = True  # False shows Chinese characters instead (needs fonts/cjk20.bin)


class PinyinTable:
    """Looks up one character at a time from fonts/pinyin.bin (no big RAM use)."""

    def __init__(self, path):
        self.f = open(path, "rb")
        if self.f.read(4) != b"PY01":
            raise ValueError("bad pinyin.bin")
        self.start, count, blob_len = struct.unpack("<HHI", self.f.read(8))
        self.syllables = self.f.read(blob_len).decode().split("\n")
        self.table_at = self.f.tell()  # table starts right after the syllable list
        self.f.seek(0, 2)  # jump to the end to learn the file size
        self.end = self.start + (self.f.tell() - self.table_at) // 2

    def get(self, cp):
        if not self.start <= cp < self.end:
            return None
        self.f.seek(self.table_at + (cp - self.start) * 2)
        i = struct.unpack("<H", self.f.read(2))[0]
        return self.syllables[i - 1] if i else None


try:
    PINYIN = PinyinTable("/fonts/pinyin.bin") if SHOW_PINYIN else None
except OSError:
    PINYIN = None
    print("fonts/pinyin.bin not found: no pinyin")

# Chinese punctuation -> plain versions the fonts have
CN_PUNCT = {0x3000: " ", 0x3001: ",", 0x3002: ".", 0x300A: "\"", 0x300B: "\"",
            0x300C: "\"", 0x300D: "\"", 0x300E: "\"", 0x300F: "\"",
            0x3010: "[", 0x3011: "]", 0x30FB: "\u00b7"}
NO_SPACE_BEFORE = " ,.!?;:)]}\"'"
NO_SPACE_AFTER = " ([{\"'-/"


def romanize(text):
    """Swap Chinese characters for pinyin: 晴天 -> Qíng Tiān."""
    if PINYIN is None or not text:
        return text
    out = ""
    prev_han = False
    for c in text:
        cp = ord(c)
        if 0xFF01 <= cp <= 0xFF5E:  # full-width letters and symbols
            c = chr(cp - 0xFEE0)
            cp = ord(c)
        elif cp in CN_PUNCT:
            c = CN_PUNCT[cp]
            cp = ord(c)
        syl = PINYIN.get(cp)
        if syl:
            if out and out[-1] not in NO_SPACE_AFTER:
                out += " "
            out += syl
            prev_han = True
        else:
            if prev_han and c not in NO_SPACE_BEFORE:
                out += " "
            out += c
            prev_han = False
    return out

root = displayio.Group()
root.append(background())
BG_MID = mix(BG_TOP, BG_BOTTOM, 0.35)

# header
TextBox(root, 26, 16, 200, 20, ACCENT, f_label, tracking=2).set("NOW PLAYING")
status_box = TextBox(root, W - 26 - 200, 16, 200, 20, TEXT3, f_label, align="right", tracking=2)

# album art with neon glow
FRAME = mix(ACCENT, BG_MID, 0.35)
for grow, t in ((16, 0.93), (11, 0.86), (7, 0.76), (3, 0.6)):
    root.append(rounded_tile(ART + 2 * grow, ART + 2 * grow, 14 + grow,
                             mix(ACCENT, BG_MID, t), BG_MID, ART_X - grow, ART_Y - grow))
root.append(rounded_tile(ART + 4, ART + 4, 16, FRAME, BG_MID, ART_X - 2, ART_Y - 2))

art_slot = displayio.Group(x=ART_X, y=ART_Y)
root.append(art_slot)


def placeholder_art():
    g = displayio.Group()
    g.append(rounded_tile(ART, ART, 14, 0x161616, FRAME))
    c = ART // 2
    for d, col in ((112, 0x080808), (104, 0x141414), (96, 0x080808), (80, 0x141414),
                   (72, 0x080808), (36, ACCENT), (8, 0x080808)):
        g.append(rounded_tile(d, d, d // 2, col, 0x161616, c - d // 2, c - d // 2))
    return g


PLACEHOLDER = placeholder_art()
art_slot.append(PLACEHOLDER)

# rounded corners for album art: masks painted in the frame colour
R = 14
corner = displayio.Bitmap(R, R, 2)
corner_pal = displayio.Palette(2)
corner_pal.make_transparent(0)
corner_pal[1] = FRAME
for cy in range(R):
    for cx in range(R):
        if math.sqrt((R - cx - 0.5) ** 2 + (R - cy - 0.5) ** 2) > R - 0.3:
            corner[cx, cy] = 1
for fx, fy, px, py in ((False, False, 0, 0), (True, False, ART - R, 0),
                       (False, True, 0, ART - R), (True, True, ART - R, ART - R)):
    tg = displayio.TileGrid(corner, pixel_shader=corner_pal, x=ART_X + px, y=ART_Y + py)
    tg.flip_x, tg.flip_y = fx, fy
    root.append(tg)

# track text
title_box = TextBox(root, TEXT_X, ART_Y + 2, TEXT_W, 74, TEXT, f_title, line_h=33)
artist_box = TextBox(root, TEXT_X, 0, TEXT_W, 28, TEXT2, f_artist)
album_box = TextBox(root, TEXT_X, 0, TEXT_W, 46, TEXT3, f_small, line_h=21)

# progress card
root.append(rounded_tile(CARD_W, CARD_H, 20, CARD_EDGE, BG_BOTTOM, CARD_X, CARD_Y))
root.append(rounded_tile(CARD_W - 2, CARD_H - 2, 19, CARD, CARD_EDGE, CARD_X + 1, CARD_Y + 1))
root.append(rounded_tile(BAR_W, BAR_H, BAR_H // 2, TRACK, CARD, BAR_X, BAR_Y))

bar_bmp = displayio.Bitmap(BAR_W, BAR_H, 16)
bar_pal = displayio.Palette(16)
bar_pal.make_transparent(0)
for _i in range(1, 16):
    bar_pal[_i] = mix(ACCENT, ACCENT2, (_i - 1) / 14)
root.append(displayio.TileGrid(bar_bmp, pixel_shader=bar_pal, x=BAR_X, y=BAR_Y))

knob = displayio.Group(x=BAR_X - 8, y=BAR_Y + BAR_H // 2 - 8)
knob.append(rounded_tile(16, 16, 8, mix(ACCENT2, CARD, 0.65), CARD))
knob.append(rounded_tile(10, 10, 5, mix(ACCENT2, CARD, 0.3), CARD, 3, 3))
knob.append(rounded_tile(6, 6, 3, TEXT, ACCENT2, 5, 5))
root.append(knob)

elapsed_box = TextBox(root, BAR_X, BAR_Y + 16, 90, 22, TEXT2, f_small)
total_box = TextBox(root, BAR_X + BAR_W - 90, BAR_Y + 16, 90, 22, TEXT3, f_small, align="right")

display.root_group = root
bar_px = 0


def set_bar(px):
    """Gradient pill from 0 to px. Only redraws the columns which changed."""
    global bar_px
    px = max(0, min(BAR_W, int(px)))
    if px == bar_px:
        return False
    r = BAR_H / 2
    lo = max(0, min(px, bar_px) - BAR_H)
    hi = min(BAR_W, max(px, bar_px) + 1)
    bitmaptools.fill_region(bar_bmp, lo, 0, hi, BAR_H, 0)
    for x in range(lo, min(px, hi)):
        if x < r:
            d = r - x - 0.5
        elif x > px - r:
            d = x - (px - r) + 0.5
        else:
            d = 0
        half = math.sqrt(max(0, r * r - d * d)) if d > 0 else r
        y0, y1 = int(round(r - half)), int(round(r + half))
        if y1 > y0:
            bitmaptools.fill_region(bar_bmp, x, y0, x + 1, y1, 1 + x * 14 // BAR_W)
    bar_px = px
    knob.x = BAR_X + px - 8
    return True


def layout_text(title, artist, album):
    n = title_box.set(title, max_lines=2)
    y = title_box.tg.y + n * 33 + 6
    artist_box.tg.y = y
    artist_box.set(artist)
    album_box.tg.y = y + 28
    album_box.set(album, max_lines=2 if n == 1 else 1)


def set_status(text, color=TEXT3):
    status_box.set(text.upper(), color=color)


def fmt_time(ms):
    s = max(0, int(ms // 1000))
    return "%d:%02d" % (s // 60, s % 60)


def show_message(title, line2="", line3="", status="", status_color=TEXT3):
    global art_url
    show_art(None)
    art_url = None
    layout_text(title, line2, line3)
    set_status(status, status_color)
    set_bar(0)
    elapsed_box.set("")
    total_box.set("")
    display.refresh()


# ==================== ALBUM ART ====================
art_tg = None
art_url = None


def show_art(tg):
    global art_tg
    if art_tg is not None:
        art_slot.remove(art_tg)
    art_tg = tg
    PLACEHOLDER.hidden = tg is not None
    if tg is not None:
        art_slot.append(tg)


def load_art(images):
    """Download the album cover and decode it straight into RAM."""
    global art_url
    if not images or jpegio is None:
        if jpegio is None:
            print("jpegio missing: showing placeholder art")
        show_art(None)
        art_url = None
        return
    # smallest image which still fills the 150px slot (Spotify sends 640/300/64)
    images = sorted(images, key=lambda im: im.get("width") or 0)
    pick = images[-1]
    for im in images:
        if (im.get("width") or 0) >= ART:
            pick = im
            break
    if pick["url"] == art_url:
        return
    response = None
    try:
        show_art(None)
        gc.collect()
        response = requests.get(pick["url"])
        data = response.content
        response.close()
        response = None
        decoder = jpegio.JpegDecoder()
        w, h = decoder.open(data)
        scale = 0
        while scale < 3 and (w >> (scale + 1)) >= ART:
            scale += 1
        bw, bh = w >> scale, h >> scale
        bmp = displayio.Bitmap(bw, bh, 65535)
        decoder.decode(bmp, scale=scale)
        del data
        space = displayio.Colorspace.RGB565_SWAPPED if ART_SWAPPED else displayio.Colorspace.RGB565
        tg = displayio.TileGrid(
            bmp,
            pixel_shader=displayio.ColorConverter(input_colorspace=space),
            x=(ART - bw) // 2,
            y=(ART - bh) // 2,
        )
        show_art(tg)
        art_url = pick["url"]
    except Exception as error:
        print("Album art error:", error)
        show_art(None)
        art_url = None
    finally:
        if response is not None:
            response.close()
        gc.collect()


# ==================== WI-FI ====================
def connect_wifi():
    while True:
        for ssid, pw in WIFI_NETWORKS:
            show_message("Connecting", ssid, "", "wifi", WARN)
            try:
                wifi.radio.connect(ssid, pw)
                print("Connected to", ssid, "IP:", wifi.radio.ipv4_address)
                return
            except Exception as error:
                print("Could not join", ssid, ":", error)
        show_message("No Wi-Fi found", "Retrying in 10 seconds", "", "offline", ERROR)
        time.sleep(10)


connect_wifi()
pool = socketpool.SocketPool(wifi.radio)
requests = adafruit_requests.Session(pool, ssl.create_default_context())


# ==================== SPOTIFY AUTH ====================
refresh_token = SPOTIFY_REFRESH_TOKEN


def post_token(payload):
    response = None
    try:
        response = requests.post(TOKEN_URL, data=payload, headers=AUTH_HEADERS)
        data = response.json()
        if response.status_code != 200:
            print("Token request failed:", response.status_code, data)
            return None
        return data
    except Exception as error:
        print("Token request error:", error)
        return None
    finally:
        if response is not None:
            response.close()


def fresh_access_token():
    global refresh_token
    data = post_token({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if data and "refresh_token" in data:
        refresh_token = data["refresh_token"]  # kept in RAM only
    return data


def setup_mode():
    show_message("Setup mode", "Paste your auth code into the serial console",
                 "", "setup", ACCENT2)
    print("\n" + "=" * 60)
    print("SETUP MODE: SPOTIFY_REFRESH_TOKEN is empty in settings.toml")
    print("Paste the code (or the whole redirect URL) and press Enter.")
    print("=" * 60)
    while True:
        raw = input("Authorization code: ").strip()
        if "code=" in raw:
            raw = raw.split("code=", 1)[1]
        code = raw.split("&", 1)[0].strip()
        if not code:
            continue
        data = post_token({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": SPOTIFY_REDIRECT_URI})
        if data and "refresh_token" in data:
            print("\n" + "=" * 60)
            print("SUCCESS. Copy this whole line into settings.toml:\n")
            print('SPOTIFY_REFRESH_TOKEN = "' + data["refresh_token"] + '"')
            print("\nSave settings.toml and the board restarts in normal mode.")
            print("=" * 60)
            show_message("Token ready", "Copy the line from the serial console",
                         "into settings.toml, then save", "done", ACCENT2)
            while True:
                time.sleep(60)
        print("That code did not work. Codes expire ~10 min after you get them")
        print("and work once. Get a fresh code and paste again.")
        show_message("Code rejected", "Get a fresh code and paste again", "", "setup", ERROR)


if not refresh_token:
    setup_mode()


# ==================== NOW PLAYING ====================
def fetch_now_playing(access_token):
    """Returns (kind, data). kind: ok, idle, expired, error, slow."""
    response = None
    try:
        response = requests.get(CURRENT_TRACK_URL,
                                headers={"Authorization": "Bearer " + access_token})
        code = response.status_code
        if code == 200:
            return "ok", response.json()
        if code == 204:
            return "idle", None
        if code == 401:
            return "expired", None
        if code == 429:
            try:
                wait = int(response.headers.get("retry-after", "30"))
            except ValueError:
                wait = 30
            return "slow", wait
        body = response.text
        print("HTTP", code, body)
        if code == 403:
            return "error", "premium needed" if "premium" in body.lower() else "not allowed"
        return "error", "http " + str(code)
    except Exception as error:
        print("Request error:", error)
        return "error", "network"
    finally:
        if response is not None:
            response.close()


show_message("Spotify", "Signing in…", "", "loading", ACCENT)

token_json = None
token_expires_at = 0.0
track_id = None
duration = 0
progress_at = 0
fetched_at = time.monotonic()
playing = False
next_poll = 0.0
shown_second = -1

while True:
    now = time.monotonic()

    if now >= next_poll:
        next_poll = now + POLL_SECONDS
        if not wifi.radio.connected:
            connect_wifi()
        if token_json is None or now >= token_expires_at:
            token_json = fresh_access_token()
            if token_json is None:
                set_status("token failed", ERROR)
                display.refresh()
                next_poll = now + 30
                continue
            token_expires_at = now + token_json.get("expires_in", 3600) - 60

        kind, data = fetch_now_playing(token_json["access_token"])
        now = time.monotonic()

        if kind == "ok" and data.get("item"):
            item = data["item"]
            if item.get("id") != track_id:
                track_id = item.get("id")
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                album = item.get("album", {})
                print("Now playing:", artists, "-", item.get("name"))
                layout_text(romanize(item.get("name", "")), romanize(artists),
                            romanize(album.get("name", "")))
                load_art(album.get("images"))
                now = time.monotonic()
            duration = item.get("duration_ms") or 1
            progress_at = data.get("progress_ms") or 0
            fetched_at = now
            playing = bool(data.get("is_playing"))
            set_status("playing" if playing else "paused", ACCENT2 if playing else TEXT3)
            total_box.set(fmt_time(duration))
            shown_second = -1
            if playing:  # check again right after the song should end
                next_poll = min(next_poll, now + (duration - progress_at) / 1000 + 1.5)
        elif kind == "ok":
            kind_name = data.get("currently_playing_type", "")
            track_id = None
            show_message("Advertisement" if kind_name == "ad" else "Podcast playing",
                         "Back to music soon" if kind_name == "ad" else "Podcasts are not shown",
                         "", kind_name or "idle")
        elif kind == "idle":
            track_id = None
            show_message("Nothing playing", "Start a song on Spotify", "", "idle")
        elif kind == "expired":
            token_json = None
            next_poll = now
        elif kind == "slow":
            set_status("rate limited", WARN)
            next_poll = now + data
        else:
            set_status(data, ERROR)
        display.refresh()

    # smooth progress between polls
    if track_id is not None:
        pos = progress_at + ((now - fetched_at) * 1000 if playing else 0)
        pos = min(pos, duration)
        changed = set_bar(pos * BAR_W / duration)
        sec = int(pos // 1000)
        if sec != shown_second:
            shown_second = sec
            elapsed_box.set(fmt_time(pos))
            changed = True
        if changed:
            display.refresh()

    time.sleep(0.1)
