# LocalAgent 休假值守自治能力 Goal 校验报告

日期：2026-08-14

## 1. 校验目标

本次校验按既定 Goal 执行，目标是判断当前 LocalAgent 是否已经达到：

- 只自动回复低风险结论。
- 所有自动回复必须带可回溯证据引用。
- 退改底座低风险答疑允许自动回复，但必须引用语雀、代码或历史报告证据。
- 审计解决方案、监控异常、订单、traceId、金额、资损、责任归因、批量异常、工具失败等高风险事项必须进入待确认或不回复。
- 每次处理都能沉淀报告、证据、工具调用摘要、回复决策原因。

## 2. 总体结论

当前 LocalAgent 的基础闭环稳定，但尚未达到休假值守自治 Goal。

已达标部分：

- 钉钉消息处理、授权群、报警匹配、报告落盘、pending_reply、pending_confirm、写操作门禁、审计播报解析、引擎降级、同类报警关联等基础能力可用。
- 现有自动化测试全部通过。
- 真实报警回归报告校验通过，能验证 evidence 非空、日志取证优先走 `flyeye-log-query` skill、未取证缺口可见。

未达标部分：

- 当前没有独立的 `Reply Risk Gate`。
- 当前没有 `reply_policy.yaml`。
- 当前没有结构化 `scenario_type`、`risk_markers`、`reply_decision`、`reply_reason`。
- 当前自动回复主要由群级 `auto_reply` 或 `auto_reply_types` 决定，没有强制校验 A 档证据。
- 退改底座低风险答疑的语雀/代码证据收集链路尚未实现。
- 报告 JSON 尚未保存回复决策和证据等级。

结论：当前适合作为阶段 0 基线，不适合直接开启休假期间低风险自动回复。必须先完成阶段 1 Reply Risk Gate，再灰度开启自动回复。

## 3. 已执行测试

### 3.1 V1 闭环测试

命令：

```bash
./.venv/bin/python tests/test_v1_closure.py
```

结果：

- 79/79 通过。

覆盖能力：

- 写操作真实执行。
- online 条目进入 `pending_confirm`。
- `writes_disabled` 紧急开关拦截写操作。
- 手动确认执行闭环。
- `auto_reply=false` 进入 `pending_reply`。
- `auto_reply=true` 立即回复。
- 群级 `auto_reply_types` 控制自动回复类型。
- 审计播报解析。
- 方案门禁关闭/开启。
- 多动作执行计划。
- 被抑制报警、仅标题报警解析规则。

关键发现：

- 当前测试证明 `auto_reply=true` 会立即调用 `ding.reply`。
- 当前测试没有要求自动回复必须带 evidence。
- 这与休假自治 Goal 的“所有自动回复必须带 A 档证据”存在缺口。

### 3.2 UI 与引擎输出测试

命令：

```bash
./.venv/bin/python tests/test_v2_ui_engine.py
```

结果：

- 60/60 通过。

覆盖能力：

- prompt 取证策略存在。
- codex JSON 事件流提取。
- evidence 保留。
- 报告 Markdown/JSON 侧车落盘。
- 报告详情页渲染。
- 历史消息筛选。
- pending_reply 页面展示。
- 存储清理边界。

关键发现：

- 报告能保存 `evidence`。
- 当前报告不保存 `reply_decision`、`reply_reason`、`risk_markers`、证据等级。

### 3.3 引擎可用性与降级测试

命令：

```bash
./.venv/bin/python tests/test_v3_engine_availability.py
```

结果：

- 40/40 通过。

覆盖能力：

- 额度/鉴权/限流识别为 `EngineUnavailable`。
- 模型 fallback。
- qodercli 到 codex 的引擎链。
- engine_unavailable 不写报告、不产生假报警。
- 消息保留可重跑。
- 引擎探针和 skill warning 展示。

关键发现：

- 引擎失败不会产出假结论，这符合 Goal。
- 但引擎成功后是否允许自动回复，仍缺少独立风险门禁。

