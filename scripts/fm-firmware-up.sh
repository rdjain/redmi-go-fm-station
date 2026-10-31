#!/system/bin/sh
# fm-firmware-up.sh : start the FM firmware/transport the way the stock app does (run as root, Android side)
setprop hw.fm.mode normal
setprop hw.fm.init 0
setprop ctl.start fm_dl
i=0
while [ "$(getprop hw.fm.init)" != "1" ] && [ $i -lt 100 ]; do sleep 0.2; i=$((i+1)); done
echo "hw.fm.init=$(getprop hw.fm.init) after $i polls; fm_dl=$(getprop init.svc.fm_dl)"