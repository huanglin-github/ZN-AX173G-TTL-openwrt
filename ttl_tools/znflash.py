# -*- coding: utf-8 -*-
"""兆能 ZN-AX173G (EN7523) U-Boot + TFTP 刷机工具。

为什么需要它：
  设备当前运行的系统的分区表由内核 DTS 决定。如果要刷的镜像比现有
  firmware 分区大，`mtd write` 写不下 —— 必须走 U-Boot，因为 U-Boot 的
  flash 读写只认 flash 绝对偏移，不受内核分区表限制。

用法：
  # 完整刷机（推荐）：进 U-Boot -> tftpboot -> erase -> write -> bootm 验证
  python znflash.py --image firmware.bin

  # 只加载到 RAM 检查通路（不碰 flash，零风险）
  python znflash.py --image firmware.bin --load-only

  # 镜像已写入过，只要从 flash 启动看看
  python znflash.py --boot-only

  # 刷完同时把 bootcmd 改成从 slave 启动（断电重启也进新系统）
  python znflash.py --image firmware.bin --set-bootcmd

  # 自定义目标偏移/长度（默认 0x1ec0000，长度按镜像自动 128K 对齐）
  python znflash.py --image fw.bin --addr 0x1ec0000

安全网：
  masterbank(0xc0000, 30MB) 里的原厂固件从不被写；
  刷坏时在 U-Boot 里跑 `flash imgread 2048; bootm` 即回原厂，
  或用 restore_stock.py 把 bootcmd 改回原厂值。
"""
import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tftpd_min import TftpServer
from ztool import Console, kill_serial, local_ip

HERE = os.path.dirname(os.path.abspath(__file__))
UBOOT_USER = "telecomadmin"
UBOOT_PASS = "nE7jA%5m"
DEFAULT_ADDR = 0x1EC0000
RAM_ADDR = 0x88000000
RBUF_ADDR = 0x8A000000       # 回读校验用的第二块 RAM，避免覆盖已加载的镜像
PROMPT = "ECNT>"

# 安全边界：flash 256MB
SAFE_MIN = 0x1EC0000         # 之前是原厂 master bank（含 bootloader），一个字节都不许写
SAFE_MAX = 0xDDC0000         # reservearea 起点，其 0x200000 处就是 bootflag


class UBoot:
    def __init__(self, port="COM6", baud=115200, log="znflash.log"):
        self.c = Console(port, baud, logpath=os.path.join(HERE, log), echo=True)

    def close(self):
        self.c.close()

    def login(self, timeout=20):
        # 注意：这台 U-Boot 的登录提示是 "UserName: " 先出现，
        # 发一个裸回车会被当成空用户名直接跳到 "Password:"，
        # 结果口令被塞进密码框、用户名永远为空 —— 所以千万不要先敲回车。
        c = self.c
        c.wake(1)
        time.sleep(0.3)
        c.drain(1.0)
        if PROMPT in c.tail:
            return True
        if "UserName" in c.tail:
            c.tail = ""
            c.write(UBOOT_USER + "\r")
            time.sleep(1.2)
            c.drain(1.2)
            if "Password" in c.tail:
                c.tail = ""
                c.write(UBOOT_PASS + "\r")
                time.sleep(1.8)
                c.drain(1.8)
        return PROMPT in c.tail

    def cmd(self, line, wait=2.0, quiet=True):
        c = self.c
        c.tail = ""
        c.write(line + "\r")
        c.drain(wait)
        out = c.tail
        if not quiet:
            print(out[-800:])
        return out

    def cmd_long(self, line, timeout=600, label=None):
        c = self.c
        print("\n>>> %s" % (label or line))
        c.tail = ""
        c.write(line + "\r")
        t0 = time.time()
        ok = c.drain_until(PROMPT, timeout)
        dt = time.time() - t0
        print("[%s] %s in %.0fs" % (label or line, "done" if ok else "TIMEOUT", dt))
        return ok, c.tail

    def bootm(self, addr, timeout=300):
        c = self.c
        print("\n>>> bootm 0x%x" % addr)
        c.tail = ""
        c.write("bootm 0x%x\r" % addr)
        ok = c.drain_until("Please press Enter to activate this console", timeout)
        print("[bootm] %s" % ("console ready" if ok else "NOT READY"))
        return ok, c.tail


