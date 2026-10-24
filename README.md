# Device Cast

Cast a macOS screen to a TV on the local network, as a mirror or as an extended display.

Many TVs already support AirPlay, Google Cast or Miracast. If yours does, use that. Device Cast is for
TVs that do not: it works with any DLNA renderer, and with much lower latency on Samsung Tizen TVs
through a small receiver app.

| Receiver | How | Latency (measured) |
|---|---|---|
| Any DLNA TV | MPEG-TS stream played by the TV's media player | 2-3 s |
| Samsung Tizen TV with the receiver app | changed screen regions sent as tiles over WebSocket | 30-60 ms |
| Any modern browser | same as above, using `receiver/receiver.html` | similar |

## Features

- **Tile streaming.** Only the regions of the screen that changed are sent, as JPEG tiles at native
  resolution. Typing or moving a window costs a few kilobytes.
- **Adaptive quality.** Large moving regions are sent at reduced resolution and refined when they stop
  moving. When the network saturates, moving regions are reduced further; static text stays sharp.
- **Automatic video mode.** When a video starts playing, the sender switches to H.264 for smooth playback
  and switches back to tiles when it stops.
- **Separate cursor channel.** The pointer position is sent 60 times per second and drawn by the receiver,
  so it stays smooth even when the network is busy.
