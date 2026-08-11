import asyncio
import json
import re
import shutil

PROMPT_TEMPLATE = """你是退改系统值班分析助手。你拥有本机 CLI 的全部 skills 与 MCP 能力。

取证通道优先级（强制策略，按序执行，不得跳级）：
P1 记忆优先：动手取证前必须先调用 memory skill 检索智能体自身记忆（同类告警的历史处置经验、既有结论与排查规范），把命中的记忆作为取证起点。
P2 skill 取证：所有取证优先使用 skill。其中查 Flyeye 日志必须使用 `flyeye-log-query` skill；
   严禁直接调用 flyeye MCP 或自行拼参数绕过 skill（历史报告错误的主因即直调 MCP 失败/误用）。
P3 降级 MCP：仅当 skill 确实不可用（未安装 / 调用失败）时，才允许降级到对应 MCP，
   且必须在 evidence 的 action 中标注实际所用通道：memory / skill:<名称> / MCP:<工具名>（降级时补记 skill 不可用原因）。
P4 未取证兜底：若记忆、skill、MCP 全部不可用，必须在 evidence 中如实标注「未取证」及原因，
   conclusion 说明取证缺口与还需查什么；严禁在未取证情况下臆断归因或编造日志内容。

取证铁律（违反即视为分析失败）：
1. 查 Flyeye 日志必须先调用 `flyeye-log-query` skill，严格按 skill 内参数规范执行；禁止绕过 skill 自行拼参数直调底层 MCP。
   关键规范：bizGroup=all（不要填应用名）；queryType 只取 objId（订单号）或 eagleEyeId（鹰眼ID），URL 的 orderId= 不是合法 queryType；
   订单号与鹰眼 ID 都走 orderId 字段；logLevel 过滤查询用 ERROR,WARN、pageSize=50；
   时间窗用毫秒时间戳且 startTime<endTime：30 位鹰眼 ID 的第 9-21 位是入口毫秒，startTime=trace_ms-60000，同步请求 endTime=trace_ms+600000；
   <24h 日志 queryHistoryLibrary=false，>=24h 才 true；queryRelatedEagleEyeLog 默认 false。
2. 动手前先调用 memory skill 检索本地记忆中的相关规范与历史结论（P1）。
3. 有订单号 → 用 flight-order-data-query / 退改业务分析 skill 核对订单实际状态与金额。
4. Sunfire 报警 → 用 sunfire-cli 查指标趋势与报警配置，判断是否瞬时波动。
5. 基于取证给出明确结论：是什么问题 / 排除了什么，依据是什么；某项取证失败时如实记录失败原因（参数错误/服务异常），并给出还需查什么。
6. 上下文含 traceId（鹰眼ID）或订单号时，必须先完成 flyeye-log-query / 订单查询取证再下结论；禁止未取证直接输出结论。
   evidence 数组必须非空：每个取证动作一条记录，含动作与关键发现；取证失败也要记录，finding 写明失败原因。
7. BCP/审计播报类告警（含告警码如 TRP_xxx_ADUIT / FLIGGY_xxx_ADUIT）→ 优先用 jarvis MCP（list_audit_scripts 按 monitorIdentifier=告警码、list_audit_rules_by_script、list_audit_rule_check_points 等）深挖规则定义与校验点，再结合订单/日志 skill 取证；
   禁止用 WebFetch 抓取 bcp.alibaba-inc.com 页面（SSO 拦截，属无效取证）；上下文给出沉淀解决方案时，必须按方案步骤取证，suggestions 的 app/feature 与授权条目保持一致。

取证完成后严格只输出 JSON：
{{"normal": bool, "conclusion": str（一句话明确结论+依据，≤60字，精炼不啰嗦）,
"summary": str（列表展示用的极简摘要，≤30字，只说结论不谈过程）,
"evidence": [{{"action": str（排查动作，如 flyeye-log-query skill 查询 eagleEyeId=xxx）, "finding": str（关键发现/日志摘录，详细完整，含关键数值与原文摘录）}}],
"anomalies": [{{"severity": "P1|P2|P3", "summary": str（≤30字精炼概括）}}],
"suggestions": [{{"app": str, "feature": str, "action_type": "data_correction", "params": {{}}}}]}}
要求：报警中心列表只展示 summary/anomalies.summary，必须精炼；详细分析过程全部写入 evidence，报告将完整呈现取证与结论。
上下文：{context}"""


