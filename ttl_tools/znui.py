# -*- coding: utf-8 -*-
"""ZN-AX173G (EN7523) TTL 工具台 —— 串口终端 + 一键刷机 / 还原 / 固化

用法：
    python znui.py

功能：
    串口终端（实时收发、可手敲命令、命令历史）
    设备自检          识别 U-Boot / U-Boot 登录 / autoboot / OpenWrt / 原厂 / 启动中
    进 U-Boot         重启并精确打断 autoboot，自动应答 U-Boot 账号密码
    TFTP 刷机         选镜像 -> tftpboot 到 RAM -> erase -> write -> 回读 crc32 -> bootm
    只加载到 RAM      tftpboot + 校验，不写 flash（零风险试载，可手敲 bootm 试跑）
    备份              masterbank / 任意分区，8MB 分块走 HTTP，不会撑爆设备 /tmp
    固化 bootcmd      setenv bootcmd + saveenv（断电重启自动进 OpenWrt）
    还原原厂          bootcmd 改回 flash imgread 2048;bootm（主 bank 从未被写）
    重启设备

安全护栏（与 znflash.py 一致）：
    只允许写 flash 0x1EC0000 ~ 0xDDC0000（firmware 分区）；
    masterbank(0xC0000, 30MB 原厂) 和 reservearea(bootflag/EEPROM) 一个字节都不碰。
"""
import os
import queue
import re
import shutil
import socket
import struct
import sys
import threading
import time
import hashlib
import zlib
import ctypes
from ctypes import wintypes
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    _msg = ("缺少 pyserial 模块，无法打开串口。\n\n"
            "当前解释器：\n%s\n\n"
            "请执行（二选一）：\n"
            "    1) \"%s\" -m pip install pyserial\n"
            "    2) \"C:\\Program Files\\Python311\\python.exe\" -m pip install pyserial\n\n"
            "或直接双击 ZN-TTL工具台.bat（已自动挑选带 pyserial 的解释器）。"
            % (sys.executable, sys.executable))
    try:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("缺少依赖 pyserial", _msg)
        _r.destroy()
    except Exception:
        print(_msg)
    raise SystemExit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tftpd_min import TftpServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = HERE

# ---- flash 布局（与当前 DTS 一致） ----
FLASH_SAFE_MIN = 0x1EC0000      # firmware 起点；之前是原厂 masterbank
FLASH_SAFE_MAX = 0xDDC0000      # reservearea 起点（bootflag 在其 +0x200000）
RAM_ADDR = 0x88000000
RBUF_ADDR = 0x8A000000
DEFAULT_ADDR = 0x1EC0000
DEFAULT_READ_LEN = 0x500000     # bootcmd 里从 flash 读到 RAM 的长度
TFTP_NAME = "znfw.bin"

UBOOT_USER = "telecomadmin"
UBOOT_PASS = "nE7jA%5m"
STOCK_BOOTCMD = "flash imgread 2048;bootm"
PROMPT_UBOOT = "ECNT>"
PROMPT_OWRT = "root@OpenWrt"
STOCK_MARK = "Linux version 5.4"      # 原厂内核 5.4.55；OpenWrt 是 6.18
OPENWRT_MARK = "Linux version 6."     # OpenWrt 编译用 6.x 内核
BACKUP_CHUNK = 8 << 20                # 备份分块大小：每块先落 /tmp 再传，避免 tmpfs 爆掉

# 设备状态识别表：按“优先级从低到高”排列。
# 同一位置命中多条时，后面的（更具体的）覆盖前面的。
STATE_PATTERNS = [
    ("autoboot",    r"(?:Hit any key|stop autoboot)[^\n]*$"),
    ("booting",     r"(?:Starting kernel|Uncompressing Linux)[^\n]*$"),
    ("stock-login", r"(?i)(?:user\s*-?\s*name|login)\s*:\s*$"),
    ("uboot-login", r"(?i)(?:user\s*-?\s*name|pass\s*-?\s*word)\s*:\s*$"),
    ("stock",       r"(?m)^[^\n#]{0,60}[#$][ \t]*$"),
    ("openwrt",     r"root@[^\s:]+[^\n]*#[ \t]*$"),
    ("uboot",       r"ECNT>[ \t]*$"),
]
STATE_LABELS = {
    "uboot":       "U-Boot 命令行 (ECNT>)",
    "uboot-login": "U-Boot 登录提示（账号/密码）",
    "autoboot":    "U-Boot autoboot 倒计时",
    "openwrt":     "OpenWrt 运行中",
    "stock":       "原厂系统运行中",
    "stock-login": "原厂系统登录提示",
    "booting":     "内核启动中",
    "unknown":     "未识别",
}
SHELL_STATES = ("openwrt", "stock")     # 已进入可用 shell 的状态

DEFAULT_IMG = os.path.join(
    HERE, "firmware_191mb",
    "openwrt-airoha-en7523-airoha_en7523-ax3000-router-squashfs-sysupgrade.bin")

BG = "#f4f6f9"          # 窗口底
CARD = "#ffffff"        # 卡片底
BG2 = CARD              # 兼容旧引用
FG = "#1d2433"          # 主文字
SUB = "#8b95a7"         # 次文字
ACC = "#2f6bff"         # 强调蓝
LINE = "#e8ecf3"        # 边框
TERM_BG = "#12171f"     # 终端底
TERM_FG = "#c7d2e0"     # 终端字
BTN = dict(bg="#f4f6fa", fg="#28303d", activebackground="#e9edf5",
           activeforeground="#1d2433", relief="flat", bd=0, padx=10, pady=7,
           font=("Microsoft YaHei UI", 9), cursor="hand2")
BTN_PRIMARY = dict(bg="#2f6bff", fg="#ffffff", activebackground="#245be4",
                   activeforeground="#ffffff", relief="flat", bd=0, padx=10,
                   pady=7, font=("Microsoft YaHei UI", 9, "bold"),
                   cursor="hand2")
BTN_RED = dict(bg="#fff3f3", fg="#d92d20", activebackground="#ffe6e6",
               activeforeground="#c0261a", relief="flat", bd=0, padx=10,
               pady=7, font=("Microsoft YaHei UI", 9), cursor="hand2")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _host_ipv4s():
    """本机所有 IPv4 地址（多网卡时用来挑对 192.168.1.x 那张）。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def local_ip(target="192.168.1.1", prefer="192.168.1."):
    """返回与设备同网段的 PC 地址；多网卡时优先 192.168.1.x。"""
    ips = _host_ipv4s()
    for ip in sorted(ips):
        if ip.startswith(prefer):
            return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        s.close()
    # 最后兜底：任意一个非回环地址
    for ip in sorted(ips):
        if not ip.startswith("127."):
            return ip
    return "127.0.0.1"


def align_up(x, a):
    return (x + a - 1) // a * a


def human(n):
    return "%.2f MB" % (n / 1048576.0)


def http_get(url, dst, timeout=90):
    """带超时的 HTTP 下载（urlretrieve 没有超时，设备卡住会永久挂住）。"""
    with urllib.request.urlopen(url, timeout=timeout) as r, \
            open(dst, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    return os.path.getsize(dst)


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def file_crc32(path):
    crc = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            crc = zlib.crc32(b, crc)
    return "%08x" % (crc & 0xFFFFFFFF)


UB_CRC_RE = re.compile(r"==>\s*([0-9a-fA-F]{8})")


def uboot_crc32(out):
    """解析 U-Boot `crc32 addr len` 的输出：crc32 for ... ==> deadbeef"""
    if not out:
        return ""
    m = UB_CRC_RE.search(out)
    if m:
        return m.group(1).lower()
    # 兜底：取最后一段 8 位十六进制
    cand = re.findall(r"\b([0-9a-fA-F]{8})\b", out)
    return cand[-1].lower() if cand else ""


def parse_mtd(text):
    """解析 /proc/mtd -> [(name, num, size)]

    行格式：mtd3: 0bf00000 00020000 "firmware"
    中间那个 erasesize 必须跳过，否则一条都匹配不上。
    """
    out = []
    for num, szh, name in re.findall(
            r'mtd(\d+):\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"([^"]*)"', text):
        try:
            out.append((name, num, int(szh, 16)))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 串口
class SerialLink:
    """后台线程持续收串口数据；提供 tail / take_new / wait_until 等同步原语。"""

    def __init__(self, on_data=None):
        self.ser = None
        self._run = False
        self._th = None
        self._lock = threading.Lock()
        self.buf = ""
        self.new = ""
        self.last_rx = 0.0
        self.abort = None
        self.on_data = on_data

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud):
        if self.ser is not None:          # 重复点「连接」不要起两个收线程
            try:
                self.close()
            except Exception:
                pass
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=2.0)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._run = True
        self._th = threading.Thread(target=self._rx, daemon=True)
        self._th.start()

    def _rx(self):
        while self._run:
            ser = self.ser
            if ser is None:
                break
            try:
                n = ser.in_waiting
                raw = ser.read(n if n else 1)
            except Exception:
                break
            if not raw:
                continue
            txt = raw.decode("utf-8", "replace")
            with self._lock:
                self.buf = (self.buf + txt)[-400000:]
                self.new = (self.new + txt)[-200000:]
                self.last_rx = time.time()
            if self.on_data:
                self.on_data(txt)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("ascii", "replace")
        ser = self.ser
        if ser is not None and ser.is_open:
            ser.write(data)
            ser.flush()

    def close(self):
        self._run = False
        time.sleep(0.12)
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def tail(self):
        with self._lock:
            return self.buf

    def clear(self):
        with self._lock:
            self.buf = ""
            self.new = ""

    def take_new(self):
        with self._lock:
            d = self.new
            self.new = ""
            return d

    def touch(self):
        with self._lock:
            self.last_rx = time.time()

    def quiet(self, idle=1.0):
        with self._lock:
            return time.time() - self.last_rx > idle

    def wait_until(self, markers, timeout, poll=0.05):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.abort is not None and self.abort():
                return None
            t = self.tail()
            for m in markers:
                if m in t:
                    return m
            time.sleep(poll)
        return None

    def wait_quiet(self, idle=2.0, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.abort is not None and self.abort():
                return False
            if time.time() - t0 > idle and self.quiet(idle):
                return True
            time.sleep(0.05)
        return False


# ---------------------------------------------------------------- 圆角控件
import tkinter.font as tkfont

YAHEI = "Microsoft YaHei UI"

BTN_STYLES = {
    "normal": dict(fill="#f4f6fa", hover="#e9edf5", press="#dfe5ef",
                   line="#e4e9f1", fg="#28303d"),
    "primary": dict(fill="#2f6bff", hover="#245be4", press="#1d4cc4",
                    line="", fg="#ffffff"),
    "danger": dict(fill="#fff3f3", hover="#ffe6e6", press="#ffd8d8",
                   line="#ffd3d3", fg="#d92d20"),
    "ghost": dict(fill="#ffffff", hover="#f4f6fa", press="#eaeff7",
                  line="#e4e9f1", fg="#4b5566"),
}


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（平滑多边形近似）。"""
    r = max(0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """圆角按钮：Canvas 自绘，不依赖系统原生外观。"""

    def __init__(self, master, text="", command=None, kind="normal",
                 height=34, stretch=False, font=(YAHEI, 9), bg=None,
                 padx=16, radius=9):
        self._bg = bg or master.cget("bg")
        super().__init__(master, bd=0, highlightthickness=0, bg=self._bg,
                         height=height, cursor="hand2", takefocus=0,
                         width=1 if stretch else 1)
        self.text = text
        self.command = command
        self.kind = kind
        self.font = font
        self._stretch = stretch
        self._padx = padx
        self._radius = radius
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._mf = tkfont.Font(font=self.font)
        self.configure(width=(1 if stretch else
                              self._mf.measure(text) + padx * 2))
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- 状态
    def set_text(self, text):
        self.text = text
        if not self._stretch:
            self.configure(width=self._mf.measure(text) + self._padx * 2)
        self._redraw()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._redraw()

    def set_kind(self, kind):
        self.kind = kind
        self._redraw()

    def config(self, **kw):
        if "text" in kw:
            self.set_text(kw.pop("text"))
        if "state" in kw:
            self.set_enabled(kw.pop("state") != "disabled")
        if kw:
            super().config(**kw)

    configure = config

    # -- 事件
    def _on_enter(self, _=None):
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _=None):
        self._hover = self._pressed = False
        self._redraw()

    def _on_press(self, _=None):
        if self._enabled:
            self._pressed = True
            self._redraw()

    def _on_release(self, _=None):
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        self._hover = True
        self._redraw()
        if was and self.command:
            self.command()

    # -- 绘制
    def _redraw(self):
        self.delete("all")
        w = self.winfo_width() or int(self["width"])
        h = self.winfo_height() or int(self["height"])
        if w <= 1 or h <= 1:
            return
        st = BTN_STYLES.get(self.kind, BTN_STYLES["normal"])
        if not self._enabled:
            fill, line, fg = "#f7f8fb", "#eef1f6", "#b3bcc9"
        elif self._pressed:
            fill, line, fg = st["press"], st["line"], st["fg"]
        elif self._hover:
            fill, line, fg = st["hover"], st["line"], st["fg"]
        else:
            fill, line, fg = st["fill"], st["line"], st["fg"]
        kw = dict(fill=fill, outline=line if line else fill)
        if line:
            kw["width"] = 1
        round_rect(self, 1, 1, w - 1, h - 1, self._radius, **kw)
        self.create_text(w / 2.0, h / 2.0 + 1, text=self.text, fill=fg,
                         font=self.font)


class RoundProgress(tk.Canvas):
    """圆角进度条。"""

    def __init__(self, master, height=8, bg=None, color="#2f6bff"):
        self._bg = bg or master.cget("bg")
        super().__init__(master, bd=0, highlightthickness=0, bg=self._bg,
                         height=height)
        self._pct = 0
        self._color = color
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, pct):
        self._pct = max(0, min(100, int(pct)))
        self._redraw()

    def reset(self):
        self.set(0)

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height() or 8
        if w <= 1:
            return
        r = h / 2.0
        round_rect(self, 0, 0, w, h, r, fill="#e7ebf3", outline="")
        if self._pct > 0:
            fw = max(h, w * self._pct / 100.0)
            round_rect(self, 0, 0, fw, h, r, fill=self._color, outline="")


class Card(tk.Frame):
    """白底卡片：细边框 + 内边距。"""

    def __init__(self, master, title=None, bg=CARD):
        super().__init__(master, bg=bg, highlightthickness=1,
                         highlightbackground="#e8ecf3",
                         highlightcolor="#e8ecf3")
        self.body = self
        if title:
            tk.Label(self, text=title, bg=bg, fg="#8b95a7",
                     font=(YAHEI, 9, "bold")).pack(anchor="w", padx=14,
                                                   pady=(12, 6))


