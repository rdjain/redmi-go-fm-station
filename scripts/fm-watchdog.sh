#!/system/bin/sh
# Keeps the FM/YouTube box alive. Run as root.   fm-watchdog.sh start | stop | status | run
#  - holds a CPU wakelock
#  - protects the important processes from Android's low-memory killer
#  - restarts the audio hub / tuner daemon / tailscaled if they die (the daemon resumes the YouTube stream itself)
#  - keeps the battery between 70 and 80 % (charging switched off above 80, on again below 70, off when hot)
#  - keeps Wi-Fi power-save off (latency and jitter)
#  - toggles Wi-Fi if the internet stays down for ~1 minute, restarts tailscaled if that does not help
FM=/data/local/fm
LOG=/data/local/tmp/fm-watchdog.log
PIDF=/data/local/tmp/fm-watchdog.pid
INTERVAL=15

# Battery guard: the phone stays on the charger 24/7, so keep the cell between BATT_RESUME and BATT_MAX percent.
BATT=/sys/class/power_supply/battery
BATT_MAX=80       # stop charging at or above this level
BATT_RESUME=70    # allow charging again at or below this level
BATT_HOT=450      # tenths of a degree C: no charging while the cell is warmer than 45.0 C
BATT_LOW=15       # never leave charging off at or below this level, whatever the temperature

log() {
  echo "$(date '+%m-%d %H:%M:%S') $*" >> $LOG
  if [ "$(wc -c < $LOG)" -gt 200000 ]; then tail -n 300 $LOG > $LOG.tmp && mv $LOG.tmp $LOG; fi
}

# a PID file from before a reboot can point at an unrelated process that reused the PID: check the name too
is_running() {
  [ -f $PIDF ] && grep -q fm-watchdog "/proc/$(cat $PIDF)/cmdline" 2>/dev/null
}

# Pings alone can fail when the uplink is saturated by the stream, which would cause a needless Wi-Fi toggle that
# kills the live stream. So: ICMP first, then a real HTTP request over both address families.
net_ok() {
  ping -c1 -W3 1.1.1.1 >/dev/null 2>&1 && return 0
  ping -c1 -W3 8.8.8.8 >/dev/null 2>&1 && return 0
  /data/adb/magisk/busybox wget -q -T 6 -O /dev/null http://connectivitycheck.gstatic.com/generate_204 >/dev/null 2>&1 && return 0
  /data/adb/magisk/busybox wget -q -T 6 -O /dev/null http://www.google.com/generate_204 >/dev/null 2>&1
}

# The Wi-Fi chip's power-save mode adds hundreds of ms of latency and jitter (it comes back after every Wi-Fi toggle).
wifi_awake() {
  iw dev wlan0 get power_save 2>/dev/null | grep -q 'on' && iw dev wlan0 set power_save off 2>/dev/null && log "wifi power-save was on -> off"
}

protect() {
  for n in tinycap ffmpeg icecast2 tailscaled; do
    for p in $(pidof $n); do echo -900 > /proc/$p/oom_score_adj 2>/dev/null; done
  done
  for p in $(pgrep -f '^python3 /opt/fm/fm_tuner.py'); do echo -900 > /proc/$p/oom_score_adj 2>/dev/null; done
}

# Hysteresis on /sys/.../charging_enabled. An unreadable level changes nothing, so a sysfs hiccup cannot flip the switch.
battery_guard() {
  [ -w $BATT/charging_enabled ] || return 0
  lvl=$(cat $BATT/capacity 2>/dev/null); temp=$(cat $BATT/temp 2>/dev/null); cur=$(cat $BATT/charging_enabled 2>/dev/null)
  case "$lvl$temp" in ''|*[!0-9]*) return 0 ;; esac
  want=$cur
  if [ "$lvl" -le $BATT_LOW ]; then want=1
  elif [ "$temp" -ge $BATT_HOT ]; then want=0
  elif [ "$lvl" -ge $BATT_MAX ]; then want=0
  elif [ "$lvl" -le $BATT_RESUME ]; then want=1
  fi
  if [ "$want" != "$cur" ]; then
    echo $want > $BATT/charging_enabled
    log "battery ${lvl}% ${temp}/10 C -> charging $([ $want = 1 ] && echo on || echo off)"
  fi
}

