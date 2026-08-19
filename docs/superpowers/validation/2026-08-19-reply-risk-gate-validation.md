# Reply Risk Gate 验证报告：休假值守自治 P0 阻塞解除判定

日期：2026-08-19
依据：《LocalAgent 休假值守自治能力 Goal 校验报告》（2026-08-14）§8 阻塞解决方案

## 1. 验证目标

验证最新改动是否解除休假值守自治的 5 项 P0 阻塞：
1. 无独立 Reply Risk Gate
2. 自动回复未强制校验 A 档证据
3. 群级 auto_reply=true 可绕过风险门禁
4. 无 risk_markers/reply_decision/reply_reason 结构化记录
5. 报告 JSON 与待确认草稿 payload 缺回复决策与拦截原因

判定口径（写死）：**仅当「高风险误自动回复数 = 0」且「所有自动回复都含 A 档证据引用」同时满足，才判定 P0 阻塞解除。**

## 2. 执行命令

```bash
# 静态验证
ls localagent/reply_policy.py workspace/config/reply_policy.yaml
grep -n "_gate_evaluate\|rp.decide\|_patch_report_meta\|gate_meta" localagent/pipeline.py

# 单元/集成/回归
./.venv/bin/python tests/test_v12_reply_risk_gate.py      # 门禁单测（16 场景）
./.venv/bin/python tests/test_v13_gate_integration.py     # LOCALAGENT_MOCK=1 全流程集成
./.venv/bin/python tests/test_v1_closure.py ... test_v11_normal_reply.py
./.venv/bin/python tests/acceptance.py
./.venv/bin/python tests/verify_regress_reports.py
./.venv/bin/python tests/validate_skill_configs.py

# 真实样例回放
./.venv/bin/python tests/replay_risk_gate.py
```

## 3. 静态代码检查结论（全部通过）

| 检查项 | 结论 |
| --- | --- |
| 独立门禁模块 `localagent/reply_policy.py` | ✅ 场景分类/12 风险标记/A-B 证据分级/三类决策 |
| 策略配置 `workspace/config/reply_policy.yaml` | ✅ 白名单场景+A 档强制+12 阻断标记；当前 enabled=false 灰度保守态 |
| Pipeline 接入 | ✅ `_gate_evaluate()` 统一评估；异常结论走 `_reply_if_allowed`，无问题结论先过门禁再回退简短确认 |
| 群级配置不可绕过 | ✅ `auto_reply_types` 仅作外层放行输入；决策由门禁硬规则产出（v12「白名单命中但门禁拦截仍转待确认」断言通过） |
| 决策落盘 | ✅ `_patch_report_meta` 写 reply_decision/reply_reason/risk_markers/scenario_type/evidence_grade 至报告 JSON 侧车 |
| pending payload | ✅ 两处 pending_reply 插入均带 `"gate": gate_meta`（决策+原因+标记） |

## 4. 测试结果

### 4.1 单元测试（test_v12，30 断言全过）

Goal §2 全部 16 场景覆盖：

| 场景 | 期望 | 结果 |
| --- | --- | --- |
| 无 evidence | pending_confirm | ✅ evidence_missing 标记 |
| 仅 B 档 evidence | pending_confirm | ✅ 「缺少 A 档可回溯证据」 |
| traceId/eagleEyeId | pending_confirm | ✅ has_trace_id |
| 订单号 | pending_confirm | ✅ has_order_id |
| 金额/资损 | pending_confirm | ✅ has_amount |
| suggestions 写动作 | pending_confirm | ✅ needs_write |
| 外部域归因 | pending_confirm | ✅ needs_external_attribution（v6 §4 断言） |
| batch_impact | pending_confirm | ✅ |
| tool_failed/evidence_warning | pending_confirm | ✅ |
| low_risk_qa + A 档代码证据 | auto_reply | ✅ 正文含 source_ref |
| monitor_recovered + 原始指标证据 | auto_reply | ✅ |
| history_duplicate + run_id 证据 | auto_reply | ✅ |
| reply_enabled=false | no_reply | ✅ |
| 群级未放行 | pending_confirm | ✅ |
| auto_reply_types 未命中 | pending_confirm | ✅ |
| A 档但无可展示 source_ref | pending_confirm | ✅ |