# ---------------------------------------------------------------- App
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.link = SerialLink(self._on_serial)
        self._cancel = threading.Event()
        self.link.abort = self._aborted
        self.btn_stop = None
        self._start_emergency_exit()
        self.busy = False
        self.btns = []
        self.hist = []
        self.hist_i = 0
        self.tftp_name = TFTP_NAME
        self.dev_ip = tk.StringVar(value="192.168.1.1")
        self.opt_confirm = tk.BooleanVar(value=False)
        self._backup_ts = 0.0
        self._backup_dir = ""
        self._at_bottom = True
        self._main_thr = threading.current_thread()
        self._closing = False
        self._bulk = False        # TFTP 传输中：丢掉 U-Boot 的 # 进度字符
        self._last_see = 0.0
        self._panic_noted = False
        self.build_ui()
        self.root.after(60, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI
    def build_ui(self):
        r = self.root
        r.title("ZN-AX173G (EN7523) TTL 工具台")
        # 窗口更大并打开时居中
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(1400, sw - 40)
        h = min(940, sh - 80)
        r.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                    max(0, (sh - h) // 2 - 20)))
        r.configure(bg=BG)
        r.minsize(1180, 760)
        try:
            ttk.Style().theme_use("vista")
        except Exception:
            pass

        # ================= 顶栏
        header = tk.Frame(r, bg=CARD, highlightthickness=1,
                          highlightbackground=LINE)
        header.pack(fill="x")
        hinner = tk.Frame(header, bg=CARD)
        hinner.pack(fill="x", padx=18, pady=11)

        brand = tk.Frame(hinner, bg=CARD)
        brand.pack(side="left")
        logo = tk.Canvas(brand, width=34, height=34, bg=CARD, bd=0,
                         highlightthickness=0)
        logo.pack(side="left")
        round_rect(logo, 1, 1, 33, 33, 10, fill=ACC, outline="")
        logo.create_text(17, 18, text="ZN", fill="#ffffff",
                         font=(YAHEI, 10, "bold"))
        tt = tk.Frame(brand, bg=CARD)
        tt.pack(side="left", padx=(11, 0))
        tk.Label(tt, text="ZN-AX173G TTL 工具台", bg=CARD, fg=FG,
                 font=(YAHEI, 12, "bold")).pack(anchor="w")
        tk.Label(tt, text="EcoNet EN7523 · 刷机 / 备份 / 还原",
                 bg=CARD, fg=SUB, font=(YAHEI, 8)).pack(anchor="w")

        bar = tk.Frame(hinner, bg=CARD)
        bar.pack(side="right")
        dot = tk.Canvas(bar, width=12, height=12, bg=CARD, bd=0,
                        highlightthickness=0)
        dot.pack(side="left", padx=(0, 5))
        self._conn_dot_canvas = dot
        self._conn_dot = dot.create_oval(3, 3, 10, 10, fill="#c9d1dd",
                                         outline="")
        self.conn_var = tk.StringVar(value="未连接")
        tk.Label(bar, textvariable=self.conn_var, bg=CARD, fg=SUB,
                 font=(YAHEI, 9)).pack(side="left", padx=(0, 18))

        tk.Label(bar, text="串口", bg=CARD, fg=SUB,
                 font=(YAHEI, 9)).pack(side="left")
        self.port_var = tk.StringVar(value="COM6")
        self.cb_port = ttk.Combobox(bar, textvariable=self.port_var, width=20,
                                    state="readonly")
        self.cb_port.pack(side="left", padx=(6, 6))
        b = RoundButton(bar, "刷新", command=self.refresh_ports, kind="ghost",
                        height=28, padx=12, bg=CARD, font=(YAHEI, 8))
        b.pack(side="left")
        self.baud_var = tk.StringVar(value="115200")
        ttk.Combobox(bar, textvariable=self.baud_var, width=7,
                     state="readonly",
                     values=["115200", "57600", "38400", "9600"]).pack(
            side="left", padx=6)
        b = RoundButton(bar, "使用帮助", command=self.show_help, kind="ghost",
                        height=30, padx=12, bg=CARD, font=(YAHEI, 8))
        b.pack(side="left", padx=(0, 6))
        self.btn_stop = RoundButton(bar, "停止", command=self.request_cancel,
                                    kind="danger", height=30, padx=14,
                                    bg=CARD, font=(YAHEI, 8))
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_stop.set_enabled(False)
        self.btn_conn = RoundButton(bar, "连接", command=self.toggle_connect,
                                    kind="primary", height=30, padx=22,
                                    bg=CARD, font=(YAHEI, 9, "bold"))
        self.btn_conn.pack(side="left", padx=(4, 0))

        # ================= 主体
        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=14)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=BG, width=398)
        right.pack(side="right", fill="y", padx=(14, 0))
        right.pack_propagate(False)

        # ---- 终端卡片
        termcard = tk.Frame(left, bg=CARD, highlightthickness=1,
                            highlightbackground=LINE)
        termcard.pack(fill="both", expand=True)
        th = tk.Frame(termcard, bg=CARD)
        th.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(th, text="串口日志", bg=CARD, fg=FG,
                 font=(YAHEI, 10, "bold")).pack(side="left")
        tk.Label(th, text="115200 · 8N1", bg=CARD, fg=SUB,
                 font=(YAHEI, 8)).pack(side="left", padx=(8, 0))
        b = RoundButton(th, "清屏", command=self.clear_log, kind="ghost",
                        height=28, padx=12, bg=CARD, font=(YAHEI, 8))
        b.pack(side="right")
        b = RoundButton(th, "保存日志", command=self.save_log, kind="ghost",
                        height=28, padx=12, bg=CARD, font=(YAHEI, 8))
        b.pack(side="right", padx=(0, 6))

        tk.Frame(termcard, bg="#f1f4f8", height=1).pack(fill="x", padx=16,
                                                        pady=(0, 10))
        term = tk.Frame(termcard, bg=TERM_BG)
        term.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.txt = tk.Text(term, bg=TERM_BG, fg=TERM_FG,
                           insertbackground="#7ea8ff",
                           font=("Consolas", 9), wrap="char", bd=0,
                           padx=12, pady=10, selectbackground="#1f4a8f",
                           relief="flat", highlightthickness=0)
        sb = ttk.Scrollbar(term, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.bind("<Key>", self._text_key)

        # ---- 命令行
        row = tk.Frame(left, bg=BG)
        row.pack(fill="x", pady=(10, 6))
        self.ent = tk.Entry(row, bg=CARD, fg=FG, insertbackground=ACC,
                            relief="flat", font=("Consolas", 10),
                            highlightthickness=1, highlightbackground=LINE,
                            highlightcolor=ACC)
        self.ent.pack(side="left", fill="x", expand=True, ipady=8)
        self.ent.bind("<Return>", self._on_enter)
        self.ent.bind("<Up>", self._hist_prev)
        self.ent.bind("<Down>", self._hist_next)
        b = RoundButton(row, "发送", command=self.send_line, kind="primary",
                        height=34, padx=20, bg=BG, font=(YAHEI, 9, "bold"))
        b.pack(side="left", padx=(10, 0))
        self.btns.append(b)

        # ---- 状态 + 进度
        prow = tk.Frame(left, bg=BG)
        prow.pack(fill="x")
        self.stat_var = tk.StringVar(value="就绪")
        tk.Label(prow, textvariable=self.stat_var, bg=BG, fg=ACC,
                 font=(YAHEI, 9, "bold"), width=40,
                 anchor="w").pack(side="left")
        self.pbar = RoundProgress(prow, height=8, bg=BG, color=ACC)
        self.pbar.pack(side="right", fill="x", expand=True, padx=(8, 0),
                       pady=5)

        # ---- 右侧：操作卡片
        canvas = tk.Canvas(right, bg=BG, bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        col = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=col, anchor="nw",
                                   tags="inner")
        col.bind("<Configure>",
                 lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self._col = col

        def card(title):
            cf = tk.Frame(col, bg=CARD, highlightthickness=1,
                          highlightbackground=LINE)
            cf.pack(fill="x", pady=(0, 12))
            tk.Label(cf, text=title, bg=CARD, fg=SUB,
                     font=(YAHEI, 9, "bold")).pack(anchor="w", padx=16,
                                                   pady=(12, 8))
            return cf

        def grow(cf, text, cmd, kind="normal", pad=(0, 8), h=34):
            b = RoundButton(cf, text, command=cmd, kind=kind, height=h,
                            stretch=True, padx=14, bg=CARD)
            b.pack(fill="x", padx=16, pady=pad)
            self.btns.append(b)
            return b

        def grow2(cf, t1, c1, t2, c2, k1="normal", k2="normal",
                  pad=(0, 8)):
            f = tk.Frame(cf, bg=CARD)
            f.pack(fill="x", padx=16, pady=pad)
            b = RoundButton(f, t1, command=c1, kind=k1, height=34,
                            stretch=True, padx=10, bg=CARD)
            b.pack(side="left", fill="x", expand=True)
            self.btns.append(b)
            b = RoundButton(f, t2, command=c2, kind=k2, height=34,
                            stretch=True, padx=10, bg=CARD)
            b.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self.btns.append(b)

        # ① 设备控制
        cf = card("设备控制")
        grow2(cf, "设备自检", lambda: self.run_task(self.task_selfcheck),
              "重启设备", self.ask_reboot)
        grow(cf, "进 U-Boot（重启并打断 autoboot）",
             lambda: self.run_task(self.task_enter_uboot), pad=(0, 16))

        # ② 刷写 OpenWrt（有风险·仅限懂行）
        cf = card("刷写 OpenWrt（有风险·仅限懂行）")
        self.img_var = tk.StringVar(
            value=DEFAULT_IMG if os.path.isfile(DEFAULT_IMG) else "")
        tk.Entry(cf, textvariable=self.img_var, bg="#f7f9fc", fg="#41506b",
                 insertbackground=ACC, relief="flat", font=("Consolas", 8),
                 highlightthickness=1, highlightbackground=LINE).pack(
            fill="x", padx=16, ipady=6, pady=(0, 8))
        f2 = tk.Frame(cf, bg=CARD)
        f2.pack(fill="x", padx=16, pady=(0, 10))
        b = RoundButton(f2, "浏览镜像…", command=self.pick_image,
                        kind="ghost", height=34, stretch=True, padx=10,
                        bg=CARD)
        b.pack(side="left", fill="x", expand=True)
        self.btns.append(b)
        tk.Label(f2, text="偏移", bg=CARD, fg=SUB,
                 font=(YAHEI, 9)).pack(side="left", padx=(10, 6))
        self.addr_var = tk.StringVar(value="0x1ec0000")
        tk.Entry(f2, textvariable=self.addr_var, bg="#f7f9fc", fg=FG,
                 insertbackground=ACC, relief="flat", width=10,
                 font=("Consolas", 9), highlightthickness=1,
                 highlightbackground=LINE).pack(side="left", ipady=6)

        self.opt_boot = tk.BooleanVar(value=True)
        self.opt_bootcmd = tk.BooleanVar(value=False)
        for var, text in ((self.opt_boot, "写完从 RAM 启动验证"),
                          (self.opt_bootcmd, "顺便固化 bootcmd")):
            tk.Checkbutton(cf, text=text, variable=var, bg=CARD, fg="#3b4554",
                           activebackground=CARD, activeforeground=FG,
                           selectcolor="#f7f9fc", anchor="w",
                           font=(YAHEI, 9), bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)
        tk.Checkbutton(cf, text="我已确认镜像适用于本机(防刷错变砖)",
                       variable=self.opt_confirm, bg=CARD, fg="#b42318",
                       activebackground=CARD, activeforeground="#b42318",
                       selectcolor="#f7f9fc", anchor="w",
                       font=(YAHEI, 9, "bold"), bd=0,
                       highlightthickness=0).pack(fill="x", padx=18, pady=(2, 0))
        grow(cf, "立即刷机（U-Boot 直写 flash）", self.ask_flash,
             "primary", pad=(0, 8), h=36)
        grow(cf, "仅加载到 RAM 验证（不写 flash）",
             lambda: self.run_task(self.task_flash, True), pad=(0, 16))

        # ③ 备份与还原
        cf = card("备份与还原")
        grow(cf, "① 备份全部分区（刷机前必做）", self.ask_backup_all,
             "primary", pad=(0, 8), h=38)
        row_ip = tk.Frame(cf, bg=CARD)
        row_ip.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(row_ip, text="设备IP", bg=CARD, fg=SUB,
                 font=(YAHEI, 9)).pack(side="left")
        tk.Entry(row_ip, textvariable=self.dev_ip, bg="#f7f9fc", fg=FG,
                 insertbackground=ACC, relief="flat", width=14,
                 font=("Consolas", 9), highlightthickness=1,
                 highlightbackground=LINE).pack(side="left", padx=(6, 0), ipady=4)
        tk.Label(row_ip, text="(HTTP 备份用)", bg=CARD, fg=SUB,
                 font=(YAHEI, 8)).pack(side="left", padx=(6, 0))
        grow(cf, "备份原厂固件（只备 masterbank）",
             self.ask_backup_stock)
        grow(cf, "登录原厂系统，选分区备份",
             self.ask_backup_stock_ttl)
        grow2(cf, "备份分区…", self.ask_backup,
              "分区 / mtdparts", lambda: self.run_task(self.task_partitions))
        grow2(cf, "还原原厂固件（写 masterbank）", self.ask_restore_stock_fw,
              "切换原厂引导", self.ask_restore, "danger", "normal")
        grow(cf, "固化 bootcmd（断电自动进 OpenWrt）",
             lambda: self.run_task(self.task_fix_bootcmd), pad=(0, 16))


        self.refresh_ports()
        try:
            _mt = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(os.path.getmtime(__file__)))
            self._append("[源码] %s（修改于 %s）\n"
                         % (os.path.abspath(__file__), _mt))
            self._append("[版本] 大镜像自动抬高 RAM 地址（>36MB → 0x8C000000）\n\n")
        except Exception:
            pass

        self._append("ZN-AX173G TTL 工具台已就绪\n"
                     "1) 选 COM 口点「连接」  2) 「设备自检」看当前阶段\n"
                     "3) 【刷机前必做】点「① 备份全部分区」：把 bootloader/romfile/\n"
                     "   masterbank/firmware/reservearea 逐块拉到 PC 并校验 md5。\n"
                     "   设备还在 U-Boot 时先「仅加载到 RAM 验证」→ 回车 bootm 进临时系统再备份。\n"
                     "4) 只想试跑不写 flash：「仅加载到 RAM 验证」→ 回车 bootm\n"
                     "5) 刷机：选镜像 → 勾「确认镜像适用于本机」→ 「立即刷机」\n"
                     "   大固件建议：先把小系统刷起来，再用浏览器 Web 升级（系统→备份/升级）\n"
                     "安全网：只写 firmware 分区；原厂在主 bank(0xC0000, 30MB)，\n"
                     "可「切换原厂引导」随时还原。不确定就看右上「使用帮助」。\n\n")

    def pbar_set(self, pct):
        self.q.put(("pbar", max(0, min(100, int(pct)))))

    def _section(self, parent, title):
        tk.Frame(parent, bg=LINE, height=1).pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(parent, text=title, bg=BG2, fg=SUB,
                 font=(YAHEI, 8, "bold")).pack(anchor="w", padx=12, pady=(0, 4))

    # ---------------- 日志
    def _text_key(self, e):
        """只读终端：放行 Ctrl 组合（复制等），拦掉普通输入。"""
        if e.state & 0x4:
            return None
        return "break"

    def _on_serial(self, txt):
        txt = ANSI_RE.sub("", txt).replace("\r", "").replace("\b", "")
        if not self._panic_noted and ("Kernel panic" in txt
                                      or "Internal error: Oops" in txt):
            self._panic_noted = True
            t = self.link.tail()
            if "overlay/upper/etc/modules-boot.d" in t:
                why = ("initramfs 启动时把 flash 上的 rootfs_data(/overlay) 挂进来了，\n"
                       "    又从 /overlay/upper/etc/modules-boot.d/ 加载了**另一个内核版本\n"
                       "    编译的 kmod**，ABI 不匹配才崩的——镜像本身没坏。\n"
                       "    原厂机器没有 rootfs_data 分区，不会触发这个；\n"
                       "    已经刷过 OpenWrt 的机器上，旧 initramfs 必然会这样。")
            elif "Unknown symbol" in t:
                why = ("镜像里自带的 kmod 与它自己的内核不是同一批编译的，ABI 不匹配。")
            else:
                why = "镜像自身问题（内核/模块/设备树不匹配等）。"
            self.q.put(("log", "\n" + "!" * 66 + "\n"
                        "[!] 检测到内核 panic / Oops\n"
                        "    原因：" + why + "\n"
                        "    flash 一个字节没写；设备几秒后会自动重启，\n"
                        "    由 bootcmd 从 flash 启动原系统。\n"
                        + "!" * 66 + "\n"))
        if self._bulk:
            # U-Boot 每收一块打一个 '#'，几十万个字符灌进 Text 会把主线程
            # 拖死，进而抢不到 GIL 给 TFTP 线程，反过来把传输拖崩。
            txt = txt.replace("#", "")
            if not txt.strip():
                return
        txt = re.sub(r"#{20,}", "####", txt)
        self.q.put(("log", txt))

    def log(self, s):
        self.q.put(("log", s))

    def stat(self, s):
        self.q.put(("stat", s))

    def _append(self, s):
        if not s:
            return
        try:
            at_bottom = self.txt.yview()[1] > 0.97
        except Exception:
            at_bottom = True
        self.txt.insert("end", s)
        n = int(self.txt.index("end-1c").split(".")[0])
        if n > 5000:
            self.txt.delete("1.0", "%d.0" % (n - 4000))
        now = time.time()
        if at_bottom and now - self._last_see > 0.25:
            self._last_see = now
            self.txt.see("end")

    def _pump(self):
        out = []
        stat = None
        done = False
        pbar = None
        prefill = None
        while True:
            try:
                kind, val = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                out.append(val)
            elif kind == "stat":
                stat = val
            elif kind == "pbar":
                pbar = val
            elif kind == "prefill":
                prefill = val
            elif kind == "done":
                done = True
        if out:
            self._append("".join(out))
        if prefill:
            self.ent.delete(0, "end")
            self.ent.insert(0, prefill)
            self.ent.focus_set()
        if stat is not None:
            self.stat_var.set(stat)
        if pbar is not None:
            self.pbar.set(pbar)
            if pbar >= 100:
                self.root.after(1600, self.pbar.reset)
        if done:
            self.busy = False
            self._set_enabled(True)
            if self.btn_stop:
                self.btn_stop.set_enabled(False)
        self.root.after(60, self._pump)

    def clear_log(self):
        self.txt.delete("1.0", "end")

    def save_log(self):
        p = filedialog.asksaveasfilename(
            initialdir=LOG_DIR, defaultextension=".log",
            initialfile=time.strftime("znui_%Y%m%d_%H%M%S.log"),
            filetypes=[("log", "*.log"), ("text", "*.txt")])
        if not p:
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.txt.get("1.0", "end"))
        self.log("\n[OK] 日志已保存: %s\n" % p)

    # ---------------- 串口连接
    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports
        cur = self.port_var.get()
        if ports and cur not in ports:
            self.port_var.set("COM6" if "COM6" in ports else ports[0])

    def toggle_connect(self):
        if self.busy and self.link.is_open:
            if not messagebox.askyesno(
                    "任务进行中",
                    "当前有任务在跑，断开串口会让它卡在等数据上。\n"
                    "建议先点「停止」。仍要断开吗？", icon="warning"):
                return
        if self.link.is_open:
            self.link.close()
            self.btn_conn.config(text="连接")
            self.btn_conn.set_kind("primary")
            self.conn_var.set("未连接")
            self._set_dot("#c9d1dd")
            self.log("\n[串口已断开]\n")
            return
        port = self.port_var.get()
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 115200
        try:
            self.link.open(port, baud)
        except Exception as e:
            messagebox.showerror("串口打不开",
                                 "%s\n\n可能被其他脚本/终端占用，先关掉再试。" % e)
            return
        self.btn_conn.config(text="断开")
        self.btn_conn.set_kind("normal")
        self.conn_var.set("已连接 %s @%d" % (port, baud))
        self._set_dot("#22c55e")
        self.log("\n[串口已打开 %s @%d]\n" % (port, baud))

    def _set_dot(self, color):
        try:
            dot = getattr(self, "_conn_dot_canvas", None)
            if dot:
                dot.itemconfigure(self._conn_dot, fill=color)
        except Exception:
            pass

    # ---------------- 命令输入
    def _on_enter(self, _):
        self.send_line()

    def _hist_prev(self, _):
        if not self.hist:
            return
        self.hist_i = max(0, self.hist_i - 1)
        self.ent.delete(0, "end")
        self.ent.insert(0, self.hist[self.hist_i])

    def _hist_next(self, _):
        if not self.hist:
            return
        self.hist_i = min(len(self.hist), self.hist_i + 1)
        self.ent.delete(0, "end")
        if self.hist_i < len(self.hist):
            self.ent.insert(0, self.hist[self.hist_i])

    def send_line(self):
        s = self.ent.get()
        self.ent.delete(0, "end")
        if not s:
            self.link.write("\r")
            return
        self.hist.append(s)
        self.hist_i = len(self.hist)
        self.link.write(s + "\r")

    # ---------------- 任务框架
    def _set_enabled(self, on):
        for b in self.btns:
            try:
                if hasattr(b, "set_enabled"):
                    b.set_enabled(on)
                else:
                    b.config(state="normal" if on else "disabled")
            except Exception:
                pass

    def run_task(self, fn, *a):
        if self.busy:
            return
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先选择串口并点「连接」。")
            return
        self.busy = True
        self._cancel.clear()
        self._panic_noted = False
        self._set_enabled(False)
        if self.btn_stop:
            self.btn_stop.set_enabled(True)

        def wrapper():
            try:
                fn(*a)
            except Exception as e:
                self.log("\n[X] 任务异常: %r\n" % e)
            finally:
                self.q.put(("done", None))

        threading.Thread(target=wrapper, daemon=True).start()

    def run_cmd(self, cmd, idle=2.0, timeout=120):
        self.log("\n>>> %s\n" % cmd)
        self.link.clear()
        self.link.touch()
        self.link.write(cmd + "\r")
        self.link.wait_quiet(idle, timeout)
        return self.link.tail()

    def send_and_until(self, cmd, marker=PROMPT_UBOOT, timeout=600, label=None):
        self.log("\n>>> %s\n" % (label or cmd))
        self.link.clear()
        self.link.write(cmd + "\r")
        ok = self.link.wait_until([marker], timeout) is not None
        if not ok:
            self.log("[!] 等待 %r 超时（%ds）\n" % (marker, timeout))
        return ok

    # ---------------- 设备定位
    def _sniff(self, tail=None):
        """只读串口缓冲末尾，按“最后发生的那件事”判断设备状态。"""
        # 串口行尾是 \r\n，\r 会让 "提示符 + 行尾" 匹配不上，先去掉
        t = (self.link.tail() if tail is None else tail)[-800:].replace("\r", "")
        if not t:
            return "unknown"
        best, best_pos = "unknown", -1
        for name, pat in STATE_PATTERNS:
            pos = -1
            try:
                for m in re.finditer(pat, t):
                    pos = m.start()
            except re.error:
                continue
            if pos >= 0 and pos >= best_pos:     # 后面的更具体，同位置优先
                best, best_pos = name, pos
        return best

    def detect(self, wait=8.0, probe=True):
        """识别设备当前阶段，返回：
        uboot / uboot-login / autoboot / openwrt / stock / stock-login /
        booting / unknown

        关键：先静默观察，绝不轻易写串口——一个回车就可能打断 autoboot。
        """
        t0 = time.time()
        st = self._sniff()
        if st != "unknown" and self.link.quiet(0.35):
            return st
        while time.time() - t0 < min(wait, 4.0):
            if self._aborted():
                return "unknown"
            st = self._sniff()
            if st != "unknown" and self.link.quiet(0.45):
                return st
            time.sleep(0.08)
        # 完全静默又判不出来，才发一个回车试探（此时不可能在 autoboot 倒计时）
        if probe and self.link.quiet(1.2):
            self.link.write("\r")
            t1 = time.time()
            while time.time() - t1 < 3.0:
                if self._aborted():
                    return "unknown"
                st = self._sniff()
                if st != "unknown" and self.link.quiet(0.3):
                    return st
                time.sleep(0.08)
        return self._sniff()

    def _uboot_login(self, window=90):
        """在 U-Boot 账号/密码提示上完成登录；已在 ECNT> 则直接成功。"""
        self.stat("U-Boot 登录 ...")
        # 缓冲里已经在提问就直接答：再多敲一个回车反而会把提示搅乱
        t = self.link.tail()[-400:].replace("\r", "")
        mu = mp = None
        for mm in re.finditer(r"User\s*-?\s*Name\s*:", t, re.I):
            mu = mm
        for mm in re.finditer(r"Pass\s*-?\s*word\s*:", t, re.I):
            mp = mm
        user_n = pass_n = 0
        if mu and (mp is None or mu.start() > mp.start()):
            self.log("\n>>> U-Boot 账号: %s\n" % UBOOT_USER)
            self.link.write(UBOOT_USER + "\r")
            self.link.take_new()
            user_n = 1
        elif mp:
            self.link.write(UBOOT_PASS + "\r")
            self.link.take_new()
            pass_n = 1
        else:
            self.link.clear()
            self.link.write("\r")
        t0 = time.time()
        while time.time() - t0 < window:
            if self._aborted():
                self.log("[!] 已取消 U-Boot 登录\n")
                return False
            chunk = self.link.take_new()
            if chunk:
                # 一轮只应答一个提示，避免把密码当账号发出去（旧版就栽在这）
                if re.search(r"User\s*-?\s*Name\s*:", chunk, re.I):
                    self.log("\n>>> U-Boot 账号: %s\n" % UBOOT_USER)
                    self.link.write(UBOOT_USER + "\r")
                    user_n += 1
                elif re.search(r"Pass\s*-?\s*word\s*:", chunk, re.I):
                    self.link.write(UBOOT_PASS + "\r")
                    pass_n += 1
            if self._sniff() == "uboot" and self.link.quiet(0.5):
                self.log("[OK] 已进入 U-Boot\n")
                return True
            if user_n > 3 or pass_n > 3:
                self.log("[X] U-Boot 账号/密码被拒（原厂可能改过密码）\n")
                return False
            time.sleep(0.03)
        self.log("[X] U-Boot 登录超时\n")
        return False

    def enter_uboot(self, reboot_first=True, window=240):
        """让设备停在 U-Boot 命令行(ECNT>)。reboot_first=True 时会先重启。"""
        self.stat("识别设备状态 ...")
        state = self.detect()
        self.log("[状态] 当前: %s\n" % STATE_LABELS.get(state, state))

        if state == "uboot":
            # 刚上电时 ECNT> 可能只是缓冲里的残留，回一次车确认真能收命令
            self.link.clear()
            self.link.write("\r")
            if self._shell_wait(8):
                return True
            self.log("[!] U-Boot 提示符不稳定（可能刚上电/看门狗），重新等待 ...\n")
            state = "unknown"
        if state == "uboot-login":
            return self._uboot_login(window)
        if state == "autoboot":
            self.log("[步骤] 正在 autoboot 倒计时，直接打断\n")
            self.link.write("x")
            return self._uboot_login(60)
        if state == "openwrt" and not reboot_first:
            self.log("[X] 设备正在 OpenWrt 运行；本操作需要重启进 U-Boot\n")
            return False
        if state == "stock-login":
            # 停在 tc login: 时直接发 reboot 会被当成账号，先登进去再重启
            self.log("[步骤] 设备停在原厂登录提示，先登录再重启进 U-Boot\n")
            if not self._ensure_stock_login():
                self.log("[X] 原厂登录失败，无法重启；可以断电重启后再试\n")
                return False
            self.log("[OK] 已登录，开始重启\n")
            state = "stock"

        rebooted = False
        if state in SHELL_STATES:
            self.log("[步骤] 重启设备（%s），准备在 autoboot 处打断\n"
                     % STATE_LABELS.get(state, state))
            self.link.clear()
            self.link.write("reboot\r")
            rebooted = True
            time.sleep(0.8)
        else:
            # booting / unknown：不发 reboot，避免把字符灌进登录提示
            if state == "unknown":
                self.log("\n" + "-" * 60 + "\n"
                         "[请通电] 串口没有任何输出，设备应该是断电状态。\n"
                         ">>> 现在给路由器通电即可，工具台会自动抓 autoboot 并打断，\n"
                         "    全程不用按任何键。\n" + "-" * 60 + "\n")
                self.stat("等待设备上电 ...")
            else:
                self.log("[步骤] 设备正在启动，等待 autoboot 窗口（不发送字符）\n")

        self.stat("等待 U-Boot ...")
        t0 = time.time()
        aborted = False
        user_n = pass_n = 0
        restarted = 0
        powered = state != "unknown"
        while time.time() - t0 < window:
            if self._aborted():
                self.log("[!] 已取消进 U-Boot\n")
                return False
            chunk = self.link.take_new()
            if chunk:
                if not powered:
                    powered = True
                    self.log("[OK] 检测到设备上电，开始抓 autoboot\n")
                if ("Hit any key" in chunk or "stop autoboot" in chunk) and not aborted:
                    self.link.write("x")
                    aborted = True
                    self.log("\n[!] 已打断 autoboot\n")
                if re.search(r"User\s*-?\s*Name\s*:", chunk, re.I):
                    self.link.write(UBOOT_USER + "\r")
                    user_n += 1
                elif re.search(r"Pass\s*-?\s*word\s*:", chunk, re.I):
                    self.link.write(UBOOT_PASS + "\r")
                    pass_n += 1
                if user_n > 3 or pass_n > 3:
                    self.log("[X] U-Boot 账号/密码被拒（原厂可能改过密码）\n")
                    return False
                if "Starting kernel" in chunk and rebooted and restarted < 2:
                    # 打断没抢上，内核已经跑起来了：重启重来
                    restarted += 1
                    self.log("[!] 没抢到 autoboot 窗口，重启重试（%d/2）\n"
                             % restarted)
                    aborted = False
                    user_n = pass_n = 0
                    self.link.clear()
                    self.link.write("reboot\r")
            if self._sniff() == "uboot" and self.link.quiet(0.6):
                self.log("[OK] 已进入 U-Boot\n")
                return True
            time.sleep(0.02)
        st = self._sniff()
        if st in SHELL_STATES or st == "stock-login":
            self.log("[X] 没抢到 autoboot，设备已经启动进系统了（%s）。\n"
                     "    再点一次即可：工具台会先登录/重启，然后重新抓窗口。\n"
                     % STATE_LABELS.get(st, st))
        else:
            self.log("[X] 未能进入 U-Boot（%.0fs）；确认串口接线和供电后重试\n"
                     % window)
        return False

    def wait_openwrt(self, timeout=240):
        if self.link.wait_until(["Starting kernel"], 120) is None:
            self.log("[!] 没看到 Starting kernel，继续等用户态 ...\n")
        self.stat("等待 OpenWrt 用户态 ...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._aborted():
                return False
            if self._sniff() == "openwrt" and self.link.quiet(0.4):
                self.log("\n[OK] OpenWrt 已启动（%.0fs）\n" % (time.time() - t0))
                return True
            time.sleep(1.0)
            self.link.write("\r")
        self.log("[!] 未见 OpenWrt 提示符，请观察串口输出\n")
        return False

    # ---------------- 动作：自检
    def task_selfcheck(self):
        self.stat("自检中 ...")
        state = self.detect()
        self.log("\n[自检] 当前状态: %s\n" % STATE_LABELS.get(state, state))
        if state in SHELL_STATES:
            if state == "stock":
                for cmd, idle in (("cat /proc/mtd", 1.5),
                                  ("cat /proc/version", 1.2),
                                  ("df -h", 1.5),
                                  ("ifconfig 2>/dev/null | grep -E 'inet addr|^[a-z]'",
                                   1.5),
                                  ("uptime", 1.0)):
                    self.run_cmd(cmd, idle, 20)
                self.log("[提示] 原厂系统(内核 5.4)，原厂固件在 masterbank"
                         "(0xC0000, 30MB)；可直接备份/刷机。\n")
            else:
                for cmd, idle in (("cat /proc/mtd", 1.5),
                                  ("cat /proc/version", 1.2),
                                  ("df -h", 1.5),
                                  ("ip -4 addr show | grep -E 'inet |^[0-9]'", 1.5),
                                  ("iwinfo 2>/dev/null | grep -E 'ESSID|Channel|Encryption'",
                                   2.0),
                                  ("uptime", 1.0)):
                    self.run_cmd(cmd, idle, 20)
                self.log("[提示] OpenWrt 运行中，firmware 区在 0x1EC0000。\n")
        elif state == "uboot":
            for cmd, idle in (("printenv bootcmd", 1.2),
                              ("printenv bootdelay ipaddr serverip netmask", 1.2),
                              ("printenv mtdparts", 1.2)):
                self.run_cmd(cmd, idle, 20)
        elif state == "uboot-login":
            self.log("[!] 设备停在 U-Boot 登录提示（账号/密码）；"
                     "点「进 U-Boot」会自动登录。\n")
        elif state == "autoboot":
            self.log("[!] 正在 autoboot 倒计时；点「进 U-Boot」可立刻打断。\n")
        elif state == "stock-login":
            self.log("[!] 设备停在原厂系统登录提示；"
                     "点「备份原厂固件」会要求填 Web 账号密码。\n")
        elif state == "booting":
            self.log("[!] 内核正在启动，等 20~30 秒后再自检。\n")
        else:
            self.log("[!] 未识别到任何提示符；请确认 TTL 接线(GND/RX/TX，TX/RX 要交叉)、"
                     "波特率 115200、设备已上电。\n")
        self.stat("自检完成")

    # ---------------- 动作：进 U-Boot
    def task_enter_uboot(self):
        self.stat("进入 U-Boot ...")
        if self.enter_uboot(True):
            self.run_cmd("printenv bootcmd", 1.2, 20)
        self.stat("就绪")

    # ---------------- 动作：镜像选择
    def pick_image(self):
        p = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.img_var.get()) or HERE,
            title="选择要刷的 FIT 镜像",
            filetypes=[("FIT 镜像", "*.bin"), ("所有文件", "*.*")])
        if p:
            self.img_var.set(p)

    # ---------------- 动作：刷机
    def ask_flash(self):
        img = self.img_var.get().strip()
        if not img or not os.path.isfile(img):
            messagebox.showwarning("镜像无效", "请先选择存在的镜像文件。")
            return
        if not self._backup_ts:
            # 用过就记住：磁盘上有近期的整机备份就不再啰嗦
            recent = self._find_recent_backup()
            if recent:
                self._backup_dir = recent
                self.log("[备份] 发现已有整机备份：%s\n" % recent)
            else:
                end = 0x1EC0000 + align_up(size, 128 * 1024)
                if not messagebox.askyesno(
                        "还没备份整机",
                        "本次只会写 flash 0x1EC0000 ~ 0x%X（%s），\n"
                        "bootloader / romfile / reservearea(WiFi EEPROM)\n"
                        "都在这个范围之外，不会被覆盖。\n\n"
                        "但新固件会取代原厂固件区，回退需要原厂固件。\n"
                        "建议先点「① 备份全部分区（刷机前必做）」。\n\n"
                        "仍要继续刷机吗？"
                        % (end, human(size)), icon="warning", default="no"):
                    return
        if not self.opt_confirm.get():
            messagebox.showwarning(
                "需先确认",
                "请先勾选「我已确认镜像适用于本机(防刷错变砖)」。\n\n"
                "不同软件版本 / 机型的镜像混刷会直接导致变砖！\n"
                "刷前请务必先备份原厂固件。")
            return
        try:
            addr = int(self.addr_var.get(), 16)
        except ValueError:
            messagebox.showwarning("偏移无效", "目标偏移要写成 0x1ec0000 这种十六进制。")
            return
        size = os.path.getsize(img)
        ram = 0x8C000000 if size > 36 * 1024 * 1024 else RAM_ADDR
        msg = ("即将刷入：\n%s\n\n"
               "大小: %d 字节 (%.1f MB)\n"
               "目标: flash 0x%X ~ 0x%X\n"
               "RAM : 0x%X%s\n\n"
               "会先 tftpboot 到 RAM，再 erase + write + 回读 crc32 校验。\n"
               "bootloader / romfile / reservearea 不会被写。\n\n确定继续？"
               % (img, size, size / 1048576.0, addr,
                  addr + align_up(size, 0x20000), ram,
                  "（镜像较大，已抬高避免解压覆盖）"
                  if ram != RAM_ADDR else ""))
        if not messagebox.askyesno("确认刷机", msg, icon="warning"):
            return
        self.run_task(self.task_flash, False)

    def task_flash(self, load_only=False):
        img = self.img_var.get().strip()
        if not os.path.isfile(img):
            self.log("[X] 镜像不存在: %r\n" % img)
            return
        size = os.path.getsize(img)
        with open(img, "rb") as f:
            head = f.read(8)
        if head[:4] != b"\xd0\x0d\xfe\xed":
            self.log("[X] 不是裸 FIT 镜像（magic 不是 d0 0d fe ed）\n")
            return
        fit = struct.unpack(">I", head[4:8])[0]
        aligned = align_up(size, 0x20000)
        try:
            addr = int(self.addr_var.get(), 16)
        except ValueError:
            addr = DEFAULT_ADDR

        self.log("\n" + "=" * 66 + "\n")
        self.log("image : %s\n" % img)
        try:
            self.log("md5   : %s（拿去和编译输出核对）\n" % md5_file(img))
        except Exception:
            pass
        self.log("size  : %d (0x%X)  FIT 头=0x%X\n" % (size, size, fit))
        self.log("写入  : flash 0x%X ~ 0x%X（%s，含 squashfs rootfs）\n"
                 % (addr, addr + aligned, human(aligned)))
        # 大内核解压后能长到 130MB+，从 0x80208000 会盖住 0x88000000 的原镜像，
        # U-Boot 会报 "new format image overwritten" 然后复位。往高处挪。
        ram = RAM_ADDR
        if size > 36 * 1024 * 1024:
            ram = 0x8C000000
            self.log("[!] 镜像 %s 偏大：解压后可能覆盖 0x%X 处的原镜像\n"
                     "    （U-Boot 会报 new format image overwritten 并复位）\n"
                     "    已自动把 TFTP 加载地址改到 0x%X\n"
                     % (human(size), RAM_ADDR, ram))
        self.log("RAM   : 0x%X\n" % ram)
        self.log("[范围] bootloader(0x0) / romfile(0x80000) / "
                 "reservearea(0x%X) 都在范围外，不会被写\n" % FLASH_SAFE_MAX)

        if not load_only:
            if addr < FLASH_SAFE_MIN:
                self.log("[X] 目标 0x%X 落在原厂 masterbank/bootloader 区，拒绝执行\n" % addr)
                return
            if addr + aligned > FLASH_SAFE_MAX:
                self.log("[X] 目标区间 0x%X 越过 reservearea(0x%X)，拒绝执行\n"
                         % (addr + aligned, FLASH_SAFE_MAX))
                return
            # 原厂布局的 reservearea 比新 DTS 更靠前（约 0xA11A000），
            # 大镜像要额外提醒一次
            if addr + aligned > 0xA11A000:
                self.log("[!] 写入末端 0x%X 已接近原厂 reservearea(约 0xA11A000)，"
                         "请确认镜像大小\n" % (addr + aligned))

        # initramfs 的 FIT 总长等于文件大小；sysupgrade 的 FIT 之后还跟着 rootfs
        is_sysupgrade = fit < size - 0x10000
        if load_only and is_sysupgrade:
            self.log("\n[!] 这是 sysupgrade 镜像：FIT %s（kernel+dtb）+ squashfs %s。\n"
                     "    加载到 RAM 后 **不能 bootm**——rootfs 不在 FIT 里，\n"
                     "    内核会报 \"no rootfs found\" 然后 panic、自动重启回原系统。\n"
                     "    要装它：点「立即刷机」自动 erase+write+校验；\n"
                     "    或者让这次的加载跑完，用给你填好的 flash erase/write 手动写。\n"
                     "    现在继续加载到 RAM ...\n"
                     % (human(fit), human(size - fit)))

        fwdir = os.path.dirname(os.path.abspath(img))
        name = os.path.basename(img)
        # 只有当名字会被串口/U-Boot 搞坏时才复制成短名；正常情况直接用原文件
        need_rename = (not name.isascii()) or (" " in name) or len(name) > 120
        if need_rename:
            dst = os.path.join(fwdir, TFTP_NAME)
            self.log("[TFTP] 文件名含中文/空格或过长，复制为 %s（%s）\n"
                     % (TFTP_NAME, human(size)))
            try:
                shutil.copyfile(img, dst)
            except Exception as e:
                self.log("[X] 复制镜像失败: %r\n" % e)
                return
            got = os.path.getsize(dst)
            if got != size:
                self.log("[X] 副本大小不符 %d ≠ %d，中止\n" % (got, size))
                return
            name = TFTP_NAME
        self.tftp_name = name
        self.log("[TFTP] 直接发送：%s（%s，无需复制）\n" % (name, human(size)))

        self.stat("进入 U-Boot ...")
        if not self.enter_uboot(True):
            self.stat("失败")
            return

        pc_ip = local_ip("192.168.1.1")
        self.log("[网络] 本机 IP: %s\n" % pc_ip)
        if not pc_ip.startswith("192.168.1."):
            self.log("[!] 本机不在 192.168.1.x 网段，U-Boot 很可能 ping 不通\n")
        self.run_cmd("setenv ipaddr 192.168.1.1", 0.8, 20)
        self.run_cmd("setenv netmask 255.255.255.0", 0.8, 20)
        self.run_cmd("setenv serverip %s" % pc_ip, 0.8, 20)
        self.stat("检查网络连通性 ...")
        self.link.clear()
        self.link.write("ping %s\r" % pc_ip)
        self._shell_wait(10)          # 别在 U-Boot 命令行 idle 太久
        if "is alive" in self.link.tail():
            self.log("[OK] PC %s 可达\n" % pc_ip)
        else:
            self.log("[!] ping 没回应（防火墙可能禁 ICMP），继续试 TFTP；\n"
                     "    若 TFTP 也不通，检查网线是否在 LAN 口、PC 是否 192.168.1.x、\n"
                     "    Windows 防火墙是否放行 python 的 UDP 69\n")

        srv = TftpServer(fwdir, port=69, verbose=False).start()
        if not srv.wait_ready(3.0):
            self.log("[X] TFTP 服务启动失败: %s\n"
                     % (srv.err or "端口 69 可能被占用"))
            self.stat("TFTP 失败")
            return
        try:
            ok = False
            for blk in (1468, 1024, 512):
                # 上一次失败后 U-Boot 可能还陷在自己的重试循环里，这时发过去
                # 的命令会被当成 TFTP 的垃圾吃掉，必须先等它回到命令行
                if not self._shell_wait(240):
                    self.log("[X] U-Boot 未回到命令行，无法继续；请重新刷机\n")
                    break
                m = re.search(r"Bytes transferred\s*=\s*(\d+)",
                              self.link.tail())
                if m and int(m.group(1)) == size:
                    self.log("[OK] U-Boot 自身重试后传输成功\n")
                    ok = True
                    break
                self.run_cmd("setenv tftpblocksize %d" % blk, 0.8, 20)
                srv.reset()
                ok = self.tftp_fetch(srv, size, blk, ram)
                if ok:
                    break
                self.log("[!] %d 分块失败，降级重试\n" % blk)
            if not ok:
                self.stat("TFTP 失败")
                return

            out = self.run_cmd("md.b 0x%x 0x4" % ram, 1.2, 20)
            if "d0 0d fe ed" not in out:
                self.log("[X] RAM 首部不是 FIT magic，已中止（未写 flash）\n")
                self.stat("校验失败")
                return

            if load_only:
                self.log("\n[LOAD-ONLY] 镜像已在 RAM 0x%X，未写 flash，"
                         "断电/reset 即丢弃。\n" % ram)
                if is_sysupgrade:
                    self.log(">>> sysupgrade 不能 bootm；手动刷写请依次执行（已填好第一条）：\n"
                             "    flash erase 0x%X 0x%X\n"
                             "    flash write 0x%X 0x%X 0x%X\n"
                             "    或关掉任务后直接点「立即刷机」全自动完成。\n"
                             % (addr, aligned, addr, size, ram))
                    self.q.put(("prefill",
                                "flash erase 0x%X 0x%X" % (addr, aligned)))
                    self.stat("已加载到 RAM（可手动 flash write）")
                else:
                    self.log(">>> 试跑命令已填进下方输入框，直接回车即可 bootm\n")
                    self.log(">>> 想放弃：把输入框改成 reset 回车\n")
                    self.q.put(("prefill", "bootm 0x%X" % ram))
                    self.stat("已加载到 RAM（回车即试跑）")
                self.pbar_set(100)
                return

            if self._aborted():
                self.log("[!] 已取消，未执行擦写\n")
                self.stat("已取消")
                return
            self.stat("擦除 flash ...")
            if not self.send_and_until("flash erase 0x%X 0x%X" % (addr, aligned),
                                       PROMPT_UBOOT, 1200,
                                       label="flash erase 0x%X 0x%X (%s)"
                                             % (addr, aligned, human(aligned))):
                self.stat("擦除超时")
                return

            self.stat("写入 flash ...")
            if not self.send_and_until("flash write 0x%X 0x%X 0x%X" % (addr, size, ram),
                                       PROMPT_UBOOT, 1800,
                                       label="flash write %d bytes" % size):
                self.stat("写入超时")
                return

            self.stat("回读校验 ...")
            # 回读整份镜像再算 crc32：只读 FIT 却按整份算，必然对不上。
            # 缓冲区必须避开 RAM 里正被写入的数据区（ram ~ ram+size）。
            rbuf = 0x90000000
            if ram + size > rbuf:
                rbuf = align_up(ram + size, 0x1000000)
            self.log("\n>>> 回读 %s 到 0x%X 校验\n" % (human(size), rbuf))
            self.link.clear()
            self.link.write("flash read 0x%X 0x%X 0x%X\r" % (addr, size, rbuf))
            if self.link.wait_until([PROMPT_UBOOT], 1200) is None:
                self.log("[!] 回读超时，跳过校验\n")
            else:
                verified = False
                out = self.run_cmd("md.b 0x%X 0x4" % rbuf, 1.0, 20)
                if "d0 0d fe ed" not in out:
                    self.log("[X] 回读首部不是 FIT magic，写入可能失败！\n")
                    self.stat("校验失败")
                    return
                dev_crc = uboot_crc32(
                    self.run_cmd("crc32 0x%X 0x%X" % (rbuf, size), 2.5, 180))
                pc_crc = file_crc32(img)
                if dev_crc:
                    self.log("回读 crc32 = %s / 本地 crc32 = %s\n"
                             % (dev_crc, pc_crc))
                    if dev_crc != pc_crc:
                        self.log("[X] 回读 crc32 不一致，写入失败！\n"
                                 "    可能撞上 NAND 坏块，重来一次（先 erase）\n")
                        self.stat("校验失败")
                        return
                    self.log("[OK] 回读 crc32 与本地镜像一致\n")
                    verified = True
                if not verified:
                    info = self.run_cmd("iminfo 0x%X" % rbuf, 2.5, 90)
                    if "sha1+ OK" in info or "Hash Integrity ... OK" in info:
                        self.log("[OK] iminfo sha1 校验通过\n")
                        verified = True
                if not verified:
                    self.log("[!] 未能自动校验（U-Boot 可能没编 crc32 命令），"
                             "请人工确认上面的回读输出\n")

            if self.opt_bootcmd.get():
                # 读取长度必须覆盖整个 FIT：
                #   sysupgrade 的 FIT 只有 3~4MB，5MB 够；
                #   initramfs 的 FIT 就是整个文件（32~42MB），必须按实际大小读。
                rlen = max(DEFAULT_READ_LEN, align_up(fit, 0x20000))
                cmd = "flash read 0x%X 0x%X 0x%X; bootm 0x%X" % (addr, rlen, ram, ram)
                if rlen > DEFAULT_READ_LEN:
                    self.log("[bootcmd] FIT 有 %s，读取长度改为 0x%X\n"
                             "          （原来固定 0x%X 只够 sysupgrade）\n"
                             % (human(fit), rlen, DEFAULT_READ_LEN))
                self.log("\n>>> 固化 bootcmd\n")
                self.run_cmd('setenv bootcmd "%s"' % cmd, 1.2, 30)
                chk = self.run_cmd("printenv bootcmd", 1.2, 30)
                # 不要拿固定地址比对：大镜像用的是 0x8C000000，不是 0x88000000
                want_bootm = "bootm 0x%X" % ram
                if want_bootm.lower() not in chk.lower() \
                        or "flash read" not in chk.lower():
                    self.log("[X] setenv 未生效（期望 %s），未执行 saveenv\n"
                             % want_bootm)
                else:
                    self.run_cmd("saveenv", 3.0, 300)
                    self.log("[OK] bootcmd 已保存\n")

            if self.opt_boot.get():
                self.stat("从 RAM 启动新系统 ...")
                self.log("\n>>> bootm 0x%X\n" % RAM_ADDR)
                self.link.clear()
                self.link.write("bootm 0x%X\r" % RAM_ADDR)
                if self.wait_openwrt(300):
                    self.run_cmd("cat /proc/mtd", 1.2, 20)
                    self.run_cmd("df -h / /overlay", 1.2, 20)
                    self.log("\n[完成] 刷机成功并已启动\n")
                    self.stat("刷机完成")
                else:
                    self.stat("启动未确认")
            else:
                self.log("\n[完成] 已写入 flash（未启动）。可点「重启设备」。\n")
                self.stat("刷机完成")
        finally:
            srv.stop()

    def tftp_fetch(self, srv, size, blk, ram=None):
        ram = ram or RAM_ADDR
        self.log("\n>>> tftpboot 0x%X %s (blksize=%d)\n"
                 % (ram, self.tftp_name, blk))
        self.link.clear()
        self.link.write("tftpboot 0x%X %s\r" % (ram, self.tftp_name))
        t0 = time.time()
        last_pct = -1
        fail_seen = 0
        self._bulk = True
        try:
            while time.time() - t0 < 2400:
                if self._aborted():
                    self.log("[!] 已取消 TFTP 传输\n")
                    return False
                sent = srv.sent or 0
                pct = min(100, int(sent * 100 / size)) if size else 0
                if pct != last_pct:
                    last_pct = pct
                    sp = sent / 1024.0 / max(time.time() - t0, 0.001)
                    self.stat("TFTP %d%%  %s / %s  %.0f KB/s"
                             % (pct, human(sent), human(size), sp))
                    self.pbar_set(pct)
                tail = self.link.tail()
                m = re.search(r"Bytes transferred\s*=\s*(\d+)", tail)
                if m:
                    got = int(m.group(1))
                    self.link.wait_until([PROMPT_UBOOT], 60)
                    if got != size:
                        self.log("[X] 传输字节数不符: %d ≠ %d\n" % (got, size))
                        return False
                    if sent and sent < size:
                        self.log("[!] 服务端只发出 %d / %d 字节\n"
                                 % (sent, size))
                    self.pbar_set(100)
                    self.log("[OK] tftpboot 完成（%.0fs）\n"
                             % (time.time() - t0))
                    return True
                # 不可恢复的错误
                if ("Load address not aligned" in tail
                        or re.search(r"TFTP error:\s*(?!First block is not block)",
                                     tail)):
                    self.log("[X] TFTP 报错（见上方串口输出）\n")
                    return False
                # U-Boot 自己会重开传输：服务端已按新 RRQ 重新开始，继续等
                n_fail = (tail.count("Retry count exceeded")
                          + tail.count("First block is not block"))
                if n_fail > fail_seen:
                    fail_seen = n_fail
                    if fail_seen >= 3:
                        self.log("[X] U-Boot 连续 %d 次重传失败\n" % fail_seen)
                        return False
                    self.log("[!] U-Boot 第 %d 次重开传输，服务端已跟进，继续等\n"
                             % fail_seen)
                    srv.reset()
                time.sleep(0.12)
        finally:
            self._bulk = False
        self.log("[X] tftpboot 超时\n")
        return False

    # ---------------- 动作：固化 bootcmd
    def task_fix_bootcmd(self):
        self.stat("检查 bootcmd ...")
        if not self.enter_uboot(True):
            return
        out = self.run_cmd("printenv bootcmd", 1.2, 20)
        low = out.lower()
        if "0x1ec0000" in low and "bootm 0x88000000" in low:
            self.log("[OK] bootcmd 已指向 firmware 分区的 OpenWrt，无需改动\n")
            self.stat("已是最新")
            return
        rlen = DEFAULT_READ_LEN
        cmd = "flash read 0x%X 0x%X 0x%X; bootm 0x%X" % (DEFAULT_ADDR, rlen, RAM_ADDR, RAM_ADDR)
        self.log("\n>>> setenv bootcmd \"%s\"\n" % cmd)
        self.run_cmd('setenv bootcmd "%s"' % cmd, 1.2, 30)
        chk = self.run_cmd("printenv bootcmd", 1.2, 30)
        if "bootm 0x88000000" not in chk:
            self.log("[X] setenv 未生效，放弃 saveenv\n")
            return
        self.run_cmd("saveenv", 3.0, 300)
        self.log("[OK] bootcmd 已固化\n")
        self.stat("固化完成")

    # ---------------- 动作：还原原厂
    def ask_restore(self):
        msg = ("把 U-Boot 的 bootcmd 改回原厂值：\n\n"
               "    %s\n\n"
               "原厂固件一直完好在 masterbank(0xC0000, 30MB)，本操作不写任何 flash 数据区，\n"
               "只改 U-Boot 环境变量。执行后会 reset 启动原厂系统。\n\n确定还原？" % STOCK_BOOTCMD)
        if not messagebox.askyesno("确认还原原厂", msg, icon="warning"):
            return
        self.run_task(self.task_restore)

    def task_restore(self):
        self.stat("还原原厂 ...")
        if not self.enter_uboot(True):
            return
        out = self.run_cmd("printenv bootcmd", 1.2, 20)
        self.log("[原 bootcmd] %s\n" % out.strip()[-160:])
        self.run_cmd('setenv bootcmd "%s"' % STOCK_BOOTCMD, 1.2, 30)
        chk = self.run_cmd("printenv bootcmd", 1.2, 30)
        if "imgread 2048" not in chk:
            self.log("[X] setenv 未生效，放弃 saveenv\n")
            return
        self.run_cmd("saveenv", 3.0, 300)
        self.log("\n>>> reset 启动原厂系统\n")
        self.link.clear()
        self.link.write("reset\r")
        self.stat("等待原厂启动 ...")
        if self.link.wait_until([STOCK_MARK, "tc3162ser_startup"], 180):
            self.log("\n[完成] 已回到原厂系统\n")
            self.stat("已还原原厂")
        else:
            self.log("\n[!] 180s 内未见原厂启动特征，请观察串口\n")

    # ---------------- 线程安全的对话框（工作线程 -> 主线程）
    def _place_center(self, win, w, h):
        """把弹窗摆到主窗口正中（主窗没起来就按屏幕居中）。"""
        try:
            win.update_idletasks()
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            if pw < 300 or ph < 300:
                px = py = 0
                pw, ph = win.winfo_screenwidth(), win.winfo_screenheight()
        except Exception:
            px = py = 0
            try:
                pw, ph = win.winfo_screenwidth(), win.winfo_screenheight()
            except Exception:
                pw, ph = 1280, 800
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _modal_input(self, title, initial, label=""):
        """弹出文本编辑框（主线程/工作线程均可安全调用）；返回 None=取消。"""
        box = {"v": None}
        ev = threading.Event()
        holder = {}

        def ui():
            d = tk.Toplevel(self.root)
            holder["d"] = d
            d.title(title)
            d.configure(bg=BG2)
            self._place_center(d, 640, 360)
            d.grab_set()
            if label:
                tk.Label(d, text=label, bg=BG2, fg=FG, justify="left",
                         font=("Microsoft YaHei UI", 9)).pack(
                    anchor="w", padx=10, pady=(10, 4))
            txt = tk.Text(d, bg="#fbfbfb", fg="#1b1b1b", font=("Consolas", 10),
                          bd=0, padx=8, pady=6, height=10)
            txt.pack(fill="both", expand=True, padx=10)
            txt.insert("1.0", initial)

            def ok():
                box["v"] = txt.get("1.0", "end").strip()
                d.destroy()
                ev.set()

            def cancel():
                d.destroy()
                ev.set()

            fr = tk.Frame(d, bg=BG2)
            fr.pack(fill="x", padx=10, pady=8)
            tk.Button(fr, text="保存并执行", command=ok, **BTN).pack(side="right")
            tk.Button(fr, text="取消", command=cancel, **BTN).pack(side="right", padx=(0, 8))
            d.protocol("WM_DELETE_WINDOW", cancel)

        if threading.current_thread() is self._main_thr:
            ui()
            self.root.wait_window(holder["d"])
        else:
            self.root.after(0, ui)
            ev.wait(600)
        return box["v"]

    def _modal_checklist(self, title, items):
        """弹出勾选列表（主线程/工作线程均可安全调用）；items=[(name, mtdN, size)]，返回选中的名字或 None。"""
        box = {"v": None}
        ev = threading.Event()
        holder = {}
        vars_ = []

        def ui():
            d = tk.Toplevel(self.root)
            holder["d"] = d
            d.title(title)
            d.configure(bg=BG2)
            self._place_center(d, 460, 440)
            d.grab_set()
            tk.Label(d, text="选择要备份的分区：", bg=BG2, fg=FG,
                     font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=10,
                                                           pady=(10, 6))
            inner = tk.Frame(d, bg=BG2)
            inner.pack(fill="both", expand=True, padx=10)
            for name, mtdn, size in items:
                v = tk.BooleanVar(value=True)
                vars_.append((v, name))
                tk.Checkbutton(inner, text="%s  (%s, %s)"
                               % (name, "mtd" + mtdn, human(size)),
                               variable=v, bg=BG2, fg=FG,
                               activebackground=BG2, activeforeground=FG,
                               selectcolor="#fbfbfb", anchor="w",
                               font=("Microsoft YaHei UI", 9)).pack(fill="x")

            def ok():
                box["v"] = [n for v, n in vars_ if v.get()]
                d.destroy()
                ev.set()

            def cancel():
                d.destroy()
                ev.set()

            fr = tk.Frame(d, bg=BG2)
            fr.pack(fill="x", padx=10, pady=8)
            tk.Button(fr, text="开始备份", command=ok, **BTN).pack(side="right")
            tk.Button(fr, text="取消", command=cancel, **BTN).pack(side="right", padx=(0, 8))
            d.protocol("WM_DELETE_WINDOW", cancel)

        if threading.current_thread() is self._main_thr:
            ui()
            self.root.wait_window(holder["d"])
        else:
            self.root.after(0, ui)
            ev.wait(600)
        return box["v"]

    # ---------------- 动作：一键备份
    def ask_backup(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        save_dir = filedialog.askdirectory(
            title="选择备份保存目录（PC 本地）", initialdir=HERE)
        if not save_dir:
            return
        self.run_task(self.task_backup, save_dir)

    def _shell_wait(self, timeout=180, idle=0.3, fail=None):
        """等设备回到 shell / U-Boot 提示符。

        命令回显行里提示符后面还跟着命令文本，不会被 _sniff 命中，
        所以这里只会在“命令真的执行完”之后返回。
        fail 给一个错误串（如 "tftp: "），出现就立刻返回 False，不用干等超时。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._aborted():
                return False
            tail = self.link.tail()
            if fail and fail in tail:
                return False
            if self._sniff(tail) in ("openwrt", "stock", "uboot") \
                    and self.link.quiet(idle):
                return True
            time.sleep(0.08)
        return False

    def _dump_mtd_chunked(self, num, name, size, dst):
        """把 /dev/mtdN 分块读进 /tmp、逐块 HTTP 拉回并拼接。

        不再一次性 dd 整个分区：191MB 的 firmware 分区会直接把 tmpfs 撑爆。
        """
        self.stat("备份 %s (%s) ..." % (name, human(size)))
        base = "http://%s:8080" % (self.dev_ip.get().strip() or "192.168.1.1")
        part = dst + ".part"
        chunkname = "chunk.bin"
        done = 0
        self.pbar_set(0)
        try:
            with open(part, "wb") as f:
                while done < size:
                    if self._aborted():
                        raise RuntimeError("已取消")
                    this = min(BACKUP_CHUNK, size - done)
                    mb = this // 1048576 + (1 if this % 1048576 else 0)
                    self.link.clear()
                    self.link.write(
                        "dd if=/dev/mtd%s of=/tmp/bak/%s bs=1048576 "
                        "skip=%d count=%d 2>/dev/null\r"
                        % (num, chunkname, done // 1048576, mb))
                    if not self._shell_wait(180 + this // 32768):
                        raise RuntimeError("设备端 dd 超时（偏移 %d）" % done)
                    self.stat("传输 %s  %s / %s ..."
                              % (name, human(done + this), human(size)))
                    tmp = os.path.join(os.path.dirname(dst) or ".", chunkname)
                    n = http_get(base + "/" + chunkname, tmp, timeout=180)
                    if n != this:
                        raise RuntimeError("分块大小不符 %d ≠ %d（偏移 %d）"
                                           % (n, this, done))
                    with open(tmp, "rb") as g:
                        while True:
                            b = g.read(1 << 20)
                            if not b:
                                break
                            f.write(b)
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    done += n
                    self.pbar_set(done * 100 // max(size, 1))
            os.replace(part, dst)
        except Exception as e:
            self.log("[X] %s 备份失败: %s\n" % (name, e))
            for p in (part, os.path.join(os.path.dirname(dst) or ".", chunkname)):
                try:
                    os.remove(p)
                except Exception:
                    pass
            return False
        self.log("[OK] %s -> %s (%s, 大小校验一致)\n"
                 % (name, dst, human(size)))
        return True

    CRED_FILE = os.path.join(HERE, ".znui_cred")

    def _find_recent_backup(self, days=90):
        """在工具目录下找最近一次的整机备份（有 backup_manifest.txt 的目录）。"""
        best, best_t = "", 0.0
        try:
            for name in os.listdir(HERE):
                p = os.path.join(HERE, name, "backup_manifest.txt")
                if not os.path.isfile(p):
                    continue
                t = os.path.getmtime(p)
                if time.time() - t < days * 86400 and t > best_t:
                    best, best_t = os.path.join(HERE, name), t
        except Exception:
            pass
        return best

    def _load_cred(self):
        """读上次登录成功的原厂串口账号（登录失败会自动作废）。"""
        try:
            with open(self.CRED_FILE, "r", encoding="utf-8") as f:
                lines = [x.rstrip("\n") for x in f.readlines()]
            if len(lines) >= 2 and lines[0].strip():
                return lines[0].strip(), lines[1]
        except Exception:
            pass
        return None

    def _save_cred(self, user, pw):
        try:
            with open(self.CRED_FILE, "w", encoding="utf-8") as f:
                f.write("%s\n%s\n" % (user, pw))
        except Exception as e:
            self.log("[!] 保存账号失败: %r\n" % e)

    def _ensure_stock_login(self):
        """保证进入原厂 shell：先用记住的凭据，失败才弹窗。"""
        creds = self._load_cred()
        if creds:
            self.log("[说明] 用已保存的账号 %s 直接登录 ...\n" % creds[0])
            if self.login_stock(creds[0], creds[1]):
                self.log("[OK] 已进入原厂系统\n")
                return True
            self.log("[!] 保存的账号登录失败（密码可能被改过），请重新输入\n")
            try:
                os.remove(self.CRED_FILE)      # 作废，免得下次又拿错的试
            except Exception:
                pass
        creds = self._modal_credentials(
            "原厂系统登录",
            "设备停在原厂登录提示。\n"
            "点「自动获取」会先用 telnet 读出真正的串口密码；也可以手动填。\n"
            "登录成功后会记住，下次不再询问。")
        if not creds:
            self.log("[取消] 未提供账号密码\n")
            return False
        if not self.login_stock(creds[0], creds[1]):
            return False
        self._save_cred(creds[0], creds[1])
        self.log("[OK] 已进入原厂系统（账号已记住，下次不再询问）\n")
        return True

    def _probe_httpd(self):
        """设备 busybox 有没有 httpd（原厂 v1.00 没有）。"""
        out = self.run_cmd("busybox httpd -p 0.0.0.0:8080 -h /tmp & 2>&1", 1.2, 20)
        low = out.lower()
        return not ("applet not found" in low or "httpd: not found" in low)

    def _dump_mtd_tftp(self, srv, num, name, size, dst, pc_ip):
        """串口只发命令（原厂 TTL 是 root），数据让设备 tftp -p 主动推给 PC。

        原厂 busybox 没有 httpd、/tmp 又要 root 才能写，所以只能这样走：
        dd 落到 /tmp -> tftp 上传 -> 串口等提示符，数据全程走网线。
        """
        self.stat("备份 %s (%s) ..." % (name, human(size)))
        part = dst + ".part"
        tmpdev = "/tmp/bk.bin"
        remote = "blk.bin"
        rpath = os.path.join(srv.root, remote)
        done = 0
        self.pbar_set(0)
        try:
            with open(part, "wb") as f:
                while done < size:
                    if self._aborted():
                        raise RuntimeError("已取消")
                    this = min(BACKUP_CHUNK, size - done)
                    mb = this // 1048576 + (1 if this % 1048576 else 0)
                    self.link.clear()
                    self.link.write(
                        "dd if=/dev/mtd%s of=%s bs=1048576 skip=%d count=%d\r"
                        % (num, tmpdev, done // 1048576, mb))
                    if not self._shell_wait(180):
                        raise RuntimeError("dd 超时（偏移 %d）" % done)
                    try:
                        os.remove(rpath)
                    except Exception:
                        pass
                    self.stat("上传 %s  %s / %s ..."
                              % (name, human(done + this), human(size)))
                    self.link.clear()
                    self.link.write("tftp -p -l %s -r %s %s\r"
                                    % (tmpdev, remote, pc_ip))
                    if not self._shell_wait(300, fail="tftp: "):
                        raise RuntimeError("tftp 上传失败/超时（偏移 %d）" % done)
                    # 文件可能比提示符稍晚落地
                    t1 = time.time()
                    while not os.path.exists(rpath) and time.time() - t1 < 10:
                        time.sleep(0.2)
                    if not os.path.exists(rpath):
                        raise RuntimeError("PC 没收到分块（偏移 %d）" % done)
                    got = os.path.getsize(rpath)
                    if got != this:
                        raise RuntimeError("分块大小不符 %d ≠ %d" % (got, this))
                    with open(rpath, "rb") as g:
                        shutil.copyfileobj(g, f)
                    try:
                        os.remove(rpath)
                    except Exception:
                        pass
                    done += this
                    self.pbar_set(done * 100 // max(size, 1))
            self.run_cmd("rm -f %s" % tmpdev, 1.0, 30)
            os.replace(part, dst)
        except Exception as e:
            self.log("[X] %s 备份失败: %s\n" % (name, e))
            for p in (part, rpath):
                try:
                    os.remove(p)
                except Exception:
                    pass
            return False
        self.log("[OK] %s -> %s (%s, 大小校验一致)\n"
                 % (name, dst, human(size)))
        return True

    def task_backup(self, save_dir):
        self.stat("一键备份 ...")
        state = self.detect()
        if state not in SHELL_STATES:
            self.log("[X] 一键备份需要设备进入系统（OpenWrt / 原厂 shell），"
                     "当前: %s\n" % STATE_LABELS.get(state, state))
            self.stat("失败")
            return

        out = self.run_cmd("cat /proc/mtd", 1.5, 30)
        items = parse_mtd(out)
        if not items:
            self.log("[X] 没读到 /proc/mtd 分区表\n")
            self.stat("失败")
            return
        chosen = self._modal_checklist("选择备份分区", items)
        if not chosen:
            self.log("[取消] 未选择分区\n")
            self.stat("就绪")
            return
        targets = [it for it in items if it[0] in chosen]

        self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.bin", 1.0, 30)
        self.run_cmd("killall httpd 2>/dev/null; "
                     "busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
        ok_all = True
        t0 = time.time()
        try:
            for name, num, size in targets:
                if self._aborted():
                    self.log("[!] 已取消\n")
                    ok_all = False
                    break
                if not self._dump_mtd_chunked(num, name, size,
                                              os.path.join(save_dir, "mtd%s_%s.bin"
                                                           % (num, name))):
                    ok_all = False
        finally:
            self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak", 1.5, 30)

        if ok_all:
            self.log("\n[完成] 全部备份已保存到 %s（耗时 %.0fs）\n"
                     % (save_dir, time.time() - t0))
            self.stat("备份完成")
        else:
            self.stat("备份有失败项")

    # ---------------- 动作：备份全部分区（刷机前必做）
    MTD_CHILD = {"kernel": "firmware", "rootfs": "firmware",
                 "rootfs_data": "firmware"}

    def _top_parts(self, items):
        """去掉 firmware 下切出来的 kernel/rootfs/rootfs_data，避免重复备份。"""
        names = {n for n, _, _ in items}
        out = []
        for name, num, size in items:
            parent = self.MTD_CHILD.get(name)
            if parent and parent in names:
                continue
            out.append((name, num, size))
        return out

    def ask_backup_all(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        save_dir = filedialog.askdirectory(
            title="选择整机备份目录（PC 本地）", initialdir=HERE)
        if not save_dir:
            return
        self.run_task(self.task_backup_all, save_dir)

    def task_backup_all(self, save_dir):
        """把设备上所有 mtd 分区逐块拉回 PC，生成清单 + md5。"""
        state = self.detect()
        self.log("[状态] %s\n" % STATE_LABELS.get(state, state))
        if state == "stock-login":
            self.log("[说明] 原厂系统停在登录提示，先登录再备份。\n")
            if not self._ensure_stock_login():
                self.stat("登录失败")
                return
            state = "stock"
        if state not in SHELL_STATES:
            self.log("[X] 这一步要在设备系统里做。\n"
                     "    设备停在 U-Boot 的话：先点「仅加载到 RAM 验证」→ 回车 bootm，\n"
                     "    等出现 root@OpenWrt 提示符，再回来点备份。\n")
            self.stat("需要先进入系统")
            return

        out = self.run_cmd("cat /proc/mtd", 1.5, 30)
        items = parse_mtd(out)
        if not items:
            self.log("[X] 没读到 /proc/mtd\n")
            self.stat("失败")
            return
        top = self._top_parts(items)
        total = sum(s for _, _, s in top)
        self.log("\n[备份] %d 个分区，合计 %s\n" % (len(top), human(total)))

        os.makedirs(save_dir, exist_ok=True)
        pc_ip = local_ip("192.168.1.1")
        use_http = self._probe_httpd()
        srv_tftp = None
        if use_http:
            self.log("[通道] HTTP：设备 busybox 带 httpd，PC 直接拉取\n")
            self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.bin", 1.0, 30)
            self.run_cmd("killall httpd 2>/dev/null; "
                         "busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
        else:
            self.log("[通道] TFTP：设备没有 httpd，改为设备主动上传到 %s\n"
                     % pc_ip)
            self.run_cmd("rm -f /tmp/bk.bin", 1.0, 20)
            srv_tftp = TftpServer(save_dir, port=69, verbose=False).start()
            if not srv_tftp.wait_ready(3.0):
                self.log("[X] PC 的 TFTP 服务起不来（端口 69 被占用？）: %s\n"
                         % (srv_tftp.err or "超时"))
                self.stat("失败")
                return

        rows = []
        ok_all = True
        t0 = time.time()
        try:
            for name, num, size in top:
                if self._aborted():
                    self.log("[!] 已取消\n")
                    ok_all = False
                    break
                dst = os.path.join(save_dir, "mtd%s_%s.bin" % (num, name))
                if use_http:
                    ok = self._dump_mtd_chunked(num, name, size, dst)
                else:
                    ok = self._dump_mtd_tftp(srv_tftp, num, name, size, dst,
                                             pc_ip)
                if not ok:
                    ok_all = False
                    continue
                md5 = md5_file(dst)
                rows.append((name, num, size, os.path.basename(dst), md5))
                self.log("    md5 %s  %s\n" % (md5, os.path.basename(dst)))
        finally:
            if use_http:
                self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak",
                             1.5, 30)
            else:
                self.run_cmd("rm -f /tmp/bk.bin", 1.0, 20)
                if srv_tftp is not None:
                    srv_tftp.stop()

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        man = os.path.join(save_dir, "backup_manifest.txt")
        with open(man, "w", encoding="utf-8") as f:
            f.write("# ZN-AX173G 整机分区备份  %s\n" % stamp)
            f.write("# 设备: %s   PC: %s\n" % (
                self.dev_ip.get().strip() or "192.168.1.1",
                local_ip("192.168.1.1")))
            f.write("# name\tmtd\tsize\tfile\tmd5\n")
            for name, num, size, fn, md5 in rows:
                f.write("%s\tmtd%s\t%d\t%s\t%s\n" % (name, num, size, fn, md5))
        self.log("\n[清单] %s\n" % man)

        # 再写一份给人看的说明（分区含义 / EEPROM 位置 / 回退方法）
        desc = {
            "bootloader": "U-Boot 引导程序",
            "romfile": "出厂配置（明文，含 TTL/Telnet/Web 账号密码）",
            "kernel": "原厂内核（Linux 5.4.55）",
            "rootfs": "原厂根文件系统 squashfs",
            "tclinux": "原厂完整固件（回退用）",
            "kernel_slave": "备用系统内核头",
            "rootfs_slave": "备用系统 rootfs 头",
            "tclinux_slave": "原厂第二套固件（备份系统）",
            "java": "运营商 Java 业务数据区",
            "yaffs": "运营商数据区（挂载点 /usr/osgi）",
            "reservearea": "保留区，WiFi EEPROM 在 +0x4C000",
            "masterbank": "原厂主固件（OpenWrt 布局下的称呼）",
            "firmware": "OpenWrt 固件区（kernel + rootfs）",
        }
        readme = os.path.join(save_dir, "备份说明.txt")
        try:
            with open(readme, "w", encoding="utf-8-sig") as f:
                f.write("ZN-AX173G 原厂分区备份说明\n")
                f.write("=" * 64 + "\n")
                f.write("备份时间 : %s\n" % stamp)
                f.write("设备 IP  : %s\n"
                        % (self.dev_ip.get().strip() or "192.168.1.1"))
                f.write("PC   IP  : %s\n" % local_ip("192.168.1.1"))
                f.write("备份方式 : 串口(root)发 dd + 设备 tftp -p 上传，"
                        "每块校验大小\n")
                f.write("\n分区清单\n" + "-" * 64 + "\n")
                for name, num, size, fn, md5 in rows:
                    f.write("%-30s %10s  %s\n"
                            % (fn, human(size), desc.get(name, "(未知分区)")))
                f.write("\n重要提示\n" + "-" * 64 + "\n")
                f.write(
                    "1. WiFi EEPROM\n"
                    "   mtd10_reservearea.bin 偏移 0x4C000 处的 4KB 是 MT7916 的\n"
                    "   校准数据（开头是 16 79，即芯片 ID 0x7916）。\n"
                    "   刷 191MB 布局的 OpenWrt 后，reservearea 会挪到 0xDDC0000，\n"
                    "   位置变了但区内偏移仍是 0x4C000，需要把这 4KB 写回，\n"
                    "   否则 WiFi 只能用默认参数（可能功率/频偏不准甚至起不来）：\n"
                    "     dd if=mtd10_reservearea.bin of=/dev/mtdblockX \\\n"
                    "        bs=4096 skip=76 count=1 conv=notrunc\n"
                    "   （X = reservearea 的 mtd 号，76 = 0x4C000 / 4096）\n"
                    "\n"
                    "2. 回退原厂\n"
                    "   mtd4_tclinux.bin 是原厂完整固件。写回 flash 0x1EC0000，\n"
                    "   再把 bootcmd 改回 flash imgread 2048;bootm 即可。\n"
                    "\n"
                    "3. 账号密码\n"
                    "   mtd1_romfile.bin 是明文配置，用记事本搜这些字段：\n"
                    "     ConsoleEntry   console_username / console_passwd  (TTL 串口)\n"
                    "     TelnetEntry    telnet_username  / telnet_passwd   (telnet)\n"
                    "     Entry0         username / web_passwd              (Web 超管)\n"
                    "\n"
                    "4. 校验\n"
                    "   backup_manifest.txt 里逐行记录了文件名 / mtd 号 / 大小 / md5。\n"
                    "   校验命令（Windows）：certutil -hashfile 文件名 MD5\n"
                    "\n"
                    "5. 刷机范围\n"
                    "   刷 56MB 的 squashfs-sysupgrade.bin 时，只写 flash\n"
                    "   0x1EC0000 ~ 0x5520000，只会覆盖原厂 tclinux 主固件区。\n"
                    "   bootloader(0x0) / romfile(0x80000) / masterbank(0xC0000) /\n"
                    "   tclinux_slave / java / yaffs / reservearea 都不会被覆盖。\n")
            self.log("[说明] %s\n" % readme)
        except Exception as e:
            self.log("[!] 写备份说明失败: %r\n" % e)

        if ok_all and len(rows) == len(top):
            self._backup_dir = save_dir
            self._backup_ts = time.time()
            self.log("\n[完成] %d 个分区全部备份并校验通过（%.0fs）\n"
                     ">>> 目录：%s\n" % (len(rows), time.time() - t0, save_dir))
            self.stat("备份完成（可刷机）")
        else:
            self.log("\n[!] 有分区没备份成功（%d/%d），修好之前别刷机\n"
                     % (len(rows), len(top)))
            self.stat("备份未完成")

    # ---------------- 动作：分区查看 / 编辑
    def task_partitions(self):
        self.stat("分区信息 ...")
        state = self.detect()
        self.log("[状态] %s\n" % STATE_LABELS.get(state, state))
        if state in SHELL_STATES:
            self.log("\n===== %s /proc/mtd =====\n"
                     % ("原厂系统" if state == "stock" else "OpenWrt"))
            self.run_cmd("cat /proc/mtd", 1.5, 30)
            self.run_cmd("df -h 2>/dev/null", 1.2, 30)
            self.log("[说明] %s\n[步骤] 下面进 U-Boot 查看/编辑 mtdparts 环境变量\n"
                     % ("OpenWrt 分区由设备树(DTS)固定，改分区要改 DTS 并重编固件。"
                        if state == "openwrt" else
                        "原厂分区表也由 DTS 固定，一般不建议改。"))
            if not self.enter_uboot(True):
                self.stat("失败")
                return
        elif state != "uboot":
            self.log("[X] 当前状态无法进入 U-Boot（%s）；"
                     "请先让设备进入系统或手动打进 ECNT>\n"
                     % STATE_LABELS.get(state, state))
            self.stat("失败")
            return

        self.log("\n===== U-Boot mtdparts =====\n")
        out = self.run_cmd("printenv mtdparts", 1.2, 30)
        m = re.search(r"mtdparts=(.*)", out)
        cur = m.group(1).strip() if m else ""
        if not cur:
            self.log("[!] mtdparts 未定义（当前分区来自 DTS，U-Boot 不干预）\n")
        new = self._modal_input(
            "编辑 U-Boot mtdparts",
            cur,
            "修改分区表环境变量（清空并保存 = 删除该变量，恢复 DTS 默认）。\n"
            "格式示例：mtdparts=nand0:0x80000@0x0(bootloader),0x1ec0000@0x80000(master)\n"
            "警告：写错可能导致系统无法启动，请谨慎修改！")
        if new is None:
            self.log("[取消] 未修改 mtdparts\n")
            self.stat("就绪")
            return
        if new == cur:
            self.log("[=] mtdparts 未变化\n")
            self.stat("就绪")
            return

        if new:
            self.run_cmd('setenv mtdparts "%s"' % new, 1.2, 30)
        else:
            self.run_cmd("setenv mtdparts", 1.2, 30)
        chk = self.run_cmd("printenv mtdparts", 1.2, 30)
        ok = (new in chk) if new else ("mtdparts=" not in chk)
        if not ok:
            self.log("[X] setenv 未生效，放弃 saveenv\n")
            self.stat("失败")
            return
        self.run_cmd("saveenv", 3.0, 300)
        self.log("[OK] mtdparts 已保存\n")
        self.stat("分区已更新")

    # ---------------- 动作：备份原厂固件（masterbank）
    STOCK_MTD_NAME = "masterbank"
    STOCK_ADDR = 0xC0000

    def ask_backup_stock(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        save_dir = filedialog.askdirectory(
            title="选择原厂固件保存目录（PC 本地）", initialdir=HERE)
        if not save_dir:
            return
        self.run_task(self.task_backup_stock, save_dir)

    def task_backup_stock(self, save_dir):
        """备份原厂固件 masterbank(0xC0000, 30MB)。

        - 系统在跑（OpenWrt / 原厂 shell）：走 /proc/mtd 分块备份；
        - 停在原厂登录提示：要 Web 账号密码登录后再备份；
        - 停在 U-Boot：这一层没有可靠的 30MB 回传通道，先启动系统。
        """
        self.stat("备份原厂固件 ...")
        state = self.detect()
        self.log("[状态] %s\n" % STATE_LABELS.get(state, state))

        if state in SHELL_STATES:
            self._backup_stock_mtd(save_dir)
            return
        if state == "stock-login":
            self.log("[说明] 原厂系统停在登录提示，先登录再备份。\n")
            if not self._ensure_stock_login():
                self.stat("登录失败")
                return
            self._backup_stock_mtd(save_dir)
            return
        if state in ("uboot", "uboot-login", "autoboot"):
            self.log("[!] 设备停在 U-Boot，这一层没有把 30MB 数据回传 PC 的通道。\n"
                     "    请点「重启设备」让设备启动进系统，再点本按钮备份。\n")
            self.stat("需要先进系统")
            return
        self.log("[!] 当前状态（%s）无法备份；先让设备正常启动再说。\n"
                 % STATE_LABELS.get(state, state))
        self.stat("失败")

    def _dump_masterbank_ttl(self, save_dir, user, pw):
        """原厂系统下登录并仅备份 masterbank 分区（即原厂固件）。"""
        self.stat("登录原厂系统 ...")
        if not self.login_stock(user, pw):
            self.log("[X] 未能登录原厂系统（超时 / 提示符未识别 / 账号密码错）。\n"
                     "说明：TTL 串口登录用的是原厂 Web 管理账号密码。\n")
            self.stat("登录失败")
            return
        self.log("[OK] 已进入原厂系统控制台\n")
        out = self.run_cmd("cat /proc/mtd", 2.0, 30)
        items = parse_mtd(out)
        if not items:
            self.log("[X] /proc/mtd 为空，原厂系统不支持该命令。\n")
            self.stat("失败")
            return
        # OpenWrt 布局里叫 masterbank，原厂自己的布局里这块叫 tclinux
        want = (self.STOCK_MTD_NAME, "tclinux", "firmware")
        mb = [it for it in items if it[0] in want]
        if not mb:
            self.log("[X] 没找到原厂固件分区（masterbank/tclinux/firmware 都没有）。\n"
                     "    原厂分区有：%s\n"
                     "    建议直接点「① 备份全部分区」更稳。\n"
                     % ", ".join(n for n, _, _ in items))
            self.stat("失败")
            return
        name, num, size = mb[0]
        dev_ip = self.dev_ip.get().strip() or "192.168.1.1"
        self.stat("读取 %s (%s) ..." % (self.STOCK_MTD_NAME, human(size)))
        self.log("[步骤] 设备端读取 /dev/mtd%s (%d 字节)\n" % (num, size))
        ok, fail = self.stock_http_backup([(name, num, size)], save_dir, dev_ip)
        if fail:
            for nm, nnum, sz in fail:
                self.stat("串口备份 %s (%s) ..." % (nm, human(sz)))
                self.log("[步骤] 串口读取 mtd%s (%s)\n" % (nnum, human(sz)))
                data = self.stock_serial_dump(nnum, sz, lambda p: self.pbar_set(p))
                dst = os.path.join(save_dir, "mtd%s_%s.bin" % (nnum, nm))
                if data is None:
                    self.log("[X] 串口备份 %s 失败（可能不支持 dd/base64）\n" % nm)
                    continue
                with open(dst, "wb") as f:
                    f.write(data)
                self.log("[OK] %s -> %s (%s, 串口校验一致)\n"
                         % (nm, dst, human(sz)))
                ok.append((nm, nnum, sz))
        if ok:
            self.log("\n[完成] 已备份原厂固件(masterbank) 到 %s\n" % save_dir)
            self.stat("备份完成")
        else:
            self.stat("备份失败")

    def _backup_stock_mtd(self, save_dir):
        """系统在跑（OpenWrt / 原厂 shell）时，用 /proc/mtd 分块备份 masterbank。"""
        self.stat("备份原厂固件 ...")
        out = self.run_cmd("cat /proc/mtd", 1.5, 30)
        items = parse_mtd(out)
        mb = [it for it in items if it[0] == self.STOCK_MTD_NAME]
        if not mb:
            self.log("[X] /proc/mtd 里没有 %s 分区；"
                     "可改点「备份分区…」手动选。\n" % self.STOCK_MTD_NAME)
            self.stat("失败")
            return
        name, num, size = mb[0]
        fn = "stock_masterbank_0x%X.bin" % self.STOCK_ADDR
        dst = os.path.join(save_dir, fn)
        self.log("[步骤] 读取 %s (mtd%s, %s) -> %s\n"
                 % (name, num, human(size), dst))
        pc_ip = local_ip("192.168.1.1")
        if self._probe_httpd():
            self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.bin", 1.0, 30)
            self.run_cmd("killall httpd 2>/dev/null; "
                         "busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
            try:
                ok = self._dump_mtd_chunked(num, name, size, dst)
            finally:
                self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak",
                             1.5, 30)
        else:
            self.log("[通道] 设备没有 httpd，改用 TFTP 上传\n")
            srv = TftpServer(save_dir, port=69, verbose=False).start()
            if not srv.wait_ready(3.0):
                self.log("[X] TFTP 服务起不来: %s\n" % (srv.err or "超时"))
                self.stat("失败")
                return
            try:
                ok = self._dump_mtd_tftp(srv, num, name, size, dst, pc_ip)
            finally:
                srv.stop()
        if not ok:
            self.stat("备份失败")
            return
        crc = file_crc32(dst)
        with open(dst + ".crc32.txt", "w") as f:
            f.write("%s  %s  size=%d\n" % (crc, fn, size))
        self.log("\n[OK] 原厂固件已备份: %s\n" % dst)
        self.log("[OK] 大小 %s  crc32=%s\n" % (human(size), crc))
        self.log("[提示] 之后可用「还原原厂固件」把它写回 masterbank。\n")
        self.pbar_set(100)
        self.stat("备份完成")

    # ---------------- 动作：从备份还原原厂固件（写 masterbank，高危）
    def ask_restore_stock_fw(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        img = filedialog.askopenfilename(
            title="选择原厂固件备份文件（stock_masterbank_*.bin）",
            initialdir=HERE, filetypes=[("bin", "*.bin"), ("所有文件", "*.*")])
        if not img:
            return
        size = os.path.getsize(img)
        if size != 0x1E00000:
            if not messagebox.askyesno(
                    "大小异常",
                    "文件大小 %d 字节，与 masterbank(%d) 不一致，仍要继续吗？\n"
                    "（写错大小可能损坏 flash）" % (size, 0x1E00000),
                    icon="warning"):
                return
        if not messagebox.askyesno(
                "高危操作确认",
                "即将把以下文件写入 flash 原厂固件区（masterbank 0xC0000）：\n\n"
                "%s\n大小: %d 字节\n\n"
                "这是唯一允许写 masterbank 的操作；写入中断电可能导致\n"
                "原厂固件损坏（OpenWrt 不受影响）。确定继续？"
                % (img, size), icon="warning"):
            return
        self.run_task(self.task_restore_stock_fw, img)

    def task_restore_stock_fw(self, img):
        self.stat("还原原厂固件 ...")
        size = os.path.getsize(img)
        aligned = align_up(size, 0x20000)
        addr = self.STOCK_ADDR
        if addr + aligned > 0x1EC0000:
            self.log("[X] 文件超出 masterbank 区域，拒绝执行\n")
            self.stat("失败")
            return
        pc_crc = file_crc32(img)
        self.log("\n" + "=" * 66 + "\n")
        self.log("[高危] %s (%s) -> flash 0x%X\n" % (img, human(size), addr))
        self.log("本地 crc32 = %s\n" % pc_crc)

        fwdir = os.path.dirname(os.path.abspath(img))
        dst = os.path.join(fwdir, TFTP_NAME)
        if os.path.abspath(dst) != os.path.abspath(img):
            shutil.copyfile(img, dst)
        self.tftp_name = TFTP_NAME

        self.stat("进入 U-Boot ...")
        if not self.enter_uboot(True):
            self.stat("失败")
            return
        pc_ip = local_ip("192.168.1.1")
        self.run_cmd("setenv ipaddr 192.168.1.1", 0.8, 20)
        self.run_cmd("setenv netmask 255.255.255.0", 0.8, 20)
        self.run_cmd("setenv serverip %s" % pc_ip, 0.8, 20)
        self.link.clear()
        self.link.write("ping %s\r" % pc_ip)
        self._shell_wait(30)
        if "is alive" not in self.link.tail():
            self.log("[!] ping 没回应（防火墙可能禁 ICMP），继续试 TFTP\n")

        srv = TftpServer(fwdir, port=69, verbose=False).start()
        if not srv.wait_ready(3.0):
            self.log("[X] TFTP 服务启动失败: %s\n"
                     % (srv.err or "端口 69 可能被占用"))
            self.stat("TFTP 失败")
            return
        try:
            ok = False
            for blk in (1468, 1024, 512):
                if not self._shell_wait(240):
                    self.log("[X] U-Boot 未回到命令行，无法继续\n")
                    break
                m = re.search(r"Bytes transferred\s*=\s*(\d+)",
                              self.link.tail())
                if m and int(m.group(1)) == size:
                    self.log("[OK] U-Boot 自身重试后传输成功\n")
                    ok = True
                    break
                self.run_cmd("setenv tftpblocksize %d" % blk, 0.8, 20)
                srv.reset()
                ok = self.tftp_fetch(srv, size, blk)
                if ok:
                    break
                self.log("[!] %d 分块失败，降级重试\n" % blk)
            if not ok:
                self.stat("TFTP 失败")
                return
            dev_crc = uboot_crc32(
                self.run_cmd("crc32 0x%X 0x%X" % (RAM_ADDR, size), 2.5, 120))
            self.log("RAM crc32 = %s\n" % (dev_crc or "?"))
            if dev_crc and dev_crc != pc_crc:
                self.log("[X] RAM crc32 与 PC 不一致，中止（未写 flash）\n")
                self.stat("校验失败")
                return
            if self._aborted():
                self.log("[!] 已取消，未执行擦写\n")
                self.stat("已取消")
                return
            self.stat("擦除原厂固件区 ...")
            if not self.send_and_until(
                    "flash erase 0x%X 0x%X" % (addr, aligned),
                    PROMPT_UBOOT, 1500,
                    label="flash erase masterbank (%s)" % human(aligned)):
                self.stat("擦除超时")
                return
            self.stat("写入原厂固件 ...")
            if not self.send_and_until(
                    "flash write 0x%X 0x%X 0x%X" % (addr, size, RAM_ADDR),
                    PROMPT_UBOOT, 1800,
                    label="flash write %d bytes" % size):
                self.stat("写入超时")
                return
            self.stat("回读校验 ...")
            self.link.clear()
            self.link.write("flash read 0x%X 0x%X 0x%X\r"
                            % (addr, size, RBUF_ADDR))
            if self.link.wait_until([PROMPT_UBOOT], 900) is None:
                self.log("[!] 回读超时，跳过校验\n")
            else:
                rb = uboot_crc32(
                    self.run_cmd("crc32 0x%X 0x%X" % (RBUF_ADDR, size),
                                 2.5, 120))
                self.log("回读 crc32 = %s\n" % (rb or "?"))
                if rb and rb == pc_crc:
                    self.log("[OK] 回读 crc32 与 PC 一致，原厂固件还原成功\n")
                elif rb:
                    self.log("[X] 回读 crc32 不一致！请重新还原\n")
                    self.stat("校验失败")
                    return
            self.log("\n>>> reset 启动原厂系统\n")
            self.link.write("reset\r")
            self.stat("等待原厂启动 ...")
            if self.link.wait_until([STOCK_MARK, "tc3162ser_startup"], 200):
                self.log("\n[完成] 原厂固件已还原并启动\n")
                self.stat("还原完成")
            else:
                self.log("[!] 未见原厂启动特征，请观察串口\n")
                self.stat("启动未确认")
        finally:
            srv.stop()

    # ---------------- 动作：重启
    def ask_reboot(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        self.run_task(self.task_reboot)

    def task_reboot(self):
        state = self.detect()
        self.log("\n[状态] %s\n" % STATE_LABELS.get(state, state))
        if state == "stock-login":
            self.log("[X] 设备停在登录提示，串口发的 reboot 会被当成账号；"
                     "请断电重启。\n")
            self.stat("未执行")
            return
        cmd = "reset" if state in ("uboot", "uboot-login") else "reboot"
        self.log(">>> %s\n" % cmd)
        self.link.clear()
        self.link.write(cmd + "\r")
        self.stat("重启中 ...")
        time.sleep(3)
        self.stat("就绪")

    # ---------------- 动作：原厂系统(TTL 登录)备份
    STOCK_START = "__BK_START__"
    STOCK_END = "__BK_END__"

    # 常见原厂 Web / 维护账号，界面里可以下拉也可以直接改
    WEB_USERS = ("admin", "telecomadmin", "CMCCAdmin", "root", "user")
    # 实测 ZN-AX173G 移动版：ConsoleEntry=admin/1234，TelnetEntry=admin/aDm8H%MdA，
    # Web 超管=CMCCAdmin/（各机不同，可用自动获取或看背面标签）
    WEB_PASSWORDS = ("1234", "nE7jA%5m", "aDm8H%MdA", "admin", "Cmcc10086#",
                     "admintelecom", "Cmcc10086")

    def gateway_cred(self, dev_ip=None):
        """请求 /cgi-bin/getGateWay.cgi，返回 ((user, pw, telnet), msg)。

        实测该 CGI 多数固件不验认证，直接回：
            Family GateWay<br>admin<br>aDm8H%MdA<br>Yes
        """
        ip = dev_ip or (self.dev_ip.get().strip() or "192.168.1.1")
        url = "http://%s/cgi-bin/getGateWay.cgi" % ip
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                html = r.read().decode("utf-8", "replace")
        except Exception as e:
            return None, "打不开 %s（%s）" % (url, e)
        lines = [x.strip() for x in re.sub(r"<[^>]+>", "\n", html).splitlines()
                 if x.strip()]
        if len(lines) < 3:
            return None, "返回内容看不懂：%r" % lines[:6]
        user, pw = lines[1], lines[2]
        tel = lines[3] if len(lines) > 3 else ""
        if not user or not pw:
            return None, "没解析出账号密码：%r" % lines[:6]
        return (user, pw, tel), "ok"

    def stock_console_cred(self, dev_ip=None):
        """用 telnet 凭据登录原厂系统，从 ctromfile.cfg 里读 ConsoleEntry。

        getGateWay.cgi 只给 TelnetEntry；TTL 串口用的是 ConsoleEntry，
        两者是不同的账号密码，必须进 telnet 里读出来。
        返回 ((user, pw), msg)。
        """
        ip = dev_ip or (self.dev_ip.get().strip() or "192.168.1.1")
        cred, info = self.gateway_cred(ip)
        if not cred:
            return None, info
        tuser, tpass = cred[0], cred[1]
        try:
            import telnetlib
            tn = telnetlib.Telnet(ip, 23, timeout=8)
        except Exception as e:
            return None, "telnet %s:23 连不上（%s）" % (ip, e)
        try:
            tn.read_until(b"login:", 6)
            tn.write(tuser.encode() + b"\r\n")
            tn.read_until(b"assword:", 6)
            tn.write(tpass.encode() + b"\r\n")
            time.sleep(1.5)
            tn.read_very_eager()
            tn.write(b"cat /tmp/ctromfile.cfg | grep -i ConsoleEntry\r\n")
            time.sleep(2.5)
            out = tn.read_very_eager().decode("utf-8", "replace")
        except Exception as e:
            return None, "telnet 读配置失败（%s）" % e
        finally:
            try:
                tn.close()
            except Exception:
                pass
        m = re.search(r'console_username="([^"]*)"\s+console_passwd="([^"]*)"', out)
        if not m:
            return None, "telnet 里没找到 ConsoleEntry（密码可能被改过）"
        return (m.group(1), m.group(2)), "ok"

    def _modal_credentials(self, title, label):
        """原厂登录框：账号下拉可改、密码手填、支持 getGateWay.cgi 自动获取。"""
        box = {"v": None}
        ev = threading.Event()
        holder = {}

        def ui():
            d = tk.Toplevel(self.root)
            holder["d"] = d
            d.title(title)
            d.configure(bg=BG2)
            self._place_center(d, 540, 430)
            d.grab_set()

            tk.Label(d, text=label, bg=BG2, fg=FG, justify="left",
                     wraplength=490, font=(YAHEI, 9)).pack(
                anchor="w", padx=14, pady=(12, 4))
            tk.Label(d, text="TTL 串口用的是 ConsoleEntry（本机型实测 admin / 1234）。\n"
                             "点「自动获取」会先用 telnet 登进去，把真正的串口密码读出来；\n"
                             "读不到就退回手动填（密码在路由器背面标签上）。",
                     bg=BG2, fg=SUB, justify="left", wraplength=490,
                     font=(YAHEI, 8)).pack(anchor="w", padx=14, pady=(0, 10))

            tk.Label(d, text="账号（可下拉，也可直接输入）", bg=BG2, fg=SUB,
                     font=(YAHEI, 9)).pack(anchor="w", padx=14)
            uvar = tk.StringVar(value=self.WEB_USERS[0])
            ttk.Combobox(d, textvariable=uvar,
                         values=list(self.WEB_USERS)).pack(
                fill="x", padx=14, pady=(2, 10))

            tk.Label(d, text="密码（手动输入，或从常用值里选）", bg=BG2,
                     fg=SUB, font=(YAHEI, 9)).pack(anchor="w", padx=14)
            pvar = tk.StringVar(value="1234")
            ttk.Combobox(d, textvariable=pvar,
                         values=list(self.WEB_PASSWORDS)).pack(
                fill="x", padx=14, pady=(2, 8))

            msg = tk.StringVar(value="")
            tk.Label(d, textvariable=msg, bg=BG2, fg=ACC, justify="left",
                     wraplength=490, font=(YAHEI, 8)).pack(
                anchor="w", padx=14, pady=(0, 6))

            def auto_fetch():
                btn_auto.set_enabled(False)
                msg.set("正在请求 getGateWay.cgi ...")
                try:
                    d.update()
                except Exception:
                    pass
                cred, info = self.gateway_cred()
                if not cred:
                    btn_auto.set_enabled(True)
                    msg.set("自动获取失败：%s\n可以先手动试 admin / 1234，"
                            "或看路由器背面标签。" % info)
                    return
                tuser, tpass, tel = cred
                msg.set("telnet 凭据：%s / %s%s\n正在用 telnet 读取 TTL 串口密码 ..."
                        % (tuser, tpass,
                           "（telnet=%s）" % tel if tel else ""))
                try:
                    d.update()
                except Exception:
                    pass
                con, cinfo = self.stock_console_cred()
                btn_auto.set_enabled(True)
                if con:
                    uvar.set(con[0])
                    pvar.set(con[1])
                    msg.set("telnet：%s / %s\nTTL 串口：%s / %s（已填入）"
                            % (tuser, tpass, con[0], con[1]))
                else:
                    msg.set("telnet：%s / %s\nTTL 密码没读到（%s）；"
                            "先试 admin / 1234 或看背面标签。"
                            % (tuser, tpass, cinfo))

            def ok():
                box["v"] = (uvar.get().strip(), pvar.get())
                d.destroy()
                ev.set()

            def cancel():
                d.destroy()
                ev.set()

            fr = tk.Frame(d, bg=BG2)
            fr.pack(fill="x", padx=14, pady=10)
            tk.Button(fr, text="登录并继续", command=ok, **BTN).pack(side="right")
            tk.Button(fr, text="取消", command=cancel, **BTN).pack(
                side="right", padx=(0, 8))
            btn_auto = RoundButton(fr, "自动获取 getGateWay.cgi",
                                   command=auto_fetch, kind="ghost",
                                   height=30, padx=12, bg=BG2,
                                   font=(YAHEI, 8))
            btn_auto.pack(side="left")
            d.protocol("WM_DELETE_WINDOW", cancel)

        if threading.current_thread() is self._main_thr:
            ui()
            self.root.wait_window(holder["d"])
        else:
            self.root.after(0, ui)
            ev.wait(600)
        return box["v"]

    def _shell_ready(self, tail):
        t = tail.strip()
        if t.endswith(">"):          # U-Boot 的 ECNT> 不是 Linux shell
            return False
        return (bool(re.search(r"[#\$]\s*$", t[-60:]))
                or "root@" in tail[-200:])

    def login_stock(self, user, pw, timeout=90):
        """登录原厂 Linux CLI（串口）。

        原厂 console 的真实时序（实测）：
            Please press Enter to activate this console.
            -> 回车 -> tc login: -> 账号 -> Password: -> 密码 -> shell
        """
        if self._sniff() == "stock":
            self.log("[=] 已经在原厂 shell 里，无需登录\n")
            return True
        self.link.clear()
        self.link.touch()
        self.link.write("\r")
        t0 = time.time()
        got_user = got_pass = entered = False
        tries = 0
        while time.time() - t0 < timeout:
            if self._aborted():
                return self._shell_ready(self.link.tail())
            tail = self.link.tail()
            if self._shell_ready(tail):
                return True
            chunk = self.link.take_new()
            if chunk:
                low = chunk.lower()
                if "please press enter" in low and not entered:
                    self.log("\n>>> 激活原厂 console（回车）\n")
                    self.link.write("\r")
                    entered = True
                    time.sleep(0.5)
                elif re.search(r"user\s*-?\s*name|login\s*:", low) and not got_user:
                    self.log("\n>>> 原厂账号: %s\n" % user)
                    self.link.write(user + "\r")
                    got_user = True
                    time.sleep(0.5)
                elif "password" in low and got_user and not got_pass:
                    self.log(">>> 发送密码\n")
                    self.link.write(pw + "\r")
                    got_pass = True
                    time.sleep(0.5)
                if ("incorrect" in low or "denied" in low) and got_pass:
                    tries += 1
                    self.log("[!] 账号或密码错误（第 %d 次）\n" % tries)
                    if tries >= 2:
                        self.log("[!] 原厂串口密码不是通用默认值，"
                                 "请查看路由器背面标签后重试\n")
                        return False
                    got_user = got_pass = entered = False
            time.sleep(0.05)
        return self._shell_ready(self.link.tail())

    def stock_http_backup(self, items, save_dir, dev_ip):
        """设备端 dd 到 /tmp/bak，起 httpd，PC 拉取。返回 (ok, fail) 列表。"""
        self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.bin", 1.0, 30)
        for name, num, size in items:
            self.log("\n>>> dd if=/dev/mtd%s (~%s)\n" % (num, human(size)))
            self.link.clear()
            self.link.write("dd if=/dev/mtd%s bs=64k of=/tmp/bak/mtd%s_%s.bin "
                            "2>/dev/null\r" % (num, num, name))
            if not self._shell_wait(max(300, size // 4096)):
                self.log("[!] dd 等待超时，稍后按文件大小兜底校验\n")
        self.run_cmd("killall httpd 2>/dev/null; "
                     "busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
        ok, fail = [], []
        base = "http://%s:8080" % dev_ip
        for name, num, size in items:
            fn = "mtd%s_%s.bin" % (num, name)
            dst = os.path.join(save_dir, fn)
            self.stat("传输 %s ..." % fn)
            try:
                n = http_get(base + "/" + fn, dst, timeout=300)
            except Exception as e:
                self.log("[X] HTTP 拉取 %s 失败: %r\n" % (fn, e))
                fail.append((name, num, size))
                continue
            if n != size:
                self.log("[X] %s 大小不符: %d ≠ %d\n" % (fn, n, size))
                fail.append((name, num, size))
                continue
            self.log("[OK] %s -> %s (%s)\n" % (name, dst, human(size)))
            ok.append((name, num, size))
        self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak", 1.5, 30)
        return ok, fail

    def stock_serial_dump(self, num, size, prog_cb=None):
        """串口 base64 直传（慢但零网络依赖）。返回 bytes 或 None。"""
        self.log("[!] 串口直传很慢：%s 走 115200 大约要 %.0f 分钟\n"
                 % (human(size), size * 4 / 3 / 7000.0 / 60.0))
        self.link.clear()
        self.link.write(
            "echo %s; dd if=/dev/mtd%s bs=512 2>/dev/null | base64; echo %s\r"
            % (self.STOCK_START, num, self.STOCK_END))
        acc = ""
        t0 = time.time()
        limit = max(300, size * 1.6 / 7000.0)
        while time.time() - t0 < limit:
            if self._aborted():
                return None
            chunk = self.link.take_new()
            if chunk:
                acc += chunk
                if prog_cb:
                    prog_cb(min(99, int(len(acc) * 0.75 * 100 / max(size, 1))))
            # 结束标记至少要比开始标记更靠后——否则命中的是命令回显
            if acc.rfind(self.STOCK_END) > acc.rfind(self.STOCK_START):
                break
            time.sleep(0.05)
        s = acc.rfind(self.STOCK_START)
        e = acc.rfind(self.STOCK_END)
        if s < 0 or e <= s:
            return None
        # 去掉串口回显/换行里混进来的非 base64 字符
        b64 = re.sub(r"[^A-Za-z0-9+/=]", "",
                     acc[s + len(self.STOCK_START):e])
        b64 += "=" * (-len(b64) % 4)
        try:
            import base64
            data = base64.b64decode(b64)
        except Exception:
            return None
        if len(data) != size:
            self.log("[!] 串口直传长度不符: %d ≠ %d\n" % (len(data), size))
            return None
        return data

    def ask_backup_stock_ttl(self):
        if not self.link.is_open:
            messagebox.showwarning("未连接", "请先连接串口。")
            return
        save_dir = filedialog.askdirectory(
            title="选择备份保存目录（PC 本地）", initialdir=HERE)
        if not save_dir:
            return
        self.run_task(self.task_backup_stock_ttl, save_dir)

    def task_backup_stock_ttl(self, save_dir):
        self.stat("登录原厂系统 ...")
        if not self._ensure_stock_login():
            self.log("[X] 未能登录原厂系统（超时 / 提示符未识别 / 账号密码错）。\n"
                     "    串口密码一般印在路由器背面标签上，也可能被运营商改过。\n"
                     "    进不去不影响刷机：「进 U-Boot」→「仅加载到 RAM 验证」→ 回车 bootm，\n"
                     "    在临时系统里用「① 备份全部分区」照样能备份全部 flash。\n")
            self.stat("登录失败")
            return
        self.log("[OK] 已进入原厂系统控制台\n")

        out = self.run_cmd("cat /proc/mtd", 2.0, 30)
        items = parse_mtd(out)
        if not items:
            self.log("[X] /proc/mtd 为空，原厂系统不支持该命令。\n")
            self.stat("失败")
            return
        chosen = self._modal_checklist("选择备份分区（原厂系统）", items)
        if not chosen:
            self.log("[取消] 未选择分区\n")
            self.stat("就绪")
            return
        items = [it for it in items if it[0] in chosen]

        dev_ip = self.dev_ip.get().strip() or "192.168.1.1"
        self.stat("HTTP 备份 ...")
        ok, fail = self.stock_http_backup(items, save_dir, dev_ip)
        if fail:
            self.log("\n[!] %d 个分区 HTTP 失败，改用串口直传（较慢）...\n"
                     % len(fail))
            for name, num, size in fail:
                self.stat("串口备份 %s (%s) ..." % (name, human(size)))
                self.log("[步骤] 串口读取 mtd%s (%s)\n" % (num, human(size)))
                data = self.stock_serial_dump(
                    num, size, lambda p: self.pbar_set(p))
                dst = os.path.join(save_dir, "mtd%s_%s.bin" % (num, name))
                if data is None:
                    self.log("[X] 串口备份 %s 失败（可能不支持 dd/base64）\n" % name)
                    continue
                with open(dst, "wb") as f:
                    f.write(data)
                self.log("[OK] %s -> %s (%s, 串口校验一致)\n"
                         % (name, dst, human(size)))
                ok.append((name, num, size))
        if ok:
            self.log("\n[完成] 已备份 %d 个分区到 %s\n" % (len(ok), save_dir))
            self.stat("备份完成")
        else:
            self.stat("备份失败")

    def show_help(self):
        txt = (
            "【ZN-AX173G 刷机工具台 · 使用说明】\n\n"
            "0) 准备：TTL 接好(GND/RX/TX)，网线接设备 LAN 口；\n"
            "   PC 网卡设 192.168.1.x，临时关闭防火墙（仅影响备份速度）。\n\n"
            "1) 连接：选 COM 口与波特率(115200)，点「连接」。\n\n"
            "2) 看状态：点「设备自检」，会显示具体阶段——\n"
            "   U-Boot 命令行 / U-Boot 登录提示 / autoboot 倒计时 /\n"
            "   OpenWrt 运行中 / 原厂系统运行中 / 原厂登录提示 / 内核启动中。\n\n"
            "3) 【刷机第一步，必做】点「① 备份全部分区（刷机前必做）」\n"
            "   把 bootloader / romfile / masterbank / firmware / reservearea\n"
            "   逐个分区拉到 PC（8MB 分块走 HTTP），每块校验大小 + md5，\n"
            "   最后生成 backup_manifest.txt 清单。备份没成功刷机按钮会二次警告。\n"
            "   设备停在 U-Boot 时：先「仅加载到 RAM 验证」→ 回车 bootm 进临时系统，\n"
            "   在那里面点备份即可（不需要原厂系统密码）。\n"
            "   要进原厂系统时：账号从下拉选（admin/telecomadmin/CMCCAdmin/root/user），\n"
            "   密码手填或从常用值里选；也可点「自动获取 getGateWay.cgi」。\n\n"
            "4) 刷机建议分两步，别用 U-Boot 一口气写几十 MB：\n"
            "   第一步（U-Boot 直写，只写小东西）：\n"
            "      选一个小固件/initramfs → 「立即刷机」。U-Boot 走 TFTP + 裸\n"
            "      flash write，没有坏块重映射，10~35MB 以内比较稳。\n"
            "   第二步（设备内 Web 升级，写大固件）：\n"
            "      小系统启动后，浏览器开 http://192.168.1.1\n"
            "      → 系统 → 备份/升级 → 刷写新固件 → 上传大 sysupgrade。\n"
            "      这条路是设备内 sysupgrade，内核 mtd 驱动 + TCP，几十 MB 没问题。\n\n"
            "   设备已经在跑 OpenWrt？那第一步可以跳过，直接 Web 升级。\n"
            "   只想验证镜像能不能启动：「仅加载到 RAM 验证」→ 回车 bootm。\n\n"
            "5) 只想试跑不想写：点「仅加载到 RAM 验证」，加载完在命令框敲\n"
            "   bootm 0x88000000 就能跑起来；敲 reset 放弃，flash 一个字节没动。\n\n"
            "6) 安全网：只写 firmware 分区，绝不碰原厂主 bank / bootflag；\n"
            "   原厂固件始终保留在主 bank，失败可「切换原厂引导」一键还原。\n\n"
            "7) 任务运行中可点「停止」中止；随时点「退出」或按 Ctrl+Alt+Q 强制退出。\n"
        )
        ev = threading.Event()
        holder = {}

        def ui():
            d = tk.Toplevel(self.root)
            holder["d"] = d
            d.title("使用帮助")
            d.configure(bg=BG2)
            self._place_center(d, 640, 500)
            d.grab_set()
            t = tk.Text(d, bg="#fbfbfb", fg="#1b1b1b", font=("Consolas", 9),
                        bd=0, padx=12, pady=10, wrap="word")
            t.insert("1.0", txt)
            t.configure(state="disabled")
            t.pack(fill="both", expand=True, padx=12, pady=12)
            tk.Button(d, text="知道了",
                      command=lambda: (d.destroy(), ev.set()),
                      **BTN).pack(side="right", padx=12, pady=(0, 12))
            d.protocol("WM_DELETE_WINDOW", lambda: (d.destroy(), ev.set()))

        if threading.current_thread() is self._main_thr:
            ui()
            self.root.wait_window(holder["d"])
        else:
            self.root.after(0, ui)
            ev.wait(600)

    # ---------------- 退出
    def on_close(self):
        self._closing = True
        self._cancel.set()
        # 兜底看门狗：万一关闭链路本身挂住（串口驱动/销毁异常），3 秒后强制退进程
        threading.Thread(target=self._force_exit, daemon=True).start()
        try:
            self.link.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 后台线程可能卡在 ev.wait/串口读，直接终止进程保证窗口必关
        os._exit(0)

    def _force_exit(self):
        time.sleep(3.0)
        os._exit(0)

    def _aborted(self):
        return self._closing or self._cancel.is_set()

    def request_cancel(self):
        self._cancel.set()
        self.log("\n[!] 已请求停止当前任务，正在安全退出该操作...\n")

    def _start_emergency_exit(self):
        try:
            threading.Thread(target=self._emergency_loop, daemon=True).start()
        except Exception:
            pass

    def _emergency_loop(self):
        # 独立于 Tk 主线程的看门狗：Ctrl+Alt+Q 无条件退出，哪怕界面卡死
        try:
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, 0x0002 | 0x0001, 0x51):
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    os._exit(0)
                user32.TranslateMessageW(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass
    app = App(root)

    def _dump():
        with open(os.path.join(LOG_DIR, "_znui_autotest.log"), "w",
                  encoding="utf-8") as f:
            f.write(app.txt.get("1.0", "end"))

    if "--selftest" in sys.argv:
        root.after(1500, app.on_close)
    if "--autotest" in sys.argv:
        def _auto():
            app.toggle_connect()
            app.run_task(app.task_selfcheck)

        root.after(400, _auto)
        root.after(30000, _dump)
        root.after(32000, app.on_close)
    if "--autotest-uboot" in sys.argv:
        def _auto2():
            app.toggle_connect()

            def job():
                if not app.enter_uboot(True):
                    return
                app.run_cmd("printenv bootcmd", 1.2, 20)
                app.log("\n>>> bootm 回系统\n")
                app.link.clear()
                app.link.write("flash read 0x1ec0000 0x500000 0x88000000;"
                               " bootm 0x88000000\r")
                if app.wait_openwrt(240):
                    app.run_cmd("uptime", 1.2, 20)

            app.busy = True
            app._set_enabled(False)
            threading.Thread(
                target=lambda: (job(), app.q.put(("done", None))),
                daemon=True).start()

        root.after(400, _auto2)
        root.after(140000, _dump)
        root.after(143000, app.on_close)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        with open(os.path.join(LOG_DIR, "znui_error.log"), "w",
                  encoding="utf-8") as _f:
            _f.write(traceback.format_exc())
        raise
