# Codex Status Companion 设计

## 1. 背景

官方 Codex CLI 的状态栏只支持编译期内置的 `StatusLineItem`，插件、MCP、Skill 与 Hook 都不能向 TUI footer 注册新的字段或布局。现有 `codex-custom` fork 已验证 Claude 风格多行状态栏、Context、会话 Token、Today/Week/Month/Total 等信息的使用价值，但 fork 需要跟随上游持续 rebase，只适合个人使用，不适合作为外部用户的安装方案。

本项目提供一个独立的 Codex Marketplace 插件，在不修改、不包裹、不替换官方 Codex CLI 的前提下，通过 Codex Hooks 获取会话上下文，在终端原生状态区域展示丰富状态信息。

## 2. 目标

- 用户继续直接运行官方 `codex`。
- 不维护对外 Codex fork，不实现另一套 Codex TUI，不通过 PTY 拦截官方 TUI。
- 插件安装后自动响应 Codex 会话事件，无需每次手工启动伴随进程。
- 展示 Model、Reasoning、工作目录、Git 分支、Context、会话 Input/Output Token、Today、Week、Month、Total、5 小时与每周额度。
- Today/Week/Month/Total 直接使用这些名称，不增加 `Local` 前缀。
- 允许用户用外部命令自定义最终文本、颜色和进度条，形成类似 Claude Code `statusLine.command` 的渲染协议。
- 自动识别终端环境，并在能力不足时有明确、无干扰的降级行为。
- 数据采集和刷新不能阻塞 Codex 的交互与渲染。

## 3. 非目标

- 不向官方 Codex 提交功能或等待官方接受扩展协议。
- 不在官方 Codex TUI 内部 footer 中增加行或字段。
- 不保证 Today/Week/Month/Total 等同于跨设备的官方账户账单统计。
- 首版不实现完整 GUI、Web Dashboard 或菜单栏应用。
- 首版不修改用户的 shell alias，也不替换 PATH 中的 `codex`。

## 4. 用户体验

### 4.1 安装

项目以 Codex Marketplace 仓库分发。用户添加 marketplace、安装插件并新建 Codex 会话后，插件 Hooks 自动生效。插件不能静默修改官方 Codex 二进制。

### 4.2 默认展示

默认内容分为三组：

1. 会话：Model、Reasoning、目录、Git 分支。
2. 当前上下文：Context 使用率、Input、Output。
3. 使用量：Today、Week、Month、Total、5h、Weekly。

终端宽度不足时，按“目录细节 → Total → Month → Week → Input/Output → Model”顺序逐步缩减，始终优先保留 Context 和额度告警。

### 4.3 展示位置

适配器按以下顺序自动选择：

1. tmux：当前 pane 对应的 pane border 或专用 status segment。
2. WezTerm：用户变量与右侧状态区域。
3. Zellij：pane frame 或状态插件桥接。
4. iTerm2、Kitty：Tab/窗口标题与受支持的终端元数据区域。
5. 通用终端：OSC 标题，只显示精简摘要。

状态信息位于同一终端窗口的原生状态区域，但不占用 Codex TUI 内部 footer。

## 5. 总体架构

```text
官方 Codex CLI
    │
    │ SessionStart / Stop / PostToolUse / PreCompact / PostCompact Hooks
    │ stdin: session_id, transcript_path, cwd, model, permission_mode
    ▼
codex-status-hook
    │
    ├── 读取并增量解析当前 session JSONL
    ├── 更新按 session 与日期聚合的状态存储
    ├── 读取 Git、终端和额度信息
    └── 生成统一 StatusSnapshot
             │
             ├── 默认 Renderer
             ├── 用户 command Renderer
             └── Terminal Adapter
                    ├── tmux
                    ├── wezterm
                    ├── zellij
                    ├── iterm2 / kitty
                    └── osc-title
```

实现采用单个 Rust CLI `codex-status`，避免依赖 Node、Python、jq 或常驻 Runtime。Hook 每次以短进程方式调用；后续只有在需要空闲定时刷新时才增加可选 daemon，首版不默认常驻。

## 6. 组件设计

### 6.1 Hook 入口

插件提供 `hooks/hooks.json`，订阅：

- `SessionStart`：建立 session 状态、识别终端、首次渲染。
- `Stop`：在助手响应完成后读取最新 Token 与 Context 数据并刷新。
- `PostToolUse`：在 Git 状态可能改变时刷新目录、分支与变更摘要。
- `PreCompact`、`PostCompact`：在压缩前后刷新 Context。

