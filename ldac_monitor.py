#!/usr/bin/env python3
"""
LDAC Monitor - choose the LDAC quality and measure the real bitrate (Fedora / PipeWire)

Usage:
    python3 ldac_monitor.py            # start the app and open the browser
    python3 ldac_monitor.py --demo     # fake data, to try the interface
    python3 ldac_monitor.py --port 9000

How it works
  * State/codec:  pw-dump           (no privileges needed)
  * LDAC quality: pw-cli set-param  (runtime) + WirePlumber file (persistent)
  * Codec LDAC:   pactl set-card-profile
  * Bitrate:      a small "sampler" reads the kernel's HCI monitor channel
                  (the same one btmon uses) and sums the ACL bytes sent to the
                  headphones. Needs CAP_NET_RAW: the app starts it with pkexec.
Python 3 standard library only.
"""
import argparse
import collections
import csv
import ctypes
import datetime
import json
import os
import random
import secrets
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
BACKEND_VERSION = 4   # the GTK GUI refuses older backends
QUALITY_VALUES = {"auto": -1, "hq": 0, "sq": 1, "mq": 2}   # PipeWire Props 'quality'
QUALITY_BY_VALUE = {v: k for k, v in QUALITY_VALUES.items()}
NOMINAL = [  # (kbps, label) 48/96 kHz and 44.1/88.2 kHz
    (330, "mq"), (303, "mq"), (660, "sq"), (606, "sq"), (990, "hq"), (909, "hq"),
]
HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_MONITOR = 2
MON_EVENT_PKT, MON_ACL_TX, MON_ACL_RX = 3, 4, 5
# per-PDU overhead of A2DP over L2CAP: L2CAP header (4) + RTP (12) + LDAC header (1)
A2DP_OVERHEAD = 17
WP_CONF = os.path.expanduser(
    "~/.config/wireplumber/wireplumber.conf.d/51-ldac-quality.conf")


# --------------------------------------------------------------------------
# HCI sampler (privileged part) - runs as `--sampler`
# --------------------------------------------------------------------------
class Aggregator:
    """Sums ACL TX bytes per connection handle. Pure logic, easy to test."""

    def __init__(self):
        self.addr = {}                    # handle -> "AA:BB:.."
        self.reset()

    def reset(self):
        self.acc = collections.defaultdict(lambda: {"raw": 0, "net": 0, "pkts": 0})

    def feed(self, pkt: bytes):
        if len(pkt) < 6:
            return
        opcode, _index, plen = struct.unpack_from("<HHH", pkt, 0)
        body = pkt[6:6 + plen]
        if opcode == MON_ACL_TX and len(body) >= 4:
            hf, dlen = struct.unpack_from("<HH", body, 0)
            handle, pb = hf & 0x0FFF, (hf >> 12) & 0x3
            dlen = min(dlen, len(body) - 4)
            a = self.acc[handle]
            a["raw"] += dlen
            # pb 0b00/0b10 = first fragment (carries the L2CAP/RTP headers)
            net = dlen - A2DP_OVERHEAD if (pb & 1) == 0 and dlen > A2DP_OVERHEAD else dlen
            if (pb & 1) == 0:
                a["pkts"] += 1
            a["net"] += max(net, 0)
        elif opcode == MON_EVENT_PKT and len(body) >= 13:
            ev = body[0]
            if ev == 0x03 and body[2] == 0:        # Connection Complete
                handle = struct.unpack_from("<H", body, 3)[0] & 0x0FFF
                self.addr[handle] = ":".join(f"{b:02X}" for b in reversed(body[5:11]))
            elif ev == 0x05 and len(body) >= 6:    # Disconnection Complete
                handle = struct.unpack_from("<H", body, 3)[0] & 0x0FFF
                self.addr.pop(handle, None)

    def flush(self, dt: float):
        out = {"t": time.time(), "dt": dt, "handles": {
            str(h): dict(v, addr=self.addr.get(h)) for h, v in self.acc.items()}}
        self.reset()
        return out


