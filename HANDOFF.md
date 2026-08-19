# LocalAgent 任务交接存档（2026-08-13）

## 项目要点
- 钉钉报警值班分析智能体：FastAPI 后台 http://127.0.0.1:8765 + launchd（com.huayang.localagent）
- 重启：`bash scripts/stop.sh && bash scripts/start.sh`（启动需 ~15s）
- workspace：`/Users/huayang/developer/localagent/workspace`，DB：`workspace/data/localagent.sqlite`
- 测试：`./.venv/bin/python tests/test_v1_closure.py ... test_v5_correlation.py acceptance.py`（当前 213 项全过）
- git main 最新：fcd8187（已推送 github）

## 已交付能力
1. 引擎可用性治理：model/model_fallback 降级链（qodercli→codex）、额度/限流判 EngineUnavailable
   （run 状态 engine_unavailable，不算分析失败、不产假报警）、引擎探针、一键重跑。
2. reanalyze/rerun 后台化：prepare/execute 拆分 + BackgroundTasks，客户端断开不中断分析。
3. codex 降级卡 stdin 修复：所有引擎子进程 stdin=DEVNULL。
4. 归因体系（engine.py PROMPT_TEMPLATE）：
   - conclusion 必须以【外部域问题】/【域内问题】开头；
   - 域边界 = 外部域 → 改签底座(change-flight-tp)；底座/flycp/atr 均属改签域，域内交互不是归因边界；
   - 必须先取证外部返回原文（成功/失败）再结合本地代码归因；
   - 外部失败区分「不应返回失败」vs「数据缺失」；外部成功区分域内「兼容性缺陷」vs「逻辑漏洞」vs 外部数据缺失；
   - 取证缺口写【归因待定】，禁止臆断。
5. 建议动作规范：≤40字；notify_external 说清问题性质、不替外部域决策；
   tech_requirement 说清缺陷类型 + 类/模块级修复方向。
6. 一键创建 Aone 需求（aone.py）：报告页 tech_requirement 建议按钮 → 草稿确认（标题=归因摘要，
   描述=结论+证据+修复方向，项目可编辑）→ 引擎调 aone-requirement-create skill → 回写链接
   （报告侧车/报警记录/审计），幂等防重复。
7. 同类报警归类（correlate.py）：10 分钟窗口关键词家族归组（验价/询价/验座等）；
   单订单=单用户不升级；多订单=批量信号，引擎须查 sunfire 趋势 + 近 30 分钟发布/变更；
   批量至少 P2（兜底升级+审计）；报警中心聚合卡片（多订单标红）。

## 回归基准
- run-678f0085fb66：验价报警归因正确样例（外部域 no fare + iacs jar 伴生缺陷）。
- run-a106cb45cded：一键创建 Aone 需求可用的 tech_requirement 样例。

## 遗留问题
- skill config "2 warnings" 根因未定位（flyeye-log-query 已注册可用，不影响功能）。
- LocalAgent 引擎侧 memory skill 认证未配置（P1 记忆检索跳过）：需配置 user-memory authTicket。

## 关键文件
- localagent/engine.py（提示词+引擎链）、pipeline.py（路由/关联/重分析）、correlate.py（归类）、
  aone.py（需求创建）、webapp.py（后台页）、reports.py/render.py（报告）、notify.py（通知）

---

# 交接存档追加（2026-08-14）

## 当日已交付（测试 249 项全过，服务已重启生效）
1. /alerts 重构：默认窗口近 2 小时（range=2h/today/yesterday/3d）、统一搜索条（时间/群/级别/关键词/匹配规则）、
   待回复操作内联进消息卡片（run_id 关联+兜底卡片）、4 张历史状态表折叠、监听范围卡移入 /groups。
2. 采集过滤：审计播报 owner≠华扬 不采集（matched_rule=broadcast_not_my_owner）；
   监控报警支持 owner 标记命中（@华扬/华扬(主班)/owner=华扬，auth_list.yaml rule1 compound_or）；
   抑制标题报警（[YYYY-MM-DD HH:MM] 开头，/ 和 - 分隔均兼容）纳入采集；owner_name 配置在 agent.yaml dingtalk 段。
