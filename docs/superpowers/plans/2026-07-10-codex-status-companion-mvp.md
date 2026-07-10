# Codex Status Companion MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a marketplace-installable companion that uses official Codex Hooks to incrementally calculate Context and Token usage and render it through tmux pane titles or the generic terminal title without modifying or wrapping the official Codex CLI.

**Architecture:** A single Rust binary consumes Hook JSON on stdin, incrementally parses the Hook-provided Codex transcript, updates an atomically persisted usage store, builds a versioned status snapshot, renders it with either the default formatter or a bounded user command, and sends the result to an auto-detected terminal adapter. A repo marketplace plugin contributes lifecycle hooks and a setup skill; the plugin never replaces the `codex` executable.

**Tech Stack:** Rust 2024 edition, Rust 1.85+, `serde`, `serde_json`, `chrono`, `chrono-tz`, `toml`, `fs2`, `tempfile`, `wait-timeout`, standard `std::process::Command`, Codex plugin manifest and hooks JSON.

## Global Constraints

- Run the official `codex` binary unchanged; do not create a Codex fork, alternate TUI, PTY wrapper, or shell alias.
- Display Today/Week/Month/Total exactly with those names and no `Local` prefix.
- Derive date aggregates from locally available Codex session JSONL and never present missing values as zero.
- Default timezone is the system timezone; allow an explicit IANA timezone such as `Asia/Shanghai`.
- Week begins on Monday by default.
- A Hook invocation must fail open and target a 300 ms total budget; failures must not interrupt Codex.
- The MVP supports macOS/Linux, tmux pane titles when already visible, and generic OSC terminal titles.
- Do not modify global tmux or terminal configuration.
- Do not upload data, listen on a network port, or inspect transcript message bodies.

---

## Planned File Structure

```text
Cargo.toml                                      # Rust workspace and shared dependency versions
crates/codex-status/Cargo.toml                  # CLI crate manifest
crates/codex-status/src/main.rs                 # stdin/CLI boundary and fail-open exit behavior
crates/codex-status/src/lib.rs                  # public module surface used by integration tests
crates/codex-status/src/hook.rs                 # tolerant Codex Hook input schema
crates/codex-status/src/transcript.rs           # incremental JSONL token event parser
crates/codex-status/src/state.rs                # cursor/date buckets, locking, atomic persistence
crates/codex-status/src/config.rs               # config defaults and TOML loading
crates/codex-status/src/snapshot.rs             # versioned renderer input contract
crates/codex-status/src/render.rs               # default and custom-command renderers
crates/codex-status/src/terminal/mod.rs          # adapter detection and common interface
crates/codex-status/src/terminal/tmux.rs         # non-global tmux pane-title adapter
crates/codex-status/src/terminal/osc.rs          # sanitized OSC title adapter
crates/codex-status/src/run.rs                   # Hook orchestration
crates/codex-status/tests/hook_pipeline.rs       # end-to-end fixture-driven Hook tests
crates/codex-status/tests/fixtures/session.jsonl # representative Codex token_count stream
plugins/codex-status/.codex-plugin/plugin.json  # plugin metadata
plugins/codex-status/hooks/hooks.json            # lifecycle Hook declarations
plugins/codex-status/scripts/run-hook.sh          # PATH-safe fail-open launcher
plugins/codex-status/skills/codex-status/SKILL.md # setup and diagnostic workflow
.agents/plugins/marketplace.json                 # repository marketplace catalog
scripts/validate_plugin.py                       # repository-local CI manifest validator
README.md                                        # install, configuration, support matrix
```

---

### Task 1: Rust workspace and Hook contract

**Files:**
- Create: `Cargo.toml`
- Create: `crates/codex-status/Cargo.toml`
- Create: `crates/codex-status/src/lib.rs`
- Create: `crates/codex-status/src/main.rs`
- Create: `crates/codex-status/src/hook.rs`
- Test: `crates/codex-status/src/hook.rs`

**Interfaces:**
- Produces: `hook::HookInput` with `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, and optional event-specific fields.
- Produces: `hook::read_hook_input<R: Read>(reader: R) -> Result<HookInput, HookInputError>`.
- Later tasks consume `HookInput` without depending on raw `serde_json::Value`.

- [ ] **Step 1: Create the workspace manifests and write the failing Hook parsing tests**

Workspace manifest:

```toml
[workspace]
members = ["crates/codex-status"]
resolver = "2"

[workspace.package]
edition = "2024"
rust-version = "1.85"
license = "MIT"

