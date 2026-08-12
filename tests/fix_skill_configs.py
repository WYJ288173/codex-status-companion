"""修复 ~/.agents/skills 下导致 "warnings loading skill configs" 的硬性缺陷。
幂等：已修则跳过；修完用 yaml 复核 frontmatter 可解析。
运行：./.venv/bin/python tests/fix_skill_configs.py [--apply]
"""
import io
import os
import sys

import yaml

APPLY = "--apply" in sys.argv
# 防呆：--appl / --aply 之类拼错会静默走预演，直接报错退出
for _a in sys.argv[1:]:
    if _a.startswith("--a") and _a not in ("--apply",):
        sys.exit(f"参数拼写错误：{_a}（是否想输入 --apply ？）未做任何修改。")
# --root <dir> 便于在夹具上自测；缺省为本机 skills 根目录
SK = os.path.expanduser("~/.agents/skills")
if "--root" in sys.argv:
    SK = os.path.abspath(sys.argv[sys.argv.index("--root") + 1])
changed = []


def read(p):
    return io.open(p, encoding="utf-8").read()


def write(p, s):
    if APPLY:
        io.open(p, "w", encoding="utf-8").write(s)


def split_fm(s):
    """返回 (frontmatter 文本, 分隔符, 其余)；无 frontmatter 返回 (None, None, s)。"""
    if not s.startswith("---"):
        return None, None, s
    end = s.find("\n---", 3)
    if end == -1:
        return None, None, s
    return s[3:end], s[end:end + 4], s[end + 4:]


def quote_scalar(v):
    v = v.strip()
    if v.startswith(('"', "'")) and v.endswith(('"', "'")) and len(v) > 1:
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', "'") + '"'


def fix_unquoted_description(path):
    """description 值含 ':' 未加引号导致 YAML 解析失败 → 给标量加双引号。"""
    s = read(path)
    fm, sep, rest = split_fm(s)
    if fm is None:
        return "无 frontmatter（交由 add_frontmatter 处理）"
    try:
        yaml.safe_load(fm)
        return "frontmatter 已可解析，跳过"
    except Exception:
        pass
    out, fixed = [], False
    for line in fm.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("description:") and not fixed:
            indent = line[:len(line) - len(stripped)]
            val = stripped[len("description:"):]
            out.append(f"{indent}description: {quote_scalar(val)}")
            fixed = True
        elif stripped.startswith("description_zh:"):
            indent = line[:len(line) - len(stripped)]
            val = stripped[len("description_zh:"):]
            out.append(f"{indent}description_zh: {quote_scalar(val)}")
        else:
            out.append(line)
    new_fm = "\n".join(out)
    try:
        yaml.safe_load(new_fm)
    except Exception as e:
        return f"修复后仍不可解析，放弃：{str(e)[:80]}"
    write(path, "---" + new_fm + sep + rest)
    changed.append(path)
    return "已给 description 加引号"


def add_frontmatter(path, name):
    """整文件缺 frontmatter → 用首个 'Use when' 段落作为 description 补齐。"""
    s = read(path)
    if s.startswith("---"):
        return "已有 frontmatter，跳过"
    desc = ""
    for line in s.split("\n"):
        t = line.strip()
        if t.startswith("Use when") or t.startswith("Use When"):
            desc = t
            break
    if not desc:
        for line in s.split("\n"):
            t = line.strip()
            if t and not t.startswith("#") and not t.startswith("---"):
                desc = t
                break
    if not desc:
        return "找不到可用描述，跳过"
    fm = f"---\nname: {name}\ndescription: {quote_scalar(desc)}\n---\n\n"
    yaml.safe_load(fm[3:fm.find(chr(10) + '---', 3)])
    write(path, fm + s)
    changed.append(path)
    return f"已补 frontmatter（description {len(desc)} 字）"


