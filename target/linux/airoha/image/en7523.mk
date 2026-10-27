define Target/Description
	Build firmware images for Airoha EN7523 ARM based boards.
endef

define Device/airoha_en7523-evb
  DEVICE_VENDOR := Airoha
  DEVICE_MODEL := EN7523 Evaluation Board
  DEVICE_DTS := en7523-evb
endef
TARGET_DEVICES += airoha_en7523-evb

define Device/airoha_en7523-ax3000-router
  DEVICE_VENDOR := Airoha
  DEVICE_MODEL := EN7523 AX3000 Router
  DEVICE_DTS := en7523-ax3000-router
  DEVICE_PACKAGES := kmod-ubifs kmod-ubi ubi-utils kmod-mtdblock \
	kmod-mt7915e kmod-mt7915-firmware kmod-mt7916-firmware fitblk \
	kmod-airoha-npu kmod-input-gpio-keys-polled \
	kmod-usb-ledtrig-usbport kmod-usb-storage kmod-usb-storage-uas kmod-scsi-core \
	kmod-fs-vfat kmod-nls-cp437 kmod-nls-iso8859-1 kmod-fs-exfat kmod-fs-ntfs3 \
	kmod-fs-ext4 kmod-fs-ksmbd ksmbd-server block-mount usbutils \
	wpad-basic-mbedtls luci \
	luci-i18n-base-zh-cn luci-i18n-firewall-zh-cn luci-i18n-package-manager-zh-cn \
	luci-theme-argon luci-i18n-argon-config-zh-cn \
	luci-app-ddns ddns-scripts-cloudflare luci-i18n-ddns-zh-cn \
	luci-app-aria2 ariang luci-i18n-aria2-zh-cn \
	kmod-wireguard wireguard-tools luci-proto-wireguard \
	igmpproxy udpxy luci-app-udpxy luci-i18n-udpxy-zh-cn xupnpd \
	luci-app-wifischedule luci-i18n-wifischedule-zh-cn \
	luci-app-travelmate luci-i18n-travelmate-zh-cn \
	luci-app-ttyd ttyd luci-i18n-ttyd-zh-cn \
	luci-app-wol etherwake luci-i18n-wol-zh-cn \
	luci-app-ksmbd luci-i18n-ksmbd-zh-cn \
	luci-app-hd-idle hd-idle luci-i18n-hd-idle-zh-cn \
	luci-app-openclash smartdns luci-app-diskman \
	kmod-tun \
	relayd luci-proto-relay \
	mwan3 luci-app-mwan3 \
	luci-app-upnp \
	zerotier luci-app-zerotier \
	softethervpn5-server \
	minidlna luci-app-minidlna \
	transmission-web luci-app-transmission \
	nfs-kernel-server kmod-nfsd \
	kmod-cifs luci-app-cifs-mount \
	p910nd kmod-usb-printer luci-app-p910nd \
	samba4 luci-app-samba4 \
	luci-app-nlbwmon luci-app-wrtbwmon \
	luci-app-sqm luci-app-qos luci-app-statistics bcp38 \
	luci-app-easytier luci-app-wechatpush
endef
TARGET_DEVICES += airoha_en7523-ax3000-router