run() {
  echo fmserver > /sys/power/wake_lock 2>/dev/null
  log "watchdog started (pid $$)"
  fails=0; toggles=0; last_toggle=0; last_hub=0; last_tuner=0; last_ts=0; last_lan=0
  while true; do
    now=$(date +%s)

    # ---- internet
    if net_ok; then
      [ $fails -ge 3 ] && log "internet is back"
      fails=0; toggles=0
    else
      fails=$((fails + 1))
      if [ $fails -ge 3 ] && [ $((now - last_toggle)) -ge 120 ]; then
        toggles=$((toggles + 1)); last_toggle=$now
        log "no internet for $fails checks -> toggling Wi-Fi (#$toggles)"
        svc wifi disable; sleep 3; svc wifi enable
        if [ $toggles -ge 3 ]; then
          log "still down -> restarting tailscaled"
          $FM/fm-tailscale.sh stop >/dev/null 2>&1; sleep 2; $FM/fm-tailscale.sh start >/dev/null 2>&1
          toggles=0
        fi
      fi
    fi

    # ---- services (60 s cooldown per service so a persistent fault cannot cause a restart storm)
    if { ! pidof tinycap >/dev/null || ! pidof icecast2 >/dev/null || ! pgrep -f '[f]fmpeg.*-f s16le' >/dev/null; } && [ $((now - last_hub)) -ge 60 ]; then
      last_hub=$now; log "audio hub incomplete -> restarting"
      $FM/fm-stream.sh stop >/dev/null 2>&1; sleep 1; $FM/fm-stream.sh start >/dev/null 2>&1
    fi
    if ! pgrep -f '^python3 /opt/fm/fm_tuner.py' >/dev/null && [ $((now - last_tuner)) -ge 60 ]; then
      last_tuner=$now; log "tuner daemon down -> restarting"
      $FM/fm-tuner.sh start >/dev/null 2>&1
    fi
    if ! pgrep -f '^/usr/sbin/tailscaled' >/dev/null && [ $((now - last_ts)) -ge 60 ]; then
      last_ts=$now; log "tailscaled down -> restarting"
      $FM/fm-tailscale.sh start >/dev/null 2>&1
    fi
    # optional LAN listen proxy: only when switched on in fm.env
    if grep -q '^FM_LAN_LISTEN=1' /data/debian/root/fm.env 2>/dev/null && ! pgrep -f '^python3 /opt/fm/lan_listen.py' >/dev/null \
       && [ $((now - last_lan)) -ge 60 ]; then
      last_lan=$now; log "LAN listener down -> restarting"
      $FM/fm-lan.sh start >/dev/null 2>&1
    fi

    battery_guard
    wifi_awake
    protect
    sleep $INTERVAL
  done
}

case "$1" in
  run) run ;;
  start)
    if is_running; then echo "already running (pid $(cat $PIDF))"; exit 0; fi
    nohup sh "$0" run </dev/null >/dev/null 2>&1 &
    echo $! > $PIDF; sleep 1; echo "started (pid $(cat $PIDF))" ;;
  stop)
    is_running && kill "$(cat $PIDF)" 2>/dev/null; rm -f $PIDF
    echo 1 > $BATT/charging_enabled 2>/dev/null   # never leave charging switched off behind us
    echo fmserver > /sys/power/wake_unlock 2>/dev/null; echo stopped ;;
  status)
    if is_running; then echo "running (pid $(cat $PIDF))"; else echo stopped; fi
    tail -n 8 $LOG 2>/dev/null ;;
  *) echo "usage: $0 start|stop|status"; exit 1 ;;
esac
