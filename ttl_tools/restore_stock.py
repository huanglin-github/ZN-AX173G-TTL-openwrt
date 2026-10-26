# -*- coding: utf-8 -*-
"""一键还原 ZN-AX173G 的原厂系统。

原理：只是把 U-Boot 的 bootcmd 改回原厂值 `flash imgread 2048;bootm`，
      原厂固件一直完好地躺在 master bank(0xc0000) 里，从未被改动。
      所以不需要重刷任何 flash 内容，也不碰 reservearea / WiFi EEPROM。

用法：
  python restore_stock.py

流程：
  提示断电上电 -> 打断 autoboot -> 登录 U-Boot -> setenv bootcmd（原厂值）
  -> saveenv -> reset -> 检查是否出现原厂内核日志

可选：
  --bootflag 0   额外把 reservearea 偏移 0x200000 的 boot flag 写成 '0'
                 （通常不需要：bootcmd 正确时 bootflag 不参与决策）
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ztool import Console, kill_serial

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_BOOTCMD = "flash imgread 2048;bootm"
BOOTFLAG_FLASH = 0xDFC0000   # reservearea 0xddc0000 + 0x200000
BOOTFLAG_SPARE = 0x88000000  # 用来放一个字节的临时内存地址

STOCK_MARKERS = ("tc3162ser_startup", "NETDEV_UP", "ECNT_HOOK_ERROR", "br0: port")


def enter_uboot(c, window=900):
    print("\n" + "=" * 62)
    print("请给设备断电，等 2 秒再上电（脚本会自动打断 autoboot）")
    print("=" * 62)
    aborted = user_sent = pass_sent = False
    t0 = time.time()
    c.tail = ""
    while time.time() - t0 < window:
        txt = c.pump()
        if txt and ("Hit any key" in txt or "stop autoboot" in txt) and not aborted:
            c.write("x")
            aborted = True
            print("\n[!] 打断 autoboot")
        if txt and "UserName" in txt and not user_sent:
            time.sleep(0.05)
            c.write("telecomadmin\r")
            user_sent = True
        if txt and "Password" in txt and not pass_sent:
            time.sleep(0.05)
            c.write("nE7jA%5m\r")
            pass_sent = True
        if pass_sent and c.quiet() and "ECNT" in c.tail:
            print("\n[OK] 进入 U-Boot")
            return True
        time.sleep(0.005)
    return False


def set_bootflag_zero(c):
    """把 boot flag 写成 ASCII '0'(0x30)，只擦一个 128KB 块，不碰 EEPROM(0x4c000)。"""
    c.run_cmd("mw.b 0x%x 0x30 1" % BOOTFLAG_SPARE, idle=1.0, timeout=20)
    c.run_cmd("flash erase 0x%x 0x20000" % BOOTFLAG_FLASH, idle=4.0, timeout=180)
    c.run_cmd("flash write 0x%x 1 0x%x" % (BOOTFLAG_FLASH, BOOTFLAG_SPARE),
              idle=3.0, timeout=120)
    c.run_cmd("flash read 0x%x 1 0x%x" % (BOOTFLAG_FLASH, BOOTFLAG_SPARE),
              idle=1.5, timeout=30)
    c.run_cmd("md.b 0x%x 1" % BOOTFLAG_SPARE, idle=1.5, timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--bootflag", choices=["0", "1"], default=None,
                    help="可选：额外写 boot flag；0=原厂 master，1=OpenWrt slave")
    args = ap.parse_args()

    kill_serial()
    time.sleep(1)
    try:
        c = Console(args.port, args.baud, logpath=os.path.join(HERE, "restore_stock.log"))
    except Exception as e:
        print("!!! 打不开 %s: %s" % (args.port, e))
        return 2

    try:
        if not enter_uboot(c):
            print("\n[X] 未能进入 U-Boot")
            return 1

        old = c.run_cmd("printenv bootcmd", idle=1.5, timeout=30)
        print("\n[原 bootcmd] %r" % old[-200:])

        c.run_cmd('setenv bootcmd "%s"' % STOCK_BOOTCMD, idle=1.5, timeout=30)
        chk = c.run_cmd("printenv bootcmd", idle=1.5, timeout=30)
        if "imgread 2048" not in chk:
            print("\n[X] setenv 未生效，放弃 saveenv")
            return 1
        c.run_cmd("saveenv", idle=4.0, timeout=300)

        if args.bootflag:
            print("\n>>> 额外写 boot flag = %s" % args.bootflag)
            c.run_cmd("mw.b 0x%x 0x%02x 1" % (BOOTFLAG_SPARE, ord(args.bootflag)),
                      idle=1.0, timeout=20)
            c.run_cmd("flash erase 0x%x 0x20000" % BOOTFLAG_FLASH, idle=4.0, timeout=180)
            c.run_cmd("flash write 0x%x 1 0x%x" % (BOOTFLAG_FLASH, BOOTFLAG_SPARE),
                      idle=3.0, timeout=120)

        print("\n>>> reset 启动原厂系统")
        c.tail = ""
        c.write("reset\r")
        time.sleep(2)
        c.drain(60)

        hit = [m for m in STOCK_MARKERS if m in c.tail]
        if hit:
            print("\n[完成] 已回原厂系统（命中特征: %s）" % ", ".join(hit))
            return 0
        print("\n[!] 60 秒内未见原厂特征日志，请继续观察串口输出")
        return 1
    except KeyboardInterrupt:
        print("\n[中断]")
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
