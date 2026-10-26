# EN7523 AX3000（兆能 ZN-AX173G）OpenWrt 移植 —— 源码与 TTL 工具

针对兆能 ZN-AX173G（EcoNet EN7523 SoC，SPI-NAND 256MB）的 OpenWrt 主线移植补丁、
TTL 刷机工具，以及逆向过程中收集的原厂证据。

## 通用性说明（先读）

| 内容 | 适用范围 |
|---|---|
| `ttl_tools/`（znui.py / znflash.py 等） | **同型号 ZN-AX173G 通用**：串口参数、U-Boot 凭据、flash 布局为该批次出厂一致配置 |
| `target/linux/airoha/` 固件补丁与 DTS | **仅限 ZN-AX173G 这一块板**；DTS 引脚（GPIO4/5=VBUS、LED 线序）、分区表按该板定制，刷其他型号可能不启动或损坏外设 |
| 其他型号光猫 | 工具流程可复用，但凭据/偏移/镜像必须重新验证 |

## target/linux/airoha/（编译机改动快照 2026-10-26）

- `patches-6.18/220-07-phy-airoha-Add-support-for-Airoha-AN7581-USB-PHY.patch`

  EN7523 USB 关键修复（三件套）：

  1. **U2 PHY 双通道初始化** —— EN7523 有两个 USB2 口，第二通道寄存器在
     `base+0x1000`（原厂 DTB usb_phy reg size=0x1fff 证实），mainline 该驱动为
     AN7581 单 U2 口设计，只初始化第一通道 → 第二口 PHY 停在无效 PLS=14。
  2. **VBUS 供电** —— 两个 USB2 口的 5V 由 GPIO4/GPIO5 开关，probe 最早处直接写
     GPIO 控制器 `0x1fbf0200`（dir=0x200 bit(pin*2)、output=0x214 bit(pin)、
     data=0x204 bit(pin)，语义来自 gpio-en7523.c）拉高。原厂等价操作是
     `echo active 4 1 > /proc/tc3162/gpio_output`（tcledctrl.ko 反汇编证实只写
     GPIO 控制器，不碰 SCU）。
     ⚠️ 原厂脚本里的 SCU `0x1fa201e4/e8` 写入属于低功耗段，与 USB 无关，
     且在 6.18 内核 raw 写会导致整机静默，勿模仿。
  3. **端口重扫** —— xHCI 端口停在 Disabled 不会自动重扫；delayed work 对
     PORTSC `0x420/0x440` 写 `0xA0`(RxDetect) 触发检测（跳过工作口 0x430），
     残留 Disabled 口补 `0x2A0`（LWS|PP）。

- `dts/en7523-ax3000-router.dts`

  板级设备树。**usb_phy reg 必须 `0x2000`**（第二窗口）：regmap 映射 0x1000 时
  写第二通道偏移 0x136c 越界 → kernel oops → bootloop（踩过的坑）。

- `en7523/config-6.18`

  追加 `CONFIG_NEW_LEDS=y / CONFIG_LEDS_CLASS=y / CONFIG_LEDS_GPIO=y`
  （generic config 默认未开，面板 LED 需要它），DTS 里 led-usb1/usb2 加
  `default-state="on"`。

## ttl_tools/（PC 端 TTL 工具，Windows / pyserial + tkinter）

- `ztool.py`      串口控制台封装（wake/run_cmd/drain_until）
- `tftpd_min.py`  迷你 TFTP 服务器（支持 blksize 协商到 1468）
- `znflash.py`    U-Boot TFTP 刷机库：erase→write→回读 sha1 校验；
                  护栏只允许写 0x1EC0000~0xDDC0000（firmware 分区），
                  masterbank（原厂 30MB）与 reservearea 一个字节不碰
- `znui.py`       TTL 工具台 GUI：
                  串口终端 · 设备自检 · 进 U-Boot · TFTP 刷机 · RAM 试载 ·
                  固化 bootcmd · 还原原厂 · **一键备份分区**（/proc/mtd →
                  dd|gzip → busybox httpd → PC 拉回 → 字节数校验）·
                  **分区查看/编辑**（/proc/mtd + U-Boot mtdparts）
- `flash_now.py`  OpenWrt 在线刷机（设备 wget 固件 + sysupgrade）

## analysis/（逆向证据）

- `_ssh_usb_scripts.log`  原厂初始化脚本（CMCC_p1.sh 全文：GPIO4/5=VBUS、
  PORTSC 全写 0xA0、SCU 低功耗段）
- `uboot_usb_md.log`      U-Boot 下寄存器实测（IPPC/PHY 复位默认值、xHCI 断电态）

## 刷机安全网

原厂固件一直完好在 masterbank（flash 0xC0000，30MB），任何刷机流程都不写它；
随时可在 U-Boot 执行 `flash imgread 2048;bootm` 或用工具的「还原原厂系统」回退。
