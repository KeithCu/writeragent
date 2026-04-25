#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GDB_CMDS="$ROOT/gdb-catch-uno.txt"
GDB_LOG="$ROOT/gdb-catch-uno.log"
LO_PROFILE="${HOME}/.config/libreoffice/4"

cd "$ROOT"

cat > "$GDB_CMDS" <<'GDB'
set pagination off
set debuginfod enabled on
set print pretty on
set breakpoint pending on
set logging file /home/keithcu/Desktop/Python/writeragent/gdb-catch-uno.log
set logging overwrite on
set logging enabled on

handle SIGABRT stop print nopass
break gcc3::raiseException
commands
  silent
  echo \n=== HIT gcc3::raiseException ===\n
  bt 28
  continue
end

run

echo \n=== STOPPED AFTER SIGNAL/EXIT ===\n
bt full
thread apply all bt 30
quit
GDB

echo "[1/5] Building WriterAgent OXT..."
make build

echo "[2/5] Stopping LibreOffice..."
make lo-kill
rm -f "$LO_PROFILE/.lock" "$LO_PROFILE/user/.lock"

echo "[3/5] Installing build/WriterAgent.oxt..."
unopkg remove org.extension.writeragent 2>/dev/null || true
unopkg add build/WriterAgent.oxt

echo "[4/5] Starting LibreOffice under gdb."
echo "      Reproduce: Tools -> Options -> Language Settings -> Writing Aids."
echo "      Log: $GDB_LOG"
rm -f "$GDB_LOG"

cd /usr/lib/libreoffice/program
gdb -nx -iex "set debuginfod enabled on" -x "$GDB_CMDS" --args ./soffice.bin --norestore --writer

echo "[5/5] Extracting summary..."
cd "$ROOT"
rg -n "HIT gcc3::raiseException|STOPPED AFTER SIGNAL|SIGABRT|RuntimeException|createInstance|libcuilo|cpp2uno|SvTreeListBox|Writing|Grammar|Proof|#0|#1|#2|#3|#4|#5|#6|#7|#8|#9|#10|#11|#12|#13|#14|#15|#16" "$GDB_LOG" -C 4 || true
