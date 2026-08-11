"""GUI 子进程入口：macOS AppKit 要求窗口/菜单栏在进程主线程创建，
守护进程主线程被 asyncio 占用，故 pet / menubar 各跑一个独立进程。

用法：python -m localagent.gui pet|menubar
"""
import os
import sys


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pet"
    from .config import Config
    from .db import DB
    cfg = Config()
    db = DB(os.path.join(cfg.workspace, "data", "localagent.sqlite"))
    if mode == "menubar":
        from .menubar import MenuBar
        MenuBar(cfg, db).run_foreground()
    else:
        from .pet import PetUI
        PetUI(cfg, db).start_foreground()


if __name__ == "__main__":
    main()
