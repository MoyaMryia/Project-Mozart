#!/bin/bash
# Build and validate candidates only; never install, bind, unload or reboot.
set -euo pipefail
root=$(dirname "$(dirname "$(readlink -f "$0")")")
make -C "$root/candidate" -j2
python3 -m unittest discover -s "$root/tests" -v
bash -n "$root/candidate/mozart-gadget.sh"
cmp "$root/tegra-xudc.c" "$root/candidate/tegra-xudc.c"
readelf -n "$root/candidate/tegra-xudc.ko"
sha256sum "$root/candidate/tegra-xudc.ko"
