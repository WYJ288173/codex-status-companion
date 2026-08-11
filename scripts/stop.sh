#!/bin/bash
# 手动停止 LocalAgent（卸载 launchd 托管，不再自启）
launchctl unload ~/Library/LaunchAgents/com.huayang.localagent.plist 2>/dev/null \
  || launchctl bootout gui/$(id -u)/com.huayang.localagent
pkill -f "localagent.gui pet" 2>/dev/null
pkill -f "localagent.gui menubar" 2>/dev/null
# 等待 GUI 子进程真正退出，避免重启时 pgrep 竞态误判“已在运行”而跳过拉起菜单栏
for _ in $(seq 1 20); do
  pgrep -f "localagent.gui" >/dev/null 2>&1 || break
  sleep 0.3
done
echo "LocalAgent stopped. Re-start with scripts/start.sh"
