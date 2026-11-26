#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from collections import deque
import contextlib
import dataclasses
from dataclasses import dataclass, field
import functools
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Literal, Protocol
import urllib.parse
import uuid

WorkerId = Literal["codex", "dsh"]
RunMode = Literal["scout", "ship", "review"]
WriteMode = Literal["scout", "ship"]
CodexEffort = Literal["medium", "high", "xhigh"]
DshPermissionMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["ask", "never"]
ThreadResolutionKind = Literal[
    "matched",
    "ambiguous",
    "desktop_writer_lock",
    "not_found",
    "daemon_offline",
]
AttachOutcomeStatus = Literal[
    "completed",
    "failed",
    "interrupted",
    "approval_required",
    "user_input_required",
    "thread_closed",
    "timeout",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_LOCK_CONFLICT = 3

NON_ORIGINATING_CLIENT_NAME = "codex-backend"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_MAX_WS_FRAME_BYTES = 16 * 1024 * 1024
MAX_INBOUND_WS_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_WS_HANDSHAKE_BYTES = 65536
DEFAULT_DISCOVERY_TIMEOUT = 10.0
DEFAULT_ATTACH_TIMEOUT = 600.0
APPROVAL_DETAIL_GRACE_SECONDS = 0.05
OPT_OUT_NOTIFICATION_METHODS: tuple[str, ...] = (
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "item/plan/delta",
)
APPROVAL_REQUEST_METHODS: frozenset[str] = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
    }
)
USER_INPUT_REQUEST_METHODS: frozenset[str] = frozenset(
    {
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
    }
)

CODEX_ALLOWED_EFFORTS: frozenset[str] = frozenset({"medium", "high", "xhigh"})
CODEX_FORBIDDEN_EFFORTS: frozenset[str] = frozenset({"ultra", "ultrafast"})
CODEX_ALLOWED_SERVICE_TIERS: frozenset[str] = frozenset({"default", "priority", "flex"})
SUBAGENT_DISPATCH_PREAMBLE = (
    "Dispatched as a subagent for a bounded slice; "
    "skip workflow-routing skills such as using-ryugi and execute the task directly."
)
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
COMPACT_COMMAND_LIMIT = 120
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


class DaemonOfflineError(DriverRuntimeError):
    def __init__(self, message: str, endpoint: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint


class WebSocketClosedError(DriverRuntimeError):
    pass


class JsonRpcError(DriverRuntimeError):
    def __init__(self, method: str, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.rpc_message = message
        self.data = data


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
    status: str | None = None


@dataclass(frozen=True)
class DaemonEndpoint:
    transport: Literal["unix", "tcp", "tls"]
    address: str
    host_header: str
    ws_path: str = "/rpc"
    port: int | None = None
    bearer_token: str | None = None
    is_custom_remote: bool = False

    def display(self) -> str:
        if self.transport == "unix":
            return self.address
        scheme = "wss" if self.transport == "tls" else "ws"
        return f"{scheme}://{self.host_header}{self.ws_path}"


@dataclass(frozen=True)
class LoadedTuiThread:
    thread_id: str
    title: str
    cwd: str
    resolved_cwd: str
    git_root: str | None
    status: str
    active_flags: tuple[str, ...] = ()
    model: str | None = None
    originator: str | None = None
    source: str = "unknown"
    updated_at: int = 0
    created_at: int = 0

    def to_candidate_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "thread_id": self.thread_id,
            "title": self.title,
            "cwd": self.cwd,
            "status": self.status,
        }
        if self.active_flags:
            payload["active_flags"] = list(self.active_flags)
        if self.model:
            payload["model"] = self.model
        if self.originator:
            payload["originator"] = self.originator
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        return payload


@dataclass(frozen=True)
class ThreadResolution:
    kind: ThreadResolutionKind
    thread: LoadedTuiThread | None = None
    candidates: tuple[LoadedTuiThread, ...] = ()
    locked_thread_ids: tuple[str, ...] = ()
    endpoint: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class DaemonTurnObservation:
    thread_id: str
    turn_id: str | None
    client_user_message_id: str
    dispatch_method: Literal["thread/queue/add", "turn/start"]
    queued_behind_active_turn: bool
    status: AttachOutcomeStatus
    commands: list[CommandRecord]
    changed_files: list[str]
    errors: list[str]
    final_message: str
    usage: dict[str, Any] | None
    approval_request: dict[str, Any] | None = None
    event_count: int = 0


@dataclass(frozen=True)
class AttachRequest:
    worker: WorkerId
    mode: WriteMode
    cwd: Path | None
    session_id: str | None = None
    select_latest: bool = False
    create: bool = False
    effort: str | None = None
    model: str | None = None
    fast: bool = False
    schema_path: Path | None = None
    output_last_message: Path | None = None
    images: tuple[Path, ...] = ()
    timeout: float = DEFAULT_ATTACH_TIMEOUT
    wait_for_approval: bool = False
    remote: str | None = None
    remote_auth_token_env: str | None = None


@dataclass(frozen=True)
class RunRequest:
    worker: WorkerId
    mode: WriteMode
    cwd: Path
    effort: str | None = None
    model: str | None = None
    fast: bool = False
    with_mcp: bool = False
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
    fast: bool = False
    with_mcp: bool = False
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
    head_sha: str | None = None


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
    cmd = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args=cmd, returncode=127, stdout="", stderr=str(exc))


def _resolve_git_path(cwd: Path, raw: str) -> Path:
    p = Path(raw.strip())
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


@functools.lru_cache(maxsize=256)
def _inspect_git_context_cached(resolved_cwd_str: str) -> GitContext:
    cwd = Path(resolved_cwd_str)
    res = _run_git(cwd, ["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir", "HEAD"])
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    head_sha: str | None = None
    if res.returncode == 0 and len(lines) >= 3:
        git_root = Path(lines[0]).resolve()
        git_dir = _resolve_git_path(cwd, lines[1])
        git_common_dir = _resolve_git_path(cwd, lines[2])
        if len(lines) >= 4:
            head_sha = lines[3]
        is_linked = git_dir != git_common_dir
        return GitContext(True, git_root, git_dir, git_common_dir, is_linked, head_sha)

    stderr_lower = (res.stderr or "").lower()
    if "not a git repository" not in stderr_lower and (
        "head" in stderr_lower or "unknown revision" in stderr_lower
    ):
        fallback = _run_git(cwd, ["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"])
        fb_lines = [line.strip() for line in fallback.stdout.splitlines() if line.strip()]
        if fallback.returncode == 0 and len(fb_lines) >= 3:
            git_root = Path(fb_lines[0]).resolve()
            git_dir = _resolve_git_path(cwd, fb_lines[1])
            git_common_dir = _resolve_git_path(cwd, fb_lines[2])
            is_linked = git_dir != git_common_dir
            return GitContext(True, git_root, git_dir, git_common_dir, is_linked, None)

    return GitContext(False, None, None, None, False, None)


def clear_git_context_cache() -> None:
    _inspect_git_context_cached.cache_clear()


def inspect_git_context(cwd: Path) -> GitContext:
    if not cwd.exists():
        return GitContext(False, None, None, None, False, None)
    return _inspect_git_context_cached(str(cwd.resolve()))


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
    if fcntl is None:
        yield lock_path
        return
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
    normalized = effort.strip().lower()
    if normalized in CODEX_FORBIDDEN_EFFORTS or effort not in CODEX_ALLOWED_EFFORTS:
        raise ValidationError(
            f"Unsupported Codex reasoning effort {effort!r}. "
            f"Allowed values: {', '.join(sorted(CODEX_ALLOWED_EFFORTS))}. "
            "'ultra' and 'ultrafast' are explicitly forbidden."
        )
    return effort  # type: ignore[return-value]


def _validate_codex_service_tier(service_tier: str | None) -> str | None:
    if service_tier is None:
        return None
    normalized = service_tier.strip().lower()
    if normalized in CODEX_FORBIDDEN_EFFORTS or service_tier not in CODEX_ALLOWED_SERVICE_TIERS:
        raise ValidationError(
            f"Unsupported Codex service tier {service_tier!r}. "
            f"Allowed values: {', '.join(sorted(CODEX_ALLOWED_SERVICE_TIERS))}. "
            "'ultra' and 'ultrafast' are explicitly forbidden."
        )
    return service_tier


