"""后台展示层渲染：钉群文本降噪 + 分析报告结构化渲染（结论先见、证据可折叠）。"""
import json
import os
import re

MAX_URL_TEXT = 42


def esc(x):
    if x is None:
        return ""
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_ding_text(text, max_len=None):
    """去除钉群 Markdown 噪音：font 标签、裸链接堆砌、粗体/标题/分隔线标记。"""
    if not text:
        return ""
    t = re.sub(r"</?font[^>]*>", "", text)
    t = re.sub(r"\[([^\]]*)\]\(\s*(dingtalk://[^)]*)\)", r"\1", t)  # 钉钉跳转链接只留文案
    t = re.sub(r"\[([^\]]*)\]\(\s*([^)]*)\)",
               lambda m: m.group(1) or _short_url(m.group(2)), t)   # 普通链接只留文案
    t = re.sub(r"(?<![(\[])https?://\S+", lambda m: _short_url(m.group(0)), t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*-{3,}\s*$", "", t, flags=re.M)
    t = t.replace("**", "")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if max_len and len(t) > max_len:
        t = t[:max_len].rstrip() + "…"
    return t


def _short_url(url):
    if len(url) <= MAX_URL_TEXT:
        return url
    m = re.match(r"https?://([^/]+)", url)
    return f"{m.group(1)}/…" if m else url[:MAX_URL_TEXT] + "…"


# ---------------- 报告解析 ----------------

_SECTIONS = {"结论": "conclusion_text", "排查过程与证据": "evidence_text",
             "异常明细": "anomalies_text", "建议动作": "suggestions_text",
             "审计摘要": "audit_text"}


def parse_report_md(text):
    """解析旧版报告 Markdown（无 JSON 侧车时兼容用）。"""
    data = {"title": "", "ts": "", "source": "", "engine": "", "verdict": "",
            "conclusion": "", "evidence": [], "anomalies": [], "suggestions": [],
            "audit": [], "evidence_warning": ""}
    cur = None
    buf = {}
    for line in (text or "").splitlines():
        if line.startswith("# "):
            data["title"] = line[2:].strip()
            continue
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = _SECTIONS.get(m.group(1))
            continue
        if cur is None:
            for key, field in (("执行时间", "ts"), ("触发来源", "source"),
                               ("使用引擎", "engine"), ("判定", "verdict")):
                if line.startswith(f"- {key}："):
                    data[field] = line.split("：", 1)[1].strip()
            continue
        buf.setdefault(cur, []).append(line)

    def lines(field):
        return [x.strip() for x in buf.get(field, []) if x.strip()]

    data["conclusion"] = " ".join(lines("conclusion_text"))
    for ln in lines("evidence_text"):
        m = re.match(r"^-\s*【(.*?)】(.*)$", ln)
        if m:
            data["evidence"].append({"action": m.group(1), "finding": m.group(2).strip()})
        elif ln.startswith("⚠️"):
            data["evidence_warning"] = ln
        elif ln not in ("- 无取证记录",):
            data["evidence"].append({"action": "", "finding": ln.lstrip("- ")})
    for ln in lines("anomalies_text"):
        m = re.match(r"^-\s*\[(.*?)\]\s*(.*)$", ln)
        if m:
            data["anomalies"].append({"severity": m.group(1), "summary": m.group(2).strip()})
    for ln in lines("suggestions_text"):
        if ln not in ("- 无",):
            data["suggestions"].append(ln.lstrip("- "))
    data["audit"] = [ln.lstrip("- ") for ln in lines("audit_text")]
    data["normal"] = data["verdict"] == "无问题"
    return data


def load_report(workspace, rel_path):
    """优先读 JSON 侧车（新报告），否则回退解析 Markdown（旧报告兼容）。"""
    md_abs = os.path.join(workspace, rel_path)
    js_abs = md_abs[:-3] + ".json" if md_abs.endswith(".md") else md_abs + ".json"
    if os.path.exists(js_abs):
        try:
            with open(js_abs, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("evidence", [])
            d.setdefault("anomalies", [])
            d.setdefault("suggestions", [])
            d.setdefault("audit", [])
            d["_src"] = "json"
            return d
        except Exception:
            pass
    if not os.path.exists(md_abs):
        return None
    with open(md_abs, encoding="utf-8") as f:
        d = parse_report_md(f.read())
    d["_src"] = "md"
    return d


# ---------------- 报告渲染 ----------------

SEV_COLOR = {"P1": "#f87171", "P2": "#f59e0b", "P3": "#38bdf8", "OK": "#7ee7b0"}


def _fmt_suggestion(s):
    if isinstance(s, dict):
        t = s.get("action_type") or ""
        tag, color = {"notify_external": ("通知外部域", "#38bdf8"),
                      "tech_requirement": ("技术需求修复", "#7ee7b0")}.get(t, (t, "#9fb3c0"))
        act = esc(s.get("action") or "")
        head = (f"<span style='color:{color}'>[{tag}]</span> "
                f"{esc(s.get('app'))}/{esc(s.get('feature'))}")
        return f"{head}：{act}" if act else head
    return esc(s)


def render_report_html(data, rel_path=""):
    """渲染报告为后台内页（深色主题）：结论置顶、异常表格化、证据时间线可折叠。"""
    if data is None:
        return "<div class='card'><h2>报告不存在</h2></div>"
    normal = bool(data.get("normal"))
    verdict = data.get("verdict") or ("无问题" if normal else "有问题")
    sevs = [a.get("severity") for a in data.get("anomalies") or [] if a.get("severity")]
    top_sev = next((s for s in ("P1", "P2", "P3", "OK") if s in sevs), "OK" if normal else "-")
    vcolor = "#7ee7b0" if normal else "#f87171"
    ev = data.get("evidence") or []
    warn = data.get("evidence_warning") or ""
    raw_link = (f"<a style='color:#9fb3c0;font-size:12px' target='_blank' "
                f"href='/reports/by-path?p={esc(rel_path)}'>查看原始 Markdown</a>" if rel_path else "")
    html = [
        "<div class='card' style='border-left:4px solid %s'>" % vcolor,
        f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
        f"<span style='background:{vcolor};color:#06281a;border-radius:6px;padding:2px 10px;"
        f"font-weight:600'>{esc(verdict)}</span>"
        f"<span style='background:{SEV_COLOR.get(top_sev, '#374151')};color:#111;border-radius:6px;"
        f"padding:2px 8px;font-size:12px'>最高级别 {esc(top_sev)}</span>"
        f"<span style='color:#9fb3c0;font-size:12px'>{esc(data.get('source'))} · "
        f"{esc(data.get('engine'))} · {esc(data.get('ts'))}</span>{raw_link}</div>",
        f"<div style='margin-top:10px;font-size:15px;line-height:1.6'>"
        f"{esc(data.get('conclusion') or data.get('summary') or '无结论')}</div>",
        (f"<div class='warn' style='margin-top:8px;font-size:12px'>{esc(warn)}</div>" if warn else ""),
        "</div>",
    ]
    an = data.get("anomalies") or []
    rows = "".join(
        f"<tr><td><span style='color:{SEV_COLOR.get(a.get('severity'), '#e6edf3')}'>"
        f"{esc(a.get('severity'))}</span></td><td>{esc(a.get('summary'))}</td></tr>" for a in an)
    html.append("<div class='card'><h2>异常明细</h2><table>"
                "<tr><th style='width:70px'>级别</th><th>问题</th></tr>"
                + (rows or "<tr><td colspan=2>无异常</td></tr>") + "</table></div>")
    tl = []
    for i, e in enumerate(ev, 1):
        action = esc(e.get("action") or f"取证步骤 {i}")
        finding = esc(e.get("finding") or "")
        tl.append(
            f"<div style='border-left:2px solid #22303a;padding:0 0 10px 14px;position:relative'>"
            f"<span style='position:absolute;left:-5px;top:4px;width:8px;height:8px;border-radius:50%;"
            f"background:#38bdf8'></span>"
            f"<details><summary style='cursor:pointer;color:#e6edf3;font-size:13px'>"
            f"<b style='color:#7ee7b0'>{i}.</b> {action}</summary>"
            f"<pre style='margin:6px 0 0'>{finding}</pre></details></div>")
    html.append("<div class='card'><h2>排查证据（%d 步，点击展开详情）</h2>%s</div>"
                % (len(ev), "".join(tl) or "<p style='color:#9fb3c0'>无取证记录</p>"))
    sug = data.get("suggestions") or []
    html.append("<div class='card'><h2>建议动作</h2>"
                + ("".join(f"<div style='font-size:13px;margin:4px 0'>· {_fmt_suggestion(s)}</div>"
                           for s in sug) or "<p style='color:#9fb3c0'>无</p>") + "</div>")
    audit = data.get("audit") or []
    html.append("<div class='card'><h2>审计摘要</h2><details><summary "
                "style='cursor:pointer;color:#9fb3c0;font-size:12px'>展开</summary>"
                f"<pre>{esc(chr(10).join(str(a) for a in audit)) or '见审计日志'}</pre>"
                "</details></div>")
    return "".join(html)
