import os
import yaml

DEFAULT_WORKSPACE = os.path.expanduser("~/developer/localagent/workspace")


class Config:
    def __init__(self, workspace=None):
        self.workspace = workspace or os.environ.get("LOCALAGENT_WORKSPACE", DEFAULT_WORKSPACE)
        self.agent = self._load("config/agent.yaml", {})
        self.auth_entries = self._load("config/auth_list.yaml", {}).get("entries", [])

    def _load(self, rel, default):
        p = os.path.join(self.workspace, rel)
        if not os.path.exists(p):
            return default
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or default

    @property
    def mock(self):
        if os.environ.get("LOCALAGENT_MOCK") == "1":
            return True
        return bool(self.agent.get("mock", True))

    @property
    def dingtalk(self):
        return self.agent.get("dingtalk", {})

    @property
    def groups(self):
        from . import configsync
        return configsync.load_groups(self.workspace)

    @property
    def solutions(self):
        from . import solutions as solmod
        return solmod.load_solutions(self.workspace)

    @property
    def engines(self):
        return self.agent.get("engines", {"default": "qoder", "list": []})

    @property
    def notify(self):
        return self.agent.get("notify", {})

    @property
    def reply_policy(self):
        from . import reply_policy as rp
        p = self._load("config/reply_policy.yaml", None)
        return p if p is not None else rp.default_policy()

    @property
    def web(self):
        return self.agent.get("web", {"host": "127.0.0.1", "port": 8765})

    def engine_cmd(self, name):
        for e in self.engines.get("list", []):
            if e.get("name") == name:
                return e.get("cmd")
        return None
