#!/usr/bin/env bash
# 建立 FunctionFS bulk gadget；可重复执行。不绑定 UDC（由守护进程绑定）。
set -euo pipefail

G=/sys/kernel/config/usb_gadget
MNT=/dev/ffs-mozart

systemctl stop nv-l4t-usb-device-mode.service 2>/dev/null || true
systemctl disable nv-l4t-usb-device-mode.service 2>/dev/null || true

modprobe usb_f_fs

if [ -e "$G/mozart" ] && [ ! -e "$G/mozart/functions/ffs.mozart" ]; then
    echo "" > "$G/mozart/UDC" 2>/dev/null || true
    mountpoint -q "$MNT" && umount "$MNT"
    rm -rf "$G/mozart"
fi

if [ ! -e "$G/mozart" ]; then
    mkdir "$G/mozart"
    cd "$G/mozart"
    echo 0x1d6b > idVendor
    echo 0x0105 > idProduct
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB
    mkdir -p strings/0x409
    echo "MOZARTFFS01" > strings/0x409/serialnumber
    echo "Mozart" > strings/0x409/manufacturer
    echo "Mozart FFS bulk" > strings/0x409/product
    mkdir -p configs/c.1/strings/0x409
    echo "bulk" > configs/c.1/strings/0x409/configuration
    mkdir functions/ffs.mozart
    ln -s functions/ffs.mozart configs/c.1/
fi

mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
    mount -t functionfs mozart "$MNT"
fi
echo "gadget 就绪：$MNT"