- **Extended display.** With [DeskPad](https://github.com/Stengo/DeskPad) the TV becomes a second
  display. DeskPad is started only while casting.
- **Menu bar app** for everyday use, plus a command-line interface.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools (`xcode-select --install`), for `swiftc`
- [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`), used by the DLNA and H.264 modes
- Optional: DeskPad, for the extended display
- For the Samsung receiver: Tizen Studio command-line tools and a TV in Developer Mode

## Quick start

```bash
git clone https://github.com/<user>/device-cast.git
cd device-cast
app/build.sh                 # builds "Device Cast.app" in the project folder
open "Device Cast.app"
```

The app adds an icon to the menu bar and the Dock, finds TVs on the network and starts casting to the
selected one. macOS asks for the **Screen Recording** and **Local Network** permissions on first use.

Keep the app in the project folder: it runs the scripts in `sender/` next to it and stores its state in
`data/`.

### Menu

| Item | Purpose |
|---|---|
| Start / Stop casting | also available from the Dock menu, or by opening the app again |
| TV | TVs found on the network; the choice is remembered by UPnP identifier, not IP address |
| Screen to cast | main display, another display, or the TV as an extended display (DeskPad) |
| Quality | 540p, 720p or 1080p |
| Low latency | tiles mode (on) or H.264 video with audio (off) |
| Automatic quality | lets the sender reduce moving regions when the network saturates |
| Automatic video | switches to H.264 while a video is playing |
| Open log | `data/devicecast.log` |

## Command line

```bash
python3 sender/devicecast.py list                   # DLNA renderers on the network
python3 sender/devicecast.py devices                # displays and audio devices
python3 sender/devicecast.py cast                   # remembered TV, the only one found, or ask
python3 sender/devicecast.py cast --tv "Living Room" --width 1920 --height 1080
python3 sender/devicecast.py stop
```

`cast` uses the receiver app when it is installed on the TV and falls back to DLNA otherwise
(`--mode dlna|tizen|auto`). Run `python3 sender/devicecast.py cast --help` for all options.

The low-latency server can also be started directly, for example to use a browser as the receiver:

```bash
python3 sender/server.py --codec tiles --allow 192.0.2.20
```

Then open `http://<computer-ip>:8091/receiver.html` on the receiving device.

## Samsung Tizen receiver

The receiver app (`receiver/tizen/`) is a small loader: it finds the sender on the TV's subnet, downloads
`receiver.js` from it and runs it. Updates to the receiver therefore do not require reinstalling the app.

1. Install the Tizen Studio command-line tools. By default the scripts look for them in
   `tools.noindex/tizen-studio` inside the project; set `TIZEN_HOME` to use another location.
2. Create an author certificate and a security profile:

   ```bash
   tizen certificate -a devicecast -p <password> -f devicecast
   tizen security-profiles add -n devicecast -a ~/tizen-studio-data/keystore/author/devicecast.p12 -p <password>
   ```

3. On the TV, open Apps, type `12345` on the remote, enable Developer Mode, enter the computer's IP
   address and restart the TV.
4. Install the app:

   ```bash
   receiver/tizen/install.sh <tv-ip> devicecast
   ```

The sender opens the app on the TV when casting starts and closes it when casting stops.

## How it works

```
macOS                                                    TV
ScreenCaptureKit -> screencap (dirty regions, JPEG) --+
ffmpeg (H.264, while a video plays) ------------------+-> server.py :8091 -> WebSocket -> receiver.js
cursor position and shape ----------------------------+                                   canvas + <video>
```

- `sender/screencap.swift` captures the display with ScreenCaptureKit, merges the dirty rectangles on a
  64 px grid, encodes tiles with Core Image and detects video playback. It is compiled on first use into
  `tools.noindex/screencap`.
- `sender/server.py` serves the receiver, forwards tiles and cursor updates, and applies flow control: it
  never sends more than the receiver has confirmed drawing, so latency does not build up on slow networks;
  pending changes are merged instead.
- `receiver/receiver.js` draws tiles on a canvas, overlays the cursor and plays the H.264 stream through
  Media Source Extensions when the sender switches to video.
- `sender/devicecast.py` handles discovery (SSDP), the DLNA fallback, configuration and the hand-off to
  `server.py`.

## Configuration

`data/config.json` is written by the app. Keys that can also be edited by hand:

| Key | Values |
|---|---|
| `codec` | `tiles` (default with low latency), `jpeg`, `h264` |
| `mode` | `auto` (default), `tizen`, `dlna` |
| `auto_quality`, `auto_video` | `true` / `false` |
| `jpeg_q` | JPEG quality for the `jpeg` codec, 2 (best) to 31 |
| `extra_args` | extra arguments passed to `devicecast.py cast` |

## Network

Tile streaming is as smooth as the network between the computer and the TV. A wired TV and a computer
close to the router give the best results. On Wi-Fi, packet loss shows up as short pauses; the log
reports throughput, waits and receiver timing every 10 seconds.

On Macs, AWDL (used by AirDrop, Universal Control and Sidecar) periodically switches the Wi-Fi radio to
other channels and causes latency spikes. Disabling it while casting can help:

```bash
sudo ifconfig awdl0 down     # sudo ifconfig awdl0 up to restore it
```

## Security

The stream is not encrypted or authenticated. Use Device Cast only on a network you trust.

- The low-latency server only answers the TV it was started for, loopback and addresses given with
  `--allow`. Started without `--tv` or `--allow`, it answers any address and logs a warning.
- The DLNA server binds to the interface that reaches the TV and only answers the TV.
- Device descriptions found through SSDP are size-limited, must come from the address that answered the
  discovery request, and are rejected if they contain DTD or entity declarations.
- Signing certificates, Tizen Studio and local state are kept out of the repository (`tools.noindex/`,
  `data/`).

## Troubleshooting

| Symptom | Cause |
|---|---|
| The TV shows nothing and the log says "no data from capture" | Screen Recording permission missing for Device Cast |
| No TV found | Local Network permission missing, or the TV is on another network |
| The TV app keeps looking for the sender | The sender is not running, or a firewall blocks port 8091 |
| Pauses of a few hundred milliseconds | Packet loss on Wi-Fi; see [Network](#network) |
| Video looks soft | Automatic quality reduced it on a saturated network; disable it in the Quality menu |

## Third-party components

- [Space Grotesk](https://github.com/floriankarsten/space-grotesk) and
  [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) fonts, bundled with the Tizen receiver,
  SIL Open Font License 1.1 (`receiver/tizen/fonts/`).
- [DeskPad](https://github.com/Stengo/DeskPad) and [ffmpeg](https://ffmpeg.org) are installed separately
  and are not distributed with this project.

## Project layout

```
app/              menu bar app (Swift), build script, icons
sender/           devicecast.py, server.py, screencap.swift, deskpad.py
receiver/         receiver.js, receiver.html
receiver/tizen/   Samsung Tizen receiver app and install script
data/             local state and log (not tracked)
tools.noindex/    Tizen Studio, DeskPad, compiled helpers (not tracked; ignored by Spotlight)
```
