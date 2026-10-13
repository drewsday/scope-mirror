# Oscilloscope Mirror

Live, scope-style mirror of a Tektronix TBS1000-series oscilloscope (tested on a TBS 1042)
connected by USB. Shows both channels with the graticule, ground and trigger markers, and
per-channel Vpp / Vmax / Vmin / Vrms / frequency readouts.

```
run_scope_mirror.bat            (or)   python scope_mirror.py
python scope_mirror.py --selftest 40   # no window: fetch 40 updates and report speed
```

**X-Y view:** CH1 is plotted on the horizontal axis against CH2 on the vertical. The default
`Auto` view follows the scope's own display format (`DISplay:FORMat`), so putting the scope in
X-Y switches the mirror within ~2 s; press **X** to force Y-T or X-Y. X-Y needs both channels on
(otherwise it shows Y-T).

The scope free-runs, so CH1 and CH2 read one after the other come from different acquisitions
with unrelated relative phase, and the figure would jitter. With **Steady X-Y** ticked (default),
each read does `ACQuire:STATE STOP`, reads both channels from the same acquisition, then
`ACQuire:STATE RUN` and waits for a fresh sweep. The scope's own screen therefore updates about
once a second while X-Y is showing. If you have already pressed Stop on the scope, it is left
stopped. Untick the box to read without touching the scope's run state (the figure will drift).

Keys: **X** cycle view, **Space** freeze/resume, **F11** fullscreen, **Esc** leave fullscreen.

Requires `pip install pyvisa numpy` and TekVISA (installed with OpenChoice Desktop).
Close OpenChoice Desktop's own live capture while this runs; both share one USB connection.

## How it works

It reads the 2500-point waveform record of each enabled channel (`CURVe?`) and draws it
itself, instead of pulling screenshots, which on these scopes is much slower.

Speed is limited by the scope's firmware, not the PC: each remote query costs ~100 ms and
each 2500-sample block ~90 ms more (the scope paces the transfer; VISA read size makes no
difference). Practical result: each channel refreshes about twice a second (about 4 times a
second with one channel switched off). Both channels are requested in one message, and the
scaling (V/div, position) is re-read only every ~3 s, so the numeric readouts lag a few seconds
behind knob changes; the trace itself does not.

## The 32-bit bridge

On this PC the 64-bit VISA stack returns `VI_ERROR_INV_OBJECT` for every client, while the 32-bit
one works (the TekVISA installer ran with "noreboot"; a restart may fix it). `scope_mirror.py`
first tries native pyvisa, and if that fails automatically uses `visa_bridge32.ps1`: a 32-bit
PowerShell process that holds the VISA session and relays commands. Force one with
`--backend pyvisa` or `--backend bridge`.