def read_console_after_boot(ub, cmds):
    c = ub.c
    c.write("\r")
    time.sleep(2.0)
    c.drain(2.0)
    for cmd, wait in cmds:
        c.tail = ""
        c.write(cmd + "\r")
        c.drain(wait)
        print("\n$ %s" % cmd)
        print(c.tail[:1500])


def check_range(addr, length):
    """写前护栏：只允许动 slave bank，master bank 与 reservearea 一律拒绝。"""
    if addr < SAFE_MIN:
        return "addr 0x%X 落在原厂 master bank (0x0~0x%X) 内，拒绝写入" % (addr, SAFE_MIN)
    if addr + length > SAFE_MAX:
        return "addr+len = 0x%X 越过 reservearea 起点 0x%X，拒绝写入" % (addr + length, SAFE_MAX)
    return None


def verify_flash(ub, addr, size):
    """回读进第二块 RAM，用 FIT 自带的 sha1 校验实际落盘内容。"""
    ok, _ = ub.cmd_long("flash read 0x%x 0x%X 0x%x" % (addr, size, RBUF_ADDR),
                        timeout=900, label="回读校验 %d bytes" % size)
    if not ok:
        print("[!] 回读未返回提示符，继续检查内容")
    head = ub.cmd("md.b 0x%x 0x4" % RBUF_ADDR, 1.5)
    if "d0 0d fe ed" not in head:
        print("[X] 回读首部不是 FIT magic（d0 0d fe ed），写入失败")
        return False
    info = ub.cmd("iminfo 0x%x" % RBUF_ADDR, 4.0)
    if "sha1+ OK" in info or "Verifying Hash Integrity ... OK" in info:
        print("[OK] 回读 FIT sha1 校验通过")
        return True
    print("[!] iminfo 未见 hash 校验结果，请人工确认：")
    print(info[-800:])
    return True


def save_bootcmd(ub, addr, size):
    cmd = "flash read 0x%x 0x%X 0x%x; bootm 0x%x" % (addr, size, RAM_ADDR, RAM_ADDR)
    print("\n>>> setenv bootcmd \"%s\"" % cmd)
    ub.cmd('setenv bootcmd "%s"' % cmd, 1.5)
    out = ub.cmd("printenv bootcmd", 1.5)
    if cmd not in out:
        print("[X] bootcmd 设置后再读不一致，已放弃 saveenv")
        print(out[-400:])
        return False
    print(">>> saveenv")
    ub.cmd("saveenv", 4.0)
    print("[OK] bootcmd 已持久化")
    return True