3. 批量失败风险分级（correlate.py）：窗口扩 30 分钟；high（近10分钟≥5单 或 跨度>10分钟且近10分钟持续有新单）→P1；
   medium（3-4单）→P2；low（≤2单无持续）→不升级。
4. engine.py 提示词先验：
   - 供销 NPE 实锤链路（ServiceProxy.java:2422 裸 return → CreatePnrAdapterServiceImpl:104 拆箱 NPE → :150 吞码 C-1-4004）；
     无堆栈帧禁止推测抛出点；
   - 生单超时轮询机制：超时后上游轮询是设计机制，禁止「超时诱发重试」归因；
   - 终态判定先验：禁止以报警停止作为「已恢复」依据，订单终态必须实证；
   - flyeye 取证 bizGroup 必须显式传 reverse/all。
5. 深入分析报告：workspace/reports/2026-08-14/run-6242377411dd-deepdive.md
   （订单 10061676337629 占座失败：4 次改签申请全部 closeType=13 关闭；超时兜底「提交中」比外部失败早 36ms 透出、
   用户重试命中已关闭单抛 TradeBizException 的透出缺陷）。

## 【未完事项·新会话执行】
1. ~~订单 10061676337629 终态核验~~（已完成·2026-08-14）：
   - flight-order-data-query skill → flight_datafactory queryOrder（errorCode=200，分类 ORDER）；
   - 实证：4 张改签单（810544577629/825993860629/826007735629/817073405629）trp 全部 CLOSE，
     无第 5 张改签单；主订单交易成功[70]、出票销采单已完成、航班仍为原 9C8546 NGB-CGQ 08-15
     → 订单未改签成功，结论②已升级为实证表述；
   - deepdive 报告【未验证】段落、结论②、审计摘要均已更新。
   - 注：qodercli 会话未注册该 MCP，走 mcp-remote stdio 桥接（token 已缓存于 ~/.mcp-auth），
     复用脚本 workspace/tmp/mcp_query_order.py。
2. 可选后续处置结果：
   - ~~超时兜底透出缺陷（P-1008-99-003 竞态）是否提 Aone 技术需求~~（已确认·2026-08-14：owner 认定
     P-1008-99-003 文案与超时兜底为设计机制、非缺陷，不提需求；deepdive 结论③已改判「设计机制·非缺陷」、
     建议动作2已撤回；engine.py 生单超时先验已收紧「不得登记缺陷/建议修复」，服务重启后生效）；
   - 报警中心遗留 7 条 P3 待确认（用户操作项，非 agent 待办）。

## 备注（2026-08-14 更新）
- 测试基线更新为 249 项（v1:79/v2:60/v3:40/v4:19/v5:30/acceptance:21）。
- 改 prompt/匹配规则后必须重启服务才生效（scripts/stop.sh && scripts/start.sh）。
- git 状态：当日改动未提交未推送（用户未指示）。

---

# 交接存档追加（2026-08-17）

## 当日已交付（测试 278 项全过，服务已重启生效）
1. 【bug 修复】/alerts 发送回复假死：根因=共享 sqlite 连接并发 IndexError（实证 2 次）+ dws 发送
   TimeoutExpired 未捕获 → 500 HTML → 前端 r.json() 静默抛异常（35 条 pending 从未发出过一条）。
   修复：db.py RLock 串行化全部读写；dingtalk.reply 30s 超时捕获/返回 bool/失败必留审计；
   pipeline.send_reply 按结果如实流转（失败保持 pending 可重试）；webapp 端点 try/except 永返 JSON；
   前端 jfetch 统一 r.ok+try/catch 错误提示。
2. 【功能】报警类型自动回复白名单落地：_group_auto_reply 仅认 auto_reply_types（废除 auto_reply=true
   一刀切直通，unclassified 永不自动）；自动发送失败转人工卡（reply_auto_failed 审计）；
   /groups 页 checkbox 类型编辑器（替换 prompt 输入）；待回复卡片带 alert_type 徽标；
   groups.yaml 初始白名单=改签底座质量监控群×[验价,验座]（用户批准）。
3. 真机验证 G3：POST /api/auth_exec/171/send_reply → 1.4s 返回成功，auth_exec=replied，
   audit reply_sent_manual+reply_sent（dws openTaskId），消息已到「改签底座质量监控」群。
