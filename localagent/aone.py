"""域内问题闭环：基于 tech_requirement 建议一键创建 Aone 技术需求。

草稿由报告 JSON 侧车生成（标题=归因摘要，描述=结论+关键证据+修复方向）；
创建走引擎（qodercli→codex 降级链）调用 aone-requirement-create skill /
coop MCP create_workitem；必须经人工在确认页确认后才执行，禁止自动创建。
"""
import json
import os

from . import engine
from .db import now

AONE_REQ_PROMPT = """你是 Aone 工作项创建执行器。请使用 aone-requirement-create skill（底层 coop MCP create_workitem）创建一条技术需求（stamp=Req）。
目标需求空间：{project}
标题：{title}
描述：
{desc}

要求：
1. 必须真实调用创建接口，禁止编造工作项 ID 或链接。
2. 创建成功后严格只输出 JSON：{{"ok": true, "workitem_id": str, "url": str}}
3. 创建失败时严格只输出 JSON：{{"ok": false, "error": str（一句话失败原因）}}"""


def _load_sidecar(cfg, db, run_id):
    run = db.one("SELECT report_path FROM runs WHERE run_id=?", run_id)
    if not run or not run["report_path"]:
        return None, None
    js = os.path.join(cfg.workspace, run["report_path"][:-3] + ".json")
    if not os.path.exists(js):
        return None, None
    try:
        with open(js, encoding="utf-8") as f:
            return json.load(f), js
    except Exception:
        return None, None


def draft(cfg, db, run_id, idx):
    """生成第 idx 条 tech_requirement 建议的需求草稿；不满足条件返回 {"error": ...}。"""
    data, _ = _load_sidecar(cfg, db, run_id)
    if data is None:
        return {"error": "报告 JSON 侧车不存在"}
    sug = data.get("suggestions") or []
    if not (0 <= idx < len(sug)) or not isinstance(sug[idx], dict):
        return {"error": "建议序号无效"}
    s = sug[idx]
    if s.get("action_type") != "tech_requirement":
        return {"error": "仅 tech_requirement 类建议可创建 Aone 需求"}
    if s.get("aone_req_url"):
        return {"error": f"已创建过需求：{s['aone_req_url']}"}
    conclusion = data.get("conclusion") or data.get("summary") or ""
    ev_lines = "\n".join(f"- 【{e.get('action', '')}】{(e.get('finding') or '')[:300]}"
                         for e in (data.get("evidence") or [])[:8])
    title = f"[LocalAgent]{conclusion[:50]}"
    desc = (f"## 问题结论\n{conclusion}\n\n"
            f"## 建议修复方向\n{s.get('app', '')}/{s.get('feature', '')}：{s.get('action', '')}\n\n"
            f"## 关键证据\n{ev_lines}\n\n"
            f"## 来源\nLocalAgent 分析报告 {run_id}（自动生成，开发者已确认）")
    project = cfg.agent.get("aone", {}).get("project", "") if isinstance(cfg.agent.get("aone"), dict) else ""
    return {"run_id": run_id, "idx": idx, "title": title, "desc": desc, "project": project}


async def execute(cfg, db, run_id, idx, title, desc, project):
    """人工确认后执行创建：引擎调用 skill 建需求，成功回写链接到报告侧车/报警记录/审计。"""
    data, js_path = _load_sidecar(cfg, db, run_id)
    if data is None:
        return {"error": "报告 JSON 侧车不存在"}
    sug = data.get("suggestions") or []
    if not (0 <= idx < len(sug)) or not isinstance(sug[idx], dict):
        return {"error": "建议序号无效"}
    if sug[idx].get("aone_req_url"):
        return {"error": f"已创建过需求：{sug[idx]['aone_req_url']}"}
    db.audit("aone_req", "create_requested", run_id,
             json.dumps({"idx": idx, "title": title, "project": project}, ensure_ascii=False)[:400])
    if cfg.mock:
        out = {"ok": True, "workitem_id": "mock-90001",
               "url": "https://project.aone.alibaba-inc.com/req/mock-90001"}
    else:
        prompt = AONE_REQ_PROMPT.format(project=project or "用户默认退改需求空间（由 skill 判定）",
                                        title=title, desc=desc)
        try:
            out, eng, model = await engine._run_with_downgrade(cfg, db, prompt, run_id)
        except engine.EngineUnavailable as e:
            db.audit("aone_req", "engine_unavailable", run_id, str(e)[:200])
            return {"error": f"引擎资源不可用，稍后重试：{str(e)[:120]}"}
        except Exception as e:
            db.audit("aone_req", "create_failed", run_id, str(e)[:300])
            return {"error": f"创建失败：{str(e)[:200]}"}
    if not (isinstance(out, dict) and out.get("ok")):
        err = (out or {}).get("error", "引擎未返回有效创建结果") if isinstance(out, dict) else "引擎未返回有效创建结果"
        db.audit("aone_req", "create_failed", run_id, str(err)[:300])
        return {"error": f"创建失败：{str(err)[:200]}"}
    url = out.get("url") or f"https://project.aone.alibaba-inc.com/req/{out.get('workitem_id')}"
    sug[idx]["aone_req_url"] = url
    sug[idx]["aone_req_at"] = now()
    try:
        data["suggestions"] = sug
        with open(js_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    try:
        al = db.one("SELECT alert_id, detail FROM alerts WHERE run_id=?", run_id)
        if al:
            db.update("alerts", "alert_id", al["alert_id"],
                      detail=((al["detail"] or "") + f"\nAone需求: {url}")[:500])
    except Exception:
        pass
    db.audit("aone_req", "created", run_id, json.dumps(out, ensure_ascii=False)[:300])
    return {"ok": True, "url": url, "workitem_id": out.get("workitem_id")}
