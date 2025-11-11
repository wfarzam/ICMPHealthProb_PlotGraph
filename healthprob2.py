#!/usr/bin/env python3
# monitor_devices.py

import os, sys, platform, subprocess, time, math, re, socket, signal
from typing import Dict, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import paramiko
from paramiko.ssh_exception import AuthenticationException, SSHException, NoValidConnectionsError, BadHostKeyException

STOP_REQUESTED = False
def request_stop(*_a, **_kw):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    try: plt.close("all")
    except Exception: pass

signal.signal(signal.SIGINT, request_stop)
try: signal.signal(signal.SIGTERM, request_stop)
except Exception: pass

# ----- Paths -----
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(BASE_DIR, "devices.txt")

USERNAME = "admin"
PASSWORDS = ["Cisco123", "Admin123"]
SSH_TIMEOUT = 3.0
HOSTNAME_REFRESH_SEC = 120
MODEL_REFRESH_SEC = 300
RADIUS_UP, RADIUS_DOWN = 1.3, 1.5
LABEL_FS, STATUS_FS = 10, 12
COLS = 7
BLINK_PERIOD_SEC, DIM_ALPHA, FULL_ALPHA = 1.0, 0.25, 1.0
SUFFIXES = (".elements.local", ".intel.com", ".corp.nandps.com")
DEVICES_RELOAD_SEC, DNS_REFRESH_SEC = 10, 300

_hostname_cache, _model_cache = {}, {}
_dns_forward_cache, _dns_reverse_cache = {}, {}

# -------- helpers ----------
def clean_hostname(hn: str) -> str:
    if not hn: return "unknown"
    h = hn.strip()
    for sfx in SUFFIXES:
        if h.lower().endswith(sfx): h = h[:-len(sfx)]
    return h or "unknown"

def wrap_text(s, width=16):
    if len(s) <= width: return s
    lines = []
    while len(s) > width:
        cut = max(s[:width].rfind('-'), s[:width].rfind('.'))
        if cut >= 8: lines.append(s[:cut]); s = s[cut+1:]
        else: lines.append(s[:width]); s = s[width:]
    lines.append(s)
    return "\n".join(lines)

def is_ip(s):
    try: socket.inet_aton(s); return True
    except OSError: return False

def run_silent(cmd):
    if platform.system().lower()=="windows":
        si = subprocess.STARTUPINFO(); si.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        CREATE_NO_WINDOW=0x08000000
        return subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              startupinfo=si,creationflags=CREATE_NO_WINDOW)
    else:
        return subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def dns_reverse(ip):
    now=time.time()
    rec=_dns_reverse_cache.get(ip)
    if rec and now-rec[1]<DNS_REFRESH_SEC: return rec[0]
    try: name=socket.gethostbyaddr(ip)[0]
    except Exception: name=""
    _dns_reverse_cache[ip]=(name,now)
    return name

def dns_forward(e):
    now=time.time()
    rec=_dns_forward_cache.get(e)
    if rec and now-rec[2]<DNS_REFRESH_SEC: return rec[0],rec[1]
    ip,cname="",""
    try:
        if is_ip(e):
            ip=e; cname=dns_reverse(ip)
        else:
            ip=socket.gethostbyname(e)
            cname=socket.getfqdn(e)
    except Exception: pass
    _dns_forward_cache[e]=(ip,cname,now)
    return ip,cname

def ping_target(t):
    cmd=["ping","-n","1","-w","1000",t] if platform.system().lower()=="windows" else ["ping","-c","1","-W","1",t]
    try: return run_silent(cmd).returncode==0
    except Exception: return False

# ---------- SSH -------------
def ssh_exec_once(ip, cmd):
    for pwd in PASSWORDS:
        client=None
        try:
            client=paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(ip,username=USERNAME,password=pwd,timeout=SSH_TIMEOUT,
                           look_for_keys=False,allow_agent=False,banner_timeout=SSH_TIMEOUT)
            _,stdout,_=client.exec_command(cmd,timeout=SSH_TIMEOUT)
            return True,stdout.read().decode(errors="ignore").strip()
        except Exception: pass
        finally:
            try: client and client.close()
            except Exception: pass
    return False,""

