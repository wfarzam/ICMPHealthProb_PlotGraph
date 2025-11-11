#!/usr/bin/env python3
# monitor_devices.py

import os
import sys
import platform
import subprocess
import time
import math
import re
import socket
from typing import Dict, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- Set matplotlib backend BEFORE importing pyplot (best for Windows/PyInstaller) ----
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

import paramiko
from paramiko.ssh_exception import (
    AuthenticationException, SSHException, NoValidConnectionsError, BadHostKeyException
)

# --------------------------------------------------------------------------------------
# Paths: make sure devices.txt is read from the same folder as the .py or compiled .exe
# --------------------------------------------------------------------------------------
if getattr(sys, "frozen", False):  # PyInstaller --onefile
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEVICES_FILE = os.path.join(BASE_DIR, "devices.txt")

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------
# SSH
USERNAME = "admin"
PASSWORDS = ["cisco", "Admin123"]
SSH_TIMEOUT = 3.0
HOSTNAME_REFRESH_SEC = 120  # refresh SSH hostname cache every N seconds (when UP)

# Visuals / Layout
RADIUS_UP = 1.3                # green circles
RADIUS_DOWN = 1.5              # red circles (slightly bigger for readability)
LABEL_FS = 10
STATUS_FS = 12
COLS = 7                       # devices per row

# Blinking
BLINK_PERIOD_SEC = 1.0         # red blink cadence
DIM_ALPHA = 0.25
FULL_ALPHA = 1.0

# Hostname cleanup
SUFFIXES = (".elements.local", ".intel.com", ".corp.nandps.com")

# File/DNS refresh
DEVICES_RELOAD_SEC = 10        # re-read devices.txt every N seconds
DNS_REFRESH_SEC = 300          # re-check DNS every 5 minutes

# Caches
#   SSH hostname cache: ip -> (hostname, ts)
_hostname_cache: Dict[str, Tuple[str, float]] = {}
#   DNS caches
_dns_forward_cache: Dict[str, Tuple[str, str, float]] = {}  # entry -> (ip, cname, ts)
_dns_reverse_cache: Dict[str, Tuple[str, float]] = {}       # ip -> (name, ts)

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def clean_hostname(hn: str) -> str:
    if not hn:
        return "unknown"
    h = hn.strip()
    hl = h.lower()
    for sfx in SUFFIXES:
        if hl.endswith(sfx):
            h = h[:-(len(sfx))]
            break
    return h if h else "unknown"

def wrap_text(s: str, width: int = 16) -> str:
    """Soft wrap hostname for badge: prefer breaks at '-' or '.'; otherwise hard-wrap."""
    if len(s) <= width:
        return s
    parts = []
    buf = s
    while len(buf) > width:
        window = buf[:width]
        cut = max(window.rfind('-'), window.rfind('.'))
        if cut >= 8:  # avoid tiny first line
            parts.append(buf[:cut])
            buf = buf[cut+1:]
        else:
            parts.append(window)
            buf = buf[width:]
    parts.append(buf)
    return "\n".join(parts)