### 3.4 归因与 Aone 测试

命令：

```bash
./.venv/bin/python tests/test_v4_attribution_aone.py
```

结果：

- 19/19 通过。

覆盖能力：

- 归因格式要求。
- 外部返回原文取证要求。
- 取证缺口写归因待定。
- 外部域建议不替外部域决策。
- tech_requirement 草稿和 Aone 创建。

关键发现：

- 归因和技术需求创建已有约束。
- 这类事项按 Goal 必须待确认，不能自动回复；当前自动回复链路尚未显式拦截责任归因类结论。

### 3.5 同类报警关联测试

命令：

```bash
./.venv/bin/python tests/test_v5_correlation.py
```

结果：

- 29/29 通过。

覆盖能力：

- 验价/询价/验座等报警家族归组。
- 单订单重试不升级。
- 多订单批量风险识别。
- 中风险升级 P2，高风险升级 P1。
- prompt 要求批量失败追加 sunfire 和变更取证。

关键发现：

- 多订单批量风险识别可用。
- 当前自动回复门禁还没有把 `batch_impact` 作为统一阻断条件。

### 3.6 总验收

命令：

```bash
./.venv/bin/python tests/acceptance.py
```

结果：

- 21/21 通过。

覆盖能力：

- 监控群正常消息处理。
- 报警命中、生成 pending alert。
- 群回复进入 pending_reply。
- 手动发送回复。
- online 写条目进入 pending_confirm。
- 未授权群不处理。
- 审计播报处理模式。
- 审计方案门禁和两步执行计划。

关键发现：

- 当前默认验收配置中各群 `auto_reply=false`，所以多数群回复进入待确认。
- 该路径较安全，但不等于已具备“低风险可自动回复”的证据门禁能力。

### 3.7 真实报告回归

命令：

```bash
./.venv/bin/python tests/verify_regress_reports.py
```

结果：

- PASS。

覆盖样例：

- `run-4c0abc3ea6c9.json`
- `run-7caa3529eb5c.json`

通过项：

- evidence 非空。
- P1 memory 通道已检索。
- 日志取证经 `flyeye-log-query` skill。
- MCP 降级有标注。
- 取证失败已标注未取证。
- 结论有取证依据。

关键发现：

- 历史真实报告质量有可回归样例。
- 回放规模远未达到 Goal 中 50-100 条真实消息的验收要求。

### 3.8 Skill 配置校验

命令：

```bash
./.venv/bin/python tests/validate_skill_configs.py
```

结果：

- 已校验 `SKILL.md` 85 个。
- 硬性缺陷 0 处。
- 提示 22 处，主要是参考目录无 `SKILL.md` 或 frontmatter 非标准字段。

关键发现：

- skill 配置无硬性阻塞。
- 提示项不影响本次 Goal，但休假前建议保留在运行手册风险项里。

## 4. 静态代码校验结果

### 4.1 自动回复链路

代码位置：

- `localagent/pipeline.py:337-385`

当前逻辑：

- `_build_reply_markdown()` 只使用 `summary` 和 `anomalies` 构造回复。
- `_group_auto_reply()` 只看群级 `auto_reply` 或 `auto_reply_types`。
- `_reply_if_allowed()` 在群配置允许时直接调用 `ding.reply()`。

问题：

- 未校验 evidence 是否存在。
- 未校验证据等级。
- 未校验回复正文是否包含证据引用。
- 未识别订单号、traceId、金额、资损、责任归因、批量风险等高风险标记。
- 未保存 `reply_decision` 和 `reply_reason`。

Goal 判定：未达标。

### 4.2 引擎 evidence 补偿

代码位置：

- `localagent/engine.py:316-330`

当前逻辑：

- 如果异常结论缺 evidence，会触发一次 retry。
- retry 后仍缺 evidence，会写入 `evidence_warning`。

符合项：

