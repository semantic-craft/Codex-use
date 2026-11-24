from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = REPO_ROOT / "skills" / "codex-use"
DRIVE_SCRIPT = SKILL_ROOT / "scripts" / "drive.py"

SPEC = importlib.util.spec_from_file_location("codex_use_drive", DRIVE_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
drive = importlib.util.module_from_spec(SPEC)
sys.modules["codex_use_drive"] = drive
SPEC.loader.exec_module(drive)


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_temp_repo(root: Path) -> Path:
    repo = root / "sample-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI Tester")
    (repo / "README.md").write_text("# Sample\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _run_drive(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(DRIVE_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=merged_env,
    )


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = drive.ADAPTERS["codex"]

    def test_build_run_cmd_scout_and_ship(self) -> None:
        cwd = Path("/tmp/demo-repo")
        scout_cmd = self.adapter.build_run_cmd(
            drive.RunRequest(worker="codex", mode="scout", cwd=cwd, effort="medium")
        )
        self.assertEqual(
            scout_cmd,
            [
                "codex",
                "exec",
                "-s",
                "read-only",
                "-C",
                str(cwd),
                "-c",
                'model_reasoning_effort="medium"',
                "--json",
                "-",
            ],
        )

        schema = Path("/tmp/receipt.schema.json")
        out_msg = Path("/tmp/last.json")
        ship_cmd = self.adapter.build_run_cmd(
            drive.RunRequest(
                worker="codex",
                mode="ship",
                cwd=cwd,
                effort="xhigh",
                model="gpt-6.2-codex",
                schema_path=schema,
                output_last_message=out_msg,
            )
        )
        self.assertEqual(
            ship_cmd,
            [
                "codex",
                "exec",
                "-s",
                "workspace-write",
                "-C",
                str(cwd),
                "-c",
                'model_reasoning_effort="xhigh"',
                "--json",
                "-o",
                str(out_msg),
                "--output-schema",
                str(schema),
                "-m",
                "gpt-6.2-codex",
                "-",
            ],
        )

    def test_rejects_ultra_and_unknown_effort_before_spawn(self) -> None:
        with self.assertRaises(drive.ValidationError):
            self.adapter.build_run_cmd(
                drive.RunRequest(worker="codex", mode="scout", cwd=Path("/tmp"), effort="ultra")
            )
        with self.assertRaises(drive.ValidationError):
            self.adapter.build_resume_cmd(
                drive.ResumeRequest(
                    worker="codex",
                    session_id="thread-1",
                    mode="ship",
                    cwd=Path("/tmp"),
                    effort="turbo",
                )
            )

        with tempfile.TemporaryDirectory() as td:
            proc = _run_drive(
                [
                    "run",
                    "--worker",
                    "codex",
                    "--mode",
                    "scout",
                    "--cwd",
                    td,
                    "--effort",
                    "ultra",
                    "--prompt",
                    "inspect repo",
                ]
            )
            self.assertEqual(proc.returncode, drive.EXIT_INVALID_ARGS)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "invalid_arguments")
            self.assertIn("ultra", payload["message"].lower())

    def test_build_resume_cmd_never_passes_sandbox_or_cd(self) -> None:
        cmd = self.adapter.build_resume_cmd(
            drive.ResumeRequest(
                worker="codex",
                session_id="019c1234-5678-7000-8000-000000000001",
                mode="ship",
                cwd=Path("/tmp/wt"),
                effort="high",
                model="gpt-6.2-codex",
                schema_path=Path("/tmp/receipt.schema.json"),
                output_last_message=Path("/tmp/last.txt"),
            )
        )
        self.assertNotIn("-s", cmd)
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("-C", cmd)
        self.assertNotIn("--cd", cmd)
        self.assertEqual(
            cmd,
            [
                "codex",
                "exec",
                "resume",
                "019c1234-5678-7000-8000-000000000001",
                "--json",
                "-o",
                "/tmp/last.txt",
                "--output-schema",
                "/tmp/receipt.schema.json",
                "-c",
                'model_reasoning_effort="high"',
                "-m",
                "gpt-6.2-codex",
                "-",
            ],
        )

    def test_build_review_cmd_selectors_and_prompt(self) -> None:
        base_cmd = self.adapter.build_review_cmd(
            drive.ReviewRequest(worker="codex", cwd=Path("/tmp"), base="main", model="gpt-6.2")
        )
        self.assertEqual(
            base_cmd,
            ["codex", "exec", "review", "--json", "--base", "main", "-m", "gpt-6.2"],
        )

        commit_cmd = self.adapter.build_review_cmd(
            drive.ReviewRequest(
                worker="codex",
                cwd=Path("/tmp"),
                commit="abc1234",
                has_prompt=True,
            )
        )
        self.assertEqual(
            commit_cmd,
            ["codex", "exec", "review", "--json", "--commit", "abc1234", "-"],
        )

        default_cmd = self.adapter.build_review_cmd(
            drive.ReviewRequest(worker="codex", cwd=Path("/tmp"))
        )
        self.assertEqual(default_cmd, ["codex", "exec", "review", "--json", "--uncommitted"])

        with self.assertRaises(drive.ValidationError):
            self.adapter.build_review_cmd(
                drive.ReviewRequest(
                    worker="codex", cwd=Path("/tmp"), base="main", uncommitted=True
                )
            )

    def test_image_passthrough_and_missing_image_validation(self) -> None:
        img_a = Path("/tmp/mock-a.png")
        img_b = Path("/tmp/mock-b.png")
        run_cmd = self.adapter.build_run_cmd(
            drive.RunRequest(
                worker="codex",
                mode="ship",
                cwd=Path("/tmp/wt"),
                effort="high",
                images=(img_a, img_b),
            )
        )
        self.assertEqual(
            run_cmd[-5:],
            ["--image", str(img_a), "--image", str(img_b), "-"],
        )

        resume_cmd = self.adapter.build_resume_cmd(
            drive.ResumeRequest(
                worker="codex",
                session_id="thread-img",
                mode="ship",
                cwd=Path("/tmp/wt"),
                images=(img_a,),
            )
        )
        self.assertEqual(resume_cmd[-3:], ["--image", str(img_a), "-"])

        with tempfile.TemporaryDirectory() as td:
            missing_img = Path(td) / "does-not-exist.png"
            proc = _run_drive(
                [
                    "run",
                    "--worker",
                    "codex",
                    "--mode",
                    "scout",
                    "--cwd",
                    td,
                    "--image",
                    str(missing_img),
                    "--prompt",
                    "check screenshot",
                ]
            )
            self.assertEqual(proc.returncode, drive.EXIT_INVALID_ARGS)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "invalid_arguments")
            self.assertIn("does not exist", payload["message"].lower())

    def test_standalone_codex_binary_fallback_when_not_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            fake_home = root / "home"
            standalone = (
                fake_home
                / ".codex"
                / "packages"
                / "standalone"
                / "current"
                / "bin"
                / "codex"
            )
            standalone.parent.mkdir(parents=True, exist_ok=True)
            standalone.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli 0.171.0'; exit 0; fi\n"
                "if [ \"$1\" = \"login\" ]; then echo 'Logged in via standalone'; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            standalone.chmod(standalone.stat().st_mode | stat.S_IXUSR)

            with mock.patch.object(drive.shutil, "which", return_value=None), mock.patch.object(
                drive.Path, "home", return_value=fake_home
            ):
                resolved, source = drive._resolve_codex_binary()
                self.assertEqual(resolved, str(standalone))
                self.assertEqual(source, "standalone")

                preflight = self.adapter.preflight(repo)
                self.assertTrue(preflight.ok, preflight.issues)
                self.assertEqual(preflight.binary_path, str(standalone))
                self.assertEqual(preflight.version, "codex-cli 0.171.0")

                run_cmd = self.adapter.build_run_cmd(
                    drive.RunRequest(worker="codex", mode="scout", cwd=repo)
                )
                resume_cmd = self.adapter.build_resume_cmd(
                    drive.ResumeRequest(
                        worker="codex", session_id="t-1", mode="ship", cwd=repo
                    )
                )
                review_cmd = self.adapter.build_review_cmd(
                    drive.ReviewRequest(worker="codex", cwd=repo)
                )
                self.assertEqual(run_cmd[0], str(standalone))
                self.assertEqual(resume_cmd[0], str(standalone))
                assert review_cmd is not None
                self.assertEqual(review_cmd[0], str(standalone))

            with mock.patch.object(drive.shutil, "which", return_value="/usr/local/bin/codex"):
                run_cmd_path = self.adapter.build_run_cmd(
                    drive.RunRequest(worker="codex", mode="scout", cwd=repo)
                )
                self.assertEqual(run_cmd_path[0], "codex")

    def test_parse_codex_jsonl_fixture_and_audit_subcommand(self) -> None:
        receipt_obj = {
            "summary": "Fixed retry boundary and verified unit tests.",
            "changed_files": ["src/retry.py", "tests/test_retry.py"],
            "verification": [
                {
                    "command": "python3 -m unittest",
                    "exit_code": 0,
                    "summary": "All 12 tests passed.",
                }
            ],
            "assumptions_and_risks": ["Assumed jitter stays disabled when max_retries is 0."],
            "symbol_changes": ["clamp_retry_delay (modified)"],
        }
        fixture_lines = [
            json.dumps({"type": "thread.started", "thread_id": "019c9999-aaaa-7bbb-8ccc-111122223333"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": "/bin/zsh -lc 'python3 -m unittest'",
                        "aggregated_output": "Ran 12 tests in 0.02s\nOK\n",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_2",
                        "type": "file_change",
                        "changes": [
                            {"path": "src/retry.py", "kind": "update"},
                            {"path": "tests/test_retry.py", "kind": "update"},
                        ],
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_3",
                        "type": "file_change",
                        "path": "docs/retry.md",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_4",
                        "type": "error",
                        "message": "Non-fatal warning from tool",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_5",
                        "type": "agent_message",
                        "text": json.dumps(receipt_obj),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 600,
                        "output_tokens": 240,
                    },
                }
            ),
        ]
        parsed = self.adapter.parse_jsonl(fixture_lines)
        self.assertEqual(parsed.session_id, "019c9999-aaaa-7bbb-8ccc-111122223333")
        self.assertEqual(len(parsed.commands), 1)
        self.assertEqual(parsed.commands[0].exit_code, 0)
        self.assertIn("python3 -m unittest", parsed.commands[0].command)
        self.assertIn("OK", parsed.commands[0].output_preview)
        self.assertEqual(
            parsed.changed_files,
            ["src/retry.py", "tests/test_retry.py", "docs/retry.md"],
        )
        self.assertEqual(parsed.errors, ["Non-fatal warning from tool"])
        self.assertEqual(parsed.usage, {"input_tokens": 1200, "cached_input_tokens": 600, "output_tokens": 240})

        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "codex.jsonl"
            log_file.write_text("\n".join(fixture_lines) + "\n", encoding="utf-8")
            proc = _run_drive(
                ["audit", "--worker", "codex", "--log", str(log_file), "--mode", "ship"]
            )
            self.assertEqual(proc.returncode, drive.EXIT_ERROR)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["session_id"], "019c9999-aaaa-7bbb-8ccc-111122223333")
            self.assertTrue(summary["receipt_valid"])
            self.assertIsNone(summary["final_message"])
            self.assertEqual(summary["receipt"]["changed_files"], ["src/retry.py", "tests/test_retry.py"])

    def test_compact_audit_summary_vs_full_output_and_plain_message(self) -> None:
        long_output = "X" * 300
        long_fail_output = "E" * 300
        long_multiline_cmd = (
            "python3 -c '\n"
            + "print("
            + '"very_long_segment_" * 10'
            + ")\n"
            + "' --extra-flag "
            + ("a" * 120)
        )
        receipt_obj = {
            "summary": "Verified fix.",
            "changed_files": ["src/a.py"],
            "verification": [{"command": "pytest", "exit_code": 0, "summary": "ok"}],
            "assumptions_and_risks": [],
            "symbol_changes": [],
        }
        receipt_lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread-compact"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": long_multiline_cmd,
                        "aggregated_output": long_output,
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pytest tests/test_fail.py",
                        "aggregated_output": long_fail_output,
                        "exit_code": 1,
                        "status": "failed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(receipt_obj),
                    },
                }
            ),
        ]
        plain_lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread-plain"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Plain text scout report without JSON receipt.",
                    },
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            receipt_log = Path(td) / "receipt.jsonl"
            receipt_log.write_text("\n".join(receipt_lines) + "\n", encoding="utf-8")

            compact_proc = _run_drive(
                ["audit", "--worker", "codex", "--log", str(receipt_log), "--mode", "ship"]
            )
            self.assertEqual(compact_proc.returncode, drive.EXIT_OK)
            compact = json.loads(compact_proc.stdout)
            self.assertTrue(compact["receipt_valid"])
            self.assertIsNone(compact["final_message"])
            ok_cmd, fail_cmd = compact["commands"]
            self.assertNotIn("output_preview", ok_cmd)
            self.assertNotIn("\n", ok_cmd["command"])
            self.assertEqual(len(ok_cmd["command"]), 120)
            self.assertTrue(ok_cmd["command"].endswith("..."))
            self.assertEqual(len(fail_cmd["output_preview"]), 160)
            self.assertTrue(fail_cmd["output_preview"].endswith("..."))

            full_proc = _run_drive(
                [
                    "audit",
                    "--worker",
                    "codex",
                    "--log",
                    str(receipt_log),
                    "--mode",
                    "ship",
                    "--full-output",
                ]
            )
            self.assertEqual(full_proc.returncode, drive.EXIT_OK)
            full = json.loads(full_proc.stdout)
            self.assertTrue(full["receipt_valid"])
            self.assertEqual(full["final_message"], json.dumps(receipt_obj))
            full_ok_cmd, full_fail_cmd = full["commands"]
            self.assertEqual(full_ok_cmd["command"], long_multiline_cmd)
            self.assertEqual(len(full_ok_cmd["output_preview"]), 300)
            self.assertEqual(len(full_fail_cmd["output_preview"]), 300)

            plain_log = Path(td) / "plain.jsonl"
            plain_log.write_text("\n".join(plain_lines) + "\n", encoding="utf-8")
            plain_proc = _run_drive(
                ["audit", "--worker", "codex", "--log", str(plain_log), "--mode", "scout"]
            )
            self.assertEqual(plain_proc.returncode, drive.EXIT_OK)
            plain = json.loads(plain_proc.stdout)
            self.assertIsNone(plain["receipt_valid"])
            self.assertEqual(
                plain["final_message"], "Plain text scout report without JSON receipt."
            )


class DshAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = drive.ADAPTERS["dsh"]

    def test_build_run_and_resume_in_linked_worktree_auto_adds_git_common_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            wt_info = drive.create_worktree(repo, "dsh-wt-test")
            wt_path = Path(wt_info["worktree"])
            expected_common = (repo / ".git").resolve()

            scout_cmd = self.adapter.build_run_cmd(
                drive.RunRequest(worker="dsh", mode="scout", cwd=wt_path)
            )
            self.assertEqual(
                scout_cmd,
                [
                    "dsh",
                    "--profile",
                    "headless",
                    "--permission-mode",
                    "read-only",
                    "--approval-policy",
                    "never",
                    "--json",
                    "-",
                ],
            )
            self.assertNotIn("--add-dir", scout_cmd)

            ship_cmd = self.adapter.build_run_cmd(
                drive.RunRequest(worker="dsh", mode="ship", cwd=wt_path)
            )
            self.assertEqual(
                ship_cmd,
                [
                    "dsh",
                    "--profile",
                    "headless",
                    "--permission-mode",
                    "workspace-write",
                    "--approval-policy",
                    "never",
                    "--add-dir",
                    str(expected_common),
                    "--json",
                    "-",
                ],
            )

            resume_cmd = self.adapter.build_resume_cmd(
                drive.ResumeRequest(
                    worker="dsh",
                    session_id="session-abc123",
                    mode="ship",
                    cwd=wt_path,
                    approval_policy="ask",
                )
            )
            self.assertEqual(
                resume_cmd,
                [
                    "dsh",
                    "--profile",
                    "headless",
                    "--session-id",
                    "session-abc123",
                    "--permission-mode",
                    "workspace-write",
                    "--approval-policy",
                    "ask",
                    "--add-dir",
                    str(expected_common),
                    "--json",
                    "-",
                ],
            )

            drive.remove_worktree(repo, wt_path, delete_branch=True)

    def test_danger_full_access_requires_allow_danger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            with self.assertRaises(drive.ValidationError):
                self.adapter.build_run_cmd(
                    drive.RunRequest(
                        worker="dsh",
                        mode="ship",
                        cwd=cwd,
                        permission_mode="danger-full-access",
                    )
                )

            proc = _run_drive(
                [
                    "run",
                    "--worker",
                    "dsh",
                    "--mode",
                    "ship",
                    "--cwd",
                    td,
                    "--permission-mode",
                    "danger-full-access",
                    "--prompt",
                    "run check",
                ]
            )
            self.assertEqual(proc.returncode, drive.EXIT_INVALID_ARGS)
            self.assertEqual(json.loads(proc.stdout)["error"], "invalid_arguments")

            allowed_cmd = self.adapter.build_run_cmd(
                drive.RunRequest(
                    worker="dsh",
                    mode="ship",
                    cwd=cwd,
                    permission_mode="danger-full-access",
                    allow_danger=True,
                )
            )
            self.assertIn("danger-full-access", allowed_cmd)

    def test_review_returns_clear_unsupported_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                self.adapter.build_review_cmd(
                    drive.ReviewRequest(worker="dsh", cwd=Path(td), base="main")
                )
            )
            proc = _run_drive(["review", "--worker", "dsh", "--cwd", td, "--base", "main"])
            self.assertEqual(proc.returncode, drive.EXIT_ERROR)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "unsupported_review")
            self.assertIn("--worker codex", payload["message"])

    def test_dsh_rejects_image_attachments_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "screen.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")

            with self.assertRaises(drive.ValidationError):
                self.adapter.build_run_cmd(
                    drive.RunRequest(
                        worker="dsh", mode="scout", cwd=Path(td), images=(img,)
                    )
                )
            with self.assertRaises(drive.ValidationError):
                self.adapter.build_resume_cmd(
                    drive.ResumeRequest(
                        worker="dsh",
                        session_id="session-1",
                        mode="ship",
                        cwd=Path(td),
                        images=(img,),
                    )
                )

            proc = _run_drive(
                [
                    "run",
                    "--worker",
                    "dsh",
                    "--mode",
                    "scout",
                    "--cwd",
                    td,
                    "--image",
                    str(img),
                    "--prompt",
                    "inspect screenshot",
                ]
            )
            self.assertEqual(proc.returncode, drive.EXIT_INVALID_ARGS)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"], "invalid_arguments")
            self.assertIn("--image", payload["message"])

    def test_parse_dsh_ndjson_fixtures(self) -> None:
        missing_cred_fixture = [
            json.dumps({"type": "session", "sessionId": "session-1763900000-01"}),
            json.dumps(
                {
                    "type": "status",
                    "phase": "error",
                    "reason": {
                        "error": {
                            "code": "MISSING_CREDENTIAL",
                            "message": "No API credential configured for deepseek-official.",
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "sessionId": "session-1763900000-01",
                    "reason": {
                        "error": {
                            "code": "MISSING_CREDENTIAL",
                            "message": "No API credential configured for deepseek-official.",
                        }
                    },
                    "text": "",
                }
            ),
        ]
        parsed_err = self.adapter.parse_jsonl(missing_cred_fixture)
        self.assertEqual(parsed_err.session_id, "session-1763900000-01")
        self.assertEqual(
            parsed_err.errors,
            ["MISSING_CREDENTIAL: No API credential configured for deepseek-official."],
        )
        self.assertEqual(parsed_err.final_message, "")

        tool_fixture = [
            json.dumps({"type": "session", "sessionId": "session-1763900000-02"}),
            json.dumps(
                {
                    "type": "tool_call",
                    "callId": "call_1",
                    "toolName": "exec_command",
                    "arguments": {"command": "pytest -q"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "callId": "call_1",
                    "exitCode": 0,
                    "output": "4 passed in 0.11s",
                }
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "callId": "call_2",
                    "toolName": "edit_file",
                    "arguments": {"path": "src/app.py"},
                }
            ),
            json.dumps(
                {
                    "type": "command_execution",
                    "command": "git status --short",
                    "exit_code": 0,
                    "status": "completed",
                    "output": " M src/app.py",
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "sessionId": "session-1763900000-02",
                    "reason": "completed",
                    "text": "Updated src/app.py and confirmed all 4 tests pass.",
                }
            ),
        ]
        parsed_ok = self.adapter.parse_jsonl(tool_fixture)
        self.assertEqual(parsed_ok.session_id, "session-1763900000-02")
        self.assertEqual(len(parsed_ok.commands), 2)
        self.assertEqual(parsed_ok.commands[0].command, "pytest -q")
        self.assertEqual(parsed_ok.commands[0].exit_code, 0)
        self.assertIn("4 passed", parsed_ok.commands[0].output_preview)
        self.assertEqual(parsed_ok.commands[1].command, "git status --short")
        self.assertEqual(parsed_ok.changed_files, ["src/app.py"])
        self.assertEqual(parsed_ok.errors, [])
        self.assertIn("4 tests pass", parsed_ok.final_message)


class WorktreeAndLockTests(unittest.TestCase):
    def test_worktree_create_remove_and_concurrent_ship_lock_collision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)

            create_proc = _run_drive(
                ["worktree", "create", "--repo", str(repo), "--task", "slice-alpha"]
            )
            self.assertEqual(create_proc.returncode, drive.EXIT_OK, create_proc.stderr)
            created = json.loads(create_proc.stdout)
            wt_path = Path(created["worktree"])
            self.assertTrue(wt_path.is_dir())
            self.assertEqual(created["branch"], "codex-use/slice-alpha")
            self.assertEqual(len(created["base_sha"]), 40)

            with drive.acquire_ship_lock(wt_path, "ship"):
                collide_run = _run_drive(
                    [
                        "run",
                        "--worker",
                        "codex",
                        "--mode",
                        "ship",
                        "--cwd",
                        str(wt_path),
                        "--prompt",
                        "attempt concurrent ship",
                    ]
                )
                self.assertEqual(collide_run.returncode, drive.EXIT_LOCK_CONFLICT)
                self.assertEqual(json.loads(collide_run.stdout)["error"], "lock_conflict")

                collide_resume = _run_drive(
                    [
                        "resume",
                        "--worker",
                        "codex",
                        "--session",
                        "thread-1",
                        "--mode",
                        "ship",
                        "--cwd",
                        str(wt_path),
                        "--prompt",
                        "attempt concurrent resume",
                    ]
                )
                self.assertEqual(collide_resume.returncode, drive.EXIT_LOCK_CONFLICT)
                self.assertEqual(json.loads(collide_resume.stdout)["error"], "lock_conflict")

            remove_proc = _run_drive(
                [
                    "worktree",
                    "remove",
                    "--repo",
                    str(repo),
                    "--worktree",
                    str(wt_path),
                    "--delete-branch",
                ]
            )
            self.assertEqual(remove_proc.returncode, drive.EXIT_OK, remove_proc.stderr)
            removed = json.loads(remove_proc.stdout)
            self.assertTrue(removed["ok"])
            self.assertTrue(removed["branch_deleted"])
            self.assertFalse(wt_path.exists())

    def test_idempotent_local_git_exclude_for_codex_use_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            exclude_file = repo / ".git" / "info" / "exclude"
            gitignore_file = repo / ".gitignore"

            external_prompt = root / "external-prompt.md"
            external_prompt.write_text("external prompt", encoding="utf-8")
            self.assertFalse(drive._maybe_exclude_local_prompt_dir(repo, str(external_prompt)))
            self.assertNotIn(".codex-use/", exclude_file.read_text(encoding="utf-8"))

            local_prompt = repo / ".codex-use" / "task.md"
            local_prompt.parent.mkdir(parents=True, exist_ok=True)
            local_prompt.write_text("local spec", encoding="utf-8")
            self.assertTrue(drive._maybe_exclude_local_prompt_dir(repo, str(local_prompt)))
            self.assertFalse(drive.ensure_local_git_exclude(repo))
            self.assertFalse(drive._maybe_exclude_local_prompt_dir(repo, str(local_prompt)))
            exclude_lines = [
                line.strip()
                for line in exclude_file.read_text(encoding="utf-8").splitlines()
                if line.strip() == ".codex-use/"
            ]
            self.assertEqual(len(exclude_lines), 1)
            self.assertFalse(gitignore_file.exists())
            self.assertEqual(_git(repo, "status", "--porcelain"), "")

            wt_parent = root / "wt-parent"
            wt_parent.mkdir()
            repo_wt = _init_temp_repo(wt_parent)
            created = drive.create_worktree(repo_wt, "exclude-check")
            wt_path = Path(created["worktree"])
            wt_spec = wt_path / ".codex-use" / "slice.md"
            wt_spec.parent.mkdir(parents=True, exist_ok=True)
            wt_spec.write_text("worktree spec", encoding="utf-8")
            self.assertEqual(_git(wt_path, "status", "--porcelain"), "")
            drive.remove_worktree(repo_wt, wt_path, force=True, delete_branch=True)

    def test_worktree_create_combined_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)

            missing_worker = _run_drive(
                [
                    "worktree",
                    "create",
                    "--repo",
                    str(repo),
                    "--task",
                    "pf-missing-worker",
                    "--preflight",
                ]
            )
            self.assertEqual(missing_worker.returncode, drive.EXIT_INVALID_ARGS)
            self.assertEqual(json.loads(missing_worker.stdout)["error"], "invalid_arguments")

            empty_home = root / "empty-home"
            empty_home.mkdir()
            git_dir = str(Path(drive.shutil.which("git") or "/usr/bin/git").parent)
            fail_env = {"PATH": git_dir, "HOME": str(empty_home)}
            failed = _run_drive(
                [
                    "worktree",
                    "create",
                    "--repo",
                    str(repo),
                    "--task",
                    "pf-fail",
                    "--preflight",
                    "--worker",
                    "codex",
                ],
                env=fail_env,
            )
            self.assertEqual(failed.returncode, drive.EXIT_ERROR)
            fail_payload = json.loads(failed.stdout)
            self.assertFalse(fail_payload["ok"])
            self.assertEqual(fail_payload["error"], "preflight_failed")
            self.assertFalse(fail_payload["preflight"]["ok"])
            self.assertFalse((root / ".worktrees" / repo.name / "pf-fail").exists())
            self.assertEqual(_git(repo, "branch", "--list", "codex-use/pf-fail"), "")

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli 0.171.0'; exit 0; fi\n"
                "if [ \"$1\" = \"login\" ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
            pass_env = {"PATH": f"{fake_bin}:{git_dir}", "HOME": str(empty_home)}
            passed = _run_drive(
                [
                    "worktree",
                    "create",
                    "--repo",
                    str(repo),
                    "--task",
                    "pf-pass",
                    "--preflight",
                    "--worker",
                    "codex",
                ],
                env=pass_env,
            )
            self.assertEqual(passed.returncode, drive.EXIT_OK, passed.stdout + passed.stderr)
            pass_payload = json.loads(passed.stdout)
            self.assertTrue(pass_payload["ok"])
            self.assertTrue(pass_payload["preflight"]["ok"])
            wt_path = Path(pass_payload["worktree"])
            self.assertTrue(wt_path.is_dir())
            drive.remove_worktree(repo, wt_path, delete_branch=True)

    def test_worktree_remove_cleans_safe_caches_and_fails_closed_on_stray_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)

            created = drive.create_worktree(repo, "cache-clean")
            wt_path = Path(created["worktree"])
            (wt_path / ".codex-use").mkdir(parents=True, exist_ok=True)
            (wt_path / ".codex-use" / "slice.md").write_text("brief", encoding="utf-8")
            (wt_path / "src" / "__pycache__").mkdir(parents=True, exist_ok=True)
            (wt_path / "src" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"pyc")
            (wt_path / ".pytest_cache" / "v").mkdir(parents=True, exist_ok=True)
            (wt_path / ".pytest_cache" / "v" / "stepwise").write_text("[]", encoding="utf-8")
            (wt_path / ".DS_Store").write_bytes(b"mac")

            remove_proc = _run_drive(
                [
                    "worktree",
                    "remove",
                    "--repo",
                    str(repo),
                    "--worktree",
                    str(wt_path),
                    "--delete-branch",
                ]
            )
            self.assertEqual(remove_proc.returncode, drive.EXIT_OK, remove_proc.stdout + remove_proc.stderr)
            removed = json.loads(remove_proc.stdout)
            self.assertTrue(removed["ok"])
            self.assertTrue(removed["branch_deleted"])
            self.assertTrue(removed["cleaned_caches"])
            self.assertFalse(wt_path.exists())
            self.assertFalse((root / ".worktrees" / repo.name).exists())
            self.assertFalse((root / ".worktrees").exists())

            created_dirty = drive.create_worktree(repo, "stray-guard")
            wt_dirty = Path(created_dirty["worktree"])
            cache_file = wt_dirty / "src" / "__pycache__" / "a.pyc"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(b"pyc")
            stray_file = wt_dirty / "stray.py"
            stray_file.write_text("print('keep me')\n", encoding="utf-8")

            blocked_proc = _run_drive(
                [
                    "worktree",
                    "remove",
                    "--repo",
                    str(repo),
                    "--worktree",
                    str(wt_dirty),
                ]
            )
            self.assertEqual(blocked_proc.returncode, drive.EXIT_ERROR)
            self.assertTrue(stray_file.is_file())
            self.assertTrue(cache_file.is_file())

            drive.remove_worktree(repo, wt_dirty, force=True, delete_branch=True)


