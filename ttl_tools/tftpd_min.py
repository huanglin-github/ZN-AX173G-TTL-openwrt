# -*- coding: utf-8 -*-
"""极简 TFTP 服务端（只读 RRQ），供 U-Boot tftpboot 使用。

U-Boot 的停等 TFTP 有两个容易被忽略的行为，这里都照顾到了：

1) 它超时会「Retry count exceeded; starting again」，也就是**重发 RRQ 重开一次
   会话**。如果服务端还卡在上一次会话的等 ACK 里（旧版要 8×4=32 秒），新 RRQ
   就没人理，于是它一直等、一直超时，最后彻底失败。所以每个会话放到独立线程，
   新 RRQ 一到就立刻让旧会话收工。
2) 它会重传 ACK。旧版把「重复 ACK」当成丢包，立刻重发数据块，结果 ACK 队列越
   堆越多，块号彻底错位（你看到的 First block is not block 1 (18852) 就是旧会话
   的包串进了新会话）。现在重复 ACK 直接忽略，只认期望块号。
"""
import os
import socket
import struct
import threading
import time

MAX_BLKSIZE = 1468           # 1500 MTU - 8 UDP - 4 TFTP
BLOCK_TIMEOUT = 1.0          # U-Boot 侧大约 1s 就超时，服务端别比它慢
BLOCK_RETRIES = 4


