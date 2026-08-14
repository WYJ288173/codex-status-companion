# LocalAgent 休假值守自治能力可行性方案

日期：2026-08-14

## 1. 背景与目标

LocalAgent 是运行在工作电脑上的本地工作助手，目标是在用户 8 月底休假期间，能够在用户不随时守在电脑前的情况下，替用户完成低风险答疑、审计告警分析、监控预警初步分析，并把高风险事项沉淀为待确认报告或草稿。

当前 LocalAgent 已具备钉钉轮询、授权群、报警匹配、qodercli/codex 引擎调用、报告落盘、报警中心、写操作门禁、审计播报解析、同类报警关联和 mock 验收等基础能力。但现有能力主要依赖一次大 prompt 驱动 qodercli/codex 完成分类、取证、归因和回复，稳定性不足。半个月内的建设重点应从“增强模型自由发挥”转为“建设证据驱动、低风险白名单、可回放验证的保守自治流水线”。

本方案的核心目标：

- 休假期间可自动回复低风险结论。
- 所有自动回复必须带证据引用。
- 涉及订单、traceId、金额、资损、状态订正、责任归因、批量异常、工具失败的事项默认不自动回复，只进入待确认。
- 审计解决方案、监控分析、退改底座答疑能力按阶段小步迭代，优先交付可用闭环。
- 每次分析都有报告、证据、工具调用摘要和回复决策原因，可审计、可复盘、可回放。

## 2. 当前能力判断

已具备能力：

- 钉钉通道：基于 dws 轮询消息，可按授权群和 `process_mode` 控制处理范围。
- 消息匹配：支持 Sunfire 报警、BCP 审计播报、@我分析请求。
- 引擎链：默认 qodercli，失败后降级 codex，支持模型 fallback 与资源不可用状态。
- 报告与审计：SQLite + Markdown 报告，记录 runs、messages、alerts、audit_logs、auth_exec、reports_meta。
- 审计方案库：`workspace/config/solutions.yaml` 已可按告警码沉淀诊断步骤和写操作计划。
- 回复门禁：已有 `reply_enabled`、群级 `auto_reply`、`auto_reply_types`、写操作二次确认。
- 验收基础：已有 mock acceptance、真实告警回归、引擎可用性测试和同类报警关联测试雏形。

主要风险：

- 取证流程仍主要靠 prompt 约束，缺少结构化中间结果。
- `_reply_if_allowed()` 当前主要按群配置和报警类型决定是否回复，缺少证据等级和风险标记门禁。
- 退改底座答疑尚未形成“问题分类 -> 知识/代码检索 -> 引用证据 -> 低风险回复”的稳定链路。
- 审计解决方案与监控分析的验证集还不足，真实场景下误判、漏证据、工具失败降级风险较高。
- qodercli/codex 访问语雀、代码、skill 的能力依赖本机环境，需要启动前探针和失败可见化。

可行性结论：

- 半个月内可实现“低风险自动回复 + 高风险待确认”的可用自治，不建议追求全场景自动回复。
- 审计解决方案和监控分析可以先围绕少量高频场景做白名单和回放集，逐步扩大覆盖。
- 退改底座答疑可以先支持解释型、知识型、代码定位型问题，禁止自动回答线上归因和资损处置。

## 3. 总体方案

推荐架构：证据驱动的保守自治流水线。

```text
钉钉消息
  -> Scenario Classifier 场景分类
  -> Evidence Collector 取证
  -> Analyzer 生成结构化结论
  -> Reply Risk Gate 回复风险门禁
  -> Report & Audit Trail 报告和审计
  -> Auto Reply / Pending Confirm / No Reply
```

设计原则：

1. 自动回复是白名单，不是默认行为。
2. 分析能力和回复能力解耦，Agent 可以分析，能否发群由 Reply Risk Gate 决定。
3. 证据先于结论，没有强证据引用就禁止自动回复。
4. 高风险事项宁可进入待确认，也不在群内给确定性结论。
5. 工具失败必须显式记录，不能用模型记忆补齐证据。

## 4. 功能技术方案

### 4.1 Scenario Classifier

新增或强化场景分类，输出结构化 `scenario_type`：

