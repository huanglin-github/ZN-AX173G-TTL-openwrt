#!/bin/sh
# Wireless Repeater Bridge backend
# 设计：扫描与连接完全复用 LuCI 原生「网络 -> 无线」页面的「加入网络」功能，
#       本脚本只负责中继编排（wwan 网络、relayd / NAT、LAN 地址适配），不负责扫描。
# Commands:
#   apply   -> 将现有 STA 接口配置为中继（模式取自 mode，LAN IP 取自 lan_ip，留空自动推算）
#   status  -> JSON 状态
#   stop    -> 停止中继并还原

LOGTAG=wifibridge

log() { logger -t "$LOGTAG" "$1"; }

json_escape() {
	local s="$1"
	s=$(printf '%s' "$s" | sed 's/\\/\\\\/g; s/"/\\"/g')
	printf '%s' "$s"
}

# 提取接口 IPv4 地址。
# 注意：jsonfilter 不支持含连字符的键名（如 ipv4-address 会报 Syntax error），故用 awk 提取。
if_ip() {
	ifstatus "$1" 2>/dev/null | awk '
		/"ipv4-address"/ { ina=1; next }
		ina && /"address"/ { v=$2; gsub(/[",]/,"",v); print v; exit }
	'
}

# 提取第一个 DNS 服务器
if_dns() {
	ifstatus "$1" 2>/dev/null | awk '
		/"dns-server"/ { ind=1; next }
		ind && /[[:space:]]*"/ { v=$1; gsub(/[",]/,"",v); print v; exit }
	'
}

