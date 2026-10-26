# -*- coding: utf-8 -*-
"""极简 TFTP 服务端（只读 RRQ），供 U-Boot tftpboot 使用。"""
import os
import socket
import struct
import threading
import time


class TftpServer:
    def __init__(self, root, port=69, verbose=True):
        self.root = root
        self.port = port
        self.verbose = verbose
        self.stop_evt = threading.Event()
        self.sent = 0
        self.served = None
        self.err = None
        self.delay = 0.0004      # 每块之间的节流，避免 U-Boot 侧缓冲溢出丢块
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        return self

    def stop(self):
        self.stop_evt.set()

    def _log(self, *a):
        if self.verbose:
            print("[tftp]", *a, flush=True)

    def _run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.port))
        except Exception as e:
            self.err = "bind failed: %s" % e
            self._log(self.err)
            return
        s.settimeout(1.0)
        self._log("listening on 0.0.0.0:%d root=%s" % (self.port, self.root))
        while not self.stop_evt.is_set():
            try:
                pkt, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception as e:
                self.err = str(e)
                continue
            if len(pkt) < 4:
                continue
            op = struct.unpack("!H", pkt[:2])[0]
            if op != 1:
                continue
            try:
                self._serve_rrq(pkt[2:], addr)
            except Exception as e:
                self.err = str(e)
                self._log("serve error: %s" % e)
        s.close()

    def _serve_rrq(self, body, addr):
        parts = body.split(b"\x00")
        fname = parts[0].decode("utf-8", "replace")
        opts = {}
        # parts[0]=filename, parts[1]=mode(octet/netascii), 选项对从 parts[2] 开始
        i = 2
        while i + 1 < len(parts):
            k, v = parts[i], parts[i + 1]
            if not k:
                break
            opts[k.decode("utf-8", "replace").lower()] = v.decode("utf-8", "replace")
            i += 2
        self._log("RRQ %s from %s:%d opts=%s" % (fname, addr[0], addr[1], opts))

        path = os.path.join(self.root, os.path.basename(fname))
        if not os.path.isfile(path):
            ts = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ts.sendto(struct.pack("!HH", 5, 1) + b"not found\x00", addr)
            ts.close()
            return

        with open(path, "rb") as f:
            data = f.read()
        self.served = fname
        self.sent = 0
        total = len(data)

        blksize = 512
        if "blksize" in opts:
            try:
                blksize = max(8, min(int(opts["blksize"]), 1468))
            except Exception:
                blksize = 512

        ts = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ts.bind(("0.0.0.0", 0))
        ts.settimeout(4.0)

        if opts:
            oack = b""
            if "blksize" in opts:
                oack += b"blksize\x00%d\x00" % blksize
            if "tsize" in opts:
                oack += b"tsize\x00%d\x00" % total
            if "timeout" in opts:
                oack += b"timeout\x00%s\x00" % opts["timeout"].encode()
            if oack:
                ts.sendto(struct.pack("!H", 6) + oack, addr)
                try:
                    r, _ = ts.recvfrom(2048)
                    if struct.unpack("!H", r[:2])[0] != 4:
                        pass
                except socket.timeout:
                    self._log("no ACK0 after OACK, falling back to 512")
                    blksize = 512

        n = 1
        off = 0
        t0 = time.time()
        retries_total = 0
        while off < total:
            chunk = data[off:off + blksize]
            pkt = struct.pack("!HH", 3, n & 0xFFFF) + chunk
            acked = False
            for _ in range(8):
                ts.sendto(pkt, addr)
                try:
                    r, _ = ts.recvfrom(2048)
                    if len(r) >= 4 and struct.unpack("!H", r[:2])[0] == 4 \
                            and struct.unpack("!H", r[2:4])[0] == (n & 0xFFFF):
                        acked = True
                        break
                except socket.timeout:
                    retries_total += 1
                    continue
            if not acked:
                self._log("give up at block %d/%d" % (n, (total + blksize - 1) // blksize))
                break
            off += len(chunk)
            n += 1
            self.sent = off          # 实时进度，供 GUI 轮询
            if self.delay:
                time.sleep(self.delay)
            if n % 2000 == 0:
                pct = 100.0 * off / total
                sp = off / 1024.0 / max(time.time() - t0, 0.001)
                self._log("%.1f%% (%d/%d) %.0f KB/s retries=%d" % (pct, off, total, sp, retries_total))
            if self.stop_evt.is_set():
                break
        dt = time.time() - t0
        self.sent = off
        self._log("done %d/%d bytes in %.1fs (%.0f KB/s) retries=%d"
                  % (off, total, dt, off / 1024.0 / max(dt, 0.001), retries_total))
        # 最后一个 ACK 后可能还需重发最后一个空块（多数 U-Boot 不需要）
        ts.close()
