#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import dataclasses
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Literal, Protocol

WorkerId = Literal["codex", "dsh"]
RunMode = Literal["scout", "ship", "review"]
WriteMode = Literal["scout", "ship"]
CodexEffort = Literal["medium", "high", "xhigh"]
DshPermissionMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["ask", "never"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_LOCK_CONFLICT = 3

CODEX_ALLOWED_EFFORTS: frozenset[str] = frozenset({"medium", "high", "xhigh"})
DSH_PERMISSION_MODES: frozenset[str] = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)
DSH_APPROVAL_POLICIES: frozenset[str] = frozenset({"ask", "never"})
RECEIPT_REQUIRED_KEYS: tuple[str, ...] = (
    "changed_files",
    "verification",
    "assumptions_and_risks",
    "symbol_changes",
)
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "shell",
        "bash",
        "sh",
        "zsh",
        "exec",
        "exec_command",
        "run_command",
        "command",
        "terminal",
        "execute_command",
    }
)
FILE_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "apply_patch",
        "str_replace",
        "patch_file",
    }
)
OUTPUT_PREVIEW_LIMIT = 160
FULL_OUTPUT_PREVIEW_LIMIT = 400
SAFE_DISPOSABLE_NAMES: frozenset[str] = frozenset(
    {
        ".codex-use",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
    }
)


class ValidationError(Exception):
    pass


class LockConflictError(Exception):
    def __init__(self, message: str, lock_file: Path) -> None:
        super().__init__(message)
        self.lock_file = lock_file


class DriverRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class CommandRecord:
    command: str
    exit_code: int | None
    status: str
    output_preview: str


@dataclass(frozen=True)
class ParsedEvents:
    session_id: str | None
    commands: list[CommandRecord]
    changed_files: list[str]
    errors: list[str]
    final_message: str
    usage: dict[str, Any] | None
    event_count: int


@dataclass(frozen=True)
class PreflightResult:
    worker: WorkerId
    ok: bool
    binary_path: str | None
    version: str | None
    cwd: str
    is_git_repo: bool
    git_root: str | None
    git_common_dir: str | None
    is_linked_worktree: bool
    auth_hint: str | None
    issues: list[str]


@dataclass(frozen=True)
class AuditSummary:
    worker: WorkerId
    mode: RunMode
    session_id: str | None
    exit_code: int
    commands: list[CommandRecord]
    changed_files: list[str]
    git_diff_files: list[str]
    errors: list[str]
    final_message: str | None
    receipt: dict[str, Any] | None
    receipt_valid: bool | None
    usage: dict[str, Any] | None
    raw_log: str
    command: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunRequest:
    worker: WorkerId
    mode: WriteMode
    cwd: Path
    effort: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    approval_policy: str | None = None
    allow_danger: bool = False
    schema_path: Path | None = None
    output_last_message: Path | None = None
    add_dirs: tuple[Path, ...] = ()
    images: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ResumeRequest:
    worker: WorkerId
    session_id: str
    mode: WriteMode
    cwd: Path
    effort: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    approval_policy: str | None = None
    allow_danger: bool = False
    schema_path: Path | None = None
    output_last_message: Path | None = None
    add_dirs: tuple[Path, ...] = ()
    images: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ReviewRequest:
    worker: WorkerId
    cwd: Path
    base: str | None = None
    uncommitted: bool = False
    commit: str | None = None
    model: str | None = None
    has_prompt: bool = False
    output_last_message: Path | None = None


@dataclass(frozen=True)
class GitContext:
    is_git_repo: bool
    git_root: Path | None
    git_dir: Path | None
    git_common_dir: Path | None
    is_linked_worktree: bool


class WorkerAdapter(Protocol):
    name: WorkerId

    def preflight(self, cwd: Path) -> PreflightResult: ...

    def build_run_cmd(self, req: RunRequest) -> list[str]: ...

    def build_resume_cmd(self, req: ResumeRequest) -> list[str]: ...

    def build_review_cmd(self, req: ReviewRequest) -> list[str] | None: ...

    def parse_jsonl(
        self, lines: Iterable[str], preview_limit: int = OUTPUT_PREVIEW_LIMIT
    ) -> ParsedEvents: ...


