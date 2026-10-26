# 移动兆能 ZN-AX173G（EcoNet EN7523）TTL 刷 OpenWrt 完整源码

本仓库是**可以直接一次编译出固件的完整 OpenWrt 源码树**（6.18 快照 + feeds + 配置），
已包含针对移动兆能 ZN-AX173G 路由器的全部移植改动，以及 PC 端 TTL 刷机工具。

> OpenWrt 官方英文说明见 [docs/README-openwrt-upstream.md](docs/README-openwrt-upstream.md)

---

## ⚠️ 通用性说明

| 内容 | 适用范围 |
|---|---|
| `ttl_tools/`（znui.py / znflash.py 等 TTL 工具） | **同型号 ZN-AX173G 通用**（串口参数、U-Boot 凭据、flash 布局为出厂一致配置） |
| 本仓库编译出的固件 | **仅限 ZN-AX173G 这一块板**，刷其他型号可能不启动或损坏外设 |

## 原厂系统 TTL 密码（这台机器的出厂凭据）

| 场景 | 用户名 | 密码 |
|---|---|---|
| U-Boot 命令行（串口打断 autoboot 后） | telecomadmin | nE7jA%5m |
| 原厂系统 telnet / 串口登录 | admin | aDm8H%MdA |
| 原厂系统提权（su） | — | @Huang8369687 |

> 原厂提示符：U-Boot 为 `ECNT>`；原厂系统 telnet 登录后用 `su` 提权。
> 原厂调试命令：`sys memrl <地址>` 读寄存器、`sys memwl <地址> <值>` 写寄存器、
> `echo active N V > /proc/tc3162/gpio_output` 控制 GPIO。

## Flash 分区表（SPI-NAND 256MB）

| 分区 | 偏移 | 大小 | 说明 |
|---|---|---|---|
| bootloader | 0x0 | 0x80000 | U-Boot（不要动） |
| romfile | 0x80000 | 0x40000 | 原厂配置 |
| **masterbank** | **0xC0000** | **0x1E00000 (30MB)** | **原厂完整系统（kernel+rootfs），安全网** |
| firmware | 0x1EC0000 | 0xBF00000 | OpenWrt（kernel 0x3A0000 + rootfs） |
| reservearea | 0xDDC0000 | 0x240000 | bootflag / EEPROM（不要动） |

## TTL 刷机方法

**方法一：PC 工具（推荐）** —— `ttl_tools/znui.py`

```
python znui.py
1. 选 COM 口 → 连接（115200）
2. 「立即刷机」：选 sysupgrade.bin → 自动重启打断进 U-Boot →
   TFTP 上传 → 擦写 → 回读 sha1 校验 → 启动验证
3. 「备份原厂固件」：把 masterbank 30MB 原厂系统完整备份到 PC（crc32 双端校验）
4. 「从备份还原原厂固件」：把备份写回 flash（三重 crc32 校验）
```

**方法二：U-Boot 手动**

```
串口 115200，开机按任意键打断 autoboot → telecomadmin / nE7jA%5m
ECNT> setenv ipaddr 192.168.1.1; setenv serverip <PC_IP>
ECNT> tftpboot 0x88000000 znfw.bin
ECNT> flash erase 0x1ec0000 <128K对齐长度>
ECNT> flash write 0x1ec0000 <字节数> 0x88000000
ECNT> bootm 0x88000000
```

安全网：原厂固件一直完好在 masterbank，刷 OpenWrt 不碰它。
引导切回原厂：U-Boot 里 `setenv bootcmd "flash imgread 2048;bootm"; saveenv; reset`。

## 移植成果（相对原厂/上游主线）

- ✅ **双 USB2.0 口全部可用**：
  1. U2 PHY 双通道初始化（第二通道寄存器在 base+0x1000）
  2. VBUS 5V 由 GPIO4/5 控制（probe 直接写 GPIO 控制器 0x1fbf0200）
  3. PORTSC RxDetect poke（0x420/0x440 写 0xA0，跳过工作口 0x430）
- ✅ 面板 LED 全部工作（LEDS_GPIO + DTS default-state）
- ✅ NPU 硬件加速（econet-npu，rv32 固件加载）
- ✅ 326 个软件包（luci-app-diskman、openclash、ksmbd、smartdns、argon 等）
- ✅ WAN/LAN 口序修正、mt7530 PHY LED
- ❌ 未包含：passwall/ssr-plus/filebrowser（按需自行勾选）

## 一次性编译（Linux，Debian/Ubuntu 示例）

```bash
# 依赖
sudo apt install build-essential clang flex bison g++ gawk gcc-multilib \
  g++-multilib gettext git libncurses-dev libssl-dev python3-distutils \
  rsync unzip zlib1g-dev file wget

git clone https://github.com/huanglin-github/ZN-AX173G-TTL-openwrt.git
cd ZN-AX173G-TTL-openwrt

make defconfig        # 应用仓库自带 .config
make download -j8     # 预下载源码包（首次建议）
make -j$(nproc)       # 完整编译，约 30-60 分钟

# 产物
bin/targets/airoha/en7523/openwrt-airoha-en7523-airoha_en7523-ax3000-router-squashfs-sysupgrade.bin
```

刷入：Web 界面「系统→备份/升级」上传，或用 `ttl_tools/znui.py` 的 TTL 刷机。

## 关键改动位置（改硬件适配时看这里）

| 文件 | 内容 |
|---|---|
| `target/linux/airoha/patches-6.18/220-07-phy-airoha-*.patch` | USB 双通道/VBUS GPIO/RxDetect poke |
| `target/linux/airoha/dts/en7523-ax3000-router.dts` | 板级设备树（usb_phy reg 必须 0x2000） |
| `target/linux/airoha/en7523/config-6.18` | LEDS_GPIO 等内核配置 |
| `ttl_tools/` | PC 端工具（不属于编译树） |

## 踩坑记录（改其他机型前必读）

1. **SCU 0x1fa201e4/e8 不要 raw 写**——原厂脚本里属于低功耗段，6.18 内核下
   整字写导致整机静默（串口/网络全断）。
2. **usb_phy reg 必须 ≥0x136c 对齐到 0x2000**——regmap 窗口 0x1000 时写第二
   通道偏移越界 → kernel oops → bootloop。
3. xHCI 端口停在 Disabled 不会自动重扫，VBUS 上电后必须写 PORTSC RxDetect。
4. U-Boot 登录不要提前发回车（空用户名会把后续凭据全部错位）。