class TftpServer:
    def __init__(self, root, port=69, verbose=True):
        self.root = root
        self.port = port
        self.verbose = verbose
        self.stop_evt = threading.Event()
        self.ready = threading.Event()
        self.sent = 0
        self.total = 0
        self.served = None
        self.err = None
        self.delay = 0.0002
        self._busy = False
        self._restart = False
        self._pending = None
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        return self

    def stop(self):
        self._restart = True
        self.stop_evt.set()
        th = self._th
        if th is not None and th.is_alive():
            th.join(2.0)          # 等它真的放手端口，否则下一次 bind 会打架

    def reset(self):
        """新一轮传输前清进度，避免 GUI 读到上一次的残留值。"""
        self.sent = 0
        self.served = None
        self.err = None

    def wait_ready(self, timeout=3.0):
        """等服务端 bind 完成；返回 True 表示可以开始传输。"""
        self.ready.wait(timeout)
        return self.ready.is_set() and not self.err

    def _log(self, *a):
        if self.verbose:
            print("[tftp]", *a, flush=True)

    # ---------------- 主循环：只负责接 RRQ
    def _run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.port))
        except Exception as e:
            self.err = "bind failed: %s" % e
            self._log(self.err)
            self.ready.set()
            s.close()
            return
        s.settimeout(0.5)
        self.ready.set()
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
            if op == 2:                                   # WRQ：设备往 PC 上传
                threading.Thread(target=self._serve_wrq,
                                 args=(pkt[2:], addr), daemon=True).start()
                continue
            if op != 1:                                   # 其余只处理 RRQ
                continue
            if self._busy:
                # U-Boot 重开了会话：让旧会话马上收工，并把这个 RRQ 排队
                self._restart = True
                self._pending = (pkt[2:], addr)
                self._log("new RRQ -> abort previous session")
                continue
            self._busy = True
            self._restart = False
            threading.Thread(target=self._worker, args=(pkt[2:], addr),
                             daemon=True).start()
        s.close()

    def _worker(self, body, addr):
        try:
            self._serve_rrq(body, addr)
        except Exception as e:
            self.err = str(e)
            self._log("serve error: %s" % e)
        finally:
            self._busy = False
            nxt, self._pending = self._pending, None
            if nxt is not None and not self.stop_evt.is_set():
                # 旧会话已经让位，马上服务 U-Boot 那次重开的请求
                self._busy = True
                self._restart = False
                threading.Thread(target=self._worker, args=nxt,
                                 daemon=True).start()

    # ---------------- 接收上传（busybox `tftp -p`）
    def _serve_wrq(self, body, addr):
        """设备主动把文件推上来，写进 self.root。

        原厂 busybox 没有 httpd，备份只能靠这条路。
        """
        parts = body.split(b"\x00")
        fname = os.path.basename(
            parts[0].decode("utf-8", "replace") or "upload.bin")
        opts = {}
        i = 2
        while i + 1 < len(parts):
            k, v = parts[i], parts[i + 1]
            if not k:
                break
            opts[k.decode("utf-8", "replace").lower()] = \
                v.decode("utf-8", "replace")
            i += 2
        self._log("WRQ %s from %s:%d opts=%s"
                  % (fname, addr[0], addr[1], opts))

        blksize = 512
        if "blksize" in opts:
            try:
                blksize = max(8, min(int(opts["blksize"]), MAX_BLKSIZE))
            except Exception:
                blksize = 512

        path = os.path.join(self.root, fname)
        tmp = path + ".part"
        ts = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ts.bind(("0.0.0.0", 0))
        ts.settimeout(BLOCK_TIMEOUT)

        if opts:
            oack = b""
            if "blksize" in opts:
                oack += b"blksize\x00%d\x00" % blksize
            if "tsize" in opts:
                oack += b"tsize\x00%s\x00" % opts["tsize"].encode()
            if "timeout" in opts:
                oack += b"timeout\x00%s\x00" % opts["timeout"].encode()
            if oack:
                ts.sendto(struct.pack("!H", 6) + oack, addr)
        else:
            ts.sendto(struct.pack("!HH", 4, 0), addr)

        expect = 1
        total = 0
        idle = 0
        t0 = time.time()
        ok = False
        try:
            with open(tmp, "wb") as f:
                while not self.stop_evt.is_set():
                    try:
                        pkt, _ = ts.recvfrom(65536)
                    except socket.timeout:
                        idle += 1
                        if idle > 20:          # 20 秒没数据就放弃
                            self._log("WRQ %s stalled at %d" % (fname, total))
                            break
                        ts.sendto(struct.pack("!HH", 4,
                                              (expect - 1) & 0xFFFF), addr)
                        continue
                    if len(pkt) < 4:
                        continue
                    op = struct.unpack("!H", pkt[:2])[0]
                    if op == 5:
                        self._log("WRQ %s peer error: %r" % (fname, pkt[4:]))
                        break
                    if op != 3:
                        continue
                    idle = 0
                    n = struct.unpack("!H", pkt[2:4])[0]
                    data = pkt[4:]
                    if n == (expect & 0xFFFF):
                        f.write(data)
                        total += len(data)
                        ts.sendto(struct.pack("!HH", 4, n), addr)
                        expect += 1
                        self.sent = total
                        if len(data) < blksize:      # 末块
                            ok = True
                            break
                    else:
                        ts.sendto(struct.pack("!HH", 4, n), addr)
            if ok:
                os.replace(tmp, path)
                self.served = fname
                self._log("WRQ done %s %d bytes in %.1fs"
                          % (fname, total, time.time() - t0))
            else:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        except Exception as e:
            self.err = "WRQ failed: %s" % e
            self._log(self.err)
            try:
                os.remove(tmp)
            except Exception:
                pass
        finally:
            ts.close()

    # ---------------- 单个会话
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
        self.total = total

        blksize = 512
        if "blksize" in opts:
            try:
                blksize = max(8, min(int(opts["blksize"]), MAX_BLKSIZE))
            except Exception:
                blksize = 512

        ts = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ts.bind(("0.0.0.0", 0))
        ts.settimeout(BLOCK_TIMEOUT)

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
                    if len(r) < 4 or struct.unpack("!H", r[:2])[0] != 4:
                        self._log("unexpected reply to OACK")
                except socket.timeout:
                    self._log("no ACK0 after OACK, falling back to 512")
                    blksize = 512

        n = 1
        off = 0
        t0 = time.time()
        retries_total = 0
        while True:
            if self._restart or self.stop_evt.is_set():
                self._log("session aborted at block %d" % n)
                break
            chunk = data[off:off + blksize]
            pkt = struct.pack("!HH", 3, n & 0xFFFF) + chunk
            acked = False
            for _attempt in range(BLOCK_RETRIES):
                if self._restart:
                    break
                ts.sendto(pkt, addr)
                deadline = time.time() + BLOCK_TIMEOUT
                while time.time() < deadline:
                    try:
                        r, _r = ts.recvfrom(2048)
                    except socket.timeout:
                        break
                    if len(r) < 2:
                        continue
                    rop = struct.unpack("!H", r[:2])[0]
                    if rop == 5:                      # 对端放弃
                        self._log("peer ERROR: %r" % r[4:])
                        ts.close()
                        self.sent = min(off, total)
                        return
                    if rop != 4 or len(r) < 4:
                        continue                      # 不是 ACK，丢掉
                    rblk = struct.unpack("!H", r[2:4])[0]
                    if rblk == (n & 0xFFFF):
                        acked = True
                        break
                    # 重复/陈旧 ACK：直接忽略，等正确的那个（不要重发！）
                if acked:
                    break
                retries_total += 1
            if not acked:
                self._log("give up at block %d/%d"
                          % (n, (total + blksize - 1) // blksize))
                break
            off += len(chunk)
            self.sent = min(off, total)
            if len(chunk) < blksize:              # 最后一块（含 0 字节块）
                break
            n += 1
            if self.delay:
                time.sleep(self.delay)
            if n % 5000 == 0:
                pct = 100.0 * off / total
                sp = off / 1024.0 / max(time.time() - t0, 0.001)
                self._log("%.1f%% (%d/%d) %.0f KB/s retries=%d"
                          % (pct, off, total, sp, retries_total))
        dt = time.time() - t0
        self.sent = min(off, total)
        self._log("done %d/%d bytes in %.1fs (%.0f KB/s) retries=%d"
                  % (off, total, dt, off / 1024.0 / max(dt, 0.001),
                     retries_total))
        ts.close()
