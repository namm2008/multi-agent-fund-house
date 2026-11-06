#!/usr/bin/env bash
for l in com.fundhouse.daily com.fundhouse.execute com.fundhouse.sentinel com.fundhouse.dashboard; do
  launchctl unload "$HOME/Library/LaunchAgents/$l.plist" 2>/dev/null && rm -f "$HOME/Library/LaunchAgents/$l.plist" && echo "removed $l"
done