- 异常无 evidence 会被识别。
- 报告会提示结论未经证据验证。

问题：

- 该逻辑只处理 `result.get("anomalies") and not result.get("evidence")`。
- 没有把 evidence 缺失传递成自动回复阻断。
- 没有证据等级。

Goal 判定：部分达标。

### 4.3 报告落盘

代码位置：

- `localagent/reports.py:45-85`

当前逻辑：

- Markdown 和 JSON 侧车保存 conclusion、summary、evidence、anomalies、suggestions、correlation、evidence_warning。

符合项：

- evidence 可以回溯。
- 报告落盘稳定。

问题：

- 不保存 `scenario_type`。
- 不保存 `risk_markers`。
- 不保存 `reply_decision`。
- 不保存 `reply_reason`。
- 不保存 evidence `strength/source_ref`。

Goal 判定：部分达标。

### 4.4 场景分类

当前实现：

- `matcher.py` 可解析 Sunfire 报警和 BCP 审计播报。
- `pipeline.py` 可区分非@我报警、@我分析请求、审计播报。
- `correlate.py` 可按关键词族做同类报警关联。

缺口：

- 没有统一 `Scenario Classifier`。
- 没有 `low_risk_qa`。
- 没有 `monitor_recovered` 独立类型。
- 没有 `history_duplicate` 独立类型。
- 没有 `unknown` 统一落点。

Goal 判定：部分达标。

### 4.5 退改底座低风险答疑

当前实现：

- @我消息中包含“分析、报警、审计、订单、traceId、异常、订正、退票、改签、审查、工单”会进入分析引擎。
- engine prompt 中包含改签知识库和本机代码查询要求。

缺口：

- 没有独立低风险答疑分类。
- 没有语雀/代码只读查询结构化 evidence。
- 没有证据引用强制模板。
- 没有高风险伪装答疑拦截测试。

Goal 判定：未达标。

### 4.6 审计解决方案

当前实现：

- `solutions.yaml` 已有 `TRP_INTER_MODIFY_STATUS_ADUIT` 方案。
- 审计播报解析可抽取告警码、规则名、告警时间、owner。
- 方案门禁关闭时拦截写操作。
- 门禁开启时生成 `pending_confirm` 执行计划。

缺口：

- 命中方案后的 evidence 结构化强度不足。
- 审计类回复仍未统一经过 Reply Risk Gate。

Goal 判定：大部分基础达标，但自动回复安全门禁未达标。

### 4.7 监控分析

当前实现：

- Sunfire 文本解析、traceId 提取、同类报警关联、批量风险升级可用。
- prompt 约束要求 sunfire、flyeye、代码取证。

缺口：

- 监控恢复/正常自动回复没有证据模板。
- 异常报警自动回复没有统一阻断策略。
- 工具失败未结构化进入 `risk_markers=tool_failed`。

Goal 判定：部分达标。

## 5. Goal 逐项判定

| Goal 项 | 当前判定 | 依据 |
| --- | --- | --- |
| 正确识别低风险答疑 | 未达标 | 无 `low_risk_qa` 分类 |
| 正确识别监控恢复 | 部分达标 | 可处理正常消息，但无独立类型和证据门禁 |
| 正确识别 Sunfire 异常报警 | 部分达标 | 解析和分析可用，但缺统一风险决策 |
| 正确识别 BCP 审计播报 | 达标 | 播报解析和方案匹配测试通过 |
| 正确识别历史同根因重复报警 | 部分达标 | 同类报警关联可用，但非“同根因报告复用” |
| Reply Risk Gate 生效 | 未达标 | 当前无独立门禁 |
| 自动回复必须带 A 档证据 | 未达标 | 当前回复模板不包含 evidence |
| 群级 auto_reply 不能绕过门禁 | 未达标 | 当前群级允许后直接回复 |
| 退改底座低风险答疑自动回复 | 未达标 | 无结构化证据收集链路 |
| 高风险答疑拦截 | 未达标 | 无统一 risk_markers |
| 审计方案生成处理计划 | 达标 | acceptance 和 V1 测试通过 |
| 审计订正写操作待确认 | 达标 | `pending_confirm` 测试通过 |
| 监控异常待确认 | 部分达标 | 默认群配置安全，但无门禁硬规则 |
| 工具失败不自动回复 | 部分达标 | engine_unavailable 安全；其他工具失败无统一风险标记 |
| 报告和审计可回溯 | 部分达标 | evidence 可回溯，回复决策不可回溯 |