def is_ip(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return False

# --------------------------------------------------------------------------------------
# Subprocess (Windows-safe, no flashing consoles)
# --------------------------------------------------------------------------------------
def run_silent(cmd_list):
    """Run a subprocess with no visible window on Windows."""
    if platform.system().lower() == "windows":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        CREATE_NO_WINDOW = 0x08000000
        return subprocess.run(
            cmd_list,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=CREATE_NO_WINDOW,
            shell=False,
        )
    else:
        return subprocess.run(
            cmd_list,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )

# --------------------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------------------
def dns_reverse(ip: str) -> str:
    now = time.time()
    rec = _dns_reverse_cache.get(ip)
    if rec and now - rec[1] < DNS_REFRESH_SEC:
        return rec[0]
    name = ""
    try:
        name = socket.gethostbyaddr(ip)[0]  # FQDN if available
    except Exception:
        name = ""
    _dns_reverse_cache[ip] = (name, now)
    return name

def dns_forward(entry: str) -> Tuple[str, str]:
    """
    Resolve a hostname or IP from devices.txt.
    Returns (ip, canonical_name). If not resolvable, returns ("", "").
    """
    now = time.time()
    rec = _dns_forward_cache.get(entry)
    if rec and now - rec[2] < DNS_REFRESH_SEC:
        return rec[0], rec[1]

    ip, cname = "", ""
    try:
        if is_ip(entry):
            ip = entry
            cname = dns_reverse(ip)
        else:
            ip = socket.gethostbyname(entry)
            try:
                cname = socket.getfqdn(entry)
            except Exception:
                cname = entry
    except Exception:
        ip, cname = "", ""

    _dns_forward_cache[entry] = (ip, cname, now)
    return ip, cname

# --------------------------------------------------------------------------------------
# Ping
# --------------------------------------------------------------------------------------
def ping_target(target: str) -> bool:
    """Ping target (IP or hostname) silently without spawning consoles."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", target]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", target]
    try:
        res = run_silent(cmd)
        return res.returncode == 0
    except Exception:
        return False

# --------------------------------------------------------------------------------------
# SSH hostname retrieval (NX-OS + IOS-XE)
# --------------------------------------------------------------------------------------
def ssh_exec_once(ip: str, command: str) -> Tuple[bool, str]:
    for pwd in PASSWORDS:
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                ip,
                username=USERNAME,
                password=pwd,
                look_for_keys=False,
                allow_agent=False,
                timeout=SSH_TIMEOUT,
                banner_timeout=SSH_TIMEOUT,
                auth_timeout=SSH_TIMEOUT,
            )
            _, stdout, _ = client.exec_command(command, timeout=SSH_TIMEOUT)
            out = stdout.read().decode(errors="ignore").strip()
            return True, out
        except (AuthenticationException, BadHostKeyException, SSHException,
                NoValidConnectionsError, socket.error, socket.timeout):
            pass
        except Exception:
            pass
        finally:
            try:
                if client:
                    client.close()
            except Exception:
                pass
    return False, ""

def parse_iosxe_hostname(output: str) -> str:
    for line in output.splitlines():
        m = re.search(r'^\s*hostname\s+([A-Za-z0-9._\-]+)\s*$', line)
        if m:
            return m.group(1)
    return ""

def get_hostname_via_ssh(ip: str) -> str:
    # NX-OS: 'show hostname'
    ok, out = ssh_exec_once(ip, "show hostname")
    if ok and out:
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        for l in lines:
            m = re.search(r'Hostname\s*:\s*([A-Za-z0-9._\-]+)', l, flags=re.IGNORECASE)
            if m:
                return m.group(1)
        if len(lines) == 1 and re.match(r'^[A-Za-z0-9._\-]+$', lines[0]):
            return lines[0]

    # IOS-XE: 'show running-config | include ^hostname'
    ok, out = ssh_exec_once(ip, "show running-config | include ^hostname")
    if ok and out:
        hn = parse_iosxe_hostname(out)
        if hn:
            return hn

    return "unknown"

def get_hostname_cached(ip: str, should_try_ssh: bool) -> str:
    """
    Keep last-known hostname while DOWN.
    Only refresh via SSH if device is UP and cache is stale.
    """
    now = time.time()
    if ip in _hostname_cache and now - _hostname_cache[ip][1] < HOSTNAME_REFRESH_SEC:
        return _hostname_cache[ip][0]
    if should_try_ssh:
        hn = get_hostname_via_ssh(ip)
        _hostname_cache[ip] = (hn, now)
        return hn
    if ip in _hostname_cache:
        return _hostname_cache[ip][0]
    return "unknown"

# --------------------------------------------------------------------------------------
# Devices model
# --------------------------------------------------------------------------------------
class DeviceEntry:
    def __init__(self, original: str, ip: str, dns_name: str):
        self.original = original  # original line from devices.txt
        self.ip = ip              # resolved IP ('' if unresolved)
        self.dns_name = dns_name  # forward/reverse DNS name ('' if none)

def read_devices_file(file_path: str) -> List[str]:
    try:
        with open(file_path, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []

def resolve_devices(entries: List[str]) -> List[DeviceEntry]:
    resolved: List[DeviceEntry] = []
    for e in entries:
        ip, cname = dns_forward(e)
        if not ip:
            ip = e if is_ip(e) else ""
        if is_ip(e) and not cname and ip:
            cname = dns_reverse(ip)
        resolved.append(DeviceEntry(original=e, ip=ip, dns_name=cname))
    return resolved

# --------------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------------
def concurrent_ping(targets: List[str]) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(32, max(1, len(targets)))) as ex:
        fut_map = {ex.submit(ping_target, t): t for t in targets}
        for fut in as_completed(fut_map):
            t = fut_map[fut]
            try:
                results[t] = fut.result()
            except Exception:
                results[t] = False
    return results

def concurrent_hostname_refresh(ips_up: List[str]):
    now = time.time()
    to_refresh = []
    for ip in ips_up:
        if ip not in _hostname_cache or now - _hostname_cache[ip][1] >= HOSTNAME_REFRESH_SEC:
            to_refresh.append(ip)

    def fetch(ip):
        hn = get_hostname_via_ssh(ip)
        _hostname_cache[ip] = (hn, time.time())

    if to_refresh:
        with ThreadPoolExecutor(max_workers=min(16, len(to_refresh))) as ex:
            list(ex.map(fetch, to_refresh))

# --------------------------------------------------------------------------------------
# Layout / Drawing
# --------------------------------------------------------------------------------------
def compute_grid_positions(n, cols, x_gap, y_gap):
    rows = math.ceil(n / cols) if cols else 1
    positions = []
    for i in range(n):
        r = i // cols
        c = i % cols
        x = c * x_gap
        y = -r * y_gap
        positions.append((x, y))
    if n > 0:
        last_row_count = n % cols if (n % cols) != 0 else cols
        total_width = (cols - 1) * x_gap if n > cols else (last_row_count - 1) * x_gap
        x_offset = -total_width / 2.0
        total_height = (rows - 1) * y_gap
        y_offset = total_height / 2.0
        positions = [(x + x_offset, y + y_offset) for (x, y) in positions]
    return positions, rows

def draw_health_map(device_list: List[DeviceEntry],
                    up_map: Dict[str, bool],
                    hostnames: Dict[str, str],
                    ax, blink_on: bool):
    ax.clear()
    ax.set_facecolor("black")
    ax.axis("off")
    ax.set_aspect("equal", adjustable="box")

    # Build preview labels and determine spacing
    longest = 12
    preview_labels = []
    for dev in device_list:
        ip = dev.ip or dev.original
        ssh_hn = hostnames.get(ip, "unknown")
        disp_hn = ssh_hn if ssh_hn != "unknown" else (clean_hostname(dev.dns_name) if dev.dns_name else "unknown")
        disp_hn = clean_hostname(disp_hn)
        hn_wrapped = wrap_text(disp_hn, width=16)
        label = f"{hn_wrapped}\n{ip}"
        preview_labels.append(label)
        # longest line for spacing calc
        longest_line = max(len(line) for line in hn_wrapped.splitlines())
        longest = max(longest, longest_line, len(ip))

    # Wider gaps to avoid overlap
    X_GAP_MIN = 4.2
    X_GAP = max(X_GAP_MIN, 0.45 * longest + 1.8)
    Y_GAP = 5.0

    positions, rows = compute_grid_positions(len(device_list), COLS, X_GAP, Y_GAP)

    for dev, (x, y), label_text in zip(device_list, positions, preview_labels):
        target = dev.ip or dev.original
        up = up_map.get(target, False)

        # blinking for DOWN
        alpha = FULL_ALPHA if up else (FULL_ALPHA if blink_on else DIM_ALPHA)
        face_color = "green" if up else "red"
        status_text = "UP" if up else "DOWN"
        status_color = "white" if up else "yellow"
        radius = RADIUS_UP if up else RADIUS_DOWN

        circ = plt.Circle((x, y), radius, facecolor=face_color, edgecolor="white",
                          linewidth=1.8, antialiased=True, alpha=alpha)
        ax.add_patch(circ)

        ax.text(x, y, status_text, color=status_color, ha="center", va="center",
                fontsize=STATUS_FS, fontweight="bold", alpha=alpha)

        ax.text(x, y - (radius + 1.0), label_text,
                color="black", ha="center", va="center",
                fontsize=LABEL_FS, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="none", alpha=1.0))

    if device_list:
        width = max(1, (COLS - 1)) * X_GAP
        height = max(1, (rows - 1) * Y_GAP)
        pad_x = 2.5
        pad_y = 2.5
        max_radius = max(RADIUS_UP, RADIUS_DOWN)
        ax.set_xlim(-width/2 - pad_x, width/2 + pad_x)
        ax.set_ylim(-height/2 - (max_radius + 2.0) - pad_y, height/2 + (max_radius + 2.0) + pad_y)

    ax.set_title("Live Network Device Health", color="white", fontsize=16, fontweight="bold", pad=16)

# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main():
    # initial load & resolve
    raw_entries = read_devices_file(DEVICES_FILE)
    device_list = resolve_devices(raw_entries)
    last_reload = time.time()

    fig, ax = plt.subplots(figsize=(18, 10), dpi=110)
    plt.ion()
    plt.show()

    print("[*] Live monitoring (Ctrl+C to stop).")
    print("    - DOWN nodes blink.")
    print(f"    - Auto-reloads {DEVICES_FILE} every {DEVICES_RELOAD_SEC}s.")
    print("    - SSH hostnames cached and persist while DOWN.")
    print("    - Windows build: use --windowed and this file reads devices.txt next to the .exe")

    last_blink_toggle = time.time()
    blink_on = True

    try:
        while True:
            now = time.time()

            # periodic devices.txt reload
            if now - last_reload >= DEVICES_RELOAD_SEC:
                new_entries = read_devices_file(DEVICES_FILE)
                if new_entries != raw_entries:
                    raw_entries = new_entries
                    device_list = resolve_devices(raw_entries)
                last_reload = now

            # blink toggle
            if now - last_blink_toggle >= BLINK_PERIOD_SEC:
                blink_on = not blink_on
                last_blink_toggle = now

            # Targets for ping (prefer resolved IP, fall back to original)
            targets = [dev.ip if dev.ip else dev.original for dev in device_list]

            # Ping concurrently
            ping_map = concurrent_ping(targets)

            # Refresh hostnames for UP IPs (concurrent); cache keeps last-known for DOWN
            ips_up = [dev.ip for dev in device_list if dev.ip and ping_map.get((dev.ip or dev.original), False)]
            if ips_up:
                concurrent_hostname_refresh(ips_up)

            # Build hostnames map for plotting (IP key); preserve last-known when DOWN
            host_map: Dict[str, str] = {}
            for dev in device_list:
                target = dev.ip if dev.ip else dev.original
                up = ping_map.get(target, False)
                if dev.ip:
                    host_map[dev.ip] = get_hostname_cached(dev.ip, should_try_ssh=up)
                else:
                    host_map[dev.original] = "unknown"

            # Console view (optional—won't pop windows due to --windowed build)
            if platform.system().lower() != "windows":
                os.system('clear')
            else:
                # On Windows --windowed, there's no console; skip clear.
                pass
            print("Network Device Health Probe (auto-reload & DNS aware)\n" + "-" * 72)
            for dev in device_list:
                target = dev.ip if dev.ip else dev.original
                up = ping_map.get(target, False)
                ssh_hn = host_map.get(dev.ip or dev.original, "unknown")
                disp_name = clean_hostname(ssh_hn if ssh_hn != "unknown"
                                           else clean_hostname(dev.dns_name) if dev.dns_name else "unknown")
                print(f"{(dev.ip or dev.original):<18}  {'UP  ' if up else 'DOWN'}  hostname: {disp_name}")
            print("-" * 72)

            # Draw
            draw_health_map(device_list, ping_map, host_map, ax, blink_on=blink_on)
            plt.pause(0.12)

    except KeyboardInterrupt:
        print("\n[✓] Monitoring stopped by user.")
        plt.ioff()
        plt.close()

if __name__ == "__main__":
    main()
