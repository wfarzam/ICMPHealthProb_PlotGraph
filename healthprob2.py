#!/usr/bin/env python3
import os
import platform
import subprocess
import time
import math
import re
import socket
from typing import Dict, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib.pyplot as plt
import paramiko
from paramiko.ssh_exception import (
    AuthenticationException, SSHException, NoValidConnectionsError, BadHostKeyException
)

# ---------- Auth / SSH ----------
USERNAME = "admin"
PASSWORDS = ["cisco", "Admin123"]
SSH_TIMEOUT = 3.0
HOSTNAME_REFRESH_SEC = 120

# ---------- Visuals / Layout ----------
RADIUS = 1.3                 # bigger circle so UP/DOWN fits cleanly
LABEL_FS = 10
STATUS_FS = 12
COLS = 7

# ---------- Blinking ----------
BLINK_PERIOD_SEC = 1.0       # toggle every 1s
DIM_ALPHA = 0.25
FULL_ALPHA = 1.0

# ---------- Domain suffixes to strip from hostnames (case-insensitive) ----------
SUFFIXES = (".elements.local", ".intel.com", ".corp.nandps.com")

# ---------- Caches ----------
_hostname_cache: Dict[str, Tuple[str, float]] = {}

# ---------- Helpers ----------
def clean_hostname(hn: str) -> str:
    if not hn:
        return "unknown"
    h = hn.strip()
    h_lower = h.lower()
    for sfx in SUFFIXES:
        if h_lower.endswith(sfx):
            h = h[: -(len(sfx))]
            break
    return h if h else "unknown"

def ping_device(ip: str) -> bool:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

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
            stdin, stdout, stderr = client.exec_command(command, timeout=SSH_TIMEOUT)
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
    # NX-OS
    ok, out = ssh_exec_once(ip, "show hostname")
    if ok and out:
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        for l in lines:
            m = re.search(r'Hostname\s*:\s*([A-Za-z0-9._\-]+)', l, flags=re.IGNORECASE)
            if m:
                return m.group(1)
        if len(lines) == 1 and re.match(r'^[A-Za-z0-9._\-]+$', lines[0]):
            return lines[0]

    # IOS-XE
    ok, out = ssh_exec_once(ip, "show running-config | include ^hostname")
    if ok and out:
        hn = parse_iosxe_hostname(out)
        if hn:
            return hn

    return "unknown"

def get_hostname_cached(ip: str, should_try_ssh: bool) -> str:
    now = time.time()
    if ip in _hostname_cache:
        hn, ts = _hostname_cache[ip]
        if now - ts < HOSTNAME_REFRESH_SEC:
            return hn

    hn = "unknown"
    if should_try_ssh:
        hn = get_hostname_via_ssh(ip)

    _hostname_cache[ip] = (hn, now)
    return hn

def read_devices(file_path="devices.txt") -> List[str]:
    try:
        with open(file_path, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"[!] {file_path} not found.")
        return []

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

# ---------- Drawing ----------
def draw_health_map(devices, statuses, hostnames, ax, blink_on: bool):
    ax.clear()
    ax.set_facecolor("black")
    ax.axis("off")
    ax.set_aspect("equal", adjustable="box")

    # spacing based on cleaned hostname length + IP length
    longest = 12
    for ip in devices:
        hn = clean_hostname(hostnames.get(ip, "unknown"))
        longest = max(longest, len(hn), len(ip))

    X_GAP_MIN = 3.8
    X_GAP = max(X_GAP_MIN, 0.35 * longest + 1.6)
    Y_GAP = 4.4

    positions, rows = compute_grid_positions(len(devices), COLS, X_GAP, Y_GAP)

    for (ip, up), (x, y) in zip(zip(devices, statuses), positions):
        hn_clean = clean_hostname(hostnames.get(ip, "unknown"))

        alpha = FULL_ALPHA if up else (FULL_ALPHA if blink_on else DIM_ALPHA)

        face_color = "green" if up else "red"
        status_text = "UP" if up else "DOWN"
        status_color = "white" if up else "yellow"

        circ = plt.Circle((x, y), RADIUS, facecolor=face_color, edgecolor="white",
                          linewidth=1.8, antialiased=True, alpha=alpha)
        ax.add_patch(circ)

        ax.text(x, y, status_text, color=status_color, ha="center", va="center",
                fontsize=STATUS_FS, fontweight="bold", alpha=alpha)

        label_text = f"{hn_clean}\n{ip}"
        ax.text(x, y - (RADIUS + 0.9), label_text,
                color="black", ha="center", va="center",
                fontsize=LABEL_FS, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="none", alpha=1.0))

    if devices:
        width = max(1, (COLS - 1)) * X_GAP
        height = max(1, (rows - 1)) * Y_GAP
        pad_x = 2.0
        pad_y = 2.0
        ax.set_xlim(-width/2 - pad_x, width/2 + pad_x)
        ax.set_ylim(-height/2 - (RADIUS + 1.8) - pad_y, height/2 + (RADIUS + 1.8) + pad_y)

    ax.set_title("Live Network Device Health", color="white", fontsize=16, fontweight="bold", pad=16)