def open_hci_monitor():
    # Python's socket.bind() cannot select the HCI channel: call bind(2) through ctypes
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC,
                      socket.BTPROTO_HCI)
    addr = struct.pack("<HHH", socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(s.fileno(), ctypes.create_string_buffer(addr, len(addr)), len(addr)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"HCI monitor bind failed: {os.strerror(e)} "
                         f"(CAP_NET_RAW / root privileges required)")
    return s


def sampler_main(source=None, stop_fd=None):
    """Reads the HCI monitor. Stops when stop_fd closes (EOF) or receives data."""
    sock = source or open_hci_monitor()
    watch = [sock] + ([stop_fd] if stop_fd is not None else [])
    agg, last = Aggregator(), time.monotonic()
    while True:
        r, _, _ = select.select(watch, [], [], 0.25)
        if stop_fd is not None and stop_fd in r:
            os.read(stop_fd, 1024)   # EOF or "q": exit in both cases
            break
        if sock in r:
            data = sock.recv(65536)
            if not data:
                break
            agg.feed(data)
        now = time.monotonic()
        if now - last >= 1.0:
            print(json.dumps(agg.flush(now - last)), flush=True)
            last = now


# --------------------------------------------------------------------------
# System helpers (pw-dump, pw-cli, pactl)
# --------------------------------------------------------------------------
def run(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(cmd)}"


def parse_bt_sinks(dump_json):
    """Extracts the Bluetooth sinks from pw-dump output (list of objects)."""
    out = []
    for o in dump_json:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        if "api.bluez5.codec" not in props and not str(props.get("node.name", "")).startswith("bluez_output"):
            continue
        params = (o.get("params") or {}) or ((o.get("info") or {}).get("params") or {})
        pcm = None
        # 1) active format (only while the node is playing); 2) expected format (EnumFormat)
        for f in (params.get("Format") or []):
            if isinstance(f, dict) and isinstance(f.get("format"), str):
                pcm = {"format": f["format"], "rate": f.get("rate"), "channels": f.get("channels"),
                       "source": "format"}
                break
        if pcm is None:
            for f in (params.get("EnumFormat") or []):
                fmt = f.get("format") if isinstance(f, dict) else None
                fmt = fmt.get("default") if isinstance(fmt, dict) else fmt
                if isinstance(fmt, str):
                    pcm = {"format": fmt, "rate": f.get("rate"), "channels": f.get("channels"),
                           "source": "enum"}
                    break
        quality = None   # current LDAC quality: the {'quality': N} entry inside Props
        for p in (params.get("Props") or []):
            if isinstance(p, dict) and isinstance(p.get("quality"), int):
                quality = QUALITY_BY_VALUE.get(p["quality"])
        out.append({
            "quality": quality,
            "pcm": pcm,
            "node_id": o["id"],
            "name": props.get("node.description") or props.get("node.nick") or props.get("node.name"),
            "address": props.get("api.bluez5.address"),
            "codec": props.get("api.bluez5.codec"),
            "state": (o.get("info") or {}).get("state"),
        })
    return out


LDAC_RATES = {0x20: 44100, 0x10: 48000, 0x08: 88200, 0x04: 96000, 0x02: 176400, 0x01: 192000}
LDAC_MODES = {0x04: "mono", 0x02: "dual channel", 0x01: "stereo"}   # as in BlueZ a2dp-codecs.h


def parse_ldac_config(cfg):
    """LDAC A2DP configuration: vendor(4) + codec id(2) + sample rate(1) + channel mode(1)."""
    try:
        b = bytes(cfg)
    except (TypeError, ValueError):
        return None
    if len(b) != 8 or b[0:4] != b"\x2d\x01\x00\x00" or b[4:6] != b"\xaa\x00":
        return None
    return {"rate": LDAC_RATES.get(b[6]), "mode": LDAC_MODES.get(b[7])}


def _walk_transports(obj, out, path=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("/org/bluez/"):
                _walk_transports(v, out, k)
            elif k == "org.bluez.MediaTransport1" and isinstance(v, dict):
                out.append((path, v))
            else:
                _walk_transports(v, out, path)
    elif isinstance(obj, list):
        for v in obj:
            _walk_transports(v, out, path)


def ldac_config_from_objects(objs, address):
    """Finds the headphones' transport in GetManagedObjects (busctl JSON)."""
    found = []
    _walk_transports(objs, found)
    want = ("dev_" + (address or "").replace(":", "_")).upper()
    for path, props in found:
        if address and want not in (path or "").upper():
            continue
        cfg = props.get("Configuration")
        cfg = cfg.get("data") if isinstance(cfg, dict) else cfg
        parsed = parse_ldac_config(cfg) if cfg is not None else None
        if parsed:
            return parsed
    return None


def get_ldac_config(address):
    rc, out, _ = run(["busctl", "--system", "--json=short", "call", "org.bluez", "/",
                      "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"])
    if rc != 0:
        return None
    try:
        return ldac_config_from_objects(json.loads(out), address)
    except json.JSONDecodeError:
        return None


def get_bt_sinks():
    rc, out, err = run(["pw-dump"])
    if rc != 0:
        return [], err.strip() or "pw-dump not available"
    try:
        return parse_bt_sinks(json.loads(out)), None
    except json.JSONDecodeError as e:
        return [], f"invalid pw-dump output: {e}"


def find_ldac_profile(address):
    """Returns (card_name, profile_name) with 'ldac' in its name, for the given address."""
    rc, out, _ = run(["pactl", "-f", "json", "list", "cards"])
    if rc != 0:
        return None, None
    try:
        cards = json.loads(out)
    except json.JSONDecodeError:
        return None, None
    want = (address or "").replace(":", "_").upper()
    for c in cards:
        if not c.get("name", "").startswith("bluez_card"):
            continue
        if want and want not in c["name"].upper():
            continue
        for p in (c.get("profiles") or {}):
            if "ldac" in p:
                return c["name"], p
    return None, None


def set_quality_runtime(node_id, quality):
    v = QUALITY_VALUES[quality]
    rc, out, err = run(["pw-cli", "set-param", str(int(node_id)), "Props", f"{{quality={v}}}"])
    return rc == 0, (err or out).strip()


def persist_quality(quality):
    conf = f'''# Generated by ldac_monitor.py
monitor.bluez.rules = [
  {{
    matches = [ {{ device.name = "~bluez_card.*" }} ]
    actions = {{
      update-props = {{
        bluez5.a2dp.ldac.quality = "{quality}"
      }}
    }}
  }}
]
'''
    os.makedirs(os.path.dirname(WP_CONF), exist_ok=True)
    with open(WP_CONF, "w") as f:
        f.write(conf)
    return WP_CONF


# --------------------------------------------------------------------------
# Application state: sampler management + demo
# --------------------------------------------------------------------------
class App:
    def __init__(self, demo=False):
        self.demo = demo
        self.samples = collections.deque(maxlen=600)
        self.proc = None
        self.sampler_error = None
        self.lock = threading.Lock()
        self.demo_quality = "auto"
        self.current_quality = None
        self.threshold = 0          # kbit/s; 0 = alert off
        self.drops = 0
        self.last_codec = ""
        self.log_file = self.log_writer = self.log_path = None
        self._cfg_cache = (0.0, None, None)
        self._demo_on = False
        self._demo_thread = None
        if demo:
            self.start_sampler()

    # ---- sampler ----
    def sampler_running(self):
        if self.demo:
            return self._demo_on
        return self.proc is not None and self.proc.poll() is None

    def start_sampler(self):
        self.sampler_error = None
        if self.demo:
            self._demo_on = True
            if not (self._demo_thread and self._demo_thread.is_alive()):
                self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True)
                self._demo_thread.start()
            return
        if self.sampler_running():
            return
        cmd = [sys.executable, os.path.abspath(__file__), "--sampler"]
        if os.geteuid() != 0:
            if not shutil.which("pkexec"):
                self.sampler_error = "pkexec not found: run the app with sudo"
                return
            cmd = ["pkexec"] + cmd
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def shutdown(self):
        self.stop_sampler()
        self.stop_log()

    def stop_sampler(self):
        if self.demo:
            self._demo_on = False
            return
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            # The sampler runs as root: a signal sent to pkexec does not reach it.
            # Close its stdin instead: it notices and exits by itself.
            try:
                proc.stdin.write("q\n")
                proc.stdin.close()
            except (OSError, ValueError):
                pass

            def reap():
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.terminate()
                    except OSError:    # root process: cannot be signalled, ignore
                        pass
            threading.Thread(target=reap, daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            if proc is not self.proc:      # stopped: discard any leftover lines
                break
            try:
                self._ingest(json.loads(line))
            except (json.JSONDecodeError, KeyError):
                continue
        rc = proc.wait()
        if proc is self.proc and rc not in (0, -15):
            err = (proc.stderr.read() or "").strip()
            self.sampler_error = (
                "authentication cancelled" if rc in (126, 127) and not err else err or f"sampler exited (code {rc})")

    def _ingest(self, msg):
        dt = msg.get("dt") or 1.0
        hs = msg.get("handles") or {}
        if hs:   # dominant handle = A2DP stream
            h, v = max(hs.items(), key=lambda kv: kv[1]["net"])
        else:
            h, v = None, {"net": 0, "raw": 0, "pkts": 0, "addr": None}
        net = round(v["net"] * 8 / dt / 1000, 1)
        s = {"t": msg["t"], "handle": h, "addr": v.get("addr"),
             "net_kbps": net,
             "raw_kbps": round(v["raw"] * 8 / dt / 1000, 1),
             "pkts": v["pkts"],
             # "drop" = an audio stream is flowing (>40 kbit/s) but below the chosen threshold
             "below": bool(self.threshold and 40 < net < self.threshold)}
        with self.lock:
            self.samples.append(s)
            if s["below"]:
                self.drops += 1
            if self.log_writer:
                self.log_writer.writerow([
                    datetime.datetime.fromtimestamp(s["t"]).isoformat(timespec="seconds"),
                    s["net_kbps"], s["raw_kbps"], s["pkts"], s["handle"] or "",
                    self.current_quality or "", self.last_codec, int(s["below"])])
                self.log_file.flush()

    # ---- demo ----
    def _demo_loop(self):
        t = 0
        while self.demo and self._demo_on:
            time.sleep(1)
            t += 1
            q = self.demo_quality
            base = {"hq": 990, "sq": 660, "mq": 330}.get(q)
            if base is None:  # auto: fluctuating ABR
                base = random.choice([660, 660, 909, 990, 990])
            kbps = base * random.uniform(0.97, 1.0)
            self._ingest({"t": time.time(), "dt": 1.0, "handles": {
                "66": {"net": kbps * 125, "raw": kbps * 125 * 1.025, "pkts": 150,
                       "addr": "AA:BB:CC:DD:EE:FF"}}})

    # ---- threshold and log ----
    def set_threshold(self, kbps):
        self.threshold = max(0, int(kbps))
        return True, "ok"

    def reset_drops(self):
        self.drops = 0

    def start_log(self):
        with self.lock:
            if self.log_file:
                return True, self.log_path
            self.log_path = os.path.expanduser(
                f"~/ldac-monitor-{datetime.datetime.now():%Y%m%d-%H%M%S}.csv")
            self.log_file = open(self.log_path, "w", newline="")
            self.log_writer = csv.writer(self.log_file)
            self.log_writer.writerow(["time", "net_kbps", "raw_kbps", "pkts", "handle",
                                      "quality", "codec", "below_threshold"])
            self.log_file.flush()
        return True, self.log_path

    def stop_log(self):
        with self.lock:
            path = self.log_path
            if self.log_file:
                self.log_file.close()
            self.log_file = self.log_writer = None
        return True, path

    def stream_info(self, dev):
        """LDAC sample rate (from BlueZ) and PCM format (from PipeWire). 5 s cache."""
        if self.demo:
            return {"rate": 96000, "mode": "stereo",
                    "pcm": {"format": "S32LE", "rate": 96000, "channels": 2}}
        if not dev:
            return {}
        ts, addr, cfg = self._cfg_cache
        if time.time() - ts > 5 or addr != dev.get("address"):
            cfg = get_ldac_config(dev.get("address")) if dev.get("codec") == "ldac" else None
            self._cfg_cache = (time.time(), dev.get("address"), cfg)
        return dict(cfg or {}, pcm=dev.get("pcm"))

    # ---- state for the UI ----
    def state(self):
        if self.demo:
            devs, err = [{"node_id": 66, "name": "Soundcore Liberty (demo)",
                          "address": "AA:BB:CC:DD:EE:FF", "codec": "ldac",
                          "state": "running"}], None
        else:
            devs, err = get_bt_sinks()
        with self.lock:
            samples = list(self.samples)[-300:]
        if devs:
            self.last_codec = devs[0].get("codec") or ""
            if devs[0].get("quality"):      # real value read from PipeWire
                self.current_quality = devs[0]["quality"]
        return {"devices": devs, "devices_error": err, "samples": samples,
                "threshold": self.threshold, "drops": self.drops,
                "log_path": self.log_path if self.log_file else None,
                "stream": self.stream_info(devs[0] if devs else None),
                "sampler_running": self.sampler_running(),
                "sampler_error": self.sampler_error, "nominal": NOMINAL,
                "demo": self.demo, "persist_file": WP_CONF}

    # ---- actions ----
    def set_quality(self, node_id, quality):
        if quality not in QUALITY_VALUES:
            return False, "invalid quality"
        if self.demo:
            self.demo_quality = quality
            self.current_quality = quality
            return True, "ok (demo)"
        ok, msg = set_quality_runtime(node_id, quality)
        if ok:
            self.current_quality = quality
        return ok, msg or "applied"

    def force_ldac(self, address):
        if self.demo:
            return True, "ok (demo)"
        card, prof = find_ldac_profile(address)
        if not card:
            return False, ("The headphones do not offer an LDAC profile. Enable LDAC in the Soundcore app, "
                           "then disconnect and reconnect. Also check that 'libspa-codec-bluez5-ldac' is installed.")
        rc, out, err = run(["pactl", "set-card-profile", card, prof])
        return rc == 0, (err or f"profile {prof} activated").strip()


# --------------------------------------------------------------------------
# Local HTTP server
# --------------------------------------------------------------------------
def make_handler(app, token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, header_token):
            # Anti DNS-rebinding and anti CSRF: local host + random token
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, '{"error":"host"}')
                return False
            if header_token != token:
                self._send(403, '{"error":"token"}')
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                if not self._guard(parse_qs(u.query).get("t", [""])[0]):
                    return
                self._send(200, PAGE.replace("__TOKEN__", token), "text/html")
            elif u.path == "/api/state":
                if self._guard(self.headers.get("X-Token")):
                    self._send(200, json.dumps(app.state()))
            else:
                self._send(404, "{}")

        def do_POST(self):
            if not self._guard(self.headers.get("X-Token")):
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, '{"error":"json"}')
            p = urlparse(self.path).path
            ok, msg = False, "unknown action"
            if p == "/api/quality":
                ok, msg = app.set_quality(body.get("node_id"), body.get("quality"))
            elif p == "/api/force-ldac":
                ok, msg = app.force_ldac(body.get("address"))
            elif p == "/api/persist":
                q = body.get("quality")
                if q in QUALITY_VALUES:
                    path = persist_quality(q) if not app.demo else WP_CONF
                    ok, msg = True, (f"Written {path}. To apply it: "
                                     f"systemctl --user restart wireplumber and reconnect the headphones.")
                else:
                    msg = "invalid quality"
            elif p == "/api/sampler":
                if body.get("action") == "start":
                    app.start_sampler()
                else:
                    app.stop_sampler()
                ok, msg = True, "ok"
            self._send(200, json.dumps({"ok": ok, "message": msg}))

    return H


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LDAC Monitor</title>
<style>
:root{--bg:#fff;--fg:#1c1e21;--mut:#6b7078;--card:#f4f5f7;--acc:#2a6df4;--ok:#1a9a5b;--bad:#d33}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8eaed;--mut:#9aa0a8;--card:#22252b;--acc:#6b9bff;--ok:#4cc88a;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:860px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);margin-bottom:16px}.card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.grow{flex:1}
.big{font-size:44px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
.lbl{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
button,select{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #8884;background:var(--bg);color:var(--fg);cursor:pointer}
button.pri{background:var(--acc);color:#fff;border-color:transparent}button:hover{filter:brightness(1.08)}
.seg button.on{background:var(--acc);color:#fff;border-color:transparent}.seg{display:flex;gap:6px;flex-wrap:wrap}
canvas{width:100%;height:220px;display:block}.msg{min-height:20px;color:var(--mut);font-size:13px;margin-top:8px}
.err{color:var(--bad)}.pill{display:inline-block;padding:2px 10px;border-radius:99px;background:var(--acc);color:#fff;font-size:12px}
small{color:var(--mut)}
</style>
<main>
<h1>LDAC Monitor <span id="demo" class="pill" hidden>DEMO</span></h1>
<div class="sub" id="dev">Looking for headphones…</div>

<div class="card">
  <div class="lbl">LDAC quality</div>
  <div class="row" style="margin-top:8px">
    <div class="seg" id="q">
      <button data-q="auto">Auto (ABR)</button><button data-q="hq">High 990</button>
      <button data-q="sq">Standard 660</button><button data-q="mq">Mobile 330</button>
    </div>
    <span class="grow"></span>
    <button id="force">Force LDAC codec</button>
    <button id="persist">Make permanent</button>
  </div>
  <div class="msg" id="msg"></div>
</div>

<div class="card">
  <div class="row" style="margin-bottom:10px">
    <div class="lbl grow">Measured bitrate (kbit/s)</div>
    <button id="samp" class="pri">Start measuring</button>
  </div>
  <div class="stats">
    <div><div class="lbl">Now</div><div class="big" id="now">–</div></div>
    <div><div class="lbl">10 s average</div><div class="big" id="avg">–</div></div>
    <div><div class="lbl">Min / Max 60 s</div><div class="big" id="mm" style="font-size:28px;padding-top:12px">–</div></div>
    <div><div class="lbl">Closest profile</div><div class="big" id="near" style="font-size:28px;padding-top:12px">–</div></div>
  </div>
  <canvas id="cv" width="800" height="220" style="margin-top:12px"></canvas>
  <small>Net value: ACL bytes sent to the headphones minus the L2CAP/RTP overhead (≈ codec bitrate).
  LDAC references: 990/909 · 660/606 · 330/303 kbit/s depending on the sample rate.</small>
  <div class="msg err" id="serr"></div>
</div>
</main>
<script>
const TOKEN="__TOKEN__";let S=null,cur=null;
const $=id=>document.getElementById(id);
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function nearest(v){if(v<40)return '–';let b=null,d=1e9;for(const [k,l] of S.nominal){if(Math.abs(k-v)<d){d=Math.abs(k-v);b=[k,l]}}return b[1].toUpperCase()+' ~'+b[0]}
function draw(){
  const c=$('cv'),x=c.getContext('2d'),W=c.width,H=c.height,dark=matchMedia('(prefers-color-scheme: dark)').matches;
  x.clearRect(0,0,W,H);const fg=dark?'#9aa0a8':'#6b7078',ac=dark?'#6b9bff':'#2a6df4';
  const P={l:44,r:8,t:8,b:8},mx=1100,Y=v=>H-P.b-(v/mx)*(H-P.t-P.b);
  x.font='11px system-ui';x.fillStyle=fg;x.strokeStyle=dark?'#ffffff1f':'#0000001a';x.lineWidth=1;
  for(const g of [0,330,660,990]){x.beginPath();x.moveTo(P.l,Y(g));x.lineTo(W-P.r,Y(g));x.stroke();x.fillText(g,6,Y(g)+4)}
  const d=S.samples.slice(-120);if(d.length<2)return;
  x.strokeStyle=ac;x.lineWidth=2;x.beginPath();
  d.forEach((s,i)=>{const px=P.l+i*(W-P.l-P.r)/119,py=Y(s.net_kbps);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
}
function render(){
  $('demo').hidden=!S.demo;
  const d=S.devices;cur=d[0]||null;
  $('dev').textContent=cur?`${cur.name} · codec: ${(cur.codec||'?').toUpperCase()} · node ${cur.node_id}`
    :(S.devices_error?('Error: '+S.devices_error):'No Bluetooth headphones active as audio output');
  const sm=S.samples,last=sm[sm.length-1];
  $('now').textContent=last?Math.round(last.net_kbps):'–';
  const a=sm.slice(-10);$('avg').textContent=a.length?Math.round(a.reduce((p,s)=>p+s.net_kbps,0)/a.length):'–';
  const m=sm.slice(-60).map(s=>s.net_kbps);$('mm').textContent=m.length?Math.round(Math.min(...m))+' / '+Math.round(Math.max(...m)):'–';
  $('near').textContent=a.length?nearest(a.reduce((p,s)=>p+s.net_kbps,0)/a.length):'–';
  $('samp').textContent=S.sampler_running?'Stop measuring':'Start measuring';
  $('serr').textContent=S.sampler_error||'';draw();
}
async function tick(){try{S=await api('/api/state');render()}catch(e){$('dev').textContent='App unreachable'}}
document.querySelectorAll('#q button').forEach(b=>b.onclick=async()=>{
  if(!cur){$('msg').textContent='No active headphones.';return}
  const r=await api('/api/quality',{node_id:cur.node_id,quality:b.dataset.q});
  document.querySelectorAll('#q button').forEach(x=>x.classList.toggle('on',x===b&&r.ok));
  $('msg').textContent=r.ok?'Quality set: '+b.textContent+'. Check the bitrate to confirm.':'Error: '+r.message;
});
$('force').onclick=async()=>{const r=await api('/api/force-ldac',{address:cur&&cur.address});$('msg').textContent=r.message};
$('persist').onclick=async()=>{const q=document.querySelector('#q button.on');
  if(!q){$('msg').textContent='Select a quality first.';return}
  $('msg').textContent=(await api('/api/persist',{quality:q.dataset.q})).message};
$('samp').onclick=async()=>{await api('/api/sampler',{action:S&&S.sampler_running?'stop':'start'});tick()};
tick();setInterval(tick,1000);
</script></html>"""


# --------------------------------------------------------------------------
def system_info():
    """Distro, kernel and versions of the components the app depends on (for bug reports)."""
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["distro"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    info["kernel"] = os.uname().release
    for name, cmd in (("pipewire", ["pipewire", "--version"]),
                      ("wireplumber", ["wireplumber", "--version"]),
                      ("bluez", ["bluetoothctl", "--version"])):
        rc, out, _ = run(cmd, 3)
        if rc == 0 and out.strip():
            info[name] = " | ".join(l.strip() for l in out.splitlines() if l.strip())
    return info


def main():
    ap = argparse.ArgumentParser(description="LDAC quality selector and bitrate monitor")
    ap.add_argument("--sampler", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--demo", action="store_true", help="simulated data")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--diag", action="store_true", help="print what the app sees (for debugging)")
    a = ap.parse_args()

    if a.diag:
        print("system:", json.dumps(system_info(), indent=2))
        devs, err = get_bt_sinks()
        print("bluetooth sinks:", json.dumps(devs, indent=2), "error:", err)
        for d in devs:
            print("LDAC config", d["address"], "->", get_ldac_config(d["address"]))
        rc, out, _ = run(["pw-dump"])
        try:
            for o in json.loads(out) if rc == 0 else []:
                if o.get("id") in [d["node_id"] for d in devs]:
                    print(f"\nnode {o['id']}: top-level keys = {list(o.keys())}")
                    print("  info =", list((o.get("info") or {}).keys()))
                    print("  params (top-level) =", list((o.get("params") or {}).keys()))
                    print("  info.params =", (o.get("info") or {}).get("params"))
                    p = (o.get("info") or {}).get("props") or {}
                    print("  props audio/rate/format =", {k: v for k, v in p.items()
                          if any(w in k.lower() for w in ("rate", "format", "audio", "quantum"))})
        except json.JSONDecodeError:
            pass
        return

    if a.sampler:
        try:
            sampler_main(stop_fd=0)
        except OSError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            pass
        return

    app, token = App(demo=a.demo), secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app, token, a.port))
    url = f"http://127.0.0.1:{a.port}/?t={token}"
    print(f"LDAC Monitor running at {url}\nPress Ctrl+C to quit", flush=True)
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
