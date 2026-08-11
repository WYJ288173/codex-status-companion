import os

import yaml

GROUPS_REL = os.path.join("config", "groups.yaml")
AUTH_REL = os.path.join("config", "auth_list.yaml")


def load_groups(workspace):
    p = os.path.join(workspace, GROUPS_REL)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("groups", [])


def save_groups(workspace, groups):
    """groups.yaml 为群清单唯一源；同步刷新 auth_list.yaml 的读/写条目 groups。"""
    with open(os.path.join(workspace, GROUPS_REL), "w", encoding="utf-8") as f:
        yaml.safe_dump({"groups": groups}, f, allow_unicode=True, sort_keys=False)

    ap = os.path.join(workspace, AUTH_REL)
    with open(ap, encoding="utf-8") as f:
        auth = yaml.safe_load(f) or {"entries": []}
    enabled = [g["name"] for g in groups if g.get("enabled", True)]
    alert_groups = [g["name"] for g in groups
                    if g.get("enabled", True) and g.get("mode") in ("alert", "both")]
    me_groups = [g["name"] for g in groups
                 if g.get("enabled", True) and g.get("mode") in ("at_me", "both")]
    for e in auth.get("entries", []):
        if e.get("id") == "dingtalk-read-monitor-msg":
            e.setdefault("constraints", {})["groups"] = alert_groups
        elif e.get("id") == "dingtalk-read-audit-at-me":
            e.setdefault("constraints", {})["groups"] = me_groups
        elif e.get("id") == "dingtalk-write-reply":
            e.setdefault("constraints", {})["groups"] = enabled
    with open(ap, "w", encoding="utf-8") as f:
        yaml.safe_dump(auth, f, allow_unicode=True, sort_keys=False)
    return enabled, alert_groups, me_groups


def load_auth(workspace):
    p = os.path.join(workspace, AUTH_REL)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("entries", [])


def save_auth(workspace, entries):
    with open(os.path.join(workspace, AUTH_REL), "w", encoding="utf-8") as f:
        yaml.safe_dump({"entries": entries}, f, allow_unicode=True, sort_keys=False)