[workspace.dependencies]
chrono = { version = "0.4", features = ["serde"] }
chrono-tz = "0.10"
fs2 = "0.4"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tempfile = "3"
thiserror = "2"
toml = "0.9"
wait-timeout = "0.2"
```

Crate manifest:

```toml
[package]
name = "codex-status"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true
license.workspace = true

[dependencies]
chrono.workspace = true
chrono-tz.workspace = true
fs2.workspace = true
serde.workspace = true
serde_json.workspace = true
tempfile.workspace = true
thiserror.workspace = true
toml.workspace = true
wait-timeout.workspace = true
```

```rust
#[test]
fn parses_session_start_and_ignores_new_fields() {
    let input = br#"{
      "session_id":"abc",
      "transcript_path":"/tmp/session.jsonl",
      "cwd":"/repo",
      "hook_event_name":"SessionStart",
      "model":"gpt-5.4",
      "permission_mode":"default",
      "source":"startup",
      "future_field":{"safe":true}
    }"#;
    let parsed = read_hook_input(&input[..]).unwrap();
    assert_eq!(parsed.session_id, "abc");
    assert_eq!(parsed.hook_event_name, "SessionStart");
    assert_eq!(parsed.transcript_path.unwrap(), PathBuf::from("/tmp/session.jsonl"));
}

