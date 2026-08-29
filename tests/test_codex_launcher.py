import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "codex.sh"


class CodexLauncherTests(unittest.TestCase):
    def test_uses_isolated_local_provider_and_forwards_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            capture = temp / "capture.txt"
            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                "#!/bin/zsh\n"
                "print -r -- \"CODEX_HOME=$CODEX_HOME\" > \"$CAPTURE_FILE\"\n"
                "printf \"ARG=%s\\n\" \"$@\" >> \"$CAPTURE_FILE\"\n"
            )
            fake_codex.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp / "home"),
                    "PATH": f"{bin_dir}:{env['PATH']}",
                    "CAPTURE_FILE": str(capture),
                    "QWEN38_MODEL": "example/uncensored:27b",
                }
            )
            result = subprocess.run(
                [str(LAUNCHER), "exec", "只回答 OK"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            output = capture.read_text()
            expected_home = temp / "home" / ".local/share/qwen38-ollama/codex"
            self.assertIn(f"CODEX_HOME={expected_home}", output)
            self.assertIn("ARG=--model\nARG=example/uncensored:27b", output)
            self.assertIn('ARG=model_provider="qwen38_local"', output)
            self.assertIn(
                'ARG=model_providers.qwen38_local.base_url="http://127.0.0.1:11439/v1"',
                output,
            )
            self.assertIn('ARG=model_providers.qwen38_local.wire_api="responses"', output)
            self.assertIn('ARG=approval_policy="on-request"', output)
            self.assertIn('ARG=sandbox_mode="workspace-write"', output)
            self.assertTrue(output.endswith("ARG=exec\nARG=只回答 OK\n"))

    def test_fails_clearly_when_codex_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.update({"HOME": temp_dir, "PATH": "/usr/bin:/bin"})
            result = subprocess.run(
                [str(LAUNCHER)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("未找到 codex 命令", result.stderr)


if __name__ == "__main__":
    unittest.main()
