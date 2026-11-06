#!/usr/bin/env bash
# Fund House — macOS setup. Run from Terminal:
#   cd multi-agent-fund-house
#   bash deploy/setup_mac.sh
# Installs Python deps and three launchd jobs (times are Hong Kong local time):
#   com.fundhouse.daily     Tue–Sat 06:30  after-close data + stops + research pack
#   com.fundhouse.execute   Mon–Fri 21:40 & 22:40  executes queued/approved orders once after US open
#   com.fundhouse.sentinel  always on      crash / drawdown watch during US hours
#   com.fundhouse.dashboard always on      http://127.0.0.1:8765
set -euo pipefail
FH="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$(command -v python3)}"
echo "Fund House at: $FH"
echo "Python:        $PY  ($($PY --version))"
"$PY" -m pip install -q -r "$FH/requirements.txt"
mkdir -p "$FH/logs" "$FH/state" "$FH/data" "$FH/research"
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA"

cal() { # weekday hour minute -> plist dict
  printf '<dict><key>Weekday</key><integer>%s</integer><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>' "$1" "$2" "$3"; }

write_plist() { # label script keepalive calendar-xml
  local label=$1 script=$2 keep=$3 calxml=$4
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$FH/scripts/$script</string></array>
  <key>WorkingDirectory</key><string>$FH</string>
  <key>StandardOutPath</key><string>$FH/logs/launchd_$label.out</string>
  <key>StandardErrorPath</key><string>$FH/logs/launchd_$label.err</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  $( [ "$keep" = yes ] && echo '<key>KeepAlive</key><true/><key>RunAtLoad</key><true/>' )
  $( [ -n "$calxml" ] && echo "<key>StartCalendarInterval</key><array>$calxml</array>" )
</dict></plist>
PLIST
  launchctl unload "$LA/$label.plist" 2>/dev/null || true
  launchctl load "$LA/$label.plist"
  echo "  loaded $label"
}

DAILY=""; for d in 2 3 4 5 6; do DAILY+=$(cal $d 6 30); done
EXEC="";  for d in 1 2 3 4 5; do EXEC+=$(cal $d 21 40)$(cal $d 22 40); done
write_plist com.fundhouse.daily    daily_update.py no  "$DAILY"
write_plist com.fundhouse.execute  execute.py      no  "$EXEC"
write_plist com.fundhouse.sentinel sentinel.py     yes ""
# dashboard lives in dashboard/, not scripts/
sed "s#$FH/scripts/sentinel.py#$FH/dashboard/server.py#; s#com.fundhouse.sentinel#com.fundhouse.dashboard#g" \
  "$LA/com.fundhouse.sentinel.plist" > "$LA/com.fundhouse.dashboard.plist"
launchctl unload "$LA/com.fundhouse.dashboard.plist" 2>/dev/null || true
launchctl load "$LA/com.fundhouse.dashboard.plist"; echo "  loaded com.fundhouse.dashboard (http://127.0.0.1:8765)"

cat <<MSG

Done. Next:
  1. macOS privacy: the folder is on your Desktop, which background jobs cannot read by default.
     System Settings > Privacy & Security > Full Disk Access > add: $PY
  2. Stop the Mac sleeping on power:  sudo pmset -c sleep 0 disksleep 0
  3. Keep IB Gateway (paper) logged in; enable Configure > Settings > Lock and Exit > Auto restart.
  4. Test:  $PY scripts/check_setup.py   then   $PY scripts/daily_update.py
To remove the jobs:  bash deploy/uninstall_mac.sh
MSG
