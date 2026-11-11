# SPDX-License-Identifier: Unlicense
# Spotify Now Playing, NO boot.py version
# Adafruit Feather ESP32-S3 + 3.5" TFT FeatherWing (HX8357, 480x320)
# Based on LWHSTechnicalArts spotify.py.
#
# The board never writes to its own filesystem, so VS Code saving keeps working.
#
# TWO MODES:
#   1. SETUP MODE (runs when SPOTIFY_REFRESH_TOKEN is empty in settings.toml)
#      Asks for your authorization code in the serial console, trades the code
#      with Spotify, and PRINTS your refresh token. Copy the printed token into
#      settings.toml, save, and the board restarts in normal mode.
#   2. NORMAL MODE (runs when SPOTIFY_REFRESH_TOKEN has a value)
#      Shows the current song on the screen, refreshing every 30 seconds.

import os
import time
import board
import displayio
import socketpool
import ssl
import terminalio
import wifi
import adafruit_binascii
import adafruit_requests
from adafruit_display_text import bitmap_label
from fourwire import FourWire
import adafruit_hx8357

# ==================== CONFIGURATION ====================
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI") or "https://example.com/callback"
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN") or ""

for _key, _value in (
    ("SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID),
    ("SPOTIFY_CLIENT_SECRET", SPOTIFY_CLIENT_SECRET),
):
    if not _value:
        raise RuntimeError("settings.toml is missing " + _key)

# Up to three Wi-Fi networks, tried in order (school, home, hotspot).
WIFI_NETWORKS = []
for _n in ("1", "2", "3"):
    _ssid = os.getenv("WIFI_SSID_" + _n)
    _pw = os.getenv("WIFI_PASSWORD_" + _n)
    if _ssid:
        WIFI_NETWORKS.append((_ssid, _pw or ""))
if not WIFI_NETWORKS:
    raise RuntimeError("settings.toml has no WIFI_SSID_1 / WIFI_PASSWORD_1")

# ==================== SETTINGS ====================
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENT_TRACK_URL = "https://api.spotify.com/v1/me/player/currently-playing?market=from_token"
POLL_SECONDS = 30  # dev-mode quota is shared per developer account, be polite
MAX_LINE = 25      # characters per line at scale 3 on a 480px-wide screen

_credentials = SPOTIFY_CLIENT_ID + ":" + SPOTIFY_CLIENT_SECRET
_encoded = adafruit_binascii.b2a_base64(_credentials.encode())[:-1].decode("ascii")
AUTH_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Authorization": "Basic " + _encoded,
}

# Held in RAM only. Updated if Spotify hands back a new one during a run.
refresh_token = SPOTIFY_REFRESH_TOKEN.strip()

# ==================== DISPLAY SETUP ====================
displayio.release_displays()
spi = board.SPI()
display_bus = FourWire(spi, command=board.D10, chip_select=board.D9, reset=None)
display = adafruit_hx8357.HX8357(display_bus, width=480, height=320)


# ==================== TEXT WRAPPING ====================
def wrap_text(text, max_line_length):
    words = text.split()
    wrapped_lines = []
    current_line = ""
    for word in words:
        if len(current_line) + len(word) + 1 <= max_line_length:
            current_line = current_line + " " + word if current_line else word
        else:
            wrapped_lines.append(current_line)
            current_line = word
    if current_line:
        wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


# ==================== DRAWING ====================
def draw(artist="", track="", album="", status="", status_color=0x888888):
    group = displayio.Group()

    artist_label = bitmap_label.Label(
        terminalio.FONT, text=wrap_text(artist, MAX_LINE), scale=3, color=0xFFFF00
    )
    artist_label.x = 10
    artist_label.y = 30
    group.append(artist_label)

    track_label = bitmap_label.Label(
        terminalio.FONT, text=wrap_text(track, MAX_LINE), scale=3, color=0x00FF00
    )
    track_label.x = 10
    track_label.y = artist_label.y + artist_label.bounding_box[3] * 3 + 20
    group.append(track_label)

    album_label = bitmap_label.Label(
        terminalio.FONT, text=wrap_text(album, MAX_LINE + 12), scale=2, color=0xAAAAAA
    )
    album_label.x = 10
    album_label.y = track_label.y + track_label.bounding_box[3] * 3 + 20
    group.append(album_label)

    status_label = bitmap_label.Label(
        terminalio.FONT, text=wrap_text(status, 39), scale=2, color=status_color
    )
    status_label.x = 10
    status_label.y = 295
    group.append(status_label)

    display.root_group = group


def show_status(message, color=0x888888):
    print(message)
    draw(status=message, status_color=color)


# ==================== INTERNET CONNECTION ====================
def connect_wifi():
    while True:
        for ssid, pw in WIFI_NETWORKS:
            show_status("Connecting to " + ssid + "...")
            try:
                wifi.radio.connect(ssid, pw)
                print("Connected to", ssid, "IP:", wifi.radio.ipv4_address)
                return
            except Exception as error:
                print("Could not join", ssid, ":", error)
        show_status("No Wi-Fi found, retrying in 10s", 0xFF8800)
        time.sleep(10)


