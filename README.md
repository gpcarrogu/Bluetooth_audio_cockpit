# LDAC Monitor

Pick the LDAC quality of your Bluetooth headphones and **see the real bitrate** on Linux (PipeWire).

PipeWire lets you choose LDAC quality (Auto / High / Standard / Mobile), but gives no feedback about
what is actually being sent over the air. LDAC Monitor shows the measured bitrate second by second,
so you can confirm that the setting is applied and spot dropouts.

Developed with a Soundcore Liberty 3 Pro (LDAC, 48 kHz) on Fedora Linux. Other LDAC headphones
should work the same way; this has not been tested.

## Features

- Select LDAC quality at runtime: **Auto (ABR)**, **High (990)**, **Standard (660)**, **Mobile (330)** kbit/s
- Live graph of the measured bitrate, with current / 10 s average / 60 s min-max and the closest LDAC profile
- Shows the negotiated LDAC sample rate and the PCM format PipeWire feeds to the encoder
- Alert threshold: highlights every second where the bitrate falls below a value you choose, and counts them
- CSV recording of every measurement (for comparing headphones, distances, settings)
- "Force LDAC codec" button (switches the Bluetooth card profile to LDAC if the headphones offer it)
- "Make permanent" writes a WirePlumber rule so the quality survives reboots
- Two front ends sharing one backend: a native **GTK4** window and a local **web page**
- `--demo` mode with simulated data, `--diag` mode for bug reports

<img width="717" height="793" alt="immagine" src="https://github.com/user-attachments/assets/c7d01971-34fd-456f-aefd-34a32856117a" />


## Requirements

**Tested on:** Fedora Linux (GNOME, PipeWire + WirePlumber, BlueZ), Python 3.13 and 3.15.
Run `python3 ldac_monitor.py --diag` to print your distribution, kernel and component versions.

| Requirement | Notes |
|---|---|
| Linux kernel | No specific version needed. The app uses the kernel's Bluetooth HCI *monitor* channel (the same one `btmon` reads), which has been in mainline kernels for over a decade. |
| Distribution | Written for **Fedora**. Fedora's `pipewire-libs` ships the LDAC plugin (`libspa-codec-bluez5-ldac.so`) in the Fedora 40 to 44 package listings. Other distributions should work if they provide the same components (below), but are untested. |
| PipeWire + WirePlumber | PipeWire must be built with LDAC support. The "Make permanent" file uses the WirePlumber 0.5 `wireplumber.conf.d` format; WirePlumber 0.4 (Lua configuration) will ignore it. |
| BlueZ | Bluetooth stack, plus a Bluetooth adapter. |
| polkit (`pkexec`) | Used to start the measurement as root (see *How it works*). |
| Python 3 | 3.9 or newer should work (standard library only). |
| GTK front end only | GTK 4.10 or newer, PyGObject, pycairo. |

### Packages (Fedora)

Most of these are already installed on Fedora Workstation:

```bash
sudo dnf install pipewire pipewire-utils pipewire-pulseaudio wireplumber \
                 pulseaudio-utils bluez polkit python3 \
                 python3-gobject gtk4 python3-cairo
```

Commands the app calls: `pw-dump`, `pw-cli` (`pipewire-utils`), `pactl` (`pulseaudio-utils`),
`busctl` (`systemd`), `pkexec` (`polkit`).

## Usage

Put `ldac_monitor.py` and `ldac_monitor_gtk.py` in the same folder, then:

```bash
python3 ldac_monitor_gtk.py        # GTK window
python3 ldac_monitor.py            # web page (opens your browser at 127.0.0.1)
python3 ldac_monitor_gtk.py --demo # simulated data, no headphones needed
```

1. Connect your headphones and start playing audio.
2. Click **Start measuring**. A password prompt appears (polkit): the sampler needs root to read HCI traffic.
3. Choose a quality. The graph should settle near the nominal value within a few seconds.

LDAC nominal bitrates:

