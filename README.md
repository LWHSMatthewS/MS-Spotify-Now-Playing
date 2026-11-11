# Spotify Now Playing for CircuitPython (no boot.py)

Shows the song you're playing on Spotify on an Adafruit Feather ESP32-S3 with the 3.5" TFT FeatherWing (HX8357, 480x320), running CircuitPython 10.

Based on the LWHS Technical Arts `spotify.py` example, changed so **no `boot.py` is needed**. The original saved the Spotify token to `token.json`, which required `boot.py` to make the drive writable for the board (and read-only for your computer). These versions keep the refresh token in `settings.toml` instead, so the board never writes a file and saving from your editor keeps working.

## Two versions

| | `basic/` | `neon-ui/` |
|---|---|---|
| Look | Plain text (artist, track, album) | Black and red theme, rounded cards, glow |
| Album cover | No | Yes (needs the built-in `jpegio` module) |
| Progress bar | No | Live bar with elapsed and total time |
| Fonts | Built-in terminal font | Exo 2 and Orbitron (in `fonts/`, about 80 KB) |
| Chinese titles | Not shown | Shown as pinyin with tone marks |
| Space on CIRCUITPY | about 11 KB + libraries | about 120 KB + libraries |

Pick one and copy its `code.py` (plus `fonts/` for the neon UI) to CIRCUITPY.

## Libraries (CircuitPython 10.x bundle, circuitpython.org/libraries)

Both versions:
```
lib/adafruit_hx8357.mpy
lib/adafruit_requests.mpy
lib/adafruit_connection_manager.mpy
lib/adafruit_binascii.mpy
```
The basic version also needs:
```
lib/adafruit_display_text/
lib/adafruit_ticks.mpy
```

## Setup

1. Create an app at developer.spotify.com/dashboard. Add `https://example.com/callback` as a Redirect URI and copy the Client ID and Client Secret.
2. Copy `settings.toml.example` to CIRCUITPY, rename it `settings.toml`, and fill in Wi-Fi plus the Client ID and Secret. Leave `SPOTIFY_REFRESH_TOKEN = ""`.
3. Copy your chosen `code.py` (and `fonts/` for the neon UI) plus the libraries to CIRCUITPY.
4. Open the serial console. The screen says it's waiting for an authorization code.
5. Open this link (put in your Client ID), log in, and copy the URL you land on:
   ```
   https://accounts.spotify.com/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=https://example.com/callback&scope=user-read-currently-playing
   ```
6. Wait for the `Authorization code:` prompt, paste the URL, and press Enter within 10 minutes. Codes work once.
7. The console prints a `SPOTIFY_REFRESH_TOKEN = "..."` line. Paste the line into `settings.toml` and save. The board restarts and shows your song.

## Neon UI settings (top of `neon-ui/code.py`)

- `ACCENT`, `ACCENT2`: highlight colors (deep red and bright red by default)
- `SHOW_PINYIN`: `False` shows Chinese characters instead (needs a `cjk20.bin` font, about 341 KB, not included)
- `ART_SWAPPED`: set to `False` if album cover colors look wrong
- `POLL_SECONDS`: how often to check Spotify (default 20)

## Troubleshooting

- `no module named ...`: copy that library from the bundle into `lib/`.
- Pasted URL causes `SyntaxError` in the REPL: `code.py` crashed before the setup prompt. Fix the error shown above it, press Ctrl+D, and wait for `Authorization code:`.
- "Code rejected": the code expired or was already used. Get a fresh one.
- `403`: the Spotify app owner needs Premium, or your account isn't on the app's user list (Dashboard, User Management).
- Mac error -43 copying `fonts/`: create the folder on CIRCUITPY by hand and drag the `.bin` files in, or use `cp -X` in Terminal.
- To revoke access, remove the app at spotify.com/account/apps.

## Security

Your Client Secret and refresh token together give read access to your listening activity. Keep them in `settings.toml` only. `.gitignore` already excludes the file.

## Credits

- Original example: LWHS Technical Arts (Unlicense)
- Fonts in `neon-ui/fonts/` are bitmap conversions of Exo 2 and Orbitron, both under the SIL Open Font License 1.1
- Pinyin table built from pypinyin data (MIT License)
