from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
                "-c",
                "mcp_servers={}",
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
                fast=True,
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
                "-c",
                "mcp_servers={}",
                "-c",
                'service_tier="priority"',
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

        with_mcp_cmd = self.adapter.build_run_cmd(
            drive.RunRequest(
                worker="codex",
                mode="scout",
                cwd=cwd,
                effort="medium",
                with_mcp=True,
            )
        )
        self.assertNotIn("mcp_servers={}", with_mcp_cmd)

    def test_rejects_ultra_and_unknown_effort_before_spawn(self) -> None:
        for forbidden_val in ("ultra", "ultrafast", "ULTRAFAST", "turbo"):
            with self.assertRaises(drive.ValidationError):
                self.adapter.build_run_cmd(
                    drive.RunRequest(
                        worker="codex", mode="scout", cwd=Path("/tmp"), effort=forbidden_val
                    )
                )
            with self.assertRaises(drive.ValidationError):
                self.adapter.build_resume_cmd(
                    drive.ResumeRequest(
                        worker="codex",
                        session_id="thread-1",
                        mode="ship",
                        cwd=Path("/tmp"),
                        effort=forbidden_val,
                    )
                )
        for bad_tier in ("ultra", "ultrafast"):
            with self.assertRaises(drive.ValidationError):
                drive._validate_codex_service_tier(bad_tier)
        self.assertEqual(drive._validate_codex_service_tier("priority"), "priority")

        with tempfile.TemporaryDirectory() as td:
            for bad_effort in ("ultra", "ultrafast"):
                for sub_args in (
                    [
                        "run",
                        "--worker",
                        "codex",
                        "--mode",
                        "scout",
                        "--cwd",
                        td,
                        "--effort",
                        bad_effort,
                        "--prompt",
                        "inspect repo",
                    ],
                    [
                        "attach",
                        "--cwd",
                        td,
                        "--effort",
                        bad_effort,
                        "--prompt",
                        "inspect repo",
                    ],
                ):
                    proc = _run_drive(sub_args)
                    self.assertEqual(proc.returncode, drive.EXIT_INVALID_ARGS)
                    payload = json.loads(proc.stdout)
                    self.assertEqual(payload["error"], "invalid_arguments")
                    self.assertIn(bad_effort, payload["message"].lower())

    def test_build_resume_cmd_never_passes_sandbox_or_cd(self) -> None:
        cmd = self.adapter.build_resume_cmd(
            drive.ResumeRequest(
                worker="codex",
                session_id="019c1234-5678-7000-8000-000000000001",
                mode="ship",
                cwd=Path("/tmp/wt"),
                effort="high",
                model="gpt-6.2-codex",
                fast=True,
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
                "-c",
                "mcp_servers={}",
                "-c",
                'service_tier="priority"',
                "-m",
                "gpt-6.2-codex",
                "-",
            ],
        )

        resume_with_mcp = self.adapter.build_resume_cmd(
            drive.ResumeRequest(
                worker="codex",
                session_id="019c1234-5678-7000-8000-000000000001",
                mode="ship",
                cwd=Path("/tmp/wt"),
                with_mcp=True,
            )
        )
        self.assertNotIn("mcp_servers={}", resume_with_mcp)

    def test_subagent_preamble_and_headless_mcp_flags_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            captured_args = root / "captured_args.json"
            captured_stdin = root / "captured_stdin.txt"
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                f"open({str(captured_args)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
                f"open({str(captured_stdin)!r}, 'w', encoding='utf-8').write(sys.stdin.read())\n"
                "print(json.dumps({'type': 'thread.started', 'thread_id': 't-pre'}))\n"
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
            env = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}

            default_run = _run_drive(
                [
                    "run",
                    "--worker",
                    "codex",
                    "--mode",
                    "scout",
                    "--fast",
                    "--cwd",
                    str(repo),
                    "--prompt",
                    "Check retry logic.",
                ],
                env=env,
            )
            self.assertEqual(default_run.returncode, drive.EXIT_OK, default_run.stdout + default_run.stderr)
            argv_default = json.loads(captured_args.read_text(encoding="utf-8"))
            stdin_default = captured_stdin.read_text(encoding="utf-8")
            self.assertIn("mcp_servers={}", argv_default)
            self.assertIn('service_tier="priority"', argv_default)
            self.assertTrue(stdin_default.startswith(drive.SUBAGENT_DISPATCH_PREAMBLE))
            self.assertEqual(stdin_default.count("using-ryugi"), 1)
            self.assertTrue(stdin_default.endswith("Check retry logic."))

            raw_with_mcp = _run_drive(
                [
                    "resume",
                    "--worker",
                    "codex",
                    "--session",
                    "t-pre",
                    "--mode",
                    "scout",
                    "--with-mcp",
                    "--raw-prompt",
                    "--cwd",
                    str(repo),
                    "--prompt",
                    "Check retry logic verbatim.",
                ],
                env=env,
            )
            self.assertEqual(raw_with_mcp.returncode, drive.EXIT_OK)
            argv_raw = json.loads(captured_args.read_text(encoding="utf-8"))
            stdin_raw = captured_stdin.read_text(encoding="utf-8")
            self.assertNotIn("mcp_servers={}", argv_raw)
            self.assertEqual(stdin_raw, "Check retry logic verbatim.")

            template_prompt = (SKILL_ROOT / "templates" / "scout-task.md").read_text(encoding="utf-8")
            deduped = drive._apply_prompt_preamble(template_prompt, raw_prompt=False)
            self.assertEqual(deduped, template_prompt)
            self.assertEqual(deduped.count(drive.SUBAGENT_DISPATCH_PREAMBLE), 1)

    def test_single_subprocess_inspect_git_context_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            wt_info = drive.create_worktree(repo, "git-ctx-perf")
            wt_path = Path(wt_info["worktree"])
            unborn = root / "unborn-repo"
            unborn.mkdir()
            _git(unborn, "init", "-b", "main")
            non_git = root / "plain-dir"
            non_git.mkdir()

            drive.clear_git_context_cache()
            with mock.patch.object(drive, "_run_git", wraps=drive._run_git) as spy_git:
                ctx1 = drive.inspect_git_context(wt_path)
                self.assertEqual(spy_git.call_count, 1)
                self.assertTrue(ctx1.is_git_repo)
                self.assertTrue(ctx1.is_linked_worktree)
                self.assertEqual(ctx1.git_root, wt_path.resolve())
                self.assertEqual(ctx1.git_common_dir, (repo / ".git").resolve())
                self.assertIsNotNone(ctx1.head_sha)

                ctx2 = drive.inspect_git_context(wt_path)
                self.assertEqual(spy_git.call_count, 1)
                self.assertEqual(ctx1, ctx2)

                plain_ctx = drive.inspect_git_context(non_git)
                self.assertFalse(plain_ctx.is_git_repo)
                self.assertEqual(spy_git.call_count, 2)
                drive.inspect_git_context(non_git)
                self.assertEqual(spy_git.call_count, 2)

                unborn_ctx = drive.inspect_git_context(unborn)
                self.assertTrue(unborn_ctx.is_git_repo)
                self.assertFalse(unborn_ctx.is_linked_worktree)
                self.assertEqual(unborn_ctx.git_root, unborn.resolve())
                self.assertIsNone(unborn_ctx.head_sha)
                self.assertEqual(spy_git.call_count, 4)

                drive.clear_git_context_cache()
                drive.inspect_git_context(wt_path)
                self.assertEqual(spy_git.call_count, 5)

            drive.remove_worktree(repo, wt_path, delete_branch=True)

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

            for bad_args in (
                [
                    "run",
                    "--worker",
                    "dsh",
                    "--mode",
                    "scout",
                    "--fast",
                    "--cwd",
                    td,
                    "--prompt",
                    "check",
                ],
                [
                    "run",
                    "--worker",
                    "dsh",
                    "--mode",
                    "scout",
                    "--daemon",
                    "--cwd",
                    td,
                    "--prompt",
                    "check",
                ],
                [
                    "resume",
                    "--worker",
                    "dsh",
                    "--session",
                    "s-1",
                    "--fast",
                    "--cwd",
                    td,
                    "--prompt",
                    "check",
                ],
            ):
                bad_proc = _run_drive(bad_args)
                self.assertEqual(bad_proc.returncode, drive.EXIT_INVALID_ARGS)
                self.assertEqual(json.loads(bad_proc.stdout)["error"], "invalid_arguments")

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
            tpl_path = SKILL_ROOT / "templates" / template_name
            self.assertTrue(tpl_path.is_file())
            tpl_text = tpl_path.read_text(encoding="utf-8")
            self.assertTrue(
                tpl_text.startswith(drive.SUBAGENT_DISPATCH_PREAMBLE),
                f"{template_name} must start with SUBAGENT_DISPATCH_PREAMBLE",
            )
        self.assertIn("2 `drive.py resume` rounds", skill_md)
        self.assertIn("read_file", skill_md)
        self.assertIn("codex-driver", skill_md)

        for bundle_label, cold_start_files in (
            (
                "bugfix",
                [
                    SKILL_ROOT / "SKILL.md",
                    SKILL_ROOT / "workers" / "codex.md",
                    SKILL_ROOT / "hosts" / "antigravity.md",
                    SKILL_ROOT / "templates" / "bugfix-task.md",
                ],
            ),
            (
                "ship+schema",
                [
                    SKILL_ROOT / "SKILL.md",
                    SKILL_ROOT / "workers" / "codex.md",
                    SKILL_ROOT / "hosts" / "antigravity.md",
                    SKILL_ROOT / "templates" / "ship-task.md",
                    SKILL_ROOT / "schemas" / "receipt.schema.json",
                ],
            ),
        ):
            cold_start_bytes = sum(path.stat().st_size for path in cold_start_files)
            self.assertLessEqual(
                cold_start_bytes,
                11500,
                f"Cold-start skill bundle ({bundle_label}) grew to {cold_start_bytes} bytes (budget <= 11500)",
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
            if path.suffix == ".md":
                self.assertNotIn(
                    "\u2014",
                    content,
                    f"Em-dash found in markdown file {path.relative_to(REPO_ROOT)}",
                )


class _MockWsConnection:
    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buf = bytearray()
        self.request_path = ""
        self.headers: dict[str, str] = {}
        self.pong_payloads: list[bytes] = []

    def handshake(self, max_frame_bytes: int = 16 * 1024 * 1024) -> None:
        while b"\r\n\r\n" not in self.buf:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise EOFError("client closed during handshake")
            self.buf.extend(chunk)
        idx = self.buf.index(b"\r\n\r\n")
        raw = bytes(self.buf[:idx]).decode("iso-8859-1")
        del self.buf[: idx + 4]
        lines = raw.split("\r\n")
        parts = lines[0].split(" ")
        self.request_path = parts[1] if len(parts) >= 2 else "/"
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                self.headers[k.strip().lower()] = v.strip()
        if "x-codex-app-server-compression" in self.headers:
            raise AssertionError("Client must not negotiate zstd compression")
        sec_key = self.headers["sec-websocket-key"]
        accept = base64.b64encode(
            hashlib.sha1((sec_key + drive.WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            f"x-codex-websocket-max-unfragmented-message-bytes: {max_frame_bytes}\r\n\r\n"
        )
        self.conn.sendall(resp.encode("ascii"))

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise EOFError("client closed websocket")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_json(self) -> dict | None:
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            if not masked:
                raise AssertionError("RFC 6455 requires client-to-server frames to be masked")
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4)
            payload = drive._xor_mask(self._read_exact(length), mask)
            if opcode == 0x8:
                return None
            if opcode == 0xA:
                self.pong_payloads.append(payload)
                continue
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            fragments.append(payload)
            if fin:
                return json.loads(b"".join(fragments).decode("utf-8"))

    def send_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
        n = len(payload)
        if n < 126:
            hdr = bytes([b0, n])
        elif n < 65536:
            hdr = bytes([b0, 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([b0, 127]) + struct.pack(">Q", n)
        self.conn.sendall(hdr + payload)

    def send_json(self, obj: dict, fragmented: bool = False) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if fragmented and len(raw) > 8:
            mid = len(raw) // 2
            self.send_frame(0x1, raw[:mid], fin=False)
            self.send_frame(0x0, raw[mid:], fin=True)
        else:
            self.send_frame(0x1, raw, fin=True)


class _MockAppServer:
    def __init__(
        self,
        threads: list[dict],
        on_dispatch: callable | None = None,
        unix_path: Path | None = None,
        max_frame_bytes: int = 16 * 1024 * 1024,
        no_rollout_thread_ids: set[str] | None = None,
        empty_rollout_counts: dict[str, int] | None = None,
        hard_resume_error_threads: dict[str, str] | None = None,
        replay_on_resume: list[dict] | None = None,
    ) -> None:
        self.threads = {t["id"]: dict(t) for t in threads}
        self.on_dispatch = on_dispatch
        self.unix_path = unix_path
        self.max_frame_bytes = max_frame_bytes
        self.no_rollout_thread_ids = set(no_rollout_thread_ids or ())
        self.empty_rollout_counts = dict(empty_rollout_counts or {})
        self.hard_resume_error_threads = dict(hard_resume_error_threads or {})
        self.replay_on_resume = list(replay_on_resume or ())
        self.received_messages: list[dict] = []
        self.handshake_headers: list[dict[str, str]] = []
        self.pong_payloads: list[bytes] = []
        self.unsubscribed_threads: list[str] = []
        self.archived_threads: list[str] = []
        self.started_thread_params: list[dict] = []
        self.approval_responses: list[dict] = []
        self._stop = threading.Event()

        if unix_path is not None:
            self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_sock.bind(str(unix_path))
            self.port = None
        else:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind(("127.0.0.1", 0))
            self.port = self.server_sock.getsockname()[1]
        self.server_sock.listen(8)
        self.server_sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn: socket.socket) -> None:
        ws = _MockWsConnection(conn)
        try:
            ws.handshake(max_frame_bytes=self.max_frame_bytes)
            self.handshake_headers.append(dict(ws.headers))
            while not self._stop.is_set():
                msg = ws.recv_json()
                if msg is None:
                    break
                self.received_messages.append(msg)
                if "method" not in msg and "id" in msg:
                    self.approval_responses.append(msg)
                    continue
                method = msg.get("method")
                req_id = msg.get("id")
                params = msg.get("params") or {}
                if method == "initialize":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "userAgent": "codex-mock/0.171.0",
                                "codexHome": "/tmp/mock-codex",
                                "platformFamily": "unix",
                                "platformOs": "macos",
                            },
                        }
                    )
                elif method == "initialized":
                    continue
                elif method == "thread/loaded/list":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"data": list(self.threads.keys()), "nextCursor": None},
                        }
                    )
                elif method == "thread/read":
                    tid = params["threadId"]
                    if tid not in self.threads:
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {"code": -32600, "message": f"unknown thread {tid}"},
                            }
                        )
                    else:
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "result": {"thread": self.threads[tid]},
                            }
                        )
                elif method == "thread/start":
                    self.started_thread_params.append(dict(params))
                    new_id = f"019d-started-{len(self.started_thread_params):04d}"
                    raw_cwd = params.get("cwd") or "/tmp"
                    created_thread = {
                        "id": new_id,
                        "cwd": raw_cwd,
                        "name": "",
                        "preview": "",
                        "ephemeral": bool(params.get("ephemeral", False)),
                        "parentThreadId": None,
                        "source": "appServer",
                        "model": params.get("model") or "gpt-6.2-codex",
                        "updatedAt": int(time.time()),
                        "createdAt": int(time.time()),
                        "status": {"type": "idle"},
                    }
                    self.threads[new_id] = created_thread
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "thread": created_thread,
                                "cwd": raw_cwd,
                                "model": created_thread["model"],
                                "modelProvider": "openai",
                                "approvalPolicy": params.get("approvalPolicy") or "never",
                                "approvalsReviewer": "user",
                                "sandbox": {"type": "readOnly"}
                                if params.get("sandbox") == "read-only"
                                else {"type": "workspaceWrite"},
                                "serviceTier": params.get("serviceTier"),
                                "reasoningEffort": (params.get("config") or {}).get(
                                    "model_reasoning_effort"
                                ),
                            },
                        }
                    )
                elif method == "thread/resume":
                    tid = params["threadId"]
                    if tid in self.hard_resume_error_threads:
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {
                                    "code": -32603,
                                    "message": self.hard_resume_error_threads[tid],
                                },
                            }
                        )
                    elif self.empty_rollout_counts.get(tid, 0) > 0:
                        self.empty_rollout_counts[tid] -= 1
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {
                                    "code": -32603,
                                    "message": (
                                        "failed to read session metadata: "
                                        f"rollout at /tmp/sessions/rollout-{tid}.jsonl is empty"
                                    ),
                                },
                            }
                        )
                    elif tid in self.no_rollout_thread_ids:
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {
                                    "code": -32600,
                                    "message": f"no rollout found for thread id {tid}",
                                },
                            }
                        )
                    else:
                        for replay_frame in self.replay_on_resume:
                            ws.send_json(replay_frame)
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "result": {"thread": self.threads[tid]},
                            }
                        )
                elif method == "thread/unsubscribe":
                    tid = params["threadId"]
                    self.unsubscribed_threads.append(tid)
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"status": "unsubscribed"},
                        }
                    )
                elif method == "thread/archive":
                    tid = params["threadId"]
                    self.archived_threads.append(tid)
                    self.threads.pop(tid, None)
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {},
                        }
                    )
                elif method in {"thread/queue/add", "turn/start"}:
                    tid = params["threadId"]
                    self.no_rollout_thread_ids.discard(tid)
                    if self.on_dispatch is not None:
                        self.on_dispatch(ws, method, req_id, params, self)
            self.pong_payloads.extend(ws.pong_payloads)
        except (EOFError, OSError):
            self.pong_payloads.extend(ws.pong_payloads)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self.server_sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class TuiAttachTests(unittest.TestCase):
    def test_sessions_and_attach_auto_discovery_by_cwd_over_unix_socket(self) -> None:
        receipt_obj = {
            "summary": "Implemented retry clamp and verified unit tests in live TUI.",
            "changed_files": ["README.md"],
            "verification": [
                {
                    "command": "python3 -m unittest",
                    "exit_code": 0,
                    "summary": "All tests passed.",
                }
            ],
            "assumptions_and_risks": [],
            "symbol_changes": ["clamp_retry (added)"],
        }
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            subdir = repo / "packages" / "core"
            subdir.mkdir(parents=True)
            codex_home = root / "codex-home"
            control_dir = codex_home / "app-server-control"
            control_dir.mkdir(parents=True)
            real_sock = Path(sock_td) / "daemon.sock"
            symlink_sock = control_dir / "app-server-control.sock"
            symlink_sock.symlink_to(real_sock)

            threads = [
                {
                    "id": "019d0000-1111-7000-8000-000000000001",
                    "cwd": str(subdir),
                    "name": "Live repo TUI",
                    "preview": "Working on core package",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "vscode",
                    "originator": "codex-tui",
                    "model": "gpt-6.2-codex",
                    "updatedAt": 1764200100,
                    "createdAt": 1764200000,
                    "status": {"type": "idle"},
                },
                {
                    "id": "019d0000-1111-7000-8000-000000000002",
                    "cwd": str(repo),
                    "name": "Ephemeral helper",
                    "preview": "helper",
                    "ephemeral": True,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                },
                {
                    "id": "019d0000-1111-7000-8000-000000000003",
                    "cwd": str(repo),
                    "name": "Spawned subagent",
                    "preview": "sub",
                    "ephemeral": False,
                    "parentThreadId": "019d0000-1111-7000-8000-000000000001",
                    "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "019d0000-1111-7000-8000-000000000001"}}},
                    "status": {"type": "active", "activeFlags": []},
                },
                {
                    "id": "019d0000-1111-7000-8000-000000000004",
                    "cwd": str(root),
                    "name": "Other directory TUI",
                    "preview": "unrelated",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                },
            ]

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                self.assertEqual(method, "turn/start")
                self.assertIn("outputSchema", params)
                turn_id = "turn-live-1"
                tid = params["threadId"]
                client_msg_id = params["clientUserMessageId"]
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/started",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {"id": "u1", "type": "userMessage", "clientId": client_msg_id},
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {
                                "id": "c1",
                                "type": "commandExecution",
                                "command": "python3 -m unittest",
                                "exitCode": 0,
                                "status": "completed",
                                "aggregatedOutput": "Ran 5 tests\nOK\n",
                            },
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {
                                "id": "f1",
                                "type": "fileChange",
                                "changes": [{"path": "README.md", "kind": "update"}],
                                "status": "completed",
                            },
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {
                                "id": "m1",
                                "type": "agentMessage",
                                "text": json.dumps(receipt_obj),
                            },
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "tokenUsage": {
                                "last": {"inputTokens": 500, "outputTokens": 120, "totalTokens": 620}
                            },
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"threadId": tid, "turn": {"id": turn_id, "status": "completed"}},
                    }
                )

            srv = _MockAppServer(threads=threads, on_dispatch=on_dispatch, unix_path=real_sock)
            try:
                env = {"CODEX_HOME": str(codex_home)}
                ses_proc = _run_drive(["sessions", "--cwd", str(repo)], env=env)
                self.assertEqual(ses_proc.returncode, drive.EXIT_OK, ses_proc.stdout + ses_proc.stderr)
                ses_data = json.loads(ses_proc.stdout)
                self.assertTrue(ses_data["ok"])
                self.assertEqual(len(ses_data["sessions"]), 1)
                self.assertEqual(
                    ses_data["sessions"][0]["thread_id"],
                    "019d0000-1111-7000-8000-000000000001",
                )

                list_proc = _run_drive(["attach", "--list", "--cwd", str(repo)], env=env)
                self.assertEqual(list_proc.returncode, drive.EXIT_OK)
                self.assertEqual(len(json.loads(list_proc.stdout)["sessions"]), 1)

                schema_path = SKILL_ROOT / "schemas" / "receipt.schema.json"
                log_dir = root / "logs"
                att_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "ship",
                        "--schema",
                        str(schema_path),
                        "--log-dir",
                        str(log_dir),
                        "--prompt",
                        "Implement retry clamp and return JSON receipt.",
                    ],
                    env=env,
                )
                self.assertEqual(att_proc.returncode, drive.EXIT_OK, att_proc.stdout + att_proc.stderr)
                summary = json.loads(att_proc.stdout)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["session_id"], "019d0000-1111-7000-8000-000000000001")
                self.assertTrue(summary["receipt_valid"])
                self.assertIsNone(summary["final_message"])
                self.assertEqual(len(summary["commands"]), 1)
                self.assertEqual(summary["commands"][0]["exit_code"], 0)
                self.assertNotIn("output_preview", summary["commands"][0])
                self.assertEqual(summary["changed_files"], ["README.md"])
                self.assertEqual(summary["usage"]["totalTokens"], 620)

                init_msgs = [m for m in srv.received_messages if m.get("method") == "initialize"]
                self.assertTrue(init_msgs)
                for im in init_msgs:
                    self.assertEqual(
                        im["params"]["clientInfo"]["name"],
                        drive.NON_ORIGINATING_CLIENT_NAME,
                    )
                    self.assertTrue(im["params"]["capabilities"]["experimentalApi"])
                self.assertIn(
                    "019d0000-1111-7000-8000-000000000001",
                    srv.unsubscribed_threads,
                )

                reaudit = _run_drive(
                    [
                        "audit",
                        "--worker",
                        "codex",
                        "--log",
                        summary["raw_log"],
                        "--mode",
                        "ship",
                    ]
                )
                self.assertEqual(reaudit.returncode, drive.EXIT_OK)
                reaudited = json.loads(reaudit.stdout)
                self.assertTrue(reaudited["receipt_valid"])
                self.assertEqual(reaudited["commands"][0]["exit_code"], 0)
            finally:
                srv.close()

    def test_attach_multi_thread_ambiguity_rejection_and_latest_override(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            (codex_home / "app-server-control").mkdir(parents=True)
            real_sock = Path(sock_td) / "ambig.sock"
            (codex_home / "app-server-control" / "app-server-control.sock").symlink_to(real_sock)

            threads = [
                {
                    "id": "thread-older",
                    "cwd": str(repo),
                    "name": "Older TUI",
                    "preview": "first",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "updatedAt": 100,
                    "createdAt": 90,
                    "status": {"type": "idle"},
                },
                {
                    "id": "thread-newer",
                    "cwd": str(repo),
                    "name": "Newer TUI",
                    "preview": "second",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "updatedAt": 200,
                    "createdAt": 150,
                    "status": {"type": "active", "activeFlags": []},
                },
            ]

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"queuedSubmission": {"id": "q1", "clientUserMessageId": cid, "input": []}},
                    }
                )
                if tid == "thread-newer":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "prior-turn", "status": "completed"}},
                        }
                    )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": f"turn-{tid}", "status": "inProgress"}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/started",
                        "params": {
                            "threadId": tid,
                            "turnId": f"turn-{tid}",
                            "item": {"id": "u1", "type": "userMessage", "clientId": cid},
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": f"turn-{tid}",
                            "item": {"id": "m1", "type": "agentMessage", "text": f"handled by {tid}"},
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"threadId": tid, "turn": {"id": f"turn-{tid}", "status": "completed"}},
                    }
                )

            srv = _MockAppServer(threads=threads, on_dispatch=on_dispatch, unix_path=real_sock)
            try:
                env = {"CODEX_HOME": str(codex_home)}
                ambig = _run_drive(
                    ["attach", "--cwd", str(repo), "--mode", "scout", "--prompt", "check status"],
                    env=env,
                )
                self.assertEqual(ambig.returncode, drive.EXIT_INVALID_ARGS)
                ambig_payload = json.loads(ambig.stdout)
                self.assertEqual(ambig_payload["error"], "ambiguous_tui_threads")
                self.assertEqual(len(ambig_payload["candidates"]), 2)
                self.assertEqual(
                    {c["thread_id"] for c in ambig_payload["candidates"]},
                    {"thread-older", "thread-newer"},
                )
                for cand in ambig_payload["candidates"]:
                    for key in ("thread_id", "title", "cwd", "status"):
                        self.assertIn(key, cand)

                latest = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--latest",
                        "--mode",
                        "scout",
                        "--prompt",
                        "check status",
                    ],
                    env=env,
                )
                self.assertEqual(latest.returncode, drive.EXIT_OK, latest.stdout + latest.stderr)
                latest_payload = json.loads(latest.stdout)
                self.assertEqual(latest_payload["session_id"], "thread-newer")
                self.assertEqual(latest_payload["final_message"], "handled by thread-newer")

                explicit = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--session",
                        "thread-older",
                        "--mode",
                        "scout",
                        "--prompt",
                        "check older",
                    ],
                    env=env,
                )
                self.assertEqual(explicit.returncode, drive.EXIT_OK)
                self.assertEqual(json.loads(explicit.stdout)["session_id"], "thread-older")
            finally:
                srv.close()

    def test_attach_idle_vs_active_queue_correlation_and_zero_turn_thread_over_ws_tcp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            threads = [
                {
                    "id": "thread-active",
                    "cwd": str(repo),
                    "name": "Active TUI",
                    "preview": "busy",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "active", "activeFlags": []},
                },
                {
                    "id": "thread-zero-turn",
                    "cwd": str(root),
                    "name": "Brand new 0-turn TUI",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                },
            ]

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                self.assertEqual(method, "thread/queue/add")
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                ws.send_frame(0x9, b"keepalive")
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"queuedSubmission": {"id": "q-live", "clientUserMessageId": cid, "input": []}},
                    }
                )
                if tid == "thread-active":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-prior",
                                "item": {
                                    "id": "old-c1",
                                    "type": "commandExecution",
                                    "command": "echo old_turn_noise",
                                    "exitCode": 1,
                                    "status": "failed",
                                    "aggregatedOutput": "old failure",
                                },
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-prior",
                                "item": {"id": "old-m1", "type": "agentMessage", "text": "STALE_PRIOR_TURN"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "turn-prior", "status": "completed"}},
                        }
                    )

                def _emit_our_turn() -> None:
                    time.sleep(0.03)
                    try:
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "turn/started",
                                "params": {"threadId": tid, "turn": {"id": "turn-queued", "status": "inProgress"}},
                            }
                        )
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "item/started",
                                "params": {
                                    "threadId": tid,
                                    "turnId": "turn-queued",
                                    "item": {"id": "u-ours", "type": "userMessage", "clientId": cid},
                                },
                            }
                        )
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "item/completed",
                                "params": {
                                    "threadId": tid,
                                    "turnId": "turn-queued",
                                    "item": {
                                        "id": "c-ours",
                                        "type": "commandExecution",
                                        "command": "pytest -q",
                                        "exitCode": 0,
                                        "status": "completed",
                                        "aggregatedOutput": "3 passed",
                                    },
                                },
                            }
                        )
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "item/completed",
                                "params": {
                                    "threadId": tid,
                                    "turnId": "turn-queued",
                                    "item": {
                                        "id": "m-ours",
                                        "type": "agentMessage",
                                        "text": f"Queued slice completed on {tid} over fragmented frame.",
                                    },
                                },
                            },
                            fragmented=True,
                        )
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "turn/completed",
                                "params": {"threadId": tid, "turn": {"id": "turn-queued", "status": "completed"}},
                            }
                        )
                    except OSError:
                        pass

                threading.Thread(target=_emit_our_turn, daemon=True).start()

            srv = _MockAppServer(
                threads=threads,
                on_dispatch=on_dispatch,
                unix_path=None,
                no_rollout_thread_ids={"thread-zero-turn"},
            )
            try:
                remote_url = f"ws://127.0.0.1:{srv.port}/rpc"
                env = {"CODEX_HOME": str(root / "empty-codex"), "TEST_REMOTE_TOKEN": "secret-tok-123"}
                queued_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "scout",
                        "--remote",
                        remote_url,
                        "--remote-auth-token-env",
                        "TEST_REMOTE_TOKEN",
                        "--prompt",
                        "Run queued scout check.",
                    ],
                    env=env,
                )
                self.assertEqual(queued_proc.returncode, drive.EXIT_OK, queued_proc.stdout + queued_proc.stderr)
                q_summary = json.loads(queued_proc.stdout)
                self.assertEqual(q_summary["status"], "completed")
                self.assertEqual(len(q_summary["commands"]), 1)
                self.assertEqual(q_summary["commands"][0]["command"], "pytest -q")
                self.assertEqual(
                    q_summary["final_message"],
                    "Queued slice completed on thread-active over fragmented frame.",
                )
                self.assertEqual(srv.handshake_headers[0].get("authorization"), "Bearer secret-tok-123")
                self.assertIn(b"keepalive", srv.pong_payloads)

                reaudit_queued = _run_drive(
                    [
                        "audit",
                        "--worker",
                        "codex",
                        "--log",
                        q_summary["raw_log"],
                        "--mode",
                        "scout",
                    ]
                )
                self.assertEqual(
                    reaudit_queued.returncode,
                    drive.EXIT_OK,
                    reaudit_queued.stdout + reaudit_queued.stderr,
                )
                reaudit_q_data = json.loads(reaudit_queued.stdout)
                self.assertEqual(len(reaudit_q_data["commands"]), 1)
                self.assertEqual(reaudit_q_data["commands"][0]["command"], "pytest -q")
                self.assertNotIn("old_turn_noise", json.dumps(reaudit_q_data))
                self.assertEqual(
                    reaudit_q_data["final_message"],
                    "Queued slice completed on thread-active over fragmented frame.",
                )

                zero_turn_proc = _run_drive(
                    [
                        "attach",
                        "--session",
                        "thread-zero-turn",
                        "--mode",
                        "scout",
                        "--remote",
                        remote_url,
                        "--prompt",
                        "First turn in brand new TUI.",
                    ],
                    env=env,
                )
                self.assertEqual(
                    zero_turn_proc.returncode,
                    drive.EXIT_OK,
                    zero_turn_proc.stdout + zero_turn_proc.stderr,
                )
                zt_summary = json.loads(zero_turn_proc.stdout)
                self.assertEqual(zt_summary["status"], "completed")
                self.assertIn("thread-zero-turn", zt_summary["final_message"])
                self.assertIn("thread-zero-turn", srv.unsubscribed_threads)
            finally:
                srv.close()

    def test_attach_passive_observer_approval_wakeup_and_wait_for_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            (codex_home / "app-server-control").mkdir(parents=True)
            real_sock = Path(sock_td) / "approval.sock"
            (codex_home / "app-server-control" / "app-server-control.sock").symlink_to(real_sock)

            mode_holder = {"kind": "command_approval"}
            threads = [
                {
                    "id": "thread-approval",
                    "cwd": str(repo),
                    "name": "Approval TUI",
                    "preview": "approval test",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                }
            ]

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"queuedSubmission": {"id": "q-app", "clientUserMessageId": cid, "input": []}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": "turn-app", "status": "inProgress"}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/started",
                        "params": {
                            "threadId": tid,
                            "turnId": "turn-app",
                            "item": {"id": "u-app", "type": "userMessage", "clientId": cid},
                        },
                    }
                )
                if mode_holder["kind"] == "command_approval":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 9001,
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-app",
                                "itemId": "cmd-1",
                                "command": "rm -rf build/",
                                "reason": "Needs sandbox escalation",
                            },
                        }
                    )
                elif mode_holder["kind"] == "user_input":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 9002,
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-app",
                                "itemId": "ask-1",
                                "isBlocking": True,
                                "questions": [{"question": "Which target environment?"}],
                            },
                        }
                    )
                elif mode_holder["kind"] == "wait_then_complete":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 9003,
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-app",
                                "itemId": "cmd-2",
                                "command": "git clean -fd",
                            },
                        }
                    )

                    def _user_approves_in_tui() -> None:
                        time.sleep(0.05)
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "item/completed",
                                "params": {
                                    "threadId": tid,
                                    "turnId": "turn-app",
                                    "item": {"id": "m-ok", "type": "agentMessage", "text": "Approved in TUI."},
                                },
                            }
                        )
                        ws.send_json(
                            {
                                "jsonrpc": "2.0",
                                "method": "turn/completed",
                                "params": {"threadId": tid, "turn": {"id": "turn-app", "status": "completed"}},
                            }
                        )

                    threading.Thread(target=_user_approves_in_tui, daemon=True).start()

            srv = _MockAppServer(threads=threads, on_dispatch=on_dispatch, unix_path=real_sock)
            try:
                env = {"CODEX_HOME": str(codex_home)}
                app_proc = _run_drive(
                    ["attach", "--cwd", str(repo), "--timeout", "3", "--prompt", "clean build"],
                    env=env,
                )
                self.assertEqual(app_proc.returncode, drive.EXIT_ERROR)
                app_summary = json.loads(app_proc.stdout)
                self.assertEqual(app_summary["status"], "approval_required")
                self.assertTrue(any("rm -rf build/" in e for e in app_summary["errors"]))
                self.assertEqual(srv.approval_responses, [])
                self.assertEqual(len(srv.unsubscribed_threads), 1)

                mode_holder["kind"] = "user_input"
                ui_proc = _run_drive(
                    ["attach", "--cwd", str(repo), "--timeout", "3", "--prompt", "deploy"],
                    env=env,
                )
                self.assertEqual(ui_proc.returncode, drive.EXIT_ERROR)
                ui_summary = json.loads(ui_proc.stdout)
                self.assertEqual(ui_summary["status"], "user_input_required")
                self.assertTrue(any("Which target environment?" in e for e in ui_summary["errors"]))
                self.assertEqual(srv.approval_responses, [])
                self.assertEqual(len(srv.unsubscribed_threads), 2)

                mode_holder["kind"] = "wait_then_complete"
                wait_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--wait-for-approval",
                        "--timeout",
                        "3",
                        "--prompt",
                        "clean with user approval",
                    ],
                    env=env,
                )
                self.assertEqual(wait_proc.returncode, drive.EXIT_OK, wait_proc.stdout + wait_proc.stderr)
                wait_summary = json.loads(wait_proc.stdout)
                self.assertEqual(wait_summary["status"], "completed")
                self.assertEqual(wait_summary["final_message"], "Approved in TUI.")
                self.assertEqual(srv.approval_responses, [])
                self.assertEqual(len(srv.unsubscribed_threads), 3)
            finally:
                srv.close()

    def test_attach_unsubscribe_on_failure_timeout_and_desktop_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            (codex_home / "app-server-control").mkdir(parents=True)
            real_sock = Path(sock_td) / "lifecycle.sock"
            (codex_home / "app-server-control" / "app-server-control.sock").symlink_to(real_sock)

            behavior = {"mode": "fail"}
            threads = [
                {
                    "id": "thread-life",
                    "cwd": str(repo),
                    "name": "Lifecycle TUI",
                    "preview": "test",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                }
            ]

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"queuedSubmission": {"id": "q-life", "clientUserMessageId": cid, "input": []}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": "turn-life", "status": "inProgress"}},
                    }
                )
                if behavior["mode"] == "fail":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {
                                "threadId": tid,
                                "turn": {
                                    "id": "turn-life",
                                    "status": "failed",
                                    "error": {"message": "Upstream model rate limit"},
                                },
                            },
                        }
                    )
                elif behavior["mode"] == "scout_mutate":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-life",
                                "item": {
                                    "id": "f-bad",
                                    "type": "fileChange",
                                    "changes": [{"path": "README.md", "kind": "update"}],
                                },
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "turn-life", "status": "completed"}},
                        }
                    )

            srv = _MockAppServer(threads=threads, on_dispatch=on_dispatch, unix_path=real_sock)
            try:
                env = {"CODEX_HOME": str(codex_home)}
                fail_proc = _run_drive(
                    ["attach", "--cwd", str(repo), "--prompt", "trigger fail"],
                    env=env,
                )
                self.assertEqual(fail_proc.returncode, drive.EXIT_ERROR)
                fail_summary = json.loads(fail_proc.stdout)
                self.assertEqual(fail_summary["status"], "failed")
                self.assertIn("Upstream model rate limit", fail_summary["errors"])
                self.assertEqual(len(srv.unsubscribed_threads), 1)

                behavior["mode"] = "scout_mutate"
                scout_bad = _run_drive(
                    ["attach", "--cwd", str(repo), "--mode", "scout", "--prompt", "read only"],
                    env=env,
                )
                self.assertEqual(scout_bad.returncode, drive.EXIT_ERROR)
                self.assertTrue(
                    any("scout_contract_violation" in e for e in json.loads(scout_bad.stdout)["errors"])
                )
                self.assertEqual(len(srv.unsubscribed_threads), 2)

                behavior["mode"] = "stall"
                timeout_proc = _run_drive(
                    ["attach", "--cwd", str(repo), "--timeout", "0.15", "--prompt", "stall turn"],
                    env=env,
                )
                self.assertEqual(timeout_proc.returncode, drive.EXIT_ERROR)
                timeout_summary = json.loads(timeout_proc.stdout)
                self.assertEqual(timeout_summary["status"], "timeout")
                self.assertEqual(len(srv.unsubscribed_threads), 3)
            finally:
                srv.close()

            desktop_repo = root / "desktop-repo"
            desktop_repo.mkdir()
            _git(desktop_repo, "init", "-b", "main")
            desktop_tid = "019d9999-desktop-lock-thread"
            locks_dir = codex_home / "thread-writer-locks"
            locks_dir.mkdir(parents=True, exist_ok=True)
            lock_file = locks_dir / f"{desktop_tid}.lock"
            lock_file.write_bytes(b"")
            rollout_dir = codex_home / "sessions" / "2026" / "11" / "27"
            rollout_dir.mkdir(parents=True, exist_ok=True)
            (rollout_dir / f"rollout-2026-11-27-{desktop_tid}.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": desktop_tid, "cwd": str(desktop_repo)}})
                + "\n",
                encoding="utf-8",
            )

            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        f"import fcntl, os, time; fd = os.open({str(lock_file)!r}, os.O_RDWR); "
                        "fcntl.flock(fd, fcntl.LOCK_EX); print('HELD', flush=True); time.sleep(10)"
                    ),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "HELD")
                offline_env = {"CODEX_HOME": str(codex_home), "PATH": os.path.dirname(sys.executable)}

                by_session = _run_drive(
                    ["attach", "--session", desktop_tid, "--prompt", "try desktop attach"],
                    env=offline_env,
                )
                self.assertEqual(by_session.returncode, drive.EXIT_LOCK_CONFLICT)
                self.assertEqual(json.loads(by_session.stdout)["error"], "desktop_writer_lock")

                by_cwd = _run_drive(
                    ["attach", "--cwd", str(desktop_repo), "--prompt", "try desktop cwd attach"],
                    env=offline_env,
                )
                self.assertEqual(by_cwd.returncode, drive.EXIT_LOCK_CONFLICT)
                cwd_err = json.loads(by_cwd.stdout)
                self.assertEqual(cwd_err["error"], "desktop_writer_lock")
                self.assertIn(desktop_tid, cwd_err["locked_thread_ids"])
            finally:
                if holder.stdout is not None:
                    holder.stdout.close()
                holder.terminate()
                holder.wait(timeout=5)

            offline_proc = _run_drive(
                ["attach", "--cwd", str(desktop_repo), "--prompt", "when daemon is off"],
                env={"CODEX_HOME": str(codex_home), "PATH": os.path.dirname(sys.executable)},
            )
            self.assertEqual(offline_proc.returncode, drive.EXIT_ERROR)
            self.assertEqual(json.loads(offline_proc.stdout)["error"], "daemon_offline")

    def test_attach_edge_races_zero_turn_failed_harvest_and_sqlite_numeric_order(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            (codex_home / "app-server-control").mkdir(parents=True)
            real_sock = Path(sock_td) / "races.sock"
            (codex_home / "app-server-control" / "app-server-control.sock").symlink_to(real_sock)

            db5 = codex_home / "state_5.sqlite"
            db10 = codex_home / "state_10.sqlite"
            for db_p, cwd_val in ((db5, "/nonexistent/stale"), (db10, str(repo))):
                con = sqlite3.connect(str(db_p))
                con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
                con.execute("INSERT INTO threads VALUES (?, ?)", ("sqlite-tid", cwd_val))
                con.commit()
                con.close()
            self.assertEqual(
                drive._lookup_thread_cwd_from_codex_home(codex_home, "sqlite-tid"),
                str(repo),
            )

            threads = [
                {
                    "id": "thread-zero-fail",
                    "cwd": str(root),
                    "name": "Zero turn instant fail",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                    "turns": [],
                },
                {
                    "id": "thread-race",
                    "cwd": str(repo),
                    "name": "Simultaneous TUI race",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "active", "activeFlags": []},
                },
            ]

            scenario = {"mode": "zero_fail"}

            def on_dispatch(ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer) -> None:
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                if scenario["mode"] == "zero_fail":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "remoteControl/status/changed",
                            "params": {"enabled": False},
                        }
                    )
                    srv.threads[tid]["status"] = {"type": "idle"}
                    srv.threads[tid]["turns"] = [
                        {
                            "id": "turn-0-fail",
                            "status": "failed",
                            "error": {"message": "0-turn sandbox setup failed"},
                            "items": [
                                {"id": "u0", "type": "userMessage", "clientId": cid},
                            ],
                        }
                    ]
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"queuedSubmission": {"id": "q0", "clientUserMessageId": cid, "input": []}},
                        }
                    )
                elif scenario["mode"] == "model_after_pending_idle":
                    self.assertEqual(method, "turn/start")
                    self.assertEqual(params.get("model"), "gpt-6.2-astra")
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"turn": {"id": "turn-override", "status": "inProgress"}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-override",
                                "item": {"id": "m-ov", "type": "agentMessage", "text": "override turn ok"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "turn-override", "status": "completed"}},
                        }
                    )
                elif scenario["mode"] == "simultaneous_enter":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"queuedSubmission": {"id": "q-sim", "clientUserMessageId": cid, "input": []}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {"threadId": tid, "turn": {"id": "turn-human", "status": "inProgress"}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/started",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-human",
                                "item": {"id": "u-human", "type": "userMessage", "clientId": "human-enter-id"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-human",
                                "item": {"id": "m-human", "type": "agentMessage", "text": "HUMAN_TURN_TEXT"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "turn-human", "status": "completed"}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {"threadId": tid, "turn": {"id": "turn-agent", "status": "inProgress"}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/started",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-agent",
                                "item": {"id": "u-agent", "type": "userMessage", "clientId": cid},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "turn-agent",
                                "item": {"id": "m-agent", "type": "agentMessage", "text": "AGENT_TURN_TEXT"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "turn-agent", "status": "completed"}},
                        }
                    )

            srv = _MockAppServer(
                threads=threads,
                on_dispatch=on_dispatch,
                unix_path=real_sock,
                no_rollout_thread_ids={"thread-zero-fail"},
            )
            try:
                env = {"CODEX_HOME": str(codex_home)}
                zf_proc = _run_drive(
                    [
                        "attach",
                        "--session",
                        "thread-zero-fail",
                        "--mode",
                        "scout",
                        "--timeout",
                        "2",
                        "--prompt",
                        "0-turn fail test",
                    ],
                    env=env,
                )
                self.assertEqual(zf_proc.returncode, drive.EXIT_ERROR)
                zf_data = json.loads(zf_proc.stdout)
                self.assertEqual(zf_data["status"], "failed")
                self.assertIn("0-turn sandbox setup failed", zf_data["errors"])

                scenario["mode"] = "model_after_pending_idle"
                srv.replay_on_resume = [
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"threadId": "thread-race", "turn": {"id": "prior", "status": "completed"}},
                    }
                ]
                ov_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "scout",
                        "--model",
                        "gpt-6.2-astra",
                        "--timeout",
                        "2",
                        "--prompt",
                        "run after pending turn/completed",
                    ],
                    env=env,
                )
                self.assertEqual(ov_proc.returncode, drive.EXIT_OK, ov_proc.stdout + ov_proc.stderr)
                self.assertEqual(json.loads(ov_proc.stdout)["final_message"], "override turn ok")

                srv.replay_on_resume = []
                srv.threads["thread-race"]["status"] = {"type": "idle"}
                scenario["mode"] = "simultaneous_enter"
                sim_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "scout",
                        "--timeout",
                        "2",
                        "--prompt",
                        "race against human enter",
                    ],
                    env=env,
                )
                self.assertEqual(sim_proc.returncode, drive.EXIT_OK, sim_proc.stdout + sim_proc.stderr)
                sim_data = json.loads(sim_proc.stdout)
                self.assertEqual(sim_data["final_message"], "AGENT_TURN_TEXT")
            finally:
                srv.close()

    def test_zero_turn_empty_rollout_minus_32603_recovery_and_hard_error_propagation(self) -> None:
        self.assertTrue(
            drive._is_unmaterialized_rollout_error(
                drive.JsonRpcError(
                    "thread/resume",
                    -32603,
                    "failed to read session metadata: rollout at /tmp/rollout-0.jsonl is empty",
                )
            )
        )
        self.assertTrue(
            drive._is_unmaterialized_rollout_error(
                drive.JsonRpcError("thread/resume", -32600, "No Rollout Found for thread id 123")
            )
        )
        self.assertFalse(
            drive._is_unmaterialized_rollout_error(
                drive.JsonRpcError("thread/resume", -32603, "sqlite disk io error")
            )
        )

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            (codex_home / "app-server-control").mkdir(parents=True)
            real_sock = Path(sock_td) / "empty-rollout.sock"
            (codex_home / "app-server-control" / "app-server-control.sock").symlink_to(real_sock)

            threads = [
                {
                    "id": "thread-empty-rollout",
                    "cwd": str(repo),
                    "name": "Fresh 0-byte rollout TUI",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                },
                {
                    "id": "thread-hard-error",
                    "cwd": str(root),
                    "name": "Broken thread",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                },
            ]

            def on_dispatch(
                ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer
            ) -> None:
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"queuedSubmission": {"id": "q-emp", "clientUserMessageId": cid, "input": []}},
                    }
                )

                def _emit_after_retry() -> None:
                    time.sleep(0.08)
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {"threadId": tid, "turn": {"id": "t-emp", "status": "inProgress"}},
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/started",
                            "params": {
                                "threadId": tid,
                                "turnId": "t-emp",
                                "item": {"id": "u-emp", "type": "userMessage", "clientId": cid},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": "t-emp",
                                "item": {"id": "m-emp", "type": "agentMessage", "text": "RECOVERED_EMPTY_ROLLOUT"},
                            },
                        }
                    )
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"threadId": tid, "turn": {"id": "t-emp", "status": "completed"}},
                        }
                    )

                threading.Thread(target=_emit_after_retry, daemon=True).start()

            srv = _MockAppServer(
                threads=threads,
                on_dispatch=on_dispatch,
                unix_path=real_sock,
                empty_rollout_counts={"thread-empty-rollout": 3},
                hard_resume_error_threads={"thread-hard-error": "sqlite disk io error"},
            )
            try:
                env = {"CODEX_HOME": str(codex_home)}
                ok_proc = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "scout",
                        "--timeout",
                        "3",
                        "--prompt",
                        "recover empty rollout",
                    ],
                    env=env,
                )
                self.assertEqual(ok_proc.returncode, drive.EXIT_OK, ok_proc.stdout + ok_proc.stderr)
                ok_data = json.loads(ok_proc.stdout)
                self.assertEqual(ok_data["status"], "completed")
                self.assertEqual(ok_data["final_message"], "RECOVERED_EMPTY_ROLLOUT")
                self.assertEqual(srv.empty_rollout_counts["thread-empty-rollout"], 0)
                self.assertIn("thread-empty-rollout", srv.unsubscribed_threads)

                hard_proc = _run_drive(
                    [
                        "attach",
                        "--session",
                        "thread-hard-error",
                        "--mode",
                        "scout",
                        "--timeout",
                        "2",
                        "--prompt",
                        "should fail closed",
                    ],
                    env=env,
                )
                self.assertEqual(hard_proc.returncode, drive.EXIT_ERROR)
                hard_data = json.loads(hard_proc.stdout)
                self.assertEqual(hard_data["error"], "runtime_error")
                self.assertIn("sqlite disk io error", hard_data["message"])
            finally:
                srv.close()

    def test_local_daemon_autostart_attach_create_and_run_daemon_fast(self) -> None:
        receipt_obj = {
            "summary": "Completed warm daemon ship slice.",
            "changed_files": ["README.md"],
            "verification": [{"command": "python3 -m unittest", "exit_code": 0, "summary": "ok"}],
            "assumptions_and_risks": [],
            "symbol_changes": [],
        }
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory(dir="/tmp") as sock_td:
            root = Path(td)
            repo = _init_temp_repo(root)
            codex_home = root / "codex-home"
            codex_home.mkdir(parents=True)
            real_sock = Path(sock_td) / "autostart.sock"
            start_log = root / "daemon_starts.log"

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:3] == ['app-server', 'daemon', 'start']:\n"
                f"    open({str(start_log)!r}, 'a', encoding='utf-8').write('started\\n')\n"
                "    ch = pathlib.Path(os.environ['CODEX_HOME']) / 'app-server-control'\n"
                "    ch.mkdir(parents=True, exist_ok=True)\n"
                "    sock = ch / 'app-server-control.sock'\n"
                "    if not sock.exists():\n"
                f"        sock.symlink_to(pathlib.Path({str(real_sock)!r}))\n"
                "    sys.exit(0)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

            dispatched_inputs: list[tuple[str, dict]] = []

            def on_dispatch(
                ws: _MockWsConnection, method: str, req_id: int, params: dict, srv: _MockAppServer
            ) -> None:
                dispatched_inputs.append((method, dict(params)))
                tid = params["threadId"]
                cid = params["clientUserMessageId"]
                turn_id = f"turn-{len(dispatched_inputs)}"
                if method == "turn/start":
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"turn": {"id": turn_id, "status": "inProgress"}},
                        }
                    )
                else:
                    ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"queuedSubmission": {"id": "q-1", "clientUserMessageId": cid, "input": []}},
                        }
                    )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}},
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/started",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {"id": "u1", "type": "userMessage", "clientId": cid},
                        },
                    }
                )
                reply_text = (
                    json.dumps(receipt_obj)
                    if "outputSchema" in params
                    else f"warm reply on {tid}"
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {"id": "m1", "type": "agentMessage", "text": reply_text},
                        },
                    }
                )
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"threadId": tid, "turn": {"id": turn_id, "status": "completed"}},
                    }
                )

            srv = _MockAppServer(threads=[], on_dispatch=on_dispatch, unix_path=real_sock)
            try:
                git_dir = str(Path(drive.shutil.which("git") or "/usr/bin/git").parent)
                env = {
                    "CODEX_HOME": str(codex_home),
                    "PATH": f"{fake_bin}:{git_dir}",
                }

                offline_ses = _run_drive(["sessions", "--cwd", str(repo)], env=env)
                self.assertEqual(offline_ses.returncode, drive.EXIT_ERROR)
                self.assertEqual(json.loads(offline_ses.stdout)["error"], "daemon_offline")
                self.assertFalse(start_log.exists())

                custom_remote_att = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--create",
                        "--remote",
                        f"unix://{root / 'custom-missing.sock'}",
                        "--prompt",
                        "do not autostart custom socket",
                    ],
                    env=env,
                )
                self.assertEqual(custom_remote_att.returncode, drive.EXIT_ERROR)
                self.assertFalse(start_log.exists())

                no_auto_att = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--create",
                        "--no-auto-daemon",
                        "--prompt",
                        "opt out of autostart",
                    ],
                    env=env,
                )
                self.assertEqual(no_auto_att.returncode, drive.EXIT_ERROR)
                self.assertFalse(start_log.exists())

                created_att = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--create",
                        "--fast",
                        "--mode",
                        "ship",
                        "--effort",
                        "high",
                        "--model",
                        "gpt-6.2-sol",
                        "--prompt",
                        "Create warm session and run slice.",
                    ],
                    env=env,
                )
                self.assertEqual(created_att.returncode, drive.EXIT_OK, created_att.stdout + created_att.stderr)
                self.assertEqual(len(start_log.read_text(encoding="utf-8").splitlines()), 1)
                created_summary = json.loads(created_att.stdout)
                created_tid = created_summary["session_id"]
                self.assertEqual(created_tid, "019d-started-0001")
                self.assertEqual(created_summary["status"], "completed")
                self.assertEqual(srv.archived_threads, [])
                self.assertIn(created_tid, srv.unsubscribed_threads)
                self.assertEqual(srv.started_thread_params[0]["cwd"], str(repo.resolve()))
                self.assertEqual(srv.started_thread_params[0]["model"], "gpt-6.2-sol")
                self.assertEqual(srv.started_thread_params[0]["sandbox"], "workspace-write")
                self.assertEqual(srv.started_thread_params[0]["approvalPolicy"], "never")
                self.assertEqual(srv.started_thread_params[0]["serviceTier"], "priority")
                self.assertEqual(
                    srv.started_thread_params[0]["config"],
                    {"model_reasoning_effort": "high"},
                )
                first_method, first_turn = dispatched_inputs[0]
                self.assertEqual(first_method, "turn/start")
                self.assertEqual(first_turn["serviceTier"], "priority")
                self.assertEqual(first_turn["effort"], "high")
                self.assertTrue(
                    first_turn["input"][0]["text"].startswith(drive.SUBAGENT_DISPATCH_PREAMBLE)
                )

                reuse_att = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--mode",
                        "ship",
                        "--prompt",
                        "Second turn reuses warm session.",
                    ],
                    env=env,
                )
                self.assertEqual(reuse_att.returncode, drive.EXIT_OK, reuse_att.stdout + reuse_att.stderr)
                self.assertEqual(json.loads(reuse_att.stdout)["session_id"], created_tid)
                self.assertEqual(len(srv.started_thread_params), 1)

                srv.threads["019d-second-ambig"] = {
                    "id": "019d-second-ambig",
                    "cwd": str(repo),
                    "name": "Second TUI",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                }
                ambig_create = _run_drive(
                    [
                        "attach",
                        "--cwd",
                        str(repo),
                        "--create",
                        "--prompt",
                        "must never create on ambiguous",
                    ],
                    env=env,
                )
                self.assertEqual(ambig_create.returncode, drive.EXIT_INVALID_ARGS)
                self.assertEqual(json.loads(ambig_create.stdout)["error"], "ambiguous_tui_threads")
                self.assertEqual(len(srv.started_thread_params), 1)
                srv.threads.pop("019d-second-ambig", None)

                schema_path = SKILL_ROOT / "schemas" / "receipt.schema.json"
                daemon_run = _run_drive(
                    [
                        "run",
                        "--worker",
                        "codex",
                        "--daemon",
                        "--fast",
                        "--mode",
                        "ship",
                        "--effort",
                        "xhigh",
                        "--model",
                        "gpt-6.2-sol",
                        "--schema",
                        str(schema_path),
                        "--cwd",
                        str(repo),
                        "--prompt",
                        "Ephemeral daemon ship run.",
                    ],
                    env=env,
                )
                self.assertEqual(daemon_run.returncode, drive.EXIT_OK, daemon_run.stdout + daemon_run.stderr)
                daemon_summary = json.loads(daemon_run.stdout)
                daemon_tid = daemon_summary["session_id"]
                self.assertEqual(daemon_tid, "019d-started-0002")
                self.assertTrue(daemon_summary["receipt_valid"])
                self.assertIn(daemon_tid, srv.unsubscribed_threads)
                self.assertIn(daemon_tid, srv.archived_threads)
                self.assertNotIn(daemon_tid, srv.threads)
                self.assertEqual(
                    srv.started_thread_params[1]["config"],
                    {"model_reasoning_effort": "xhigh", "mcp_servers": {}},
                )
                self.assertEqual(srv.started_thread_params[1]["serviceTier"], "priority")

                keep_loaded_run = _run_drive(
                    [
                        "run",
                        "--worker",
                        "codex",
                        "--daemon",
                        "--keep-loaded",
                        "--with-mcp",
                        "--raw-prompt",
                        "--mode",
                        "scout",
                        "--cwd",
                        str(repo),
                        "--prompt",
                        "Raw scout kept loaded.",
                    ],
                    env=env,
                )
                self.assertEqual(keep_loaded_run.returncode, drive.EXIT_OK)
                kept_tid = json.loads(keep_loaded_run.stdout)["session_id"]
                self.assertNotIn(kept_tid, srv.archived_threads)
                self.assertIn(kept_tid, srv.threads)
                self.assertEqual(
                    srv.started_thread_params[2]["config"],
                    {"model_reasoning_effort": "medium"},
                )
                self.assertEqual(srv.started_thread_params[2]["sandbox"], "read-only")
                self.assertEqual(dispatched_inputs[-1][1]["input"][0]["text"], "Raw scout kept loaded.")

                ensure_ses = _run_drive(["sessions", "--cwd", str(repo), "--ensure-daemon"], env=env)
                self.assertEqual(ensure_ses.returncode, drive.EXIT_OK)
                ses_ids = {s["thread_id"] for s in json.loads(ensure_ses.stdout)["sessions"]}
                self.assertEqual(ses_ids, {created_tid, kept_tid})
            finally:
                srv.close()

    def test_pipelined_request_batch_and_direct_session_id_resolution(self) -> None:
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("127.0.0.1", 0))
        port = srv_sock.getsockname()[1]
        srv_sock.listen(2)
        burst_verified = {"all_three_before_reply": False}

        def _serve_pipelined() -> None:
            conn, _ = srv_sock.accept()
            ws = _MockWsConnection(conn)
            try:
                ws.handshake()
                init_msg = ws.recv_json()
                assert init_msg is not None
                ws.send_json({"jsonrpc": "2.0", "id": init_msg["id"], "result": {"userAgent": "mock"}})
                inited = ws.recv_json()
                assert inited is not None and inited.get("method") == "initialized"

                r1 = ws.recv_json()
                r2 = ws.recv_json()
                r3 = ws.recv_json()
                assert r1 is not None and r2 is not None and r3 is not None
                burst_verified["all_three_before_reply"] = True

                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "remoteControl/status/changed",
                        "params": {"enabled": True},
                    }
                )
                ws.send_json({"jsonrpc": "2.0", "id": r3["id"], "result": {"thread": {"id": "t3"}}})
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": r2["id"],
                        "error": {"code": -32600, "message": "unknown thread t2"},
                    }
                )
                ws.send_json({"jsonrpc": "2.0", "id": r1["id"], "result": {"thread": {"id": "t1"}}})
            finally:
                with mock.patch.object(socket.socket, "close", wraps=conn.close):
                    conn.close()
                srv_sock.close()

        t = threading.Thread(target=_serve_pipelined, daemon=True)
        t.start()
        endpoint = drive.resolve_daemon_endpoint(remote=f"ws://127.0.0.1:{port}/rpc")
        rpc = drive.AppServerRpcClient(endpoint, connect_timeout=3.0)
        try:
            deadline = time.monotonic() + 3.0
            rpc.initialize(deadline=deadline)
            batch_out = rpc.request_batch(
                [
                    ("thread/read", {"threadId": "t1"}),
                    ("thread/read", {"threadId": "t2"}),
                    ("thread/read", {"threadId": "t3"}),
                ],
                deadline=deadline,
            )
            self.assertTrue(burst_verified["all_three_before_reply"])
            self.assertEqual(len(batch_out), 3)
            self.assertEqual(batch_out[0], {"thread": {"id": "t1"}})
            self.assertIsInstance(batch_out[1], drive.JsonRpcError)
            self.assertEqual(batch_out[2], {"thread": {"id": "t3"}})
            self.assertTrue(rpc.has_pending())
            notif = rpc.next_message(deadline=deadline)
            self.assertEqual(notif.get("method"), "remoteControl/status/changed")
        finally:
            rpc.close()
            t.join(timeout=2.0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _init_temp_repo(root)
            threads = [
                {
                    "id": f"thread-other-{i}",
                    "cwd": str(root),
                    "name": f"Other {i}",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                }
                for i in range(4)
            ] + [
                {
                    "id": "thread-target",
                    "cwd": str(repo),
                    "name": "Target",
                    "preview": "",
                    "ephemeral": False,
                    "parentThreadId": None,
                    "source": "cli",
                    "status": {"type": "idle"},
                }
            ]
            srv = _MockAppServer(threads=threads, unix_path=None)
            try:
                ep = drive.resolve_daemon_endpoint(remote=f"ws://127.0.0.1:{srv.port}/rpc")
                client = drive.AppServerRpcClient(ep, connect_timeout=3.0)
                try:
                    dl = time.monotonic() + 3.0
                    client.initialize(deadline=dl)
                    res = drive._resolve_thread_with_client(
                        rpc=client,
                        endpoint=ep,
                        codex_home=root / "empty-home",
                        target_cwd=None,
                        session_id="thread-target",
                        select_latest=False,
                        deadline=dl,
                    )
                    self.assertEqual(res.kind, "matched")
                    assert res.thread is not None
                    self.assertEqual(res.thread.thread_id, "thread-target")
                    read_msgs = [
                        m for m in srv.received_messages if m.get("method") == "thread/read"
                    ]
                    self.assertEqual(len(read_msgs), 1)
                    self.assertEqual(read_msgs[0]["params"]["threadId"], "thread-target")
                finally:
                    client.close()
            finally:
                srv.close()


if __name__ == "__main__":
    unittest.main()
