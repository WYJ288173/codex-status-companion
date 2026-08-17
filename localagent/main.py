import asyncio
import json
import os
import subprocess
import sys
import threading

import uvicorn

from .config import Config
from .db import DB, now
from .dingtalk import build as build_ding
from .menubar import MenuBar
from .notify import Notifier
from .pet import PetUI
from .pipeline import Pipeline
from . import webapp


class App:
    pass

APP = App()


def reload_config():
    """管理页面改配置后热加载：刷新 APP 及各组件的 cfg 引用。"""
    cfg = Config()
    APP.cfg = cfg
    APP.pipeline.cfg = cfg
    APP.notifier.cfg = cfg
    APP.ding.cfg = cfg
    if hasattr(APP.ding, "groups"):
        APP.ding.groups = {g.get("name"): g for g in cfg.groups}
    return cfg


def bootstrap():
    cfg = Config()
    os.makedirs(os.path.join(cfg.workspace, "reports"), exist_ok=True)
    os.makedirs(os.path.join(cfg.workspace, "data"), exist_ok=True)
    db = DB(os.path.join(cfg.workspace, "data", "localagent.sqlite"))
    pet = PetUI(cfg, db)
    notifier = Notifier(cfg, db, ui=pet)
    APP.cfg, APP.db, APP.notifier, APP.pet = cfg, db, notifier, pet
    ding = build_ding(cfg, db)
    pipeline = Pipeline(cfg, db, notifier, ding)
    if hasattr(ding, "on_message"):
        ding.on_message = pipeline.process
    APP.ding, APP.pipeline = ding, pipeline
    return APP


async def run():
    app = bootstrap()
    app.db.set_state("agent_status", "idle")
    app.db.set_state("agent_started_at", now())
    stale = app.db.q("SELECT run_id FROM runs WHERE status='running'")
    for r in stale:
        app.db.update("runs", "run_id", r["run_id"], status="failed",
                      finished_at=now(), error_msg="进程重启，执行中断（PRD 12.8）")
    if stale:
        app.db.audit("task", "stale_runs_marked_failed", "", f"{len(stale)} runs")
    from . import storage
    analysis_days = app.cfg.agent.get("storage", {}).get("analysis_days", 3)
    cleaned = storage.cleanup_analysis(app.db, analysis_days)
    if cleaned:
        app.db.audit("storage", "startup_cleanup_analysis", "", f"deleted {cleaned} rows")
    from . import engine as engine_mod
    versions = await engine_mod.detect_versions(app.cfg)
    app.db.set_state("engine_versions", json.dumps(versions, ensure_ascii=False))
    app.db.audit("engine", "versions_detected", "", json.dumps(versions, ensure_ascii=False))
    app.db.audit("task", "agent_started", "", f"mock={app.cfg.mock}")

    web = webapp.build_app(app)
    srv = threading.Thread(
        target=uvicorn.run, args=(web,),
        kwargs={"host": app.cfg.web.get("host", "127.0.0.1"),
                "port": app.cfg.web.get("port", 8765), "log_level": "warning"},
        daemon=True)
    srv.start()
    app.pet.start_gui_process()
    MenuBar(app.cfg, app.db).start()
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(86400)
            storage.cleanup_analysis(app.db, analysis_days)

    async def _probe_engines():
        try:
            await engine_mod.probe_engines(app.cfg, app.db)
        except Exception as e:
            app.db.audit("engine", "probe_failed", "", str(e)[:200])

    asyncio.create_task(app.notifier.reopen_loop())
    asyncio.create_task(_periodic_cleanup())
    if not app.cfg.mock and app.cfg.engines.get("probe_on_start", True):
        asyncio.create_task(_probe_engines())
    await app.ding.start()


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        APP.db.audit("task", "agent_stopped")
        APP.db.set_state("agent_status", "stopped")


if __name__ == "__main__":
    main()
