"""同类报警归类分析：近 10 分钟窗口同类报警归组 + 订单比对（单订单重试 vs 多订单批量）
+ 批量问题定级升级兜底。报警分析的目的：保证系统没有严重问题，及时发现影响面大的问题。"""
import re
from datetime import datetime, timedelta

ORDER_RE = re.compile(r"(?<!\d)(\d{10,20})(?!\d)")

# 同类归组家族：验价失败/验价成功率下跌/验价失败量上升等同关键词报警归为一类
FAMILIES = ("验价", "询价", "验座", "预订", "生单", "支付", "出票", "退款", "改签超时")


def family_key(text, codes=None):
    """归组键：优先文本关键词家族；无关键词时退化为首个告警码；都没有返回 None。"""
    t = text or ""
    for kw in FAMILIES:
        if kw in t:
            return f"kw:{kw}"
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


def build_context(db, text, codes, run_id=None, window_min=10):
    """检索窗口内同类历史 run 并研判；同类≥2 条（含当前）返回关联上下文，否则 None。"""
    key = family_key(text, codes)
    if not key:
        return None
    cutoff = datetime.now() - timedelta(minutes=window_min)
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
            peers.append(r)
    if not peers:
        return None
    orders = set(extract_orders(text))
    for p in peers:
        orders |= set(extract_orders(p["source_text"]))
    orders = sorted(orders)
    same_order = len(orders) == 1
    batch = len(orders) >= 2
    return {"type_key": key, "window_min": window_min,
            "count": len(peers) + 1, "orders": orders,
            "same_order": same_order, "batch": batch,
            "peer_runs": [p["run_id"] for p in peers][:20]}


def render_context(corr):
    """生成注入引擎上下文的归类研判段落。"""
    if not corr:
        return ""
    kind = corr["type_key"].split(":", 1)[1]
    if corr["same_order"]:
        impact = (f"仅涉及同一订单 {corr['orders'][0]} 的频繁重试，影响面=单一用户，"
                  f"定级不升级，结论须注明『单订单重试，影响单一用户』")
    elif corr["batch"]:
        impact = (f"涉及 {len(corr['orders'])} 个不同订单均失败——批量问题信号！必须追加取证："
                  f"①用 sunfire-cli 查该类操作成功率/失败量趋势确认是否整体下跌；"
                  f"②排查近 30 分钟内相关应用是否有发布/配置变更/开关动作；"
                  f"判定是否发布或其他动作导致的批量问题")
    else:
        impact = "未提取到订单号，按报警内容研判影响面"
    return (f"【同类报警关联】近 {corr['window_min']} 分钟内同类报警（{kind}）共 {corr['count']} 条，"
            f"涉及订单 {len(corr['orders'])} 个：{', '.join(corr['orders'][:10]) or '无'}。{impact}。"
            f"conclusion 必须带上归类视角（N 条同类/M 个订单/影响面）。")


def apply_batch_escalation(result, corr):
    """批量问题定级兜底：多订单批量失败时 anomalies 至少 P2（引擎已给 P1 则保留）。
    单订单重试不升级。返回是否发生升级。"""
    if not corr or not corr.get("batch") or result.get("normal"):
        return False
    ans = result.get("anomalies") or []
    sevs = [a.get("severity") for a in ans if isinstance(a, dict)]
    if "P1" in sevs or "P2" in sevs:
        return False
    n, m = corr["count"], len(corr["orders"])
    if ans and isinstance(ans[0], dict):
        ans[0]["severity"] = "P2"
        ans[0]["summary"] = f"{ans[0].get('summary', '')}（{n}条同类/{m}订单批量，升级P2）"[:60]
    else:
        result.setdefault("anomalies", []).append(
            {"severity": "P2", "summary": f"多订单批量失败（{n}条同类/{m}订单），需排查发布/变更关联"})
    result["correlation"] = corr
    return True
