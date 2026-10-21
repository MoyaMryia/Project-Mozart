#!/usr/bin/env bash
# USB 桥重连验收。
#   默认：       重启主机侧守护进程，确认两端帧计数恢复。
#   --check-only：只观察计数是否增长，不重启服务（供开机自检使用）。
#   --reboot：   重启主机；重启后由开机自检服务记录结果。
#
# 用法：usb-audio/verify-reconnect.sh [--check-only|--reboot]
set -euo pipefail

jetson="${MOZART_JETSON:-jetson}"
wait_seconds="${MOZART_WAIT:-10}"

counters() {
    local host_line jetson_line
    host_line=$(journalctl -u mozart-usb-audio -n 1 -o cat | tail -1)
    jetson_line=$(ssh "$jetson" "journalctl -u mozart-usb-audio -n 1 --no-pager -o cat | tail -1")
    echo "$host_line|$jetson_line"
}

frames() {
    python3 -c 'import re,sys; n=[int(v) for v in re.findall(r"\d+", sys.argv[1])]; print(" ".join(map(str,n[:2])) if len(n)>=2 else "0 0")' "$1"
}

growing() {
    read -r sent received <<<"$(frames "$1")"
    [ "$sent" -gt 0 ] && [ "$received" -gt 0 ]
}

case "${1:-}" in
--check-only)
    first=$(counters)
    sleep 10
    second=$(counters)
    echo "$(date '+%F %T') 开机自检"
    echo "  第一次：$first"
    echo "  第二次：$second"
    host_ok=false; jetson_ok=false
    growing "${second%%|*}" && host_ok=true
    growing "${second##*|}" && jetson_ok=true
    if $host_ok && $jetson_ok; then
        echo "  结果：两个方向都在出帧"
        exit 0
    fi
    echo "  结果：至少一个方向没有出帧"
    exit 1
    ;;
--reboot)
    echo "重启前：$(counters)"
    echo "重启后由 mozart-usb-reconnect-check.service 记录到 /var/log/mozart-usb-reconnect.log"
    sudo -n systemctl reboot
    exit 0
    ;;
esac

before=$(counters)
echo "重启前：$before"
echo "重启主机侧守护进程…"
sudo -n systemctl restart mozart-usb-audio
sleep "$wait_seconds"
after=$(counters)
echo "重启后：$after"

host_ok=false; jetson_ok=false
growing "${after%%|*}" && host_ok=true
# Jetson 计数不会清零，比较重启前后总和。
jeteson_before=$(frames "${before##*|}" | awk '{print $1+$2}')
jetson_after=$(frames "${after##*|}" | awk '{print $1+$2}')
[ "$jetson_after" -gt "$jeteson_before" ] && jetson_ok=true

if $host_ok && $jetson_ok; then
    echo "恢复成功：两个方向都在出帧"
    exit 0
fi
echo "恢复失败：主机=$host_ok Jetson=$jetson_ok"
exit 1
