import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localagent.main import bootstrap


async def main():
    app = bootstrap()
    app.ding.on_message = app.pipeline.process
    hits = await app.ding.poll_once("2026-08-04T09:00:00+08:00")
    print(f"poll hits={hits}")
    for r in app.db.q("SELECT run_id, trigger_type, source, status, engine, started_at FROM runs "
                      "WHERE engine NOT LIKE 'mock%' ORDER BY started_at DESC LIMIT 6"):
        print(dict(r))


asyncio.run(main())
