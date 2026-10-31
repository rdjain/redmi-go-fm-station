#!/system/bin/sh
# FM radio -> HTTP (Icecast) on the Redmi Go. Run as root.
#   fm-stream.sh start | stop | status
# Listen at:  http://<phone-ip>:8000/fm   (VLC: Media > Open Network Stream)
# Tune the station first (FM app; wired headset/antenna must be plugged in).

R=/data/debian
DEB=/data/local/debian.sh
LOG=/data/local/tmp/fm-stream.log
PIDF=/data/local/tmp/fm-stream.pid
FIFO=$R/tmp/fm.pcm
RATE=48000
CH=2
MIXER='MultiMedia1 Mixer INTERNAL_FM_TX'

# a PID file left over from before a reboot may name an unrelated process that reused the PID
is_running() { [ -f $PIDF ] && grep -q fm-stream "/proc/$(cat $PIDF)/cmdline" 2>/dev/null; }

start() {
  if is_running; then
    echo "already running (pid $(cat $PIDF))"; return 0
  fi
  $DEB start
  [ -f $R/root/fm.env ] || { echo "run fm-setup-chroot.sh in the chroot first"; exit 1; }

  # Icecast (runs as its own user inside the chroot)
  $DEB enter 'pgrep -x icecast2 >/dev/null || { mkdir -p /var/log/icecast2 && chown icecast2 /var/log/icecast2; start-stop-daemon --start --background --chuid icecast2 --exec /usr/bin/icecast2 -- -c /etc/icecast2/icecast.xml </dev/null >/dev/null 2>&1; }' </dev/null >/dev/null 2>&1

  rm -f $FIFO; mkfifo $FIFO
  # supervise: route FM into capture PCM, read it, encode to MP3, push to Icecast
  (
    while true; do
      tinymix "$MIXER" 1 >/dev/null
      tinycap $FIFO -D 0 -d 0 -c $CH -r $RATE -b 16 &
      CAP=$!
      $DEB enter ". /root/fm.env; tail -c +45 /tmp/fm.pcm | ffmpeg -nostdin -loglevel warning -f s16le -ar $RATE -ac $CH -thread_queue_size 1024 -i - -c:a flac -compression_level 5 -content_type application/ogg -password \$ICECAST_SOURCE_PASSWORD -f ogg icecast://127.0.0.1:\$ICECAST_PORT\$FM_MOUNT.flac"
      kill $CAP 2>/dev/null
      sleep 3
    done
  ) >>$LOG 2>&1 &
  echo $! > $PIDF
  echo "started (pid $(cat $PIDF)); lossless mount (localhost only): http://127.0.0.1:8000/fm.flac"
}

stop() {
  is_running && kill "$(cat $PIDF)" 2>/dev/null; rm -f $PIDF
  pkill -x tinycap 2>/dev/null
  $DEB enter 'pkill -f "[f]fmpeg.*-f s16le"; pkill -x icecast2; for i in $(seq 1 30); do pgrep -x icecast2 >/dev/null || break; sleep 0.2; done' 2>/dev/null
  tinymix "$MIXER" 0 >/dev/null 2>&1
  echo stopped
}

status() {
  if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "supervisor: running"; else echo "supervisor: stopped"; fi
  pgrep -x tinycap >/dev/null && echo "tinycap: running" || echo "tinycap: not running"
  tinymix "$MIXER" 2>/dev/null
  tail -n 5 $LOG 2>/dev/null
}

case "$1" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status"; exit 1 ;;
esac
