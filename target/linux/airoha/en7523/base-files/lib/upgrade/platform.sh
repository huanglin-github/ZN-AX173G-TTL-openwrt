#
# Copyright (C) 2024 OpenWrt.org
#
# Ax3000 router: the DTS has no chosen/rootdisk and the fitblk driver is not
# bound (/dev/fit0, /dev/fitrw absent), so the generic fit_do_upgrade path
# returns early and sysupgrade always fails. The firmware partition is already
# split by mtdsplit into kernel/rootfs/rootfs_data, and the sysupgrade image is
# kernel+pad+rootfs, so writing the image straight into "firmware" is exactly
# equivalent to the previously used "mtd write firmware".
# This touches nothing but the firmware partition: bootloader, romfile,
# masterbank and reservearea are read-only in the DTS, and no fw_setenv is
# involved, so the U-Boot environment (bootcmd) is never modified.

RAMFS_COPY_BIN='fitblk fit_check_sign'

REQUIRE_IMAGE_METADATA=1

platform_check_image() {
	local board=$(board_name)

	[ "$#" -gt 1 ] && return 1

	case "$board" in
	airoha,en7523-ax3000-router)
		[ "$(get_magic_long "$1")" = "d00dfeed" ] || {
			echo "Invalid image type."
			return 1
		}
		return 0
		;;
	esac

	echo "Unsupported board: $board"
	return 1
}

platform_do_upgrade() {
	local board=$(board_name)

	case "$board" in
	airoha,en7523-ax3000-router)
		PART_NAME="firmware"
		default_do_upgrade "$1"
		;;
	*)
		default_do_upgrade "$1"
		;;
	esac
}

platform_pre_upgrade() {
	sync
}
