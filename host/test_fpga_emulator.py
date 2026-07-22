#!/usr/bin/env python3
"""
test_fpga_emulator.py — fpga_emulator.py's own standalone CLI.

    python3 test_fpga_emulator.py

fpga_emulator.py has been used as an IMPORTED class throughout this
project's test suite since day one, but its own standalone entry point
(the thing a person actually runs in a terminal: `python3
fpga_emulator.py --symbol ... --port-symlink ...`) never had dedicated
coverage of its own. v3.29 made that entry point a first-class,
hardware-free way to run this whole project (see the README's "Running
without an FPGA board" section), so it earns the same rigor everything
else here gets: real subprocesses, real serial traffic, no shortcuts.

Covers:
  G1  every CLI flag needed for full bitstream-parameter parity with
      order_manager.py exists (--ema-kf/-ks, --vwap-warmup, --vwap-k2-q8)
  G2  the constructor threads vwap_warmup/vwap_k2_q8 through correctly
      (not just accepted and silently ignored)
  G3  --port-symlink: a real subprocess creates a working symlink, real
      serial traffic flows through it (pyserial opening the symlink
      path, not the raw pty), and the symlink is removed on shutdown —
      via SIGINT AND via SIGTERM (`kill` without -9; the common way a
      terminal or process manager would ask it to stop, which does NOT
      raise KeyboardInterrupt on its own — this is the exact gap found
      and fixed while building this)
  G4  a stale symlink (or a plain file) already sitting at the target
      path is replaced cleanly, not an error
  G5  --port-symlink '' disables the mechanism entirely (no file created,
      nothing to clean up)
"""

from __future__ import annotations
import os, signal, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpga_emulator import FPGAEmulator
from tick_protocol import VWAPMirror

PASS = FAIL = 0


def check(name, got, exp):
    global PASS, FAIL
    if got == exp:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, expected {exp!r}")


EMULATOR_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fpga_emulator.py")

# ---------------------------------------------------------------------------
print("[G1] --help exposes every bitstream-matching flag order_manager.py "
     "needs to align")
r = subprocess.run([sys.executable, EMULATOR_PY, "--help"],
                   capture_output=True, text=True, timeout=15)
for flag in ("--ema-kf", "--ema-ks", "--vwap-warmup", "--vwap-k2-q8",
            "--port-symlink"):
    check(f"{flag} is a real CLI flag", flag in r.stdout, True)

# ---------------------------------------------------------------------------
print("[G2] vwap_warmup/vwap_k2_q8 are actually threaded through, not "
     "just accepted")
emu_custom = FPGAEmulator(symbol="SPY", vwap_warmup=5, vwap_k2_q8=64)
m = emu_custom.models["vwap_bounce"]["SPY"]
check("custom vwap_warmup reached the actual VWAPMirror", m.warmup_n, 5)
check("custom vwap_k2_q8 reached the actual VWAPMirror", m.k2_q8, 64)
check("defaults still match the RTL's defaults when not overridden",
      (FPGAEmulator(symbol="SPY").models["vwap_bounce"]["SPY"].warmup_n,
       FPGAEmulator(symbol="SPY").models["vwap_bounce"]["SPY"].k2_q8),
      (20, 256))

# ---------------------------------------------------------------------------
print("[G3] --port-symlink: real subprocess, real serial traffic, clean "
     "shutdown on BOTH SIGINT and SIGTERM")

def start_emulator(symlink_path, extra_args=()):
    proc = subprocess.Popen(
        [sys.executable, EMULATOR_PY, "--symbol", "SPY",
         "--port-symlink", symlink_path, *extra_args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        # a pre-existing stale link at this same path also satisfies
        # os.path.islink() trivially -- wait for it to resolve to a
        # REAL, currently-existing pty target, proving the emulator
        # actually created (or replaced) it, not just that something
        # was already sitting there
        if (os.path.islink(symlink_path)
                and os.path.exists(os.path.realpath(symlink_path))):
            break
        time.sleep(0.05)
    return proc

tmp = tempfile.mkdtemp()

for sig_name, sig in (("SIGINT", signal.SIGINT), ("SIGTERM", signal.SIGTERM)):
    link = os.path.join(tmp, f"emu_{sig_name}")
    proc = start_emulator(link)
    check(f"[{sig_name}] symlink created", os.path.islink(link), True)

    real_target = os.path.realpath(link) if os.path.islink(link) else None
    check(f"[{sig_name}] symlink points at a real pty",
          bool(real_target and real_target.startswith("/dev/pts/")), True)

    if real_target:
        import serial
        from tick_protocol import pack_symcfg
        ser = serial.Serial(link, 921_600, timeout=2.0)
        ser.write(pack_symcfg(0, "SPY", enable=True))
        resp = ser.read(32)
        ser.close()
        check(f"[{sig_name}] real serial traffic flows through the "
             f"symlink (0x90 symcfg ack, not the raw pty path)",
             len(resp) >= 2 and resp[0] == 0xBB and resp[1] == 0x90, True)

    proc.send_signal(sig)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    check(f"[{sig_name}] symlink removed on shutdown "
         f"(this exact gap was found and fixed for SIGTERM while "
         f"building this feature — a bare `kill` doesn't raise "
         f"KeyboardInterrupt on its own)",
         os.path.islink(link) or os.path.exists(link), False)

# ---------------------------------------------------------------------------
print("[G4] a stale symlink (or a plain file) at the target path is "
     "replaced cleanly, not an error")

link2 = os.path.join(tmp, "emu_stale")
os.symlink("/nonexistent/stale/target", link2)      # a dangling stale link
proc = start_emulator(link2)
check("stale dangling symlink replaced with a working one",
      os.path.islink(link2)
      and os.path.realpath(link2).startswith("/dev/pts/"), True)
proc.send_signal(signal.SIGTERM)
proc.wait(timeout=5)

link3 = os.path.join(tmp, "emu_plainfile")
with open(link3, "w") as f:
    f.write("not a symlink, just a file that happens to be here")
proc = start_emulator(link3)
check("a plain file already at the target path is replaced, not an error "
     "(process didn't crash / refuse to start)",
     os.path.islink(link3), True)
proc.send_signal(signal.SIGTERM)
proc.wait(timeout=5)

# ---------------------------------------------------------------------------
print("[G5] --port-symlink '' disables the mechanism entirely")

proc = subprocess.Popen(
    [sys.executable, EMULATOR_PY, "--symbol", "SPY", "--port-symlink", ""],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(1)
proc.send_signal(signal.SIGTERM)
try:
    out, _ = proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
    out, _ = proc.communicate()
check("no 'stable path' banner line when --port-symlink is disabled",
      "stable path" in out, False)

print(f"\n==============================================")
print(f"  RESULT: {PASS} PASS / {FAIL} FAIL")
print(f"==============================================")
sys.exit(1 if FAIL else 0)