## 6. 当前可开启范围

不建议开启：

- 群级 `auto_reply=true`。
- 任意异常报警自动回复。
- 审计播报自动回复。
- 包含订单、traceId、金额、资损、责任归因的问题自动回复。

可以保守使用：

- 当前默认 `auto_reply=false`，让回复进入 `pending_reply`。
- 审计方案生成 `pending_confirm` 执行计划。
- 引擎不可用后重跑。
- 报警中心查看报告、手动发送回复、手动确认写操作。

## 7. 阻塞休假自治的 P0 缺口

P0-1：新增 Reply Risk Gate。

- 自动回复前必须校验场景白名单、risk_markers、A 档 evidence、回复模板证据引用、群配置和全局开关。
- 没有门禁前，不能开启休假自动回复。

P0-2：自动回复模板必须带证据。

- `_build_reply_markdown()` 必须接收 evidence refs。
- 没有证据引用时只能 `pending_reply`。

P0-3：新增高风险标记。

- 至少识别订单号、改签单号、退票单号、traceId/eagleEyeId、金额、资损、订正、写操作、责任归因、批量影响、工具失败。

P0-4：报告记录回复决策。

- 每次处理必须保存 `reply_decision`、`reply_reason`、`risk_markers`。

## 8. 阻塞解决方案：Reply Risk Gate

针对上述 P0 阻塞，采用“独立 Reply Risk Gate 模块 + 结构化证据与风险标记 + 自动回复硬门禁”的方案解决。核心原则是：分析引擎可以给出结论，但是否能自动发群必须由本地确定性策略决定，不能由模型或群级 `auto_reply` 直接决定。

### 8.1 新增模块

新增模块：

```text
localagent/reply_policy.py
```

模块职责：

```text
analysis result + source_text + group config + evidence
  -> scenario_type
  -> risk_markers
  -> evidence_grade
  -> reply_decision
  -> reply_reason
  -> reply_markdown
```

输出三类回复决策：

- `auto_reply`：允许自动发群。
- `pending_confirm`：生成待确认草稿。
- `no_reply`：不回复，只记录报告和审计。

第一阶段不强制修改 DB schema，优先把结构化结果写入报告 JSON、`audit_logs.detail` 和 `auth_exec.payload`，降低对现有存储的侵入。后续如果查询和统计需求稳定，再补 DB 字段。

### 8.2 结构化结果

分析结果和回复决策需要补齐以下字段：

```json
{
  "scenario_type": "low_risk_qa",
  "risk_markers": ["has_trace_id"],
  "reply_decision": "pending_confirm",
  "reply_reason": "命中高风险标记 has_trace_id，禁止自动回复",
  "evidence": [
    {
      "action": "code-search",
      "finding": "change-flight-tp/.../XxxService.java:123",
      "source_type": "code",
      "source_ref": "change-flight-tp/.../XxxService.java:123",
      "strength": "A"
    }
  ]
}
```

兼容策略：

- 现有 evidence 如果没有 `strength/source_ref`，按规则推导。
- 可回溯代码路径、语雀链接、历史报告 run_id、监控恢复原文可推导为 A 档。
- 只有 `action/finding` 且无明确来源时最多视为 B 档。
- 无 evidence 或 evidence 为空时标记 `evidence_missing`。

### 8.3 自动回复硬门禁

自动回复必须同时满足：

