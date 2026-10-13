#!/usr/bin/env python3
"""Live mirror of a Tektronix TBS1000-series oscilloscope connected over USB.

Rather than pulling screenshots (slow on these scopes), this reads the
waveform record of each enabled channel over VISA as fast as the scope
allows and redraws it as a scope-style display.

    python scope_mirror.py                 # GUI
    python scope_mirror.py --selftest 50   # no GUI: fetch 50 frames, report fps
"""
import argparse
import base64
import contextlib
import os
import subprocess
import sys
import threading
import time
import tkinter as tk

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TEK_VID = "0x0699"
X_DIVS, Y_DIVS = 10, 8
LEVELS_PER_DIV = 25          # 8-bit samples: 25 counts per vertical division
RECORD_POINTS = 2500


class ScopeError(Exception):
    pass


# --------------------------------------------------------------------------
# Transport layer: two interchangeable ways to talk VISA
# --------------------------------------------------------------------------

def _silence_pyvisa_del_noise():
    """A half-initialised pyvisa ResourceManager (broken 64-bit VISA) prints a traceback
    from __del__ when garbage collected; that is expected when we fall back to the bridge."""
    previous = sys.unraisablehook

    def hook(unraisable):
        if "pyvisa" not in repr(unraisable.object) and "ResourceManager" not in repr(unraisable.object):
            previous(unraisable)
    sys.unraisablehook = hook


class PyVisaLink:
    """Native pyvisa (needs a working 64-bit VISA)."""
    name = "pyvisa"

    def __init__(self, resource=None):
        import pyvisa
        candidates = [None] + [p for p in (
            r"C:\Windows\System32\tkVisa64.dll",
            r"C:\Program Files\IVI Foundation\VISA\Win64\TekVISA\Bin\tkVisa64.dll",
        ) if os.path.exists(p)]
        errors = []
        _silence_pyvisa_del_noise()
        for lib in candidates:
            try:
                rm = pyvisa.ResourceManager(lib) if lib else pyvisa.ResourceManager()
                found = list(rm.list_resources())
                res = resource or next((r for r in found if TEK_VID in r.lower()), None)
                if not res:
                    raise ScopeError("no Tektronix USB instrument found")
                inst = rm.open_resource(res)
                inst.timeout = 5000
                self.rm, self.inst, self.resource = rm, inst, res
                return
            except Exception as e:
                errors.append(f"{lib or 'default'}: {e}")
        raise ScopeError("pyvisa unusable: " + "; ".join(errors))

    def write(self, cmd):
        self.inst.write_raw(cmd.encode("ascii") + b"\n")

    def query_raw(self, cmd):
        self.write(cmd)
        return bytes(self.inst.read_raw())

    def read_raw(self):
        return bytes(self.inst.read_raw())

    def close(self):
        with contextlib.suppress(Exception):
            self.inst.close()
        with contextlib.suppress(Exception):
            self.rm.close()


class BridgeLink:
    """32-bit PowerShell process that owns the 32-bit VISA library."""
    name = "32-bit bridge"
    PS32 = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"

    def __init__(self, resource=None):
        if not os.path.exists(self.PS32):
            raise ScopeError("32-bit PowerShell not found")
        args = [self.PS32, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                os.path.join(HERE, "visa_bridge32.ps1")]
        if resource:
            args += ["-Resource", resource]
        self.p = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        junk = []
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise ScopeError("bridge exited: " + " ".join(junk)[:300])
            text = line.decode("ascii", "replace").strip()
            if text.startswith("READY|"):
                self.resource = text[6:]
                return
            if text.startswith("ERR|"):
                self.close()
                raise ScopeError("bridge: " + text[4:])
            junk.append(text)

    def _call(self, line):
        self.p.stdin.write(line.encode("ascii") + b"\n")
        self.p.stdin.flush()
        while True:
            resp = self.p.stdout.readline()
            if not resp:
                raise ScopeError("bridge closed unexpectedly")
            text = resp.decode("ascii", "replace").strip()
            if text.startswith("OK|"):
                return text[3:]
            if text.startswith("ERR|"):
                raise ScopeError(text[4:])

    def write(self, cmd):
        self._call("W|" + cmd)

    def query_raw(self, cmd):
        return base64.b64decode(self._call("Q|" + cmd))

    def read_raw(self):
        return base64.b64decode(self._call("R|"))

    def close(self):
        with contextlib.suppress(Exception):
            self.p.stdin.write(b"X|\n")
            self.p.stdin.flush()
        with contextlib.suppress(Exception):
            self.p.wait(timeout=2)
        with contextlib.suppress(Exception):
            self.p.kill()


