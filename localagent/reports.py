import os
from datetime import datetime

from .db import new_id, now

TPL = """# {title}

- 执行时间：{ts}
- 触发来源：{source}
- 使用引擎：{engine}
- 判定：{verdict}

## 结论

{conclusion}

## 排查过程与证据

{evidence}

## 异常明细

{anomalies}

## 建议动作

{suggestions}

## 审计摘要

{audit}
"""


def _fmt_suggestion(s):
    if isinstance(s, dict):
        t = s.get("action_type") or ""
        tag = {"notify_external": "通知外部域", "tech_requirement": "技术需求修复"}.get(t, t)
        act = s.get("action") or ""
        base = f"- [{tag}] {s.get('app')}/{s.get('feature')}"
        return f"{base}：{act}" if act else base
    return f"- {s}"


def write_report(cfg, db, run_id, result, source, engine, kb_refs=None, audit_lines=None):
    day = datetime.now().strftime("%Y-%m-%d")
    rel = os.path.join("reports", day)
    os.makedirs(os.path.join(cfg.workspace, rel), exist_ok=True)
    verdict = "无问题" if result.get("normal") else "有问题"
    conclusion = result.get("conclusion") or result.get("summary", "")
    corr = result.get("correlation")
    if corr:
        kind = str(corr.get("type_key", "")).split(":", 1)[-1]
        impact = ("单订单重试，影响单一用户" if corr.get("same_order")
                  else f"多订单批量信号（{len(corr.get('orders', []))} 订单）")
        conclusion = (f"{conclusion}\n\n> 同类报警关联：近 {corr.get('window_min', 10)} 分钟 {kind} 类共 "
                      f"{corr.get('count', 0)} 条，涉及订单 {len(corr.get('orders', []))} 个，影响面：{impact}")
    ev = result.get("evidence") or []
    evidence = "\n".join(f"- 【{e.get('action')}】{e.get('finding')}" for e in ev) or "- 无取证记录"
    if result.get("evidence_warning"):
        evidence = result["evidence_warning"] + "\n" + evidence
    anomalies = "\n".join(f"- [{a.get('severity')}] {a.get('summary')}" for a in result.get("anomalies", [])) or "- 无"
    suggestions = "\n".join(_fmt_suggestion(s) for s in result.get("suggestions", []) if s) or "- 无"
    path = os.path.join(rel, f"{run_id}.md")
    with open(os.path.join(cfg.workspace, path), "w", encoding="utf-8") as f:
        f.write(TPL.format(title=f"LocalAgent 分析报告 {run_id}", ts=now(), source=source,
                           engine=engine, verdict=verdict, conclusion=conclusion,
                           evidence=evidence, anomalies=anomalies, suggestions=suggestions,
                           audit="\n".join(audit_lines or []) or "- 见审计日志"))
    try:
        import json as _json
        with open(os.path.join(cfg.workspace, path[:-3] + ".json"), "w", encoding="utf-8") as f:
            _json.dump({"run_id": run_id, "title": f"LocalAgent 分析报告 {run_id}",
                        "ts": now(), "source": source, "engine": engine, "verdict": verdict,
                        "normal": bool(result.get("normal")), "conclusion": conclusion,
                        "summary": result.get("summary", ""),
                        "evidence": ev, "anomalies": result.get("anomalies", []),
                        "suggestions": [s for s in result.get("suggestions", []) if s],
                        "correlation": result.get("correlation"),
                        "evidence_warning": result.get("evidence_warning", ""),
                        "audit": audit_lines or []}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    db.insert("reports_meta", report_id=new_id("rep"), run_id=run_id,
              title=(result.get("conclusion") or result.get("summary", ""))[:60],
              file_path=path, created_at=now(), feedback_state="可回灌")
    db.update("runs", "run_id", run_id, report_path=path)
    db.set_state("last_report", path)
    return path
