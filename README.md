# LocalAgent V1 — 钉钉集成闭环

个人本地工作助手 Agent 的第一个迭代：钉钉报警/审计消息 → AI 分析 → 分级提醒 → 受控写操作，全部数据本地存储。

## 部署与启停（launchd 托管）

```bash
cd ~/developer/localagent
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp scripts/com.huayang.localagent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.huayang.localagent.plist   # 登录自启 + 崩溃自拉起
```

- 手动停止：`scripts/stop.sh`（卸载托管，不再自启）
- 手动启动：`scripts/start.sh`
- 日志：`workspace/logs/localagent.{log,err}`

## 管理页面

http://127.0.0.1:8765

- 状态：dws 轮询状态、群回复开关（随时切换）、待确认异常
- 历史记录：默认最近 6 个月，7/30/90/180 天切换
- 报警中心：待确认 / 已确认 / 已忽略（关闭≠确认，30 分钟重弹）
- 报告：全文查看
- 授权清单：三维（应用 × 读/写 × 具体功能），条目独立启停
- 钉群配置：添加/移除/启停授权群、解析会话 ID，热加载生效
- 存储管理：路径/占用/配额 + 清理超期 / 归档 6-12 个月 / 清空 1 年以上 / 超容量清理

## 通道与配置

- 接收：`dws` CLI 轮询（list-mentions + 群全量），用户钉钉账号鉴权，零应用凭证
- 发送：`dws` 发群消息（带【LocalAgent】前缀 + 仅供参考），`dingtalk.reply_enabled` 控制开关
- `workspace/config/agent.yaml`：引擎（默认 qodercli，备用 codex）、轮询/冷却/保留策略、web 端口
- `workspace/config/groups.yaml`：授权群清单（唯一源，管理页面可改）
- `workspace/config/auth_list.yaml`：三维授权清单；`env: online` 写条目强制二次确认
- 小角色形象：替换 `workspace/assets/character/{idle,working,attention,error}.png` 即换形象

## 验收（mock 模式）

```bash
LOCALAGENT_MOCK=1 ./.venv/bin/python tests/acceptance.py   # 14 项场景断言
```

mock 模式可通过 `POST /api/simulate` 注入消息：

```json
{"group": "改签底座质量监控", "sender": "技术风险", "text": "【报警】change-flight-tp 改签底座 成功率 当前值 0 差异45元 P2"}
```

## 数据存储

SQLite（WAL，`workspace/data/localagent.sqlite`）：runs / alerts / messages / audit_logs / auth_exec / reports_meta / evidence / conn_state；报告正文为 Markdown（`workspace/reports/YYYY-MM-DD/`）。保留：报告 90 天 / 证据 30 天 / 审计 180 天；6-12 个月归档；>1 年清空。详见 PRD 8.14 / 10.3。

## 隐私

仅处理启用群的消息；未授权群消息不落地、不记内容。数据最小化原则见 PRD 14 条 15。