- `low_risk_qa`：退改底座低风险答疑。
- `monitor_recovered`：监控已恢复、指标正常类消息。
- `monitor_alert`：Sunfire 或监控异常报警。
- `audit_broadcast`：BCP 审计播报。
- `audit_solution`：命中已沉淀解决方案的审计告警码。
- `history_duplicate`：近期同类问题或同根因重复报警。
- `unknown`：无法识别或边界不清。

分类阶段只做轻量解析，不下业务结论。输出字段建议：

```json
{
  "scenario_type": "low_risk_qa",
  "risk_markers": [],
  "entities": {
    "app": "change-flight-tp",
    "order_id": null,
    "trace_id": null,
    "audit_code": null
  },
  "reason": "命中退改底座解释型问答"
}
```

风险标记包括：

- `has_order_id`
- `has_trace_id`
- `has_amount`
- `has_audit_loss_risk`
- `needs_write`
- `needs_external_attribution`
- `batch_impact`
- `tool_failed`
- `evidence_missing`
- `unknown_scope`

### 4.2 Evidence Collector

取证从 prompt 内部隐式执行改为结构化收集，至少保存：

- `source_type`：`yuque`、`code`、`local_report`、`monitor_text`、`flyeye`、`sunfire`、`jarvis`、`aone`。
- `source_ref`：文档链接、文件路径、run_id、报警 ID、命令摘要。
- `quote_or_summary`：短摘录或结构化摘要。
- `strength`：`A`、`B`、`C`。
- `success`：是否取证成功。
- `failure_reason`：失败原因。

证据等级：

- A 档强证据：语雀文档标题+链接、本机代码文件+类/方法/行号、历史 LocalAgent 报告 run_id、监控恢复消息原文明确指标。
- B 档弱证据：只命中关键词、模型总结、工具失败后的经验判断、疑似代码位置。
- C 档无证据：纯模型记忆、无来源、无法回溯。

低风险答疑取证：

- 允许 qodercli/codex 只读查询语雀和本机代码。
- 必须优先取语雀或代码强证据。
- 允许回答解释型问题，如状态含义、链路职责、应用边界、审计规则含义、代码位置。
- 包含订单号、traceId、金额、资损、责任归因时立即打风险标记，禁止自动回复。

监控分析取证：

- 已恢复或正常类消息可直接使用监控消息原文作为 A 档证据。
- 异常报警需取 Sunfire 指标趋势、Flyeye 日志、代码定位、同类报警关联。
- 工具失败或缺少 trace/日志/趋势时只能待确认。

审计分析取证：

- 先按告警码匹配 `solutions.yaml`。
- 已沉淀方案可提供处理步骤和建议，但写操作仍走待确认。
- 需要 BCP/Jarvis、订单、日志取证时，任何取证缺口都转待确认。

历史复用取证：

- 以 `correlate.py` 的同类报警聚合为基础，新增近期报告检索。
- 命中同根因且风险未升级时，可自动回复“已关联到上一次分析”，附 run_id 和摘要。
- 如果同类报警跨多个订单或成功率下跌，转监控异常待确认。

### 4.3 Analyzer

Analyzer 继续使用 qodercli/codex，但职责收窄：

- 输入：场景分类结果、结构化 evidence、原始消息。
- 输出：结构化分析结果，不直接决定是否回复。
- 对低风险答疑输出短结论和证据引用。
- 对高风险分析输出报告用结论、异常列表、建议动作。

建议输出结构：

```json
{
  "normal": true,
  "scenario_type": "low_risk_qa",
  "conclusion": "change-flight-tp 是改签底座，负责改签创建、验价、预订、出票等底座链路。",
  "summary": "改签底座职责说明",
  "evidence_refs": ["ev_001", "ev_002"],
  "risk_markers": [],
  "confidence": "high",
  "anomalies": [],
  "suggestions": []
}
```

### 4.4 Reply Risk Gate

新增回复风险门禁，替代当前仅靠群配置和 `alert_type` 的自动回复判断。

决策结果：

- `auto_reply`：自动发群。
- `pending_confirm`：生成待确认草稿。
- `no_reply`：不回复，只记录报告或审计。

自动回复必须同时满足：

- 场景属于低风险白名单：`low_risk_qa`、`monitor_recovered`、`history_duplicate`。
- evidence 至少有一条 A 档证据。
- 回复正文包含证据引用。
- 无风险标记。
- 群级 `auto_reply` 或 `auto_reply_types` 允许。
- 全局 `reply_enabled=true`。
- 引擎输出结构完整。

