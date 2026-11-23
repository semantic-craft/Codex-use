# Codex-use

[English README](README.md)

**Codex-use** 是一个独立开源的 Agent Skill 与零依赖 Python 执行器（`skills/codex-use/scripts/drive.py`），供任意具备本地 Shell 能力的宿主 Agent 直接调用官方无头编程 CLI（Codex CLI 与 DeepSeek Harness）。

无需额外守护进程、代理服务或 MCP 控制面，即可落地隔离 Git Worktree、并发写锁、CLI 参数门禁与 JSONL 实跑命令审计。

## 解决什么问题

仅靠提示词约束无头编程 Worker，在实际工程中常出现四类重复故障：

- 传入非法推理档位（如 `ultra`），或向 `codex exec resume` 误传 `-s` / `-C` 导致调用直接失败。
- 在 linked git worktree 中以 `workspace-write` 运行 `dsh --profile headless` 时，未自动放行主仓 `.git` 元数据目录（`--add-dir <git-common-dir>`）导致 Git 操作报错。
- 多个写任务并发落入同一工作树，造成差分污染与文件覆盖。
- 仅凭 Worker 文本自述验收，漏掉未实际运行的测试、被吞掉的错误和多余抽象。

Codex-use 将这些规则直接编码进 `scripts/drive.py` 的结构与边界校验中，并配套精炼的 `SKILL.md` 执行协议。

## 架构（Host x Worker）

```text
宿主（Antigravity / Grok Bot / DSH / 任意 SKILL.md 宿主）
  -> skills/codex-use/SKILL.md          （Worker 无关的 scout / ship / review 协议）
  -> skills/codex-use/scripts/drive.py  （预检、工作树写锁、命令构造、JSONL 审计）
       ├─ CodexAdapter + workers/codex.md   （codex exec / resume / review）
       └─ DshAdapter   + workers/dsh.md     （dsh --profile headless）
```

- **自包含技能目录。** 运行所需的协议文档、Worker/宿主配置、`templates/` 任务模板、Schema 与脚本全部集中在 `skills/codex-use/` 内部，单独复制或软链接该目录即可完整工作。
- **零第三方依赖。** `scripts/drive.py` 仅使用 Python 3.10+ 标准库；当 `PATH` 中未安装 `codex` 时，自动回退检测 Codex Desktop 自带的独立二进制（`~/.codex/packages/standalone/current/bin/codex`）。
- **本地任务单自动排除、紧凑回执与有界返工。** 放在仓库 `.codex-use/` 下的任务单自动幂等写入 `<git_common_dir>/info/exclude`（不改 `.gitignore`）；`AuditSummary` 默认紧凑输出（命令预览截断至 160 字符，且 `receipt_valid` 为真时省略重复的 `final_message`，按需传 `--full-output` 恢复完整预览）；`worktree remove` 自动清理安全缓存目录（`.codex-use`、`__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.DS_Store`）并对真实未跟踪或已改动文件保持失败保护；`auto-fix` 同会话自修上限 2 轮。

## 安装方式

### 通用安装（`~/.agents/skills`）

将自包含的技能目录软链接到全局 Skills 目录，所有支持 `SKILL.md` 的宿主均可直接发现：

```bash
git clone https://github.com/semantic-craft/Codex-use.git Codex-use
mkdir -p ~/.agents/skills
ln -s "$(pwd)/Codex-use/skills/codex-use" ~/.agents/skills/codex-use
```

### Antigravity

仓库根目录自带 Antigravity 原生清单 `plugin.json`。将本仓库克隆至 Antigravity 插件目录，或将 `skills/codex-use` 软链接到 `~/.agents/skills/codex-use`。

### Grok Bot

仓库内置 `.grok-plugin/plugin.json`（声明 `"skills": "./skills"`，无 MCP 依赖）。作为本地插件加载后，请启用 **Execution on this computer**，确保 `drive.py` 在目标开发机本地访问 Git 仓库与 Worker CLI。

### DeepSeek Harness（DSH）

仓库内置 `package.json` 与 `cordis.patch.yml`，通过 `@deepseek-ai/dsh-skill-filesystem` 注册本技能。可作为 DSH Bundle 安装，或直接以 `--patch` 叠加验证：

```bash
dsh --profile headless --patch /path/to/Codex-use/cordis.patch.yml --dump-config
```

## 快速上手

### 1. 运行预检

检查 CLI 二进制、鉴权状态与 Git 仓库路径：

```bash
python3 skills/codex-use/scripts/drive.py preflight --worker codex --cwd /path/to/repo
python3 skills/codex-use/scripts/drive.py preflight --worker dsh --cwd /path/to/repo
```

### 2. 只读侦察（`scout`）

免建工作树，以只读权限完成代码摸排或故障定位：

```bash
python3 skills/codex-use/scripts/drive.py run \
  --worker codex \
  --mode scout \
  --effort medium \
  --cwd /path/to/repo \
  --prompt "定位请求重试的配置来源，列出精确文件路径与行号。"
```

### 3. 隔离实现（`ship`）

可基于 `skills/codex-use/templates/ship-task.md` 或 `bugfix-task.md`（只读侦察用 `scout-task.md`）撰写任务单，通过 `--preflight --worker <w>` 一步完成预检与独立 linked worktree 创建，带互斥写锁执行实现切片（`--worker codex` 支持重复传入 `--image <path>`），并通过 stdout 返回紧凑 `AuditSummary`（按需加 `--full-output` 输出完整预览与原始 `final_message`）：

```bash
python3 skills/codex-use/scripts/drive.py worktree create \
  --repo /path/to/repo --task fix-retry --preflight --worker codex

python3 skills/codex-use/scripts/drive.py run \
  --worker codex \
  --mode ship \
  --effort high \
  --cwd /path/to/.worktrees/repo/fix-retry \
  --base <base_sha> \
  --image /path/to/mock.png \
  --schema skills/codex-use/schemas/receipt.schema.json \
  --prompt-file /path/to/.worktrees/repo/fix-retry/.codex-use/slice-prompt.md
```

### 4. 同会话自修（`resume`，最多 2 轮）

本地差分审计或验证发现缺陷时，直接回到同一会话按精确 `file:line` 纠偏；连续 2 轮自动返工仍未通过时停止循环，将阻塞点与差分升级给用户处理：

```bash
python3 skills/codex-use/scripts/drive.py resume \
  --worker codex \
  --session <session_id> \
  --mode ship \
  --cwd /path/to/.worktrees/repo/fix-retry \
  --base <base_sha> \
  --prompt "修复 src/retry.py:42 的边界条件，并重新运行 python3 -m unittest。"
```

### 5. 非交互审查与离线审计

```bash
python3 skills/codex-use/scripts/drive.py review --worker codex --cwd /path/to/worktree --base main
python3 skills/codex-use/scripts/drive.py audit --worker codex --log /tmp/codex-use-logs/run.jsonl --cwd /path/to/worktree --base <base_sha>
```

合入主线后清理临时工作树：

```bash
python3 skills/codex-use/scripts/drive.py worktree remove --repo /path/to/repo --worktree /path/to/.worktrees/repo/fix-retry --delete-branch
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 开源协议

基于 [Apache License 2.0](LICENSE) 发布。
