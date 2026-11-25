#!/usr/bin/env python3
"""
LDAC Monitor - native GTK4 interface.

Requires (installed by default on Fedora Workstation): python3-gobject, gtk4, python3-cairo.
Put this file in the same folder as ldac_monitor.py (the backend).

    python3 ldac_monitor_gtk.py          # real headphones
    python3 ldac_monitor_gtk.py --demo   # simulated data
"""
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib  # noqa: E402

import ldac_monitor as core  # noqa: E402

if getattr(core, "BACKEND_VERSION", 1) < 4:
    sys.exit(f"ERROR: outdated backend ({core.__file__}).\n"
             "Replace ldac_monitor.py with the new version (same folder as this file,\n"
             "the name must be exactly ldac_monitor.py, without '(1)'), then stop the old\n"
             "sampler with:  sudo pkill -f 'ldac_monitor.py --sampler'")

try:  # GLib >= 2.79 moves Unix signal handling to GLibUnix
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402

    def _on_sigint_add(cb):
        return GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cb)
except (ValueError, ImportError):
    def _on_sigint_add(cb):
        return GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cb)

QUALITIES = [("auto", "Auto (ABR)"), ("hq", "High · 990"),
             ("sq", "Standard · 660"), ("mq", "Mobile · 330")]


def nearest_profile(kbps, nominal):
    if kbps < 40:
        return "–"
    k, label = min(nominal, key=lambda n: abs(n[0] - kbps))
    return f"{label.upper()} ~{k}"


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app, backend):
        super().__init__(application=app, title="LDAC Monitor")
        self.set_default_size(720, 800)
        self.backend = backend
        self.state = None
        self.device = None
        self.selected = None
        self._busy = False
        self._updating = False
        self._shown_q = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(16)
        self.set_child(root)
        self.connect("close-request", self.on_close)

        # --- device ---
        self.dev_label = Gtk.Label(label="Looking for headphones…", xalign=0)
        self.dev_label.add_css_class("title-4")
        root.append(self.dev_label)

        # --- quality ---
        qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cap = Gtk.Label(label="LDAC quality", xalign=0)
        cap.add_css_class("dim-label")
        qbox.append(cap)
        row = Gtk.Box(spacing=6)
        row.add_css_class("linked")
        self.qbtn = {}
        first = None
        for key, text in QUALITIES:
            b = Gtk.ToggleButton(label=text)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self.on_quality, key)
            self.qbtn[key] = b
            row.append(b)
        qbox.append(row)

        row2 = Gtk.Box(spacing=8)
        self.force_btn = Gtk.Button(label="Force LDAC codec")
        self.force_btn.connect("clicked", self.on_force)
        self.persist_btn = Gtk.Button(label="Make permanent")
        self.persist_btn.connect("clicked", self.on_persist)
        row2.append(self.force_btn)
        row2.append(self.persist_btn)
        qbox.append(row2)
        self.msg = Gtk.Label(label="", xalign=0, wrap=True)
        self.msg.add_css_class("dim-label")
        qbox.append(self.msg)
        root.append(qbox)

        # --- statistics ---
        grid = Gtk.Grid(column_spacing=24, row_spacing=2, column_homogeneous=True)
        self.stat = {}
        for i, (key, title) in enumerate([("now", "Now (kbit/s)"), ("avg", "10 s average"),
                                          ("mm", "Min / Max 60 s"), ("near", "Closest profile")]):
            t = Gtk.Label(label=title, xalign=0)
            t.add_css_class("dim-label")
            v = Gtk.Label(label="–", xalign=0)
            v.add_css_class("title-1" if i < 2 else "title-3")
            grid.attach(t, i, 0, 1, 1)
            grid.attach(v, i, 1, 1, 1)
            self.stat[key] = v
        root.append(grid)

        grid2 = Gtk.Grid(column_spacing=24, row_spacing=2, column_homogeneous=True)
        for i, (key, title) in enumerate([("rate", "LDAC sample rate"), ("pcm", "PCM format (input)"),
                                          ("drops", "Seconds below threshold")]):
            t = Gtk.Label(label=title, xalign=0)
            t.add_css_class("dim-label")
            v = Gtk.Label(label="–", xalign=0)
            v.add_css_class("title-4")
            grid2.attach(t, i, 0, 1, 1)
            grid2.attach(v, i, 1, 1, 1)
            self.stat[key] = v
        root.append(grid2)

        # --- alert and recording ---
        ctl = Gtk.Box(spacing=8)
        ctl.append(Gtk.Label(label="Alert below"))
        self.spin = Gtk.SpinButton.new_with_range(0, 1100, 10)
        self.spin.set_tooltip_text("0 = alert off. E.g. 900 with High quality")
        self.spin.connect("value-changed", lambda s: self.backend.set_threshold(int(s.get_value())))
        ctl.append(self.spin)
        ctl.append(Gtk.Label(label="kbit/s"))
        reset = Gtk.Button(label="Reset drops")
        reset.connect("clicked", lambda _b: self.backend.reset_drops())
        ctl.append(reset)
        spacer = Gtk.Box(hexpand=True)
        ctl.append(spacer)
        self.csv_btn = Gtk.ToggleButton(label="Record CSV")
        self.csv_btn.connect("toggled", self.on_csv)
        ctl.append(self.csv_btn)
        root.append(ctl)

        # --- chart ---
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_content_height(240)
        self.area.set_draw_func(self.draw)
        root.append(self.area)

        # --- measurement ---
        bar = Gtk.Box(spacing=8)
        self.samp_btn = Gtk.Button(label="Start measuring")
        self.samp_btn.add_css_class("suggested-action")
        self.samp_btn.connect("clicked", self.on_sampler)
        bar.append(self.samp_btn)
        note = Gtk.Label(label="Net value (without L2CAP/RTP headers). LDAC: 990/909 · 660/606 · 330/303",
                         xalign=0, hexpand=True, wrap=True)
        note.add_css_class("dim-label")
        bar.append(note)
        root.append(bar)
        self.err = Gtk.Label(label="", xalign=0, wrap=True)
        self.err.add_css_class("error")
        root.append(self.err)

        self.poll()
        GLib.timeout_add_seconds(1, self.poll)

    def on_close(self, _win):
        self.get_application().quit()
        return False   # lascia che GTK chiuda la finestra

    # ---------- thread helper ----------
    def bg(self, fn, done):
        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                res = (False, str(e))
            GLib.idle_add(done, res)
        threading.Thread(target=work, daemon=True).start()

    def set_msg(self, text):
        self.msg.set_text(text)

    # ---------- polling ----------
    def poll(self):
        if not self._busy:
            self._busy = True

            def work():
                try:
                    st = self.backend.state()
                except Exception as e:  # noqa: BLE001
                    st = {"devices": [], "devices_error": str(e), "samples": [],
                          "sampler_running": False, "sampler_error": None, "nominal": core.NOMINAL}
                GLib.idle_add(self.render, st)
            threading.Thread(target=work, daemon=True).start()
        return True  # keeps the timer running

    def render(self, st):
        self._busy = False
        self.state = st
        devs = st["devices"]
        self.device = devs[0] if devs else None
        if self.device:
            self.dev_label.set_text(
                f"{self.device['name']} · codec {(self.device['codec'] or '?').upper()} · node {self.device['node_id']}")
        else:
            self.dev_label.set_text("Error: " + st["devices_error"] if st.get("devices_error")
                                    else "No Bluetooth headphones active as audio output")
        sm = st["samples"]
        if sm:
            self.stat["now"].set_text(str(round(sm[-1]["net_kbps"])))
            a = [s["net_kbps"] for s in sm[-10:]]
            avg = sum(a) / len(a)
            self.stat["avg"].set_text(str(round(avg)))
            m = [s["net_kbps"] for s in sm[-60:]]
            self.stat["mm"].set_text(f"{round(min(m))} / {round(max(m))}")
            self.stat["near"].set_text(nearest_profile(avg, st["nominal"]))
        stream = st.get("stream") or {}
        self.stat["rate"].set_text(f"{stream['rate'] / 1000:g} kHz" if stream.get("rate") else "–")
        pcm = stream.get("pcm")
        if pcm:
            txt = pcm["format"] + (f" · {pcm['rate'] / 1000:g} kHz" if pcm.get("rate") else "")
            self.stat["pcm"].set_text(txt + (" (expected)" if pcm.get("source") == "enum" else ""))
        else:
            self.stat["pcm"].set_text("–")
        q = self.device.get("quality") if self.device else None
        if q and q != self._shown_q and q in self.qbtn:   # align the buttons with the real quality
            self._shown_q = q
            self.selected = q
            self._updating = True
            self.qbtn[q].set_active(True)
            self._updating = False
        self.stat["drops"].set_text(str(st.get("drops", 0)) if st.get("threshold") else "alert off")
        if st.get("drops") and st.get("threshold"):
            self.stat["drops"].add_css_class("error")
        else:
            self.stat["drops"].remove_css_class("error")
        self.samp_btn.set_label("Stop measuring" if st["sampler_running"] else "Start measuring")
        self.err.set_text(st.get("sampler_error") or "")
        self.area.queue_draw()
        return False

    # ---------- actions ----------
    def on_quality(self, btn, key):
        if self._updating or not btn.get_active():
            return
        if not self.device:
            self.set_msg("No active headphones.")
            return
        self.selected = key
        node = self.device["node_id"]
        self.bg(lambda: self.backend.set_quality(node, key),
                lambda r: self.set_msg(
                    f"Quality set: {key.upper()}. Check the bitrate to confirm."
                    if r[0] else f"Error: {r[1]}"))

    def on_force(self, _b):
        addr = self.device["address"] if self.device else None
        self.bg(lambda: self.backend.force_ldac(addr), lambda r: self.set_msg(r[1]))

    def on_persist(self, _b):
        if not self.selected:
            self.set_msg("Select a quality first.")
            return
        q = self.selected

        def work():
            path = core.persist_quality(q) if not self.backend.demo else core.WP_CONF
            return True, (f"Written {path}. To apply it: systemctl --user restart wireplumber "
                          f"and reconnect the headphones.")
        self.bg(work, lambda r: self.set_msg(r[1]))

    def on_csv(self, btn):
        fn = self.backend.start_log if btn.get_active() else self.backend.stop_log
        on = btn.get_active()
        self.bg(lambda: fn(), lambda r: self.set_msg(
            f"Recording to {r[1]}" if on else f"Recording closed: {r[1]}"))

    def on_sampler(self, _b):
        running = self.state and self.state["sampler_running"]

        def work():
            (self.backend.stop_sampler if running else self.backend.start_sampler)()
            return True, ""
        self.bg(work, lambda _r: self.poll())

    # ---------- chart ----------
    def draw(self, _area, cr, w, h):
        fg = self.area.get_color()  # theme Gdk.RGBA (light/dark)
        left, top, bottom, ymax = 40, 8, 8, 1100.0

        def y(v):
            return h - bottom - v / ymax * (h - top - bottom)

        cr.set_font_size(11)
        cr.set_line_width(1)
        for g in (0, 330, 660, 990):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.move_to(left, y(g))
            cr.line_to(w - 6, y(g))
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(4, y(g) + 4)
            cr.show_text(str(g))
        data = (self.state or {}).get("samples", [])[-120:]
        if len(data) < 2:
            return
        step = (w - left - 6) / 119.0
        th = (self.state or {}).get("threshold") or 0
        if th:   # red dashed threshold line
            cr.set_source_rgba(0.86, 0.2, 0.2, 0.8)
            cr.set_line_width(1)
            cr.set_dash([6.0, 4.0])
            cr.move_to(left, y(th))
            cr.line_to(w - 6, y(th))
            cr.stroke()
            cr.set_dash([])
        cr.set_source_rgba(0.21, 0.52, 0.89, 1)  # GNOME blue
        cr.set_line_width(2)
        for i, s in enumerate(data):
            px, py = left + i * step, y(min(s["net_kbps"], ymax))
            cr.line_to(px, py) if i else cr.move_to(px, py)
        cr.stroke()
        for i, s in enumerate(data):   # red dots where the bitrate fell below the threshold
            if s.get("below"):
                cr.set_source_rgba(0.86, 0.2, 0.2, 1)
                cr.arc(left + i * step, y(min(s["net_kbps"], ymax)), 3.5, 0, 6.2832)
                cr.fill()


class LdacApp(Gtk.Application):
    def __init__(self, demo):
        super().__init__(application_id="io.github.ldacmonitor")
        self.backend = core.App(demo=demo)

    def do_activate(self):
        MainWindow(self, self.backend).present()
        # Ctrl+C in the terminal closes the app cleanly
        _on_sigint_add(self._on_sigint)

    def _on_sigint(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def _safe_shutdown(self):
        try:
            self.backend.shutdown()
        except Exception as e:  # noqa: BLE001
            print(f"sampler stop: {e}", file=sys.stderr)

    def do_shutdown(self):
        t = threading.Thread(target=self._safe_shutdown, daemon=True)
        t.start()
        t.join(3)   # never wait more than 3 s
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    try:
        rc = LdacApp(demo).run(sys.argv[:1])
    except KeyboardInterrupt:
        rc = 130
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)   # exit now: do not wait for stray threads or subprocess pipes
