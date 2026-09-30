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
        self.curl_capture = self.temp / "curl.jsonl"
        self.write_command(
            "launchctl",
            '#!/bin/zsh\nprintf "%s\\n" "$*" >> "$TEST_LAUNCHCTL_CAPTURE"\n'
            '[[ "$1" != print ]]\n',
        )
        self.write_command(
            "curl",
            f"#!{sys.executable}\nimport json, os, sys\n"
            'url = sys.argv[-1]\n'
            'with open(os.environ["TEST_CURL_CAPTURE"], "a") as handle:\n'
            '    handle.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'expected = os.environ.get("TEST_COMPAT_URL")\n'
            'if ":11440/" in url and expected and url != expected:\n'
            '    sys.exit(1)\n'
            'print(json.dumps({"version": "test-version"}))\n',
        )
        self.write_command("sleep", "#!/bin/zsh\nexit 0\n")
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
            "TEST_CURL_CAPTURE": str(self.curl_capture),
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

    def test_readiness_uses_configured_host_and_loopback_for_wildcard(self):
        for host, probe_host in (
            ("192.0.2.10", "192.0.2.10"), ("localhost", "localhost"),
            ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
            ("", "127.0.0.1"),
        ):
            with self.subTest(host=host):
                self.env.update({
                    "QWEN38_COMPAT_HOST": host, "QWEN38_COMPAT_TOKEN": "dummy-host",
                    "TEST_COMPAT_URL": f"http://{probe_host}:11440/api/version",
                })
                self.curl_capture.write_text("")
                self.start()
                probes = [json.loads(line)[-1] for line in self.curl_capture.read_text().splitlines()]
                compat_probes = [url for url in probes if ":11440/" in url]
                self.assertEqual(compat_probes, [self.env["TEST_COMPAT_URL"]])

    def test_rejected_config_preserves_installed_symlink_and_target(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(target)
        self.env.update({"QWEN38_COMPAT_HOST": "0.0.0.0", "QWEN38_COMPAT_TOKEN": ""})
        result = self.run_script("start.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to bind", result.stderr)
        self.assertTrue(self.installed.is_symlink())
        self.assertEqual(self.installed.readlink(), target)
        self.assertEqual(target.read_bytes(), b"previous plist")
        self.assertNotIn(COMPAT_LABEL, self.capture.read_text())

    def test_render_failure_preserves_installed_symlink_and_target(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(target)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy\x01token"
        result = self.run_script("start.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ValueError", result.stderr)
        self.assertTrue(self.installed.is_symlink())
        self.assertEqual(target.read_bytes(), b"previous plist")
        self.assertNotIn(COMPAT_LABEL, self.capture.read_text())
        self.assertEqual(list(self.installed.parent.glob(".launchd-*")), [])

    def test_success_replaces_symlink_without_modifying_target(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        target.chmod(0o644)
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(target)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-replacement"
        settings = self.start()["EnvironmentVariables"]
        self.assertEqual(settings["QWEN38_COMPAT_TOKEN"], "dummy-replacement")
        self.assertFalse(self.installed.is_symlink())
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_bytes(), b"previous plist")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.installed.parent.glob(".launchd-*")), [])

    def prepare_meter(self):
        snapshot = self.temp / "stale-meter.json"
        snapshot.write_text(json.dumps({"last_completed": {"tok_s": 9876.5}}))
        self.env["TEST_METER_SNAPSHOT"] = str(snapshot)
        # Execute the actual meter Python body for one frame. Redirect its default
        # path to a dummy snapshot so no user's meter file is read or overwritten.
        self.write_command(
            "python3",
            f"#!{sys.executable}\nimport builtins, os, sys, time\n"
            'original_open = builtins.open\n'
            'def open_snapshot(path, *args, **kwargs):\n'
            '    if path == "/tmp/qwen38-ollama-meter.json":\n'
            '        path = os.environ["TEST_METER_SNAPSHOT"]\n'
            '    return original_open(path, *args, **kwargs)\n'
            'builtins.open = open_snapshot\n'
            'def stop_after_frame(_):\n'
            '    raise KeyboardInterrupt\n'
            'time.sleep = stop_after_frame\n'
            'exec(compile(sys.stdin.read(), "meter.sh", "exec"))\n',
        )
        return snapshot

    def test_empty_meter_does_not_display_stale_default_snapshot(self):
        self.prepare_meter()
        self.env["QWEN38_METER_FILE"] = ""
        result = self.run_script("meter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已关闭", result.stdout)
        self.assertNotIn("9876.5", result.stdout)
        self.assertNotIn("暂无数据", result.stderr)

    def test_meter_preserves_unset_default_and_custom_path(self):
        snapshot = self.prepare_meter()
        for path in (None, str(snapshot)):
            with self.subTest(path=path):
                if path is None:
                    self.env.pop("QWEN38_METER_FILE", None)
                else:
                    self.env["QWEN38_METER_FILE"] = path
                result = self.run_script("meter.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("9876.5", result.stdout)

    def test_generated_token_is_owner_private_even_when_replacing_public_plist(self):
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text("old plist")
        self.installed.chmod(0o644)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-private"
        self.start()
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
