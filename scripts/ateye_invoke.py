#!/usr/bin/env python3
"""Ateye 订正执行器：通过 qodercli + ateye-online MCP 执行线上订正。

仅由授权清单写条目在人工二次确认后调用（authlist.execute_write）。
机器范围强约束：只允许 node-group 与 unit 同时匹配的存活机器。

用法：
  python3 scripts/ateye_invoke.py --app trp --invoker 修改改签单subStatus \
      --node-group trphost_spe --unit center \
      --modify-id 12345678 --sub-status 40
  --dry-run 只打印将执行的 prompt，不调用 qodercli。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

PROMPT_TPL = """你是线上订正执行器，严格按以下步骤操作，禁止执行任何步骤之外的操作：
1. 调用 ateye-online MCP get_machines_with_detail(app={app})，获取机器列表；
   只允许选择 nodeGroup 为 {node_group} 且 unit 为 {unit} 的存活机器；
   若没有同时满足两个条件的存活机器，立即只输出一行 JSON：
   {{"ok": false, "ip": null, "beanName": null, "detail": "无匹配机器(nodeGroup={node_group} 且 unit={unit})"}} 并结束。
2. 对选中机器调用 get_invoker_list(ip=选中IP, app={app}, signature="subStatus")，
   找到名称为【{invoker}】的 Invoker，记录其 beanName 与完整 signature；找不到则输出失败 JSON 结束。
3. 调用 invoke_method 执行该 Invoker：ip=选中IP, app={app}, beanName=上一步结果,
   signature=上一步结果, paramCount 与 params 按 signature 定义传入：改签单号={modify_id}，目标 subStatus={sub_status}。
4. 执行完成后只输出一行 JSON（不要输出其他内容）：
   {{"ok": true或false, "ip": "执行机器IP", "beanName": "所用beanName", "detail": "执行结果摘要(含返回值要点)"}}"""


def build_prompt(a):
    return PROMPT_TPL.format(app=a.app, invoker=a.invoker, node_group=a.node_group,
                             unit=a.unit, modify_id=a.modify_id, sub_status=a.sub_status)


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
        if isinstance(obj, dict) and "ok" in obj:
            best = obj
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--invoker", required=True)
    ap.add_argument("--node-group", required=True)
    ap.add_argument("--unit", required=True)
    ap.add_argument("--modify-id", required=True)
    ap.add_argument("--sub-status", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not re.match(r"^\d{6,20}$", a.modify_id):
        print(json.dumps({"ok": False, "ip": None, "beanName": None,
                          "detail": f"modify-id 非法: {a.modify_id}"}, ensure_ascii=False))
        return 1
    if not re.match(r"^[0-9A-Za-z_]{1,32}$", a.sub_status):
        print(json.dumps({"ok": False, "ip": None, "beanName": None,
                          "detail": f"sub-status 非法: {a.sub_status}"}, ensure_ascii=False))
        return 1

    prompt = build_prompt(a)
    if a.dry_run:
        print(prompt)
        return 0

    qoder = shutil.which("qodercli")
    if not qoder:
        print(json.dumps({"ok": False, "ip": None, "beanName": None,
                          "detail": "qodercli 不在 PATH"}, ensure_ascii=False))
        return 1
    try:
        r = subprocess.run([qoder, "-p", prompt, "--permission-mode", "auto"],
                           capture_output=True, timeout=560)
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "ip": None, "beanName": None,
                          "detail": "qodercli 执行超时(560s)"}, ensure_ascii=False))
        return 1
    out = (r.stdout or b"").decode(errors="replace")
    err = (r.stderr or b"").decode(errors="replace")
    result = extract_json(out) or extract_json(err)
    if result is None:
        print(json.dumps({"ok": False, "ip": None, "beanName": None,
                          "detail": f"未解析到结果JSON rc={r.returncode} {err[:200] or out[:200]}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
