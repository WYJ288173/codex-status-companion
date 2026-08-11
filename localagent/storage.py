import os
import shutil
import zipfile
from datetime import datetime, timedelta

from .db import now
from .dingtalk import CST

DIRS = ["reports", "evidence", "archive", "logs", "data", "config", "assets"]


def _scan_dir(root):
    total = 0
    for base, _, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def scan(workspace):
    out = {}
    for d in DIRS:
        p = os.path.join(workspace, d)
        out[d] = _scan_dir(p) if os.path.isdir(p) else 0
    out["total"] = sum(out.values())
    return out


def _cutoff(days):
    return (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")


def _day_dirs(base, before_day, after_day=None):
    """返回 base 下日期目录名列表（YYYY-MM-DD），before_day 之前（可选 after_day 之后）。"""
    if not os.path.isdir(base):
        return []
    out = []
    for name in os.listdir(base):
        if len(name) != 10 or not os.path.isdir(os.path.join(base, name)):
            continue
        if name < before_day and (after_day is None or name >= after_day):
            out.append(name)
    return sorted(out)


def cleanup_expired(db, workspace, cfg):
    """按保留策略删除超期数据（报告/证据/审计/消息），返回删除计数。"""
    st = cfg.agent.get("storage", {})
    rep_days = int(st.get("report_days", 90))
    ev_days = int(st.get("evidence_days", 30))
    audit_days = int(st.get("audit_days", 180))
    deleted = {"report_dirs": 0, "evidence_dirs": 0, "audit_rows": 0, "message_rows": 0}

    for day in _day_dirs(os.path.join(workspace, "reports"), _cutoff(rep_days)):
        shutil.rmtree(os.path.join(workspace, "reports", day), ignore_errors=True)
        deleted["report_dirs"] += 1
    db.conn.execute("DELETE FROM reports_meta WHERE substr(created_at,1,10) < ?",
                    (_cutoff(rep_days),))
    for day in _day_dirs(os.path.join(workspace, "evidence"), _cutoff(ev_days)):
        shutil.rmtree(os.path.join(workspace, "evidence", day), ignore_errors=True)
        deleted["evidence_dirs"] += 1
    db.conn.execute("DELETE FROM evidence WHERE substr(created_at,1,10) < ?",
                    (_cutoff(ev_days),))
    r = db.conn.execute("DELETE FROM audit_logs WHERE substr(ts,1,10) < ?",
                        (_cutoff(audit_days),))
    deleted["audit_rows"] = r.rowcount
    r = db.conn.execute("DELETE FROM messages WHERE substr(received_at,1,10) < ?",
                        (_cutoff(audit_days),))
    deleted["message_rows"] = r.rowcount
    db.conn.commit()
    db.audit("storage", "cleanup_expired", "", str(deleted))
    return deleted


def cleanup_analysis(db, days=30):
    """删除超过指定天数的分析记录（runs/alerts/auth_exec/evidence/reports_meta/messages）。"""
    cutoff = _cutoff(days)
    deleted = 0
    for table, col in [("auth_exec", "ts"), ("alerts", "created_at"),
                       ("evidence", "created_at"), ("reports_meta", "created_at"),
                       ("messages", "received_at"), ("runs", "started_at")]:
        r = db.conn.execute(f"DELETE FROM {table} WHERE substr({col},1,10) < ?", (cutoff,))
        deleted += r.rowcount
    db.conn.commit()
    db.audit("storage", "cleanup_analysis", "", f"deleted {deleted} rows, cutoff={cutoff}")
    return deleted


def archive_old(db, workspace, months=6, purge_months=12):
    """6 个月~1 年的报告/证据压缩归档到 archive/；返回归档月份数。"""
    before = _cutoff(months * 30)
    after = _cutoff(purge_months * 30)
    archived = []
    arc = os.path.join(workspace, "archive")
    os.makedirs(arc, exist_ok=True)
    for kind in ("reports", "evidence"):
        for day in _day_dirs(os.path.join(workspace, kind), before, after):
            month = day[:7]
            zpath = os.path.join(arc, f"{kind}-{month}.zip")
            src = os.path.join(workspace, kind, day)
            with zipfile.ZipFile(zpath, "a", zipfile.ZIP_DEFLATED) as z:
                for base, _, files in os.walk(src):
                    for f in files:
                        fp = os.path.join(base, f)
                        z.write(fp, os.path.relpath(fp, src))
            shutil.rmtree(src, ignore_errors=True)
            archived.append(f"{kind}/{day}")
    db.conn.commit()
    db.audit("storage", "archive_old", "", f"{len(archived)} dirs")
    return archived


def purge_year(db, workspace, purge_months=12):
    """清空超过 1 年的数据：DB 行 + 归档 zip + 残留日期目录。"""
    before = _cutoff(purge_months * 30)
    purged = {"db_rows": 0, "archive_files": 0, "dirs": 0}
    for table in ("runs", "reports_meta", "evidence", "audit_logs", "messages", "alerts", "auth_exec"):
        col = {"runs": "started_at", "reports_meta": "created_at", "evidence": "created_at",
               "audit_logs": "ts", "messages": "received_at", "alerts": "created_at",
               "auth_exec": "ts"}[table]
        r = db.conn.execute(f"DELETE FROM {table} WHERE substr({col},1,10) < ?", (before,))
        purged["db_rows"] += r.rowcount
    db.conn.commit()
    arc = os.path.join(workspace, "archive")
    if os.path.isdir(arc):
        for f in os.listdir(arc):
            m = f.split("-")
            if len(m) >= 2 and f"{m[-2]}-{m[-1].split('.')[0]}" < before[:7]:
                os.remove(os.path.join(arc, f))
                purged["archive_files"] += 1
    for kind in ("reports", "evidence"):
        for day in _day_dirs(os.path.join(workspace, kind), before):
            shutil.rmtree(os.path.join(workspace, kind, day), ignore_errors=True)
            purged["dirs"] += 1
    db.audit("storage", "purge_year", "", str(purged))
    return purged


def enforce_quota(db, workspace, cfg):
    """总容量超 quota_gb 时按 证据→报告 从最旧清到配额内，返回清理条目数。"""
    quota = int(cfg.agent.get("storage", {}).get("quota_gb", 5)) * 1024 ** 3
    cleaned = 0
    while scan(workspace)["total"] > quota:
        oldest = None
        for kind in ("evidence", "reports"):
            days = _day_dirs(os.path.join(workspace, kind), "9999-99-99")
            if days:
                oldest = (kind, days[0])
                break
        if not oldest:
            break
        kind, day = oldest
        shutil.rmtree(os.path.join(workspace, kind, day), ignore_errors=True)
        db.conn.execute(f"DELETE FROM {kind if kind == 'evidence' else 'reports_meta'} "
                        f"WHERE substr(created_at,1,10) = ?", (day,))
        db.conn.commit()
        cleaned += 1
    if cleaned:
        db.audit("storage", "enforce_quota", "", f"{cleaned} dirs")
    return cleaned
