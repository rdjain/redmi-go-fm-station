#!/system/bin/sh
# Magisk late_start service script: bring the FM / YouTube server up after every boot.
# Install as /data/adb/service.d/99-fm-server.sh (must be executable). Log: /data/local/tmp/fm-boot.log
FM=/data/local/fm
LOG=/data/local/tmp/fm-boot.log

{
  echo "=== boot $(date '+%F %T') ==="

  # wait for Android to finish booting
  i=0
  while [ "$(getprop sys.boot_completed)" != "1" ] && [ $i -lt 180 ]; do sleep 2; i=$((i + 1)); done
  sleep 15

  # leftovers from before the reboot
  rm -f /data/local/tmp/fm-watchdog.pid /data/local/tmp/fm-stream.pid

  settings put global wifi_sleep_policy 2          # keep Wi-Fi on while the screen is off
  echo fmserver > /sys/power/wake_lock             # keep the CPU awake

  # order matters: Tailscale (remote access), audio hub (needs the chroot), tuner (needs FM firmware), watchdog
  $FM/fm-tailscale.sh start
  $FM/fm-stream.sh start
  $FM/fm-tuner.sh start
  $FM/fm-lan.sh start                              # no-op unless FM_LAN_LISTEN=1 in fm.env
  $FM/fm-watchdog.sh start

  echo "=== boot sequence done $(date '+%T') ==="
} >> $LOG 2>&1

# keep the log small
if [ "$(wc -c < $LOG)" -gt 100000 ]; then tail -n 200 $LOG > $LOG.tmp && mv $LOG.tmp $LOG; fi
