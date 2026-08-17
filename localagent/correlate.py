"""同类报警归类分析：近 10 分钟窗口同类报警归组 + 订单比对（单订单重试 vs 多订单批量）
+ 批量问题定级升级兜底。报警分析的目的：保证系统没有严重问题，及时发现影响面大的问题。"""
import re
from datetime import datetime, timedelta

ORDER_RE = re.compile(r"(?<!\d)(\d{10,20})(?!\d)")

# 同类归组家族：验价失败/验价成功率下跌/验价失败量上升等同关键词报警归为一类
FAMILIES = ("验价", "询价", "验座", "预订", "生单", "支付", "出票", "退款", "改签超时")

# 兜底归类：报警正文无家族关键词时（如 Sunfire 噪音卡片），按监控项/规则名二次归类
MONITOR_PATTERNS = (
    ("预定", "预订"),
    ("预订", "预订"),
    ("生单", "生单"),
    ("offer", "生单"),
    ("验价", "验价"),
    ("询价", "询价"),
    ("验座", "验座"),
    ("出票", "出票"),
    ("支付", "支付"),
    ("退款", "退款"),
)


def family_key(text, codes=None):
    """归组键：优先文本关键词家族；无关键词时按监控项名兜底归类；再退化为首个告警码；都没有返回 None。"""
    t = text or ""
    for kw in FAMILIES:
        if kw in t:
            return f"kw:{kw}"
    for pat, fam in MONITOR_PATTERNS:
        if pat in t:
            return f"kw:{fam}"
    if codes:
        return f"code:{codes[0]}"
    return None


def extract_orders(text):
    """从报警文本提取订单号（10-20 位纯数字），去重返回排序列表。"""
    return sorted(set(ORDER_RE.findall(text or "")))


def _parse_ts(s):
    try:
        ts = datetime.fromisoformat(s)
    except Exception:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts


def build_context(db, text, codes, run_id=None, window_min=30):
    """检索窗口内同类历史 run 并研判；同类≥2 条（含当前）返回关联上下文，否则 None。
    窗口取 30 分钟以支撑「持续 >10 分钟」判定；近 10 分钟子窗口用于风险分级。"""
    key = family_key(text, codes)
    if not key:
        return None
    now = datetime.now()
    cutoff = now - timedelta(minutes=window_min)
    recent_cutoff = now - timedelta(minutes=10)
    rows = db.q("SELECT run_id, source_text, started_at FROM runs "
                "WHERE started_at IS NOT NULL ORDER BY started_at DESC LIMIT 200")
    peers = []
    for r in rows:
        if run_id and r["run_id"] == run_id:
            continue
        ts = _parse_ts(r["started_at"])
        if not ts or ts < cutoff:
            continue
        if family_key(r["source_text"] or "") == key:
            peers.append({"run_id": r["run_id"], "source_text": r["source_text"], "ts": ts})
    if not peers:
        return None
    orders = set(extract_orders(text))
    recent_orders = set(extract_orders(text))
    for p in peers:
        po = set(extract_orders(p["source_text"]))
        orders |= po
        if p["ts"] >= recent_cutoff:
            recent_orders |= po
    orders = sorted(orders)
    same_order = len(orders) == 1
    batch = len(orders) >= 2
    ts_all = [p["ts"] for p in peers] + [now]
    span_min = (max(ts_all) - min(ts_all)).total_seconds() / 60
    # 持续性：近 10 分钟滚动窗口内出现 ≥2 个不同失败订单（含当前）视为持续有新订单失败
    sustained = len(recent_orders) >= 2
    m = len(orders)
    if len(recent_orders) >= 5 or (span_min > 10 and sustained):
        risk = "high"
    elif m >= 3:
        risk = "medium"
    else:
        risk = "low"
    return {"type_key": key, "window_min": window_min,
            "count": len(peers) + 1, "orders": orders,
            "same_order": same_order, "batch": batch,
            "span_min": round(span_min, 1),
            "recent_order_count": len(recent_orders),
            "sustained": sustained, "risk_level": risk,
            "peer_runs": [p["run_id"] for p in peers][:20]}


def render_context(corr):
    """生成注入引擎上下文的归类研判段落。"""
    if not corr:
        return ""
    kind = corr["type_key"].split(":", 1)[1]
    risk = corr.get("risk_level", "medium")
    if corr["same_order"]:
        impact = (f"仅涉及同一订单 {corr['orders'][0]} 的频繁重试，影响面=单一用户，"
                  f"定级不升级，结论须注明『单订单重试，影响单一用户』")
    elif corr["batch"]:
        base = (f"涉及 {len(corr['orders'])} 个不同订单均失败（窗口跨度 {corr.get('span_min', 0)} 分钟，"
                f"近10分钟新订单 {corr.get('recent_order_count', 0)} 个），风险等级={risk}。")
        if risk == "high":
            impact = (base + "高风险批量失败！必须追加取证：①用 sunfire-cli 查该类操作成功率/失败量趋势；"
                      "②排查近 30 分钟内相关应用发布/配置变更/开关动作；"
                      "conclusion/summary 必须明确提醒『批量失败可能代表系统出现问题，需人工介入排查』并列出订单号")
        elif risk == "medium":
            impact = (base + "批量问题信号，必须追加取证：①用 sunfire-cli 查该类操作成功率/失败量趋势确认是否整体下跌；"
                      "②排查近 30 分钟内相关应用是否有发布/配置变更/开关动作")
        else:
            impact = (base + "≤2 订单且无持续新订单报警，属偶发，影响面小，定级不升级（≤P3），"
                      "结论须注明『偶发批量失败，影响面小』，给出涉及订单号")
    else:
        impact = "未提取到订单号，按报警内容研判影响面"
    return (f"【同类报警关联】近 {corr['window_min']} 分钟内同类报警（{kind}）共 {corr['count']} 条，"
            f"涉及订单 {len(corr['orders'])} 个：{', '.join(corr['orders'][:10]) or '无'}。{impact}。"
            f"conclusion 必须带上归类视角（N 条同类/M 个订单/影响面）。")


def apply_batch_escalation(result, corr):
    """批量问题分级定级兜底：high→至少P1，medium→至少P2，low→不升级。
    引擎已给更高定级则保留。单订单重试不升级。返回是否发生升级。"""
    if not corr or not corr.get("batch") or result.get("normal"):
        return False
    risk = corr.get("risk_level", "medium")
    if risk == "low":
        result["correlation"] = corr
        return False
    target = "P1" if risk == "high" else "P2"
    ans = result.get("anomalies") or []
    sevs = [a.get("severity") for a in ans if isinstance(a, dict)]
    rank = {"P1": 3, "P2": 2, "P3": 1, "OK": 0}
    if any(rank.get(s, 0) >= rank[target] for s in sevs):
        result["correlation"] = corr
        return False
    n, m = corr["count"], len(corr["orders"])
    suffix = (f"（{n}条同类/{m}订单批量高风险，升级{target}，可能系统问题需人工介入）" if risk == "high"
              else f"（{n}条同类/{m}订单批量，升级{target}）")
    if ans and isinstance(ans[0], dict):
        ans[0]["severity"] = target
        ans[0]["summary"] = f"{ans[0].get('summary', '')}{suffix}"[:60]
    else:
        result.setdefault("anomalies", []).append(
            {"severity": target,
             "summary": f"多订单批量失败（{n}条同类/{m}订单），需排查发布/变更关联"})
    result["correlation"] = corr
    return True