| Quality | 48 / 96 kHz | 44.1 / 88.2 kHz |
|---|---|---|
| High | 990 kbit/s | 909 kbit/s |
| Standard | 660 kbit/s | 606 kbit/s |
| Mobile | 330 kbit/s | 303 kbit/s |

In **Auto** mode the encoder adapts to link quality, so the bitrate moves up and down by design.

### Alert and CSV

- **Alert below**: set a value in kbit/s (for example 900 with High quality; 0 turns the alert off).
  A dashed red line appears on the chart, seconds below it get a red dot, and the counter increments.
  Silence or paused playback (almost no traffic) is not counted as a drop.
- **Record CSV** writes `~/ldac-monitor-YYYYMMDD-HHMMSS.csv` with one row per second:
  `time, net_kbps, raw_kbps, pkts, handle, quality, codec, below_threshold`.

## How it works

| Information | Source | Privileges |
|---|---|---|
| Headphones, codec, current quality, PCM format | `pw-dump` | user |
| Set LDAC quality | `pw-cli set-param <node> Props '{quality=N}'` (-1 auto, 0 hq, 1 sq, 2 mq) | user |
| LDAC sample rate and channel mode | BlueZ `MediaTransport1.Configuration` via `busctl` | user |
| Force LDAC codec | `pactl set-card-profile` | user |
| **Bitrate** | HCI monitor socket: sums the length of ACL packets sent to the headphones | **root** (`CAP_NET_RAW`) |

The sampler is the same file started as `ldac_monitor.py --sampler` through `pkexec`. It prints one
JSON line per second and exits when the app closes its standard input. It only counts packet
lengths and connection events: audio payloads are not stored or forwarded.

The reported **net bitrate** is the ACL byte count minus 17 bytes of L2CAP/RTP/LDAC header per
packet, which approximates the codec bitrate. Expect a few percent of jitter around the nominal
value from one-second windows. If several Bluetooth devices stream at once, the busiest connection
is reported.

The web front end listens on `127.0.0.1` only and requires a random token (printed in the URL) on
every request, with a Host check against DNS-rebinding.

## Troubleshooting

**Run `python3 ldac_monitor.py --diag` first** and include its output in any bug report.

- **"No Bluetooth headphones active as audio output"**: connect the headphones and make sure they are
  the PipeWire output device. The node only has a PCM format while audio is playing.
- **Codec is not LDAC**: enable LDAC in the headphones' own app (for Soundcore: the LDAC setting),
  reconnect, then try **Force LDAC codec**. Check that `libspa-codec-bluez5-ldac.so` is installed.
- **Password prompt cancelled / sampler error**: the message appears in red under the chart.
  Without `pkexec`, start the app with `sudo`.
- **Measurement keeps running after closing**: stop it with
  `sudo pkill -f "ldac_monitor.py --sampler"` (should not happen in the current version).
- **GTK front end refuses to start with "outdated backend"**: the two files must come from the same
  release, and the backend must be named exactly `ldac_monitor.py`.
- **Dropouts at High quality**: 990 kbit/s needs a clean 2.4 GHz link. Try Standard or Auto, use
  5 GHz Wi-Fi, keep a clear line of sight, and keep USB receivers and USB 3.0 devices away from the
  Bluetooth antenna. A Bluetooth mouse shares the same radio; use the alert threshold and CSV
  recording to check whether it causes drops.
- **Frequency / PCM format show "–"**: send the `--diag` output.

## Limitations

- Measures what the host sends, not what the headphones decode.
- The quality change uses a PipeWire `Props` parameter; if your PipeWire version does not expose the
  LDAC `quality` property, the button reports an error.
- "Make permanent" needs a WirePlumber restart (`systemctl --user restart wireplumber`) and a
  reconnection of the headphones.
- The web page does not include the alert, CSV, sample-rate and PCM-format features; they are in the GTK window.

## Files

- `ldac_monitor.py`: backend, sampler, local web server and web page
- `ldac_monitor_gtk.py`: GTK4 front end (imports the backend)