### 4.2 集成测试（test_v13，LOCALAGENT_MOCK=1 全流程，18 断言全过）

- I1 低风险答疑（@我）→ 自动发群，正文含「依据：」与 A 档 source_ref；报告 JSON reply_decision=auto_reply ✅
- I2 监控恢复 → 自动发群，含依据引用 ✅
- I3 订单号+金额高风险 → 不自动发群；pending payload 带 gate（has_order_id/has_amount）；报告 JSON 记录 risk_markers ✅
- I4 审计播报命中方案 → 写操作 pending_confirm；不自动发群；gate 标记 has_audit_loss_risk+needs_write ✅
- I5 多订单批量（3 单）→ 报告 JSON risk_markers 含 batch_impact、reply_decision=pending_confirm；不自动发群 ✅

### 4.3 回归测试（全部通过）

| 套件 | 结果 |
| --- | --- |
| test_v1_closure | 84/84 |
| test_v2_ui_engine | 60/60 |
| test_v3_engine_availability | 40/40 |
| test_v4_attribution_aone | 19/19 |
| test_v5_correlation | 30/30 |
| test_v6_reply_gate | 82/82 |
| test_v7_reply_identity | 19/19 |
| test_v8_correlation_accuracy | 15/15 |
| test_v9_aggregation | 17/17 |
| test_v10_batch_header | 11/11 |
| test_v11_normal_reply | 11/11 |
| test_v12_reply_risk_gate | 30/30 |
| test_v13_gate_integration | 18/18 |
| acceptance | 21/21 |
| verify_regress_reports | PASS |
| validate_skill_configs | 85 个 SKILL.md，硬性缺陷 0，提示 23 |

合计 407 断言 + 2 套件判定，全部通过。

## 5. 真实样例回放结果（tests/replay_risk_gate.py）

- 样例规模：**81 条**（Goal 要求 ≥20；达到建议的 50-100 区间）
- 覆盖类别（真实数据）：Sunfire 异常报警、订单号/traceId/金额高风险、同类多订单批量（batch_impact）、外部域归因、写操作建议、工具失败、历史同根因重复（history_duplicate）
- 真实数据中无低风险答疑/监控恢复正样本 → 该两类 auto_reply 正向路径由 v12 单测 + v13 集成（I1/I2）覆盖
- 逐条记录：run_id / scenario_type / risk_markers / 期望决策 / 实际决策 / 一致性（完整表由脚本输出）
- **期望与实际决策一致率：81/81 = 100%**
- **高风险误自动回复数：0**（81 条真实样例全部 pending_confirm，无一误放行）
- 门禁开启态（enabled=true）下所有 auto_reply 输出强制校验 A 档 source_ref 出现在回复正文（脚本内置断言）

## 6. 未通过项

无。

## 7. 最终判定

**P0 阻塞解除。** 判定依据：
1. 高风险误自动回复数 = 0 ✅（81 条真实回放 + 全部高风险单测场景）
2. 所有自动回复均含 A 档证据引用 ✅（门禁硬规则 + 单测/集成断言）
3. 群级 auto_reply=true 不能绕过门禁 ✅
4. risk_markers/reply_decision/reply_reason 在报告 JSON 与 pending payload 可回溯 ✅
5. 现有回归全过，旧能力无破坏 ✅

## 8. 灰度建议

**可以进入「低风险自动回复灰度」**，建议步骤：
1. 将 `workspace/config/reply_policy.yaml` 的 `auto_reply.enabled` 置为 `true`（当前为 false 保守态，改动后重启服务生效）。
2. 灰度首周仅保留白名单三场景（low_risk_qa/monitor_recovered/history_duplicate）；每日用 `tests/replay_risk_gate.py` 回放新增报告，确认高风险误自动持续为 0。
3. 观察指标：`audit_logs` 中 reply_auto（含 normal）与 reply_pending 比例、reply_auto_blocked 拦截原因分布。
4. 已知限制（P1，不阻塞灰度）：真实答疑的 A 档证据依赖引擎自觉输出 source_type/source_ref（提示词已约束），灰度期如出现答疑 A 档证据率偏低，可收紧为仅 monitor_recovered 自动。
5. 回滚开关：`auto_reply.enabled: false` 一键回到全待确认模式。