强制待确认：

- 包含订单号、改签单号、退票单号、traceId、eagleEyeId。
- 涉及金额、资损、状态订正、赔付、补偿。
- 涉及责任归因、外部域通知、技术需求创建、写操作。
- 多订单批量异常、P1/P2 风险。
- 任一关键取证工具失败。
- evidence 缺失、证据等级不足、结论和证据不一致。

自动回复模板：

```text
【LocalAgent】结论：{summary}

依据：
1. {source_ref_1}
2. {source_ref_2}

仅供参考；涉及线上数据、订正、资损或责任归因请以人工确认为准。
```

待确认草稿也使用同一模板，但在后台标注拦截原因：

```text
拦截原因：has_trace_id, needs_external_attribution
决策：pending_confirm
```

### 4.5 Report & Audit Trail

每次处理都保存：

- 原始消息。
- 场景分类结果。
- evidence 列表。
- Analyzer 输出。
- Reply Risk Gate 决策和原因。
- 自动回复或待确认草稿内容。
- 工具调用摘要和失败原因。

建议新增或复用字段：

- `runs.scenario_type`
- `runs.reply_decision`
- `runs.reply_reason`
- `evidence.strength`
- `evidence.source_ref`
- `auth_exec.exec_result=pending_reply/auto_replied/no_reply`

如果暂不改 DB schema，可先把这些字段写入报告 JSON 和 audit_logs，降低第一阶段改动风险。

### 4.6 配置方案

新增 `workspace/config/reply_policy.yaml`：

```yaml
auto_reply:
  enabled: true
  allowed_scenarios:
    - low_risk_qa
    - monitor_recovered
    - history_duplicate
  require_evidence_strength: A
  require_evidence_in_reply: true
  block_risk_markers:
    - has_order_id
    - has_trace_id
    - has_amount
    - has_audit_loss_risk
    - needs_write
    - needs_external_attribution
    - batch_impact
    - tool_failed
    - evidence_missing
    - unknown_scope
```

群配置保留为外层开关：

- 群未开启自动回复，即使风险门禁通过，也进入 `pending_confirm`。
- 群开启自动回复，但风险门禁不通过，也不能发群。

## 5. 分阶段建设计划

### 阶段 0：基线冻结与回放集整理（1 天）

目标：

- 固化当前能力边界和已有真实样例。
- 建立休假前验收基线。

功能工作：

- 梳理 `workspace/reports/` 中真实报警报告，选 20-30 条作为回放样例。
- 标注每条期望：`auto_reply`、`pending_confirm`、`no_reply`。
- 整理高频低风险答疑问题 20 条。
- 固化当前 acceptance 通过状态。

验收：

- 形成 `tests/fixtures/` 或等价回放数据。
- 当前 mock acceptance 通过。
- 明确哪些样例允许自动回复。

### 阶段 1：Reply Risk Gate MVP（2-3 天）

目标：

- 先把自动回复风险降下来。
- 所有自动回复必须经过统一门禁。

功能工作：

- 实现 `reply_policy` 配置加载。
- 实现风险标记识别。
- 实现 evidence 强度判断。
- 改造 `_reply_if_allowed()`，引入 `auto_reply / pending_confirm / no_reply` 决策。
- 报告中展示回复决策和拦截原因。

自动回复范围：

- 监控已恢复/正常。
- 历史同根因重复且无升级。
- 明确低风险答疑，但先可只支持 mock evidence。

验收：

- 包含订单/trace/金额的消息不会自动回复。
- 缺 evidence 的异常结论不会自动回复。
- 群配置开启也不能绕过风险门禁。
- 低风险且有 A 档证据的消息可自动回复。

### 阶段 2：退改底座低风险答疑（3-4 天）

目标：

- 支持休假期间常见解释型答疑自动回复。
- 答案必须带语雀或代码证据。

功能工作：

- 建设 `low_risk_qa` 分类规则。
- 通过 qodercli/codex 只读查询语雀和本机代码。
- 将查询结果结构化为 evidence。
- 生成短答复，带证据引用。
- 对订单、trace、金额、资损、责任归因问题打风险标记并转待确认。

优先覆盖：

- 改签底座应用职责。
- 改签创建/支付/出票/交付链路解释。
- 常见状态字段含义。
- 常见审计规则含义。
- 常见类/方法/配置位置。

验收：

