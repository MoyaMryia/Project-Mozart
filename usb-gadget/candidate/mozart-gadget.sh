#!/bin/bash
# Mozart AI Sound Card — UAC2 USB gadget 起停脚本
# 用法: sudo ./mozart-gadget.sh start|stop
# 效果: 主机端出现一只 USB 麦克风 "Mozart AI Sound Card"（48kHz/2ch）
#       板端出现 ALSA 播放卡 "UAC2 Gadget"，写它 = 主机录到的声音
set -e -o pipefail
GADGET=mozart
CFGFS=/sys/kernel/config/usb_gadget
UDC=3550000.usb

# Candidate only: never start this unvalidated driver path at boot.
[ "${MOZART_USB_EXPERIMENT:-}" = 1 ] || {
    echo "candidate requires MOZART_USB_EXPERIMENT=1 and a disconnected host"
    exit 1
}
[ "$(cat "/sys/class/udc/$UDC/state")" = "not attached" ] || {
    echo "disconnect the USB host before changing the gadget"
    exit 1
}

stop() {
    if [ -d "$CFGFS/$GADGET" ]; then
        echo "" > "$CFGFS/$GADGET/UDC"
        rm -f "$CFGFS/$GADGET/configs/c.1/uac2.0"
        rmdir "$CFGFS/$GADGET/configs/c.1" "$CFGFS/$GADGET/functions/uac2.0" \
              "$CFGFS/$GADGET/strings/0x409" "$CFGFS/$GADGET"
        echo "mozart gadget removed"
    fi
}

start() {
    [ -d "$CFGFS/$GADGET" ] && { echo "gadget exists"; exit 1; }
    for g in "$CFGFS"/*/; do
        if [ "$(cat "$g/UDC" 2>/dev/null)" = "$UDC" ]; then
            echo "UDC already owned by $g; refusing to detach it"
            exit 1
        fi
    done
    # Never silently use the installed, crash-prone modules.
    candidate_dir=$(dirname "$(readlink -f "$0")")
    for module in tegra-xudc u_audio f_uac2; do
        expected=$(objcopy -O binary --only-section=.note.gnu.build-id \
            "$candidate_dir/$module.ko" /dev/stdout | od -An -v -tx1)
        loaded=$(od -An -v -tx1 \
            "/sys/module/${module//-/_}/notes/.note.gnu.build-id")
        if [ -z "$expected" ] || [ "$expected" != "$loaded" ]; then
            echo "loaded $module does not match candidate; refusing to start"
            exit 1
        fi
    done

    mkdir "$CFGFS/$GADGET"
    cd "$CFGFS/$GADGET"

    echo 0x1d6b > idVendor   # Linux Foundation
    echo 0x0104 > idProduct  # Multifunction Composite Gadget
    echo 0x0100 > bcdDevice
    echo 0xef > bDeviceClass
    echo 0x02 > bDeviceSubClass
    echo 0x01 > bDeviceProtocol
    echo 0x0200 > bcdUSB     # Descriptor baseline, not a link-speed limit

    mkdir strings/0x409
    echo "Mozart Project"   > strings/0x409/manufacturer
    echo "Mozart AI Sound Card" > strings/0x409/product
    echo "MOZART0001" > strings/0x409/serialnumber

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

    echo "$UDC" > UDC
    echo "bound to $UDC"
}

case "$1" in
    start) start ;;
    stop)  stop ;;
    *) echo "usage: $0 start|stop"; exit 1 ;;
esac
