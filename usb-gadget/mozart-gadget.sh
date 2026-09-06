#!/bin/bash
# Mozart AI Sound Card — UAC2 USB gadget 起停脚本
# 用法: sudo ./mozart-gadget.sh start|stop
# 效果: 主机端出现一只 USB 麦克风 "Mozart AI Sound Card"（48kHz/2ch）
#       板端出现 ALSA 播放卡 "UAC2 Gadget"，写它 = 主机录到的声音
set -e
GADGET=mozart
CFGFS=/sys/kernel/config/usb_gadget
UDC=3550000.usb

stop() {
    if [ -d "$CFGFS/$GADGET" ]; then
        echo "" > "$CFGFS/$GADGET/UDC" 2>/dev/null || true
        rm -f "$CFGFS/$GADGET/configs/c.1/uac2.0"
        rmdir "$CFGFS/$GADGET/configs/c.1" "$CFGFS/$GADGET/functions/uac2.0" \
              "$CFGFS/$GADGET/strings/0x409" "$CFGFS/$GADGET" 2>/dev/null || true
        echo "mozart gadget removed"
    fi
}

start() {
    [ -d "$CFGFS/$GADGET" ] && { echo "gadget exists"; exit 1; }
    modprobe libcomposite 2>/dev/null || true
    modprobe u_audio 2>/dev/null || true
    modprobe f_uac2 2>/dev/null || true

    mkdir "$CFGFS/$GADGET"
    cd "$CFGFS/$GADGET"

    echo 0x1d6b > idVendor   # Linux Foundation
    echo 0x0104 > idProduct  # Multifunction Composite Gadget
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB     # USB 2.0（UAC2 高带宽足够）

    mkdir strings/0x409
    echo "Mozart Project"   > strings/0x409/manufacturer
    echo "Mozart AI Sound Card" > strings/0x409/product

    mkdir functions/uac2.0
    # p_ = gadget 侧 playback → 主机录到的"麦克风"（变声输出走这里）
    echo 0x3   > functions/uac2.0/p_chmask   # stereo
    echo 48000 > functions/uac2.0/p_srate
    echo 2     > functions/uac2.0/p_ssize    # S16_LE
    # c_ = gadget 侧 capture → 主机的"扬声器"（预留，板端可录主机音频）
    echo 0x3   > functions/uac2.0/c_chmask
    echo 48000 > functions/uac2.0/c_srate
    echo 2     > functions/uac2.0/c_ssize

    mkdir configs/c.1
    echo 120 > configs/c.1/MaxPower
    ln -s functions/uac2.0 configs/c.1/

    # 释放 l4t 默认 gadget 占用的 UDC
    for g in "$CFGFS"/*/; do
        [ "$(cat "$g/UDC" 2>/dev/null)" = "$UDC" ] && echo "" > "$g/UDC"
    done
    echo "$UDC" > UDC
    echo "bound to $UDC"
}

case "$1" in
    start) start ;;
    stop)  stop ;;
    *) echo "usage: $0 start|stop"; exit 1 ;;
esac
