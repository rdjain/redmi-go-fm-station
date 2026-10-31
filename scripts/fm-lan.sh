#!/system/bin/sh
# Optional LAN listen-only proxy for the FM audio. Run as root.   fm-lan.sh enable | disable | start | stop | status
# Off by default. `enable` sets FM_LAN_LISTEN=1 in fm.env and starts it; start/watchdog/boot only run it when that flag is set.
# Serves a single read-only URL (the audio) to private-network clients; Icecast itself stays on loopback.
# Only share audio you have the right to rebroadcast; the listener cap (FM_LAN_MAX) keeps the phone responsive.

R=/data/debian
DEB=/data/local/debian.sh
ENVF=$R/root/fm.env
LOG=/data/local/tmp/fm-lan.log
BB=/data/adb/magisk/busybox
PAT='^python3 /opt/fm/lan_listen.py'

setenv() {  # setenv KEY VALUE : replace or append in fm.env
  grep -v "^$1=" $ENVF > $ENVF.new 2>/dev/null
  echo "$1=$2" >> $ENVF.new
  mv $ENVF.new $ENVF
  chmod 600 $ENVF
}

running() { pgrep -f "$PAT" >/dev/null; }
enabled() { grep -q '^FM_LAN_LISTEN=1' $ENVF 2>/dev/null; }
port() { p=$(grep '^FM_LAN_PORT=' $ENVF 2>/dev/null | cut -d= -f2); echo "${p:-8081}"; }

start() {
  enabled || { echo "LAN listening is disabled (run: fm-lan.sh enable)"; return 0; }
  running && { echo "already running"; return 0; }
  [ -f $R/opt/fm/lan_listen.py ] || { echo "missing $R/opt/fm/lan_listen.py"; return 1; }
  $DEB start
  $DEB enter 'exec python3 /opt/fm/lan_listen.py' </dev/null >>$LOG 2>&1 &
  sleep 2
  if running; then
    IP=$(ip -4 addr show wlan0 | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -n1)
    echo "listening: http://${IP:-<phone-ip>}:$(port)/   (stream: /fm.flac)"
  else
    echo "failed, see $LOG"; tail -n 8 $LOG; return 1
  fi
  if [ "$(wc -c < $LOG)" -gt 100000 ]; then tail -n 200 $LOG > $LOG.tmp && mv $LOG.tmp $LOG; fi
}

stop() {
  pkill -TERM -f "$PAT"
  i=0; while running && [ $i -lt 10 ]; do sleep 0.3; i=$((i+1)); done
  running && pkill -KILL -f "$PAT"
  echo stopped
}

case "$1" in
  enable)  setenv FM_LAN_LISTEN 1; start ;;
  disable) setenv FM_LAN_LISTEN 0; stop ;;
  start)   start ;;
  stop)    stop ;;
  status)  enabled && echo "enabled" || echo "disabled"; running && echo "running" || echo "not running"; tail -n 5 $LOG 2>/dev/null ;;
  *) echo "usage: $0 enable|disable|start|stop|status"; exit 1 ;;
esac