def open_link(backend="auto", resource=None):
    if backend in ("auto", "pyvisa"):
        try:
            return PyVisaLink(resource)
        except Exception as e:
            if backend == "pyvisa":
                raise
            first_error = e
    try:
        return BridgeLink(resource)
    except Exception as e:
        raise ScopeError(f"{first_error if backend == 'auto' else ''} | {e}")


# --------------------------------------------------------------------------
# Scope protocol
# --------------------------------------------------------------------------

class Trace:
    def __init__(self, raw, xincr, pt_off, xzero, ymult, yoff, yzero):
        self.raw = raw
        self.xincr, self.pt_off, self.xzero = xincr, pt_off, xzero
        self.ymult, self.yoff, self.yzero = ymult, yoff, yzero

    @property
    def volts(self):
        return (self.raw.astype(np.float64) - self.yoff) * self.ymult + self.yzero

    @property
    def volts_per_div(self):
        return self.ymult * LEVELS_PER_DIV

    @property
    def secs_per_div(self):
        return self.xincr * len(self.raw) / X_DIVS


def parse_block(raw):
    i = raw.index(b"#")
    n = int(raw[i + 1:i + 2])
    length = int(raw[i + 2:i + 2 + n])
    start = i + 2 + n
    data = raw[start:start + length]
    if len(data) != length:
        raise ScopeError(f"short waveform block ({len(data)} of {length} bytes)")
    return np.frombuffer(data, dtype=np.int8)


class Scope:
    """Every query costs ~100 ms inside a TBS1000 plus ~45 us per sample returned, so
    the waveform fetch is kept to a single message per channel and the scaling
    (which only changes when someone turns a knob) is refreshed only occasionally."""
    SCALE_REFRESH_S = 3.0
    FULL_SCALE = ("XINcr", "PT_Off", "XZEro", "YMUlt", "YOFf", "YZEro")
    QUICK_SCALE = ("XINcr", "YMUlt", "YOFf")

    def __init__(self, link):
        self.link = link
        self.scales = {}

    def query(self, cmd):
        return self.link.query_raw(cmd).decode("ascii", "replace").strip()

    def setup(self):
        # Communication settings only; nothing that changes what the scope displays.
        self.link.write("HEADer OFF")
        self.link.write(f"DATa:ENCdg RIBinary;WIDth 1;STARt 1;STOP {RECORD_POINTS}")

    def read_enabled(self):
        """-> (list of enabled channels, display format 'YT'/'XY' or None)"""
        try:
            sel = self.query("SELect:CH1?;CH2?;:DISplay:FORMat?").split(";")
            fmt = sel[2].strip().upper() if len(sel) > 2 else None
            return [i + 1 for i, v in enumerate(sel[:2]) if v.strip() in ("1", "ON")], fmt
        except Exception:
            return [1, 2], None

    def read_trigger(self):
        try:
            level, src = self.query("TRIGger:MAIn:LEVel?;EDGe:SOUrce?").split(";")[:2]
            return {"trig_level": float(level), "trig_src": src.strip().upper()}
        except Exception:
            return {"trig_level": None, "trig_src": None}

    def fetch(self, ch):
        now = time.time()
        sc = self.scales.get(ch)
        if sc is None:
            items = self.FULL_SCALE
        elif now - sc["t"] > self.SCALE_REFRESH_S:
            items = self.QUICK_SCALE
        else:
            items = ()
        cmd = f"DATa:SOUrce CH{ch}"
        if items:
            cmd += ";:WFMPre:" + "?;".join(items) + "?"
        raw = self.link.query_raw(cmd + ";:CURVe?")
        parts = raw.split(b";", len(items))
        if len(parts) < len(items) + 1:
            raise ScopeError(f"unexpected reply: {raw[:80]!r}")
        if items:
            sc = {**(sc or {}), **{k: float(v) for k, v in zip(items, parts)}, "t": now}
            self.scales[ch] = sc
        return self._trace(ch, parse_block(parts[-1]))

    def _trace(self, ch, data):
        sc = self.scales[ch]
        return Trace(data, sc["XINcr"], sc["PT_Off"], sc["XZEro"], sc["YMUlt"], sc["YOFf"], sc["YZEro"])

    def fetch_many(self, chans):
        """Yield (channel, Trace) as each arrives. When no scaling refresh is due, all
        channels are requested in ONE message (one saved round trip) and the replies
        are read back one block at a time, so the first channel can be shown while
        the second is still being sent."""
        now = time.time()
        stale = any(ch not in self.scales or now - self.scales[ch]["t"] > self.SCALE_REFRESH_S
                    for ch in chans)
        if stale or len(chans) < 2:
            for ch in chans:
                yield ch, self.fetch(ch)
            return
        self.link.write(";:".join(f"DATa:SOUrce CH{ch};:CURVe?" for ch in chans))
        for ch in chans:
            yield ch, self._trace(ch, parse_block(self.link.read_raw()))

    def close(self):
        self.link.close()


