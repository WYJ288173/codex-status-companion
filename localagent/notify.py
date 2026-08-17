import asyncio
import subprocess
from datetime import datetime, timedelta

from .db import new_id, now


def _parse(ts):
    return datetime.fromisoformat(ts)


class Notifier:
    """提醒分级：toast（正常）/ 持久弹框（异常，关闭≠确认，30 分钟重弹）/ 合并列表。"""

    def __init__(self, cfg, db, ui=None):
        self.cfg = cfg
        self.db = db
        self.ui = ui  # 实现 toast(text) / modal(alerts:list)
        self.reopen_min = cfg.notify.get("reopen_minutes", 30)

    def _sys_notify(self, text):
        try:
            subprocess.run(["osascript", "-e", f'display notification "{text}" with title "LocalAgent"'],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    def toast(self, text):
        self.db.audit("ui", "toast", "", text)
        if self.ui:
            self.ui.toast(text)
        else:
            self._sys_notify(text)

    def raise_alerts(self, run_id, group, anomalies):
        pending = []
        for a in anomalies:
            alert_id = new_id("al")
            reopen = (datetime.now().astimezone() + timedelta(minutes=self.reopen_min)).isoformat(timespec="seconds")
            self.db.insert("alerts", alert_id=alert_id, run_id=run_id, source_group=group,
                           severity=a.get("severity", "P3"), summary=a.get("summary", ""),
                           detail=a.get("detail", ""), status="pending",
                           created_at=now(), acked_at=None, ignore_until=None, reopen_at=reopen)
            pending.append({"alert_id": alert_id, "severity": a.get("severity"),
                            "summary": a.get("summary"), "source": group, "run_id": run_id})
        self.db.audit("ui", "modal_raised", group, f"{len(pending)} alerts", run_id)
        self._push_modal()
        return pending

    def _push_modal(self):
        pending = self.pending()
        if not pending:
            return
        if self.ui:
            self.ui.modal(pending)
        else:
            self._sys_notify(f"发现 {len(pending)} 个待确认异常，请打开管理页面处理")

    def pending(self):
        from .correlate import alert_time_of
        rows = self.db.q("SELECT a.*, r.report_path AS report_path, r.source_text AS run_text "
                         "FROM alerts a "
                         "LEFT JOIN runs r ON a.run_id=r.run_id "
                         "WHERE a.status='pending' AND "
                         "(r.trigger_type IS NULL OR r.trigger_type != 'simulate') "
                         "ORDER BY a.severity, a.created_at")
        # 与报警中心一致：预警时间超过 2 小时的老告警不再提醒（dws 延迟回填产物）
        cutoff = (datetime.now().astimezone() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for r in rows:
            at = alert_time_of(r["run_text"] or "")
            if at and at + ":00" < cutoff:
                continue
            out.append(dict(r))
        return out

    def ack(self, alert_id):
        self.db.update("alerts", "alert_id", alert_id, status="acked", acked_at=now())
        self.db.audit("ui", "alert_acked", alert_id)
        self._push_modal()

    def ignore(self, alert_id, hours=None):
        hours = hours or self.cfg.notify.get("ignore_cooldown_hours", 4)
        until = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.db.update("alerts", "alert_id", alert_id, status="ignored", ignore_until=until)
        self.db.audit("ui", "alert_ignored", alert_id, f"cooldown {hours}h")
        self._push_modal()

    async def reopen_loop(self):
        while True:
            await asyncio.sleep(20)
            n = datetime.now().astimezone().isoformat(timespec="seconds")
            rows = self.db.q("SELECT alert_id FROM alerts WHERE status='pending' AND reopen_at <= ?", n)
            if rows:
                for r in rows:
                    nxt = (datetime.now().astimezone() + timedelta(minutes=self.reopen_min)).isoformat(timespec="seconds")
                    self.db.update("alerts", "alert_id", r["alert_id"], reopen_at=nxt)
                    self.db.audit("ui", "modal_reopen", r["alert_id"])
                self._push_modal()
