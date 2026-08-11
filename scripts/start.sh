#!/bin/bash
# 手动启动 LocalAgent（launchd 托管）
mkdir -p ~/developer/localagent/workspace/logs
launchctl load ~/Library/LaunchAgents/com.huayang.localagent.plist 2>/dev/null \
  || launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.huayang.localagent.plist
launchctl list | grep localagent && echo "LocalAgent started (launchd managed)"
