# -*- coding: utf-8 -*-
"""ZN-AX173G (EN7523) TTL 工具台 —— 串口终端 + 一键刷机 / 还原 / 固化

用法：
    python znui.py

功能：
    串口终端（实时收发、可手敲命令、命令历史）
    设备自检          查分区表 / 系统 / overlay / 无线
    进 U-Boot         重启设备并在 "Hit any key" 时精确打断
    TFTP 刷机         选镜像 -> tftpboot 到 RAM -> erase -> write -> 回读 sha1 -> bootm
    只加载到 RAM      tftpboot + md.b 校验，不写 flash（零风险试载）
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
import gzip
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


def local_ip(target="192.168.1.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def align_up(x, a):
    return (x + a - 1) // a * a


def human(n):
    return "%.2f MB" % (n / 1048576.0)


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
        self.on_data = on_data

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud):
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
            t = self.tail()
            for m in markers:
                if m in t:
                    return m
            time.sleep(poll)
        return None

    def wait_quiet(self, idle=2.0, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
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
        self.busy = False
        self.btns = []
        self.hist = []
        self.hist_i = 0
        self.tftp_name = TFTP_NAME
        self._at_bottom = True
        self.build_ui()
        self.root.after(60, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI
    def build_ui(self):
        r = self.root
        r.title("ZN-AX173G (EN7523) TTL 工具台")
        r.geometry("1240x800")
        r.configure(bg=BG)
        r.minsize(1080, 700)
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

        # ② 刷写 OpenWrt
        cf = card("刷写 OpenWrt")
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
        grow(cf, "立即刷机（TFTP → 擦写 → 校验）", self.ask_flash,
             "primary", pad=(0, 8), h=36)
        grow(cf, "仅加载到 RAM 验证（不写 flash）",
             lambda: self.run_task(self.task_flash, True), pad=(0, 16))

        # ③ 备份与还原
        cf = card("备份与还原")
        grow(cf, "备份原厂固件（masterbank 30MB → PC）",
             lambda: self.run_task(self.task_backup_stock))
        grow2(cf, "备份分区…", self.ask_backup,
              "分区 / mtdparts", lambda: self.run_task(self.task_partitions))
        grow2(cf, "还原原厂固件（写 masterbank）", self.ask_restore_stock_fw,
              "切换原厂引导", self.ask_restore, "danger", "normal")
        grow(cf, "固化 bootcmd（断电自动进 OpenWrt）",
             lambda: self.run_task(self.task_fix_bootcmd), pad=(0, 16))


        self.refresh_ports()
        self._append("ZN-AX173G TTL 工具台已就绪\n"
                     "1) 选 COM 口点「连接」  2) 「设备自检」看当前状态\n"
                     "3) 刷机：选镜像 → 「立即刷机」，自动重启打断进 U-Boot\n"
                     "安全网：原厂固件完好在 masterbank(0xC0000, 30MB)，\n"
                     "可「备份原厂固件」到 PC；「还原原厂系统」随时引导回原厂。\n\n")

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
        if at_bottom:
            self.txt.see("end")

    def _pump(self):
        out = []
        stat = None
        done = False
        pbar = None
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
            elif kind == "done":
                done = True
        if out:
            self._append("".join(out))
        if stat is not None:
            self.stat_var.set(stat)
        if pbar is not None:
            self.pbar.set(pbar)
            if pbar >= 100:
                self.root.after(1600, self.pbar.reset)
        if done:
            self.busy = False
            self._set_enabled(True)
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
        self._set_enabled(False)

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
    def detect(self):
        """返回 'uboot' / 'openwrt' / 'unknown'。"""
        self.link.clear()
        self.link.write("\r")
        m = self.link.wait_until([PROMPT_UBOOT, PROMPT_OWRT], 4.0)
        if m == PROMPT_UBOOT:
            return "uboot"
        if m == PROMPT_OWRT:
            return "openwrt"
        return "unknown"

    def enter_uboot(self, reboot_first=True, window=200):
        """进 U-Boot；reboot_first=True 会先重启（设备在 OpenWrt 时用）。"""
        state = self.detect()
        self.log("[状态] 当前: %s\n" % state)
        if state == "uboot":
            return True
        if state == "openwrt" and not reboot_first:
            self.log("[X] 设备正在 OpenWrt 运行，请勾选/使用「重启并打断」方式\n")
            return False
        if state == "openwrt":
            self.log("[步骤] 重启设备，准备在 autoboot 处打断\n")
            self.link.clear()
            self.link.write("reboot\r")
            time.sleep(1.0)
        else:
            self.log("[!] 未识别当前状态，直接尝试抓 autoboot\n")

        self.stat("等待 U-Boot ...")
        aborted = user_sent = pass_sent = False
        t0 = time.time()
        while time.time() - t0 < window:
            chunk = self.link.take_new()
            if chunk:
                if ("Hit any key" in chunk or "stop autoboot" in chunk) and not aborted:
                    self.link.write("x")
                    aborted = True
                    self.log("\n[!] 已打断 autoboot\n")
                if "UserName" in chunk and not user_sent:
                    time.sleep(0.05)
                    self.link.write(UBOOT_USER + "\r")
                    user_sent = True
                if "Password" in chunk and not pass_sent:
                    time.sleep(0.05)
                    self.link.write(UBOOT_PASS + "\r")
                    pass_sent = True
            if PROMPT_UBOOT in self.link.tail() and self.link.quiet(0.8):
                self.log("[OK] 已进入 U-Boot\n")
                return True
            time.sleep(0.02)
        self.log("[X] 未能进入 U-Boot（%.0fs）\n" % window)
        return False

    def wait_openwrt(self, timeout=240):
        if self.link.wait_until(["Starting kernel"], 150) is None:
            self.log("[X] 没看到 Starting kernel，启动可能失败\n")
            return False
        self.stat("等待 OpenWrt 用户态 ...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(2.0)
            self.link.write("\r")
            if PROMPT_OWRT in self.link.tail():
                self.log("\n[OK] OpenWrt 已启动（%.0fs）\n" % (time.time() - t0))
                return True
        self.log("[!] 未见 OpenWrt 提示符，请观察串口输出\n")
        return False

    # ---------------- 动作：自检
    def task_selfcheck(self):
        self.stat("自检中 ...")
        state = self.detect()
        self.log("\n[自检] 当前状态: %s\n" % state)
        if state == "openwrt":
            for cmd, idle in (("cat /proc/mtd", 1.5),
                              ("df -h", 1.5),
                              ("ip -4 addr show | grep -E 'inet |^[0-9]'", 1.5),
                              ("iwinfo 2>/dev/null | grep -E 'ESSID|Channel|Encryption'", 2.0),
                              ("uptime", 1.0)):
                self.run_cmd(cmd, idle, 20)
        elif state == "uboot":
            for cmd, idle in (("printenv bootcmd", 1.2),
                              ("printenv bootdelay ipaddr serverip netmask", 1.2)):
                self.run_cmd(cmd, idle, 20)
        else:
            self.log("[!] 既不是 U-Boot 也不是 OpenWrt 提示符；"
                     "请确认串口线、波特率，或按一下回车。\n")
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
        try:
            addr = int(self.addr_var.get(), 16)
        except ValueError:
            messagebox.showwarning("偏移无效", "目标偏移要写成 0x1ec0000 这种十六进制。")
            return
        size = os.path.getsize(img)
        msg = ("即将刷入：\n%s\n\n"
               "大小: %d 字节 (%.1f MB)\n"
               "目标: flash 0x%X ~ 0x%X\n\n"
               "会先 tftpboot 到 RAM(0x%X)，再 erase + write + 回读 sha1 校验。\n"
               "masterbank(原厂 30MB) 与 reservearea 不会被写。\n\n确定继续？"
               % (img, size, size / 1048576.0, addr, addr + align_up(size, 0x20000), RAM_ADDR))
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
        self.log("size  : %d (0x%X)  FIT=%d (0x%X)  128K 对齐=0x%X\n"
                 % (size, size, fit, fit, aligned))
        self.log("target: flash 0x%X -> RAM 0x%X\n" % (addr, RAM_ADDR))

        if not load_only:
            if addr < FLASH_SAFE_MIN:
                self.log("[X] 目标 0x%X 落在原厂 masterbank/bootloader 区，拒绝执行\n" % addr)
                return
            if addr + aligned > FLASH_SAFE_MAX:
                self.log("[X] 目标区间 0x%X 越过 reservearea(0x%X)，拒绝执行\n"
                         % (addr + aligned, FLASH_SAFE_MAX))
                return

        fwdir = os.path.dirname(os.path.abspath(img))
        dst = os.path.join(fwdir, TFTP_NAME)
        if os.path.abspath(dst) != os.path.abspath(img):
            try:
                self.log("准备 TFTP 目录: 复制到 %s\n" % TFTP_NAME)
                shutil.copyfile(img, dst)
            except Exception as e:
                self.log("[X] 复制镜像失败: %r\n" % e)
                return
        self.tftp_name = TFTP_NAME

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
        out = self.run_cmd("ping %s" % pc_ip, 2.5, 40)
        if "is alive" not in out:
            self.log("[X] ping 不通：检查网线是否接在 LAN 口、PC 是否 192.168.1.x、"
                     "Windows 防火墙是否放行 python 的 UDP 69\n")
            self.stat("ping 失败")
            return

        srv = TftpServer(fwdir, port=69, verbose=False).start()
        time.sleep(0.4)
        if srv.err:
            self.log("[X] TFTP 服务启动失败: %s\n" % srv.err)
            self.stat("TFTP 失败")
            return
        try:
            ok = False
            for blk in (1468, 512):
                self.run_cmd("setenv tftpblocksize %d" % blk, 0.8, 20)
                ok = self.tftp_fetch(srv, size, blk)
                if ok:
                    break
                self.log("[!] %d 分块失败，降级重试\n" % blk)
            if not ok:
                self.stat("TFTP 失败")
                return

            out = self.run_cmd("md.b 0x%x 0x4" % RAM_ADDR, 1.2, 20)
            if "d0 0d fe ed" not in out:
                self.log("[!] RAM 首部不是 FIT magic，谨慎继续\n")

            if load_only:
                self.log("\n[LOAD-ONLY] 已加载进 RAM，未写 flash。"
                         "断电/reset 即丢弃。\n")
                self.stat("已加载到 RAM")
                return

            self.stat("擦除 flash ...")
            if not self.send_and_until("flash erase 0x%X 0x%X" % (addr, aligned),
                                       PROMPT_UBOOT, 1200,
                                       label="flash erase 0x%X 0x%X (%s)"
                                             % (addr, aligned, human(aligned))):
                self.stat("擦除超时")
                return

            self.stat("写入 flash ...")
            if not self.send_and_until("flash write 0x%X 0x%X 0x%X" % (addr, size, RAM_ADDR),
                                       PROMPT_UBOOT, 1800,
                                       label="flash write %d bytes" % size):
                self.stat("写入超时")
                return

            self.stat("回读校验 ...")
            self.log("\n>>> 回读 FIT(%d 字节) 到 0x%X 做 sha1 校验\n" % (fit, RBUF_ADDR))
            self.link.clear()
            self.link.write("flash read 0x%X 0x%X 0x%X\r" % (addr, fit, RBUF_ADDR))
            if self.link.wait_until([PROMPT_UBOOT], 900) is None:
                self.log("[!] 回读超时，跳过校验\n")
            else:
                out = self.run_cmd("md.b 0x%X 0x4" % RBUF_ADDR, 1.0, 20)
                if "d0 0d fe ed" not in out:
                    self.log("[X] 回读首部不是 FIT magic，写入可能失败！\n")
                    self.stat("校验失败")
                    return
                info = self.run_cmd("iminfo 0x%X" % RBUF_ADDR, 2.5, 90)
                if "sha1+ OK" in info or "Hash Integrity ... OK" in info:
                    self.log("[OK] 回读 sha1 校验通过\n")
                else:
                    self.log("[!] iminfo 未见 sha1 结果，请人工确认上面输出\n")

            if self.opt_bootcmd.get():
                rlen = DEFAULT_READ_LEN if addr == DEFAULT_ADDR else align_up(fit, 0x20000)
                cmd = "flash read 0x%X 0x%X 0x%X; bootm 0x%X" % (addr, rlen, RAM_ADDR, RAM_ADDR)
                self.log("\n>>> 固化 bootcmd\n")
                self.run_cmd('setenv bootcmd "%s"' % cmd, 1.2, 30)
                chk = self.run_cmd("printenv bootcmd", 1.2, 30)
                if "bootm 0x%X" % RAM_ADDR not in chk:
                    self.log("[X] setenv 未生效，未执行 saveenv\n")
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

    def tftp_fetch(self, srv, size, blk):
        self.log("\n>>> tftpboot 0x%X %s (blksize=%d)\n" % (RAM_ADDR, self.tftp_name, blk))
        self.link.clear()
        self.link.write("tftpboot 0x%X %s\r" % (RAM_ADDR, self.tftp_name))
        t0 = time.time()
        last_pct = -1
        while time.time() - t0 < 2400:
            sent = srv.sent or 0
            pct = int(sent * 100 / size) if size else 0
            if pct != last_pct:
                last_pct = pct
                sp = sent / 1024.0 / max(time.time() - t0, 0.001)
                self.stat("TFTP %d%%  %s / %s  %.0f KB/s"
                         % (pct, human(sent), human(size), sp))
                self.pbar_set(pct)
            tail = self.link.tail()
            if "Bytes transferred" in tail:
                self.link.wait_until([PROMPT_UBOOT], 60)
                if sent and sent != size:
                    self.log("[!] 服务端发出 %d / %d 字节\n" % (sent, size))
                self.log("[OK] tftpboot 完成（%.0fs）\n" % (time.time() - t0))
                return True
            if ("TFTP error" in tail or "Retry count exceeded" in tail
                    or "Load address not aligned" in tail):
                self.log("[X] TFTP 报错（见上方串口输出）\n")
                return False
            time.sleep(0.15)
        self.log("[X] tftpboot 超时\n")
        return False

    # ---------------- 动作：固化 bootcmd
    def task_fix_bootcmd(self):
        self.stat("检查 bootcmd ...")
        if not self.enter_uboot(True):
            return
        out = self.run_cmd("printenv bootcmd", 1.2, 20)
        if "0x1ec0000" in out and "bootm 0x88000000" in out:
            self.log("[OK] bootcmd 已指向 slave bank 的 OpenWrt，无需改动\n")
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
    def _modal_input(self, title, initial, label=""):
        """从工作线程弹出文本编辑框；返回 None=取消。"""
        box = {"v": None}
        ev = threading.Event()

        def ui():
            d = tk.Toplevel(self.root)
            d.title(title)
            d.geometry("640x360")
            d.configure(bg=BG2)
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

        self.root.after(0, ui)
        ev.wait(600)
        return box["v"]

    def _modal_checklist(self, title, items):
        """从工作线程弹出勾选列表；items=[(name, mtdN, size)]，返回选中的名字或 None。"""
        box = {"v": None}
        ev = threading.Event()
        vars_ = []

        def ui():
            d = tk.Toplevel(self.root)
            d.title(title)
            d.geometry("430x430")
            d.configure(bg=BG2)
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

    def task_backup(self, save_dir):
        self.stat("一键备份 ...")
        if self.detect() != "openwrt":
            self.log("[X] 一键备份需要设备在 OpenWrt 运行"
                     "（当前不在 OpenWrt 提示符）\n")
            self.stat("失败")
            return

        out = self.run_cmd("cat /proc/mtd", 1.5, 30)
        parts = re.findall(r'mtd(\d+):\s+([0-9a-fA-F]+)\s+"([^"]+)"', out)
        if not parts:
            self.log("[X] 没读到 /proc/mtd 分区表\n")
            self.stat("失败")
            return
        items = [(name, num, int(szh, 16)) for num, szh, name in parts]
        chosen = self._modal_checklist("选择备份分区", items)
        if not chosen:
            self.log("[取消] 未选择分区\n")
            self.stat("就绪")
            return

        self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.gz", 1.0, 30)
        files = []
        t_start = time.time()
        for name, num, size in items:
            if name not in chosen:
                continue
            fn = "mtd%s_%s.bin.gz" % (num, name)
            files.append((fn, name, size))
            self.stat("备份 %s (%s) ..." % (name, human(size)))
            self.run_cmd(
                "dd if=/dev/mtd%s bs=64k 2>/dev/null | gzip > /tmp/bak/%s"
                % (num, fn),
                idle=4.0, timeout=max(180, size // 4096))
        self.log("[OK] 设备端打包完成（%.0fs），启动 HTTP 传输\n"
                 % (time.time() - t_start))

        self.run_cmd("busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
        base = "http://192.168.1.1:8080"
        ok_all = True
        for fn, name, size in files:
            self.stat("传输 %s ..." % fn)
            dst = os.path.join(save_dir, fn)
            try:
                urllib.request.urlretrieve(base + "/" + fn, dst)
            except Exception as e:
                self.log("[X] 下载 %s 失败: %r\n" % (fn, e))
                ok_all = False
                continue
            try:
                n = 0
                with gzip.open(dst, "rb") as g:
                    while True:
                        b = g.read(1 << 20)
                        if not b:
                            break
                        n += len(b)
                if n != size:
                    self.log("[!] %s 解压后 %d 字节 ≠ 分区 %d 字节\n"
                             % (fn, n, size))
                    ok_all = False
                else:
                    self.log("[OK] %s -> %s  (%s, 校验一致)\n"
                             % (name, dst, human(size)))
            except Exception as e:
                self.log("[!] %s gzip 校验失败: %r\n" % (fn, e))
                ok_all = False

        self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak", 1.5, 30)
        if ok_all:
            self.log("\n[完成] 全部备份已保存到 %s\n" % save_dir)
            self.stat("备份完成")
        else:
            self.stat("备份有失败项")

    # ---------------- 动作：分区查看 / 编辑
    def task_partitions(self):
        self.stat("分区信息 ...")
        state = self.detect()
        if state == "openwrt":
            self.log("\n===== OpenWrt /proc/mtd =====\n")
            self.run_cmd("cat /proc/mtd", 1.5, 30)
            self.run_cmd("df -h / /overlay 2>/dev/null", 1.2, 30)
            self.log("[说明] OpenWrt 的分区由设备树(DTS)固定："
                     "改分区要改 DTS 并重编固件。\n"
                     "[步骤] 下面进 U-Boot 查看/编辑 mtdparts 环境变量\n")
            if not self.enter_uboot(True):
                self.stat("失败")
                return
        elif state != "uboot":
            self.log("[X] 未识别设备状态（%s），请先连接/按回车\n" % state)
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
        """备份原厂固件 masterbank(0xC0000, 30MB)：未压缩 .bin + crc32 sidecar。"""
        self.stat("备份原厂固件 ...")
        if self.detect() != "openwrt":
            self.log("[X] 备份原厂固件需要设备在 OpenWrt 运行"
                     "（若当前是原厂系统，请先进 OpenWrt 再备份）\n")
            self.stat("失败")
            return
        out = self.run_cmd("cat /proc/mtd", 1.5, 30)
        m = re.search(r'mtd(\d+):\s+([0-9a-fA-F]+)\s+"%s"' % self.STOCK_MTD_NAME,
                      out)
        if not m:
            self.log("[X] /proc/mtd 里没有 %s 分区\n" % self.STOCK_MTD_NAME)
            self.stat("失败")
            return
        num, szh = m.group(1), int(m.group(2), 16)
        fn = "stock_masterbank_0x%X.bin" % self.STOCK_ADDR
        dst = os.path.join(save_dir, fn)
        self.stat("读取 %s (%s) ..." % (self.STOCK_MTD_NAME, human(szh)))
        self.log("[步骤] 设备端读取 /dev/mtd%s (%d 字节)\n" % (num, szh))
        self.run_cmd("mkdir -p /tmp/bak && rm -f /tmp/bak/*.bin", 1.0, 30)
        self.run_cmd("dd if=/dev/mtd%s bs=64k of=/tmp/bak/stock.bin 2>/dev/null"
                     % num, idle=5.0, timeout=max(300, szh // 2048))
        crc_out = self.run_cmd("crc32 /tmp/bak/stock.bin 2>/dev/null || "
                               "busybox crc32 /tmp/bak/stock.bin 2>/dev/null",
                               idle=3.0, timeout=180)
        mdev = re.findall(r"([0-9a-fA-F]{8})", crc_out.split("crc32")[-1])
        dev_crc = mdev[-1].lower() if mdev else ""

        self.run_cmd("busybox httpd -p 0.0.0.0:8080 -h /tmp/bak &", 1.2, 30)
        self.stat("传输原厂固件 ...")
        try:
            urllib.request.urlretrieve("http://192.168.1.1:8080/stock.bin", dst)
        except Exception as e:
            self.log("[X] 下载失败: %r\n" % e)
            self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak", 1.0, 30)
            self.stat("失败")
            return
        self.run_cmd("killall httpd 2>/dev/null; rm -rf /tmp/bak", 1.0, 30)

        size = os.path.getsize(dst)
        if size != szh:
            self.log("[X] 大小不符: %d ≠ %d\n" % (size, szh))
            self.stat("校验失败")
            return
        import zlib
        crc = 0
        with open(dst, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                crc = zlib.crc32(b, crc)
        pc_crc = "%08x" % (crc & 0xFFFFFFFF)
        if dev_crc and pc_crc != dev_crc:
            self.log("[!] 设备 crc32=%s 与 PC crc32=%s 不一致，请重试\n"
                     % (dev_crc, pc_crc))
            self.stat("校验失败")
            return
        with open(dst + ".crc32.txt", "w") as f:
            f.write("%s  %s  size=%d\n" % (pc_crc, fn, size))
        self.log("\n[OK] 原厂固件已备份: %s\n" % dst)
        self.log("[OK] 大小 %s  crc32=%s%s\n" % (human(size), pc_crc,
                 ("（与设备端一致）" if dev_crc else "")))
        self.log("[提示] 之后可用「从备份还原原厂固件」把它写回 flash\n")
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
        import zlib
        crc = 0
        with open(img, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                crc = zlib.crc32(b, crc)
        pc_crc = "%08x" % (crc & 0xFFFFFFFF)
        self.log("\n" + "=" * 66 + "\n")
        self.log("[高危] %s (%s) -> flash 0x%X\n" % (img, human(size), addr))
        self.log("PC crc32 = %s\n" % pc_crc)

        fwdir = os.path.dirname(os.path.abspath(img))
        dst = os.path.join(fwdir, TFTP_NAME)
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
        out = self.run_cmd("ping %s" % pc_ip, 2.5, 40)
        if "is alive" not in out:
            self.log("[X] ping 不通，检查网线/防火墙\n")
            self.stat("ping 失败")
            return

        srv = TftpServer(fwdir, port=69, verbose=False).start()
        time.sleep(0.4)
        try:
            ok = False
            for blk in (1468, 512):
                self.run_cmd("setenv tftpblocksize %d" % blk, 0.8, 20)
                ok = self.tftp_fetch(srv, size, blk)
                if ok:
                    break
            if not ok:
                self.stat("TFTP 失败")
                return
            out = self.run_cmd("crc32 0x%X 0x%X" % (RAM_ADDR, size), 2.5, 60)
            m = re.search(r"=\s+([0-9a-fA-F]{8})", out)
            dev_crc = m.group(1).lower() if m else ""
            self.log("RAM crc32 = %s\n" % (dev_crc or "?"))
            if dev_crc and dev_crc != pc_crc:
                self.log("[X] RAM crc32 与 PC 不一致，中止（未写 flash）\n")
                self.stat("校验失败")
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
                out = self.run_cmd("crc32 0x%X 0x%X" % (RBUF_ADDR, size),
                                   2.5, 60)
                m = re.search(r"=\s+([0-9a-fA-F]{8})", out)
                rb = m.group(1).lower() if m else ""
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
        cmd = "reset" if state == "uboot" else "reboot"
        self.log("\n>>> %s（当前状态 %s）\n" % (cmd, state))
        self.link.clear()
        self.link.write(cmd + "\r")
        self.stat("重启中 ...")
        time.sleep(3)
        self.stat("就绪")

    # ---------------- 退出
    def on_close(self):
        try:
            self.link.close()
        except Exception:
            pass
        self.root.destroy()


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