def _mock_analyze(ctx_text, source):
    if "金额不一致" in ctx_text or "REFUND_FEE_MISMATCH" in ctx_text:
        m = re.search(r"订单[号:]?\s*(\d{10,20})", ctx_text)
        order = m.group(1) if m else "91234567890"
        return {
            "normal": False,
            "summary": "发现退票费金额不一致异常",
            "conclusion": "订单退票费与规则计算值不一致，差异 45 元，疑似回填错误，需订正。",
            "evidence": [{"action": "mock 核对订单费用", "finding": "实收退票费 145 元，规则应退 100 元，差异 45 元"}],
            "anomalies": [{"severity": "P2", "summary": f"订单 {order} 退票费与规则计算值不一致，差异 45 元"}],
            "suggestions": [{"app": "flyrp", "feature": "退票费金额不一致订正",
                             "action_type": "data_correction",
                             "params": {"orderId": order, "correctFee": "155.00"}}],
        }
    if "TRP_INTER_MODIFY_STATUS_ADUIT" in ctx_text or '"kind": "audit_broadcast"' in ctx_text:
        return {
            "normal": False,
            "summary": "国际改签状态流转审计告警，状态不一致",
            "conclusion": "改签单 orderStatus=改签成功 但 bizStatus=30 子状态异常，状态流转不一致，需核对订正。",
            "evidence": [{"action": "mock 解析审计播报", "finding": "命中告警码 TRP_INTER_MODIFY_STATUS_ADUIT，BCP 资损审计"},
                         {"action": "mock 状态核对", "finding": "orderStatus=改签成功, payStatus=转交易成功, bizStatus=30(卖家待出票)，子状态异常"}],
            "anomalies": [{"severity": "P2", "summary": "国际改签状态流转不一致（bizStatus=30）"}],
            "suggestions": [{"app": "ateye", "feature": "改签单subStatus订正（trp Ateye）",
                             "action_type": "data_correction",
                             "params": {"modifyId": "91234567890", "subStatus": "TO_BE_CONFIRMED"}}],
        }
    if "审计" in ctx_text and ("订正" in ctx_text or "错误" in ctx_text):
        return {
            "normal": False,
            "summary": "审计问题确认：存在需订正数据",
            "anomalies": [{"severity": "P3", "summary": "审计发现待订正数据项"}],
            "suggestions": [{"app": "ateye", "feature": "改签单subStatus订正（trp Ateye）",
                             "action_type": "data_correction",
                             "params": {"modifyId": "91234567890", "subStatus": "TO_BE_CONFIRMED"}}],
        }
    return {"normal": True, "summary": "分析完成，未发现异常",
            "conclusion": "经核对上下文无异常指标与错误日志，判定无问题。",
            "evidence": [{"action": "mock 上下文核对", "finding": "无报警指标/错误日志命中"}],
            "anomalies": [], "suggestions": []}


def _extract_json(raw):
    """从可能含多段 JSON/杂散文本的输出中提取最后一个含 normal/conclusion 的 JSON 对象。"""
    dec = json.JSONDecoder()
    best = None
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(raw[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and ("normal" in obj or "conclusion" in obj):
            best = obj
    return best


async def _run_engine(cfg, name, prompt, db=None, run_id=None):
    cmd = cfg.engine_cmd(name)
    if not cmd:
        raise RuntimeError(f"engine {name} 未配置")
    argv = [c.replace("{prompt}", prompt) for c in cmd]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
    raw = out.decode()
    result = _extract_json(raw)
    if result is None:
        if db:
            db.audit("engine", "raw_output", name,
                     f"stdout={raw[-800:]} | stderr={err.decode()[-300:]}", run_id)
        raise RuntimeError(f"{name} 输出无有效 JSON: {raw[:150]} / err={err.decode()[:100]}")
    return result


async def analyze(cfg, db, context: dict, run_id):
    ctx_text = context.get("text", "") + " " + json.dumps(context.get("extra", {}), ensure_ascii=False)
    if cfg.mock:
        await asyncio.sleep(0.2)
        result = _mock_analyze(ctx_text, context.get("source"))
        engine, version = "mock-qoder", "1.20.0"
    else:
        name = cfg.engines.get("default", "qodercli")
        engine, version = name, "unknown"
        prompt = PROMPT_TEMPLATE.format(context=ctx_text)
        try:
            result = await _run_engine(cfg, name, prompt, db, run_id)
        except Exception as e:
            db.audit("engine", "engine_failed", name, str(e), run_id)
            fb_raw = cfg.engines.get("fallback", [])
            fallback_chain = [fb_raw] if isinstance(fb_raw, str) else fb_raw
            success = False
            for fb in fallback_chain:
                if fb == name:
                    continue
                try:
                    result = await _run_engine(cfg, fb, prompt, db, run_id)
                    engine = fb
                    success = True
                    break
                except Exception as fe:
                    db.audit("engine", "engine_failed", fb, str(fe), run_id)
            if not success:
                raise
        if result.get("anomalies") and not result.get("evidence"):
            db.audit("engine", "evidence_missing_retry", engine, "", run_id)
            retry_prompt = (prompt + "\n\n【重要纠正】你上一次输出直接给出了异常结论但没有任何取证记录，该结论已被拒绝。"
                            "请严格按取证通道优先级重做：先 memory skill 检索记忆，再用 flyeye-log-query / sunfire-cli / "
                            "订单查询等 skill 取证（禁止直调 flyeye MCP），skill 不可用才降级 MCP 并在 action 标注通道；"
                            "再重新输出 JSON，evidence 数组不得为空；取证失败或全通道不可用需在 evidence 记录失败原因或标注「未取证」。")
            try:
                retried = await _run_engine(cfg, engine, retry_prompt, db, run_id)
                if retried.get("evidence"):
                    result = retried
            except Exception as e:
                db.audit("engine", "evidence_retry_failed", engine, str(e)[:200], run_id)
            if not result.get("evidence"):
                result["evidence_warning"] = ("⚠️ 引擎给出异常结论但未完成任何取证，结论未经证据验证，仅供参考；"
                                              "请在报警中心点\"重新分析\"补充线索。")
    db.audit("engine", "analyze_done", engine, json.dumps(result, ensure_ascii=False)[:500], run_id)
    return result, engine, version


async def detect_versions(cfg):
    """探测各引擎可用性（PRD 8.2.1 / 12.4）：返回 {name: version 或 错误信息}。"""
    out = {}
    for e in cfg.engines.get("list", []):
        name, cmd = e.get("name"), e.get("cmd") or []
        if not cmd:
            out[name] = "未配置"
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd[0], "--version", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            o, err = await asyncio.wait_for(proc.communicate(), timeout=10)
            txt = (o.decode() or err.decode()).strip().splitlines()
            out[name] = txt[0][:60] if txt and proc.returncode == 0 else f"不可用: {(err.decode() or o.decode())[:60]}"
        except Exception as ex:
            out[name] = f"探测失败: {str(ex)[:60]}"
    return out
