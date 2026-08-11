"""真实降级验证：把默认模型的命令替换为「输出额度耗尽文案」的桩，
其余模型走真实 qodercli，验证会自动降级到 dogfooding 模型并真实产出分析。
运行：./.venv/bin/python tests/verify_real_downgrade.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from localagent import engine
from localagent.db import DB

QUOTA_TXT = ("You've reached your credit usage limit. Please upgrade your subscription plan "
             "to get more resources.")


class Cfg:
    """默认模型 -> echo 额度耗尽（桩）；M1/M2 -> 真实 qodercli 指定模型。"""
    mock = False
    engines = {"default": "qodercli", "fallback": [], "list": [
        {"name": "qodercli", "cmd": ["qodercli", "-p", "{prompt}", "--permission-mode", "auto"],
         "model_fallback": ["Peach-07-17-DogFooding"]}]}

    def engine_cmd(self, name):
        return next((e["cmd"] for e in self.engines["list"] if e["name"] == name), None)


_real_run = engine._run_engine


async def run_with_quota_stub(cfg, name, prompt, db=None, run_id=None, model=None, timeout=900):
    if model is None:  # 默认模型：模拟额度耗尽
        tag = f"{name}/default"
        if db:
            db.audit("engine", "raw_output", tag, QUOTA_TXT, run_id)
        raise engine.EngineUnavailable(f"{tag} 资源不可用（额度/限流/鉴权）：{QUOTA_TXT[:80]}")
    return await _real_run(cfg, name, prompt, db, run_id, model=model, timeout=timeout)


async def main():
    engine._run_engine = run_with_quota_stub
    ws = os.path.join(PROJ, "tmp", "downgrade_ws")
    os.makedirs(ws, exist_ok=True)
    db = DB(os.path.join(ws, "t.sqlite"))
    ok = True
    print("默认模型模拟额度耗尽，验证自动降级到 dogfooding 模型并真实调用 qodercli…")
    result, eng, ver = await engine.analyze(
        Cfg(), db, {"text": "【报警】change-flight-tp 改签底座 验价失败量 warning，判断是否需要人工介入",
                    "extra": {"group": "改签底座质量监控"}}, "run-downgrade")
    print(f"命中引擎/模型: {eng}/{ver}")
    print(f"判定: {'无问题' if result.get('normal') else '有问题'} | 结论: {result.get('conclusion')}")
    for e in (result.get("evidence") or [])[:3]:
        print("  证据:", (e.get("action") or "")[:70], "→", (e.get("finding") or "")[:90])

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    chk("降级到 dogfooding 模型执行", ver == "Peach-07-17-DogFooding")
    chk("真实产出结论", bool(result.get("conclusion")))
    chk("写 model_downgraded 审计",
        len(db.q("SELECT 1 FROM audit_logs WHERE action='model_downgraded'")) >= 1)
    chk("写 engine_resource_unavailable 审计",
        len(db.q("SELECT 1 FROM audit_logs WHERE action='engine_resource_unavailable'")) >= 1)
    print("\n真实降级验证:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
