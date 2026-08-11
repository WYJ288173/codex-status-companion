"""校验本机 skill 配置：定位 "N warnings loading skill configs" 的具体来源。
检查每个 SKILL.md 的 YAML frontmatter（开头 --- / 闭合 --- / name / description /
name 与目录名一致 / YAML 可解析），以及 plugin 清单。
运行：./.venv/bin/python tests/validate_skill_configs.py [skills 根目录]
"""
import os
import sys

import yaml

ROOTS = sys.argv[1:] or [os.path.expanduser("~/.agents/skills"),
                         os.path.expanduser("~/.qoder/plugins")]


def parse_frontmatter(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.startswith("---"):
        return None, "frontmatter 缺开头 ---"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "frontmatter 未闭合（缺结尾 ---）"
    raw = text[3:end]
    try:
        meta = yaml.safe_load(raw)
    except Exception as e:
        return None, f"frontmatter YAML 解析失败: {str(e)[:120]}"
    if not isinstance(meta, dict):
        return None, "frontmatter 不是键值映射"
    return meta, None


problems = []
notes = []
checked = 0
for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            if not name.startswith("."):
                notes.append((os.path.join(root, name), "skills 目录下存在非目录条目"))
            continue
        sk = os.path.join(d, "SKILL.md")
        has_pkg = os.path.exists(os.path.join(d, "package.json"))
        has_plugin = os.path.isdir(os.path.join(d, ".qoder-plugin")) or \
            os.path.isdir(os.path.join(d, "skills"))
        if not os.path.exists(sk):
            if has_pkg and not has_plugin:
                problems.append((d, "有 package.json 但缺 SKILL.md（skill 无法注册）"))
            else:
                notes.append((d, "无 SKILL.md（参考资料目录，非 skill）"))
            continue
        checked += 1
        meta, err = parse_frontmatter(sk)
        if err:
            problems.append((sk, err))
            continue
        missing = [k for k in ("name", "description") if not meta.get(k)]
        if missing:
            problems.append((sk, f"frontmatter 缺字段 {missing}"))
            continue
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            problems.append((sk, "description 为空或非字符串"))
            continue
        # 以下两类实测不触发 CLI 告警（修正 name 不一致后告警数未变），仅作提示
        if meta["name"] != name:
            notes.append((sk, f"frontmatter name={meta['name']} 与目录名 {name} 不一致"))
        extra = [k for k in meta if k not in
                 ("name", "description", "license", "allowed-tools", "allowed_tools",
                  "metadata", "model", "version", "compatible-with")]
        if extra:
            notes.append((sk, f"frontmatter 含非标准字段 {extra}"))

print(f"已校验 SKILL.md: {checked} 个；硬性缺陷 {len(problems)} 处；提示 {len(notes)} 处")
for path, why in problems:
    print(f"  [缺陷] {why}\n         {path}")
for path, why in notes[:5]:
    print(f"  [提示] {why}\n         {path}")
if len(notes) > 5:
    print(f"  [提示] 其余 {len(notes) - 5} 条省略")
sys.exit(0 if not problems else 1)
