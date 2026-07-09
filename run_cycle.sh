#!/bin/zsh
# Hermes paper-bot cycle + dashboard publish. Called by launchd every 15 min.
cd "$(dirname "$0")"
[ -f bot.log ] && [ $(stat -f%z bot.log) -gt 1000000 ] && mv bot.log bot.log.1
{
  echo "=== $(date -u '+%F %T')"
  /usr/bin/python3 bot.py cycle
  git add docs && git -c user.email=michalec.erik@gmail.com -c user.name=agik66 \
    commit -qm "data update $(date -u +%H:%M)" && git push -q origin main
} >> bot.log 2>&1
exit 0
