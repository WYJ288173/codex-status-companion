import ast
import operator
import os
import re
import shlex
import subprocess
from datetime import datetime

import yaml

from .db import now


def _resolve(name, ctx):
    cur = ctx
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
        ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _eval(node, ctx):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _resolve(node.id, ctx)
    if isinstance(node, ast.Attribute):
        parts = []
        n = node
        while isinstance(n, ast.Attribute):
            parts.append(n.attr)
            n = n.value
        if isinstance(n, ast.Name):
            parts.append(n.id)
        return _resolve(".".join(reversed(parts)), ctx)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, right in zip(node.ops, node.comparators):
            r = _eval(right, ctx)
            if left is None or r is None:
                return False
            if not _CMP[type(op)](left, r):
                return False
            left = r
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, ctx) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    raise ValueError(f"unsupported condition expr: {ast.dump(node)}")


def eval_condition(expr, ctx):
    try:
        return bool(_eval(ast.parse(expr, mode="eval").body, ctx))
    except Exception:
        return False


def find_entry(entries, app, scope, feature):
    for e in entries:
        if (e.get("app") == app and e.get("scope") == scope
                and e.get("feature") == feature and e.get("enabled", False)):
            return e
    return None


def find_read_entry(entries, group, at_me):
    for e in entries:
        if e.get("app") != "dingtalk" or e.get("scope") != "read" or not e.get("enabled", False):
            continue
        cons = e.get("constraints", {})
        if group not in cons.get("groups", []):
            continue
        if cons.get("mode") == "at_me_only" and not at_me:
            continue
        if cons.get("mode") != "at_me_only" and at_me and "at_me" not in e.get("feature", ""):
            continue
        return e
    return None


def check_write(db, entry, ctx):
    """返回 (ok, reason)。校验触发条件/参数白名单/有效期/次数/环境。"""
    cons = entry.get("constraints", {})
    for cond in cons.get("conditions", []):
        if not eval_condition(cond, ctx):
            return False, f"condition not met: {cond}"
    for param, pattern in cons.get("paramsWhitelist", {}).items():
        val = str(ctx.get("params", {}).get(param, ""))
        if not re.match(pattern + "$", val):
            return False, f"param {param} failed whitelist"
    expiry = entry.get("expiry")
    if expiry and datetime.now().astimezone().isoformat() > expiry:
        return False, "entry expired"
    max_exec = entry.get("maxExecutions")
    if max_exec is not None and db.count_exec(entry["id"]) >= max_exec:
        return False, "max executions reached"
    return True, ""


def write_requires_confirm(entry):
    return entry.get("env") == "online" or entry.get("confirmBeforeRun", False)


def render_command(entry, params):
    """按 constraints.command 模板填充参数，返回 argv。占位符未全部填充时抛 ValueError。
    模板由用户配置，AI 只能填充参数值，不能修改命令本身。"""
    tpl = entry.get("constraints", {}).get("command", "")
    if not tpl:
        raise ValueError("entry 未配置 command 模板")
    missing = [k for k in re.findall(r"\{(\w+)\}", tpl) if k not in params]
    if missing:
        raise ValueError(f"参数缺失: {missing}")
    filled = tpl.format(**{k: str(v) for k, v in params.items()})
    return shlex.split(filled)


def execute_write(entry, params, cwd=None, timeout=None):
    """真实执行写命令。返回 (ok, detail)。写操作不自动重试。
    timeout 缺省取条目 constraints.timeout（默认 120s）。"""
    try:
        argv = render_command(entry, params)
    except ValueError as e:
        return False, f"命令渲染失败: {e}"
    if timeout is None:
        try:
            timeout = int(entry.get("constraints", {}).get("timeout", 120))
        except (TypeError, ValueError):
            timeout = 120
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd)
    except Exception as e:
        return False, f"执行异常: {e}"
    out = (r.stdout or b"").decode()[:300]
    err = (r.stderr or b"").decode()[:300]
    if r.returncode == 0:
        return True, f"rc=0 {out}"
    return False, f"rc={r.returncode} {err or out}"


def check_plan_order(db, run_id, plan_id, step_no):
    """严格执行顺序校验：同 plan_id 的前序步骤必须都已执行完成。
    返回 (ok, reason)。无计划信息直接放行（兼容旧记录）。"""
    import json as _json
    if not plan_id or not step_no:
        return True, ""
    done = ("executed", "replied")
    for row in db.q("SELECT payload, exec_result FROM auth_exec WHERE run_id=?", run_id):
        try:
            pp = _json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if pp.get("plan_id") != plan_id:
            continue
        pstep = pp.get("step_no") or 0
        if pstep < step_no and row["exec_result"] not in done:
            return False, f"须先完成第 {pstep} 步（当前状态 {row['exec_result']}），严格按方案顺序执行"
    return True, ""


def disable_entry(workspace, entry_id):
    """disableOnFailure：在 auth_list.yaml 中停用指定条目。"""
    ap = os.path.join(workspace, "config", "auth_list.yaml")
    with open(ap, encoding="utf-8") as f:
        auth = yaml.safe_load(f) or {"entries": []}
    changed = False
    for e in auth.get("entries", []):
        if e.get("id") == entry_id:
            e["enabled"] = False
            changed = True
    if changed:
        with open(ap, "w", encoding="utf-8") as f:
            yaml.safe_dump(auth, f, allow_unicode=True, sort_keys=False)
    return changed