- 20 条低风险问答中，自动回复准确率达到 90% 以上。
- 10 条高风险伪装问答全部拦截。
- 每条自动回复都有至少 1 条 A 档证据。
- 查询失败时不自动回复。

### 阶段 3：审计解决方案可用（3 天）

目标：

- 让已沉淀审计告警码能稳定分析、生成处理建议和待确认执行计划。

功能工作：

- 强化 `solutions.yaml` 的告警码匹配、诊断步骤、执行计划校验。
- 审计播报解析后输出结构化 `audit_code`、规则名、告警时间、owner。
- 命中方案时按步骤生成 evidence 和处理建议。
- 写操作、订正、群内处理结论全部待确认。
- 方案缺参数时明确截断并展示缺口。

验收：

- `TRP_INTER_MODIFY_STATUS_ADUIT` 回放可稳定命中方案。
- 门禁关闭时只建议不执行。
- 门禁开启时生成严格步骤：订正待确认 -> 回复待确认。
- 缺少改签单号或订正参数时不生成可执行写操作。

### 阶段 4：监控分析增强（3-4 天）

目标：

- 对 Sunfire/Flyeye/代码定位类报警形成可用的待确认分析报告。
- 自动回复仅限恢复、正常、重复且低风险场景。

功能工作：

- 完善 Sunfire 报警结构化解析。
- 按 traceId/orderId 风险标记强制待确认。
- 结构化保存 Flyeye、Sunfire、代码定位证据。
- 同类报警关联多订单时升级风险，强制待确认。
- 工具失败时报告明确缺口。

验收：

- 真实历史报警回放可生成报告。
- traceId 报警不会自动回复。
- 多订单批量问题不会自动回复。
- 已恢复报警可自动回复，并引用原始指标文本。

### 阶段 5：休假前值守演练（2 天）

目标：

- 用真实消息回放和在线小流量验证，确认休假期间不会误回复高风险内容。

工作内容：

- 回放 50-100 条历史消息。
- 打开自动回复但只放行低风险群或低风险类型。
- 观察 24 小时真实运行。
- 校验报告、审计、待确认草稿、失败降级。
- 输出休假前运行手册和紧急开关说明。

验收指标：

- 高风险消息自动回复数为 0。
- 自动回复均包含证据引用。
- 低风险答疑自动回复准确率 >= 90%。
- 工具失败全部转待确认或 no_reply。
- qodercli/codex 探针失败时不影响主进程稳定。
- 启停、重跑、关闭自动回复、关闭写操作均可在后台完成。

## 6. 测试验证方案

### 6.1 单元测试

覆盖模块：

- 场景分类：`low_risk_qa`、`monitor_recovered`、`monitor_alert`、`audit_broadcast`、`unknown`。
- 风险标记：订单号、traceId、金额、资损关键词、写操作、外部归因、多订单。
- 证据等级：A/B/C 识别。
- 回复门禁：各种组合下的 `auto_reply / pending_confirm / no_reply`。
- 回复模板：必须包含证据引用。

关键用例：

- 群开启自动回复，但 evidence 缺失 -> `pending_confirm`。
- 群开启自动回复，消息含 traceId -> `pending_confirm`。
- 低风险答疑 + 代码证据 -> `auto_reply`。
- 低风险答疑 + 查询失败 -> `pending_confirm`。
- 监控恢复 + 原文证据 -> `auto_reply`。
- 审计订正建议 -> `pending_confirm`。

### 6.2 集成测试

基于 `LOCALAGENT_MOCK=1` 和 `POST /api/simulate`：

- 模拟钉钉消息进入完整 Pipeline。
- 验证 runs、messages、reports_meta、audit_logs、auth_exec 写入。
- 验证自动回复和待确认草稿的状态。
- 验证报告中展示 evidence、reply_decision、reply_reason。

新增 acceptance 场景：

- Q1：退改底座解释型答疑自动回复。
- Q2：同一问题带订单号后被拦截。
- Q3：语雀/代码查询失败后被拦截。
- A1：审计播报命中方案但写操作待确认。
- M1：监控恢复自动回复。
- M2：traceId 异常报警待确认。
- M3：多订单同类报警待确认并升级风险。

### 6.3 真实回放测试

用 `workspace/reports/` 和真实钉群历史消息构造回放集：

