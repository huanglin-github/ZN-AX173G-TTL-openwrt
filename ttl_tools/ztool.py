# -*- coding: utf-8 -*-
"""兆能 ZN-AX173G (EN7523) 刷机工具公共库。

提供：
  Console     串口控制台封装（发送命令 / 等待空闲 / 等提示符 / 纯监听）
  serve_dir   后台线程 HTTP 服务，供设备 wget 拉取镜像
  local_ip    取本机对外的 IPv4 地址
  md5_file    文件 MD5
  kill_serial 结束占用串口的旧脚本进程（Windows）
"""
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import serial

PROMPT_PAT = r"(?m)^[^\r\n]{0,32}[#\$]\s*$"


class Console:
    """串口控制台。pump() 会把收到的数据回显到 stdout 并落盘日志。"""

    def __init__(self, port="COM6", baud=115200, logpath=None, echo=True):
        self.ser = serial.Serial(port, baud, timeout=0.03, write_timeout=1)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.log = open(logpath, "wb") if logpath else None
        self.echo = echo
        self.last_rx = time.time()
        self.total_rx = 0
        self.tail = ""

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("ascii", "replace")
        self.ser.write(data)
        self.ser.flush()
        if self.log:
            self.log.write(b"<<< " + data + b"\n")
            self.log.flush()

    def pump(self):
        n = self.ser.in_waiting
        raw = self.ser.read(n if n else 1)
        if not raw:
            return ""
        if self.log:
            self.log.write(raw)
            self.log.flush()
        if self.echo:
            try:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            except Exception:
                pass
        self.tail = (self.tail + raw.decode("utf-8", "replace"))[-2000:]
        self.last_rx = time.time()
        self.total_rx += len(raw)
        return raw.decode("utf-8", "replace")

    def quiet(self, idle=0.6):
        return time.time() - self.last_rx > idle

    def run_cmd(self, cmd, idle=2.5, timeout=120.0, label=None):
        print("\n" + "=" * 62)
        print(">>> %s" % (label or cmd))
        print("=" * 62)
        self.write(cmd + "\r")
        t0 = time.time()
        got = ""
        while time.time() - t0 < timeout:
            t = self.pump()
            if t:
                got += t
                continue
            if got and time.time() - self.last_rx > idle:
                break
            time.sleep(0.005)
        return got

    def drain(self, seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            self.pump()
            time.sleep(0.005)

    def drain_until(self, marker, seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            self.pump()
            if marker in self.tail:
                return True
            time.sleep(0.005)
        return False

    def wake(self, times=3):
        for _ in range(times):
            self.write("\r")
            self.drain(0.5)

    def close(self):
        for f in (self.ser, self.log):
            try:
                if f:
                    f.close()
            except Exception:
                pass


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def serve_dir(path, port=8000):
    """在后台线程提供 HTTP 下载服务，返回 (server, 实际端口)。"""
    handler = partial(_QuietHandler, directory=path)
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def local_ip(target="223.5.5.5"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def kill_serial(script_hints=("_uboot_", "_owrt_", "_serial", "flash_openwrt", "restore_stock")):
    """结束占用串口的旧 python 脚本（避免 COM6 被占用）。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return
    for line in out.splitlines():
        low = line.lower()
        if not any(h in low for h in script_hints):
            continue
        parts = line.split()
        pid = parts[-1] if parts else ""
        if pid.isdigit() and int(pid) != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True)
