from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

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
            self.assertEqual(summary["receipt"]["changed_files"], ["src/retry.py", "tests/test_retry.py"])


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


class RepositoryContractTests(unittest.TestCase):
    def test_manifests_and_schema_are_valid(self) -> None:
        agy = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(agy["$schema"], "https://antigravity.google/schemas/v1/plugin.json")
        self.assertEqual(agy["name"], "codex-use")
        self.assertEqual(agy["license"], "Apache-2.0")
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
            ["changed_files", "verification", "assumptions_and_risks", "symbol_changes"],
        )

    def test_skill_frontmatter_and_self_contained_relative_links(self) -> None:
        skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        fm_match = re.match(r"^---\n(.*?)\n---\n", skill_md, re.DOTALL)
        self.assertIsNotNone(fm_match, "SKILL.md must start with YAML frontmatter")
        assert fm_match is not None
        fm = fm_match.group(1)
        self.assertRegex(fm, r"(?m)^name:\s*codex-use\s*$", "SKILL.md name must be codex-use")
        self.assertRegex(fm, r"(?m)^description:\s*.+$", "SKILL.md must include description")

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