#[test]
fn rejects_missing_session_id() {
    let error = read_hook_input(br#"{"cwd":"/repo","hook_event_name":"Stop","model":"gpt-5.4"}"#.as_slice())
        .unwrap_err();
    assert!(error.to_string().contains("session_id"));
}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `cargo test -p codex-status hook::tests -- --nocapture`

Expected: compilation fails because `read_hook_input` and `HookInput` do not exist.

- [ ] **Step 3: Implement the minimal tolerant Hook schema and fail-open CLI shell**

```rust
#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct HookInput {
    pub session_id: String,
    pub transcript_path: Option<PathBuf>,
    pub cwd: PathBuf,
    pub hook_event_name: String,
    pub model: String,
    #[serde(default)]
    pub permission_mode: Option<String>,
}

pub fn read_hook_input<R: Read>(reader: R) -> Result<HookInput, HookInputError> {
    Ok(serde_json::from_reader(reader)?)
}
```

`main` must return exit code 0 after writing a short diagnostic to stderr when Hook processing fails; normal failures must never block Codex.

- [ ] **Step 4: Run tests and formatting**

Run: `cargo fmt --all -- --check && cargo test -p codex-status hook::tests -- --nocapture`

Expected: formatting succeeds and both Hook tests pass.

- [ ] **Step 5: Commit the Hook contract**

```bash
git add Cargo.toml crates/codex-status
git commit -m "feat: add Codex hook input contract"
```

---

### Task 2: Incremental transcript token parser

**Files:**
- Create: `crates/codex-status/src/transcript.rs`
- Modify: `crates/codex-status/src/lib.rs`
- Create: `crates/codex-status/tests/fixtures/session.jsonl`

**Interfaces:**
- Consumes: an absolute transcript path plus `TranscriptCursor`.
- Produces: `TranscriptBatch { cursor: TranscriptCursor, events: Vec<TokenEvent> }`.
- Produces: `read_new_token_events(path: &Path, cursor: &TranscriptCursor) -> io::Result<TranscriptBatch>`.
- `TokenEvent` contains timestamp, cumulative input/output/total tokens, optional context window, and optional five-hour/weekly rate-limit values.

- [ ] **Step 1: Write failing tests for incremental, corrupt-line, and truncation behavior**

```rust
#[test]
fn reads_only_new_token_events() {
    let first = read_new_token_events(path, &TranscriptCursor::default()).unwrap();
    assert_eq!(first.events.len(), 2);
    let second = read_new_token_events(path, &first.cursor).unwrap();
    assert!(second.events.is_empty());
}

#[test]
fn skips_corrupt_and_non_token_lines() {
    let batch = parse_bytes(b"not-json\n{\"payload\":{\"type\":\"message\"}}\n").unwrap();
    assert!(batch.events.is_empty());
}

#[test]
fn resets_cursor_when_file_is_truncated() {
    let stale = TranscriptCursor { offset: 9999, ..Default::default() };
    let batch = read_new_token_events(path, &stale).unwrap();
    assert!(!batch.events.is_empty());
}
```

- [ ] **Step 2: Run the parser tests and verify they fail**

Run: `cargo test -p codex-status transcript::tests -- --nocapture`

Expected: compilation fails because `transcript` types and functions are absent.

- [ ] **Step 3: Implement seek-based JSONL parsing with tolerant field extraction**

Use `SeekFrom::Start(cursor.offset)`, reset to zero when file length is less than the offset, parse each line as `serde_json::Value`, and accept only `payload.type == "token_count"`. Update the returned offset only through the last complete newline so a partially written line can be retried.

```rust
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct TranscriptCursor {
    pub offset: u64,
    pub previous_input: i64,
    pub previous_output: i64,
    pub previous_total: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TokenEvent {
    pub timestamp: DateTime<FixedOffset>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
    pub context_window: Option<i64>,
    pub five_hour_used_percent: Option<f64>,
    pub weekly_used_percent: Option<f64>,
}
```

- [ ] **Step 4: Run parser tests and full crate tests**

Run: `cargo test -p codex-status transcript::tests -- --nocapture && cargo test -p codex-status`

Expected: all parser and Hook tests pass.

- [ ] **Step 5: Commit the parser**

```bash
git add crates/codex-status/src crates/codex-status/tests/fixtures
git commit -m "feat: parse Codex token events incrementally"
```

---

### Task 3: Atomic usage store and date aggregation

**Files:**
- Create: `crates/codex-status/src/state.rs`
- Create: `crates/codex-status/src/config.rs`
- Modify: `crates/codex-status/src/lib.rs`
- Test: `crates/codex-status/src/state.rs`
- Test: `crates/codex-status/src/config.rs`

**Interfaces:**
- Consumes: `TranscriptBatch`, `session_id`, configured timezone, and week start.
- Produces: `UsageState::apply_batch(transcript_key, batch, timezone) -> LatestUsage`.
- Produces: `UsageSummary { today, week, month, total }`.
- Produces: `StateRepository::with_lock(path, timeout, operation)` for atomic load/update/save.

- [ ] **Step 1: Write failing aggregation and idempotency tests**

```rust
#[test]
fn aggregates_across_east8_day_week_and_month_boundaries() {
    let mut state = UsageState::default();
    state.apply_events("a", events_at_month_boundary(), "Asia/Shanghai".parse().unwrap());
    let summary = state.summary(date(2026, 7, 2), WeekStart::Monday);
    assert_eq!(summary.today, Some(3_500));
    assert_eq!(summary.week, Some(4_500));
    assert_eq!(summary.month, Some(4_500));
    assert_eq!(summary.total, Some(4_500));
}

#[test]
fn replaying_the_same_cursor_does_not_double_count() {
    let mut state = UsageState::default();
    state.apply_batch("a", batch.clone(), tz());
    state.apply_batch("a", batch, tz());
    assert_eq!(state.summary(today(), WeekStart::Monday).total, Some(4_500));
}

#[test]
fn missing_history_is_none_not_zero() {
    assert_eq!(UsageState::default().summary(today(), WeekStart::Monday).today, None);
}
```

- [ ] **Step 2: Run state tests and verify they fail**

Run: `cargo test -p codex-status state::tests config::tests -- --nocapture`

Expected: compilation fails because state/config types are absent.

- [ ] **Step 3: Implement configuration defaults, cumulative-to-delta conversion, and date buckets**

Persist schema version `1`, transcript cursors keyed by canonical path, and `BTreeMap<NaiveDate, DayUsage>`. Treat a cumulative counter decrease as transcript reset and use the new cumulative value as the delta. Clamp negative counters to zero.

```rust
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct DayUsage {
    pub input: i64,
    pub output: i64,
    pub total: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UsageSummary {
    pub today: Option<i64>,
    pub week: Option<i64>,
    pub month: Option<i64>,
    pub total: Option<i64>,
}
```

- [ ] **Step 4: Implement lock timeout and atomic persistence tests**

Write a test that persists state, reloads it, and confirms the original file stays valid when serialization to a temporary target fails. Use `fs2::FileExt::try_lock_exclusive` in a retry loop capped by the configured timeout and `tempfile::NamedTempFile::persist` for replacement.

- [ ] **Step 5: Run state tests and full crate tests**

Run: `cargo test -p codex-status state::tests config::tests -- --nocapture && cargo test -p codex-status`

Expected: all tests pass, including idempotency and atomic reload.

- [ ] **Step 6: Commit state management**

```bash
git add crates/codex-status/src
git commit -m "feat: aggregate and persist Codex usage"
```

---

### Task 4: Versioned snapshot and bounded rendering

**Files:**
- Create: `crates/codex-status/src/snapshot.rs`
- Create: `crates/codex-status/src/render.rs`
- Modify: `crates/codex-status/src/lib.rs`
- Test: `crates/codex-status/src/render.rs`

**Interfaces:**
- Consumes: Hook metadata, latest token event, `UsageSummary`, Git branch, adapter name, and width.
- Produces: `StatusSnapshot` serialized with `schema_version: 1`.
- Produces: `render_default(&StatusSnapshot, &DisplayConfig) -> String`.
- Produces: `render_with_fallback(snapshot, config) -> RenderOutcome`.

- [ ] **Step 1: Write failing snapshot and default-renderer tests**

```rust
#[test]
fn serializes_missing_values_as_null() {
    let value = serde_json::to_value(StatusSnapshot::minimal("s", "gpt-5.4")).unwrap();
    assert!(value["context"]["used_percentage"].is_null());
    assert!(value["tokens"]["today"].is_null());
}

#[test]
fn default_renderer_uses_required_labels() {
    let text = render_default(&populated_snapshot(), &DisplayConfig::default());
    assert!(text.contains("Today"));
    assert!(text.contains("Week"));
    assert!(text.contains("Month"));
    assert!(text.contains("Total"));
    assert!(!text.contains("Local"));
}

#[test]
fn narrow_output_keeps_context_before_optional_totals() {
    let text = render_default(&populated_snapshot().with_columns(48), &DisplayConfig::default());
    assert!(text.contains("Ctx"));
    assert!(!text.contains("Month"));
}
```

- [ ] **Step 2: Run renderer tests and verify they fail**

Run: `cargo test -p codex-status render::tests snapshot::tests -- --nocapture`

Expected: compilation fails because snapshot and renderer modules are absent.

- [ ] **Step 3: Implement the versioned snapshot and width-priority default renderer**

Format token values with `K`, `M`, and `B` suffixes, use `—` for missing values, and assemble segments in priority order. Never include raw transcript text in the snapshot.

- [ ] **Step 4: Implement custom command timeout, size limits, and fallback tests**

Use `wait_timeout::ChildExt` with JSON written to child stdin. Default timeout is 200 ms; cap configured timeout at 1,000 ms; cap output at 8 KiB and 3 lines. Tests must cover successful custom text, non-zero exit, timeout, empty stdout, and oversized stdout, with all failure cases returning the default renderer.

- [ ] **Step 5: Run renderer tests and full crate tests**

Run: `cargo test -p codex-status render::tests snapshot::tests -- --nocapture && cargo test -p codex-status`

Expected: all tests pass without leaked child processes.

- [ ] **Step 6: Commit rendering**

```bash
git add crates/codex-status/src
git commit -m "feat: render customizable Codex status"
```

---

### Task 5: tmux and OSC terminal adapters

**Files:**
- Create: `crates/codex-status/src/terminal/mod.rs`
- Create: `crates/codex-status/src/terminal/tmux.rs`
- Create: `crates/codex-status/src/terminal/osc.rs`
- Modify: `crates/codex-status/src/lib.rs`
- Test: `crates/codex-status/src/terminal/mod.rs`

**Interfaces:**
- Produces: `TerminalTarget::detect(env, probe) -> TerminalTarget`.
- Produces: `TerminalTarget::columns() -> Option<u16>`.
- Produces: `TerminalTarget::display(text: &str) -> Result<(), AdapterError>`.
- tmux uses `select-pane -T` only when `pane-border-status` is already enabled; otherwise detection falls back to OSC.

- [ ] **Step 1: Write failing adapter detection and sanitization tests**

```rust
#[test]
fn chooses_tmux_only_when_pane_title_is_visible() {
    let target = detect_with(FakeProbe::tmux("%3", "top"));
    assert_eq!(target, TerminalTarget::TmuxPane { pane: "%3".into() });
}

#[test]
fn falls_back_to_osc_when_tmux_border_is_off() {
    let target = detect_with(FakeProbe::tmux("%3", "off"));
    assert_eq!(target, TerminalTarget::OscTitle);
}

#[test]
fn strips_terminal_control_characters() {
    assert_eq!(sanitize("ok\u{1b}]0;bad\u{7}"), "ok]0;bad");
}
```

- [ ] **Step 2: Run terminal tests and verify they fail**

Run: `cargo test -p codex-status terminal::tests -- --nocapture`

Expected: compilation fails because terminal adapter types are absent.

- [ ] **Step 3: Implement injectable environment/command probes and tmux pane titles**

Probe `TMUX` and `TMUX_PANE`, execute `tmux show-options -gv pane-border-status`, and update only the current pane with `tmux select-pane -t <pane> -T <sanitized text>`. Apply a 100 ms command timeout. Never run `set-option` or change pane-border configuration.

- [ ] **Step 4: Implement generic OSC title output**

Open `/dev/tty` for writing and emit `ESC ] 0 ; <sanitized text> BEL`. If `/dev/tty` is unavailable, return a non-fatal adapter error. Strip C0/C1 controls, cap title length at 512 bytes, and preserve valid UTF-8 boundaries.

- [ ] **Step 5: Run terminal tests and full crate tests**

Run: `cargo test -p codex-status terminal::tests -- --nocapture && cargo test -p codex-status`

Expected: all tests pass and no test changes real terminal state.

- [ ] **Step 6: Commit terminal adapters**

```bash
git add crates/codex-status/src/terminal crates/codex-status/src/lib.rs
git commit -m "feat: add terminal status adapters"
```

---

### Task 6: End-to-end Hook pipeline

**Files:**
- Create: `crates/codex-status/src/run.rs`
- Modify: `crates/codex-status/src/main.rs`
- Modify: `crates/codex-status/src/lib.rs`
- Create: `crates/codex-status/tests/hook_pipeline.rs`
- Test: `crates/codex-status/tests/hook_pipeline.rs`

**Interfaces:**
- Consumes all prior modules through `run::run_hook(input, dependencies) -> Result<RunOutcome>`.
- Produces CLI command `codex-status hook` reading one Hook object from stdin.
- Produces CLI command `codex-status render --snapshot <path>` for renderer diagnostics.

- [ ] **Step 1: Write a failing fixture-driven pipeline test**

```rust
#[test]
fn hook_pipeline_is_incremental_and_updates_display_once() {
    let env = TestEnvironment::new_with_fixture("session.jsonl");
    env.run_hook(session_start_json()).unwrap();
    let first = env.recorded_displays();
    assert_eq!(first.len(), 1);
    assert!(first[0].contains("Today"));

    env.run_hook(stop_json()).unwrap();
    assert_eq!(env.state().summary.total, Some(4_500));
    env.run_hook(stop_json()).unwrap();
    assert_eq!(env.state().summary.total, Some(4_500));
}
```

- [ ] **Step 2: Run the integration test and verify it fails**

Run: `cargo test -p codex-status --test hook_pipeline -- --nocapture`

Expected: compilation fails because `run_hook` and dependency injection are absent.

- [ ] **Step 3: Implement orchestration with dependency injection**

The order is: parse Hook → load config → acquire short lock → read only new transcript bytes → update state → release persisted state → resolve Git branch with timeout → build snapshot → render → display only when text changed. A missing transcript on `SessionStart` produces a metadata-only snapshot instead of an error.

- [ ] **Step 4: Implement fail-open CLI behavior and diagnostic logging**

`codex-status hook` must return zero for parse, file, lock, renderer, or adapter failures after writing one concise stderr line. `codex-status doctor` may return non-zero and must report config, cache path, detected adapter, transcript access, and executable version.

- [ ] **Step 5: Run all tests, clippy, and formatting**

Run: `cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`

Expected: all checks pass.

- [ ] **Step 6: Commit the executable pipeline**

```bash
git add crates/codex-status
git commit -m "feat: run status updates from Codex hooks"
```

---

### Task 7: Marketplace plugin and setup workflow

**Files:**
- Create: `plugins/codex-status/.codex-plugin/plugin.json`
- Create: `plugins/codex-status/hooks/hooks.json`
- Create: `plugins/codex-status/scripts/run-hook.sh`
- Create: `plugins/codex-status/skills/codex-status/SKILL.md`
- Create: `.agents/plugins/marketplace.json`
- Create: `README.md`

**Interfaces:**
- Plugin name: `codex-status`.
- Marketplace name: `codex-status-companion`.
- Hook launcher consumes Codex stdin unchanged and executes `codex-status hook` from PATH.
- Setup skill builds/installs the binary, runs `codex-status doctor`, and explains adapter support.

- [ ] **Step 1: Write the plugin manifest and Hook declarations**

Plugin manifest:

```json
{
  "name": "codex-status",
  "version": "0.1.0",
  "description": "Display Codex context and token usage in terminal-native status areas.",
  "author": {
    "name": "WYJ288173",
    "url": "https://github.com/WYJ288173"
  },
  "repository": "https://github.com/WYJ288173/codex-status-companion",
  "license": "MIT",
  "keywords": ["codex", "statusline", "tokens", "context"],
  "skills": "./skills/",
  "interface": {
    "displayName": "Codex Status Companion",
    "shortDescription": "Context and token status for the official Codex CLI",
    "longDescription": "Shows Context, Input, Output, Today, Week, Month, Total, and rate limits through terminal-native status adapters without replacing the official Codex CLI.",
    "developerName": "WYJ288173",
    "category": "Developer Tools",
    "capabilities": ["Read", "Hooks"]
  }
}
```

`hooks/hooks.json` must register synchronous, fail-open commands for `SessionStart`, `Stop`, `PostToolUse`, `PreCompact`, and `PostCompact`, each invoking:

```json
{
  "type": "command",
  "command": "\"${PLUGIN_ROOT}/scripts/run-hook.sh\"",
  "timeout": 1,
  "async": false
}
```

The launcher must preserve stdin:

```sh
#!/bin/sh
if command -v codex-status >/dev/null 2>&1; then
  exec codex-status hook
fi
exit 0
```

- [ ] **Step 2: Add repo marketplace metadata and setup skill**

The marketplace entry uses `./plugins/codex-status`, `AVAILABLE`, `ON_INSTALL`, and category `Developer Tools`. The setup skill must direct Codex to run `cargo install --path <repo>/crates/codex-status --locked` during local development, then `codex-status doctor`; it must not replace the official `codex` command.

```json
{
  "name": "codex-status-companion",
  "interface": {
    "displayName": "Codex Status Companion"
  },
  "plugins": [
    {
      "name": "codex-status",
      "source": {
        "source": "local",
        "path": "./plugins/codex-status"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Developer Tools"
    }
  ]
}
```

- [ ] **Step 3: Validate the plugin and JSON files**

Run:

```bash
python3 /Users/huayang/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-status
jq empty plugins/codex-status/hooks/hooks.json .agents/plugins/marketplace.json
```

Expected: plugin validation succeeds and both JSON files parse.

- [ ] **Step 4: Install the local marketplace and plugin for smoke testing**

Run:

```bash
codex plugin marketplace add /Users/huayang/developer/LocalAgent
codex plugin add codex-status@codex-status-companion
codex plugin list
```

Expected: `codex-status@codex-status-companion` appears installed and enabled.

- [ ] **Step 5: Run an official Codex smoke test**

Start a new official Codex thread in tmux with pane-border-status already enabled, submit one prompt, and verify `codex-status doctor` identifies tmux and the pane title includes Context plus Today. Repeat outside tmux and verify the terminal title changes without corrupting the TUI.

- [ ] **Step 6: Commit the distributable plugin**

```bash
git add .agents plugins README.md
git commit -m "feat: package Codex status companion plugin"
```

---

### Task 8: MVP verification and release readiness

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/validate_plugin.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-10-codex-status-companion-design.md`

**Interfaces:**
- CI runs formatting, clippy, tests, and plugin validation.
- README documents exact official-Codex installation and uninstall paths.

- [ ] **Step 1: Add CI with pinned Rust stable behavior**

The workflow runs on `ubuntu-latest` and `macos-latest`:

```yaml
- run: cargo fmt --all -- --check
- run: cargo clippy --workspace --all-targets -- -D warnings
- run: cargo test --workspace
- run: python3 scripts/validate_plugin.py plugins/codex-status
```

Implement `scripts/validate_plugin.py` with Python standard-library JSON parsing. It must verify the required manifest fields, exact folder/name equality, strict `x.y.z` version format, referenced skills/hooks paths, marketplace source path, and required installation/authentication/category policy fields so CI does not depend on the developer's home directory.

- [ ] **Step 2: Add install, configuration, diagnostics, and uninstall documentation**

README must state that Today/Week/Month/Total are derived from accessible Codex session logs, while preserving the requested display labels. Document tmux pane-border prerequisite, OSC fallback, `codex-status doctor`, cache/config paths, plugin removal, and binary removal.

- [ ] **Step 3: Run the complete release verification**

Run:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
python3 /Users/huayang/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-status
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 4: Commit release readiness**

```bash
git add .github README.md docs/superpowers/specs
git commit -m "ci: verify Codex status companion"
```

- [ ] **Step 5: Tag only after the official Codex smoke test passes**

Run: `git tag -a v0.1.0 -m "Codex Status Companion v0.1.0"`

Expected: annotated local tag exists; pushing the tag is a separate explicit release action.