connect_wifi()
pool = socketpool.SocketPool(wifi.radio)
requests = adafruit_requests.Session(pool, ssl.create_default_context())


# ==================== TOKEN REQUESTS ====================
def post_to_token_endpoint(payload):
    """POST to Spotify's token endpoint. Returns parsed JSON or None."""
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


def get_fresh_access_token():
    """Trade the refresh token for a new access token. No file writes."""
    global refresh_token
    data = post_to_token_endpoint(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    if data and "refresh_token" in data:
        refresh_token = data["refresh_token"]
    return data


# ==================== SETUP MODE ====================
def setup_mode():
    """One-time: auth code in, refresh token printed out. Nothing saved."""
    show_status("SETUP: paste auth code in serial console", 0x00FFFF)
    print()
    print("=" * 60)
    print("SETUP MODE: SPOTIFY_REFRESH_TOKEN is empty in settings.toml")
    print("Paste ONLY the part after code= (stop before any &).")
    print("=" * 60)
    while True:
        raw = input("Authorization code: ").strip()
        # Forgive common paste mistakes: full URL, or trailing &ubi=...
        if "code=" in raw:
            raw = raw.split("code=", 1)[1]
        code = raw.split("&", 1)[0].strip()
        if not code:
            continue
        data = post_to_token_endpoint(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            }
        )
        if data and "refresh_token" in data:
            print()
            print("=" * 60)
            print("SUCCESS. Copy this whole line into settings.toml:")
            print()
            print('SPOTIFY_REFRESH_TOKEN = "' + data["refresh_token"] + '"')
            print()
            print("Save settings.toml and the board restarts in normal mode.")
            print("=" * 60)
            show_status("SUCCESS: copy token from console", 0x00FF00)
            while True:
                time.sleep(60)
        print("That code did not work. Codes expire ~10 min after you get")
        print("them and work once. Get a fresh code and paste again.")
        show_status("Code rejected: get a fresh one", 0xFF0000)


if not refresh_token:
    setup_mode()


# ==================== CURRENTLY PLAYING TRACK ====================
def get_currently_playing_track(access_token):
    headers = {"Authorization": "Bearer " + access_token}
    response = None
    try:
        response = requests.get(CURRENT_TRACK_URL, headers=headers)
        code = response.status_code
        if code == 200:
            return response.json()
        if code == 204:
            show_status("Connected - nothing playing")
            return None
        if code == 401:
            print("401 Unauthorized - access token expired.")
            return None
        if code == 403:
            body = response.text
            print("403 Forbidden. Spotify said:", body)
            if "premium" in body.lower():
                show_status("403: app owner needs Premium", 0xFF0000)
            else:
                show_status("403: user not on app allowlist", 0xFF0000)
            return None
        if code == 429:
            retry_after = response.headers.get("retry-after", "?")
            print("429 rate limited. Retry-After:", retry_after)
            show_status("429: slow down (retry " + str(retry_after) + "s)", 0xFF8800)
            return None
        print("Unexpected status code:", code, response.text)
        show_status("HTTP " + str(code), 0xFF0000)
        return None
    except Exception as error:
        print("Request error:", error)
        show_status("Network error", 0xFF0000)
        return None
    finally:
        if response is not None:
            response.close()


def extract_track(cp_json):
    if not cp_json:
        return None
    item = cp_json.get("item")
    if not item:
        return None  # ad, podcast, or nothing loaded yet
    artists = ", ".join(artist["name"] for artist in item["artists"])
    album = item.get("album", {}).get("name", "")
    return (artists, item["name"], album)


# ==================== MAIN ====================
show_status("Authorizing with Spotify...")

token_json = None
token_expires_at = 0.0
last_seen = None

while True:
    if not wifi.radio.connected:
        connect_wifi()

    if token_json is None or time.monotonic() >= token_expires_at:
        token_json = get_fresh_access_token()
        if token_json is None:
            show_status("Token refresh failed - see console", 0xFF0000)
            time.sleep(30)
            continue
        expires_in = token_json.get("expires_in", 3600)
        token_expires_at = time.monotonic() + expires_in - 60
        print("Got a token, valid for", expires_in, "seconds.")

    response = get_currently_playing_track(token_json["access_token"])

    if response is None:
        token_expires_at = 0.0
        last_seen = None
        time.sleep(POLL_SECONDS)
        continue

    current = extract_track(response)
    if current is None:
        if last_seen is not None:
            show_status("Advertisement or podcast")
        last_seen = None
    elif current != last_seen:
        artist, track, album = current
        print("Now playing: {} - {}".format(artist, track))
        draw(artist=artist, track=track, album=album, status="")
        last_seen = current

    time.sleep(POLL_SECONDS)
