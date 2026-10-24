#!/usr/bin/env bash
# demo-services.sh — 演示机服务开关
# ============================================================================
# off    停用并禁用演示不需要的服务（重启后保持精简）
# on     重新启用并启动全部被停服务（恢复日常形态）
# status 列出各服务当前状态与内存
#
# 停用范围：桌面栈（gdm/Xorg）、打印与外设、docker/containerd、snapd、
# 固件与内核维护、摄像头守护、代理与测速端口。
# 以下服务任何模式都不碰（受 PROTECTED 保护）：
#   ssh、mozart-usb-audio、NetworkManager、wpa_supplicant、nvpmodel、
#   nvfancontrol、autossh、串口控制台、systemd 基础、用户会话（含 pipewire）。
# 用法：tools/demo-services.sh off|on|status  （需要 root，脚本内部自行提权）
set -u

readonly -a DEMO_SERVICES=(
    gdm.service
    gnome-remote-desktop.service
    switcheroo-control.service
    accounts-daemon.service
    colord.service
    power-profiles-daemon.service
    rtkit-daemon.service
    bluetooth.service
    ModemManager.service
    avahi-daemon.service
    snapd.service
    snapd.socket
    snapd.seeded.service
    snapd.apparmor.service
    fwupd.service
    kerneloops.service
    udisks2.service
    upower.service
    docker.service
    docker.socket
    containerd.service
    clash-verge-service.service
    iperf3.service
    nvargus-daemon.service
)

# 任何模式下都拒绝操作的服务名（前缀匹配）。
readonly -a PROTECTED=(
    ssh mozart-usb-audio NetworkManager wpa_supplicant nvpmodel nvfancontrol
    nv-power nvpmodel autossh serial-getty@ dbus systemd- udev journald
    logind resolved networkd timesyncd haveged auditd rsyslog cron
)

mode="${1:-status}"

if [ "$(id -u)" -ne 0 ]; then
    exec sudo bash "$0" "$mode"
fi

protected() {
    local unit="$1" guard
    for guard in "${PROTECTED[@]}"; do
        case "$unit" in
            "$guard"*) return 0 ;;
        esac
    done
    return 1
}

case "$mode" in
off)
    for unit in "${DEMO_SERVICES[@]}"; do
        protected "$unit" && { echo "跳过受保护服务 $unit"; continue; }
        if systemctl list-unit-files "$unit" >/dev/null 2>&1 \
           && systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl stop "$unit" 2>/dev/null
            systemctl disable "$unit" 2>/dev/null
            echo "已停用 $unit"
        else
            echo "不存在，跳过 $unit"
        fi
    done
    # gdm 为 static 单元，禁用挡不住 graphical 目标拉起；切默认目标才能跨重启。
    systemctl set-default multi-user.target >/dev/null
    systemctl stop bluetooth.service 2>/dev/null
    echo "已切换默认目标 multi-user.target（恢复用 on）"
    echo "完成。内存："
    free -h
    ;;
on)
    for unit in "${DEMO_SERVICES[@]}"; do
        protected "$unit" && { echo "跳过受保护服务 $unit"; continue; }
        if systemctl list-unit-files "$unit" >/dev/null 2>&1 \
           && systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl enable "$unit" 2>/dev/null
            systemctl start "$unit" 2>/dev/null
            echo "已恢复 $unit"
        else
            echo "不存在，跳过 $unit"
        fi
    done
    systemctl set-default graphical.target >/dev/null
    echo "已切换默认目标 graphical.target"
    echo "完成。内存："
    free -h
    ;;
status)
    printf '%-32s %-10s %-10s\n' "SERVICE" "ACTIVE" "ENABLED"
    for unit in "${DEMO_SERVICES[@]}"; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            printf '%-32s %-10s %-10s\n' "$unit" \
                "$(systemctl is-active "$unit" 2>/dev/null || true)" \
                "$(systemctl is-enabled "$unit" 2>/dev/null || true)"
        else
            printf '%-32s %-10s %-10s\n' "$unit" "-" "absent"
        fi
    done
    echo
    echo "默认目标：$(systemctl get-default)"
    free -h
    ;;
*)
    echo "用法：$0 off|on|status" >&2
    exit 2
    ;;
esac
