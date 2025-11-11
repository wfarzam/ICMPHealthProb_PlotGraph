#!/usr/bin/env python3
import os
import platform
import subprocess
import time
import math
import matplotlib.pyplot as plt

def ping_device(ip):
    param = "-n" if platform.system().lower() == "windows" else "-c"
    cmd = ["ping", param, "1", ip]
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

def compute_grid_positions(n, cols=6, x_gap=5.0, y_gap=6.0):
    """Return list of (x,y) positions in a neat grid."""
    rows = math.ceil(n / cols)
    positions = []
    for i in range(n):
        r = i // cols
        c = i % cols
        x = c * x_gap
        y = -r * y_gap
        positions.append((x, y))
    # center grid around origin for symmetry
    if n > 0:
        last_row_count = n % cols if (n % cols) != 0 else cols
        total_width = (cols - 1) * x_gap if n > cols else (last_row_count - 1) * x_gap
        x_offset = -total_width / 2.0
        total_height = (rows - 1) * y_gap
        y_offset = total_height / 2.0
        positions = [(x + x_offset, y + y_offset) for (x, y) in positions]
    return positions, cols, rows, x_gap, y_gap

def draw_health_map(devices, results, ax, cols=6):
    ax.clear()
    ax.set_facecolor("black")
    ax.axis("off")
    ax.set_aspect("equal", adjustable="box")

    radius = 1.8
    positions, cols, rows, x_gap, y_gap = compute_grid_positions(len(devices), cols=cols)

    for (ip, up), (x, y) in zip(zip(devices, results), positions):
        color = "green" if up else "red"
        txt = "UP" if up else "DOWN"
        inside_color = "white" if up else "yellow"

        # circle (with white edge for crispness)
        circ = plt.Circle((x, y), radius, facecolor=color, edgecolor="white", linewidth=2, antialiased=True)
        ax.add_patch(circ)

        # text inside the circle
        ax.text(x, y, txt, color=inside_color, ha="center", va="center",
                fontsize=14, fontweight="bold")

        # IP label below in BLACK text with white rounded box for readability
        ax.text(x, y - (radius + 1.0), ip,
                color="black", ha="center", va="center",
                fontsize=11, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="none", alpha=1.0))

    # limits with padding
    if devices:
        width = max(1, (cols - 1)) * x_gap
        height = max(1, (rows - 1)) * y_gap
        pad_x = 3.0
        pad_y = 3.0
        ax.set_xlim(-width/2 - pad_x, width/2 + pad_x)
        ax.set_ylim(-height/2 - (radius + 2.0) - pad_y, height/2 + (radius + 2.0) + pad_y)

    ax.set_title("Live Network Device Health", color="white", fontsize=18, fontweight="bold", pad=20)

def main():
    devices = read_devices("devices.txt")
    if not devices:
        print("[!] No devices found in devices.txt.")
        return

    # tune how many per row if you like
    COLS = 6

    fig, ax = plt.subplots(figsize=(16, 9), dpi=110)
    plt.ion()
    plt.show()

    print("[*] Live device health monitoring (Ctrl + C to stop)")

    try:
        while True:
            results = [ping_device(ip) for ip in devices]

            # console view
            os.system('cls' if platform.system().lower() == 'windows' else 'clear')
            print("Network Device Health Probe\n" + "-" * 40)
            for ip, ok in zip(devices, results):
                print(f"{ip:<20} -> {'UP' if ok else 'DOWN'}")
            print("-" * 40)

            draw_health_map(devices, results, ax, cols=COLS)
            # refresh
            plt.pause(0.5)
            time.sleep(1.5)

    except KeyboardInterrupt:
        print("\n[✓] Monitoring stopped by user.")
        plt.ioff()
        plt.close()

if __name__ == "__main__":
    main()
