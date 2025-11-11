#!/usr/bin/env python3
import os
import platform
import subprocess
import time
import math
from typing import Dict, Tuple
import re
import socket

import matplotlib.pyplot as plt
import paramiko
from paramiko.ssh_exception import AuthenticationException, SSHException, NoValidConnectionsError, BadHostKeyException

# --- SSH auth ---
USERNAME = "admin"
PASSWORDS = ["cisco", "Admin123"]     # try in order
SSH_TIMEOUT = 3.0                        # seconds
HOSTNAME_REFRESH_SEC = 120               # cache refresh

# --- Visual layout ---
RADIUS = 1.0
LABEL_FS = 10
STATUS_FS = 12
COLS = 7

# --- Blink settings ---
BLINK_PERIOD_SEC = 1.0   # full on <-> dim every 1s
DIM_ALPHA = 0.25
FULL_ALPHA = 1.0

def ping_device(ip: str) -> bool:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]  # 1s timeout
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]     # 1s timeout
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def read_devices(file_path="devices.txt"):
    try:
        with open(file_path, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"[!] {file_path} not found.")
        return []

def ssh_exec_once(ip: str, command: str) -> Tuple[bool, str]:
    """Try SSH with each password; return (ok, output)."""
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
        except (AuthenticationException, BadHostKeyException, SSHException, NoValidConnectionsError, socket.error, socket.timeout):
            # try next password
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
    # looks for lines like "hostname NAME"
    for line in output.splitlines():
        m = re.search(r'^\s*hostname\s+([A-Za-z0-9._\-]+)\s*$', line)
        if m:
            return m.group(1)
    return ""

def get_hostname_via_ssh(ip: str) -> str:
    """
    Try NX-OS first, then IOS-XE. Return 'unknown' on failure.
    """
    # NX-OS (usually returns just the hostname or "Hostname: <name>")
    ok, out = ssh_exec_once(ip, "show hostname")
    if ok and out:
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # 1-liner hostname or "Hostname: NAME" or a token line
        for l in lines:
            m = re.search(r'Hostname\s*:\s*([A-Za-z0-9._\-]+)', l, flags=re.IGNORECASE)
            if m:
                return m.group(1)
        if len(lines) == 1:
            tok = lines[0]
            if re.match(r'^[A-Za-z0-9._\-]+$', tok):
                return tok

    # IOS-XE — use include to avoid pagination
    ok, out = ssh_exec_once(ip, "show running-config | include ^hostname")
    if ok and out:
        hn = parse_iosxe_hostname(out)
        if hn:
            return hn

    return "unknown"

# cache: ip -> (hostname, timestamp)
_hostname_cache: Dict[str, Tuple[str, float]] = {}

def get_hostname_cached(ip: str, should_try_ssh: bool) -> str:
    now = time.time()
    if ip in _hostname_cache:
        hn, ts = _hostname_cache[ip]
        if now - ts < HOSTNAME_REFRESH_SEC:
            return hn
    if should_try_ssh:
        hn = get_hostname_via_ssh(ip)
    else:
        hn = "unknown"
    _hostname_cache[ip] = (hn, now)
    return hn

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

def draw_health_map(devices, statuses, hostnames, ax, t_now):
    """
    statuses: list[bool] (True = UP, False = DOWN)
    hostnames: dict[ip] -> str
    t_now: time.time() for blinking
    """
    ax.clear()
    ax.set_facecolor("black")
    ax.axis("off")
    ax.set_aspect("equal", adjustable="box")

    # spacing based on longest label (hostname or IP)
    longest = 12
    for ip in devices:
        hn = hostnames.get(ip, "unknown")
        longest = max(longest, len(hn), len(ip))
    X_GAP_MIN = 3.6
    X_GAP = max(X_GAP_MIN, 0.34 * longest + 1.6)
    Y_GAP = 4.2

    positions, rows = compute_grid_positions(len(devices), COLS, X_GAP, Y_GAP)

    # blink phase toggles roughly every BLINK_PERIOD_SEC
    phase_on = int(t_now // BLINK_PERIOD_SEC) % 2 == 0

    for (ip, up), (x, y) in zip(zip(devices, statuses), positions):
        hostname = hostnames.get(ip, "unknown")

        if up:
            alpha = FULL_ALPHA
        else:
            alpha = FULL_ALPHA if phase_on else DIM_ALPHA

        face_color = "green" if up else "red"
        status_text = "UP" if up else "DOWN"
        status_color = "white" if up else "yellow"

        circ = plt.Circle((x, y), RADIUS, facecolor=face_color, edgecolor="white",
                          linewidth=1.8, antialiased=True, alpha=alpha)
        ax.add_patch(circ)

        # UP/DOWN text inside circle
        ax.text(x, y, status_text, color=status_color, ha="center", va="center",
                fontsize=STATUS_FS, fontweight="bold", alpha=alpha)

        # Hostname + IP badge (always shown)
        label_text = f"{hostname}\n{ip}"
        ax.text(x, y - (RADIUS + 0.9), label_text,
                color="black", ha="center", va="center",
                fontsize=LABEL_FS, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="none", alpha=1.0))

    # limits
    if devices:
        width = max(1, (COLS - 1)) * X_GAP
        height = max(1, (rows - 1)) * Y_GAP
        pad_x = 2.0
        pad_y = 2.0
        ax.set_xlim(-width/2 - pad_x, width/2 + pad_x)
        ax.set_ylim(-height/2 - (RADIUS + 1.6) - pad_y, height/2 + (RADIUS + 1.6) + pad_y)

    ax.set_title("Live Network Device Health", color="white", fontsize=16, fontweight="bold", pad=16)

def main():
    devices = read_devices("devices.txt")
    if not devices:
        print("[!] No devices found in devices.txt.")
        return

    fig, ax = plt.subplots(figsize=(16, 9), dpi=110)
    plt.ion()
    plt.show()

    print("[*] Live device health monitoring (Ctrl + C to stop)")
    print("[i] Will try SSH for hostname only on devices that are UP.")

    try:
        # refresh faster to make blinking obvious
        while True:
            t_now = time.time()
            statuses = [ping_device(ip) for ip in devices]

            # fetch (or reuse) hostnames; only try SSH for UP devices
            hostnames = {ip: get_hostname_cached(ip, should_try_ssh=up) for ip, up in zip(devices, statuses)}

            # console output
            os.system('cls' if platform.system().lower() == 'windows' else 'clear')
            print("Network Device Health Probe\n" + "-" * 54)
            for ip, up in zip(devices, statuses):
                hn = hostnames.get(ip, "unknown")
                print(f"{ip:<16}  {'UP  ' if up else 'DOWN'}  hostname: {hn}")
            print("-" * 54)

            draw_health_map(devices, statuses, hostnames, ax, t_now)

            # small pause—keeps UI smooth and blinking snappy
            plt.pause(0.2)

    except KeyboardInterrupt:
        print("\n[✓] Monitoring stopped by user.")
        plt.ioff()
        plt.close()

if __name__ == "__main__":
    main()