def _preview(text: Any, limit: int = OUTPUT_PREVIEW_LIMIT) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _run_git(cwd: Path, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def inspect_git_context(cwd: Path) -> GitContext:
    if not cwd.exists():
        return GitContext(False, None, None, None, False)
    top = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return GitContext(False, None, None, None, False)
    git_root = Path(top.stdout.strip()).resolve()
    gd_res = _run_git(cwd, ["rev-parse", "--git-dir"])
    gcd_res = _run_git(cwd, ["rev-parse", "--git-common-dir"])

    def _resolve_git_path(raw: str) -> Path:
        p = Path(raw.strip())
        if not p.is_absolute():
            p = cwd / p
        return p.resolve()

    git_dir = _resolve_git_path(gd_res.stdout) if gd_res.returncode == 0 else None
    git_common_dir = _resolve_git_path(gcd_res.stdout) if gcd_res.returncode == 0 else None
    is_linked = bool(git_dir and git_common_dir and git_dir != git_common_dir)
    return GitContext(True, git_root, git_dir, git_common_dir, is_linked)


def resolve_worktree_git_common_dir(cwd: Path) -> Path | None:
    ctx = inspect_git_context(cwd)
    if not ctx.is_git_repo or ctx.git_common_dir is None:
        return None
    resolved_cwd = cwd.resolve()
    if ctx.git_common_dir == resolved_cwd or ctx.git_common_dir.is_relative_to(resolved_cwd):
        return None
    return ctx.git_common_dir


def ensure_local_git_exclude(cwd: Path, entry: str = ".codex-use/") -> bool:
    ctx = inspect_git_context(cwd)
    if not ctx.is_git_repo or ctx.git_common_dir is None:
        return False
    normalized = entry.strip()
    if not normalized:
        return False
    exclude_path = ctx.git_common_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = (
        exclude_path.read_text(encoding="utf-8", errors="replace")
        if exclude_path.is_file()
        else ""
    )
    existing_lines = {line.strip() for line in existing_text.splitlines()}
    bare = normalized.rstrip("/")
    if normalized in existing_lines or (bare and bare in existing_lines):
        return False
    prefix = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
    with exclude_path.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{normalized}\n")
    return True


def _maybe_exclude_local_prompt_dir(cwd: Path, prompt_file: str | None) -> bool:
    if not prompt_file or prompt_file == "-":
        return False
    ctx = inspect_git_context(cwd)
    if not ctx.is_git_repo or ctx.git_root is None:
        return False
    prompt_path = Path(prompt_file).resolve()
    local_dir = (ctx.git_root / ".codex-use").resolve()
    if prompt_path.is_relative_to(local_dir):
        return ensure_local_git_exclude(cwd)
    return False


def resolve_ship_lock_path(cwd: Path) -> Path:
    resolved = cwd.resolve()
    ctx = inspect_git_context(resolved)
    if ctx.is_git_repo and ctx.git_dir is not None and os.access(ctx.git_dir, os.W_OK):
        return ctx.git_dir / "codex-use-ship.lock"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"codex-use-ship-{digest}.lock"


@contextlib.contextmanager
def acquire_ship_lock(cwd: Path, mode: str) -> Iterator[Path]:
    lock_path = resolve_ship_lock_path(cwd)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LockConflictError(
                f"Another ship invocation already holds the lock for {cwd.resolve()} ({lock_path}).",
                lock_path,
            ) from exc
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "cwd": str(cwd.resolve()),
                "mode": mode,
                "acquired_at": int(time.time()),
            }
        )
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _probe_binary_version(binary_path: str, args: list[str]) -> str | None:
    try:
        res = subprocess.run(
            [binary_path, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (res.stdout or res.stderr).strip()
    return output.splitlines()[0].strip() if output else None


def _extract_agent_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_text = block.get("text")
                if isinstance(block_text, str):
                    parts.append(block_text)
        return "\n".join(parts)
    return ""


def _extract_file_change_paths(item: dict[str, Any], target: list[str]) -> None:
    single = item.get("path")
    if isinstance(single, str):
        _append_unique(target, single)
    changes = item.get("changes")
    if isinstance(changes, list):
        for entry in changes:
            if isinstance(entry, str):
                _append_unique(target, entry)
            elif isinstance(entry, dict):
                path = entry.get("path") or entry.get("file") or entry.get("file_path")
                if isinstance(path, str):
                    _append_unique(target, path)


def _extract_error_text(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = payload.get("message") or payload.get("detail") or payload.get("text")
        if isinstance(code, str) and isinstance(msg, str):
            return f"{code}: {msg}"
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _extract_command_from_tool_args(args: Any) -> str | None:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return args.strip() or None
        args = parsed
    if isinstance(args, dict):
        for key in ("command", "cmd", "commandLine", "CommandLine", "script"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                return " ".join(value)
    return None


def _extract_path_from_tool_args(args: Any) -> str | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if isinstance(args, dict):
        for key in ("path", "file_path", "filePath", "target_file", "Path"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_receipt(final_message: str) -> tuple[dict[str, Any] | None, bool | None]:
    stripped = final_message.strip()
    if not stripped:
        return None, None
    candidate: Any = None
    try:
        candidate = json.loads(stripped)
    except json.JSONDecodeError:
        match = JSON_FENCE_PATTERN.search(stripped)
        if match:
            try:
                candidate = json.loads(match.group(1))
            except json.JSONDecodeError:
                candidate = None
    if not isinstance(candidate, dict):
        return None, None
    valid = _validate_receipt_shape(candidate)
    return candidate, valid


def _validate_receipt_shape(data: dict[str, Any]) -> bool:
    for key in RECEIPT_REQUIRED_KEYS:
        if key not in data:
            return False
    if not isinstance(data["changed_files"], list) or not all(
        isinstance(x, str) for x in data["changed_files"]
    ):
        return False
    if not isinstance(data["assumptions_and_risks"], list) or not all(
        isinstance(x, str) for x in data["assumptions_and_risks"]
    ):
        return False
    if not isinstance(data["symbol_changes"], list) or not all(
        isinstance(x, str) for x in data["symbol_changes"]
    ):
        return False
    verification = data["verification"]
    if not isinstance(verification, list):
        return False
    for entry in verification:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("command"), str):
            return False
        if not isinstance(entry.get("exit_code"), int) or isinstance(entry.get("exit_code"), bool):
            return False
        if not isinstance(entry.get("summary"), str):
            return False
    return True


def _validate_codex_effort(effort: str) -> CodexEffort:
    if effort not in CODEX_ALLOWED_EFFORTS:
        raise ValidationError(
            f"Unsupported Codex reasoning effort {effort!r}. "
            f"Allowed values: {', '.join(sorted(CODEX_ALLOWED_EFFORTS))}. "
            "'ultra' is explicitly forbidden."
        )
    return effort  # type: ignore[return-value]


def _resolve_codex_binary(standalone_path: Path | None = None) -> tuple[str | None, str]:
    which_path = shutil.which("codex")
    if which_path:
        return which_path, "path"
    candidate = (
        standalone_path
        if standalone_path is not None
        else Path.home() / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex"
    )
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate), "standalone"
    return None, "missing"


def _codex_command_binary() -> str:
    binary, source = _resolve_codex_binary()
    if source == "standalone" and binary is not None:
        return binary
    return "codex"


class CodexAdapter:
    name: WorkerId = "codex"

    def preflight(self, cwd: Path) -> PreflightResult:
        issues: list[str] = []
        binary, _ = _resolve_codex_binary()
        version = _probe_binary_version(binary, ["--version"]) if binary else None
        if binary is None:
            issues.append("codex CLI binary not found in PATH or Codex Desktop standalone bundle.")
        git_ctx = inspect_git_context(cwd)
        if not cwd.exists():
            issues.append(f"Working directory does not exist: {cwd}")
        elif not git_ctx.is_git_repo:
            issues.append(f"Working directory is not inside a git repository: {cwd}")

        auth_hint: str | None = None
        if binary is not None:
            try:
                res = subprocess.run(
                    [binary, "login", "status"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                )
                status_line = (res.stdout or res.stderr).strip().splitlines()
                if status_line:
                    auth_hint = status_line[0].strip()
                if res.returncode != 0 and not os.environ.get("OPENAI_API_KEY"):
                    issues.append(
                        f"Codex authentication check failed ({auth_hint or 'not logged in'}). "
                        "Run `codex login` or export OPENAI_API_KEY."
                    )
            except (OSError, subprocess.TimeoutExpired):
                auth_hint = "status probe timed out"
        elif os.environ.get("OPENAI_API_KEY"):
            auth_hint = "OPENAI_API_KEY set"

        return PreflightResult(
            worker=self.name,
            ok=len(issues) == 0,
            binary_path=binary,
            version=version,
            cwd=str(cwd.resolve()) if cwd.exists() else str(cwd),
            is_git_repo=git_ctx.is_git_repo,
            git_root=str(git_ctx.git_root) if git_ctx.git_root else None,
            git_common_dir=str(git_ctx.git_common_dir) if git_ctx.git_common_dir else None,
            is_linked_worktree=git_ctx.is_linked_worktree,
            auth_hint=auth_hint,
            issues=issues,
        )

    def build_run_cmd(self, req: RunRequest) -> list[str]:
        effort = _validate_codex_effort(req.effort or "medium")
        sandbox = "read-only" if req.mode == "scout" else "workspace-write"
        cmd = [
            _codex_command_binary(),
            "exec",
            "-s",
            sandbox,
            "-C",
            str(req.cwd),
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--json",
        ]
        if req.output_last_message is not None:
            cmd.extend(["-o", str(req.output_last_message)])
        if req.schema_path is not None:
            cmd.extend(["--output-schema", str(req.schema_path)])
        if req.model:
            cmd.extend(["-m", req.model])
        for extra_dir in req.add_dirs:
            cmd.extend(["--add-dir", str(extra_dir)])
        for img in req.images:
            cmd.extend(["--image", str(img)])
        cmd.append("-")
        return cmd

    def build_resume_cmd(self, req: ResumeRequest) -> list[str]:
        if not req.session_id.strip():
            raise ValidationError("resume requires a non-empty --session id.")
        effort = _validate_codex_effort(req.effort) if req.effort is not None else None
        # `codex exec resume` rejects `-s` and `-C`; cwd is passed via subprocess `cwd=`.
        cmd = [_codex_command_binary(), "exec", "resume", req.session_id, "--json"]
        if req.output_last_message is not None:
            cmd.extend(["-o", str(req.output_last_message)])
        if req.schema_path is not None:
            cmd.extend(["--output-schema", str(req.schema_path)])
        if effort is not None:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if req.model:
            cmd.extend(["-m", req.model])
        for img in req.images:
            cmd.extend(["--image", str(img)])
        cmd.append("-")
        return cmd

    def build_review_cmd(self, req: ReviewRequest) -> list[str] | None:
        selectors = sum(
            bool(x) for x in (req.base is not None, req.uncommitted, req.commit is not None)
        )
        if selectors > 1:
            raise ValidationError("Specify at most one of --base, --uncommitted, or --commit.")
        cmd = [_codex_command_binary(), "exec", "review", "--json"]
        if req.base is not None:
            cmd.extend(["--base", req.base])
        elif req.commit is not None:
            cmd.extend(["--commit", req.commit])
        else:
            cmd.append("--uncommitted")
        if req.output_last_message is not None:
            cmd.extend(["-o", str(req.output_last_message)])
        if req.model:
            cmd.extend(["-m", req.model])
        if req.has_prompt:
            cmd.append("-")
        return cmd

    def parse_jsonl(
        self, lines: Iterable[str], preview_limit: int = OUTPUT_PREVIEW_LIMIT
    ) -> ParsedEvents:
        session_id: str | None = None
        commands: list[CommandRecord] = []
        changed_files: list[str] = []
        errors: list[str] = []
        final_message = ""
        usage: dict[str, Any] | None = None
        event_count = 0

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            etype = event.get("type")

            if etype == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    session_id = tid
            elif etype == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "command_execution":
                    raw_exit = item.get("exit_code")
                    exit_code = raw_exit if isinstance(raw_exit, int) else None
                    commands.append(
                        CommandRecord(
                            command=str(item.get("command", "")),
                            exit_code=exit_code,
                            status=str(item.get("status", "")),
                            output_preview=_preview(
                                item.get("aggregated_output") or item.get("output") or "",
                                limit=preview_limit,
                            ),
                        )
                    )
                elif itype == "file_change":
                    _extract_file_change_paths(item, changed_files)
                elif itype == "agent_message":
                    text = _extract_agent_text(item)
                    if text:
                        final_message = text
                elif itype == "error":
                    msg = _extract_error_text(item) or str(item)
                    _append_unique(errors, msg)
            elif etype == "turn.completed":
                u = event.get("usage")
                if isinstance(u, dict):
                    usage = u
            elif etype == "turn.failed":
                err_text = _extract_error_text(event.get("error")) or _extract_error_text(event)
                if err_text:
                    _append_unique(errors, err_text)
            elif etype == "error":
                err_text = _extract_error_text(event) or _extract_error_text(event.get("error"))
                if err_text:
                    _append_unique(errors, err_text)

        return ParsedEvents(
            session_id=session_id,
            commands=commands,
            changed_files=changed_files,
            errors=errors,
            final_message=final_message,
            usage=usage,
            event_count=event_count,
        )


def _resolve_dsh_permissions(
    mode: WriteMode,
    permission_mode: str | None,
    approval_policy: str | None,
    allow_danger: bool,
) -> tuple[DshPermissionMode, ApprovalPolicy]:
    perm = permission_mode or ("read-only" if mode == "scout" else "workspace-write")
    if perm not in DSH_PERMISSION_MODES:
        raise ValidationError(
            f"Unsupported DSH permission mode {perm!r}. "
            f"Allowed values: {', '.join(sorted(DSH_PERMISSION_MODES))}."
        )
    if perm == "danger-full-access" and not allow_danger:
        raise ValidationError(
            "DSH permission mode 'danger-full-access' is blocked by default. "
            "Pass --allow-danger explicitly to opt in."
        )
    policy = approval_policy or "never"
    if policy not in DSH_APPROVAL_POLICIES:
        raise ValidationError(
            f"Unsupported DSH approval policy {policy!r}. "
            f"Allowed values: {', '.join(sorted(DSH_APPROVAL_POLICIES))}."
        )
    return perm, policy  # type: ignore[return-value]


class DshAdapter:
    name: WorkerId = "dsh"

    def preflight(self, cwd: Path) -> PreflightResult:
        issues: list[str] = []
        binary = shutil.which("dsh")
        version = _probe_binary_version(binary, ["--version"]) if binary else None
        if binary is None:
            issues.append("dsh CLI binary not found in PATH.")
        git_ctx = inspect_git_context(cwd)
        if not cwd.exists():
            issues.append(f"Working directory does not exist: {cwd}")
        elif not git_ctx.is_git_repo:
            issues.append(f"Working directory is not inside a git repository: {cwd}")

        dsh_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
        if os.environ.get("DEEPSEEK_API_KEY"):
            auth_hint = "DEEPSEEK_API_KEY set"
        elif (dsh_home / "profiles" / "headless" / "cordis.patch.yml").exists():
            auth_hint = "headless profile patch present"
        elif dsh_home.exists():
            auth_hint = "dsh home directory present"
        else:
            auth_hint = "no DEEPSEEK_API_KEY or ~/.dsh credentials detected"

        return PreflightResult(
            worker=self.name,
            ok=len(issues) == 0,
            binary_path=binary,
            version=version,
            cwd=str(cwd.resolve()) if cwd.exists() else str(cwd),
            is_git_repo=git_ctx.is_git_repo,
            git_root=str(git_ctx.git_root) if git_ctx.git_root else None,
            git_common_dir=str(git_ctx.git_common_dir) if git_ctx.git_common_dir else None,
            is_linked_worktree=git_ctx.is_linked_worktree,
            auth_hint=auth_hint,
            issues=issues,
        )

    def _collect_add_dirs(
        self, cwd: Path, perm: DshPermissionMode, explicit: tuple[Path, ...]
    ) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()
        if perm == "workspace-write":
            common = resolve_worktree_git_common_dir(cwd)
            if common is not None:
                key = str(common)
                seen.add(key)
                dirs.append(common)
        for candidate in explicit:
            resolved = candidate.resolve()
            key = str(resolved)
            if key not in seen:
                seen.add(key)
                dirs.append(resolved)
        return dirs

    def build_run_cmd(self, req: RunRequest) -> list[str]:
        if req.images:
            raise ValidationError(
                "dsh --profile headless does not support --image attachments. "
                "Remove --image or use --worker codex."
            )
        perm, policy = _resolve_dsh_permissions(
            req.mode, req.permission_mode, req.approval_policy, req.allow_danger
        )
        cmd = [
            "dsh",
            "--profile",
            "headless",
            "--permission-mode",
            perm,
            "--approval-policy",
            policy,
        ]
        for extra_dir in self._collect_add_dirs(req.cwd, perm, req.add_dirs):
            cmd.extend(["--add-dir", str(extra_dir)])
        cmd.extend(["--json", "-"])
        return cmd

    def build_resume_cmd(self, req: ResumeRequest) -> list[str]:
        if not req.session_id.strip():
            raise ValidationError("resume requires a non-empty --session id.")
        if req.images:
            raise ValidationError(
                "dsh --profile headless does not support --image attachments. "
                "Remove --image or use --worker codex."
            )
        perm, policy = _resolve_dsh_permissions(
            req.mode, req.permission_mode, req.approval_policy, req.allow_danger
        )
        cmd = [
            "dsh",
            "--profile",
            "headless",
            "--session-id",
            req.session_id,
            "--permission-mode",
            perm,
            "--approval-policy",
            policy,
        ]
        for extra_dir in self._collect_add_dirs(req.cwd, perm, req.add_dirs):
            cmd.extend(["--add-dir", str(extra_dir)])
        cmd.extend(["--json", "-"])
        return cmd

    def build_review_cmd(self, req: ReviewRequest) -> list[str] | None:
        return None

    def parse_jsonl(
        self, lines: Iterable[str], preview_limit: int = OUTPUT_PREVIEW_LIMIT
    ) -> ParsedEvents:
        session_id: str | None = None
        commands: list[CommandRecord] = []
        changed_files: list[str] = []
        errors: list[str] = []
        final_message = ""
        usage: dict[str, Any] | None = None
        event_count = 0
        pending_calls: dict[str, str] = {}

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            etype = event.get("type")

            if etype == "session":
                sid = event.get("sessionId") or event.get("session_id")
                if isinstance(sid, str) and sid:
                    session_id = sid
            elif etype == "status":
                reason = event.get("reason")
                if isinstance(reason, dict) and "error" in reason:
                    err = _extract_error_text(reason.get("error"))
                    if err:
                        _append_unique(errors, err)
                elif "error" in event:
                    err = _extract_error_text(event.get("error"))
                    if err:
                        _append_unique(errors, err)
            elif etype == "final":
                sid = event.get("sessionId") or event.get("session_id")
                if isinstance(sid, str) and sid:
                    session_id = sid
                text = event.get("text")
                if isinstance(text, str) and text:
                    final_message = text
                reason = event.get("reason")
                if isinstance(reason, dict) and "error" in reason:
                    err = _extract_error_text(reason.get("error"))
                    if err:
                        _append_unique(errors, err)
                elif (
                    isinstance(reason, str)
                    and reason.lower() not in {"done", "completed", "ok", "success", "stop", "end_turn"}
                    and not final_message
                ):
                    _append_unique(errors, reason)
                u = event.get("usage")
                if isinstance(u, dict):
                    usage = u
            elif etype == "command_execution":
                raw_exit = event.get("exit_code")
                if raw_exit is None:
                    raw_exit = event.get("exitCode")
                exit_code = raw_exit if isinstance(raw_exit, int) else None
                commands.append(
                    CommandRecord(
                        command=str(event.get("command", "")),
                        exit_code=exit_code,
                        status=str(event.get("status", "completed")),
                        output_preview=_preview(
                            event.get("aggregated_output") or event.get("output") or "",
                            limit=preview_limit,
                        ),
                    )
                )
            elif etype in {"tool_call", "tool_use", "tool"}:
                tool_name = str(
                    event.get("toolName") or event.get("tool") or event.get("name") or ""
                ).lower()
                call_id = str(
                    event.get("callId") or event.get("toolCallId") or event.get("id") or ""
                )
                args_payload = (
                    event.get("arguments")
                    if "arguments" in event
                    else event.get("input")
                    if "input" in event
                    else event.get("args")
                )
                cmd_str = _extract_command_from_tool_args(args_payload) or (
                    str(event["command"]) if isinstance(event.get("command"), str) else None
                )
                file_path = _extract_path_from_tool_args(args_payload)
                if file_path and (tool_name in FILE_WRITE_TOOL_NAMES or "write" in tool_name or "edit" in tool_name):
                    _append_unique(changed_files, file_path)

                raw_exit = event.get("exit_code")
                if raw_exit is None:
                    raw_exit = event.get("exitCode")
                if cmd_str and (
                    raw_exit is not None or "output" in event or "result" in event
                ):
                    commands.append(
                        CommandRecord(
                            command=cmd_str,
                            exit_code=raw_exit if isinstance(raw_exit, int) else None,
                            status=str(event.get("status", "completed")),
                            output_preview=_preview(
                                event.get("output") or event.get("result") or "",
                                limit=preview_limit,
                            ),
                        )
                    )
                elif cmd_str and (tool_name in SHELL_TOOL_NAMES or "command" in tool_name or not tool_name):
                    if call_id:
                        pending_calls[call_id] = cmd_str
                    else:
                        commands.append(
                            CommandRecord(
                                command=cmd_str,
                                exit_code=None,
                                status="invoked",
                                output_preview="",
                            )
                        )
            elif etype == "tool_result":
                call_id = str(
                    event.get("callId") or event.get("toolCallId") or event.get("id") or ""
                )
                cmd_str = pending_calls.pop(call_id, None) if call_id else None
                if not cmd_str and isinstance(event.get("command"), str):
                    cmd_str = event["command"]
                result_payload = event.get("result")
                raw_exit = event.get("exit_code")
                if raw_exit is None:
                    raw_exit = event.get("exitCode")
                if raw_exit is None and isinstance(result_payload, dict):
                    raw_exit = result_payload.get("exit_code")
                    if raw_exit is None:
                        raw_exit = result_payload.get("exitCode")
                exit_code = raw_exit if isinstance(raw_exit, int) else None
                output_val = (
                    event.get("output")
                    or event.get("stdout")
                    or event.get("content")
                    or (
                        result_payload.get("output")
                        or result_payload.get("stdout")
                        or result_payload.get("stderr")
                        if isinstance(result_payload, dict)
                        else result_payload
                    )
                    or ""
                )
                if cmd_str:
                    status = (
                        "failed"
                        if event.get("isError") or (exit_code is not None and exit_code != 0)
                        else str(event.get("status", "completed"))
                    )
                    commands.append(
                        CommandRecord(
                            command=cmd_str,
                            exit_code=exit_code,
                            status=status,
                            output_preview=_preview(output_val, limit=preview_limit),
                        )
                    )
                if event.get("isError"):
                    err = _extract_error_text(event.get("error")) or _preview(
                        output_val, limit=preview_limit
                    )
                    if err:
                        _append_unique(errors, err)
            elif etype == "item.completed":
                item = event.get("item")
                if isinstance(item, dict):
                    itype = item.get("type")
                    if itype == "command_execution":
                        raw_exit = item.get("exit_code")
                        if raw_exit is None:
                            raw_exit = item.get("exitCode")
                        commands.append(
                            CommandRecord(
                                command=str(item.get("command", "")),
                                exit_code=raw_exit if isinstance(raw_exit, int) else None,
                                status=str(item.get("status", "")),
                                output_preview=_preview(
                                    item.get("aggregated_output") or item.get("output") or "",
                                    limit=preview_limit,
                                ),
                            )
                        )
                    elif itype == "file_change":
                        _extract_file_change_paths(item, changed_files)
                    elif itype == "agent_message":
                        text = _extract_agent_text(item)
                        if text:
                            final_message = text
                    elif itype == "error":
                        err = _extract_error_text(item)
                        if err:
                            _append_unique(errors, err)
            elif etype == "error":
                err = _extract_error_text(event.get("error")) or _extract_error_text(event)
                if err:
                    _append_unique(errors, err)

        for unmatched_cmd in pending_calls.values():
            commands.append(
                CommandRecord(
                    command=unmatched_cmd,
                    exit_code=None,
                    status="invoked",
                    output_preview="",
                )
            )

        return ParsedEvents(
            session_id=session_id,
            commands=commands,
            changed_files=changed_files,
            errors=errors,
            final_message=final_message,
            usage=usage,
            event_count=event_count,
        )


ADAPTERS: dict[WorkerId, WorkerAdapter] = {
    "codex": CodexAdapter(),
    "dsh": DshAdapter(),
}


def get_adapter(worker: str) -> WorkerAdapter:
    if worker not in ADAPTERS:
        raise ValidationError(
            f"Unknown worker {worker!r}. Allowed workers: {', '.join(sorted(ADAPTERS))}."
        )
    return ADAPTERS[worker]  # type: ignore[index]


def collect_git_diff_files(cwd: Path | None, base: str | None = None) -> list[str]:
    if cwd is None or not cwd.exists():
        return []
    ctx = inspect_git_context(cwd)
    if not ctx.is_git_repo:
        return []
    files: list[str] = []
    if base:
        diff_res = _run_git(cwd, ["diff", "--name-only", f"{base}..HEAD"])
        if diff_res.returncode == 0:
            for line in diff_res.stdout.splitlines():
                _append_unique(files, line.strip())
    status_res = _run_git(cwd, ["status", "--porcelain"])
    if status_res.returncode == 0:
        for line in status_res.stdout.splitlines():
            if len(line) >= 4:
                path_part = line[3:].strip()
                if " -> " in path_part:
                    path_part = path_part.split(" -> ", 1)[1].strip()
                _append_unique(files, path_part)
    return files


def create_worktree(
    repo: Path,
    task: str,
    base: str = "HEAD",
    worktree_dir: Path | None = None,
) -> dict[str, Any]:
    if not SLUG_PATTERN.match(task):
        raise ValidationError(
            f"Invalid task slug {task!r}. Must match {SLUG_PATTERN.pattern}."
        )
    ctx = inspect_git_context(repo)
    if not ctx.is_git_repo or ctx.git_root is None:
        raise ValidationError(f"Repository path is not a git repository: {repo}")
    repo_root = ctx.git_root
    base_res = _run_git(repo_root, ["rev-parse", "--verify", base])
    if base_res.returncode != 0:
        raise ValidationError(f"Cannot resolve base ref {base!r}: {base_res.stderr.strip()}")
    base_sha = base_res.stdout.strip()

    target_dir = (
        worktree_dir.resolve()
        if worktree_dir is not None
        else (repo_root.parent / ".worktrees" / repo_root.name / task).resolve()
    )
    if target_dir.exists():
        raise ValidationError(f"Worktree path already exists: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = f"codex-use/{task}"
    add_res = _run_git(
        repo_root,
        ["worktree", "add", "-b", branch, str(target_dir), base_sha],
        timeout=30.0,
    )
    if add_res.returncode != 0:
        raise DriverRuntimeError(
            f"git worktree add failed: {(add_res.stderr or add_res.stdout).strip()}"
        )
    ensure_local_git_exclude(repo_root)
    lock_file = resolve_ship_lock_path(target_dir)
    return {
        "ok": True,
        "repo": str(repo_root),
        "worktree": str(target_dir),
        "branch": branch,
        "base_ref": base,
        "base_sha": base_sha,
        "lock_file": str(lock_file),
    }


def _collect_safe_disposable_paths(wt_resolved: Path) -> list[Path]:
    status_res = _run_git(
        wt_resolved, ["status", "--porcelain", "-z", "-uall", "--ignored=matching"]
    )
    if status_res.returncode != 0:
        return []
    entries = [token for token in status_res.stdout.split("\0") if token]
    targets: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        if len(entry) < 4:
            return []
        xy = entry[:2]
        if xy not in {"??", "!!"}:
            return []
        rel = entry[3:].rstrip("/")
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            return []
        parts = rel_path.parts
        safe_idx = next(
            (idx for idx, part in enumerate(parts) if part in SAFE_DISPOSABLE_NAMES),
            None,
        )
        if safe_idx is None:
            return []
        candidate = (wt_resolved.joinpath(*parts[: safe_idx + 1])).resolve()
        if candidate == wt_resolved or not candidate.is_relative_to(wt_resolved):
            return []
        if candidate not in seen:
            seen.add(candidate)
            targets.append(candidate)
    return targets


def _clean_safe_disposables(wt_resolved: Path) -> list[str]:
    targets = _collect_safe_disposable_paths(wt_resolved)
    cleaned: list[str] = []
    for target in targets:
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        cleaned.append(str(target.relative_to(wt_resolved)))
        parent = target.parent
        while parent != wt_resolved and parent.is_relative_to(wt_resolved):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return cleaned


def _prune_empty_worktree_parents(wt_resolved: Path) -> None:
    parent = wt_resolved.parent
    grandparent = parent.parent
    if grandparent.name == ".worktrees":
        for candidate in (parent, grandparent):
            try:
                candidate.rmdir()
            except OSError:
                break
    elif parent.name == ".worktrees":
        try:
            parent.rmdir()
        except OSError:
            pass


def remove_worktree(
    repo: Path,
    worktree: Path,
    force: bool = False,
    delete_branch: bool = False,
) -> dict[str, Any]:
    ctx = inspect_git_context(repo)
    if not ctx.is_git_repo or ctx.git_root is None:
        raise ValidationError(f"Repository path is not a git repository: {repo}")
    repo_root = ctx.git_root
    wt_resolved = worktree.resolve()
    if not wt_resolved.exists():
        raise ValidationError(f"Worktree path does not exist: {wt_resolved}")

    branch_name: str | None = None
    branch_res = _run_git(wt_resolved, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch_res.returncode == 0:
        candidate = branch_res.stdout.strip()
        if candidate and candidate != "HEAD":
            branch_name = candidate

    cleaned_caches: list[str] = []
    with acquire_ship_lock(wt_resolved, "remove"):
        if not force:
            cleaned_caches = _clean_safe_disposables(wt_resolved)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt_resolved))
        rem_res = _run_git(repo_root, args, timeout=30.0)
        if rem_res.returncode != 0:
            raise DriverRuntimeError(
                f"git worktree remove failed: {(rem_res.stderr or rem_res.stdout).strip()}"
            )

    _prune_empty_worktree_parents(wt_resolved)

    branch_deleted = False
    if delete_branch and branch_name:
        flag = "-D" if force else "-d"
        del_res = _run_git(repo_root, ["branch", flag, branch_name])
        branch_deleted = del_res.returncode == 0

    return {
        "ok": True,
        "repo": str(repo_root),
        "removed": str(wt_resolved),
        "branch": branch_name,
        "branch_deleted": branch_deleted,
        "cleaned_caches": cleaned_caches,
    }


def _read_prompt(prompt_file: str | None, prompt_text: str | None, required: bool) -> str | None:
    if prompt_file is not None and prompt_text is not None:
        raise ValidationError("Provide either --prompt-file or --prompt, not both.")
    if prompt_text is not None:
        if not prompt_text.strip():
            raise ValidationError("Prompt text must not be empty.")
        return prompt_text
    if prompt_file is not None:
        if prompt_file == "-":
            content = sys.stdin.read()
        else:
            path = Path(prompt_file)
            if not path.is_file():
                raise ValidationError(f"Prompt file does not exist: {path}")
            content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValidationError("Prompt content must not be empty.")
        return content
    if required:
        raise ValidationError("A prompt is required via --prompt-file <path|-> or --prompt <text>.")
    return None


def _prepare_log_path(log_dir: Path | None, worker: WorkerId, mode: RunMode) -> Path:
    base_dir = log_dir.resolve() if log_dir is not None else (Path(tempfile.gettempdir()) / "codex-use-logs")
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return base_dir / f"{worker}-{mode}-{stamp}-{os.getpid()}.jsonl"


def _execute_worker(
    adapter: WorkerAdapter,
    cmd: list[str],
    cwd: Path,
    mode: RunMode,
    prompt: str | None,
    log_path: Path,
    output_last_message: Path | None,
    base_sha: str | None = None,
    full_output: bool = False,
) -> AuditSummary:
    stderr_path = log_path.with_suffix(".stderr.log")
    try:
        with log_path.open("w", encoding="utf-8") as stdout_f, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_f:
            proc = subprocess.run(
                cmd,
                input=prompt,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                cwd=str(cwd),
                check=False,
            )
    except FileNotFoundError as exc:
        raise DriverRuntimeError(f"Worker binary not found when executing: {cmd[0]}") from exc

    preview_limit = FULL_OUTPUT_PREVIEW_LIMIT if full_output else OUTPUT_PREVIEW_LIMIT
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        parsed = adapter.parse_jsonl(f, preview_limit=preview_limit)

    raw_final_message = parsed.final_message
    if output_last_message is not None and output_last_message.is_file():
        file_msg = output_last_message.read_text(encoding="utf-8", errors="replace").strip()
        if file_msg:
            raw_final_message = file_msg

    errors = list(parsed.errors)
    if proc.returncode != 0 and not errors:
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        if stderr_text:
            _append_unique(errors, _preview(stderr_text, limit=600))

    receipt, receipt_valid = parse_receipt(raw_final_message)
    summary_final_message: str | None = (
        None if (receipt_valid is True and not full_output) else raw_final_message
    )
    git_diff_files = collect_git_diff_files(cwd, base=base_sha)

    return AuditSummary(
        worker=adapter.name,
        mode=mode,
        session_id=parsed.session_id,
        exit_code=proc.returncode,
        commands=parsed.commands,
        changed_files=parsed.changed_files,
        git_diff_files=git_diff_files,
        errors=errors,
        final_message=summary_final_message,
        receipt=receipt,
        receipt_valid=receipt_valid,
        usage=parsed.usage,
        raw_log=str(log_path),
        command=cmd,
    )


def _emit_json(payload: Any) -> None:
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _handle_preflight(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    result = adapter.preflight(Path(args.cwd))
    _emit_json(result)
    return EXIT_OK if result.ok else EXIT_ERROR


def _handle_worktree_create(args: argparse.Namespace) -> int:
    if args.preflight and not args.worker:
        raise ValidationError("--preflight requires --worker <codex|dsh>.")
    repo_path = Path(args.repo)
    preflight_dict: dict[str, Any] | None = None
    if args.preflight:
        adapter = get_adapter(args.worker)
        ctx = inspect_git_context(repo_path)
        pf_target = ctx.git_root if (ctx.is_git_repo and ctx.git_root is not None) else repo_path
        pf = adapter.preflight(pf_target)
        preflight_dict = dataclasses.asdict(pf)
        if not pf.ok:
            _emit_json(
                {
                    "ok": False,
                    "error": "preflight_failed",
                    "preflight": preflight_dict,
                }
            )
            return EXIT_ERROR

    wt_dir = Path(args.worktree_dir) if args.worktree_dir else None
    result = create_worktree(repo_path, args.task, base=args.base, worktree_dir=wt_dir)
    if preflight_dict is not None:
        result["preflight"] = preflight_dict
    _emit_json(result)
    return EXIT_OK


def _handle_worktree_remove(args: argparse.Namespace) -> int:
    result = remove_worktree(
        Path(args.repo),
        Path(args.worktree),
        force=args.force,
        delete_branch=args.delete_branch,
    )
    _emit_json(result)
    return EXIT_OK


def _resolve_image_paths(raw_images: list[str] | None) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for raw in raw_images or []:
        path = Path(raw)
        if not path.is_file():
            raise ValidationError(f"Image file does not exist: {path}")
        resolved.append(path.resolve())
    return tuple(resolved)


def _handle_run(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        raise ValidationError(f"Working directory does not exist: {cwd}")
    schema_path = Path(args.schema) if args.schema else None
    if schema_path is not None and not schema_path.is_file():
        raise ValidationError(f"Schema file does not exist: {schema_path}")
    out_msg = Path(args.output_last_message) if args.output_last_message else None
    add_dirs = tuple(Path(p) for p in (args.add_dir or []))
    images = _resolve_image_paths(args.image)
    req = RunRequest(
        worker=adapter.name,
        mode=args.mode,
        cwd=cwd.resolve(),
        effort=args.effort,
        model=args.model,
        permission_mode=args.permission_mode,
        approval_policy=args.approval_policy,
        allow_danger=args.allow_danger,
        schema_path=schema_path.resolve() if schema_path else None,
        output_last_message=out_msg.resolve() if out_msg else None,
        add_dirs=add_dirs,
        images=images,
    )
    cmd = adapter.build_run_cmd(req)
    prompt = _read_prompt(args.prompt_file, args.prompt, required=True)
    _maybe_exclude_local_prompt_dir(req.cwd, args.prompt_file)
    log_path = _prepare_log_path(Path(args.log_dir) if args.log_dir else None, adapter.name, req.mode)

    lock_cm = (
        acquire_ship_lock(req.cwd, "ship")
        if req.mode == "ship"
        else contextlib.nullcontext()
    )
    with lock_cm:
        summary = _execute_worker(
            adapter=adapter,
            cmd=cmd,
            cwd=req.cwd,
            mode=req.mode,
            prompt=prompt,
            log_path=log_path,
            output_last_message=req.output_last_message,
            base_sha=args.base,
            full_output=args.full_output,
        )
    _emit_json(summary)
    return EXIT_OK if summary.exit_code == 0 else EXIT_ERROR


def _handle_resume(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        raise ValidationError(f"Working directory does not exist: {cwd}")
    schema_path = Path(args.schema) if args.schema else None
    if schema_path is not None and not schema_path.is_file():
        raise ValidationError(f"Schema file does not exist: {schema_path}")
    out_msg = Path(args.output_last_message) if args.output_last_message else None
    add_dirs = tuple(Path(p) for p in (args.add_dir or []))
    images = _resolve_image_paths(args.image)
    req = ResumeRequest(
        worker=adapter.name,
        session_id=args.session,
        mode=args.mode,
        cwd=cwd.resolve(),
        effort=args.effort,
        model=args.model,
        permission_mode=args.permission_mode,
        approval_policy=args.approval_policy,
        allow_danger=args.allow_danger,
        schema_path=schema_path.resolve() if schema_path else None,
        output_last_message=out_msg.resolve() if out_msg else None,
        add_dirs=add_dirs,
        images=images,
    )
    cmd = adapter.build_resume_cmd(req)
    prompt = _read_prompt(args.prompt_file, args.prompt, required=True)
    _maybe_exclude_local_prompt_dir(req.cwd, args.prompt_file)
    log_path = _prepare_log_path(Path(args.log_dir) if args.log_dir else None, adapter.name, req.mode)

    lock_cm = (
        acquire_ship_lock(req.cwd, "ship-resume")
        if req.mode == "ship"
        else contextlib.nullcontext()
    )
    with lock_cm:
        summary = _execute_worker(
            adapter=adapter,
            cmd=cmd,
            cwd=req.cwd,
            mode=req.mode,
            prompt=prompt,
            log_path=log_path,
            output_last_message=req.output_last_message,
            base_sha=args.base,
            full_output=args.full_output,
        )
    _emit_json(summary)
    return EXIT_OK if summary.exit_code == 0 else EXIT_ERROR


def _handle_review(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        raise ValidationError(f"Working directory does not exist: {cwd}")
    prompt = _read_prompt(args.prompt_file, args.prompt, required=False)
    out_msg = Path(args.output_last_message) if args.output_last_message else None
    req = ReviewRequest(
        worker=adapter.name,
        cwd=cwd.resolve(),
        base=args.base,
        uncommitted=args.uncommitted,
        commit=args.commit,
        model=args.model,
        has_prompt=prompt is not None,
        output_last_message=out_msg.resolve() if out_msg else None,
    )
    cmd = adapter.build_review_cmd(req)
    if cmd is None:
        _emit_json(
            {
                "ok": False,
                "worker": adapter.name,
                "error": "unsupported_review",
                "message": (
                    "dsh --profile headless has no native review subcommand. "
                    "Run host-side git diff review or pass --worker codex."
                ),
            }
        )
        return EXIT_ERROR

    _maybe_exclude_local_prompt_dir(req.cwd, args.prompt_file)
    log_path = _prepare_log_path(Path(args.log_dir) if args.log_dir else None, adapter.name, "review")
    summary = _execute_worker(
        adapter=adapter,
        cmd=cmd,
        cwd=req.cwd,
        mode="review",
        prompt=prompt,
        log_path=log_path,
        output_last_message=req.output_last_message,
        base_sha=args.base,
        full_output=args.full_output,
    )
    _emit_json(summary)
    return EXIT_OK if summary.exit_code == 0 else EXIT_ERROR


def _handle_audit(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    log_path = Path(args.log)
    if not log_path.is_file():
        raise ValidationError(f"Log file does not exist: {log_path}")
    preview_limit = FULL_OUTPUT_PREVIEW_LIMIT if args.full_output else OUTPUT_PREVIEW_LIMIT
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        parsed = adapter.parse_jsonl(f, preview_limit=preview_limit)
    cwd = Path(args.cwd).resolve() if args.cwd else None
    git_diff_files = collect_git_diff_files(cwd, base=args.base)
    receipt, receipt_valid = parse_receipt(parsed.final_message)
    summary_final_message: str | None = (
        None if (receipt_valid is True and not args.full_output) else parsed.final_message
    )
    summary = AuditSummary(
        worker=adapter.name,
        mode=args.mode,
        session_id=parsed.session_id,
        exit_code=args.exit_code,
        commands=parsed.commands,
        changed_files=parsed.changed_files,
        git_diff_files=git_diff_files,
        errors=parsed.errors,
        final_message=summary_final_message,
        receipt=receipt,
        receipt_valid=receipt_valid,
        usage=parsed.usage,
        raw_log=str(log_path.resolve()),
    )
    _emit_json(summary)
    return EXIT_OK if summary.exit_code == 0 and not summary.errors else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drive.py",
        description="Zero-dependency worker runner, worktree guard, and JSONL auditor for Codex-use.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_pre = subparsers.add_parser("preflight", help="Check worker binary, auth, and git workspace.")
    p_pre.add_argument("--worker", required=True, choices=sorted(ADAPTERS))
    p_pre.add_argument("--cwd", required=True)
    p_pre.set_defaults(handler=_handle_preflight)

    p_wt = subparsers.add_parser("worktree", help="Create or remove isolated git worktrees.")
    wt_sub = p_wt.add_subparsers(dest="wt_action", required=True)

    p_wt_create = wt_sub.add_parser("create", help="Create a linked worktree for a ship slice.")
    p_wt_create.add_argument("--repo", required=True)
    p_wt_create.add_argument("--task", required=True)
    p_wt_create.add_argument("--base", default="HEAD")
    p_wt_create.add_argument("--worktree-dir")
    p_wt_create.add_argument("--preflight", action="store_true")
    p_wt_create.add_argument("--worker", choices=sorted(ADAPTERS))
    p_wt_create.set_defaults(handler=_handle_worktree_create)

    p_wt_remove = wt_sub.add_parser("remove", help="Remove a linked worktree safely.")
    p_wt_remove.add_argument("--repo", required=True)
    p_wt_remove.add_argument("--worktree", required=True)
    p_wt_remove.add_argument("--force", action="store_true")
    p_wt_remove.add_argument("--delete-branch", action="store_true")
    p_wt_remove.set_defaults(handler=_handle_worktree_remove)

    p_run = subparsers.add_parser("run", help="Launch a new headless worker session.")
    p_run.add_argument("--worker", required=True, choices=sorted(ADAPTERS))
    p_run.add_argument("--mode", required=True, choices=["scout", "ship"])
    p_run.add_argument("--cwd", required=True)
    p_run.add_argument("--prompt-file")
    p_run.add_argument("--prompt")
    p_run.add_argument("--effort")
    p_run.add_argument("--model")
    p_run.add_argument("--permission-mode")
    p_run.add_argument("--approval-policy")
    p_run.add_argument("--allow-danger", action="store_true")
    p_run.add_argument("--schema")
    p_run.add_argument("--output-last-message")
    p_run.add_argument("--add-dir", action="append")
    p_run.add_argument("--image", action="append")
    p_run.add_argument("--log-dir")
    p_run.add_argument("--base")
    p_run.add_argument("--full-output", action="store_true")
    p_run.set_defaults(handler=_handle_run)

    p_res = subparsers.add_parser("resume", help="Resume an existing worker session in the same thread.")
    p_res.add_argument("--worker", required=True, choices=sorted(ADAPTERS))
    p_res.add_argument("--session", required=True)
    p_res.add_argument("--mode", default="ship", choices=["scout", "ship"])
    p_res.add_argument("--cwd", required=True)
    p_res.add_argument("--prompt-file")
    p_res.add_argument("--prompt")
    p_res.add_argument("--effort")
    p_res.add_argument("--model")
    p_res.add_argument("--permission-mode")
    p_res.add_argument("--approval-policy")
    p_res.add_argument("--allow-danger", action="store_true")
    p_res.add_argument("--schema")
    p_res.add_argument("--output-last-message")
    p_res.add_argument("--add-dir", action="append")
    p_res.add_argument("--image", action="append")
    p_res.add_argument("--log-dir")
    p_res.add_argument("--base")
    p_res.add_argument("--full-output", action="store_true")
    p_res.set_defaults(handler=_handle_resume)

    p_rev = subparsers.add_parser("review", help="Run non-interactive diff review.")
    p_rev.add_argument("--worker", required=True, choices=sorted(ADAPTERS))
    p_rev.add_argument("--cwd", required=True)
    p_rev.add_argument("--base")
    p_rev.add_argument("--uncommitted", action="store_true")
    p_rev.add_argument("--commit")
    p_rev.add_argument("--model")
    p_rev.add_argument("--prompt-file")
    p_rev.add_argument("--prompt")
    p_rev.add_argument("--output-last-message")
    p_rev.add_argument("--log-dir")
    p_rev.add_argument("--full-output", action="store_true")
    p_rev.set_defaults(handler=_handle_review)

    p_aud = subparsers.add_parser("audit", help="Parse an existing worker JSONL log into an AuditSummary.")
    p_aud.add_argument("--worker", required=True, choices=sorted(ADAPTERS))
    p_aud.add_argument("--log", required=True)
    p_aud.add_argument("--cwd")
    p_aud.add_argument("--base")
    p_aud.add_argument("--mode", default="ship", choices=["scout", "ship", "review"])
    p_aud.add_argument("--exit-code", type=int, default=0)
    p_aud.add_argument("--full-output", action="store_true")
    p_aud.set_defaults(handler=_handle_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.handler(args)
    except ValidationError as exc:
        _emit_json({"ok": False, "error": "invalid_arguments", "message": str(exc)})
        return EXIT_INVALID_ARGS
    except LockConflictError as exc:
        _emit_json(
            {
                "ok": False,
                "error": "lock_conflict",
                "message": str(exc),
                "lock_file": str(exc.lock_file),
            }
        )
        return EXIT_LOCK_CONFLICT
    except DriverRuntimeError as exc:
        _emit_json({"ok": False, "error": "runtime_error", "message": str(exc)})
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
