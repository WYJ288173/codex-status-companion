"""解决方案沉淀库：按告警码沉淀诊断特征、处理步骤与执行动作，YAML 唯一源热加载。

方案结构（actions 支持多种执行动作类型）：
- code/name/enabled（enabled=写类动作门禁，默认关）
- diagnose[]：诊断特征
- steps[]：处理步骤
- actions[]：执行动作列表，每项 {type, ...}：
    dingtalk_reply  仅钉群回复（走既有回复流程）
    ateye_write     Ateye 数据订正（write_entry_id 关联授权清单写条目；可多项）
    aone_req        创建 Aone 需求走产品需求（write 类，受门禁）
    aone_bug        创建线上缺陷走技术修复（write 类，受门禁）
    manual          人工处理指引（不执行）
兼容旧字段：write_entry_id/write_hint 自动归一为单个 ateye_write 动作。
"""
import os

import yaml

from .db import now

REL = os.path.join("config", "solutions.yaml")

ACTION_LABELS = {
    "dingtalk_reply": "钉群回复",
    "ateye_write": "Ateye数据订正",
    "aone_req": "创建Aone需求",
    "aone_bug": "创建线上缺陷",
    "manual": "人工处理",
}
WRITE_ACTION_TYPES = ("ateye_write", "aone_req", "aone_bug")


def load_solutions(workspace):
    p = os.path.join(workspace, REL)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("solutions", []) or []
    except Exception:
        return []


def save_solutions(workspace, solutions):
    p = os.path.join(workspace, REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump({"solutions": solutions}, f, allow_unicode=True, sort_keys=False)


def get_solution(solutions, code):
    return next((s for s in solutions if s.get("code") == code), None)


def find_solutions_for_codes(solutions, codes):
    hit = []
    for c in codes or []:
        s = get_solution(solutions, c)
        if s and s not in hit:
            hit.append(s)
    return hit


def set_gate(workspace, code, enabled):
    sols = load_solutions(workspace)
    s = get_solution(sols, code)
    if s is None:
        return False
    s["enabled"] = bool(enabled)
    s["updated_at"] = now()
    save_solutions(workspace, sols)
    return True


def normalize_actions(s):
    """返回方案的动作列表；旧字段 write_entry_id/write_hint 归一为单个 ateye_write。"""
    actions = [a for a in (s.get("actions") or []) if isinstance(a, dict) and a.get("type")]
    if not actions and s.get("write_entry_id"):
        hint = s.get("write_hint") or {}
        actions = [{"type": "ateye_write",
                    "write_entry_id": s["write_entry_id"],
                    "app": hint.get("app"), "feature": hint.get("feature"),
                    "params": hint.get("params") or []}]
    return actions


def write_actions(s):
    return [a for a in normalize_actions(s) if a.get("type") in WRITE_ACTION_TYPES]


def build_execution_plan(s, suggestions):
    """严格按方案 actions 配置顺序构建执行计划（Agent 不得选择/调整动作与顺序）。
    suggestions 仅作为 ateye_write 步骤的参数源（按 feature 匹配）。
    返回步骤列表 [{step_no, type, name, entry_id, params, missing, template}]；
    某步缺参数时计划在该步截断（后续步骤不生成）。"""
    plan = []
    for i, a in enumerate(normalize_actions(s), 1):
        t = a.get("type")
        if t not in WRITE_ACTION_TYPES and t != "dingtalk_reply":
            continue  # manual 等仅展示类动作不进入执行计划
        step = {"step_no": i, "type": t,
                "name": a.get("name") or ACTION_LABELS.get(t, t), "missing": []}
        if t == "ateye_write":
            step["entry_id"] = a.get("write_entry_id")
            step["feature"] = a.get("feature")
            sug = next((x for x in (suggestions or [])
                        if isinstance(x, dict) and x.get("feature") == a.get("feature")), None)
            step["params"] = dict((sug or {}).get("params") or {})
            fixed = a.get("fixed_params") or {}
            if isinstance(fixed, dict):
                for k, v in fixed.items():
                    step["params"][k] = v  # 固定参数配置强指定，覆盖引擎值
                step["fixed_params"] = dict(fixed)
            need = [k for k in (a.get("params") or []) if k not in fixed]
            step["missing"] = [k for k in need if k not in step["params"]]
        elif t == "dingtalk_reply":
            step["template"] = a.get("template") or ""
        plan.append(step)
        if step["missing"]:
            break
    return plan


def find_gate_for_entry(solutions, entry_id):
    """门禁检查：返回引用了该授权条目的方案（ateye_write 动作），未引用返回 None。"""
    for s in solutions or []:
        for a in normalize_actions(s):
            if a.get("type") == "ateye_write" and a.get("write_entry_id") == entry_id:
                return s
    return None


def _render_action(a, gate_on):
    t = a.get("type")
    label = ACTION_LABELS.get(t, t)
    if t == "ateye_write":
        hint = f"app={a.get('app')}, feature={a.get('feature')}" if a.get("feature") else ""
        params = ", ".join(a.get("params") or []) or "无"
        fixed = a.get("fixed_params") or {}
        fixed_txt = ("；固定参数（配置强指定，不得更改）："
                     + ", ".join(f"{k}={v}" for k, v in fixed.items())) if fixed else ""
        gate = ("门禁已开启：系统按方案配置的动作与顺序执行，suggestions 仅用于提供该动作所需参数"
                "（feature 须一致，不得新增方案未声明的写操作）"
                if gate_on else "门禁未开启 → 只分析不要求执行写操作")
        return (f"- 动作[{label}] 关联授权条目 {a.get('write_entry_id')}；{hint}；"
                f"params 必含键：{params}{fixed_txt}；{gate}")
    if t == "dingtalk_reply":
        tpl = a.get("template") or ""
        return f"- 动作[{label}]{(' 回复要点：' + tpl) if tpl else ''}（走既有回复流程，受群回复开关控制）"
    if t in ("aone_req", "aone_bug"):
        title = a.get("title_hint") or ""
        gate = ("门禁已开启，可给出创建建议" if gate_on
                else "门禁未开启 → 只在结论中说明需要创建，不要求执行")
        return f"- 动作[{label}]{(' 标题要点：' + title) if title else ''}；{gate}"
    return f"- 动作[{label}] {a.get('instruction') or ''}"


def render_solution_context(matched):
    """注入引擎上下文的方案指令文本。"""
    if not matched:
        return ""
    lines = ["【沉淀解决方案（按告警码命中，请严格按方案取证分析）】",
             "执行约束：执行动作与顺序由方案配置固定，Agent 不得增删、跳过或调整动作，只提供参数。"]
    for s in matched:
        lines.append(f"方案[{s.get('code')}] {s.get('name', '')}")
        diag = s.get("diagnose") or []
        if diag:
            lines.append("诊断特征：")
            lines.extend(f"- {d}" for d in diag)
        steps = s.get("steps") or []
        if steps:
            lines.append("处理步骤：")
            lines.extend(f"{i}. {st}" for i, st in enumerate(steps, 1))
        actions = normalize_actions(s)
        if actions:
            lines.append("执行动作：")
            lines.extend(_render_action(a, bool(s.get("enabled"))) for a in actions)
        lines.append("")
    return "\n".join(lines)