def parse_iosxe_hostname(out):
    for line in out.splitlines():
        m=re.search(r'hostname\s+([\w\-.]+)',line)
        if m: return m.group(1)
    return ""

def get_hostname_via_ssh(ip):
    ok,out=ssh_exec_once(ip,"show hostname")
    if ok and out:
        for l in out.splitlines():
            m=re.search(r'Hostname\s*:\s*([\w\-.]+)',l,re.I)
            if m: return m.group(1)
        if re.match(r'^[\w\-.]+$',out.strip()): return out.strip()
    ok,out=ssh_exec_once(ip,"show run | include ^hostname")
    if ok and out:
        hn=parse_iosxe_hostname(out)
        if hn: return hn
    return "unknown"

def get_model_via_ssh(ip):
    # IOS-XE first
    ok,out=ssh_exec_once(ip,"show version | include Model Number")
    if ok and out:
        m=re.search(r'[Mm]odel\s+[Nn]umber\s*[:=]\s*([\w\-]+)',out)
        if m: return m.group(1).strip()
    # NX-OS show hardware
    ok,out=ssh_exec_once(ip,"show hardware")
    if ok and out:
        m=re.search(r'[Mm]odel\s+number\s+is\s+([\w\-]+)',out)
        if m: return m.group(1).strip()
    # NX-OS fallback
    ok,out=ssh_exec_once(ip,"show module")
    if ok and out:
        for line in out.splitlines():
            if re.search(r'SUP',line,re.I): continue
            m=re.search(r'^\s*\d+\s+\d+\s+.+?\s+([\w\-]+)\s+\S+',line)
            if m: return m.group(1).strip()
        m2=re.search(r'\b(N\dK[-\w]+)\b',out)
        if m2: return m2.group(1).strip()
    return "unknown"

def get_hostname_cached(ip,tryssh):
    now=time.time()
    if ip in _hostname_cache and now-_hostname_cache[ip][1]<HOSTNAME_REFRESH_SEC:
        return _hostname_cache[ip][0]
    if tryssh:
        hn=get_hostname_via_ssh(ip); _hostname_cache[ip]=(hn,now); return hn
    return _hostname_cache.get(ip,("unknown",0))[0]

def get_model_cached(ip,tryssh):
    now=time.time()
    if ip in _model_cache and now-_model_cache[ip][1]<MODEL_REFRESH_SEC:
        return _model_cache[ip][0]
    if tryssh:
        md=get_model_via_ssh(ip); _model_cache[ip]=(md,now); return md
    return _model_cache.get(ip,("unknown",0))[0]

class DeviceEntry:
    def __init__(self,o,ip,dns): self.original=o; self.ip=ip; self.dns_name=dns

def read_devices_file(path):
    try:
        with open(path) as f: return [l.strip() for l in f if l.strip()]
    except FileNotFoundError: return []

def resolve_devices(lst):
    res=[]
    for e in lst:
        ip,cname=dns_forward(e)
        if not ip: ip=e if is_ip(e) else ""
        if is_ip(e) and not cname and ip: cname=dns_reverse(ip)
        res.append(DeviceEntry(e,ip,cname))
    return res

# ---------- concurrency ----------
def concurrent_ping(targets):
    res={}
    with ThreadPoolExecutor(max_workers=min(32,max(1,len(targets)))) as ex:
        futs={ex.submit(ping_target,t):t for t in targets}
        for f in as_completed(futs):
            try: res[futs[f]]=f.result()
            except Exception: res[futs[f]]=False
    return res

def concurrent_hostname_refresh(ips):
    with ThreadPoolExecutor(max_workers=min(16,len(ips))) as ex:
        for ip in ips: ex.submit(lambda i: _hostname_cache.update({i:(get_hostname_via_ssh(i),time.time())}),ip)

def concurrent_model_refresh(ips):
    with ThreadPoolExecutor(max_workers=min(16,len(ips))) as ex:
        for ip in ips: ex.submit(lambda i: _model_cache.update({i:(get_model_via_ssh(i),time.time())}),ip)