- 低风险答疑 20 条。
- 监控恢复/正常 10 条。
- Sunfire 异常报警 20 条。
- 审计播报 10 条。
- 高风险伪装低风险问题 10 条。

每条样例标注：

- 期望场景。
- 期望回复决策。
- 必须命中的风险标记。
- 是否需要 evidence。
- 期望摘要。

验收方式：

- 自动运行回放。
- 输出混淆矩阵：应自动回复/实际自动回复，应待确认/实际待确认。
- 任何高风险误自动回复都视为阻塞。

### 6.4 在线灰度

灰度策略：

- 第一天只开 `pending_confirm`，不自动回复。
- 第二天只对一个低风险群开启 `low_risk_qa` 自动回复。
- 第三天放开 `monitor_recovered` 和 `history_duplicate`。
- 审计和异常报警保持待确认。

观察指标：

- 自动回复数。
- 待确认数。
- no_reply 数。
- evidence 缺失率。
- 工具失败率。
- 引擎不可用次数。
- 人工驳回草稿比例。

### 6.5 故障演练

必须验证：

- qodercli 不可用 -> codex 降级或待确认。
- codex 不可用 -> engine_unavailable，不产假结论。
- 语雀查询失败 -> 低风险答疑不自动回复。
- Flyeye/Sunfire 查询失败 -> 监控异常不自动回复。
- SQLite 写入失败 -> 不发群，避免无审计记录的回复。
- `reply_enabled=false` -> 不发任何群回复。
- `writes_disabled=true` -> 不生成可执行写操作。

## 7. 上线与运行策略

默认策略：

- `reply_enabled=true` 可开启，但自动回复受 Reply Risk Gate 控制。
- `writes_disabled=false` 可保持现状，但线上写操作仍必须二次确认。
- 审计订正类方案默认只生成待确认执行计划。
- 未授权群不落地、不回复。

休假前建议配置：

- 只对明确值班群开启自动回复。
- `allowed_scenarios` 仅开放 `low_risk_qa`、`monitor_recovered`、`history_duplicate`。
- 不开放异常报警自动回复。
- 每天自动生成运行摘要：自动回复数、待确认数、高风险拦截数、工具失败数。

紧急开关：

- 全局关闭回复：`workspace/config/agent.yaml` 中 `dingtalk.reply_enabled=false`。
- 全局关闭写操作：`writes_disabled=true`。
- 群级关闭自动回复：对应群 `auto_reply=false`。
- 任务暂停：DB state `tasks_paused=1` 或后台控制。

## 8. 主要风险与应对

风险 1：模型仍可能生成无依据结论。

- 应对：Reply Risk Gate 必须独立于模型输出校验 evidence；无 A 档证据禁止自动回复。

风险 2：语雀或代码查询能力不稳定。

- 应对：查询失败转待确认；启动探针展示 qodercli/codex/skill 可用性。

风险 3：真实报警场景复杂，半个月覆盖不全。

- 应对：自动回复只放行恢复、重复、解释型答疑；异常报警默认待确认。

风险 4：方案库沉淀不足。

- 应对：先覆盖高频审计码，其他审计码只解析和报告，不自动处理。

风险 5：当前工作区已有大量未提交变更，直接大改风险高。

- 应对：阶段 1 优先新增独立策略模块和测试，减少对现有 Pipeline 的侵入；后续再逐步改造。

## 9. 最小可交付定义

半个月内最小可交付不追求“什么都能答”，而是达到：

- LocalAgent 能自动回复低风险退改底座答疑，并带语雀/代码证据。
- LocalAgent 能自动回复监控恢复/正常类消息，并引用原始指标证据。
- LocalAgent 能识别高风险报警、审计、订单、trace、金额问题并生成待确认报告。
- LocalAgent 能按审计解决方案生成待确认处理计划，不越权执行。
- 所有自动回复都可在报告和审计日志中回溯。
- 真实回放证明高风险误自动回复为 0。

## 10. 后续实施建议

建议下一步先进入阶段 0 和阶段 1：

1. 固化回放集与期望决策。
2. 新增 `reply_policy` 和 Reply Risk Gate。
3. 改造回复链路，使自动回复必须经过证据和风险校验。
4. 补充 acceptance 测试，证明高风险消息即使群配置允许也不会自动回复。

阶段 1 完成后，再推进退改底座答疑的证据收集链路。这样可以先把休假期间最大的风险“误自动回复”降下来，再逐步提高可用回复率。
