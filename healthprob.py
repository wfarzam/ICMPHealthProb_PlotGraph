
#!/usr/bin/env python3
import os
import platform
import subprocess
import time
import matplotlib.pyplot as plt

def ping_device(ip):
    """Ping device once and return True if reachable."""
    param = "-n" if platform.system().lower() == "windows" else "-c"
    command = ["ping", param, "1", ip]
    try:
        output = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output.returncode == 0
    except Exception:
        return False

def read_devices(file_path="devices.txt"):
    """Read IPs or hostnames from devices.txt"""
    if not os.path.exists(file_path):
        print(f"[!] {file_path} not found.")
        return []
    with open(file_path, "r") as f:
        devices = [line.strip() for line in f if line.strip()]
    return devices

def draw_health_map(devices, results, ax):
    """Draw circles with perfect shape, internal text, and IP labels."""
    ax.clear()
    ax.set_facecolor("black")
    ax.axis("off")
    ax.set_aspect('equal', adjustable='box')  # lock aspect ratio

    total = len(devices)
    spacing = 5.0  # horizontal spacing
    radius = 1.5   # circle size

    for i, (ip, status) in enumerate(zip(devices, results)):
        x = i * spacing
        y = 0
        color = "green" if status else "red"
        label_text = "Device is UP" if status else "Device is DOWN"
        label_color = "white" if status else "yellow"

        # Draw circle
        circle = plt.Circle((x, y), radius, color=color, ec="white", lw=2)
        ax.add_patch(circle)

        # Text inside circle
        ax.text(x, y, label_text, color=label_color,
                ha="center", va="center", fontsize=10, fontweight="bold")

        # IP label below
        ax.text(x, y - (radius + 1.0), ip, color="white",
                ha="center", va="center", fontsize=11, fontweight="bold")

    # Keep everything visible and centered
    total_width = (total - 1) * spacing
    ax.set_xlim(-spacing, total_width + spacing)
    ax.set_ylim(-4, 4)
    ax.set_title("Live Network Device Health", color="white",
                 fontsize=18, fontweight="bold", pad=20)

def main():
    devices = read_devices()
    if not devices:
        print("[!] No devices found in devices.txt.")
        return

    fig, ax = plt.subplots(figsize=(14, 6))
    plt.ion()
    plt.show()

    print("[*] Starting live device health monitoring (Ctrl + C to stop)...")

    try:
        while True:
            results = [ping_device(ip) for ip in devices]

            os.system('cls' if platform.system().lower() == 'windows' else 'clear')
            print("Network Device Health Probe\n" + "-" * 40)
            for ip, res in zip(devices, results):
                status = "UP" if res else "DOWN"
                print(f"{ip:<20} --> {status}")
            print("-" * 40)

            draw_health_map(devices, results, ax)
            plt.pause(1.5)
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n[✓] Monitoring stopped by user.")
        plt.ioff()
        plt.close()

if __name__ == "__main__":
    main()
