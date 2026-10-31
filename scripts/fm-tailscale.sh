#!/system/bin/sh
# Tailscale (userspace networking, inside the Debian chroot).  Run as root.
#   fm-tailscale.sh start | stop | status | login
R=/data/debian
DEB=/data/local/debian.sh
LOG=/data/local/tmp/tailscaled.log
PAT='^/usr/sbin/tailscaled'

running() { pgrep -f "$PAT" >/dev/null; }

start() {
  running && { echo "tailscaled already running"; return 0; }
  $DEB start
  $DEB enter 'mkdir -p /var/lib/tailscale /run/tailscale; exec /usr/sbin/tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock' </dev/null >>$LOG 2>&1 &
  sleep 4
  running && echo "tailscaled started" || { echo "tailscaled failed"; tail -n 8 $LOG; return 1; }
}

stop() {
  pkill -TERM -f "$PAT"
  echo stopped
}

case "$1" in
  start) start ;;
  stop) stop ;;
  status) running && echo "tailscaled running" || echo "tailscaled stopped"; $DEB enter 'tailscale status' </dev/null 2>&1 | head -n 12 ;;
  login) $DEB enter "tailscale up --hostname=${TS_HOSTNAME:-fm-station} --accept-dns=false --accept-routes=false --ssh=false" </dev/null 2>&1 ;;
  *) echo "usage: $0 start|stop|status|login"; exit 1 ;;
esac
