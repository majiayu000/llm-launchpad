import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPAT_LABEL = "com.local.qwen38-ollama-compat"


class LaunchdEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp = Path(temp_dir.name)
        self.bin_dir = self.temp / "bin"
        self.bin_dir.mkdir()
        self.capture = self.temp / "launchctl.txt"
        self.write_command(
            "launchctl",
            '#!/bin/zsh\nprintf "%s\\n" "$*" >> "$TEST_LAUNCHCTL_CAPTURE"\n'
            '[[ "$1" != print ]]\n',
        )
        self.write_command("curl", '#!/bin/zsh\nprintf \'{"version":"test-version"}\\n\'\n')
        self.write_command("ollama", "#!/bin/zsh\nexit 0\n")
        self.write_command(
            "claude",
            f"#!{sys.executable}\nimport json, os\n"
            'print(json.dumps({"token": os.environ["ANTHROPIC_AUTH_TOKEN"]}))\n',
        )
        self.env = os.environ.copy()
        for name in tuple(self.env):
            if name.startswith("QWEN38_"):
                del self.env[name]
        self.env.update({
            "HOME": str(self.temp / "home"),
            "PATH": f"{self.bin_dir}:{self.env['PATH']}",
            "QWEN38_OLLAMA_BIN": str(self.bin_dir / "ollama"),
            "QWEN38_PYTHON_BIN": sys.executable,
            "TEST_LAUNCHCTL_CAPTURE": str(self.capture),
        })
        self.installed = Path(self.env["HOME"]) / "Library/LaunchAgents" / f"{COMPAT_LABEL}.plist"

    def write_command(self, name, content):
        command = self.bin_dir / name
        command.write_text(content)
        command.chmod(0o755)

    def run_script(self, script):
        return subprocess.run(
            [str(ROOT / "scripts" / script)], cwd=ROOT, env=self.env,
            capture_output=True, text=True, timeout=15,
        )

    def start(self):
        result = self.run_script("start.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.installed.open("rb") as handle:
            return plistlib.load(handle)

    def proxy_settings(self, settings):
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import json, sys; sys.path.insert(0, sys.argv[1]); "
                "import anthropic_proxy, meter; "
                "print(json.dumps({'QWEN38_COMPAT_HOST': anthropic_proxy.LISTEN_HOST, "
                "'QWEN38_COMPAT_TOKEN': anthropic_proxy.COMPAT_TOKEN, "
                "'QWEN38_METER_FILE': meter.meter_path()}))",
                str(self.installed.parents[2] / ".local/share/qwen38-ollama/compat"),
            ],
            env={"HOME": self.env["HOME"], **settings},
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_unset_values_preserve_defaults_and_client_token(self):
        settings = self.start()["EnvironmentVariables"]
        self.assertEqual(settings["QWEN38_COMPAT_TOKEN"], "ollama")
        self.assertEqual(settings["QWEN38_COMPAT_HOST"], "127.0.0.1")
        self.assertNotIn("QWEN38_METER_FILE", settings)
        proxy = self.proxy_settings(settings)
        self.assertEqual(proxy["QWEN38_COMPAT_TOKEN"], "ollama")
        self.assertEqual(proxy["QWEN38_COMPAT_HOST"], "127.0.0.1")
        self.assertEqual(proxy["QWEN38_METER_FILE"], "/tmp/qwen38-ollama-meter.json")
        client = self.run_script("claude-code.sh")
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(json.loads(client.stdout)["token"], settings["QWEN38_COMPAT_TOKEN"])

    def test_custom_values_survive_xml_and_match_client(self):
        self.env.update({
            "QWEN38_COMPAT_TOKEN": "dummy<&|\\\"'token",
            "QWEN38_COMPAT_HOST": "0.0.0.0",
            "QWEN38_METER_FILE": str(self.temp / "meter & <test>.json"),
        })
        settings = self.start()["EnvironmentVariables"]
        for name in ("QWEN38_COMPAT_TOKEN", "QWEN38_COMPAT_HOST", "QWEN38_METER_FILE"):
            self.assertEqual(settings[name], self.env[name])
        self.assertEqual(self.proxy_settings(settings), {name: self.env[name] for name in (
            "QWEN38_COMPAT_TOKEN", "QWEN38_COMPAT_HOST", "QWEN38_METER_FILE",
        )})
        client = self.run_script("claude-code.sh")
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(json.loads(client.stdout)["token"], settings["QWEN38_COMPAT_TOKEN"])

    def test_restart_rotates_token_and_removes_stale_meter_override(self):
        self.env.update({"QWEN38_COMPAT_TOKEN": "dummy-first", "QWEN38_METER_FILE": "dummy-meter"})
        self.assertEqual(self.start()["EnvironmentVariables"]["QWEN38_COMPAT_TOKEN"], "dummy-first")
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-second"
        del self.env["QWEN38_METER_FILE"]
        settings = self.start()["EnvironmentVariables"]
        self.assertEqual(settings["QWEN38_COMPAT_TOKEN"], "dummy-second")
        self.assertNotIn("QWEN38_METER_FILE", settings)

    def test_explicit_empty_values_remain_empty_on_loopback(self):
        self.env.update({"QWEN38_COMPAT_TOKEN": "", "QWEN38_METER_FILE": ""})
        settings = self.start()["EnvironmentVariables"]
        self.assertEqual(settings["QWEN38_COMPAT_TOKEN"], "")
        self.assertEqual(settings["QWEN38_METER_FILE"], "")
        proxy = self.proxy_settings(settings)
        self.assertEqual(proxy["QWEN38_COMPAT_TOKEN"], "")
        self.assertEqual(proxy["QWEN38_METER_FILE"], "")

    def test_non_loopback_empty_token_does_not_replace_or_bootstrap_agent(self):
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-original"
        self.start()
        original = self.installed.read_bytes()
        self.capture.write_text("")
        self.env.update({"QWEN38_COMPAT_HOST": "0.0.0.0", "QWEN38_COMPAT_TOKEN": ""})
        result = self.run_script("start.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to bind", result.stderr)
        self.assertEqual(self.installed.read_bytes(), original)
        self.assertNotIn(COMPAT_LABEL, self.capture.read_text())

    def test_generated_token_is_owner_private_even_when_replacing_public_plist(self):
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text("old plist")
        self.installed.chmod(0o644)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-private"
        self.start()
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
