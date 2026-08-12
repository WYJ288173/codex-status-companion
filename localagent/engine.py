import asyncio
import json
import re

from .db import now

PROMPT_TEMPLATE = """你是退改系统值班分析助手。你拥有本机 CLI 的全部 skills 与 MCP 能力。

知识源（分析前必读/必查）：
- 改签技术知识库主干 L0：https://aliyuque.antfin.com/serveflightchange/aicoding/kxxv0lbxhnq79l8t （分析改签报警先读 L0 主干定位链路，再按需查同空间 serveflightchange/aicoding 下的业务知识库章节）
- 改签域应用代码在本机 ~/developer/ 下：change-flight-tp（改签底座：adapter/application/domain/infrastructure 等模块）、flycp、atr、flybuy、flybp、flyragg、flyasp、atus。代码级定位必须读真实源码，禁止凭印象描述代码。
- 改签域先验知识（用户确认，可直接作为分析前提，仍需用日志验证）：供销链路预定流程 = change-flight-tp 调用行业平台接口预订 → 行业平台预订成功则返回"行业改签单号" → 改签域基于行业改签单号反查行业改签单 → 获取 PNR。若行业预订失败、未返回行业改签单号，改签域后续基于该单号反查/取值会得到 null 并抛 NPE（CreatePnrAdapterServiceImpl adapter 层预定流程异常）。因此供销链路预定 NPE 报警的归因方向是"行业平台预订失败未返回行业改签单号"，须沿 行业预订调用→返回值→反查链路 取证，禁止臆测序列化/响应解析等其他原因。

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
8. 异常深挖与代码级根因（报警含 tracerId 时必须执行）：flyeye 日志命中 Java 异常（NullPointerException / TimeoutException / 业务异常等）时，不得止步于"有异常"，必须：
   a. 提取完整堆栈：异常类名、抛出点 类全名.方法名:行号、关键调用链帧；日志被截断时换关键词/缩小时间窗重查，直到拿到堆栈帧；
   b. 到本机代码仓库定位源码（grep 类名/日志文案）：改签底座 ~/developer/change-flight-tp、flycp ~/developer/flycp、atr ~/developer/atr、flybuy ~/developer/flybuy、flybp ~/developer/flybp、flyragg ~/developer/flyragg、flyasp ~/developer/flyasp、atus ~/developer/atus、atr ~/developer/atr；
   c. 结合抛出点上下文代码分析具体报错：哪个对象为 null / 哪行抛出 / 什么条件触发，写进 evidence（含 文件:行号 与关键代码摘录）；
   d. conclusion 必须落到代码级根因（如"XxxServiceImpl.java:123 处 order.getOffer() 为 null，因上游未回填 offer"）；确实定位不到源码时在 evidence 标注「代码未定位」及原因，禁止只给"某处 NPE 需排查"这类无落点结论。
9. 反编造硬约束（证据可回溯，违反即报告作废）：
   a. evidence 引用的每条日志必须来自本次实际查询结果，且带可回溯要素：时间戳（精确到毫秒）、应用名、rpc id 或日志原文摘录；禁止凭记忆/常识/其他 trace 的内容补写证据；
   b. 因果链（A 异常导致 B 异常）成立的必要条件：A、B 均有日志证据，且同属目标 trace / rpc 链、时间先后吻合；任一环节缺证据时，只能写"伴生现象"或"未证实关联"，禁止写成因果；
   c. 下结论前自查：conclusion/anomalies 里出现的每个异常类名、错误信息、中间件名，是否都能在 evidence 中找到带时间戳的出处？找不到的一律删除或降级为"未证实"；
   d. 未能独立验证的事实（如订单终态因工具不可用没查到）必须在 evidence 显式标注「未验证」，禁止用模糊表述掩盖缺口。

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


def _scan_json(raw):
    """扫描文本中所有 JSON 对象，返回最后一个含 normal/conclusion 的对象。"""
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


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _extract_json(raw):
    """从引擎输出中提取结论 JSON。
    先直接扫描；未命中时按 JSONL 事件流（codex --json）解析，结论 JSON 常被转义嵌在事件字符串字段中。"""
    best = _scan_json(raw)
    if best is not None:
        return best
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        for s in _iter_strings(evt):
            if "normal" not in s and "conclusion" not in s:
                continue
            got = _scan_json(s)
            if got is not None:
                best = got
    return best


class EngineUnavailable(RuntimeError):
    """引擎资源不可用（额度/限流/鉴权），非分析失败，可重试。"""


RESOURCE_HINTS = ("credit usage limit", "usage limit", "rate limit", "quota",
                  "insufficient credit", "upgrade your subscription",
                  "too many requests", "unauthorized", "not logged in",
                  "please login", "authentication failed")


def engine_models(cfg, name):
    """候选模型链：显式 model 优先，未配置时首位为 None（引擎默认模型），其后接 model_fallback。"""
    for e in cfg.engines.get("list", []):
        if e.get("name") != name:
            continue
        head = [e["model"]] if e.get("model") else [None]
        return head + [m for m in (e.get("model_fallback") or []) if m != e.get("model")]
    return [None]


def _model_flag(cfg, name):
    for e in cfg.engines.get("list", []):
        if e.get("name") == name:
            return e.get("model_flag", "-m")
    return "-m"


def engine_chain(cfg):
    """引擎链：default 在前，其后 fallback（去重）。"""
    name = cfg.engines.get("default", "qodercli")
    fb_raw = cfg.engines.get("fallback", [])
    fb = [fb_raw] if isinstance(fb_raw, str) else list(fb_raw or [])
    chain = [name] + [x for x in fb if x and x != name]
    return chain


SKILL_WARN_RE = re.compile(r"(\d+)\s+warnings?\s+loading\s+skill\s+configs", re.I)


def parse_skill_warnings(stderr_text):
    """从引擎 stderr 解析 skill 配置告警数；未出现该提示时返回 None。
    有告警意味着部分 skill 未注册/配置损坏，会直接削弱取证能力，需在后台可见。"""
    if not stderr_text:
        return None
    m = SKILL_WARN_RE.search(stderr_text)
    return int(m.group(1)) if m else None


async def _run_engine(cfg, name, prompt, db=None, run_id=None, model=None, timeout=900,
                      stats=None):
    cmd = cfg.engine_cmd(name)
    if not cmd:
        raise RuntimeError(f"engine {name} 未配置")
    argv = [c.replace("{prompt}", prompt) for c in cmd]
    if model:
        argv += [_model_flag(cfg, name), model]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    raw = out.decode()
    err_text = err.decode()
    if stats is not None:
        warns = parse_skill_warnings(err_text)
        if warns is not None:
            stats["skill_warnings"] = warns
    result = _extract_json(raw)
    if result is None:
        tag = f"{name}" + (f"/{model}" if model else "")
        if db:
            db.audit("engine", "raw_output", tag,
                     f"stdout={raw[-800:]} | stderr={err_text[-300:]}", run_id)
        low = (raw + " " + err_text).lower()
        if any(h in low for h in RESOURCE_HINTS):
            raise EngineUnavailable(f"{tag} 资源不可用（额度/限流/鉴权）：{raw.strip()[:150]}")
        raise RuntimeError(f"{tag} 输出无有效 JSON: {raw[:150]} / err={err_text[:100]}")
    return result


async def _run_with_downgrade(cfg, db, prompt, run_id):
    """按「引擎链 × 模型链」逐级降级执行；返回 (result, engine, model)。
    资源不可用先同引擎换模型，再换引擎；全部资源不可用抛 EngineUnavailable。"""
    # 深度取证（堆栈+源码定位）实测需 20-40 分钟，15 分钟不够；可经 engines.timeout_seconds 调整
    timeout = int(cfg.engines.get("timeout_seconds", 1800))
    last_real_error = None
    unavailable = []
    for ei, name in enumerate(engine_chain(cfg)):
        models = engine_models(cfg, name)
        engine_all_unavailable = True
        for mi, model in enumerate(models):
            if ei or mi:
                db.audit("engine", "model_downgraded" if ei == 0 else "engine_downgraded",
                         f"{name}/{model or 'default'}",
                         f"前序不可用，降级重试：{'; '.join(unavailable)[-200:]}", run_id)
            try:
                result = await _run_engine(cfg, name, prompt, db, run_id, model=model,
                                           timeout=timeout)
                return result, name, model
            except EngineUnavailable as e:
                unavailable.append(str(e)[:120])
                db.audit("engine", "engine_resource_unavailable",
                         f"{name}/{model or 'default'}", str(e)[:200], run_id)
            except Exception as e:
                engine_all_unavailable = False
                last_real_error = e
                db.audit("engine", "engine_failed", f"{name}/{model or 'default'}",
                         str(e)[:300], run_id)
        if engine_all_unavailable and models:
            db.audit("engine", "engine_models_exhausted", name,
                     f"{len(models)} 个模型均资源不可用", run_id)
    if last_real_error is not None:
        raise last_real_error
    raise EngineUnavailable("所有引擎与模型均资源不可用：" + "; ".join(unavailable)[-400:])


async def analyze(cfg, db, context: dict, run_id):
    ctx_text = context.get("text", "") + " " + json.dumps(context.get("extra", {}), ensure_ascii=False)
    if cfg.mock:
        await asyncio.sleep(0.2)
        result = _mock_analyze(ctx_text, context.get("source"))
        engine, version = "mock-qoder", "1.20.0"
    else:
        prompt = PROMPT_TEMPLATE.format(context=ctx_text)
        result, engine, model = await _run_with_downgrade(cfg, db, prompt, run_id)
        version = model or "default-model"
        if result.get("anomalies") and not result.get("evidence"):
            db.audit("engine", "evidence_missing_retry", engine, "", run_id)
            retry_prompt = (prompt + "\n\n【重要纠正】你上一次输出直接给出了异常结论但没有任何取证记录，该结论已被拒绝。"
                            "请严格按取证通道优先级重做：先 memory skill 检索记忆，再用 flyeye-log-query / sunfire-cli / "
                            "订单查询等 skill 取证（禁止直调 flyeye MCP），skill 不可用才降级 MCP 并在 action 标注通道；"
                            "再重新输出 JSON，evidence 数组不得为空；取证失败或全通道不可用需在 evidence 记录失败原因或标注「未取证」。")
            try:
                retried = await _run_engine(cfg, engine, retry_prompt, db, run_id, model=model)
                if retried.get("evidence"):
                    result = retried
            except Exception as e:
                db.audit("engine", "evidence_retry_failed", engine, str(e)[:200], run_id)
            if not result.get("evidence"):
                result["evidence_warning"] = ("⚠️ 引擎给出异常结论但未完成任何取证，结论未经证据验证，仅供参考；"
                                              "请在报警中心点\"重新分析\"补充线索。")
    db.audit("engine", "analyze_done", engine, json.dumps(result, ensure_ascii=False)[:500], run_id)
    return result, engine, version


PROBE_PROMPT = ('Reply with exactly this JSON and nothing else: '
                '{"normal": true, "conclusion": "probe"}')


async def probe_engines(cfg, db=None):
    """最小探针：逐个引擎 × 候选模型实跑一次，记录可用性到 conn_state.engine_probe。"""
    out = {}
    stats = {}
    for name in engine_chain(cfg):
        if not cfg.engine_cmd(name):
            out[name] = "未配置"
            continue
        for model in engine_models(cfg, name):
            key = f"{name}/{model or 'default'}"
            try:
                await _run_engine(cfg, name, PROBE_PROMPT, None, None,
                                  model=model, timeout=180, stats=stats)
                out[key] = "ok"
            except EngineUnavailable as e:
                out[key] = f"资源不可用: {str(e)[:80]}"
            except Exception as e:
                out[key] = f"失败: {str(e)[:80]}"
    if db is not None:
        db.set_state("engine_probe", json.dumps(out, ensure_ascii=False))
        db.set_state("engine_probe_at", now())
        if "skill_warnings" in stats:
            db.set_state("skill_config_warnings", str(stats["skill_warnings"]))
            if stats["skill_warnings"]:
                db.audit("engine", "skill_config_warnings", "",
                         f"{stats['skill_warnings']} 个 skill 配置告警，部分 skill 未注册，取证能力被削弱")
        db.audit("engine", "engines_probed", "", json.dumps(out, ensure_ascii=False)[:400])
    return out


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