# 提取默认网关（取 route 中首个 gateway）。同样不用 jsonfilter，避免其解析数组不稳定。
if_gw() {
	ifstatus "$1" 2>/dev/null | awk '
		/"gateway"/ { v=$2; gsub(/[",]/,"",v); if (v!="") { print v; exit } }
	'
}

# 找到第一个 STA 接口（由原生无线页面的「加入网络」创建）
find_sta() {
	uci show wireless 2>/dev/null | grep "mode='sta'" | head -n1 | cut -d. -f2 | cut -d= -f1
}

sta_json() {
	local sta="$1" ssid radio
	[ -z "$sta" ] && { printf 'null'; return; }
	ssid=$(uci get wireless.$sta.ssid 2>/dev/null)
	radio=$(uci get wireless.$sta.device 2>/dev/null)
	printf '{"section":"%s","ssid":"%s","radio":"%s"}' \
		"$(json_escape "$sta")" "$(json_escape "$ssid")" "$(json_escape "$radio")"
}

ensure_wan_zone() {
	local iface="$1" z
	for z in $(uci show firewall 2>/dev/null | grep "=zone" | cut -d. -f2 | cut -d= -f1); do
		if uci get firewall.$z.name 2>/dev/null | grep -qi wan; then
			uci add_list firewall.$z.network="$iface" 2>/dev/null
			uci set firewall.$z.masq='1'
			uci set firewall.$z.mtu_fix='1'
			break
		fi
	done
	uci commit firewall
}

do_apply() {
	# 参数优先（前端直接传入 mode / lan_ip），避免与表单保存产生竞态；
	# 未传参时再回退到读取 UCI。
	local argmode="$1"
	local argip="$2"

	local mode="$argmode"
	[ -z "$mode" ] && mode=$(uci get wifibridge.main.mode 2>/dev/null)
	[ "$mode" = "route" ] || mode="bridge"

	# 自定义 LAN IP（可选；留空则按上游网段自动推算）
	local lanip="$argip"
	[ -z "$lanip" ] && lanip=$(uci get wifibridge.main.lan_ip 2>/dev/null)
	if [ -n "$lanip" ] && ! printf '%s' "$lanip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$'; then
		echo '{"error":"bad_ip","msg":"LAN IP 格式无效，应为 192.168.5.1 或 192.168.5.1/24"}'
		return 1
	fi
	# 未指定掩码时补 /24
	case "$lanip" in
		''|*/*) ;;
		*) lanip="$lanip/24" ;;
	esac

	local sta; sta=$(find_sta)
	if [ -z "$sta" ]; then
		echo '{"error":"no_sta","msg":"未检测到 STA 接口，请先到 网络->无线 页面扫描并加入上游 WiFi"}'
		return 1
	fi

	# 记录 STA 原所属网络与 LAN 原地址，便于 stop 时还原
	local orig; orig=$(uci get wireless.$sta.network 2>/dev/null)
	[ -z "$orig" ] && orig="lan"
	uci set wifibridge.main.sta_orig_net="$orig"
	uci set wifibridge.main.sta_section="$sta"
	uci set wifibridge.main.lan_orig="$(uci get network.lan.ipaddr 2>/dev/null)"

	uci set wireless.$sta.network='wwan'
	uci commit wireless

	uci -q delete network.wwan
	uci set network.wwan=interface
	uci set network.wwan.proto='dhcp'
	uci set network.wwan.metric='20'
	uci commit network

	wifi reload 2>/dev/null
	sleep 3

	local up_ip="" i=0
	while [ $i -lt 25 ]; do
		up_ip=$(if_ip wwan)
		[ -n "$up_ip" ] && break
		sleep 2; i=$((i+1))
	done
	if [ -z "$up_ip" ]; then
		echo '{"error":"no_ip","msg":"上游未获取到 IP，请检查 WiFi 是否连接成功（密码/信号）"}'
		return 1
	fi

	local up_gw up_dns
	up_gw=$(if_gw wwan)
	up_dns=$(if_dns wwan)
	[ -z "$up_dns" ] && up_dns="$up_gw"

	local third; third=$(echo "$up_ip" | cut -d. -f3)

	# 透明桥要求 LAN 与上游处于同一网段。若用户自定义了不同网段的 IP，
	# 桥接会导致本机 DHCP 关闭、下游拿不到地址且访问不到管理页，
	# 因此这种情况下自动改用 NAT(route) 模式，避免失联。
	local note=""
	if [ "$mode" = "bridge" ] && [ -n "$lanip" ]; then
		local lt ut
		lt=$(echo "$lanip" | cut -d. -f3 | cut -d/ -f1)
		ut=$(echo "$up_ip" | cut -d. -f3)
		if [ "$lt" != "$ut" ]; then
			mode="route"
			note="bridge_auto_route"
		fi
	fi

	if [ "$mode" = "route" ]; then
		if [ -n "$lanip" ]; then
			uci set network.lan.ipaddr="$lanip"
		else
			third=$((third+1)); [ $third -ge 255 ] && third=2
			uci set network.lan.ipaddr="192.168.$third.1/24"
		fi
		uci -q delete network.lan.gateway
		uci -q delete network.lan.dns
		uci set dhcp.lan.ignore='0'
		uci -q delete network.stabridge
		# IPv6：本机下发（server），不做中继
		uci set dhcp.lan.ra='server'
		uci set dhcp.lan.dhcpv6='server'
		uci set dhcp.lan.ndp='hybrid'
		uci -q delete dhcp.wwan
		uci set network.wwan.ipv6='auto'
		ensure_wan_zone wwan
	else
		if [ -n "$lanip" ]; then
			uci set network.lan.ipaddr="$lanip"
		else
			uci set network.lan.ipaddr="192.168.$third.2/24"
		fi
		uci set network.lan.gateway="$up_gw"
		uci set network.lan.dns="$up_dns"
		uci set dhcp.lan.ignore='1'
		uci -q delete network.stabridge
		uci set network.stabridge=interface
		uci set network.stabridge.proto='relay'
		uci set network.stabridge.network='lan wwan'

		# IPv6 透传：把上游的 RA / DHCPv6 / NDP 中继到 LAN（中继，不是 NAT）
		uci set network.wwan.ipv6='auto'
		uci set dhcp.lan.ra='relay'
		uci set dhcp.lan.dhcpv6='relay'
		uci set dhcp.lan.ndp='relay'
		uci -q delete dhcp.wwan
		uci set dhcp.wwan=dhcp
		uci set dhcp.wwan.interface='wwan'
		uci set dhcp.wwan.ra='relay'
		uci set dhcp.wwan.dhcpv6='relay'
		uci set dhcp.wwan.ndp='relay'
		uci set dhcp.wwan.master='1'
	fi

	uci set wifibridge.main.enabled='1'
	# 回写模式与自定义 IP，保证持久化（供开机自动连接使用）
	uci set wifibridge.main.mode="$mode"
	if [ -n "$lanip" ]; then
		uci set wifibridge.main.lan_ip="${lanip%%/*}"
	else
		uci -q delete wifibridge.main.lan_ip
	fi
	uci commit network
	uci commit dhcp
	uci commit wifibridge

	/etc/init.d/network restart 2>/dev/null
	sleep 2
	echo "{\"ok\":true,\"mode\":\"$mode\",\"upstream\":\"$up_ip\",\"lan\":\"$(uci get network.lan.ipaddr)\",\"note\":\"$note\"}"
}

do_status() {
	local sta up_ip lan en mode net
	sta=$(find_sta)
	up_ip=$(if_ip wwan)
	# STA 若还挂在原生页面分配的其他网络（尚未启用中继），取其所在网络的 IP
	if [ -z "$up_ip" ] && [ -n "$sta" ]; then
		net=$(uci get wireless.$sta.network 2>/dev/null)
		[ -n "$net" ] && up_ip=$(if_ip "$net")
	fi
	lan=$(uci get network.lan.ipaddr 2>/dev/null)
	en=$(uci get wifibridge.main.enabled 2>/dev/null)
	mode=$(uci get wifibridge.main.mode 2>/dev/null)
	local lanset; lanset=$(uci get wifibridge.main.lan_ip 2>/dev/null)
	printf '{"sta":%s,"wwan_ip":"%s","lan":"%s","enabled":"%s","mode":"%s","lan_set":"%s"}' \
		"$(sta_json "$sta")" "$up_ip" "$lan" "$en" "$mode" "$(json_escape "$lanset")"
}

do_stop() {
	local sta; sta=$(uci get wifibridge.main.sta_section 2>/dev/null)
	[ -z "$sta" ] && sta=$(find_sta)
	local orig; orig=$(uci get wifibridge.main.sta_orig_net 2>/dev/null)
	[ -z "$orig" ] && orig="lan"
	[ -n "$sta" ] && uci set wireless.$sta.network="$orig"
	uci -q delete network.wwan
	uci -q delete network.stabridge
	# 还原启用中继前的 LAN 地址
	local origlan; origlan=$(uci get wifibridge.main.lan_orig 2>/dev/null)
	[ -z "$origlan" ] && origlan="192.168.1.1/24"
	uci set network.lan.ipaddr="$origlan"
	uci -q delete network.lan.gateway
	uci -q delete network.lan.dns
	uci set dhcp.lan.ignore='0'
	# 还原 IPv6 为本机下发，去掉中继
	uci set dhcp.lan.ra='server'
	uci set dhcp.lan.dhcpv6='server'
	uci set dhcp.lan.ndp='hybrid'
	uci -q delete dhcp.wwan
	uci -q delete network.wwan.ipv6
	uci set wifibridge.main.enabled='0'
	uci commit wireless
	uci commit network
	uci commit dhcp
	uci commit wifibridge
	/etc/init.d/network restart 2>/dev/null
	echo '{"ok":true,"stopped":true}'
}

case "$1" in
	apply) shift; do_apply "$@" ;;
	status) do_status ;;
	stop) do_stop ;;
	*) echo '{"error":"unknown command"}' ;;
esac
