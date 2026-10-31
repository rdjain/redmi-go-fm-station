#!/system/bin/sh
# FM tuner daemon launcher (run as root).   fm-tuner.sh start | stop | status | token
# Web UI: http://<phone-ip>:8080/   (token is in /data/debian/root/fm.env, key FM_TOKEN)

R=/data/debian
DEB=/data/local/debian.sh
ENVF=$R/root/fm.env
LOG=/data/local/tmp/fm-tuner.log
BB=/data/adb/magisk/busybox

setenv() {  # setenv KEY VALUE : replace or append in fm.env
  grep -v "^$1=" $ENVF > $ENVF.new 2>/dev/null
  echo "$1=$2" >> $ENVF.new
  mv $ENVF.new $ENVF
  chmod 600 $ENVF
}

PAT='^python3 /opt/fm/fm_tuner.py'
running() { pgrep -f "$PAT" >/dev/null; }

start() {
  running && { echo "already running"; return 0; }
  $DEB start
  # the stock app and this daemon must not both own /dev/radio0
  am force-stop com.caf.fmradio
  /data/local/fm/fm-firmware-up.sh || return 1
  mkdir -p $R/opt/fm/web
  cp /data/misc/fm/Riva_fm_cal $R/opt/fm/Riva_fm_cal 2>/dev/null

  grep -q '^FM_TOKEN=' $ENVF || setenv FM_TOKEN "$($BB head -c 18 /dev/urandom | $BB od -An -tx1 | $BB tr -d ' \n')"
  # loopback only: Tailscale (tailscale serve) is the way in. For LAN debugging put FM_LAN=1 in fm.env.
  setenv FM_BIND 127.0.0.1
  if grep -q '^FM_LAN=1' $ENVF; then
    IP=$(ip -4 addr show wlan0 | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -n1)
    [ -n "$IP" ] && setenv FM_BIND2 "$IP"
  else
    grep -v '^FM_BIND2=' $ENVF > $ENVF.new; mv $ENVF.new $ENVF; chmod 600 $ENVF
  fi
  grep -q '^FM_PORT=' $ENVF || setenv FM_PORT 8080

  $DEB enter 'mkdir -p /run/fm; exec python3 /opt/fm/fm_tuner.py' </dev/null >>$LOG 2>&1 &
  sleep 3
  if running; then echo "started on 127.0.0.1:8080 (reach it through Tailscale)"; else echo "failed, see $LOG"; tail -n 8 $LOG; return 1; fi
}

stop() {
  pkill -TERM -f "$PAT"
  i=0; while running && [ $i -lt 20 ]; do sleep 0.3; i=$((i+1)); done
  running && pkill -KILL -f "$PAT"
  echo stopped
}

case "$1" in
  start) start ;;
  stop) stop ;;
  status) running && echo "running" || echo "stopped"; tail -n 5 $LOG 2>/dev/null ;;
  rotate-token) setenv FM_TOKEN "$($BB head -c 18 /dev/urandom | $BB od -An -tx1 | $BB tr -d ' \n')"; stop; start; echo "new token: $(grep '^FM_TOKEN=' $ENVF | cut -d= -f2)" ;;
  token) grep '^FM_TOKEN=' $ENVF | cut -d= -f2 ;;
  *) echo "usage: $0 start|stop|status|token|rotate-token"; exit 1 ;;
esac