# ---------- Concurrency Wrappers ----------
def concurrent_ping(devices: List[str]) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(32, len(devices) or 1)) as ex:
        fut_map = {ex.submit(ping_device, ip): ip for ip in devices}
        for fut in as_completed(fut_map):
            ip = fut_map[fut]
            try:
                results[ip] = fut.result()
            except Exception:
                results[ip] = False
    return results

def concurrent_hostname_refresh(devices_up: List[str]) -> Dict[str, str]:
    """
    Refresh hostname cache concurrently only for devices whose cache is stale.
    Returns dict[ip] -> hostname (from cache after refresh).
    """
    now = time.time()
    to_refresh = []
    for ip in devices_up:
        if ip not in _hostname_cache or now - _hostname_cache[ip][1] >= HOSTNAME_REFRESH_SEC:
            to_refresh.append(ip)

    # fetch in parallel
    def fetch(ip):
        hn = get_hostname_via_ssh(ip)
        _hostname_cache[ip] = (hn, time.time())
        return ip, hn

    with ThreadPoolExecutor(max_workers=min(16, len(to_refresh) or 1)) as ex:
        futs = [ex.submit(fetch, ip) for ip in to_refresh]
        for fut in as_completed(futs):
            try:
                ip, _ = fut.result()
            except Exception:
                pass

    # build final map from cache
    out = {}
    for ip in devices_up:
        hn, ts = _hostname_cache.get(ip, ("unknown", now))
        out[ip] = hn
    return out

# ---------- Main ----------
def main():
    devices = read_devices("devices.txt")
    if not devices:
        print("[!] No devices found in devices.txt.")
        return

    fig, ax = plt.subplots(figsize=(16, 9), dpi=110)
    plt.ion()
    plt.show()

    print("[*] Live monitoring (Ctrl+C to stop). Blinking = DOWN nodes.")
    print("[i] Hostnames pulled over SSH for UP nodes; cache refresh every "
          f"{HOSTNAME_REFRESH_SEC}s.")

    last_blink_toggle = time.time()
    blink_on = True

    try:
        while True:
            # toggle blink state every BLINK_PERIOD_SEC
            now = time.time()
            if now - last_blink_toggle >= BLINK_PERIOD_SEC:
                blink_on = not blink_on
                last_blink_toggle = now

            # ping concurrently
            ping_map = concurrent_ping(devices)
            statuses = [ping_map.get(ip, False) for ip in devices]

            # refresh hostnames concurrently (only UP and stale)
            up_devices = [ip for ip, up in zip(devices, statuses) if up]
            if up_devices:
                concurrent_hostname_refresh(up_devices)

            # assemble hostnames from cache (UP) or 'unknown' (DOWN)
            hostnames = {}
            for ip, up in zip(devices, statuses):
                if up:
                    hn, ts = _hostname_cache.get(ip, ("unknown", 0))
                    hostnames[ip] = hn
                else:
                    hostnames[ip] = "unknown"

            # console
            os.system('cls' if platform.system().lower() == 'windows' else 'clear')
            print("Network Device Health Probe\n" + "-" * 60)
            for ip, up in zip(devices, statuses):
                hn_display = clean_hostname(hostnames.get(ip, "unknown"))
                print(f"{ip:<16}  {'UP  ' if up else 'DOWN'}  hostname: {hn_display}")
            print("-" * 60)

            # draw
            draw_health_map(devices, statuses, hostnames, ax, blink_on=blink_on)

            # keep UI responsive and blinking smooth
            plt.pause(0.15)

    except KeyboardInterrupt:
        print("\n[✓] Monitoring stopped by user.")
        plt.ioff()
        plt.close()

if __name__ == "__main__":
    main()
