#!/usr/bin/env python3
"""从语雀文档解析解决方案配置：经 qodercli（语雀 skill/MCP）读取文档正文，
提取标准方案 JSON（code/name/diagnose/steps/actions）。

安全约束：本脚本只产出方案配置；同步后门禁始终重置为关闭，
执行仍需人工在管理页面开启门禁并逐步确认。

用法：
  python3 scripts/yuque_solution_sync.py --url <语雀文档URL> [--dry-run]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

PROMPT_TPL = """读取语雀文档 {url}（用你的语雀 skill/MCP 读取正文），从中提取告警处理方案。
文档通常按《解决方案模板》编写：基本信息（告警码 code/方案名称 name）、诊断特征（diagnose）、
处理步骤（steps）、执行动作表格（顺序/类型/动作名/执行内容/所需参数，顺序即执行顺序）。
严格按以下 JSON 结构只输出一行 JSON（不要输出任何其他内容）：
{{"code": "告警码(大写字母/数字/下划线)", "name": "方案名称",
"diagnose": ["诊断特征1", "诊断特征2"],
"steps": ["处理步骤1", "处理步骤2"],
"actions": [{{"type": "dingtalk_reply|ateye_write|aone_req|aone_bug|manual",
"name": "步骤名",
"write_entry_id": "仅 ateye_write 填写：关联授权清单写条目 id",
"feature": "仅 ateye_write 填写：授权条目的 feature",
"params": ["仅 ateye_write 填写：引擎需提供的参数键名"],
"fixed_params": {{"仅 ateye_write 可选：配置强指定的固定参数": "值，如 subStatus: 61"}},
"template": "仅 dingtalk_reply 可选：回复要点",
"title_hint": "仅 aone_req/aone_bug 可选：标题要点",
"instruction": "仅 manual 可选：人工处理指引"}}]}}
要求：actions 的顺序即执行顺序，须与文档描述一致；文档标注为固定值/强指定的参数放入 fixed_params；
文档未明确的字段不要臆造；文档内容不是告警处理方案时，只输出 {{"error": "原因"}}。"""

VALID_TYPES = ("dingtalk_reply", "ateye_write", "aone_req", "aone_bug", "manual")


def extract_json(text):
    best = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            best = obj
    return best


def validate_solution(obj):
    """校验方案 JSON 结构；返回 (ok, reason)。"""
    if not isinstance(obj, dict):
        return False, "输出不是 JSON 对象"
    if obj.get("error"):
        return False, f"文档解析失败：{obj['error']}"
    code = obj.get("code") or ""
    if not re.match(r"^[A-Z][A-Z0-9_]{3,}$", code):
        return False, f"code 非法（须为大写字母开头的大写字母/数字/下划线）：{code}"
    if not (obj.get("name") or "").strip():
        return False, "name 不能为空"
    actions = obj.get("actions")
    if not isinstance(actions, list) or not actions:
        return False, "actions 必须是非空列表"
    for i, a in enumerate(actions, 1):
        if not isinstance(a, dict) or a.get("type") not in VALID_TYPES:
            return False, f"第{i}个动作 type 非法：{a.get('type') if isinstance(a, dict) else a}"
        if a.get("type") == "ateye_write" and not a.get("write_entry_id"):
            return False, f"第{i}个动作 ateye_write 缺少 write_entry_id"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    prompt = PROMPT_TPL.format(url=a.url)
    if a.dry_run:
        print(prompt)
        return 0

    qoder = shutil.which("qodercli")
    if not qoder:
        print(json.dumps({"error": "qodercli 不在 PATH"}, ensure_ascii=False))
        return 1
    try:
        r = subprocess.run([qoder, "-p", prompt, "--permission-mode", "auto"],
                           capture_output=True, timeout=280)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "qodercli 执行超时(280s)"}, ensure_ascii=False))
        return 1
    out = (r.stdout or b"").decode(errors="replace")
    obj = extract_json(out) or extract_json((r.stderr or b"").decode(errors="replace"))
    if obj is None:
        print(json.dumps({"error": f"未解析到 JSON rc={r.returncode} {out[:200]}"},
                         ensure_ascii=False))
        return 1
    ok, reason = validate_solution(obj)
    if not ok:
        print(json.dumps({"error": reason}, ensure_ascii=False))
        return 1
    obj["enabled"] = False  # 同步后门禁强制关闭
    print(json.dumps(obj, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
