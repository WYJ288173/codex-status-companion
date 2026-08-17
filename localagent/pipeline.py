import json

from . import authlist, correlate, engine, reports
from . import solutions as solmod
from .db import new_id, now
from .matcher import match_alert, parse_sunfire_alert, parse_audit_broadcast, Cooldowns


class Pipeline:
    def __init__(self, cfg, db, notifier, ding):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.ding = ding
        self.cooldowns = Cooldowns(int(cfg.notify.get("cooldown_seconds", 300)))

    def _effective_process_mode(self, group_name):
        """群级 process_mode 覆盖 > agent.yaml dingtalk.process_mode（默认 at_me_only）。"""
        for g in self.cfg.groups:
            if g.get("name") == group_name and g.get("process_mode"):
                return g["process_mode"]
        return self.cfg.dingtalk.get("process_mode", "at_me_only")

    async def process(self, msg):
        """msg: {msg_id, group, sender, text, at_me}
        路由：先按群 read-all 条目做报警匹配（覆盖 @主班 的 Sunfire 报警），
        未命中且 at_me 时走 @我 专项分析；否则丢弃。"""
        db = self.db
        if db.get_state("tasks_paused") == "1":
            db.audit("task", "msg_skipped_paused", msg["group"], "", None)
            return {"handled": False, "reason": "tasks_paused"}
        # 全局静默开关（默认开）：非@我消息不处理、不入库、不展示；
        # 后台开启 listen_all 后监听处理所有告警消息（优先于群级 process_mode）。
        if not msg.get("at_me") and not bool(self.cfg.dingtalk.get("listen_all", False)):
            return {"handled": False, "reason": "non_at_me_silent"}
        entries = self.cfg.auth_entries
        parsed = parse_sunfire_alert(msg["text"])
        audit_parsed = parse_audit_broadcast(msg["text"])
        audit_json = json.dumps(audit_parsed, ensure_ascii=False) if audit_parsed else None

        read_all = authlist.find_read_entry(entries, msg["group"], False)
        alert_hit = match_alert(read_all, msg) if read_all else None
        if alert_hit:
            entry, rule_hit = read_all, alert_hit
            trigger, source = "dingtalk_alert", "monitor_alert"
            if audit_parsed:
                source = "audit_broadcast"
                # 审计播报仅当规则 owner/@人是本人才分析，他人 owner 的播报不采集
                owner_name = self.cfg.dingtalk.get("owner_name", "华扬")
                if owner_name not in msg["text"]:
                    db.insert("messages", ignore=True, msg_id=msg["msg_id"],
                              group_name=msg["group"], sender=msg["sender"],
                              received_at=now(), msg_time=msg.get("msg_time") or None,
                              matched_entry_id=entry["id"],
                              matched_rule="broadcast_not_my_owner", run_id=None,
                              source_text=msg["text"])
                    db.audit("dingtalk", "broadcast_skipped_other_owner", msg["group"],
                             "", None)
                    return {"handled": False, "reason": "broadcast_owner_not_me"}
            # 新鲜度守卫：dws 会延迟回填数小时~数天前的老消息，
            # 预警时间超过 2 小时的老消息只记录不分析，避免老报警反复占据页面
            _at = correlate.alert_time_of(msg["text"])
            if _at:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                _cutoff = (_dt.now(_tz(_td(hours=8))) - _td(hours=2)).strftime("%Y-%m-%d %H:%M")
                if _at[:16] < _cutoff:
                    db.insert("messages", ignore=True, msg_id=msg["msg_id"],
                              group_name=msg["group"], sender=msg["sender"],
                              received_at=now(), msg_time=msg.get("msg_time") or None,
                              matched_entry_id=entry["id"],
                              matched_rule="stale_backfill", run_id=None,
                              source_text=msg["text"])
                    db.audit("dingtalk", "msg_skipped_stale", msg["group"],
                             f"预警时间 {_at[:16]} 早于2小时窗口", None)
                    return {"handled": False, "reason": "stale_backfill"}
            cd_key = f"{msg['group']}:{(parsed or {}).get('app') or ''}"
            if self.cooldowns.hit(cd_key):
                db.insert("messages", ignore=True, msg_id=msg["msg_id"],
                          group_name=msg["group"], sender=msg["sender"],
                          received_at=now(), msg_time=msg.get("msg_time") or None,
                          matched_entry_id=entry["id"],
                          matched_rule="cooldown", run_id=None, source_text=msg["text"])
                return {"handled": False, "reason": "cooldown"}
        elif msg["at_me"]:
            entry = authlist.find_read_entry(entries, msg["group"], True)
            if entry is None:
                # 未授权群：不落地、不记录内容，仅聚合计数
                db.audit("dingtalk", "msg_dropped_unauthorized", msg["group"])
                return {"handled": False, "reason": "no_read_entry"}
            if not any(k in msg["text"] for k in
                       ("分析", "报警", "审计", "订单", "traceId", "异常", "订正", "退票", "改签", "审查", "工单")):
                self.ding.reply(msg["group"],
                                "无法识别分析请求。支持用法：@我 + 报警内容 / 订单号 / traceId / 问题描述。")
                self.db.audit("dingtalk", "at_me_unrecognized", msg["group"], msg["text"][:80])
                db.insert("messages", ignore=True, msg_id=msg["msg_id"], group_name=msg["group"],
                          sender=msg["sender"], received_at=now(),
                          msg_time=msg.get("msg_time") or None,
                          matched_entry_id=entry["id"], matched_rule="unrecognized", run_id=None,
                          source_text=msg["text"])
                return {"handled": False, "reason": "at_me_unrecognized"}
            rule_hit = "at_me_direct"
            trigger, source = "dingtalk_at_me", "audit_group_at_me"
            if self.cooldowns.hit(f"{msg['group']}:atme"):
                db.insert("messages", ignore=True, msg_id=msg["msg_id"],
                          group_name=msg["group"], sender=msg["sender"],
                          received_at=now(), msg_time=msg.get("msg_time") or None,
                          matched_entry_id=entry["id"],
                          matched_rule="cooldown", run_id=None, source_text=msg["text"])
                return {"handled": False, "reason": "cooldown"}
        else:
            # 非授权群的非@消息：不落地
            db.audit("dingtalk", "msg_dropped_unauthorized", msg["group"])
            return {"handled": False, "reason": "no_match"}

        codes = [it["code"] for it in audit_parsed["items"]] if audit_parsed else []
        matched_sols = solmod.find_solutions_for_codes(self.cfg.solutions, codes)

        run_id = new_id("run")
        db.insert("runs", run_id=run_id, task_id=entry["id"], trigger_type=trigger,
                  source=msg["group"], status="running", engine=None, engine_version=None,
                  started_at=now(), finished_at=None, report_path=None, error_msg=None,
                  source_text=msg["text"])
        db.insert("messages", ignore=True, msg_id=msg["msg_id"], group_name=msg["group"],
                  sender=msg["sender"], received_at=now(),
                  msg_time=msg.get("msg_time") or None,
                  matched_entry_id=entry["id"], matched_rule=rule_hit, run_id=run_id,
                  source_text=msg["text"], parsed_json=audit_json)
        db.audit("dingtalk", "msg_matched", msg["group"], rule_hit, run_id)

        corr = correlate.build_context(db, msg["text"], codes, run_id=run_id)
        if corr:
            db.audit("task", "correlation_detected", msg["group"],
                     json.dumps({k: corr[k] for k in ("type_key", "count", "orders", "batch")},
                                ensure_ascii=False)[:300], run_id)
        ctx_text = msg["text"]
        corr_ctx = correlate.render_context(corr)
        if corr_ctx:
            ctx_text = corr_ctx + "\n\n" + ctx_text
        sol_ctx = solmod.render_solution_context(matched_sols)
        if sol_ctx:
            ctx_text = sol_ctx + "\n\n" + ctx_text
        try:
            result, eng, ver = await engine.analyze(
                self.cfg, db, {"text": ctx_text, "source": source,
                               "extra": {"group": msg["group"], "sender": msg["sender"],
                                         "alert": parsed,
                                         "audit_broadcast": audit_parsed,
                                         "alert_codes": codes,
                                         "correlation": corr,
                                         "matched_solutions": [
                                             {"code": x["code"], "write_gate": bool(x.get("enabled")),
                                              "write_entry_id": x.get("write_entry_id")}
                                             for x in matched_sols]}}, run_id)
        except engine.EngineUnavailable as e:
            # 引擎资源不可用（额度/限流/鉴权）：非分析失败，不写报告、不产报警，保留消息待重跑
            db.update("runs", "run_id", run_id, status="engine_unavailable",
                      finished_at=now(), error_msg=str(e)[:500])
            db.audit("engine", "engine_unavailable_run", msg["group"], str(e)[:300], run_id)
            self.notifier.toast(f"{msg['group']} 引擎资源不可用，可重试")
            return {"handled": False, "reason": "engine_unavailable", "run_id": run_id}
        except Exception as e:
            db.update("runs", "run_id", run_id, status="failed", finished_at=now(), error_msg=str(e))
            fail_result = {"normal": False,
                           "conclusion": f"AI 分析引擎执行失败，未产出结论。失败原因：{e}",
                           "anomalies": [{"severity": "P3", "summary": f"分析引擎执行失败：{e}"}],
                           "evidence": [{"action": "引擎调用", "finding": f"失败：{str(e)[:200]}"}]}
            try:
                reports.write_report(self.cfg, db, run_id, fail_result, msg["group"], "failed")
            except Exception:
                pass
            self.notifier.raise_alerts(run_id, msg["group"], fail_result["anomalies"])
            return {"handled": True, "run_id": run_id, "status": "failed"}

        if corr:
            result["correlation"] = corr
            if correlate.apply_batch_escalation(result, corr):
                db.audit("task", "batch_escalated", msg["group"],
                         f"{corr['count']}条同类/{len(corr['orders'])}订单，升级P2", run_id)
        if parsed and parsed.get("trace_id") and not result.get("evidence"):
            result.setdefault("evidence_warning",
                              "⚠️ 报警含 traceId 但引擎未产出日志取证，结论置信度不足；"
                              "建议在报警中心点\"重新分析\"补充要求。")
        reports.write_report(self.cfg, db, run_id, result, msg["group"], eng)
        self._handle_suggestions(run_id, result, source, matched_sols, msg["group"])
        if result.get("normal"):
            db.update("runs", "run_id", run_id, status="success", finished_at=now(),
                      engine=eng, engine_version=ver)
            self.notifier.toast(f"{msg['group']} 分析正常 ✓")
            db.insert("alerts", alert_id=new_id("al"), run_id=run_id,
                      source_group=msg["group"], severity="OK",
                      summary=result.get("summary", ""), detail="",
                      status="no_problem", created_at=now(), acked_at=None,
                      ignore_until=None, reopen_at=None)
        else:
            db.update("runs", "run_id", run_id, status="success", finished_at=now(),
                      engine=eng, engine_version=ver)
            self.notifier.raise_alerts(run_id, msg["group"], result.get("anomalies", []))
            self._reply_if_allowed(msg["group"], result, run_id, msg.get("text") or "")
        return {"handled": True, "run_id": run_id, "normal": result.get("normal")}

    def _reanalyze_prepare(self, orig_run_id, note):
        """同步准备：校验原 run、拼装重分析文本、落 run 记录。返回 (run_id, ctx) 或 None。"""
        db = self.db
        run0 = db.one("SELECT * FROM runs WHERE run_id=?", orig_run_id)
        if not run0:
            return None
        base_text = run0["source_text"] or ""
        text = (base_text + "\n\n【上一次分析结论】用户认为不准确。"
                + f"\n【用户补充输入】{note}\n请结合用户输入重新取证分析。")
        audit_parsed = None
        m0 = db.one("SELECT parsed_json FROM messages WHERE run_id=?", orig_run_id)
        if m0 and m0["parsed_json"]:
            try:
                audit_parsed = json.loads(m0["parsed_json"])
            except Exception:
                audit_parsed = None
        codes = [it["code"] for it in audit_parsed["items"]] if audit_parsed else []
        matched_sols = solmod.find_solutions_for_codes(self.cfg.solutions, codes)
        sol_ctx = solmod.render_solution_context(matched_sols)
        if sol_ctx:
            text = sol_ctx + "\n\n" + text
        run_id = new_id("run")
        db.insert("runs", run_id=run_id, task_id=run0["task_id"],
                  trigger_type="reanalyze", source=run0["source"], status="running",
                  engine=None, engine_version=None, started_at=now(),
                  finished_at=None, report_path=None, error_msg=None,
                  source_text=text)
        return run_id, {"run0": run0, "m0": m0, "text": text, "note": note,
                        "audit_parsed": audit_parsed, "codes": codes,
                        "matched_sols": matched_sols}

    async def _reanalyze_execute(self, run_id, ctx):
        """后台执行重分析：与客户端连接解耦，断开也不中断。"""
        db = self.db
        run0, m0, text, note = ctx["run0"], ctx["m0"], ctx["text"], ctx["note"]
        audit_parsed, codes, matched_sols = ctx["audit_parsed"], ctx["codes"], ctx["matched_sols"]
        orig_run_id = run0["run_id"]
        try:
            result, eng, ver = await engine.analyze(
                self.cfg, db, {"text": text, "source": "reanalyze",
                               "extra": {"previous_run": orig_run_id, "user_note": note,
                                         "audit_broadcast": audit_parsed, "alert_codes": codes}},
                run_id)
        except engine.EngineUnavailable as e:
            db.update("runs", "run_id", run_id, status="engine_unavailable",
                      finished_at=now(), error_msg=str(e)[:500])
            db.audit("engine", "engine_unavailable_run", run0["source"], str(e)[:300], run_id)
            return
        except Exception as e:
            db.update("runs", "run_id", run_id, status="failed",
                      finished_at=now(), error_msg=str(e))
            return
        reports.write_report(self.cfg, db, run_id, result, run0["source"], eng)
        db.conn.execute("UPDATE alerts SET status='reanalyzed' "
                        "WHERE run_id=? AND status IN ('pending','no_problem')",
                        (orig_run_id,))
        db.conn.commit()
        if result.get("normal"):
            db.update("runs", "run_id", run_id, status="success",
                      finished_at=now(), engine=eng, engine_version=ver)
            db.insert("alerts", alert_id=new_id("al"), run_id=run_id,
                      source_group=run0["source"], severity="OK",
                      summary=result.get("summary", ""), detail="",
                      status="no_problem", created_at=now(), acked_at=None,
                      ignore_until=None, reopen_at=None)
            self.notifier.toast(f"重新分析完成：正常 ✓")
        else:
            db.update("runs", "run_id", run_id, status="success",
                      finished_at=now(), engine=eng, engine_version=ver)
            grp = None
            src_type = "monitor_alert"
            if m0:
                row = db.one("SELECT group_name, parsed_json FROM messages WHERE run_id=?",
                             orig_run_id)
                if row:
                    grp = row["group_name"]
                    if row["parsed_json"]:
                        try:
                            pj = json.loads(row["parsed_json"])
                            if pj.get("kind") == "audit_broadcast":
                                src_type = "audit_broadcast"
                        except Exception:
                            pass
            if src_type == "monitor_alert" and run0["trigger_type"] == "dingtalk_at_me":
                src_type = "audit_group_at_me"
            self._handle_suggestions(run_id, result, src_type, matched_sols, grp)
            self.notifier.raise_alerts(run_id, run0["source"], result.get("anomalies", []))
            self._reply_if_allowed(run0["source"], result, run_id, run0["source_text"] or "")
        db.audit("task", "reanalyze", orig_run_id, note[:200], run_id)

    async def reanalyze(self, orig_run_id, note):
        """用户认为分析不准时，携带用户输入重新触发分析（同步等待完成，供测试/内部调用）。"""
        prep = self._reanalyze_prepare(orig_run_id, note)
        if not prep:
            return None
        run_id, ctx = prep
        await self._reanalyze_execute(run_id, ctx)
        return run_id

    async def rerun(self, run_id):
        """引擎资源不可用后的一键重跑：按原始消息语义重新分析（非「结论不准」的补充分析）。"""
        db = self.db
        run0 = db.one("SELECT * FROM runs WHERE run_id=?", run_id)
        if not run0:
            return None
        m0 = db.one("SELECT * FROM messages WHERE run_id=?", run_id)
        text = (m0["source_text"] if m0 and m0["source_text"] else run0["source_text"]) or ""
        if not text.strip():
            return None
        msg = {"msg_id": f"rerun-{run_id}-{new_id('m')}",
               "group": (m0["group_name"] if m0 else run0["source"]) or "",
               "sender": (m0["sender"] if m0 else "rerun") or "rerun",
               "text": text,
               # 重跑是用户显式动作，不受「非@我静默」门禁约束
               "at_me": True}
        db.audit("task", "rerun_submitted", msg["group"], f"from {run_id}", run_id)
        return await self.process(msg)

    def manual_trigger_solution(self, code, params, group):
        """报警中心手动触发某告警码的解决方案：直接按方案 actions 生成执行计划
        （pending_confirm），不重跑分析引擎。params 提供写动作所需参数，
        fixed_params 仍由配置强指定覆盖。四层门禁与计划化执行逻辑复用
        _handle_suggestions，真实执行仍须在二次确认页手动触发。"""
        sols = solmod.load_solutions(self.cfg.workspace)
        sol = solmod.get_solution(sols, code)
        if sol is None:
            return {"error": f"告警码 {code} 无对应解决方案"}
        if self.cfg.agent.get("writes_disabled", False):
            return {"error": "写操作紧急开关已开启，禁止触发"}
        if not sol.get("enabled"):
            return {"error": "方案写门禁未开启，请先在方案库开启该方案门禁"}
        params = params or {}
        suggestions = []
        for a in solmod.write_actions(sol):
            p = dict(params)
            fixed = a.get("fixed_params") or {}
            if isinstance(fixed, dict):
                p.update(fixed)
            suggestions.append({"app": a.get("app"), "feature": a.get("feature"),
                                "action_type": "data_correction", "params": p})
        db = self.db
        run_id = new_id("run")
        db.insert("runs", run_id=run_id, task_id="", trigger_type="manual_solution",
                  source=group or "", status="success", engine=None, engine_version=None,
                  started_at=now(), finished_at=now(), report_path=None, error_msg=None,
                  source_text=f"手动触发解决方案 {code}")
        result = {"normal": False, "summary": f"手动触发解决方案 {code}",
                  "anomalies": [], "suggestions": suggestions}
        self._handle_suggestions(run_id, result, "audit_broadcast", [sol], group)
        steps = db.q("SELECT id, entry_id, exec_result FROM auth_exec "
                     "WHERE run_id=? ORDER BY id", run_id)
        db.audit("task", "manual_trigger_solution", code,
                 json.dumps(params, ensure_ascii=False)[:200], run_id)
        return {"ok": True, "run_id": run_id,
                "steps": [{"id": s["id"], "entry": s["entry_id"],
                           "result": s["exec_result"]} for s in steps]}

    def _build_reply_markdown(self, result):
        md = "**LocalAgent 分析结论（仅供参考）**\n\n" + result.get("summary", "")
        for a in result.get("anomalies", []):
            md += f"\n- [{a.get('severity')}] {a.get('summary')}"
        return md

    def _group_auto_reply(self, group_name, alert_type=None):
        """仅白名单类型放行自动回复；unclassified 与未配置类型一律转人工。"""
        for g in self.cfg.groups:
            if g.get("name") == group_name:
                types = g.get("auto_reply_types") or []
                return bool(alert_type) and alert_type in types
        return False

    def _reply_if_allowed(self, group, result, run_id, source_text=""):
        from . import correlate as corrmod
        key = corrmod.family_key(source_text)
        alert_type = key.split(":", 1)[1] if key and key.startswith("kw:") else None
        if not self.cfg.dingtalk.get("reply_enabled", True):
            self.db.audit("dingtalk", "reply_skipped", group, "reply_enabled=false")
            return
        entry = authlist.find_entry(self.cfg.auth_entries, "dingtalk", "write", "回复分析结论到值班群")
        if entry is None or group not in entry.get("constraints", {}).get("groups", []):
            self.db.insert("auth_exec", entry_id="dingtalk-write-reply", run_id=run_id,
                           action_type="message_write", matched=0,
                           reject_reason="no enabled entry or group not allowed",
                           exec_result="skipped", ts=now())
            return
        md = self._build_reply_markdown(result)
        type_tag = alert_type or "unclassified"
        if self._group_auto_reply(group, alert_type):
            if self.ding.reply(group, md):
                self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                               action_type="message_write", matched=1, reject_reason="",
                               exec_result="replied", ts=now())
                self.db.audit("dingtalk", "reply_auto", group, f"alert_type={type_tag}", run_id)
                return
            self.db.audit("dingtalk", "reply_auto_failed", group,
                          f"alert_type={type_tag}，自动发送失败转人工", run_id)
        self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                       action_type="message_write", matched=1,
                       reject_reason=f"awaiting manual send (alert_type={type_tag})",
                       exec_result="pending_reply", ts=now(),
                       payload=json.dumps({"group": group, "markdown": md,
                                           "run_id": run_id,
                                           "alert_type": type_tag,
                                           "summary": result.get("summary", ""),
                                           "anomalies": result.get("anomalies", [])},
                                          ensure_ascii=False))
        self.db.audit("dingtalk", "reply_pending", group,
                      f"alert_type={type_tag}", run_id)

    def send_reply(self, exec_id):
        """手动发送待回复；返回 (run_id, ok)。发送失败保持 pending_reply 可重试。"""
        row = self.db.one("SELECT * FROM auth_exec WHERE id=? AND exec_result='pending_reply'",
                          exec_id)
        if not row:
            return None
        payload = json.loads(row["payload"])
        group = payload["group"]
        md = payload["markdown"]
        ok = bool(self.ding.reply(group, md))
        if ok:
            self.db.update("auth_exec", "id", exec_id,
                           exec_result="replied", reject_reason="")
            self.db.audit("dingtalk", "reply_sent_manual", group, "", row["run_id"])
        else:
            self.db.update("auth_exec", "id", exec_id, exec_result="pending_reply",
                           reject_reason="发送失败：消息未投递到钉群，可重试（详见 reply_failed 审计）")
            self.db.audit("dingtalk", "send_manual_failed", group, "", row["run_id"])
        return row["run_id"], ok

    def reject_reply(self, exec_id):
        row = self.db.one("SELECT * FROM auth_exec WHERE id=? AND exec_result='pending_reply'",
                          exec_id)
        if not row:
            return None
        self.db.update("auth_exec", "id", exec_id,
                       exec_result="rejected", reject_reason="user discarded")
        self.db.audit("dingtalk", "reply_rejected", "", "", row["run_id"])
        return row["run_id"]

    def _handle_suggestions(self, run_id, result, source, solutions=None, group=None):
        writes_disabled = bool(self.cfg.agent.get("writes_disabled", False))
        executed = False  # 单次任务最多执行 1 次写操作
        suggestions = result.get("suggestions", [])
        planned_features = set()
        planned_entry_ids = set()

        # 门禁开启的方案：严格按 actions 配置顺序生成执行计划（Agent 不决定动作与顺序）
        for sol in solutions or []:
            if writes_disabled or not sol.get("enabled"):
                continue
            plan = solmod.build_execution_plan(sol, suggestions)
            plan_id = f"{run_id}:{sol.get('code')}"
            for step in plan:
                if step.get("missing"):
                    self.db.insert("auth_exec", entry_id=step.get("entry_id") or sol.get("code"),
                                   run_id=run_id, action_type=step["type"], matched=0,
                                   reject_reason=f"计划第{step['step_no']}步缺少参数 {step['missing']}，计划截断",
                                   exec_result="suggested", ts=now())
                    self.db.audit("auth", "plan_step_missing_params", sol.get("code"),
                                  str(step["missing"]), run_id)
                    break
                if step["type"] == "ateye_write":
                    planned_features.add(step.get("feature") or "")
                    if step.get("entry_id"):
                        planned_entry_ids.add(step["entry_id"])
                    entry = next((e for e in self.cfg.auth_entries
                                  if e.get("id") == step.get("entry_id") and e.get("enabled")), None)
                    if entry is None:
                        self.db.insert("auth_exec", entry_id=str(step.get("entry_id")),
                                       run_id=run_id, action_type=step["type"], matched=0,
                                       reject_reason=f"计划第{step['step_no']}步授权条目缺失或停用，计划截断",
                                       exec_result="suggested", ts=now())
                        self.db.audit("auth", "plan_step_no_entry", str(step.get("entry_id")), "", run_id)
                        break
                    ctx = {"alert": {"type": "AUDIT", "source": source},
                           "params": step.get("params", {}), "analysis": {"source": source}}
                    ok, reason = authlist.check_write(self.db, entry, ctx)
                    if not ok:
                        self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                                       action_type=step["type"], matched=0,
                                       reject_reason=f"计划第{step['step_no']}步校验失败: {reason}，计划截断",
                                       exec_result="suggested", ts=now())
                        self.db.audit("auth", "plan_step_rejected", entry["id"], reason, run_id)
                        break
                    self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                                   action_type=step["type"], matched=1,
                                   reject_reason=f"plan {sol.get('code')} 第{step['step_no']}步",
                                   exec_result="pending_confirm", ts=now(),
                                   payload=json.dumps({"plan_id": plan_id, "step_no": step["step_no"],
                                                       "step_type": "write", "step_name": step["name"],
                                                       "suggestion": {"app": entry.get("app"),
                                                                      "feature": step.get("feature"),
                                                                      "params": step.get("params", {})},
                                                       "entry_id": entry["id"]}, ensure_ascii=False))
                    self.db.audit("auth", "plan_step_pending_confirm", entry["id"],
                                  f"{sol.get('code')} step{step['step_no']}", run_id)
                elif step["type"] == "dingtalk_reply" and group:
                    md = self._build_reply_markdown(result)
                    self.db.insert("auth_exec", entry_id="dingtalk-write-reply", run_id=run_id,
                                   action_type="message_write", matched=1,
                                   reject_reason=f"plan {sol.get('code')} 第{step['step_no']}步",
                                   exec_result="pending_confirm", ts=now(),
                                   payload=json.dumps({"plan_id": plan_id, "step_no": step["step_no"],
                                                       "step_type": "dingtalk_reply",
                                                       "step_name": step["name"],
                                                       "group": group, "markdown": md},
                                                      ensure_ascii=False))
                    self.db.audit("auth", "plan_step_pending_confirm", "dingtalk-write-reply",
                                  f"{sol.get('code')} step{step['step_no']}", run_id)

        for s in suggestions:
            if not isinstance(s, dict):
                s = {"app": None, "feature": str(s), "action_type": "data_correction", "params": {}}
            if s.get("feature") in planned_features:
                continue  # 已由执行计划接管，严格按配置执行
            entry = authlist.find_entry(self.cfg.auth_entries, s.get("app"), "write", s.get("feature"))
            if entry is not None and entry["id"] in planned_entry_ids:
                continue  # 同一授权条目已由执行计划接管
            ctx = {"alert": {"type": "REFUND_FEE_MISMATCH" if "金额" in s.get("feature", "") else "AUDIT",
                             "source": source},
                   "diff_amount": 45, "params": s.get("params", {}),
                   "analysis": {"source": source}}
            if writes_disabled:
                self.db.insert("auth_exec", entry_id=(entry or {}).get("id", f"{s.get('app')}/{s.get('feature')}"),
                               run_id=run_id, action_type=s.get("action_type"), matched=0,
                               reject_reason="writes_disabled 紧急开关已开启", exec_result="suggested", ts=now())
                self.db.audit("auth", "write_blocked_by_switch", s.get("app"), s.get("feature"), run_id)
                continue
            if entry is None:
                self.db.insert("auth_exec", entry_id=f"{s.get('app')}/{s.get('feature')}",
                               run_id=run_id, action_type=s.get("action_type"), matched=0,
                               reject_reason="no matching auth entry → suggest only",
                               exec_result="suggested", ts=now())
                self.db.audit("auth", "write_suggested", s.get("app"), json.dumps(s, ensure_ascii=False), run_id)
                continue
            gate_sol = solmod.find_gate_for_entry(solutions or [], entry["id"])
            if gate_sol and not gate_sol.get("enabled", False):
                self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                               action_type=s.get("action_type"), matched=0,
                               reject_reason=f"solution gate closed: 方案 {gate_sol['code']} 写操作门禁未开启",
                               exec_result="suggested", ts=now())
                self.db.audit("auth", "write_blocked_by_solution_gate", entry["id"],
                              gate_sol["code"], run_id)
                continue
            ok, reason = authlist.check_write(self.db, entry, ctx)
            if not ok:
                self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                               action_type=s.get("action_type"), matched=0,
                               reject_reason=reason, exec_result="suggested", ts=now())
                self.db.audit("auth", "write_rejected", entry["id"], reason, run_id)
                continue
            if authlist.write_requires_confirm(entry):
                self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                               action_type=s.get("action_type"), matched=1,
                               reject_reason="awaiting manual confirm"
                               + (" (online env)" if entry.get("env") == "online" else ""),
                               exec_result="pending_confirm", ts=now(),
                               payload=json.dumps({"suggestion": s, "entry_id": entry["id"]},
                                                  ensure_ascii=False))
                self.db.audit("auth", "write_pending_confirm", entry["id"], "", run_id)
                continue
            if executed:
                self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                               action_type=s.get("action_type"), matched=1,
                               reject_reason="single-run write limit reached",
                               exec_result="suggested", ts=now())
                self.db.audit("auth", "write_limit_suggested", entry["id"], "", run_id)
                continue
            ok, detail = authlist.execute_write(entry, s.get("params", {}))
            executed = True
            self.db.insert("auth_exec", entry_id=entry["id"], run_id=run_id,
                           action_type=s.get("action_type"), matched=1,
                           reject_reason="" if ok else detail[:200],
                           exec_result="executed" if ok else "failed", ts=now())
            self.db.audit("auth", "write_executed" if ok else "write_failed", entry["id"],
                          detail[:300], run_id)
            if not ok and entry.get("disableOnFailure"):
                authlist.disable_entry(self.cfg.workspace, entry["id"])
                self.db.audit("auth", "entry_auto_disabled", entry["id"], "disableOnFailure", run_id)
                self._reload_cfg()

    def _reload_cfg(self):
        from .config import Config
        self.cfg = Config()
