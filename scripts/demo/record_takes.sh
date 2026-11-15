#!/bin/bash
# Record one .mkv per demo scene into build/demo/video: scripts/demo/record_takes.sh scene1 scene3 ...
# Keep hands off the desktop while it runs; wrap in kde-inhibit --screenSaver --power to stop the display sleeping.
R=$(cd "$(dirname "$0")/../.." && pwd); V=$R/build/demo/video; cd "$R" || exit 1
for s in "$@"; do
  out="$V/$s.mkv"; rm -f "$out"
  make lo-kill >/dev/null 2>&1
  /usr/bin/python3 scripts/demo/record_screen.py "$out" > "$V/$s.rec.log" 2>&1 &
  rec=$!
  for i in $(seq 1 40); do grep -q RECORDING "$V/$s.rec.log" && break; sleep 0.25; done
  timeout 500 make demo-video SCENE=$s > "$V/$s.log" 2>&1; rc=$?
  sleep 2; kill -INT $rec; wait $rec
  echo "$s rc=$rc ok=$(grep -c 'TEST end.* OK' "$V/$s.log") $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out" 2>&1)s" | tee -a "$V/takes.txt"
done
make lo-kill >/dev/null 2>&1
echo ALLDONE | tee -a "$V/takes.txt"