def create_skill_md_from_pkg(d, name):
    """有 package.json 但缺 SKILL.md → 用 package.json 的 description 生成最小 SKILL.md。"""
    sk = os.path.join(d, "SKILL.md")
    if os.path.exists(sk):
        return "SKILL.md 已存在，跳过"
    import json
    pkg_path = os.path.join(d, "package.json")
    if not os.path.exists(pkg_path):
        return "无 package.json，跳过"
    pkg = json.loads(read(pkg_path))
    desc = (pkg.get("description") or "").strip()
    if not desc:
        return "package.json 无 description，跳过（需人工补内容）"
    refs = [x for x in sorted(os.listdir(d))
            if x not in ("package.json",) and not x.startswith(".")]
    body = (f"---\nname: {name}\ndescription: {quote_scalar(desc)}\n---\n\n"
            f"# {name}\n\n## 说明\n\n{desc}\n\n"
            f"## 目录内容\n\n" + "\n".join(f"- `{x}`" for x in refs) + "\n\n"
            f"> 本 SKILL.md 由 package.json 描述重建（原安装包缺失指令体）；"
            f"详细用法参见上述目录内的参考资料与脚本。\n")
    write(sk, body)
    changed.append(sk)
    return "已由 package.json 重建 SKILL.md"


MAX_DESC = 512
SAFE_DESC = 500


def trim_long_description(path):
    """description 超过 512 字符会触发 CLI skill 配置告警 → 按句子边界裁剪到 <=500。
    有损修改，写盘前备份 SKILL.md.bak。"""
    s = read(path)
    fm, sep, rest = split_fm(s)
    if fm is None:
        return "无 frontmatter，跳过"
    try:
        meta = yaml.safe_load(fm) or {}
    except Exception as e:
        return f"frontmatter 不可解析，先修引号：{str(e)[:60]}"
    desc = str(meta.get("description") or "")
    if len(desc) <= MAX_DESC:
        return f"description {len(desc)} 字符，未超限，跳过"
    cut = desc[:SAFE_DESC]
    for mark in ("。", "；", ". ", "; "):
        i = cut.rfind(mark)
        if i > SAFE_DESC * 0.5:
            cut = cut[:i + len(mark)].rstrip()
            break
    meta["description"] = cut
    order = ["name", "description"] + [k for k in meta if k not in ("name", "description")]
    lines = []
    for k in order:
        if k not in meta:
            continue
        v = meta[k]
        lines.append(f"{k}: {quote_scalar(str(v))}" if isinstance(v, str) else f"{k}: {v}")
    new_fm = "\n" + "\n".join(lines)
    try:
        yaml.safe_load(new_fm)
    except Exception as e:
        return f"重写后不可解析，放弃：{str(e)[:60]}"
    if APPLY:
        io.open(path + ".bak", "w", encoding="utf-8").write(s)
    write(path, "---" + new_fm + sep + rest)
    changed.append(path)
    return f"description {len(desc)} -> {len(cut)} 字符（原文备份 .bak）"


TASKS = [
    ("refund-kb-loop", add_frontmatter, "refund-kb-loop/SKILL.md"),
    ("biz-rule-distill", fix_unquoted_description, "biz-rule-distill/SKILL.md"),
    ("change-flight-prd-writing", fix_unquoted_description,
     "change-flight-prd-writing/SKILL.md"),
    ("tickets-platform-skill", create_skill_md_from_pkg, None),
    # 实测告警真因：description 超过 512 字符（恰好这 2 个）
    ("a1", trim_long_description, "a1/SKILL.md"),
    ("change-flight-biz-rule-distill", trim_long_description,
     "change-flight-biz-rule-distill/SKILL.md"),
]

# --rollback：把所有 SKILL.md.bak 还原回 SKILL.md（撤销有损裁剪）
if "--rollback" in sys.argv:
    import glob
    n = 0
    for bak in sorted(glob.glob(os.path.join(SK, "*", "SKILL.md.bak"))):
        target = bak[:-4]
        io.open(target, "w", encoding="utf-8").write(io.open(bak, encoding="utf-8").read())
        os.remove(bak)
        print("已还原:", target)
        n += 1
    print(f"共还原 {n} 个文件" if n else "没有 .bak 备份，无需还原")
    sys.exit(0)

print(("应用修复" if APPLY else "预演（不写盘）") + f"，根目录 {SK}\n")
for name, fn, rel in TASKS:
    target = os.path.join(SK, rel) if rel else os.path.join(SK, name)
    if not os.path.exists(target):
        print(f"- {name}: 目标不存在 {target}")
        continue
    if fn is create_skill_md_from_pkg:
        msg = fn(target, name)
    elif fn is add_frontmatter:
        msg = fn(target, name)
    else:
        msg = fn(target)
    print(f"- {name}: {msg}")
print(f"\n{'已写入' if APPLY else '待写入'} {len(changed)} 个文件")