# ---------- drawing ----------
def compute_grid_positions(n,cols,xgap,ygap):
    rows=math.ceil(n/cols) if cols else 1
    pos=[(c*xgap,-r*ygap) for i in range(n) for r,c in [(i//cols,i%cols)]]
    if not pos: return pos,rows
    last_row=n%cols or cols
    width=(cols-1)*xgap if n>cols else (last_row-1)*xgap
    xoff=-width/2; yoff=(rows-1)*ygap/2
    return [(x+xoff,y+yoff) for x,y in pos],rows

def draw_map(devs,upmap,hosts,models,ax,blink):
    ax.clear(); ax.set_facecolor("black"); ax.axis("off"); ax.set_aspect("equal")
    longest=12; labels=[]
    for d in devs:
        ip=d.ip or d.original
        h=clean_hostname(hosts.get(ip,"unknown") or d.dns_name or "unknown")
        m=models.get(ip,"unknown")
        lbl=f"{wrap_text(h,16)}\n{m}\n{ip}"; labels.append(lbl)
        longest=max(longest,max(len(x) for x in h.split()),len(m),len(ip))
    xgap=max(4.2,0.45*longest+1.8); ygap=5.4
    pos,rows=compute_grid_positions(len(devs),COLS,xgap,ygap)
    for d,(x,y),lbl in zip(devs,pos,labels):
        t=d.ip or d.original; up=upmap.get(t,False)
        alpha=FULL_ALPHA if up else (FULL_ALPHA if blink else DIM_ALPHA)
        color="green" if up else "red"; txt="UP" if up else "DOWN"; tcol="white" if up else "yellow"
        r=RADIUS_UP if up else RADIUS_DOWN
        ax.add_patch(plt.Circle((x,y),r,facecolor=color,edgecolor="white",lw=1.8,alpha=alpha))
        ax.text(x,y,txt,color=tcol,ha="center",va="center",fontsize=STATUS_FS,fontweight="bold",alpha=alpha)
        ax.text(x,y-(r+1.0),lbl,color="black",ha="center",va="center",fontsize=LABEL_FS,fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.35",fc="white",ec="none",alpha=1.0))
    if devs:
        w=(COLS-1)*xgap; h=(rows-1)*ygap
        ax.set_xlim(-w/2-2.5,w/2+2.5); ax.set_ylim(-h/2-3.5,h/2+3.5)
    ax.set_title("Live Network Device Health",color="white",fontsize=16,fontweight="bold",pad=16)

def main():
    global STOP_REQUESTED
    raw=read_devices_file(DEVICES_FILE); devs=resolve_devices(raw); last_reload=time.time()
    fig,ax=plt.subplots(figsize=(18,10),dpi=110)
    try:
        mgr=plt.get_current_fig_manager()
        if hasattr(mgr,"window"): mgr.window.protocol("WM_DELETE_WINDOW",request_stop)
    except Exception: pass
    plt.ion(); plt.show()
    blink=True; last_blink=time.time()
    while not STOP_REQUESTED:
        if not plt.fignum_exists(fig.number): break
        now=time.time()
        if now-last_reload>DEVICES_RELOAD_SEC:
            new=read_devices_file(DEVICES_FILE)
            if new!=raw: raw=new; devs=resolve_devices(raw)
            last_reload=now
        if now-last_blink>BLINK_PERIOD_SEC: blink=not blink; last_blink=now
        targets=[d.ip or d.original for d in devs]; upmap=concurrent_ping(targets)
        ips_up=[d.ip for d in devs if d.ip and upmap.get(d.ip,False)]
        if ips_up: concurrent_hostname_refresh(ips_up); concurrent_model_refresh(ips_up)
        hmap={d.ip:get_hostname_cached(d.ip,upmap.get(d.ip,False)) for d in devs if d.ip}
        mmap={d.ip:get_model_cached(d.ip,upmap.get(d.ip,False)) for d in devs if d.ip}
        draw_map(devs,upmap,hmap,mmap,ax,blink)
        plt.pause(0.12)
    plt.ioff(); plt.close('all'); os._exit(0)

if __name__=="__main__": main()