Hook 从 stdin 读取 JSON，不向 stdout 输出普通文本，避免把状态内容写入 Codex 会话。需要与终端通信时使用适配器命令或 `/dev/tty` 的非屏幕 OSC 控制序列。

### 6.2 Session 数据采集

Hook 输入提供 `session_id`、`transcript_path`、`cwd` 和 `model`。采集器保存每个 transcript 的文件标识、已处理 offset 和最后事件指纹，只处理新增 JSONL 内容。

重点解析 `token_count` 事件：

- `total_token_usage.input_tokens`
- `total_token_usage.output_tokens`
- `total_token_usage.total_tokens`
- `model_context_window`
- `rate_limits`

Context 使用率优先使用事件中的明确值；缺少时使用 `total_tokens / model_context_window` 计算并限制在 `0..100`。

解析器忽略未知事件和未知字段，以适配官方 Codex 新增协议字段。单行损坏不能中断后续事件处理。

### 6.3 Today/Week/Month/Total 聚合

Today/Week/Month/Total 来自本机 Codex session JSONL 的 Token 增量。计算规则：

- 时区默认使用系统本地时区；用户可配置固定 IANA 时区。
- Week 默认周一开始，可配置周日起始。
- Month 为自然月累计。
- Total 为当前可访问 session 日志的累计 Token。
- 展示名称固定为 Today、Week、Month、Total。

每个 rollout 文件内部的累计 Token 转换成增量后再写入日期桶，避免同一 session 重复累计。状态存储记录文件 offset 和上一次累计值，正常刷新不全量扫描历史文件。

首次安装或状态存储丢失时执行一次后台式全量索引；Hook 本次调用只在时间预算内处理，未完成部分留到下次继续，不阻塞 Codex。

### 6.4 状态存储

默认路径：

```text
~/.cache/codex-status/state-v1.json
```

状态包括：

- schema version。
- 每个 transcript 的 offset、最后累计 Token、最近修改时间。
- 按日期聚合的 Token 桶。
- 每个 session 的最近 StatusSnapshot。
- 最近成功的额度结果及更新时间。
- 每个终端目标的上一次渲染文本，用于避免重复刷新。

写入使用临时文件加原子 rename。并发 Hook 通过短时文件锁串行化；拿锁超时则跳过本次刷新，不影响 Codex。

### 6.5 统一快照协议

内部和自定义 Renderer 使用版本化 JSON：

```json
{
  "schema_version": 1,
  "session_id": "...",
  "model": {"id": "gpt-5.4", "reasoning": "high"},
  "workspace": {"cwd": "/project", "git_branch": "main"},
  "context": {"used_percentage": 62, "remaining_percentage": 38},
  "tokens": {
    "input": 182400,
    "output": 16300,
    "today": 1200000,
    "week": 5800000,
    "month": 17400000,
    "total": 42100000
  },
  "limits": {
    "five_hour_used_percentage": 27,
    "weekly_used_percentage": 59,
    "five_hour_resets_at": null,
    "weekly_resets_at": null
  },
  "terminal": {"adapter": "tmux", "columns": 160}
}
```

缺失数据使用 `null`，不伪造 `0`。

### 6.6 Renderer

默认 Renderer 产生无控制字符的逻辑段，再由适配器决定颜色编码。颜色只表达语义：

- 正常：绿色或终端默认色。
- Context/额度接近阈值：黄色。
- Context/额度超过高风险阈值：红色。
- 路径与辅助元数据：弱化色。

用户可配置自定义命令。`codex-status` 将 StatusSnapshot JSON 写入命令 stdin，并读取 stdout 作为渲染结果。自定义命令必须满足：

- 默认超时 200 ms，可配置但有上限。
- 输出有字节数和行数上限。
- 非零退出、超时或空输出时回退到默认 Renderer。
- stderr 仅写诊断日志，不进入 Codex 对话。

### 6.7 Terminal Adapter

每个 Adapter 实现统一接口：

```text
detect(environment) -> confidence
capabilities() -> colors, multiline, links, width
render(snapshot, rendered_text)
clear(target)
```

Adapter 不应修改全局终端配置。对于 tmux，首版优先使用当前 pane 可定位的显示面，避免覆盖用户全局 `status-right`；如果终端只能修改全局状态，则必须显式 opt-in。

通用 OSC Adapter 只使用窗口标题等不会破坏 Codex alternate screen 的控制序列，不写普通字符到 TTY。

## 7. 配置

默认配置路径：

```text
~/.config/codex-status/config.toml
```

示例：