class RepositoryContractTests(unittest.TestCase):
    def test_manifests_and_schema_are_valid(self) -> None:
        agy = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(agy["$schema"], "https://antigravity.google/schemas/v1/plugin.json")
        self.assertEqual(agy["name"], "codex-use")
        self.assertEqual(agy["license"], "MIT")
        self.assertTrue(agy["suggestedPrompts"])

        grok = json.loads(
            (REPO_ROOT / ".grok-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(grok["name"], "codex-use")
        self.assertEqual(grok["skills"], "./skills")
        self.assertNotIn("mcpServers", grok)

        pkg = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["name"], "codex-use")
        self.assertEqual(pkg["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        self.assertIn("./cordis.patch.yml", pkg["exports"])
        self.assertIn("skills/", pkg["files"])

        patch_text = (REPO_ROOT / "cordis.patch.yml").read_text(encoding="utf-8")
        for expected in (
            "id: codex-use-skills",
            "name: '@deepseek-ai/dsh-skill-filesystem'",
            "providerName: codex-use",
            "includeDefaultRoots: false",
            "bundledSkillDir: ./skills",
        ):
            self.assertIn(expected, patch_text)

        schema = json.loads(
            (SKILL_ROOT / "schemas" / "receipt.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["required"],
            ["summary", "changed_files", "verification", "assumptions_and_risks", "symbol_changes"],
        )

        def assert_strict_object_schema(node: dict) -> None:
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                self.assertEqual(set(node.get("required", [])), set(node.get("properties", {}).keys()))
                for child in node.get("properties", {}).values():
                    assert_strict_object_schema(child)
            elif node.get("type") == "array" and isinstance(node.get("items"), dict):
                assert_strict_object_schema(node["items"])

        assert_strict_object_schema(schema)

    def test_skill_frontmatter_and_self_contained_relative_links(self) -> None:
        skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        fm_match = re.match(r"^---\n(.*?)\n---\n", skill_md, re.DOTALL)
        self.assertIsNotNone(fm_match, "SKILL.md must start with YAML frontmatter")
        assert fm_match is not None
        fm = fm_match.group(1)
        self.assertRegex(fm, r"(?m)^name:\s*codex-use\s*$", "SKILL.md name must be codex-use")
        self.assertRegex(fm, r"(?m)^description:\s*.+$", "SKILL.md must include description")

        for template_name in ("scout-task.md", "ship-task.md", "bugfix-task.md"):
            self.assertIn(f"templates/{template_name}", skill_md)
            self.assertTrue((SKILL_ROOT / "templates" / template_name).is_file())
        self.assertIn("2 `drive.py resume` rounds", skill_md)
        self.assertIn("read_file", skill_md)

        cold_start_files = [
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "workers" / "codex.md",
            SKILL_ROOT / "hosts" / "antigravity.md",
            SKILL_ROOT / "templates" / "bugfix-task.md",
        ]
        cold_start_bytes = sum(path.stat().st_size for path in cold_start_files)
        self.assertLessEqual(
            cold_start_bytes,
            11500,
            f"Cold-start skill bundle grew to {cold_start_bytes} bytes (budget <= 11500)",
        )

        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for md_file in sorted(SKILL_ROOT.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            for target in link_pattern.findall(text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                target_path = (md_file.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    target_path.exists(),
                    f"Broken relative link {target!r} in {md_file.relative_to(REPO_ROOT)}",
                )
                self.assertTrue(
                    target_path.is_relative_to(SKILL_ROOT.resolve()),
                    f"Link {target!r} in {md_file.relative_to(REPO_ROOT)} escapes skills/codex-use",
                )

    def test_zero_private_strings_anywhere_in_repo(self) -> None:
        forbidden = [
            "kido-" + "kantai",
            "kido-" + "mcp",
            "Kido" + "-",
            "Hel" + "ios",
            "Kar" + "ios",
            ".audit/" + "decisions.tsv",
            "/Users/" + "xianweizhang",
        ]
        for path in sorted(REPO_ROOT.rglob("*")):
            if ".git" in path.parts or "__pycache__" in path.parts or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden:
                self.assertNotIn(
                    token,
                    content,
                    f"Private token {token!r} leaked in {path.relative_to(REPO_ROOT)}",
                )


if __name__ == "__main__":
    unittest.main()