def _apply_prompt_preamble(prompt: str, raw_prompt: bool = False) -> str:
    if raw_prompt:
        return prompt
    if "using-ryugi" in prompt or "Dispatched as a subagent" in prompt:
        return prompt
    return f"{SUBAGENT_DISPATCH_PREAMBLE}\n\n{prompt}"


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
        ]
        if not req.with_mcp:
            cmd.extend(["-c", "mcp_servers={}"])
        if req.fast:
            cmd.extend(["-c", 'service_tier="priority"'])
        cmd.append("--json")
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
        if not req.with_mcp:
            cmd.extend(["-c", "mcp_servers={}"])
        if req.fast:
            cmd.extend(["-c", 'service_tier="priority"'])
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

        parsed_events: list[dict[str, Any]] = []
        attach_thread_id: str | None = None
        attach_client_msg_id: str | None = None
        attach_turn_id: str | None = None
        attach_queued = False
        attach_status: str | None = None

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            parsed_events.append(obj)
            otype = obj.get("type")
            if otype == "attach.session":
                tid = obj.get("thread_id")
                if isinstance(tid, str) and tid:
                    attach_thread_id = tid
                cid = obj.get("client_user_message_id")
                if isinstance(cid, str) and cid:
                    attach_client_msg_id = cid
            elif otype == "attach.turn":
                tid = obj.get("thread_id")
                if isinstance(tid, str) and tid:
                    attach_thread_id = tid
                t_turn = obj.get("turn_id")
                if isinstance(t_turn, str) and t_turn:
                    attach_turn_id = t_turn
                if isinstance(obj.get("queued_behind_active_turn"), bool):
                    attach_queued = obj["queued_behind_active_turn"]
                st = obj.get("status")
                if isinstance(st, str) and st:
                    attach_status = st

        if attach_turn_id is None and attach_client_msg_id is not None:
            for obj in parsed_events:
                if obj.get("method") in {"item/started", "item/completed"}:
                    p = obj.get("params") if isinstance(obj.get("params"), dict) else {}
                    if attach_thread_id and p.get("threadId") != attach_thread_id:
                        continue
                    it = p.get("item")
                    if isinstance(it, dict) and it.get("type") in {"userMessage", "user_message"}:
                        if (it.get("clientId") or it.get("clientUserMessageId")) == attach_client_msg_id:
                            cand_tid = p.get("turnId")
                            if isinstance(cand_tid, str) and cand_tid:
                                attach_turn_id = cand_tid
                                break

        for event in parsed_events:
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
            elif etype in {"attach.session", "attach.turn"}:
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    session_id = tid
                for extra_err in event.get("errors") or []:
                    if isinstance(extra_err, str) and extra_err:
                        _append_unique(errors, extra_err)
            elif isinstance(event.get("method"), str):
                method = event["method"]
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                tid = params.get("threadId") or params.get("conversationId")
                if attach_thread_id is not None and isinstance(tid, str) and tid != attach_thread_id:
                    continue
                if isinstance(tid, str) and tid and session_id is None:
                    session_id = tid

                ev_turn_id = params.get("turnId")
                if ev_turn_id is None and isinstance(params.get("turn"), dict):
                    ev_turn_id = params["turn"].get("id")

                if attach_turn_id is not None:
                    turn_matches = ev_turn_id == attach_turn_id or (
                        ev_turn_id is None and not attach_queued
                    )
                elif attach_queued or attach_client_msg_id is not None:
                    turn_matches = False
                else:
                    turn_matches = True

                if method == "item/completed" and turn_matches:
                    item = params.get("item")
                    if isinstance(item, dict):
                        _ingest_app_server_item(item, commands, changed_files, errors, preview_limit)
                        if item.get("type") in {"agentMessage", "agent_message"}:
                            text = _extract_agent_text(item)
                            if text:
                                final_message = text
                elif method == "thread/tokenUsage/updated" and turn_matches:
                    tu = params.get("tokenUsage")
                    if isinstance(tu, dict):
                        last_u = tu.get("last")
                        usage = last_u if isinstance(last_u, dict) else tu
                elif method == "turn/completed" and turn_matches:
                    turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    if turn_obj.get("status") == "failed":
                        err_text = _extract_error_text(turn_obj.get("error")) or "Turn failed."
                        _append_unique(errors, err_text)
                    elif turn_obj.get("status") == "interrupted":
                        _append_unique(errors, "turn_interrupted: Turn was interrupted in live TUI.")
                elif method == "error" and not params.get("willRetry", False) and turn_matches:
                    err_text = _extract_error_text(params.get("error")) or _extract_error_text(params)
                    if err_text:
                        _append_unique(errors, err_text)
                elif (
                    "id" in event
                    and method in (APPROVAL_REQUEST_METHODS | USER_INPUT_REQUEST_METHODS)
                    and (
                        turn_matches
                        or attach_status in {"approval_required", "user_input_required"}
                    )
                ):
                    _append_unique(errors, _format_interactive_block_message(method, params))

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
        if req.fast:
            raise ValidationError(
                'dsh --profile headless does not support --fast (service_tier="priority"). '
                "Remove --fast or use --worker codex."
            )
        if req.with_mcp:
            raise ValidationError(
                "dsh --profile headless does not support --with-mcp. "
                "Remove --with-mcp or use --worker codex."
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
        if req.fast:
            raise ValidationError(
                'dsh --profile headless does not support --fast (service_tier="priority"). '
                "Remove --fast or use --worker codex."
            )
        if req.with_mcp:
            raise ValidationError(
                "dsh --profile headless does not support --with-mcp. "
                "Remove --with-mcp or use --worker codex."
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
    clear_git_context_cache()
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
    clear_git_context_cache()
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


def _compact_command_record(record: CommandRecord) -> CommandRecord:
    collapsed = " ".join(record.command.split())
    if len(collapsed) > COMPACT_COMMAND_LIMIT:
        collapsed = collapsed[: COMPACT_COMMAND_LIMIT - 3] + "..."
    preview = "" if record.exit_code == 0 else record.output_preview
    return CommandRecord(
        command=collapsed,
        exit_code=record.exit_code,
        status=record.status,
        output_preview=preview,
    )


def _serialize_audit_summary(summary: AuditSummary, full_output: bool) -> dict[str, Any]:
    data = dataclasses.asdict(summary)
    if data.get("status") is None:
        data.pop("status", None)
    if not full_output:
        for cmd_entry in data["commands"]:
            if cmd_entry.get("exit_code") == 0 and cmd_entry.get("output_preview") == "":
                cmd_entry.pop("output_preview", None)
    return data


def _ingest_app_server_item(
    item: dict[str, Any],
    commands: list[CommandRecord],
    changed_files: list[str],
    errors: list[str],
    preview_limit: int,
) -> None:
    itype = item.get("type")
    if itype in {"commandExecution", "command_execution"}:
        raw_exit = item.get("exitCode") if "exitCode" in item else item.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        out = (
            item.get("aggregatedOutput")
            if item.get("aggregatedOutput") is not None
            else (item.get("aggregated_output") or item.get("output") or "")
        )
        commands.append(
            CommandRecord(
                command=str(item.get("command") or ""),
                exit_code=exit_code,
                status=str(item.get("status") or ""),
                output_preview=_preview(out, limit=preview_limit),
            )
        )
    elif itype in {"fileChange", "file_change"}:
        _extract_file_change_paths(item, changed_files)
    elif itype == "error":
        msg = _extract_error_text(item) or str(item)
        _append_unique(errors, msg)


def _format_interactive_block_message(method: str, params: dict[str, Any]) -> str:
    prefix = "user_input_required" if method in USER_INPUT_REQUEST_METHODS else "approval_required"
    details: list[str] = [method]
    cmd = params.get("command")
    if isinstance(cmd, str) and cmd.strip():
        details.append(f"command={_preview(cmd, limit=80)}")
    reason = params.get("reason") or params.get("message") or params.get("title")
    if isinstance(reason, str) and reason.strip():
        details.append(f"reason={_preview(reason, limit=80)}")
    questions = params.get("questions")
    if isinstance(questions, list) and questions:
        first_q = questions[0]
        if isinstance(first_q, dict) and isinstance(first_q.get("question"), str):
            details.append(f"question={_preview(first_q['question'], limit=80)}")
    joined = ", ".join(details)
    return f"{prefix}: Live TUI session is waiting for terminal interaction ({joined})."


def _xor_mask(payload: bytes, mask: bytes) -> bytes:
    length = len(payload)
    if length == 0:
        return b""
    repeated = (mask * (length // 4 + 1))[:length]
    masked_int = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return masked_int.to_bytes(length, "big")


def _resolve_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _probe_daemon_socket_from_cli() -> Path | None:
    binary, _ = _resolve_codex_binary()
    if not binary:
        return None
    try:
        res = subprocess.run(
            [binary, "app-server", "daemon", "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    first_line = (res.stdout or "").strip().splitlines()
    if not first_line:
        return None
    try:
        data = json.loads(first_line[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("status") != "running":
        return None
    sock_path = data.get("socketPath")
    return Path(sock_path) if isinstance(sock_path, str) and sock_path else None


def resolve_daemon_endpoint(
    remote: str | None = None,
    remote_auth_token_env: str | None = None,
    codex_home: Path | None = None,
) -> DaemonEndpoint:
    bearer_token: str | None = None
    if remote_auth_token_env:
        token = os.environ.get(remote_auth_token_env, "").strip()
        if not token:
            raise ValidationError(
                f"Environment variable {remote_auth_token_env!r} for --remote-auth-token-env is empty or unset."
            )
        bearer_token = token

    home = codex_home if codex_home is not None else _resolve_codex_home()
    default_sock = home / "app-server-control" / "app-server-control.sock"

    if remote is None or remote == "unix://":
        sock_candidate = default_sock
        if not sock_candidate.exists():
            cli_sock = _probe_daemon_socket_from_cli()
            if cli_sock is not None:
                sock_candidate = cli_sock
        return DaemonEndpoint(
            transport="unix",
            address=str(sock_candidate),
            host_header="localhost",
            ws_path="/rpc",
            bearer_token=bearer_token,
            is_custom_remote=False,
        )

    if remote.startswith("unix://"):
        raw_sock = remote[len("unix://") :]
        if not raw_sock.startswith("/"):
            raise ValidationError(
                f"Invalid unix endpoint {remote!r}. Expected 'unix://' or 'unix:///absolute/path'."
            )
        return DaemonEndpoint(
            transport="unix",
            address=raw_sock,
            host_header="localhost",
            ws_path="/rpc",
            bearer_token=bearer_token,
            is_custom_remote=True,
        )

    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValidationError(
            f"Unsupported --remote address {remote!r}. Use ws://host:port[/path], wss://host:port[/path], or unix:///path."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"Invalid port in --remote address {remote!r}.") from exc
    resolved_port = port if port is not None else (443 if parsed.scheme == "wss" else 80)
    host_only = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default_port = 443 if parsed.scheme == "wss" else 80
    host_header = host_only if resolved_port == default_port else f"{host_only}:{resolved_port}"
    ws_path = parsed.path or "/rpc"
    if parsed.query:
        ws_path = f"{ws_path}?{parsed.query}"
    return DaemonEndpoint(
        transport="tls" if parsed.scheme == "wss" else "tcp",
        address=parsed.hostname,
        host_header=host_header,
        ws_path=ws_path,
        port=resolved_port,
        bearer_token=bearer_token,
        is_custom_remote=True,
    )


def _is_default_local_daemon_endpoint(
    endpoint: DaemonEndpoint, codex_home: Path | None = None
) -> bool:
    if endpoint.transport != "unix" or endpoint.is_custom_remote:
        return False
    home = codex_home if codex_home is not None else _resolve_codex_home()
    default_sock = home / "app-server-control" / "app-server-control.sock"
    return Path(endpoint.address) == default_sock


def _ensure_local_daemon_running(
    endpoint: DaemonEndpoint, codex_home: Path | None = None
) -> bool:
    if not _is_default_local_daemon_endpoint(endpoint, codex_home=codex_home):
        return False
    binary, _ = _resolve_codex_binary()
    if not binary:
        return False
    env: dict[str, str] | None = None
    if codex_home is not None and os.environ.get("CODEX_HOME") != str(codex_home):
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
    try:
        res = subprocess.run(
            [binary, "app-server", "daemon", "start"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


def _connect_daemon_rpc(
    endpoint: DaemonEndpoint,
    connect_timeout: float,
    auto_start: bool,
    remote: str | None = None,
    remote_auth_token_env: str | None = None,
    codex_home: Path | None = None,
) -> tuple[AppServerRpcClient, DaemonEndpoint]:
    try:
        return AppServerRpcClient(endpoint, connect_timeout=connect_timeout), endpoint
    except DaemonOfflineError:
        if not auto_start or not _ensure_local_daemon_running(endpoint, codex_home=codex_home):
            raise
    resolved = resolve_daemon_endpoint(
        remote=remote,
        remote_auth_token_env=remote_auth_token_env,
        codex_home=codex_home,
    )
    if resolved.transport == "unix" and not Path(resolved.address).exists():
        wait_until = time.monotonic() + 0.25
        while time.monotonic() < wait_until:
            time.sleep(0.02)
            resolved = resolve_daemon_endpoint(
                remote=remote,
                remote_auth_token_env=remote_auth_token_env,
                codex_home=codex_home,
            )
            if Path(resolved.address).exists():
                break
    return AppServerRpcClient(resolved, connect_timeout=connect_timeout), resolved


class Rfc6455Client:
    def __init__(self, endpoint: DaemonEndpoint, connect_timeout: float = 5.0) -> None:
        self.endpoint = endpoint
        self.sock = self._connect_socket(endpoint, connect_timeout)
        self.selector = selectors.DefaultSelector()
        self._rx_buf = bytearray()
        self._closed = False
        self.max_outbound_frame_bytes = DEFAULT_MAX_WS_FRAME_BYTES
        try:
            self.sock.setblocking(False)
            self.selector.register(self.sock, selectors.EVENT_READ)
            self._perform_handshake(time.monotonic() + connect_timeout)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _connect_socket(endpoint: DaemonEndpoint, timeout: float) -> socket.socket:
        if endpoint.transport == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise DaemonOfflineError(
                    "Unix domain sockets (AF_UNIX) are unavailable on this platform; pass --remote ws://host:port.",
                    endpoint.display(),
                )
            sock_path = Path(endpoint.address)
            if not sock_path.exists():
                raise DaemonOfflineError(
                    f"Codex daemon control socket not found at {sock_path}.",
                    endpoint.display(),
                )
            real_target = os.path.realpath(str(sock_path))
            raw_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw_sock.settimeout(timeout)
            try:
                raw_sock.connect(real_target)
            except OSError as exc:
                raw_sock.close()
                raise DaemonOfflineError(
                    f"Cannot connect to Codex daemon socket {sock_path}: {exc}",
                    endpoint.display(),
                ) from exc
            return raw_sock

        assert endpoint.port is not None
        try:
            tcp_sock = socket.create_connection((endpoint.address, endpoint.port), timeout=timeout)
        except OSError as exc:
            raise DaemonOfflineError(
                f"Cannot connect to remote Codex app-server {endpoint.display()}: {exc}",
                endpoint.display(),
            ) from exc
        if endpoint.transport == "tls":
            try:
                ctx = ssl.create_default_context()
                return ctx.wrap_socket(tcp_sock, server_hostname=endpoint.address)
            except OSError as exc:
                tcp_sock.close()
                raise DaemonOfflineError(
                    f"TLS handshake failed for {endpoint.display()}: {exc}",
                    endpoint.display(),
                ) from exc
        return tcp_sock

    def _perform_handshake(self, deadline: float) -> None:
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        headers = [
            f"GET {self.endpoint.ws_path} HTTP/1.1",
            f"Host: {self.endpoint.host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {sec_key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.endpoint.bearer_token:
            headers.append(f"Authorization: Bearer {self.endpoint.bearer_token}")
        raw_req = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        self._write_all(raw_req, deadline)

        while b"\r\n\r\n" not in self._rx_buf:
            if len(self._rx_buf) > MAX_WS_HANDSHAKE_BYTES:
                raise DriverRuntimeError("WebSocket HTTP upgrade header exceeded 64 KiB.")
            self._fill_rx_once(deadline)

        header_end = self._rx_buf.index(b"\r\n\r\n")
        raw_head = bytes(self._rx_buf[:header_end]).decode("iso-8859-1", errors="replace")
        del self._rx_buf[: header_end + 4]

        lines = raw_head.split("\r\n")
        status_line = lines[0] if lines else ""
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise DriverRuntimeError(f"WebSocket upgrade rejected by daemon: {status_line!r}")

        parsed_headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                parsed_headers[k.strip().lower()] = v.strip()

        accept_val = parsed_headers.get("sec-websocket-accept")
        if accept_val is not None and accept_val != expected_accept:
            raise DriverRuntimeError("WebSocket Sec-WebSocket-Accept mismatch during handshake.")

        max_bytes_raw = parsed_headers.get("x-codex-websocket-max-unfragmented-message-bytes")
        if max_bytes_raw:
            with contextlib.suppress(ValueError):
                parsed_max = int(max_bytes_raw)
                if parsed_max > 0:
                    self.max_outbound_frame_bytes = parsed_max

    def _wait_io(self, event_mask: int, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting on Codex app-server WebSocket.")
        self.selector.modify(self.sock, event_mask)
        try:
            ready = self.selector.select(remaining)
        finally:
            if not self._closed:
                with contextlib.suppress(OSError, KeyError, ValueError):
                    self.selector.modify(self.sock, selectors.EVENT_READ)
        if not ready:
            raise TimeoutError("Timed out waiting on Codex app-server WebSocket.")

    def _write_all(self, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        sent_total = 0
        length = len(data)
        while sent_total < length:
            try:
                sent = self.sock.send(view[sent_total:])
                if sent == 0:
                    raise WebSocketClosedError("WebSocket closed while sending frame.")
                sent_total += sent
            except (BlockingIOError, ssl.SSLWantWriteError):
                self._wait_io(selectors.EVENT_WRITE, deadline)
            except ssl.SSLWantReadError:
                self._wait_io(selectors.EVENT_READ, deadline)
            except OSError as exc:
                raise WebSocketClosedError(f"WebSocket write failed: {exc}") from exc

    def _fill_rx_once(self, deadline: float) -> None:
        while True:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WebSocketClosedError("Codex app-server closed the WebSocket connection.")
                self._rx_buf.extend(chunk)
                if isinstance(self.sock, ssl.SSLSocket):
                    while self.sock.pending() > 0:
                        extra = self.sock.recv(65536)
                        if not extra:
                            break
                        self._rx_buf.extend(extra)
                return
            except (BlockingIOError, ssl.SSLWantReadError):
                self._wait_io(selectors.EVENT_READ, deadline)
            except ssl.SSLWantWriteError:
                self._wait_io(selectors.EVENT_WRITE, deadline)
            except OSError as exc:
                raise WebSocketClosedError(f"WebSocket read failed: {exc}") from exc

    def _encode_frame(self, opcode: int, payload: bytes) -> bytes:
        length = len(payload)
        if opcode == 0x1 and length > self.max_outbound_frame_bytes:
            raise ValidationError(
                f"Outbound JSON-RPC message ({length} bytes) exceeds daemon frame limit ({self.max_outbound_frame_bytes} bytes)."
            )
        mask = os.urandom(4)
        first_byte = 0x80 | (opcode & 0x0F)
        if length < 126:
            header = bytes([first_byte, 0x80 | length])
        elif length < 65536:
            header = bytes([first_byte, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([first_byte, 0x80 | 127]) + struct.pack(">Q", length)
        masked_payload = _xor_mask(payload, mask)
        return header + mask + masked_payload

    def send_frame(self, opcode: int, payload: bytes, deadline: float) -> None:
        self._write_all(self._encode_frame(opcode, payload), deadline)

    def send_frames(self, frames: list[tuple[int, bytes]], deadline: float) -> None:
        if not frames:
            return
        combined = bytearray()
        for opcode, payload in frames:
            combined.extend(self._encode_frame(opcode, payload))
        self._write_all(bytes(combined), deadline)

    def _try_extract_frame(self) -> tuple[bool, int, bytes] | None:
        buf_len = len(self._rx_buf)
        if buf_len < 2:
            return None
        b0 = self._rx_buf[0]
        b1 = self._rx_buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            if buf_len < 4:
                return None
            length = struct.unpack(">H", self._rx_buf[2:4])[0]
            offset = 4
        elif length == 127:
            if buf_len < 10:
                return None
            length = struct.unpack(">Q", self._rx_buf[2:10])[0]
            offset = 10
        if length > MAX_INBOUND_WS_MESSAGE_BYTES:
            raise DriverRuntimeError(f"Inbound WebSocket frame ({length} bytes) exceeds safety limit.")
        mask_len = 4 if masked else 0
        total_needed = offset + mask_len + length
        if buf_len < total_needed:
            return None
        mask_bytes = bytes(self._rx_buf[offset : offset + 4]) if masked else b""
        payload = bytes(self._rx_buf[offset + mask_len : total_needed])
        del self._rx_buf[:total_needed]
        if masked:
            payload = _xor_mask(payload, mask_bytes)
        return fin, opcode, payload

    def recv_text(self, deadline: float) -> str:
        fragments: list[bytes] = []
        total_bytes = 0
        expecting_continuation = False
        while True:
            frame = self._try_extract_frame()
            if frame is None:
                self._fill_rx_once(deadline)
                continue
            fin, opcode, payload = frame
            if opcode == 0x9:
                self.send_frame(0xA, payload[:125], min(deadline, time.monotonic() + 2.0))
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                with contextlib.suppress(Exception):
                    self.send_frame(0x8, b"", time.monotonic() + 1.0)
                raise WebSocketClosedError("Codex app-server closed the WebSocket session.")
            if opcode == 0x1:
                if expecting_continuation:
                    raise DriverRuntimeError("Received unexpected new text frame mid-fragmentation.")
                fragments.append(payload)
                total_bytes += len(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
                expecting_continuation = True
            elif opcode == 0x0:
                if not expecting_continuation:
                    raise DriverRuntimeError("Received unexpected WebSocket continuation frame.")
                fragments.append(payload)
                total_bytes += len(payload)
                if total_bytes > MAX_INBOUND_WS_MESSAGE_BYTES:
                    raise DriverRuntimeError("Fragmented WebSocket message exceeded safety limit.")
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")
            else:
                raise DriverRuntimeError(f"Unsupported WebSocket frame opcode: {opcode:#x}")

    def wait_readable(self, timeout: float) -> bool:
        if self._rx_buf:
            return True
        if timeout <= 0:
            return False
        return bool(self.selector.select(timeout))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.send_frame(0x8, b"", time.monotonic() + 0.5)
        with contextlib.suppress(Exception):
            self.selector.unregister(self.sock)
        with contextlib.suppress(Exception):
            self.selector.close()
        with contextlib.suppress(Exception):
            self.sock.close()


class AppServerRpcClient:
    def __init__(self, endpoint: DaemonEndpoint, connect_timeout: float = 5.0) -> None:
        self.ws = Rfc6455Client(endpoint, connect_timeout=connect_timeout)
        self._next_id = 1
        self._pending: deque[dict[str, Any]] = deque()
        self._log_sink: Callable[[dict[str, Any]], None] | None = None

    def set_log_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        self._log_sink = sink
        if sink is not None:
            for buffered in self._pending:
                sink(buffered)

    def _record(self, msg: dict[str, Any]) -> None:
        if self._log_sink is not None:
            self._log_sink(msg)

    def send_notification(self, method: str, params: dict[str, Any] | None, deadline: float) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.ws.send_frame(0x1, raw, deadline)

    def request_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        deadline: float,
    ) -> list[dict[str, Any] | JsonRpcError]:
        if not calls:
            return []
        id_to_slot: dict[int, tuple[int, str]] = {}
        outbound_frames: list[tuple[int, bytes]] = []
        for idx, (method, params) in enumerate(calls):
            req_id = self._next_id
            self._next_id += 1
            id_to_slot[req_id] = (idx, method)
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params if params is not None else {},
            }
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            outbound_frames.append((0x1, raw))
        self.ws.send_frames(outbound_frames, deadline)

        results: list[dict[str, Any] | JsonRpcError | None] = [None] * len(calls)
        remaining = len(calls)
        while remaining > 0:
            text = self.ws.recv_text(deadline)
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if (
                isinstance(msg_id, int)
                and not isinstance(msg_id, bool)
                and msg_id in id_to_slot
                and "method" not in msg
                and ("result" in msg or "error" in msg)
            ):
                idx, method = id_to_slot.pop(msg_id)
                if "error" in msg:
                    err_obj = msg.get("error")
                    code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    err_msg = (
                        str(err_obj.get("message") or err_obj)
                        if isinstance(err_obj, dict)
                        else str(err_obj)
                    )
                    err_data = err_obj.get("data") if isinstance(err_obj, dict) else None
                    results[idx] = JsonRpcError(
                        method, code if isinstance(code, int) else None, err_msg, err_data
                    )
                else:
                    res = msg.get("result")
                    results[idx] = res if isinstance(res, dict) else {"value": res}
                remaining -= 1
            else:
                self._record(msg)
                self._pending.append(msg)
        return [r for r in results if r is not None]

    def request(self, method: str, params: dict[str, Any] | None, deadline: float) -> dict[str, Any]:
        outcome = self.request_batch(
            [(method, params if params is not None else {})],
            deadline=deadline,
        )[0]
        if isinstance(outcome, JsonRpcError):
            raise outcome
        return outcome

    def initialize(self, deadline: float) -> dict[str, Any]:
        res = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": NON_ORIGINATING_CLIENT_NAME,
                    "title": "codex-use",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": list(OPT_OUT_NOTIFICATION_METHODS),
                },
            },
            deadline=deadline,
        )
        self.send_notification("initialized", None, deadline=deadline)
        return res

    def next_message(self, deadline: float) -> dict[str, Any]:
        if self._pending:
            return self._pending.popleft()
        while True:
            text = self.ws.recv_text(deadline)
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._record(msg)
                return msg

    def has_pending(self) -> bool:
        return bool(self._pending)

    def has_pending_turn_completed(self, thread_id: str) -> bool:
        for msg in self._pending:
            if msg.get("method") == "turn/completed":
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                if params.get("threadId") == thread_id:
                    return True
        return False

    def wait_readable(self, timeout: float) -> bool:
        if self._pending:
            return True
        return self.ws.wait_readable(timeout)

    def unsubscribe_quietly(self, thread_id: str) -> None:
        with contextlib.suppress(Exception):
            self.request(
                "thread/unsubscribe",
                {"threadId": thread_id},
                deadline=time.monotonic() + 2.0,
            )

    def archive_quietly(self, thread_id: str) -> None:
        with contextlib.suppress(Exception):
            self.request(
                "thread/archive",
                {"threadId": thread_id},
                deadline=time.monotonic() + 2.0,
            )

    def close(self) -> None:
        self.ws.close()


def _normalize_source_label(raw_source: Any) -> str:
    if isinstance(raw_source, str) and raw_source:
        return raw_source
    if isinstance(raw_source, dict):
        if "custom" in raw_source and isinstance(raw_source["custom"], str):
            return f"custom:{raw_source['custom']}"
        if "subAgent" in raw_source:
            return "subAgent"
    return "unknown"


def _is_subagent_or_ephemeral_thread(thread_obj: dict[str, Any]) -> bool:
    if bool(thread_obj.get("ephemeral")):
        return True
    if thread_obj.get("parentThreadId") is not None:
        return True
    raw_source = thread_obj.get("source")
    if isinstance(raw_source, dict) and "subAgent" in raw_source:
        return True
    return False


def _parse_loaded_tui_thread(thread_obj: dict[str, Any]) -> LoadedTuiThread | None:
    tid = thread_obj.get("id")
    raw_cwd = thread_obj.get("cwd")
    if not isinstance(tid, str) or not tid or not isinstance(raw_cwd, str) or not raw_cwd:
        return None
    status_raw = thread_obj.get("status")
    if isinstance(status_raw, dict):
        status_type = str(status_raw.get("type") or "unknown")
        flags_raw = status_raw.get("activeFlags")
        flags = (
            tuple(str(x) for x in flags_raw if isinstance(x, str))
            if isinstance(flags_raw, list)
            else ()
        )
    elif isinstance(status_raw, str):
        status_type = status_raw
        flags = ()
    else:
        status_type = "unknown"
        flags = ()
    if status_type == "notLoaded":
        return None

    cwd_path = Path(raw_cwd)
    resolved_cwd = str(cwd_path.resolve()) if cwd_path.exists() else os.path.realpath(raw_cwd)
    git_ctx = inspect_git_context(Path(resolved_cwd))
    git_root = str(git_ctx.git_root) if git_ctx.is_git_repo and git_ctx.git_root else None
    raw_name = thread_obj.get("name")
    title = (
        raw_name.strip()
        if isinstance(raw_name, str) and raw_name.strip()
        else _preview(thread_obj.get("preview") or "", limit=80)
    )
    updated_at = thread_obj.get("updatedAt")
    created_at = thread_obj.get("createdAt")
    return LoadedTuiThread(
        thread_id=tid,
        title=title,
        cwd=raw_cwd,
        resolved_cwd=resolved_cwd,
        git_root=git_root,
        status=status_type,
        active_flags=flags,
        model=thread_obj.get("model") if isinstance(thread_obj.get("model"), str) else None,
        originator=(
            thread_obj.get("originator") if isinstance(thread_obj.get("originator"), str) else None
        ),
        source=_normalize_source_label(thread_obj.get("source")),
        updated_at=updated_at if isinstance(updated_at, int) else 0,
        created_at=created_at if isinstance(created_at, int) else 0,
    )


def _cwd_matches_target(candidate_cwd: str, target_resolved: Path, target_git_root: Path | None) -> bool:
    cand_path = Path(candidate_cwd)
    cand_resolved = cand_path.resolve() if cand_path.exists() else Path(os.path.realpath(candidate_cwd))
    if cand_resolved == target_resolved:
        return True
    if target_git_root is not None:
        cand_git = inspect_git_context(cand_resolved)
        if cand_git.is_git_repo and cand_git.git_root == target_git_root:
            return True
    return False


def _thread_matches_target_cwd(
    thread: LoadedTuiThread, target_resolved: Path, target_git_root: Path | None
) -> bool:
    if Path(thread.resolved_cwd) == target_resolved:
        return True
    if target_git_root is not None and thread.git_root == str(target_git_root):
        return True
    return False


def _is_writer_lock_held(lock_path: Path) -> bool:
    if fcntl is None or not lock_path.is_file():
        return False
    fd: int | None = None
    for open_flags in (os.O_RDWR, os.O_RDONLY):
        try:
            fd = os.open(str(lock_path), open_flags)
            break
        except OSError:
            continue
    if fd is None:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _sqlite_state_sort_key(path: Path) -> tuple[int, float]:
    match = re.search(r"(\d+)", path.stem)
    version_num = int(match.group(1)) if match else -1
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (version_num, mtime)


def _lookup_thread_cwd_from_codex_home(
    codex_home: Path,
    thread_id: str,
    rpc: AppServerRpcClient | None = None,
    deadline: float | None = None,
) -> str | None:
    if rpc is not None and deadline is not None:
        with contextlib.suppress(Exception):
            read_res = rpc.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
                deadline=min(deadline, time.monotonic() + 2.0),
            )
            th = read_res.get("thread") if isinstance(read_res.get("thread"), dict) else read_res
            if isinstance(th, dict) and isinstance(th.get("cwd"), str) and th["cwd"]:
                return th["cwd"]

    for db_path in sorted(codex_home.glob("state*.sqlite"), key=_sqlite_state_sort_key, reverse=True):
        with contextlib.suppress(sqlite3.Error, OSError):
            uri = f"file:{urllib.parse.quote(str(db_path.resolve()))}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=0.5)
            try:
                row = con.execute("SELECT cwd FROM threads WHERE id = ? LIMIT 1", (thread_id,)).fetchone()
                if row and isinstance(row[0], str) and row[0]:
                    return row[0]
            finally:
                con.close()

    sessions_dir = codex_home / "sessions"
    if sessions_dir.is_dir():
        for rollout in sessions_dir.rglob(f"*{thread_id}.jsonl"):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                with rollout.open("r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                meta = json.loads(first_line)
                if isinstance(meta, dict):
                    payload = meta.get("payload")
                    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                        return payload["cwd"]
                    if isinstance(meta.get("cwd"), str):
                        return meta["cwd"]
    return None


def _find_conflicting_writer_locks(
    codex_home: Path,
    target_cwd: Path | None,
    session_id: str | None,
    excluded_ids: set[str],
    rpc: AppServerRpcClient | None = None,
    deadline: float | None = None,
) -> list[str]:
    locks_dir = codex_home / "thread-writer-locks"
    if not locks_dir.is_dir():
        return []

    if session_id:
        if session_id in excluded_ids:
            return []
        lock_file = locks_dir / f"{session_id}.lock"
        return [session_id] if _is_writer_lock_held(lock_file) else []

    held_ids: list[str] = []
    for lock_file in sorted(locks_dir.glob("*.lock")):
        if lock_file.name.startswith("."):
            continue
        tid = lock_file.stem
        if not tid or tid in excluded_ids:
            continue
        if _is_writer_lock_held(lock_file):
            held_ids.append(tid)

    if not held_ids or target_cwd is None:
        return held_ids

    target_resolved = target_cwd.resolve()
    target_git_ctx = inspect_git_context(target_resolved)
    target_git_root = target_git_ctx.git_root if target_git_ctx.is_git_repo else None
    matched_ids: list[str] = []
    for tid in held_ids:
        locked_cwd = _lookup_thread_cwd_from_codex_home(codex_home, tid, rpc=rpc, deadline=deadline)
        if locked_cwd and _cwd_matches_target(locked_cwd, target_resolved, target_git_root):
            matched_ids.append(tid)
    return matched_ids


def _list_daemon_loaded_thread_ids(
    rpc: AppServerRpcClient, deadline: float
) -> list[str]:
    loaded_ids: list[str] = []
    seen_ids: set[str] = set()
    cursor: str | None = None
    while True:
        res = rpc.request(
            "thread/loaded/list",
            {"cursor": cursor, "limit": 100},
            deadline=deadline,
        )
        batch = res.get("data")
        if isinstance(batch, list):
            for entry in batch:
                tid = entry.get("id") if isinstance(entry, dict) else entry
                if isinstance(tid, str) and tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    loaded_ids.append(tid)
        next_cursor = res.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor
    return loaded_ids


def _list_daemon_loaded_threads(
    rpc: AppServerRpcClient, deadline: float
) -> tuple[set[str], list[LoadedTuiThread]]:
    loaded_ids = _list_daemon_loaded_thread_ids(rpc, deadline)
    seen_ids = set(loaded_ids)
    read_calls = [
        ("thread/read", {"threadId": tid, "includeTurns": False}) for tid in loaded_ids
    ]
    read_results = rpc.request_batch(read_calls, deadline=deadline)

    threads: list[LoadedTuiThread] = []
    for read_res in read_results:
        if isinstance(read_res, JsonRpcError):
            continue
        th_obj = read_res.get("thread") if isinstance(read_res.get("thread"), dict) else read_res
        if not isinstance(th_obj, dict) or _is_subagent_or_ephemeral_thread(th_obj):
            continue
        parsed = _parse_loaded_tui_thread(th_obj)
        if parsed is not None:
            threads.append(parsed)
    threads.sort(key=lambda t: (t.updated_at, t.created_at, t.thread_id), reverse=True)
    return seen_ids, threads


def _resolve_thread_with_client(
    rpc: AppServerRpcClient,
    endpoint: DaemonEndpoint,
    codex_home: Path,
    target_cwd: Path | None,
    session_id: str | None,
    select_latest: bool,
    deadline: float,
) -> ThreadResolution:
    if session_id:
        loaded_id_set = set(_list_daemon_loaded_thread_ids(rpc, deadline))
        matched_thread: LoadedTuiThread | None = None
        if session_id in loaded_id_set:
            with contextlib.suppress(JsonRpcError):
                read_res = rpc.request(
                    "thread/read",
                    {"threadId": session_id, "includeTurns": False},
                    deadline=deadline,
                )
                th_obj = (
                    read_res.get("thread") if isinstance(read_res.get("thread"), dict) else read_res
                )
                if isinstance(th_obj, dict):
                    matched_thread = _parse_loaded_tui_thread(th_obj)
        if matched_thread is not None:
            return ThreadResolution(
                kind="matched",
                thread=matched_thread,
                candidates=(matched_thread,),
                endpoint=endpoint.display(),
            )
        locked = _find_conflicting_writer_locks(
            codex_home,
            target_cwd=target_cwd,
            session_id=session_id,
            excluded_ids=loaded_id_set,
            rpc=rpc,
            deadline=deadline,
        )
        if locked:
            return ThreadResolution(
                kind="desktop_writer_lock",
                locked_thread_ids=tuple(locked),
                endpoint=endpoint.display(),
                detail=(
                    f"Session {session_id} is owned by Codex Desktop or VS Code "
                    "(Layer B IPC, unsupported by CLI daemon attach)."
                ),
            )
        return ThreadResolution(
            kind="not_found",
            endpoint=endpoint.display(),
            detail=f"Session {session_id} is not loaded on the Codex Layer A daemon.",
        )

    loaded_id_set, loaded_threads = _list_daemon_loaded_threads(rpc, deadline)

    assert target_cwd is not None
    target_resolved = target_cwd.resolve()
    target_git_ctx = inspect_git_context(target_resolved)
    target_git_root = target_git_ctx.git_root if target_git_ctx.is_git_repo else None
    matches = [
        t
        for t in loaded_threads
        if _thread_matches_target_cwd(t, target_resolved, target_git_root)
    ]
    if len(matches) == 1:
        return ThreadResolution(
            kind="matched",
            thread=matches[0],
            candidates=(matches[0],),
            endpoint=endpoint.display(),
        )
    if len(matches) > 1:
        if select_latest:
            return ThreadResolution(
                kind="matched",
                thread=matches[0],
                candidates=tuple(matches),
                endpoint=endpoint.display(),
            )
        return ThreadResolution(
            kind="ambiguous",
            candidates=tuple(matches),
            endpoint=endpoint.display(),
            detail=(
                f"Found {len(matches)} loaded TUI sessions matching {target_resolved}. "
                "Pass --session <thread_id> or --latest."
            ),
        )

    locked = _find_conflicting_writer_locks(
        codex_home,
        target_cwd=target_resolved,
        session_id=None,
        excluded_ids=loaded_id_set,
        rpc=rpc,
        deadline=deadline,
    )
    if locked:
        return ThreadResolution(
            kind="desktop_writer_lock",
            locked_thread_ids=tuple(locked),
            endpoint=endpoint.display(),
            detail=(
                f"Active session(s) {', '.join(locked)} for {target_resolved} are owned by Codex Desktop "
                "or VS Code (Layer B IPC, unsupported by CLI daemon attach)."
            ),
        )
    return ThreadResolution(
        kind="not_found",
        endpoint=endpoint.display(),
        detail=f"No loaded terminal `codex` TUI session matched {target_resolved} on the Layer A daemon.",
    )


def _extract_status_block_kind(status_obj: Any) -> Literal["approval_required", "user_input_required"] | None:
    if not isinstance(status_obj, dict) or status_obj.get("type") != "active":
        return None
    flags = status_obj.get("activeFlags")
    if not isinstance(flags, list):
        return None
    if "waitingOnUserInput" in flags:
        return "user_input_required"
    if "waitingOnApproval" in flags:
        return "approval_required"
    return None


def _classify_server_interactive_request(
    msg: dict[str, Any], thread_id: str
) -> tuple[Literal["approval_required", "user_input_required"], dict[str, Any], str] | None:
    if "id" not in msg or not isinstance(msg.get("method"), str):
        return None
    method = msg["method"]
    if method not in APPROVAL_REQUEST_METHODS and method not in USER_INPUT_REQUEST_METHODS:
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    msg_tid = params.get("threadId") or params.get("conversationId")
    if isinstance(msg_tid, str) and msg_tid != thread_id:
        return None
    kind: Literal["approval_required", "user_input_required"] = (
        "user_input_required" if method in USER_INPUT_REQUEST_METHODS else "approval_required"
    )
    req_info = {"id": msg.get("id"), "method": method, "params": params}
    return kind, req_info, _format_interactive_block_message(method, params)


def _drain_companion_approval_request(
    rpc: AppServerRpcClient,
    thread_id: str,
    fallback_kind: Literal["approval_required", "user_input_required"],
    deadline: float,
) -> tuple[Literal["approval_required", "user_input_required"], dict[str, Any] | None, str]:
    grace_deadline = min(deadline, time.monotonic() + APPROVAL_DETAIL_GRACE_SECONDS)
    while rpc.wait_readable(max(0.0, grace_deadline - time.monotonic())):
        try:
            msg = rpc.next_message(grace_deadline)
        except (TimeoutError, WebSocketClosedError):
            break
        classified = _classify_server_interactive_request(msg, thread_id)
        if classified is not None:
            return classified
    fallback_msg = (
        f"{fallback_kind}: Live TUI session is paused ({fallback_kind.replace('_', ' ')}) in terminal."
    )
    return fallback_kind, None, fallback_msg


def _harvest_completed_turn_from_read(
    rpc: AppServerRpcClient,
    thread_id: str,
    target_turn_id: str | None,
    client_msg_id: str,
    preview_limit: int,
    deadline: float,
) -> tuple[AttachOutcomeStatus | None, str | None, list[CommandRecord], list[str], list[str], str]:
    commands: list[CommandRecord] = []
    changed_files: list[str] = []
    errors: list[str] = []
    final_message = ""
    outcome: AttachOutcomeStatus | None = None
    harvested_turn_id: str | None = None
    with contextlib.suppress(Exception):
        read_res = rpc.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
            deadline=min(deadline, time.monotonic() + 5.0),
        )
        th_obj = read_res.get("thread") if isinstance(read_res.get("thread"), dict) else {}
        turns = th_obj.get("turns") if isinstance(th_obj.get("turns"), list) else []
        chosen_turn: dict[str, Any] | None = None
        if target_turn_id:
            for t in turns:
                if isinstance(t, dict) and t.get("id") == target_turn_id:
                    chosen_turn = t
                    break
        if chosen_turn is None and client_msg_id:
            for t in reversed(turns):
                if not isinstance(t, dict):
                    continue
                for item in t.get("items") or []:
                    if isinstance(item, dict) and item.get("type") in {"userMessage", "user_message"}:
                        if (item.get("clientId") or item.get("clientUserMessageId")) == client_msg_id:
                            chosen_turn = t
                            break
                if chosen_turn is not None:
                    break
        if chosen_turn is None and turns and isinstance(turns[-1], dict):
            chosen_turn = turns[-1]

        if chosen_turn is not None:
            raw_status = chosen_turn.get("status")
            if raw_status in {"completed", "failed", "interrupted"}:
                if isinstance(chosen_turn.get("id"), str) and chosen_turn["id"]:
                    harvested_turn_id = chosen_turn["id"]
                for item in chosen_turn.get("items") or []:
                    if isinstance(item, dict):
                        _ingest_app_server_item(item, commands, changed_files, errors, preview_limit)
                        if item.get("type") in {"agentMessage", "agent_message"}:
                            text = _extract_agent_text(item)
                            if text:
                                final_message = text
                if raw_status == "failed":
                    outcome = "failed"
                    err_text = (
                        _extract_error_text(chosen_turn.get("error"))
                        or "turn_failed: Turn failed on Codex Layer A daemon."
                    )
                    _append_unique(errors, err_text)
                elif raw_status == "interrupted":
                    outcome = "interrupted"
                    _append_unique(errors, "turn_interrupted: Turn was interrupted in live TUI.")
                else:
                    outcome = "completed"
    return outcome, harvested_turn_id, commands, changed_files, errors, final_message


def _is_unmaterialized_rollout_error(exc: JsonRpcError) -> bool:
    lowered = exc.rpc_message.lower()
    return "no rollout found" in lowered or ("rollout" in lowered and "is empty" in lowered)


def _build_thread_start_params(
    cwd: Path,
    mode: WriteMode,
    model: str | None = None,
    effort: str | None = None,
    fast: bool = False,
    with_mcp: bool = True,
    approval_policy: str | None = "never",
) -> dict[str, Any]:
    sandbox = "read-only" if mode == "scout" else "workspace-write"
    params: dict[str, Any] = {
        "cwd": str(cwd),
        "sandbox": sandbox,
    }
    if approval_policy is not None:
        params["approvalPolicy"] = approval_policy
    if model is not None:
        params["model"] = model
    if fast:
        params["serviceTier"] = _validate_codex_service_tier("priority")
    config: dict[str, Any] = {}
    if effort is not None:
        config["model_reasoning_effort"] = effort
    if not with_mcp:
        config["mcp_servers"] = {}
    if config:
        params["config"] = config
    return params


def _synthesize_started_thread(
    start_res: dict[str, Any],
    fallback_cwd: Path,
    model: str | None = None,
) -> LoadedTuiThread:
    th_obj = start_res.get("thread") if isinstance(start_res.get("thread"), dict) else start_res
    resolved_model = (
        start_res.get("model")
        if isinstance(start_res.get("model"), str) and start_res.get("model")
        else model
    )
    if isinstance(th_obj, dict):
        parsed = _parse_loaded_tui_thread(th_obj)
        if parsed is not None:
            if parsed.model is None and resolved_model is not None:
                return dataclasses.replace(parsed, model=resolved_model)
            return parsed
        tid = th_obj.get("id")
    else:
        tid = None
    if not isinstance(tid, str) or not tid:
        raw_tid = start_res.get("threadId")
        tid = raw_tid if isinstance(raw_tid, str) and raw_tid else None
    if not tid:
        raise DriverRuntimeError("thread/start did not return a valid thread id.")
    resolved_cwd_path = fallback_cwd.resolve()
    git_ctx = inspect_git_context(resolved_cwd_path)
    return LoadedTuiThread(
        thread_id=tid,
        title="",
        cwd=str(resolved_cwd_path),
        resolved_cwd=str(resolved_cwd_path),
        git_root=str(git_ctx.git_root) if git_ctx.is_git_repo and git_ctx.git_root else None,
        status="idle",
        active_flags=(),
        model=resolved_model,
        originator=NON_ORIGINATING_CLIENT_NAME,
        source="appServer",
        updated_at=int(time.time()),
        created_at=int(time.time()),
    )


def _dispatch_and_observe_turn(
    rpc: AppServerRpcClient,
    thread: LoadedTuiThread,
    req: AttachRequest,
    prompt: str,
    schema_dict: dict[str, Any] | None,
    preview_limit: int,
    deadline: float,
    client_msg_id: str | None = None,
    already_subscribed: bool = False,
    force_turn_start: bool = False,
) -> DaemonTurnObservation:
    thread_id = thread.thread_id
    if client_msg_id is None:
        client_msg_id = str(uuid.uuid4())
    input_items: list[dict[str, Any]] = [
        {"type": "text", "text": prompt, "text_elements": []},
    ]
    for img_path in req.images:
        input_items.append({"type": "localImage", "path": str(img_path), "detail": None})

    commands: list[CommandRecord] = []
    changed_files: list[str] = []
    errors: list[str] = []
    final_message = ""
    usage: dict[str, Any] | None = None
    event_count = 0
    target_turn_id: str | None = None
    dispatch_method: Literal["thread/queue/add", "turn/start"] = "thread/queue/add"
    queued_behind_active_turn = False
    subscribed = already_subscribed

    try:
        current_status: dict[str, Any] = {
            "type": thread.status,
            "activeFlags": list(thread.active_flags),
        }
        if not subscribed:
            try:
                resume_res = rpc.request(
                    "thread/resume",
                    {"threadId": thread_id, "excludeTurns": True},
                    deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT),
                )
                subscribed = True
                th_after_resume = resume_res.get("thread")
                if isinstance(th_after_resume, dict) and isinstance(th_after_resume.get("status"), dict):
                    current_status = th_after_resume["status"]
            except JsonRpcError as exc:
                if not _is_unmaterialized_rollout_error(exc):
                    raise DriverRuntimeError(str(exc)) from exc

        if subscribed:
            while rpc.has_pending():
                queued_msg = rpc.next_message(deadline)
                event_count += 1
                classified = _classify_server_interactive_request(queued_msg, thread_id)
                if classified is not None and not req.wait_for_approval:
                    b_kind, req_info, b_msg = classified
                    _append_unique(errors, b_msg)
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=None,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=False,
                        status=b_kind,
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        approval_request=req_info,
                        event_count=event_count,
                    )
                q_method = queued_msg.get("method")
                if not isinstance(q_method, str):
                    continue
                q_params = queued_msg.get("params") if isinstance(queued_msg.get("params"), dict) else {}
                q_tid = q_params.get("threadId") or (
                    q_params.get("thread", {}).get("id")
                    if isinstance(q_params.get("thread"), dict)
                    else None
                )
                if isinstance(q_tid, str) and q_tid != thread_id:
                    continue
                if q_method == "thread/closed":
                    _append_unique(errors, "thread_closed: Live TUI session closed before turn start.")
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=None,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=False,
                        status="thread_closed",
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        event_count=event_count,
                    )
                if q_method == "thread/status/changed" and isinstance(q_params.get("status"), dict):
                    current_status = q_params["status"]
                elif q_method == "turn/completed":
                    current_status = {"type": "idle", "activeFlags": []}

            if not req.wait_for_approval:
                initial_block = _extract_status_block_kind(current_status)
                if initial_block is not None:
                    b_kind, req_info, b_msg = _drain_companion_approval_request(
                        rpc, thread_id, initial_block, deadline
                    )
                    _append_unique(errors, b_msg)
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=None,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=False,
                        status=b_kind,
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        approval_request=req_info,
                        event_count=event_count,
                    )

        is_active = current_status.get("type") == "active"
        has_turn_overrides = (
            force_turn_start
            or req.model is not None
            or req.effort is not None
            or req.fast
            or schema_dict is not None
        )

        if is_active and (req.model is not None or req.effort is not None or req.fast):
            while is_active:
                msg = rpc.next_message(deadline)
                event_count += 1
                classified = _classify_server_interactive_request(msg, thread_id)
                if classified is not None and not req.wait_for_approval:
                    b_kind, req_info, b_msg = classified
                    _append_unique(errors, b_msg)
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=None,
                        client_user_message_id=client_msg_id,
                        dispatch_method="turn/start",
                        queued_behind_active_turn=True,
                        status=b_kind,
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        approval_request=req_info,
                        event_count=event_count,
                    )
                method = msg.get("method")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                if params.get("threadId") != thread_id:
                    continue
                if method == "turn/completed":
                    is_active = False
                elif method == "thread/status/changed":
                    st = params.get("status")
                    block_kind = _extract_status_block_kind(st)
                    if block_kind is not None and not req.wait_for_approval:
                        b_kind, req_info, b_msg = _drain_companion_approval_request(
                            rpc, thread_id, block_kind, deadline
                        )
                        _append_unique(errors, b_msg)
                        return DaemonTurnObservation(
                            thread_id=thread_id,
                            turn_id=None,
                            client_user_message_id=client_msg_id,
                            dispatch_method="turn/start",
                            queued_behind_active_turn=True,
                            status=b_kind,
                            commands=commands,
                            changed_files=changed_files,
                            errors=errors,
                            final_message=final_message,
                            usage=usage,
                            approval_request=req_info,
                            event_count=event_count,
                        )
                    if isinstance(st, dict) and st.get("type") == "idle":
                        is_active = False
                elif method == "thread/closed":
                    _append_unique(errors, "thread_closed: Live TUI session closed before turn start.")
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=None,
                        client_user_message_id=client_msg_id,
                        dispatch_method="turn/start",
                        queued_behind_active_turn=True,
                        status="thread_closed",
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        event_count=event_count,
                    )

        if not is_active and has_turn_overrides:
            dispatch_method = "turn/start"
            queued_behind_active_turn = False
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "clientUserMessageId": client_msg_id,
                "input": input_items,
            }
            if req.model is not None:
                turn_params["model"] = req.model
            if req.effort is not None:
                turn_params["effort"] = req.effort
            if req.fast:
                turn_params["serviceTier"] = _validate_codex_service_tier("priority")
            if schema_dict is not None:
                turn_params["outputSchema"] = schema_dict
            start_res = rpc.request("turn/start", turn_params, deadline=deadline)
            turn_obj = start_res.get("turn") if isinstance(start_res.get("turn"), dict) else {}
            if isinstance(turn_obj.get("id"), str) and turn_obj["id"]:
                target_turn_id = turn_obj["id"]
        else:
            dispatch_method = "thread/queue/add"
            queued_behind_active_turn = is_active
            rpc.request(
                "thread/queue/add",
                {
                    "threadId": thread_id,
                    "clientUserMessageId": client_msg_id,
                    "input": input_items,
                },
                deadline=deadline,
            )

        if not subscribed:
            post_status_type = "active"
            for _ in range(4):
                try:
                    resume_res = rpc.request(
                        "thread/resume",
                        {"threadId": thread_id, "excludeTurns": True},
                        deadline=min(deadline, time.monotonic() + 5.0),
                    )
                    subscribed = True
                    th_after = resume_res.get("thread")
                    if isinstance(th_after, dict) and isinstance(th_after.get("status"), dict):
                        post_status_type = str(th_after["status"].get("type") or "active")
                    break
                except JsonRpcError as exc:
                    if not _is_unmaterialized_rollout_error(exc):
                        raise DriverRuntimeError(str(exc)) from exc
                    rpc.wait_readable(0.05)
            if (
                subscribed
                and post_status_type == "idle"
                and not rpc.has_pending_turn_completed(thread_id)
            ):
                h_outcome, h_tid, h_cmds, h_files, h_errs, h_msg = _harvest_completed_turn_from_read(
                    rpc, thread_id, target_turn_id, client_msg_id, preview_limit, deadline
                )
                if h_outcome is not None:
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=h_tid or target_turn_id,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=queued_behind_active_turn,
                        status=h_outcome,
                        commands=h_cmds,
                        changed_files=h_files,
                        errors=h_errs,
                        final_message=h_msg,
                        usage=usage,
                        event_count=event_count,
                    )

        prior_turn_completed = not queued_behind_active_turn
        candidate_turn_id: str | None = None

        while True:
            try:
                msg = rpc.next_message(deadline)
            except TimeoutError:
                _append_unique(
                    errors,
                    f"timeout: Timed out after {req.timeout:g}s waiting for live TUI turn completion.",
                )
                return DaemonTurnObservation(
                    thread_id=thread_id,
                    turn_id=target_turn_id,
                    client_user_message_id=client_msg_id,
                    dispatch_method=dispatch_method,
                    queued_behind_active_turn=queued_behind_active_turn,
                    status="timeout",
                    commands=commands,
                    changed_files=changed_files,
                    errors=errors,
                    final_message=final_message,
                    usage=usage,
                    event_count=event_count,
                )
            except WebSocketClosedError as exc:
                _append_unique(errors, f"connection_closed: {exc}")
                return DaemonTurnObservation(
                    thread_id=thread_id,
                    turn_id=target_turn_id,
                    client_user_message_id=client_msg_id,
                    dispatch_method=dispatch_method,
                    queued_behind_active_turn=queued_behind_active_turn,
                    status="thread_closed",
                    commands=commands,
                    changed_files=changed_files,
                    errors=errors,
                    final_message=final_message,
                    usage=usage,
                    event_count=event_count,
                )

            event_count += 1
            classified = _classify_server_interactive_request(msg, thread_id)
            if classified is not None:
                if not req.wait_for_approval:
                    b_kind, req_info, b_msg = classified
                    _append_unique(errors, b_msg)
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=target_turn_id,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=queued_behind_active_turn,
                        status=b_kind,
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        approval_request=req_info,
                        event_count=event_count,
                    )
                continue

            method = msg.get("method")
            if not isinstance(method, str):
                continue
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            msg_tid = params.get("threadId") or (
                params.get("thread", {}).get("id") if isinstance(params.get("thread"), dict) else None
            )
            if isinstance(msg_tid, str) and msg_tid != thread_id:
                continue

            if method == "thread/closed":
                _append_unique(errors, "thread_closed: Live TUI session closed before turn finished.")
                return DaemonTurnObservation(
                    thread_id=thread_id,
                    turn_id=target_turn_id,
                    client_user_message_id=client_msg_id,
                    dispatch_method=dispatch_method,
                    queued_behind_active_turn=queued_behind_active_turn,
                    status="thread_closed",
                    commands=commands,
                    changed_files=changed_files,
                    errors=errors,
                    final_message=final_message,
                    usage=usage,
                    event_count=event_count,
                )

            if method == "thread/status/changed":
                block_kind = _extract_status_block_kind(params.get("status"))
                if block_kind is not None and not req.wait_for_approval:
                    b_kind, req_info, b_msg = _drain_companion_approval_request(
                        rpc, thread_id, block_kind, deadline
                    )
                    _append_unique(errors, b_msg)
                    return DaemonTurnObservation(
                        thread_id=thread_id,
                        turn_id=target_turn_id,
                        client_user_message_id=client_msg_id,
                        dispatch_method=dispatch_method,
                        queued_behind_active_turn=queued_behind_active_turn,
                        status=b_kind,
                        commands=commands,
                        changed_files=changed_files,
                        errors=errors,
                        final_message=final_message,
                        usage=usage,
                        approval_request=req_info,
                        event_count=event_count,
                    )
                continue

            if method == "turn/started":
                turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                started_id = turn_obj.get("id")
                if isinstance(started_id, str) and started_id:
                    if target_turn_id is None and (
                        not queued_behind_active_turn or prior_turn_completed
                    ):
                        candidate_turn_id = started_id
                continue

            if method in {"item/started", "item/completed"}:
                item = params.get("item")
                item_turn_id = params.get("turnId")
                if isinstance(item, dict) and item.get("type") in {"userMessage", "user_message"}:
                    item_client_id = item.get("clientId") or item.get("clientUserMessageId")
                    if item_client_id == client_msg_id and isinstance(item_turn_id, str) and item_turn_id:
                        target_turn_id = item_turn_id
                        candidate_turn_id = None
                    elif (
                        item_client_id is None
                        and target_turn_id is None
                        and prior_turn_completed
                        and isinstance(item_turn_id, str)
                        and item_turn_id
                    ):
                        target_turn_id = item_turn_id
                        candidate_turn_id = None
                    elif isinstance(item_client_id, str) and item_client_id != client_msg_id:
                        if target_turn_id == item_turn_id:
                            target_turn_id = None
                        if candidate_turn_id == item_turn_id:
                            candidate_turn_id = None
                        queued_behind_active_turn = True
                        prior_turn_completed = False
                    continue

                effective_turn_id = target_turn_id or candidate_turn_id
                if method == "item/completed" and isinstance(item, dict):
                    is_our_turn = (
                        (effective_turn_id is not None and item_turn_id == effective_turn_id)
                        or (not queued_behind_active_turn and effective_turn_id is None)
                    )
                    if is_our_turn:
                        _ingest_app_server_item(item, commands, changed_files, errors, preview_limit)
                        if item.get("type") in {"agentMessage", "agent_message"}:
                            text = _extract_agent_text(item)
                            if text:
                                final_message = text
                continue

            if method == "thread/tokenUsage/updated":
                effective_turn_id = target_turn_id or candidate_turn_id
                if (
                    (effective_turn_id is not None and params.get("turnId") == effective_turn_id)
                    or (not queued_behind_active_turn and effective_turn_id is None)
                ):
                    tu = params.get("tokenUsage")
                    if isinstance(tu, dict):
                        last_u = tu.get("last")
                        usage = last_u if isinstance(last_u, dict) else tu
                continue

            if method == "error":
                effective_turn_id = target_turn_id or candidate_turn_id
                if not params.get("willRetry", False) and (
                    (effective_turn_id is not None and params.get("turnId") == effective_turn_id)
                    or (not queued_behind_active_turn and effective_turn_id is None)
                ):
                    err_text = _extract_error_text(params.get("error")) or _extract_error_text(params)
                    if err_text:
                        _append_unique(errors, err_text)
                continue

            if method == "turn/completed":
                turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                completed_id = turn_obj.get("id") or params.get("turnId")
                effective_turn_id = target_turn_id or candidate_turn_id
                if queued_behind_active_turn and effective_turn_id is None:
                    prior_turn_completed = True
                    continue
                if effective_turn_id is not None and completed_id != effective_turn_id:
                    continue
                turn_status = str(turn_obj.get("status") or "completed")
                outcome: AttachOutcomeStatus
                if turn_status == "failed":
                    outcome = "failed"
                    err_text = (
                        _extract_error_text(turn_obj.get("error"))
                        or "turn_failed: Turn failed on Codex Layer A daemon."
                    )
                    _append_unique(errors, err_text)
                elif turn_status == "interrupted":
                    outcome = "interrupted"
                    _append_unique(errors, "turn_interrupted: Turn was interrupted in live TUI.")
                else:
                    outcome = "completed"
                return DaemonTurnObservation(
                    thread_id=thread_id,
                    turn_id=completed_id if isinstance(completed_id, str) else effective_turn_id,
                    client_user_message_id=client_msg_id,
                    dispatch_method=dispatch_method,
                    queued_behind_active_turn=queued_behind_active_turn,
                    status=outcome,
                    commands=commands,
                    changed_files=changed_files,
                    errors=errors,
                    final_message=final_message,
                    usage=usage,
                    event_count=event_count,
                )
    finally:
        rpc.unsubscribe_quietly(thread_id)


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
    commands = (
        parsed.commands
        if full_output
        else [_compact_command_record(record) for record in parsed.commands]
    )
    git_diff_files = collect_git_diff_files(cwd, base=base_sha)

    return AuditSummary(
        worker=adapter.name,
        mode=mode,
        session_id=parsed.session_id,
        exit_code=proc.returncode,
        commands=commands,
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


def _load_optional_schema_dict(raw_schema: str | None) -> tuple[Path | None, dict[str, Any] | None]:
    if not raw_schema:
        return None, None
    schema_path = Path(raw_schema)
    if not schema_path.is_file():
        raise ValidationError(f"Schema file does not exist: {schema_path}")
    try:
        loaded_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in schema file {schema_path}: {exc}") from exc
    if not isinstance(loaded_schema, dict):
        raise ValidationError(f"Schema file must contain a JSON object: {schema_path}")
    return schema_path.resolve(), loaded_schema


def _build_daemon_audit_summary(
    observation: DaemonTurnObservation,
    thread_id: str,
    endpoint: DaemonEndpoint,
    req: AttachRequest,
    effective_cwd: Path,
    log_path: Path,
    base_sha: str | None,
    full_output: bool,
) -> tuple[AuditSummary, int]:
    if req.output_last_message is not None and observation.final_message:
        req.output_last_message.parent.mkdir(parents=True, exist_ok=True)
        req.output_last_message.write_text(observation.final_message, encoding="utf-8")

    errors = list(observation.errors)
    receipt, receipt_valid = parse_receipt(observation.final_message)
    if req.schema_path is not None and observation.status == "completed" and receipt_valid is not True:
        _append_unique(
            errors,
            "receipt_validation_failed: Final agent message did not satisfy the required receipt schema.",
        )
    if req.mode == "scout" and observation.changed_files:
        _append_unique(
            errors,
            "scout_contract_violation: File changes were emitted during a read-only scout attach.",
        )

    summary_final_message: str | None = (
        None if (receipt_valid is True and not full_output) else observation.final_message
    )
    commands = (
        observation.commands
        if full_output
        else [_compact_command_record(record) for record in observation.commands]
    )
    git_diff_files = collect_git_diff_files(
        effective_cwd if effective_cwd.exists() else None,
        base=base_sha,
    )
    ok_turn = (
        observation.status == "completed"
        and (req.schema_path is None or receipt_valid is True)
        and not (req.mode == "scout" and observation.changed_files)
    )
    exit_code = EXIT_OK if ok_turn else EXIT_ERROR
    summary = AuditSummary(
        worker="codex",
        mode=req.mode,
        session_id=thread_id,
        exit_code=exit_code,
        commands=commands,
        changed_files=observation.changed_files,
        git_diff_files=git_diff_files,
        errors=errors,
        final_message=summary_final_message,
        receipt=receipt,
        receipt_valid=receipt_valid,
        usage=observation.usage,
        raw_log=str(log_path),
        command=["codex-app-server", endpoint.display(), observation.dispatch_method, thread_id],
        status=observation.status,
    )
    return summary, exit_code


def _record_and_observe_daemon_turn(
    rpc: AppServerRpcClient,
    endpoint: DaemonEndpoint,
    thread: LoadedTuiThread,
    req: AttachRequest,
    effective_cwd: Path,
    prompt: str,
    schema_dict: dict[str, Any] | None,
    log_path: Path,
    preview_limit: int,
    deadline: float,
    already_subscribed: bool = False,
    force_turn_start: bool = False,
) -> DaemonTurnObservation:
    with log_path.open("w", encoding="utf-8") as log_f:
        client_msg_id = str(uuid.uuid4())
        log_f.write(
            json.dumps(
                {
                    "type": "attach.session",
                    "thread_id": thread.thread_id,
                    "client_user_message_id": client_msg_id,
                    "cwd": str(effective_cwd),
                    "endpoint": endpoint.display(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        log_f.flush()

        def _write_log_frame(frame_msg: dict[str, Any]) -> None:
            log_f.write(json.dumps(frame_msg, ensure_ascii=False) + "\n")
            log_f.flush()

        rpc.set_log_sink(_write_log_frame)
        try:
            observation = _dispatch_and_observe_turn(
                rpc=rpc,
                thread=thread,
                req=req,
                prompt=prompt,
                schema_dict=schema_dict,
                preview_limit=preview_limit,
                deadline=deadline,
                client_msg_id=client_msg_id,
                already_subscribed=already_subscribed,
                force_turn_start=force_turn_start,
            )
        finally:
            rpc.set_log_sink(None)
        log_f.write(
            json.dumps(
                {
                    "type": "attach.turn",
                    "thread_id": thread.thread_id,
                    "turn_id": observation.turn_id,
                    "client_user_message_id": observation.client_user_message_id,
                    "queued_behind_active_turn": observation.queued_behind_active_turn,
                    "status": observation.status,
                    "errors": observation.errors,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        log_f.flush()
    return observation


def _run_codex_via_daemon(
    args: argparse.Namespace,
    resolved_cwd: Path,
    schema_path: Path | None,
    schema_dict: dict[str, Any] | None,
    out_msg: Path | None,
    images: tuple[Path, ...],
    prompt: str,
) -> int:
    if args.timeout <= 0:
        raise ValidationError("--timeout must be greater than 0.")
    effort = _validate_codex_effort(args.effort or "medium")
    _maybe_exclude_local_prompt_dir(resolved_cwd, args.prompt_file)
    log_path = _prepare_log_path(
        Path(args.log_dir) if args.log_dir else None,
        "codex",
        args.mode,
    )
    preview_limit = FULL_OUTPUT_PREVIEW_LIMIT if args.full_output else OUTPUT_PREVIEW_LIMIT

    req = AttachRequest(
        worker="codex",
        mode=args.mode,
        cwd=resolved_cwd,
        effort=effort,
        model=args.model,
        fast=args.fast,
        schema_path=schema_path,
        output_last_message=out_msg,
        images=images,
        timeout=args.timeout,
        wait_for_approval=False,
        remote=args.remote,
        remote_auth_token_env=args.remote_auth_token_env,
    )
    codex_home = _resolve_codex_home()
    endpoint = resolve_daemon_endpoint(
        remote=req.remote,
        remote_auth_token_env=req.remote_auth_token_env,
        codex_home=codex_home,
    )
    deadline = time.monotonic() + req.timeout

    lock_cm = (
        acquire_ship_lock(resolved_cwd, "daemon-ship")
        if req.mode == "ship"
        else contextlib.nullcontext()
    )
    with lock_cm:
        try:
            rpc, endpoint = _connect_daemon_rpc(
                endpoint=endpoint,
                connect_timeout=min(req.timeout, DEFAULT_DISCOVERY_TIMEOUT),
                auto_start=not args.no_auto_daemon,
                remote=req.remote,
                remote_auth_token_env=req.remote_auth_token_env,
                codex_home=codex_home,
            )
        except DaemonOfflineError as exc:
            _emit_json(
                {
                    "ok": False,
                    "error": "daemon_offline",
                    "message": str(exc),
                    "endpoint": exc.endpoint,
                }
            )
            return EXIT_ERROR

        started_thread: LoadedTuiThread | None = None
        try:
            rpc.initialize(deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT))
            start_params = _build_thread_start_params(
                cwd=resolved_cwd,
                mode=req.mode,
                model=req.model,
                effort=effort,
                fast=req.fast,
                with_mcp=args.with_mcp,
                approval_policy="never",
            )
            start_res = rpc.request(
                "thread/start",
                start_params,
                deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT),
            )
            started_thread = _synthesize_started_thread(
                start_res, fallback_cwd=resolved_cwd, model=req.model
            )
            observation = _record_and_observe_daemon_turn(
                rpc=rpc,
                endpoint=endpoint,
                thread=started_thread,
                req=req,
                effective_cwd=resolved_cwd,
                prompt=prompt,
                schema_dict=schema_dict,
                log_path=log_path,
                preview_limit=preview_limit,
                deadline=deadline,
                already_subscribed=True,
                force_turn_start=True,
            )
        finally:
            if started_thread is not None and not args.keep_loaded:
                rpc.archive_quietly(started_thread.thread_id)
            rpc.close()

    summary, exit_code = _build_daemon_audit_summary(
        observation=observation,
        thread_id=started_thread.thread_id,
        endpoint=endpoint,
        req=req,
        effective_cwd=resolved_cwd,
        log_path=log_path,
        base_sha=args.base,
        full_output=args.full_output,
    )
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
    return exit_code


def _handle_run(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.worker)
    if args.daemon and adapter.name != "codex":
        raise ValidationError("--daemon is only supported for --worker codex.")
    if args.keep_loaded and not args.daemon:
        raise ValidationError("--keep-loaded requires --daemon.")
    if args.no_auto_daemon and not args.daemon:
        raise ValidationError("--no-auto-daemon requires --daemon.")

    cwd = Path(args.cwd)
    if not cwd.is_dir():
        raise ValidationError(f"Working directory does not exist: {cwd}")
    resolved_cwd = cwd.resolve()
    out_msg = Path(args.output_last_message).resolve() if args.output_last_message else None
    images = _resolve_image_paths(args.image)
    raw_prompt = _read_prompt(args.prompt_file, args.prompt, required=True)
    assert raw_prompt is not None
    prompt = _apply_prompt_preamble(raw_prompt, raw_prompt=args.raw_prompt)

    if args.daemon:
        schema_path, schema_dict = _load_optional_schema_dict(args.schema)
        return _run_codex_via_daemon(
            args=args,
            resolved_cwd=resolved_cwd,
            schema_path=schema_path,
            schema_dict=schema_dict,
            out_msg=out_msg,
            images=images,
            prompt=prompt,
        )

    schema_path = Path(args.schema) if args.schema else None
    if schema_path is not None and not schema_path.is_file():
        raise ValidationError(f"Schema file does not exist: {schema_path}")
    add_dirs = tuple(Path(p) for p in (args.add_dir or []))
    req = RunRequest(
        worker=adapter.name,
        mode=args.mode,
        cwd=resolved_cwd,
        effort=args.effort,
        model=args.model,
        fast=args.fast,
        with_mcp=args.with_mcp,
        permission_mode=args.permission_mode,
        approval_policy=args.approval_policy,
        allow_danger=args.allow_danger,
        schema_path=schema_path.resolve() if schema_path else None,
        output_last_message=out_msg,
        add_dirs=add_dirs,
        images=images,
    )
    cmd = adapter.build_run_cmd(req)
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
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
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
        fast=args.fast,
        with_mcp=args.with_mcp,
        permission_mode=args.permission_mode,
        approval_policy=args.approval_policy,
        allow_danger=args.allow_danger,
        schema_path=schema_path.resolve() if schema_path else None,
        output_last_message=out_msg.resolve() if out_msg else None,
        add_dirs=add_dirs,
        images=images,
    )
    cmd = adapter.build_resume_cmd(req)
    raw_prompt = _read_prompt(args.prompt_file, args.prompt, required=True)
    assert raw_prompt is not None
    prompt = _apply_prompt_preamble(raw_prompt, raw_prompt=args.raw_prompt)
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
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
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
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
    return EXIT_OK if summary.exit_code == 0 else EXIT_ERROR


def _handle_sessions(args: argparse.Namespace) -> int:
    if args.worker != "codex":
        raise ValidationError("Live TUI session discovery is only supported for --worker codex.")
    if args.timeout <= 0:
        raise ValidationError("--timeout must be greater than 0.")
    target_cwd: Path | None = None
    if args.cwd is not None:
        target_cwd = Path(args.cwd)
        if not target_cwd.is_dir():
            raise ValidationError(f"Working directory does not exist: {target_cwd}")
        target_cwd = target_cwd.resolve()

    codex_home = _resolve_codex_home()
    endpoint = resolve_daemon_endpoint(
        remote=args.remote,
        remote_auth_token_env=args.remote_auth_token_env,
        codex_home=codex_home,
    )
    deadline = time.monotonic() + args.timeout
    ensure_daemon = bool(getattr(args, "ensure_daemon", False)) and not bool(
        getattr(args, "no_auto_daemon", False)
    )
    try:
        rpc, endpoint = _connect_daemon_rpc(
            endpoint=endpoint,
            connect_timeout=min(args.timeout, 5.0),
            auto_start=ensure_daemon,
            remote=args.remote,
            remote_auth_token_env=args.remote_auth_token_env,
            codex_home=codex_home,
        )
    except DaemonOfflineError as exc:
        locked = _find_conflicting_writer_locks(
            codex_home,
            target_cwd=target_cwd,
            session_id=getattr(args, "session", None),
            excluded_ids=set(),
        )
        _emit_json(
            {
                "ok": False,
                "daemon_online": False,
                "error": "daemon_offline",
                "message": str(exc),
                "endpoint": exc.endpoint,
                "cwd": str(target_cwd) if target_cwd else None,
                "sessions": [],
                "desktop_locked_thread_ids": locked,
            }
        )
        return EXIT_ERROR

    try:
        rpc.initialize(deadline=deadline)
        loaded_id_set, threads = _list_daemon_loaded_threads(rpc, deadline=deadline)
        if target_cwd is not None:
            target_git_ctx = inspect_git_context(target_cwd)
            target_git_root = target_git_ctx.git_root if target_git_ctx.is_git_repo else None
            threads = [
                t for t in threads if _thread_matches_target_cwd(t, target_cwd, target_git_root)
            ]
        locked = _find_conflicting_writer_locks(
            codex_home,
            target_cwd=target_cwd,
            session_id=getattr(args, "session", None),
            excluded_ids=loaded_id_set,
            rpc=rpc,
            deadline=deadline,
        )
    finally:
        rpc.close()

    _emit_json(
        {
            "ok": True,
            "daemon_online": True,
            "endpoint": endpoint.display(),
            "cwd": str(target_cwd) if target_cwd else None,
            "sessions": [t.to_candidate_dict() for t in threads],
            "desktop_locked_thread_ids": locked,
        }
    )
    return EXIT_OK


def _emit_resolution_failure(
    resolution: ThreadResolution,
    target_cwd: Path | None,
    session_id: str | None,
) -> int:
    if resolution.kind == "ambiguous":
        _emit_json(
            {
                "ok": False,
                "error": "ambiguous_tui_threads",
                "message": resolution.detail,
                "cwd": str(target_cwd) if target_cwd else None,
                "endpoint": resolution.endpoint,
                "candidates": [c.to_candidate_dict() for c in resolution.candidates],
            }
        )
        return EXIT_INVALID_ARGS
    if resolution.kind == "desktop_writer_lock":
        _emit_json(
            {
                "ok": False,
                "error": "desktop_writer_lock",
                "message": resolution.detail,
                "cwd": str(target_cwd) if target_cwd else None,
                "session_id": session_id,
                "endpoint": resolution.endpoint,
                "locked_thread_ids": list(resolution.locked_thread_ids),
            }
        )
        return EXIT_LOCK_CONFLICT
    if resolution.kind == "daemon_offline":
        _emit_json(
            {
                "ok": False,
                "error": "daemon_offline",
                "message": resolution.detail,
                "endpoint": resolution.endpoint,
            }
        )
        return EXIT_ERROR
    _emit_json(
        {
            "ok": False,
            "error": "session_not_found",
            "message": resolution.detail,
            "cwd": str(target_cwd) if target_cwd else None,
            "session_id": session_id,
            "endpoint": resolution.endpoint,
        }
    )
    return EXIT_ERROR


def _handle_attach(args: argparse.Namespace) -> int:
    if args.list:
        return _handle_sessions(args)
    if args.worker != "codex":
        raise ValidationError(
            "Live TUI attach is only supported for --worker codex (Layer A app-server daemon)."
        )
    if args.timeout <= 0:
        raise ValidationError("--timeout must be greater than 0.")
    if args.cwd is None and not args.session:
        raise ValidationError("Provide --cwd <dir> or --session <thread_id>.")
    if args.session is not None and not args.session.strip():
        raise ValidationError("--session must not be empty.")
    if args.create and args.cwd is None:
        raise ValidationError("--create requires --cwd <dir>.")
    if args.create and args.session:
        raise ValidationError("--create cannot be combined with --session <thread_id>.")

    target_cwd: Path | None = None
    if args.cwd is not None:
        raw_cwd = Path(args.cwd)
        if not raw_cwd.is_dir():
            raise ValidationError(f"Working directory does not exist: {raw_cwd}")
        target_cwd = raw_cwd.resolve()

    effort = _validate_codex_effort(args.effort) if args.effort is not None else None
    schema_path, schema_dict = _load_optional_schema_dict(args.schema)
    out_msg = Path(args.output_last_message).resolve() if args.output_last_message else None
    images = _resolve_image_paths(args.image)
    raw_prompt = _read_prompt(args.prompt_file, args.prompt, required=True)
    assert raw_prompt is not None
    prompt = _apply_prompt_preamble(raw_prompt, raw_prompt=args.raw_prompt)

    req = AttachRequest(
        worker="codex",
        mode=args.mode,
        cwd=target_cwd,
        session_id=args.session.strip() if args.session else None,
        select_latest=args.latest,
        create=args.create,
        effort=effort,
        model=args.model,
        fast=args.fast,
        schema_path=schema_path,
        output_last_message=out_msg,
        images=images,
        timeout=args.timeout,
        wait_for_approval=args.wait_for_approval,
        remote=args.remote,
        remote_auth_token_env=args.remote_auth_token_env,
    )

    codex_home = _resolve_codex_home()
    endpoint = resolve_daemon_endpoint(
        remote=req.remote,
        remote_auth_token_env=req.remote_auth_token_env,
        codex_home=codex_home,
    )
    deadline = time.monotonic() + req.timeout
    auto_daemon = (req.create or args.ensure_daemon) and not args.no_auto_daemon

    try:
        rpc = AppServerRpcClient(endpoint, connect_timeout=min(req.timeout, DEFAULT_DISCOVERY_TIMEOUT))
    except DaemonOfflineError as exc:
        locked = _find_conflicting_writer_locks(
            codex_home,
            target_cwd=req.cwd,
            session_id=req.session_id,
            excluded_ids=set(),
        )
        if locked:
            return _emit_resolution_failure(
                ThreadResolution(
                    kind="desktop_writer_lock",
                    locked_thread_ids=tuple(locked),
                    endpoint=exc.endpoint,
                    detail=(
                        f"Active session(s) {', '.join(locked)} are owned by Codex Desktop or VS Code "
                        "(Layer B IPC, unsupported by CLI daemon attach)."
                    ),
                ),
                req.cwd,
                req.session_id,
            )
        if auto_daemon:
            try:
                rpc, endpoint = _connect_daemon_rpc(
                    endpoint=endpoint,
                    connect_timeout=min(req.timeout, DEFAULT_DISCOVERY_TIMEOUT),
                    auto_start=True,
                    remote=req.remote,
                    remote_auth_token_env=req.remote_auth_token_env,
                    codex_home=codex_home,
                )
            except DaemonOfflineError as retry_exc:
                return _emit_resolution_failure(
                    ThreadResolution(
                        kind="daemon_offline",
                        endpoint=retry_exc.endpoint,
                        detail=str(retry_exc),
                    ),
                    req.cwd,
                    req.session_id,
                )
        else:
            return _emit_resolution_failure(
                ThreadResolution(
                    kind="daemon_offline",
                    endpoint=exc.endpoint,
                    detail=str(exc),
                ),
                req.cwd,
                req.session_id,
            )

    try:
        rpc.initialize(deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT))
        resolution = _resolve_thread_with_client(
            rpc=rpc,
            endpoint=endpoint,
            codex_home=codex_home,
            target_cwd=req.cwd,
            session_id=req.session_id,
            select_latest=req.select_latest,
            deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT),
        )
        created_thread = False
        if resolution.kind == "not_found" and req.create and req.cwd is not None:
            matched_thread: LoadedTuiThread | None = None
            created_thread = True
        elif resolution.kind != "matched" or resolution.thread is None:
            return _emit_resolution_failure(resolution, req.cwd, req.session_id)
        else:
            matched_thread = resolution.thread

        effective_cwd = (
            req.cwd
            if req.cwd is not None
            else Path(matched_thread.resolved_cwd)  # type: ignore[union-attr]
        )
        _maybe_exclude_local_prompt_dir(effective_cwd, args.prompt_file)
        log_path = _prepare_log_path(
            Path(args.log_dir) if args.log_dir else None,
            "codex",
            req.mode,
        )
        preview_limit = FULL_OUTPUT_PREVIEW_LIMIT if args.full_output else OUTPUT_PREVIEW_LIMIT

        lock_cm = (
            acquire_ship_lock(effective_cwd, "attach-ship")
            if req.mode == "ship" and effective_cwd.exists()
            else contextlib.nullcontext()
        )
        with lock_cm:
            if created_thread:
                assert req.cwd is not None
                start_params = _build_thread_start_params(
                    cwd=req.cwd,
                    mode=req.mode,
                    model=req.model,
                    effort=req.effort,
                    fast=req.fast,
                    with_mcp=True,
                    approval_policy=None if req.wait_for_approval else "never",
                )
                start_res = rpc.request(
                    "thread/start",
                    start_params,
                    deadline=min(deadline, time.monotonic() + DEFAULT_DISCOVERY_TIMEOUT),
                )
                matched_thread = _synthesize_started_thread(
                    start_res, fallback_cwd=req.cwd, model=req.model
                )
            assert matched_thread is not None
            observation = _record_and_observe_daemon_turn(
                rpc=rpc,
                endpoint=endpoint,
                thread=matched_thread,
                req=req,
                effective_cwd=effective_cwd,
                prompt=prompt,
                schema_dict=schema_dict,
                log_path=log_path,
                preview_limit=preview_limit,
                deadline=deadline,
                already_subscribed=created_thread,
                force_turn_start=created_thread,
            )
    finally:
        rpc.close()

    summary, exit_code = _build_daemon_audit_summary(
        observation=observation,
        thread_id=matched_thread.thread_id,
        endpoint=endpoint,
        req=req,
        effective_cwd=effective_cwd,
        log_path=log_path,
        base_sha=args.base,
        full_output=args.full_output,
    )
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
    return exit_code


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
    commands = (
        parsed.commands
        if args.full_output
        else [_compact_command_record(record) for record in parsed.commands]
    )
    summary = AuditSummary(
        worker=adapter.name,
        mode=args.mode,
        session_id=parsed.session_id,
        exit_code=args.exit_code,
        commands=commands,
        changed_files=parsed.changed_files,
        git_diff_files=git_diff_files,
        errors=parsed.errors,
        final_message=summary_final_message,
        receipt=receipt,
        receipt_valid=receipt_valid,
        usage=parsed.usage,
        raw_log=str(log_path.resolve()),
    )
    _emit_json(_serialize_audit_summary(summary, full_output=args.full_output))
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
    p_run.add_argument("--raw-prompt", action="store_true")
    p_run.add_argument("--effort")
    p_run.add_argument("--model")
    p_run.add_argument("--fast", action="store_true")
    p_run.add_argument("--with-mcp", action="store_true")
    p_run.add_argument("--daemon", action="store_true")
    p_run.add_argument("--no-auto-daemon", action="store_true")
    p_run.add_argument("--keep-loaded", action="store_true")
    p_run.add_argument("--remote")
    p_run.add_argument("--remote-auth-token-env")
    p_run.add_argument("--timeout", type=float, default=DEFAULT_ATTACH_TIMEOUT)
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
    p_res.add_argument("--raw-prompt", action="store_true")
    p_res.add_argument("--effort")
    p_res.add_argument("--model")
    p_res.add_argument("--fast", action="store_true")
    p_res.add_argument("--with-mcp", action="store_true")
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

    p_ses = subparsers.add_parser(
        "sessions",
        help="List loaded terminal Codex TUI sessions on the Layer A app-server daemon.",
    )
    p_ses.add_argument("--worker", default="codex", choices=sorted(ADAPTERS))
    p_ses.add_argument("--cwd")
    p_ses.add_argument("--ensure-daemon", action="store_true")
    p_ses.add_argument("--remote")
    p_ses.add_argument("--remote-auth-token-env")
    p_ses.add_argument("--timeout", type=float, default=DEFAULT_DISCOVERY_TIMEOUT)
    p_ses.set_defaults(handler=_handle_sessions)

    p_att = subparsers.add_parser(
        "attach",
        help="Attach to a live terminal Codex TUI session via the Layer A daemon without stealing locks.",
    )
    p_att.add_argument("--worker", default="codex", choices=sorted(ADAPTERS))
    p_att.add_argument("--cwd")
    p_att.add_argument("--session")
    p_att.add_argument("--latest", action="store_true")
    p_att.add_argument("--list", action="store_true")
    p_att.add_argument("--create", action="store_true")
    p_att.add_argument("--ensure-daemon", action="store_true")
    p_att.add_argument("--no-auto-daemon", action="store_true")
    p_att.add_argument("--mode", default="ship", choices=["scout", "ship"])
    p_att.add_argument("--prompt-file")
    p_att.add_argument("--prompt")
    p_att.add_argument("--raw-prompt", action="store_true")
    p_att.add_argument("--effort")
    p_att.add_argument("--model")
    p_att.add_argument("--fast", action="store_true")
    p_att.add_argument("--schema")
    p_att.add_argument("--output-last-message")
    p_att.add_argument("--image", action="append")
    p_att.add_argument("--log-dir")
    p_att.add_argument("--base")
    p_att.add_argument("--timeout", type=float, default=DEFAULT_ATTACH_TIMEOUT)
    p_att.add_argument("--wait-for-approval", action="store_true")
    p_att.add_argument("--remote")
    p_att.add_argument("--remote-auth-token-env")
    p_att.add_argument("--full-output", action="store_true")
    p_att.set_defaults(handler=_handle_attach)

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