1. `dingtalk.reply_enabled=true`。
2. 群级 `auto_reply=true` 或 `auto_reply_types` 命中。
3. `scenario_type` 属于白名单：
   - `low_risk_qa`
   - `monitor_recovered`
   - `history_duplicate`
4. evidence 至少有 1 条 `strength=A`。
5. 回复正文包含 A 档证据的 `source_ref`。
6. `risk_markers` 为空。
7. analysis result 结构完整，至少包含 `summary/conclusion/evidence`。

任一条件不满足时，不允许自动发群。群级 `auto_reply=true` 只能作为外层放行条件，不能绕过 Reply Risk Gate。

### 8.4 高风险标记

第一阶段必须识别并拦截以下标记：

- `has_order_id`
- `has_modify_id`
- `has_refund_id`
- `has_trace_id`
- `has_amount`
- `has_audit_loss_risk`
- `needs_write`
- `needs_external_attribution`
- `batch_impact`
- `tool_failed`
- `evidence_missing`
- `unknown_scope`

推荐识别规则：

- 订单/改签单/退票单：从 10-20 位数字、上下文关键词和已有订单提取逻辑识别。
- trace/eagleEyeId：识别 `traceId`、`eagleEyeId`、`#Err#` 后长十六进制串等。
- 金额/资损：识别“金额、差异、资损、退款、退票费、赔付、补偿”等关键词。
- 写操作：识别 `suggestions` 中的写类动作、`auth_exec` 执行计划、订正类关键词。
- 外部归因：识别 conclusion 中的【外部域问题】、通知外部域建议或 `notify_external`。
- 批量影响：复用 `correlate.py` 的 `correlation.batch/risk_level`。
- 工具失败：从 evidence finding、`evidence_warning`、engine/tool 审计中识别。
- 未知范围：无法分类或缺少 `scenario_type` 时标记。

### 8.5 Pipeline 接入点

改造位置：

```text
localagent/pipeline.py::_reply_if_allowed()
```

当前逻辑是：

```python
md = self._build_reply_markdown(result)
if self._group_auto_reply(group, alert_type):
    self.ding.reply(group, md)
else:
    pending_reply
```

目标逻辑是：

```python
decision = reply_policy.decide(
    cfg=self.cfg,
    group=group,
    result=result,
    source_text=source_text,
)

if decision.reply_decision == "auto_reply":
    self.ding.reply(group, decision.markdown)
elif decision.reply_decision == "pending_confirm":
    insert pending_reply with decision payload
else:
    audit no_reply
```

`_group_auto_reply()` 保留，但只作为 `reply_policy.decide()` 内部的一个输入条件。

### 8.6 回复模板

自动回复统一使用短模板，必须带依据：

```text
【LocalAgent】结论：{summary}

依据：
1. {source_ref}

仅供参考；涉及线上数据、订正、资损或责任归因请以人工确认为准。
```

如果没有可展示的 A 档 `source_ref`，即使群级允许自动回复，也只能进入 `pending_confirm`。

### 8.7 配置文件

新增配置：

```text
workspace/config/reply_policy.yaml
```

建议初始内容：

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
    - has_modify_id
    - has_refund_id
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

默认策略建议保守：

- 新配置存在但初期可把 `auto_reply.enabled=false` 用于灰度。
- 阶段 1 测试通过后，再对低风险群开启。
- 审计、异常报警、写操作相关场景不进入白名单。

### 8.8 分阶段落地

阶段 1：门禁 MVP。

- 新增 `reply_policy.py`。
- 新增 `workspace/config/reply_policy.yaml`。
- 实现 `risk_markers` 识别。
- 实现 evidence A/B/C 推导。
- 实现 `auto_reply/pending_confirm/no_reply` 决策。
- 自动回复强制包含 A 档证据。
- 报告 JSON 写入 `reply_decision/reply_reason/risk_markers`。

阶段 2：接入现有 Pipeline。