# --------------------------------------------------------------------------
# Acquisition thread
# --------------------------------------------------------------------------

class Acquirer(threading.Thread):
    ENABLED_POLL_S = 2.0       # how often to check which channels are switched on
    TRIGGER_POLL_S = 4.0       # how often to re-read the trigger level

    def __init__(self, backend, resource):
        super().__init__(daemon=True)
        self.backend, self.resource = backend, resource
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.paused = False
        self.view_mode = "Auto"    # set by the GUI: Auto / YT / XY
        self.sync_xy = True        # hold the scope while reading X-Y (see _sync_cycle)
        self.frame = None         # (traces dict, state dict)
        self.seq = 0
        self.fps = 0.0
        self.status = "Connecting..."
        self.idn = ""
        self.link_name = ""

    def _connect(self):
        link = open_link(self.backend, self.resource)
        scope = Scope(link)
        try:
            self.idn = scope.query("*IDN?")
            scope.setup()
        except Exception:
            link.close()
            raise
        self.link_name = link.name
        self.status = "Connected"
        return scope

    def _publish(self, traces, state):
        with self.lock:
            self.frame = (dict(traces), dict(state))
            self.seq += 1

    def _cycle(self, scope, traces, state, due):
        """One pass over the enabled channels; the display updates after each one."""
        now = time.time()
        if now >= due["select"]:
            state["enabled"], state["format"] = scope.read_enabled()
            due["select"] = time.time() + self.ENABLED_POLL_S
            for ch in [c for c in traces if c not in state["enabled"]]:
                del traces[ch]
        if now >= due["trig"]:
            state.update(scope.read_trigger())
            due["trig"] = time.time() + self.TRIGGER_POLL_S
        if not state["enabled"]:
            self._publish(traces, state)
            time.sleep(0.2)
        if self.paused or self.stop_evt.is_set():
            return False
        if self._want_sync(state):
            self._sync_cycle(scope, traces, state)
            return True
        # Don't abandon the generator part-way: unread replies would desync the link.
        for ch, tr in scope.fetch_many(list(state["enabled"])):
            traces[ch] = tr
            self._publish(traces, state)
        return True

    def _want_sync(self, state):
        xy = self.view_mode == "XY" or (self.view_mode == "Auto" and state.get("format") == "XY")
        return self.sync_xy and xy and 1 in state["enabled"] and 2 in state["enabled"]

    def _sync_cycle(self, scope, traces, state):
        """X-Y needs CH1 and CH2 from the SAME acquisition. The scope free-runs, so records
        read one after another have unrelated relative phase and the figure jitters. Hold
        the acquisition (STOP), read both, then RUN again. If the user already stopped the
        scope, it is left stopped."""
        prev = scope.query("ACQuire:STATE?;:ACQuire:STATE STOP").upper()
        try:
            new = dict(scope.fetch_many([1, 2]))
        finally:
            if prev in ("1", "RUN", "ON"):
                scope.link.write("ACQuire:STATE RUN")
        traces.update(new)
        self._publish(traces, state)
        if prev in ("1", "RUN", "ON"):
            # let the scope capture a fresh record (a few sweeps) before the next hold
            sweep = 10 * new[1].secs_per_div
            self.stop_evt.wait(min(3.0, max(0.1, 1.5 * sweep)))

    def run(self):
        scope, fails = None, 0
        traces, state = {}, {"enabled": [1, 2], "trig_level": None, "trig_src": None}
        due, last_t = {"select": 0.0, "trig": 0.0}, None
        while not self.stop_evt.is_set():
            try:
                if scope is None:
                    scope = self._connect()
                    traces.clear()
                    fails = 0
                    due = {"select": 0.0, "trig": 0.0}
                if self.paused:
                    time.sleep(0.05)
                    last_t = None
                    continue
                if self._cycle(scope, traces, state, due):
                    now = time.time()
                    if last_t:
                        inst = 1.0 / max(now - last_t, 1e-6)
                        self.fps = inst if not self.fps else 0.8 * self.fps + 0.2 * inst
                    last_t = now
                fails = 0
            except Exception as e:
                fails += 1
                self.status = f"Error: {e}"
                time.sleep(0.5)
                if scope is not None and fails >= 3:
                    scope.close()
                    scope = None
        if scope is not None:
            scope.close()

    def get_frame(self):
        with self.lock:
            return self.seq, self.frame


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def fmt_si(v, unit):
    if v is None or not np.isfinite(v):
        return "---"
    if v == 0:
        return f"0 {unit}"
    a = abs(v)
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"),
                          (1e-6, "\u00b5"), (1e-9, "n")):
        if a >= scale * 0.9995:
            break
    else:
        scale, prefix = 1e-12, "p"
    return f"{v / scale:.4g} {prefix}{unit}"


