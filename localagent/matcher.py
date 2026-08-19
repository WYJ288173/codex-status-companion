import re
import time


class Cooldowns:
    def __init__(self, seconds=300):
        self.seconds = seconds
        self._hits = {}

    def hit(self, key):
        t = time.time()
        last = self._hits.get(key, 0)
        if t - last < self.seconds:
            return True
        self._hits[key] = t
        return False


def _rule_pass(rule, msg):
    t = rule.get("type")
    if t == "keyword":
        return any(k in msg["text"] for k in rule.get("keywords", []))
    if t == "sender":
        return msg["sender"] in rule.get("senders", [])
    if t == "format":
        return re.search(rule.get("pattern", ""), msg["text"]) is not None
    if t == "compound_or":
        return any(_rule_pass(r, msg) for r in rule.get("rules", []))
    return False


def match_alert(entry, msg):
    """rules 之间 AND，单条 rule 内 OR。全部通过返回命中描述，否则 None。"""
    rules = entry.get("alertRules", [])
    if not rules:
        return None
    passed = []
    for i, rule in enumerate(rules):
        if not _rule_pass(rule, msg):
            return None
        passed.append(f"rule{i}:{rule.get('type')}")
    return "+".join(passed)


def parse_sunfire_alert(text):
    """从 Sunfire 群报警文本中提取结构化字段；非报警文本返回 None。"""
    if "报警" not in text and "告警" not in text:
        return None
    low = text.lower()
    severity = "P1" if "critical" in low else ("P2" if "warning" in low else "P3")
    app = None
    cands = re.findall(r"^[a-z][a-z0-9-]{2,}$", text, re.M)
    # 优先带连字符的标准应用名（如 change-flight-tp），避免把 publish 等发送方名当应用
    hy = [c for c in cands if "-" in c]
    if hy:
        app = hy[0]
    elif cands:
        app = cands[0]
    if not app:
        m_app = re.search(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b", text)
        app = m_app.group(1) if m_app else None
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})#Err#([0-9a-f]{16,})", text)
    metric_lines = [l.strip() for l in text.splitlines()
                    if ("当前值" in l or "成功率" in l or "触发" in l) and l.strip()]
    links = re.findall(r"https?://[^\s\)]+", text)
    alert_stats = None
    m_sup = re.search(r"已发送(\d+)条[，,]\s*被抑制(\d+)条", text)
    if m_sup:
        alert_stats = {"sent": int(m_sup.group(1)), "suppressed": int(m_sup.group(2))}
    # 规则名：触发行的前两行（监控项组 + 指标名），排除应用名行
    rule_name = None
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if re.search(r"共有\d+条数据触发", l):
            prev = [p.strip() for p in lines[max(0, i - 2):i] if p.strip()]
            prev = [p for p in prev if p != app]
            if prev:
                rule_name = " · ".join(prev[-2:])
            break
    return {"app": app, "severity": severity,
            "sample_ip": m.group(1) if m else None,
            "trace_id": m.group(2) if m else None,
            "metrics": metric_lines[:5], "links": links[:3],
            "alert_stats": alert_stats, "rule_name": rule_name}


_ITEM_HEAD = re.compile(
    r"\*\*(\d+)\.\s*(?:<font[^>]*>【([^】]*)】</font>\s*)?"
    r"【BCP】【[^】]*】\s*\[([^\]]*?)\(([A-Z][A-Z0-9_]+)\)\]\((https?://[^)]+)\)\*\*")


def parse_audit_broadcast(text):
    """解析 BCP 审计定时播报 Markdown；非播报文本返回 None。
    返回 {kind, title, stats, items[{index, level, name, code, bcp_url,
    alert_time, status, checkfree_id, owner}]}。"""
    if not text:
        return None
    m_title = re.search(r"^###\s*(.*告警定时播报.*)$", text, re.M)
    if not m_title and not ("【BCP】" in text and "**告警时间:**" in text):
        return None
    stats = {}
    m = re.search(r"今日未完结:(\d+)个，其中\*\*待接手(\d+);待反馈(\d+);逾期(\d+)", text)
    if m:
        stats.update({"today_unfinished": int(m.group(1)), "today_wait_take": int(m.group(2)),
                      "today_wait_feedback": int(m.group(3)), "today_overdue": int(m.group(4))})
    m = re.search(r"近\d+日未完结:(\d+)个", text)
    if m:
        stats["recent_unfinished"] = int(m.group(1))
    heads = list(_ITEM_HEAD.finditer(text))
    items = []
    for i, h in enumerate(heads):
        seg = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        mt = re.search(r"\*\*告警时间:\*\*\s*([0-9][0-9: -]+\d)", seg)
        ms = re.search(r"\*\*告警状态:\*\*\s*([^\s\[]+)", seg)
        mc = re.search(r"id%3D(\d+)", seg)
        mo = re.search(r"\*\*规则owner:\*\*\s*@(\S+)", seg)
        items.append({"index": int(h.group(1)), "level": h.group(2),
                      "name": h.group(3).strip(), "code": h.group(4),
                      "bcp_url": h.group(5),
                      "alert_time": mt.group(1).strip() if mt else None,
                      "status": ms.group(1).strip() if ms else None,
                      "checkfree_id": mc.group(1) if mc else None,
                      "owner": mo.group(1).strip() if mo else None})
    if not items:
        return None
    return {"kind": "audit_broadcast",
            "title": m_title.group(1).strip() if m_title else "审计告警播报",
            "stats": stats, "items": items}
