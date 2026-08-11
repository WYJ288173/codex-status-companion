import os
import threading
import webbrowser


class MenuBar:
    """macOS 菜单栏（rumps）：状态/控制台/最近报告/暂停任务/写操作紧急开关/退出。
    无 rumps 时降级为 no-op。"""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.port = cfg.web.get("port", 8765)

    def start(self):
        """rumps 需要独占进程主线程，跑在独立 GUI 子进程里；已存在则不重复启动。
        用 PID 文件做守卫：pgrep -f 在守护进程上下文会误判“已运行”，导致从不拉起。"""
        import os
        import subprocess
        import sys
        pidfile = os.path.join(self.cfg.workspace, "data", "menubar.pid")
        if self._pid_alive(pidfile):
            self.db.audit("ui", "menubar_already_running")
            return
        # 用 sys.prefix 推导 venv 解释器：sys.executable 在 macOS 可能被解析成裸基础
        # Python（丢失 venv site-packages），导致子进程 import rumps 失败而退出。
        py = os.path.join(sys.prefix, "bin", "python")
        if not os.path.exists(py):
            py = sys.executable
        try:
            logp = os.path.join(self.cfg.workspace, "logs", "gui_menubar.log")
            lf = open(logp, "a")
            p = subprocess.Popen([py, "-m", "localagent.gui", "menubar"],
                                 start_new_session=True, stdout=lf, stderr=lf)
            with open(pidfile, "w") as f:
                f.write(str(p.pid))
            self.db.audit("ui", "menubar_process_spawned", "", str(p.pid))
        except Exception as e:
            self.db.audit("ui", "menubar_spawn_failed", "", str(e)[:120])

    @staticmethod
    def _pid_alive(pidfile):
        import os
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            return False
        except PermissionError:
            return True  # 进程存在但非当前用户所有，视为存活

    def run_foreground(self):
        try:
            import rumps  # noqa
        except ImportError:
            self.db.audit("ui", "menubar_skipped", "", "rumps not installed")
            return
        self._run()

    def _run(self):
        import rumps
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

        db, cfg, port = self.db, self.cfg, self.port

        class App(rumps.App):
            def __init__(self):
                icon = os.path.join(cfg.workspace, "assets", "menubar_icon.png")
                if os.path.exists(icon):
                    super().__init__("LocalAgent", icon=icon, template=True, quit_button=None)
                else:
                    super().__init__("LocalAgent", quit_button=None)
                self.status_item = rumps.MenuItem("状态：加载中")
                self.pause_item = rumps.MenuItem("暂停所有任务", callback=self.toggle_pause)
                self.writes_item = rumps.MenuItem("紧急开关：禁用所有写操作", callback=self.toggle_writes)
                self.menu = [self.status_item, None,
                             rumps.MenuItem("打开控制台", callback=self.open_console),
                             rumps.MenuItem("打开最近报告", callback=self.open_report),
                             rumps.MenuItem("权限设置", callback=self.open_authlist),
                             rumps.MenuItem("显示桌面小窗", callback=self.show_pet),
                             self.pause_item, self.writes_item, None,
                             rumps.MenuItem("退出", callback=rumps.quit_application)]
                self.timer = rumps.Timer(self.refresh, 5)
                self.timer.start()
                self.refresh(None)

            def refresh(self, _):
                from .config import Config
                pending = db.q("SELECT COUNT(*) c FROM alerts a LEFT JOIN runs r ON a.run_id=r.run_id "
                               "WHERE a.status='pending' AND "
                               "(r.trigger_type IS NULL OR r.trigger_type != 'simulate')")[0]["c"]
                conn = db.get_state("dingtalk_conn", "unknown")
                paused = db.get_state("tasks_paused") == "1"
                wd = bool(Config().agent.get("writes_disabled", False))
                last = db.one("SELECT source, status, started_at FROM runs WHERE "
                              "(trigger_type IS NULL OR trigger_type != 'simulate') "
                              "ORDER BY started_at DESC LIMIT 1")
                last_s = f"{last['source']} {last['status']} {last['started_at'][:16]}" if last else "无"
                self.title = f"LA{' ⚠' + str(pending) if pending else ' ✓'}"
                self.status_item.title = (f"待确认 {pending} | 钉钉 {conn} | 最近任务 {last_s}"
                                          + (" | 已暂停" if paused else "")
                                          + (" | 写已禁用" if wd else ""))
                self.pause_item.title = "恢复所有任务" if paused else "暂停所有任务"
                self.writes_item.title = "恢复写操作" if wd else "紧急开关：禁用所有写操作"

            def open_console(self, _):
                webbrowser.open(f"http://127.0.0.1:{port}/")

            def open_report(self, _):
                p = db.get_state("last_report")
                if p:
                    webbrowser.open(f"http://127.0.0.1:{port}/reports/by-path?p={p}")
                else:
                    self.open_console(None)

            def open_authlist(self, _):
                webbrowser.open(f"http://127.0.0.1:{port}/authlist")

            def show_pet(self, _):
                db.set_state("pet_hidden", "0")
                from .pet import PetUI
                PetUI(cfg, db).start_gui_process()

            def toggle_pause(self, _):
                cur = db.get_state("tasks_paused") == "1"
                db.set_state("tasks_paused", "0" if cur else "1")
                db.audit("task", "tasks_pause_toggled", "", str(not cur), None)
                self.refresh(None)

            def toggle_writes(self, _):
                import yaml
                import os
                p = os.path.join(cfg.workspace, "config", "agent.yaml")
                with open(p, encoding="utf-8") as f:
                    agent = yaml.safe_load(f)
                v = not bool(agent.get("writes_disabled", False))
                agent["writes_disabled"] = v
                with open(p, "w", encoding="utf-8") as f:
                    yaml.safe_dump(agent, f, allow_unicode=True, sort_keys=False)
                db.audit("auth", "writes_switch_toggled", "", f"writes_disabled={v} (menubar)", None)
                self.refresh(None)

        App().run()