def measure(tr):
    v = tr.volts
    vmax, vmin = float(v.max()), float(v.min())
    out = {"vpp": vmax - vmin, "vmax": vmax, "vmin": vmin,
           "vrms": float(np.sqrt(np.mean(v ** 2))), "freq": None}
    if int(tr.raw.max()) - int(tr.raw.min()) >= 6:                 # ignore pure noise / flat lines
        mid = (vmax + vmin) / 2
        edges = np.nonzero((v[:-1] < mid) & (v[1:] >= mid))[0]
        if len(edges) >= 2:
            period = (edges[-1] - edges[0]) / (len(edges) - 1) * tr.xincr
            if period > 0:
                out["freq"] = 1.0 / period
    return out


class App:
    BG, GRID, AXIS = "#000000", "#2e2e2e", "#707070"
    COLORS = {1: "#ffe100", 2: "#00e5ff"}
    PANEL_BG, PANEL_FG = "#15181c", "#d0d4d8"

    def __init__(self, root, acq):
        self.root, self.acq = root, acq
        self.last_seq = -1
        root.title("Oscilloscope Mirror")
        root.minsize(700, 420)
        root.configure(bg=self.PANEL_BG)

        panel = tk.Frame(root, bg=self.PANEL_BG, width=290)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)
        self.canvas = tk.Canvas(root, bg=self.BG, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        font = ("Consolas", 11)
        self.ch_labels = {}
        for ch in (1, 2):
            lab = tk.Label(panel, text="", font=font, justify="left", anchor="w",
                           bg=self.PANEL_BG, fg=self.COLORS[ch])
            lab.pack(fill="x", padx=12, pady=(14, 0))
            self.ch_labels[ch] = lab
        self.time_label = tk.Label(panel, text="", font=font, justify="left", anchor="w",
                                   bg=self.PANEL_BG, fg=self.PANEL_FG)
        self.time_label.pack(fill="x", padx=12, pady=(18, 0))
        self.trig_label = tk.Label(panel, text="", font=font, justify="left", anchor="w",
                                   bg=self.PANEL_BG, fg=self.PANEL_FG)
        self.trig_label.pack(fill="x", padx=12, pady=(10, 0))
        self.view_mode = "Auto"                       # Auto follows the scope; or force YT / XY
        self.sync_var = tk.BooleanVar(value=True)
        tk.Checkbutton(panel, text="Steady X-Y (briefly stops\nthe scope for each read)",
                       variable=self.sync_var, command=self.on_sync_toggle, justify="left",
                       bg=self.PANEL_BG, fg=self.PANEL_FG, selectcolor=self.BG,
                       activebackground=self.PANEL_BG, activeforeground=self.PANEL_FG,
                       anchor="w").pack(fill="x", padx=12, pady=(14, 0), side="bottom")
        self.view_btn = tk.Button(panel, text="", command=self.cycle_view)
        self.view_btn.pack(fill="x", padx=12, pady=(20, 0))
        self.cycle_view(advance=False)
        self.freeze_btn = tk.Button(panel, text="Freeze  (Space)", command=self.toggle_freeze)
        self.freeze_btn.pack(fill="x", padx=12, pady=(6, 0))
        tk.Button(panel, text="Fullscreen  (F11)", command=self.toggle_fullscreen).pack(
            fill="x", padx=12, pady=(6, 0))
        self.status_label = tk.Label(panel, text="", font=("Consolas", 9), justify="left",
                                     anchor="sw", wraplength=262, bg=self.PANEL_BG, fg="#8a9096")
        self.status_label.pack(side="bottom", fill="x", padx=12, pady=10)

        self.trace_items = {
            ch: self.canvas.create_line(0, 0, 0, 0, fill=c, width=2, state="hidden")
            for ch, c in self.COLORS.items()}
        self.gnd_items = {
            ch: self.canvas.create_text(0, 0, text=f"{ch}\u25b6", fill=c, anchor="w",
                                        font=("Consolas", 11, "bold"), state="hidden")
            for ch, c in self.COLORS.items()}
        self.xy_item = self.canvas.create_line(0, 0, 0, 0, fill="#40ff70", width=2, state="hidden")
        self.trig_item = self.canvas.create_text(0, 0, text="\u25c0T", anchor="e", fill="#ff8a00",
                                                 font=("Consolas", 11, "bold"), state="hidden")
        self.rect = (0, 0, 1, 1)
        self._xs_key, self._xs = None, None
        self.canvas.bind("<Configure>", self.on_resize)
        root.bind("<space>", lambda e: self.toggle_freeze())
        root.bind("<x>", lambda e: self.cycle_view())
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    # --- window plumbing ---------------------------------------------------
    def cycle_view(self, advance=True):
        if advance:
            order = ("Auto", "YT", "XY")
            self.view_mode = order[(order.index(self.view_mode) + 1) % 3]
            self.last_seq = -1
        self.acq.view_mode = self.view_mode
        label = {"Auto": "View: Auto (follows scope)", "YT": "View: Y-T", "XY": "View: X-Y"}
        self.view_btn.config(text=label[self.view_mode] + "  (X)")

    def on_sync_toggle(self):
        self.acq.sync_xy = self.sync_var.get()

    def toggle_freeze(self):
        self.acq.paused = not self.acq.paused
        self.freeze_btn.config(text="Resume  (Space)" if self.acq.paused else "Freeze  (Space)")

    def toggle_fullscreen(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def close(self):
        self.acq.stop_evt.set()
        self.acq.join(timeout=3)
        self.root.destroy()

    def on_resize(self, event):
        m = 30
        self.rect = (m + 14, m, max(event.width - m, m + 100), max(event.height - m, m + 100))
        self.draw_grid()
        self.last_seq = -1          # force redraw of traces

    def draw_grid(self):
        c = self.canvas
        c.delete("grid")
        x0, y0, x1, y1 = self.rect
        dx, dy = (x1 - x0) / X_DIVS, (y1 - y0) / Y_DIVS
        for i in range(X_DIVS + 1):
            c.create_line(x0 + i * dx, y0, x0 + i * dx, y1, fill=self.GRID, tags="grid")
        for j in range(Y_DIVS + 1):
            c.create_line(x0, y0 + j * dy, x1, y0 + j * dy, fill=self.GRID, tags="grid")
        xc, yc = (x0 + x1) / 2, (y0 + y1) / 2
        c.create_line(xc, y0, xc, y1, fill=self.AXIS, tags="grid")
        c.create_line(x0, yc, x1, yc, fill=self.AXIS, tags="grid")
        for i in range(X_DIVS * 5 + 1):                       # minor ticks on center axes
            x = x0 + i * dx / 5
            c.create_line(x, yc - 3, x, yc + 3, fill=self.AXIS, tags="grid")
        for j in range(Y_DIVS * 5 + 1):
            y = y0 + j * dy / 5
            c.create_line(xc - 3, y, xc + 3, y, fill=self.AXIS, tags="grid")
        c.create_rectangle(x0, y0, x1, y1, outline=self.AXIS, tags="grid")
        c.tag_lower("grid")

    # --- drawing -------------------------------------------------------------
    def y_of(self, raw):
        x0, y0, x1, y1 = self.rect
        return (y0 + y1) / 2 - raw * (y1 - y0) / (Y_DIVS * LEVELS_PER_DIV)

    def tick(self):
        seq, frame = self.acq.get_frame()
        if frame is not None and seq != self.last_seq:
            self.last_seq = seq
            self.draw(*frame)
        self.update_status()
        self.root.after(8, self.tick)

    def draw(self, traces, state):
        x0, y0, x1, y1 = self.rect
        xy = (self.view_mode == "XY" or (self.view_mode == "Auto" and state.get("format") == "XY")) \
            and 1 in traces and 2 in traces          # X-Y needs both channels
        self.draw_xy(traces, xy)
        for ch in (1, 2):
            tr = traces.get(ch)
            if xy and tr is not None:
                self.canvas.itemconfigure(self.trace_items[ch], state="hidden")
                self.canvas.itemconfigure(self.gnd_items[ch], state="hidden")
                self.show_readout(ch, tr)
                continue
            if tr is None:
                self.canvas.itemconfigure(self.trace_items[ch], state="hidden")
                self.canvas.itemconfigure(self.gnd_items[ch], state="hidden")
                self.ch_labels[ch].config(text=f"CH{ch}  off")
                continue
            n = len(tr.raw)
            if self._xs_key != (n, x0, x1):
                self._xs_key = (n, x0, x1)
                self._xs = x0 + np.arange(n) * ((x1 - x0) / (n - 1))
            pts = np.empty(2 * n)
            pts[0::2] = self._xs
            pts[1::2] = self.y_of(np.clip(tr.raw.astype(np.float32), -100, 100))
            self.canvas.coords(self.trace_items[ch], *pts.tolist())
            self.canvas.itemconfigure(self.trace_items[ch], state="normal")
            gy = self.y_of(np.clip(tr.yoff, -100, 100))
            self.canvas.coords(self.gnd_items[ch], x0 + 2, float(gy))
            self.canvas.itemconfigure(self.gnd_items[ch], state="normal")
            self.show_readout(ch, tr)

        ref = next(iter(traces.values()), None)
        if ref is not None:
            self.time_label.config(text="Mode  X-Y" if xy else f"Time  {fmt_si(ref.secs_per_div, 's')}/div")
        # trigger marker + text
        lvl, src = state.get("trig_level"), state.get("trig_src")
        trs = traces.get(int(src[2])) if src and src.startswith("CH") and src[2:3].isdigit() else None
        if lvl is not None and trs is not None and not xy:
            raw = (lvl - trs.yzero) / trs.ymult + trs.yoff
            self.canvas.coords(self.trig_item, x1 - 2, float(self.y_of(np.clip(raw, -100, 100))))
            self.canvas.itemconfigure(self.trig_item, state="normal")
        else:
            self.canvas.itemconfigure(self.trig_item, state="hidden")
        if lvl is not None:
            self.trig_label.config(text=f"Trig  {src}\n level {fmt_si(lvl, 'V')}")

    def show_readout(self, ch, tr):
        m = measure(tr)
        self.ch_labels[ch].config(text=(
            f"CH{ch}  {fmt_si(tr.volts_per_div, 'V')}/div\n"
            f" Vpp   {fmt_si(m['vpp'], 'V')}\n"
            f" Vmax  {fmt_si(m['vmax'], 'V')}\n"
            f" Vmin  {fmt_si(m['vmin'], 'V')}\n"
            f" Vrms  {fmt_si(m['vrms'], 'V')}\n"
            f" Freq  {fmt_si(m['freq'], 'Hz')}"))

    def draw_xy(self, traces, xy):
        """CH1 on the horizontal axis, CH2 on the vertical, as on the scope's X-Y screen."""
        if not xy:
            self.canvas.itemconfigure(self.xy_item, state="hidden")
            return
        x0, y0, x1, y1 = self.rect
        n = min(len(traces[1].raw), len(traces[2].raw))
        pts = np.empty(2 * n)
        pts[0::2] = (x0 + x1) / 2 + np.clip(traces[1].raw[:n].astype(np.float32), -125, 125) \
            * (x1 - x0) / (X_DIVS * LEVELS_PER_DIV)
        pts[1::2] = self.y_of(np.clip(traces[2].raw[:n].astype(np.float32), -100, 100))
        self.canvas.coords(self.xy_item, *pts.tolist())
        self.canvas.itemconfigure(self.xy_item, state="normal")

    def update_status(self):
        a = self.acq
        if a.paused:
            line = "FROZEN"
        elif a.status.startswith("Error"):
            line = a.status
        else:
            line = f"{a.fps:4.1f} full refreshes/s via {a.link_name}"
        idn = a.idn.split(",")
        who = " ".join(idn[:2]) if len(idn) >= 2 else a.idn
        self.status_label.config(text=f"{who}\n{line}")


# --------------------------------------------------------------------------

def selftest(acq, n):
    acq.start()
    t0 = time.time()
    while acq.seq < 1 and time.time() - t0 < 30:
        time.sleep(0.1)
    if acq.frame is None:
        print("FAILED:", acq.status)
        return 1
    print("IDN :", acq.idn, "| link:", acq.link_name)
    start_seq, start = acq.seq, time.time()
    while acq.seq - start_seq < n and time.time() - start < 60:
        time.sleep(0.01)
    dt = time.time() - start
    traces, state = acq.frame
    print(f"{acq.seq - start_seq} display updates in {dt:.2f}s = {(acq.seq - start_seq) / dt:.1f}/s "
          f"({acq.fps:.1f} full refreshes of all channels per second)")
    print("state:", state)
    for ch, tr in traces.items():
        m = measure(tr)
        print(f"CH{ch}: {len(tr.raw)} pts, {fmt_si(tr.volts_per_div, 'V')}/div, "
              f"{fmt_si(tr.secs_per_div, 's')}/div, Vpp={fmt_si(m['vpp'], 'V')}, "
              f"freq={fmt_si(m['freq'], 'Hz')}")
    acq.stop_evt.set()
    acq.join(timeout=3)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=("auto", "pyvisa", "bridge"), default="auto",
                    help="auto = native pyvisa, falling back to the 32-bit bridge")
    ap.add_argument("--resource", help="VISA resource string (default: first Tektronix USB device)")
    ap.add_argument("--selftest", type=int, metavar="N", help="no GUI; fetch N frames and report")
    args = ap.parse_args()

    acq = Acquirer(args.backend, args.resource)
    if args.selftest:
        sys.exit(selftest(acq, args.selftest))
    acq.start()
    with contextlib.suppress(Exception):          # crisp text on scaled displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    w, h = int(root.winfo_screenwidth() * 0.8), int(root.winfo_screenheight() * 0.8)
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    App(root, acq)
    root.mainloop()


if __name__ == "__main__":
    main()
