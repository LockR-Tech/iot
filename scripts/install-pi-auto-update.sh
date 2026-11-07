#!/usr/bin/env bash
# Cài tự cập nhật code trên Pi: dịch vụ lockr-auto-update + timer chạy 2 phút sau khi bật máy, sau đó mỗi 5 phút.
# Chạy MỘT lần trên Pi:   sudo bash ~/iot/scripts/install-pi-auto-update.sh
# Gỡ:                     sudo systemctl disable --now lockr-auto-update.timer
# Chạy ngay / xem log:    sudo systemctl start lockr-auto-update ; journalctl -u lockr-auto-update -n 50 --no-pager
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Cần chạy bằng sudo"; exit 1; }

USER_NAME="${SUDO_USER:-lockr}"
SCRIPT="/home/$USER_NAME/iot/scripts/pi-auto-update.sh"
[ -f "$SCRIPT" ] || { echo "Không thấy $SCRIPT — kéo code iot mới nhất trước"; exit 1; }
chmod +x "$SCRIPT"

cat > /etc/systemd/system/lockr-auto-update.service <<EOF
[Unit]
Description=Lock.R: tự kéo code iot (main) và cập nhật kiosk / bộ điều khiển
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=LOCKR_USER=$USER_NAME
ExecStart=/bin/bash $SCRIPT
TimeoutStartSec=20min
Nice=10
EOF

cat > /etc/systemd/system/lockr-auto-update.timer <<'EOF'
[Unit]
Description=Lock.R: kiểm tra code iot mới mỗi 5 phút

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now lockr-auto-update.timer
echo "Đã bật lockr-auto-update.timer:"
systemctl list-timers lockr-auto-update.timer --no-pager | head -3
echo "Chạy thử ngay một lần..."
systemctl start lockr-auto-update || true
journalctl -u lockr-auto-update -n 20 --no-pager