def do_flash(ub, image, addr, load_only=False, do_boot=True,
             verify=True, write_bootcmd=False):
    size = os.path.getsize(image)
    aligned = (size + 0x1FFFF) & ~0x1FFFF
    print("image: %s" % image)
    print("size : %d bytes (0x%X), 128K-aligned 0x%X" % (size, size, aligned))
    print("addr : flash 0x%X -> ram 0x%X" % (addr, RAM_ADDR))
    if not load_only:
        err = check_range(addr, aligned)
        if err:
            print("[X] %s" % err)
            return False

    fwdir = os.path.dirname(os.path.abspath(image))
    base = "znfw.bin"
    tmp = os.path.join(fwdir, base)
    if os.path.abspath(tmp) != os.path.abspath(image):
        shutil.copyfile(image, tmp)

    pc_ip = local_ip("192.168.1.1")
    print("PC ip: %s" % pc_ip)
    srv = TftpServer(fwdir, port=69, verbose=True).start()
    time.sleep(0.5)

    try:
        ub.cmd("setenv ipaddr 192.168.1.1", 1.0)
        ub.cmd("setenv netmask 255.255.255.0", 1.0)
        ub.cmd("setenv serverip %s" % pc_ip, 1.0)
        # 61MB 镜像：512 字节/包太慢，协商到 1468（tftpd_min 支持上限）
        ub.cmd("setenv tftpblocksize 1468", 1.0)
        out = ub.cmd("ping %s" % pc_ip, 6.0)
        if "is alive" not in out:
            print("[!] ping 不通，请检查网线与网段")
            return False

        ok, out = ub.cmd_long("tftpboot 0x%x %s" % (RAM_ADDR, base),
                              timeout=900, label="tftpboot %d bytes" % size)
        if "Bytes transferred" not in out:
            print("[X] tftpboot 失败")
            return False
        if srv.sent != size:
            print("[!] TFTP 只发了 %d/%d 字节" % (srv.sent, size))

        out = ub.cmd("md.b 0x%x 0x4" % RAM_ADDR, 1.5)
        if "d0 0d fe ed" not in out:
            print("[!] RAM 首部不是 FIT magic，请确认镜像是 FIT 格式")
        if load_only:
            print("\n[LOAD-ONLY] 已加载进 RAM，未写 flash。断电或 reset 即丢弃。")
            return True

        ok, _ = ub.cmd_long("flash erase 0x%x 0x%X" % (addr, aligned),
                            timeout=600, label="flash erase 0x%X" % aligned)
        if not ok:
            print("[X] erase 超时")
            return False

        ok, out = ub.cmd_long("flash write 0x%x 0x%X 0x%x" % (addr, size, RAM_ADDR),
                              timeout=900, label="flash write %d bytes" % size)
        if not ok:
            print("[X] write 超时")
            return False

        if verify and not verify_flash(ub, addr, size):
            return False

        if write_bootcmd:
            save_bootcmd(ub, addr, size)

        if do_boot:
            ok, tail = ub.bootm(RAM_ADDR, 300)
            if ok:
                read_console_after_boot(ub, [
                    ("cat /proc/mtd", 3.0),
                    ("df -h / /overlay", 3.0),
                    ("mount | grep overlay", 2.0),
                ])
            else:
                print(tail[-1500:])
            return ok
        return True
    finally:
        srv.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="要刷的 FIT 镜像（sysupgrade.bin / initramfs-kernel.bin）")
    ap.add_argument("--addr", default="0x1ec0000", help="flash 目标偏移，默认 0x1ec0000")
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--load-only", action="store_true", help="只 tftpboot 进 RAM，不写 flash")
    ap.add_argument("--no-boot", action="store_true", help="写完后不 bootm")
    ap.add_argument("--boot-only", action="store_true", help="只从 RAM 启动（需先前已 tftpboot）")
    ap.add_argument("--no-verify", action="store_true", help="跳过写后回读 sha1 校验")
    ap.add_argument("--set-bootcmd", action="store_true",
                    help="写成功后同步 setenv bootcmd + saveenv（让断电重启也能进新系统）")
    ap.add_argument("--reboot-into-uboot", action="store_true",
                    help="先重启设备并在串口打断 autoboot 进 U-Boot")
    args = ap.parse_args()

    os.chdir(HERE)
    kill_serial()
    time.sleep(1)
    ub = UBoot(args.port, args.baud)
    try:
        if args.reboot_into_uboot:
            print(">>> 重启并打断 autoboot（若设备正在 OpenWrt 运行）")
            ub.c.write("reboot\r")
            ub.c.tail = ""
            t0 = time.time()
            got = False
            while time.time() - t0 < 120:
                txt = ub.c.pump()
                if txt and ("Hit any key" in txt or "stop autoboot" in txt):
                    ub.c.write("x")
                    got = True
                    print("[!] 已打断 autoboot")
                    break
                time.sleep(0.005)
            if not got:
                print("[!] 未捕捉到 autoboot 提示，可能已停在 U-Boot 或需要手动断电")
            time.sleep(1.5)

        if not ub.login():
            print("[X] 没进到 U-Boot 提示符（ECNT>）")
            print("    请确认：串口 COM6 已接、设备已上电/已停在 U-Boot")
            return 2

        addr = int(args.addr, 16) if args.addr.lower().startswith("0x") else int(args.addr, 0)

        if args.boot_only:
            ok, tail = ub.bootm(RAM_ADDR, 300)
            if not ok:
                print(tail[-1500:])
            return 0 if ok else 1

        if not args.image:
            print("需要 --image")
            return 2
        if not os.path.isfile(args.image):
            print("镜像不存在: %s" % args.image)
            return 2

        ok = do_flash(ub, args.image, addr,
                      load_only=args.load_only, do_boot=not args.no_boot,
                      verify=not args.no_verify, write_bootcmd=args.set_bootcmd)
        return 0 if ok else 1
    finally:
        ub.close()


if __name__ == "__main__":
    sys.exit(main())