4. 新增 tests/test_v6_reply_gate.py（25 项：DB 8 线程并发压测、dws 超时/rc 失败/无 gid 路径、
   白名单门控矩阵、自动失败转人工、前端错误处理源码断言）；v1 同步更新到新语义（+4 项）。

## 备注（2026-08-17 更新）
- 测试基线更新为 278 项（v1:83/v2:60/v3:40/v4:19/v5:30/v6:25/acceptance:21）。
- G4 自动回复端到端（真实验价/验座报警进来→自动发群）待下一条真实报警自然验证；单测已覆盖门控。
- git 状态：改动未提交未推送（用户未指示）。

# 交接存档追加（2026-08-18 ~ 08-19）

## 已交付（测试 406 项全过，服务已重启生效，全部已提交推送至 origin/main）

### 08-18：回复可读性与关联准确性
1. 【/alerts 页面】消息卡片报告链接后内联「回复到钉群/丢弃」按钮，移除误导性独立待回复块；
   待确认/历史表时间拆「预警时间/采集时间」两列；状态列中文化（pending→待确认 等）。
2. 【V7 回复身份】回复卡片头部固定带：规则名（parse_sunfire_alert 新增 rule_name=触发行前两行
   监控项组·指标名）、预警时间、采集时间、应用。解决"群里看不出回复的是哪条报警"。
3. 【V7 降噪】迟到投递守卫：预警时间迟于采集 >60 分钟（stale_delivery_minutes 可配）→
   matched_rule=stale_delivery 只记录不分析不回复；同家族+同预警时间已分析 → duplicate_alert 跳过。
4. 【V8 关联准确性】（背景：2 单已值机拦截被误判 P1 批量）
   - 订单提取降噪：剔除 URL/采样行/IP#Err# 串与时间戳形态数字（alarmTime 不再当订单）；
   - 自回复拦截：LocalAgent 自己的结论消息读回后不当新报警（self_reply）；
   - 关联窗口锚定预警时间而非分析时间，迟到投递不再被"拉近"误判同批；
   - 定级门槛收紧：high 需近 10 分钟 ≥5 单，或跨度 ≤10 分钟且 ≥3 单持续；
     ≤2 单一律 low 并注入「严禁 P1/P2」约束；引擎提示词定级矩阵同步（跨度 >30 分钟按偶发）。

### 08-19：聚合与回复体验
5. 【V9 采集/分析解耦】轮询只入队（pipeline.enqueue），worker_loop 串行消费——采集不再被
   引擎阻塞（此前引擎跑 30+ 分钟期间采集停摆，报警延迟 16~90 分钟才入库）。
6. 【V9 同群聚合窗口】aggregate_minutes（默认 5 分钟）：窗口内同群报警合批，一次分析、
   一条统一回复；窗口内报警不被 cooldown 丢弃；@我/审计播报/模拟注入（no_aggregate）即时处理。
7. 【V10 聚合回复头统计式】批头不再逐条罗列：按规则聚合「规则 N次（预警时间范围）」，
   超 200 字符截断前 3 规则+等N条；引擎提示词要求批量 summary 以「近 N 分钟同类报警累计
   M 次，…」汇总句式开头；批回复「采集」标签取批内最早到达时间（不再误显分析完成时刻）。
8. 【修复】应用名解析优先带连字符标准名，Sunfire 发送方 publish 不再误识别为 app。
9. 【V11 无问题自动确认】判定无问题的报警自动回群「已确认：…，无需处理」（reply_on_normal
   开关默认开），发送失败转 pending_reply 人工补发；避免群里报警"无人认领"。

## 备注（2026-08-19 更新）
- 测试基线 406 项：v1:83/v2:60/v3:40/v4:19/v5:30/v6:80/v7:19/v8:15/v9:17/v10:11/v11:11/acceptance:21。
- 已知边界：进程重启会中断进行中的批分析（标记 failed，需手动「重新分析」）；
  自动重排队中断批分析为待评估优化项（用户未批准实施）。
- 关键配置（workspace/config/agent.yaml）：notify.aggregate_minutes=5、
  notify.stale_delivery_minutes 默认 60、dingtalk.reply_on_normal=true。
