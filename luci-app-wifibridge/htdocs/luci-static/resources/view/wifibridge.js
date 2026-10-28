'use strict';
'require view';
'require form';
'require fs';
'require ui';

function run(args) {
	return fs.exec_direct('/usr/libexec/wifibridge.sh', args);
}

return view.extend({
	render: function () {
		var self = this;

		var m = new form.Map('wifibridge', _('无线中继桥'),
			_('第一步：到「网络 → 无线」页面点击「扫描」，选择上游 WiFi 并「加入网络」（LuCI 原生功能）。第二步：回到本页选择中继模式，点「启用中继」。'));

		var s = m.section(form.TypedSection, 'wifibridge', _('中继配置'));
		s.anonymous = true;
		s.addremove = false;

		/* 状态节点：由 cfgvalue 返回并持有引用，后续直接更新内容 */
		var statusNode = E('span', {}, [ _('检测中...') ]);

		var info = s.option(form.DummyValue, '_sta', _('上游连接状态'));
		info.cfgvalue = function () { return statusNode; };

		var mode = s.option(form.ListValue, 'mode', _('中继模式'));
		mode.value('bridge', _('同网段透明桥 (推荐)'));
		mode.value('route', _('异网段 NAT'));
		mode.default = 'bridge';

		var lanip = s.option(form.Value, 'lan_ip', _('自定义 LAN IP（可选）'));
		lanip.default = '';
		lanip.rmempty = true;
		lanip.placeholder = '192.168.5.1';
		lanip.description = _('留空则按上游网段自动推算。填写后本机 LAN 固定使用该地址，可写 192.168.5.1 或 192.168.5.1/24');

		var oapply = s.option(form.Button, '_apply', _('启用中继'));
		oapply.inputstyle = 'apply';
		oapply.onclick = function () {
			/* 直接把界面上的当前值传给后端，避免与表单保存产生竞态导致自定义 IP 不生效 */
			var mv = '', lip = '';
			try {
				var mel = document.getElementById('widget.cbid.wifibridge.main.mode');
				if (mel) mv = mel.value;
			} catch (e) {}
			try {
				var lel = document.getElementById('widget.cbid.wifibridge.main.lan_ip');
				if (lel) lip = lel.value;
			} catch (e) {}
			if (!mv) { try { mv = (mode.getValue && mode.getValue()) || ''; } catch (e) {} }
			if (!lip) { try { lip = (lanip.getValue && lanip.getValue()) || ''; } catch (e) {} }
			return m.save().then(function () {
				return run(['apply', mv, lip]);
			}).then(function (res) {
				ui.addNotification('info', res);
				self.refresh();
			});
		};

		var ostop = s.option(form.Button, '_stop', _('停止中继'));
		ostop.inputstyle = 'reset';
		ostop.onclick = function () {
			return run(['stop']).then(function (r) {
				ui.addNotification('info', r);
				self.refresh();
			});
		};

		self.refresh = function () {
			run(['status']).then(function (res) {
				var d;
				try { d = JSON.parse(res); } catch (e) { return; }
				var frag;
				if (d.sta && d.sta.ssid) {
					frag = E('span', {}, [
						_('已加入上游') + ': ' + d.sta.ssid + ' [' + (d.sta.radio || '') + ']',
						E('br'),
						d.wwan_ip
							? (_('上游 IP') + ': ' + d.wwan_ip + ' | LAN: ' + (d.lan || '') + ' | ' +
								_('模式') + ': ' + (d.mode || '') +
								(d.lan_set ? ' | ' + _('自定义 LAN') + ': ' + d.lan_set : ''))
							: _('尚未获取到上游 IP（检查 WiFi 是否连接成功）')
					]);
				} else {
					frag = E('em', {}, _('未检测到 STA 接口 —— 请先到「网络 → 无线」页面点击「扫描」，选择上游 WiFi 并「加入网络」'));
				}
				while (statusNode.firstChild) statusNode.removeChild(statusNode.firstChild);
				statusNode.appendChild(frag);
			});
		};

		return m.render().then(function (node) {
			self.refresh();
			return node;
		});
	}
});