```toml
adapter = "auto"
timezone = "Asia/Shanghai"
week_starts_on = "monday"

[refresh]
history_rescan_seconds = 600
hook_timeout_ms = 300

[renderer]
command = "~/.config/codex-status/render.sh"
timeout_ms = 200

[display]
show = [
  "model",
  "cwd",
  "git-branch",
  "context",
  "input",
  "output",
  "today",
  "week",
  "month",
  "total",
  "five-hour-limit",
  "weekly-limit",
]
```

没有配置文件时使用内置默认值。配置错误时保留可运行的默认配置并记录诊断。

## 8. 错误处理与性能预算

- 单次 Hook 默认总预算 300 ms；超时直接跳过刷新。
- 正常增量解析目标小于 50 ms。
- Git 命令独立超时，失败时隐藏 Git 段。
- transcript 不存在或尚未写入 Token 时显示 `—`，不显示 `0`。
- 未识别终端时退化到 OSC title；如果终端不支持，则静默不展示，不污染对话。
- 任何错误写入轮转诊断日志，默认不向用户频繁提示。
- 状态存储损坏时隔离旧文件并重建，不删除 Codex session 数据。

## 9. 安全边界

- 插件只读取 Codex Hook 明确提供的 transcript 和当前 Git 工作区元数据。
- 不读取对话正文用于展示；解析器只消费 Token、模型、Context、额度相关事件。
- 不上传数据，不监听网络端口。
- 自定义 Renderer 是用户显式配置的本地命令，受超时和输出限制。
- 终端输出过滤危险控制序列；默认 Renderer 只产生受控颜色与文本。
- 插件卸载不删除 Codex session，只清理自身缓存和可选终端状态。

## 10. 分发结构

```text
LocalAgent/
├── .agents/plugins/marketplace.json
├── plugins/
│   └── codex-status/
│       ├── .codex-plugin/plugin.json
│       ├── hooks/hooks.json
│       ├── skills/codex-status/SKILL.md
│       ├── scripts/
│       └── assets/
├── crates/
│   └── codex-status/
├── docs/
└── .github/workflows/
```

Release 为 macOS arm64/x86_64、Linux x86_64/arm64 和 Windows x86_64 构建独立二进制。开发阶段先完成 macOS + tmux/OSC Adapter，再扩展其他平台。

## 11. 测试策略

### 单元测试

- Hook JSON 兼容解析与未知字段处理。
- JSONL 增量 offset、截断、轮转和损坏行恢复。
- 累计 Token 转增量，跨 session 不重复计算。
- 时区、周起始、月边界和跨年边界。
- Context 百分比与阈值颜色。
- Renderer 超时、空输出、超限与 fallback。
- Adapter 检测优先级和安全转义。

### 集成测试

- 使用固定 transcript fixture 连续触发多次 Hook，验证幂等聚合。
- 在临时 tmux session 中验证只更新当前目标并可清理。
- 使用伪环境变量验证 WezTerm、Zellij、Kitty 与通用终端降级。
- 安装 marketplace 插件后启动新 Codex 会话，验证 Hooks 被发现。

### 手工验收

- 官方 Codex 不在 PATH 中时给出明确诊断。
- 官方 Codex 升级后无需重新编译插件即可工作。
- 宽、窄终端下信息按优先级缩减。
- `/compact` 前后 Context 正确更新。
- 删除缓存后可以从 session 日志重建 Today/Week/Month/Total。

## 12. 实施顺序

1. 建立 Rust workspace、CLI 骨架和 fixture 测试框架。
2. 实现 Hook 输入与 transcript 增量解析。
3. 实现日期聚合、状态存储和并发锁。
4. 实现统一 StatusSnapshot 与默认 Renderer。
5. 实现 tmux Adapter 与通用 OSC Adapter。
6. 创建 Codex plugin、hooks 与 repo marketplace。
7. 完成本地官方 Codex 安装验证。
8. 增加 WezTerm、Zellij、iTerm2/Kitty Adapter。
9. 增加跨平台构建与 GitHub Release 安装流程。

## 13. 验收标准

- 安装插件后仍运行官方 `codex` 二进制。
- 不要求 fork、PTY wrapper、额外 TUI 或 shell alias。
- 一次响应完成后，Context、Input/Output 与日期聚合能在目标终端状态区更新。
- 多次处理相同 transcript 不会重复累计 Token。
- Today/Week/Month/Total 按配置时区正确计算并使用约定名称展示。
- 未安装 tmux/特殊终端时安全降级，不破坏 Codex 屏幕。
- 插件失败、超时或卸载均不影响 Codex 正常工作。