- 改造 `_reply_if_allowed()`。
- 保留现有群级配置作为外层开关。
- 让群级 `auto_reply=true` 无法绕过 Reply Risk Gate。
- 将待确认草稿 payload 补充门禁结果和拦截原因。

阶段 3：补充测试。

- 覆盖无 evidence、traceId、订单号、金额、审计订正、批量影响、工具失败等拦截场景。
- 覆盖 `low_risk_qa + A 档代码证据` 和 `monitor_recovered + 原始指标证据` 自动回复场景。
- 覆盖 `reply_enabled=false`、群级 `auto_reply=false`、`auto_reply_types` 未命中场景。

阶段 4：再建设低风险答疑 evidence 收集。

- 接入 qodercli/codex 只读查询语雀和代码。
- 输出结构化 `source_type/source_ref/strength`。
- 逐步提高低风险答疑自动回复率。

### 8.9 必须新增的测试用例

阻塞解除前必须新增并通过以下测试：

- `auto_reply=true + 无 evidence -> pending_confirm`
- `auto_reply=true + traceId -> pending_confirm`
- `auto_reply=true + 订单号 -> pending_confirm`
- `auto_reply=true + 金额 -> pending_confirm`
- `low_risk_qa + A档代码证据 -> auto_reply`
- `monitor_recovered + 原始指标证据 -> auto_reply`
- `audit_solution + 写操作建议 -> pending_confirm`
- `batch_impact -> pending_confirm`
- `tool_failed -> pending_confirm`
- `reply_enabled=false -> no_reply`
- `auto_reply_types 未命中 -> pending_confirm`
- `自动回复正文不含 source_ref -> pending_confirm`

### 8.10 阻塞解除验收标准

以下全部满足后，才视为休假自治 P0 阻塞解除：

1. 群级 `auto_reply=true` 不能绕过风险门禁。
2. 无 A 档证据不会自动回复。
3. 所有自动回复正文都包含依据。
4. 所有高风险标记都会转 `pending_confirm` 或 `no_reply`。
5. 报告 JSON 可看到 `reply_decision`、`reply_reason`、`risk_markers`。
6. 待确认草稿 payload 可看到门禁结果和拦截原因。
7. 现有 acceptance 和 v1-v5 测试全部通过。
8. 新增 Reply Risk Gate 单元测试全部通过。

## 9. P1 建设项

P1-1：退改底座低风险答疑分类。

- 支持解释型、知识型、代码定位型问题。
- 高风险伪装问题必须拦截。

P1-2：语雀/代码只读 evidence 收集。

- qodercli/codex 可执行只读查询。
- 输出结构化 `source_type/source_ref/strength`。

P1-3：真实回放集。

- 从历史消息和报告中整理 50-100 条回放样例。
- 标注期望 `auto_reply/pending_confirm/no_reply`。

## 10. 建议下一步执行顺序

1. 新增 `reply_policy.py` 和 `workspace/config/reply_policy.yaml`。
2. 实现 `risk_markers`、evidence A/B/C 推导和 `auto_reply/pending_confirm/no_reply` 决策。
3. 改造 `_reply_if_allowed()`，让群级自动回复不能绕过风险门禁。
4. 扩展报告 JSON 和待确认草稿 payload，写入 `reply_decision`、`reply_reason`、`risk_markers`。
5. 增加 Reply Risk Gate 单元测试：无 evidence 自动回复拦截、traceId 拦截、订单号拦截、金额拦截、审计订正待确认、批量影响拦截、工具失败拦截。
6. 跑通现有 acceptance、v1-v5 和新增门禁测试。
7. 再推进阶段 2：低风险答疑 evidence 收集和自动回复。

## 11. 最终判定

本次 Goal 执行结论：

- 当前基线测试通过。
- 当前具备“分析和待确认”能力。
- 当前不具备“休假期间低风险自动回复”的安全门禁。
- 达到休假自治前，必须完成 Reply Risk Gate 和证据引用强制。

最终状态：未达标，建议进入阶段 1 整改。
