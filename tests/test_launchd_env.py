import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


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
            'print(json.dumps({"token": os.environ["ANTHROPIC_AUTH_TOKEN"], '
            '"base_url": os.environ["ANTHROPIC_BASE_URL"]}))\n',
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

    def run_script(self, script, cwd=ROOT):
        return subprocess.run(
            [str(ROOT / "scripts" / script)], cwd=cwd, env=self.env,
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

    def prepare_real_probes(self, host="127.0.0.1", token="dummy-probe", compat_handler=None):
        sys.path.insert(0, str(ROOT / "compat"))
        import anthropic_proxy

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"version": "dummy-version", "models": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = b'{"choices":[{"message":{"content":"OK"}}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler)
                   for handler in (Upstream, compat_handler or anthropic_proxy.CompatHandler)]
        settings = patch.multiple(anthropic_proxy, UPSTREAM_HOST="127.0.0.1",
                                  UPSTREAM_PORT=servers[0].server_port, COMPAT_TOKEN=token)
        settings.start()
        self.addCleanup(settings.stop)
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
        self.env.update({
            "TEST_REAL_CURL": shutil.which("curl"),
            "TEST_OLLAMA_URL": f"http://127.0.0.1:{servers[0].server_port}",
            "TEST_REAL_COMPAT_URL": f"http://127.0.0.1:{servers[1].server_port}",
            "TEST_COMPAT_URL": f"http://{host}:11440/api/version",
        })
        # Route only the expected synthetic endpoint to ephemeral loopback.
        # An incorrect host fails without reaching a real service or LAN address.
        self.write_command("curl", f"#!{sys.executable}\n" + '''import os, sys
args = sys.argv[1:]
for index, arg in enumerate(args):
    if arg.startswith("http://127.0.0.1:11439/"):
        args[index] = arg.replace("http://127.0.0.1:11439", os.environ["TEST_OLLAMA_URL"])
    elif arg == os.environ["TEST_COMPAT_URL"]:
        args[index] = os.environ["TEST_REAL_COMPAT_URL"] + "/api/version"
    elif arg.startswith("http://"):
        sys.exit(1)
os.execv(os.environ["TEST_REAL_CURL"], [os.environ["TEST_REAL_CURL"], *args])
''')
        return servers

    def test_readiness_authenticates_real_proxy_at_configured_endpoint(self):
        self.env.update({"QWEN38_COMPAT_HOST": "192.0.2.10", "QWEN38_COMPAT_TOKEN": "dummy-probe"})
        self.prepare_real_probes(host="192.0.2.10")
        result = self.run_script("start.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("dummy-probe", result.stdout + result.stderr)

    def test_status_authenticates_with_persisted_token_and_preserves_wrong_token_result(self):
        self.env.update({"QWEN38_COMPAT_HOST": "192.0.2.10", "QWEN38_COMPAT_TOKEN": "dummy-probe"})
        self.start()
        del self.env["QWEN38_COMPAT_HOST"], self.env["QWEN38_COMPAT_TOKEN"]
        self.write_command("launchctl", "#!/bin/zsh\nexit 0\n")
        self.prepare_real_probes(host="192.0.2.10")
        result = self.run_script("status.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["claude_compat_running"])
        self.assertNotIn("dummy-probe", result.stdout + result.stderr)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-wrong"
        result = self.run_script("status.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["claude_compat_running"])

    def test_repeat_install_probes_persisted_host_before_explicit_overrides(self):
        self.env.update({"QWEN38_COMPAT_HOST": "192.0.2.10", "QWEN38_COMPAT_TOKEN": "dummy-probe"})
        self.start()
        del self.env["QWEN38_COMPAT_HOST"], self.env["QWEN38_COMPAT_TOKEN"]
        self.prepare_real_probes(host="192.0.2.10")
        self.write_command("uname", '#!/bin/zsh\n[[ "$1" == -s ]] && echo Darwin || echo arm64\n')
        self.write_command("sysctl", "#!/bin/zsh\necho 34359738368\n")
        self.write_command("df", "#!/bin/zsh\nprintf 'Filesystem Blocks Used Available\nfixture 0 0 104857600\n'\n")
        self.write_command("lsof", "#!/bin/zsh\nexit 0\n")
        for host in (None, "localhost", "0.0.0.0", ""):
            with self.subTest(host=host):
                if host is not None:
                    self.env["QWEN38_COMPAT_HOST"] = host
                result = subprocess.run([str(ROOT / "install.sh"), "--check"], cwd=ROOT,
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("预检通过", result.stdout)
                self.assertNotIn("dummy-probe", result.stdout + result.stderr)
        self.env.update({"QWEN38_COMPAT_HOST": "192.0.2.10", "QWEN38_COMPAT_TOKEN": "dummy-probe",
                         "TEST_COMPAT_URL": "http://192.0.2.10:11440/api/version"})
        result = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("安装完成", result.stdout)
        self.assertNotIn("dummy-probe", result.stdout + result.stderr)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-rotated"
        result = subprocess.run([str(ROOT / "install.sh"), "--check"], cwd=ROOT,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = plistlib.loads(self.installed.read_bytes())
        settings["EnvironmentVariables"]["QWEN38_COMPAT_TOKEN"] = "dummy-wrong"
        self.installed.write_bytes(plistlib.dumps(settings))
        result = subprocess.run([str(ROOT / "install.sh"), "--check"], cwd=ROOT,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("端口 11440 已被其他程序占用", result.stderr)

    def test_repeat_install_rotates_running_token_and_host_after_preflight(self):
        sys.path.insert(0, str(ROOT / "compat"))
        import anthropic_proxy

        phase = self.temp / "compat-bootstrapped"
        installed = self.installed

        class ReloadedCompat(anthropic_proxy.CompatHandler):
            def do_GET(handler):
                token = self.env["TEST_OLD_TOKEN"]
                if phase.exists():
                    token = plistlib.loads(installed.read_bytes())["EnvironmentVariables"]["QWEN38_COMPAT_TOKEN"]
                with patch.object(anthropic_proxy, "COMPAT_TOKEN", token):
                    super().do_GET()

        for old_host, old_token, new_host, new_token in (
            ("127.0.0.1", "dummy-old", "127.0.0.1", "dummy-new"),
            ("127.0.0.1", "dummy-old", "127.0.0.1", ""),
            ("127.0.0.1", "", "localhost", "dummy-new"),
            ("0.0.0.0", "dummy-old", "192.0.2.10", "dummy-new"),
            ("192.0.2.10", "dummy-old", "", "dummy-new"),
        ):
            with self.subTest(old_host=old_host, old_token=old_token, new_host=new_host, new_token=new_token):
                phase.unlink(missing_ok=True)
                self.write_command("launchctl", "#!/bin/zsh\nexit 0\n")
                self.write_command("curl", "#!/bin/zsh\necho '{\"version\":\"dummy-version\"}'\n")
                self.env.update({"QWEN38_COMPAT_HOST": old_host, "QWEN38_COMPAT_TOKEN": old_token})
                self.start()
                old_endpoint = old_host if old_host and old_host != "0.0.0.0" else "127.0.0.1"
                new_endpoint = new_host if new_host and new_host != "0.0.0.0" else "127.0.0.1"
                self.prepare_real_probes(host=old_endpoint, token=old_token, compat_handler=ReloadedCompat)
                self.env.update({
                    "QWEN38_COMPAT_HOST": new_host, "QWEN38_COMPAT_TOKEN": new_token,
                    "QWEN38_SIM_FRESH": "1", "TEST_OLD_TOKEN": old_token,
                    "TEST_NEW_COMPAT_URL": f"http://{new_endpoint}:11440/api/version",
                    "TEST_BOOTSTRAPPED": str(phase),
                })
                self.write_command("uname", '#!/bin/zsh\n[[ "$1" == -s ]] && echo Darwin || echo arm64\n')
                self.write_command("sysctl", "#!/bin/zsh\necho 34359738368\n")
                self.write_command("df", "#!/bin/zsh\nprintf 'Filesystem Blocks Used Available\nfixture 0 0 104857600\n'\n")
                self.write_command("lsof", "#!/bin/zsh\nexit 0\n")
                self.write_command("launchctl", '#!/bin/zsh\n'
                                   'if [[ "$1" == bootstrap && "$3" == *qwen38-ollama-compat.plist ]]; then\n'
                                   '  touch "$TEST_BOOTSTRAPPED"\n'
                                   'fi\nexit 0\n')
                self.curl_capture.write_text("")
                self.write_command("curl", f"#!{sys.executable}\n" + r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["TEST_CURL_CAPTURE"], "a") as handle:
    handle.write(json.dumps(args) + "\n")
expected = os.environ["TEST_NEW_COMPAT_URL"] if Path(os.environ["TEST_BOOTSTRAPPED"]).exists() else os.environ["TEST_COMPAT_URL"]
for index, arg in enumerate(args):
    if arg.startswith("http://127.0.0.1:11439/"):
        args[index] = arg.replace("http://127.0.0.1:11439", os.environ["TEST_OLLAMA_URL"])
    elif arg == expected:
        args[index] = os.environ["TEST_REAL_COMPAT_URL"] + "/api/version"
    elif arg.startswith("http://"):
        sys.exit(1)
os.execv(os.environ["TEST_REAL_CURL"], [os.environ["TEST_REAL_CURL"], *args])
''')
                result = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT,
                                        env=self.env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("安装完成", result.stdout)
                current = plistlib.loads(self.installed.read_bytes())["EnvironmentVariables"]
                self.assertEqual(current["QWEN38_COMPAT_HOST"], new_host)
                self.assertEqual(current["QWEN38_COMPAT_TOKEN"], new_token)
                probes = [json.loads(line) for line in self.curl_capture.read_text().splitlines()]
                compat_probes = [args for args in probes if any(":11440/" in arg for arg in args)]
                self.assertEqual([(args[-1], args[args.index("-H") + 1]) for args in compat_probes], [
                    (f"http://{old_endpoint}:11440/api/version", f"x-api-key: {old_token}"),
                    (f"http://{new_endpoint}:11440/api/version", f"x-api-key: {new_token}"),
                ])
                self.assertNotIn("dummy-old", result.stdout + result.stderr)
                self.assertNotIn("dummy-new", result.stdout + result.stderr)

    def test_stalled_status_probe_returns_false_within_deadline(self):
        class StalledCompat(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.server.requests.append(handler.path)
                handler.server.release.wait(8)

            def log_message(handler, *args):
                pass

        _, server = self.prepare_real_probes(compat_handler=StalledCompat)
        server.requests = []
        server.release = threading.Event()
        self.addCleanup(server.release.set)
        self.write_command("launchctl", "#!/bin/zsh\nexit 0\n")
        started = time.monotonic()
        result = self.run_script("status.sh")
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["claude_compat_running"])
        self.assertLess(elapsed, 4)
        self.assertEqual(server.requests, ["/api/version"])

    def test_stalled_readiness_probes_exhaust_with_failure(self):
        class StalledCompat(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.server.requests.append(handler.path)
                handler.server.release.wait(45)

            def log_message(handler, *args):
                pass

        _, server = self.prepare_real_probes(compat_handler=StalledCompat)
        server.requests = []
        server.release = threading.Event()
        self.addCleanup(server.release.set)
        started = time.monotonic()
        result = subprocess.run([str(ROOT / "scripts/start.sh")], cwd=ROOT, env=self.env,
                                capture_output=True, text=True, timeout=40)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1)
        self.assertIn("Claude 兼容入口启动超时", result.stderr)
        self.assertLess(elapsed, 38)
        self.assertEqual(len(server.requests), 30)

    def test_header_unsafe_tokens_preserve_installed_files_and_symlinks(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        for symlink in (False, True):
            for token in ("密钥", "dummy-🔑", "dummy\x7ftoken", "dummy\ttoken"):
                with self.subTest(symlink=symlink, token=token):
                    if self.installed.exists() or self.installed.is_symlink():
                        self.installed.unlink()
                    if symlink:
                        self.installed.symlink_to(target)
                    else:
                        self.installed.write_bytes(b"previous plist")
                    self.capture.write_text("")
                    self.env["QWEN38_COMPAT_TOKEN"] = token
                    result = self.run_script("start.sh")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("HTTP header", result.stderr)
                    self.assertNotIn(token, result.stderr)
                    self.assertEqual(self.installed.is_symlink(), symlink)
                    self.assertEqual(self.installed.read_bytes(), b"previous plist")
                    self.assertEqual(target.read_bytes(), b"previous plist")
                    self.assertNotIn(COMPAT_LABEL, self.capture.read_text())
                    self.assertEqual(list(self.installed.parent.glob(".launchd-*")), [])

    def test_printable_token_with_internal_spaces_is_preserved(self):
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy internal space!~"
        self.assertEqual(self.start()["EnvironmentVariables"]["QWEN38_COMPAT_TOKEN"],
                         self.env["QWEN38_COMPAT_TOKEN"])

    def test_client_uses_recorded_interpreter_without_python_on_path(self):
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-recorded"
        self.start()
        state = Path(self.env["HOME"]) / ".local/share/qwen38-ollama"
        (state / "env.sh").write_text(f'export QWEN38_PYTHON_BIN="{sys.executable}"\n')
        (self.bin_dir / "mkdir").symlink_to(shutil.which("mkdir"))
        self.env["PATH"] = str(self.bin_dir)
        del self.env["QWEN38_PYTHON_BIN"], self.env["QWEN38_COMPAT_TOKEN"]
        self.assertIsNone(shutil.which("python3", path=self.env["PATH"]))
        result = self.run_script("claude-code.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "base_url": "http://127.0.0.1:11440", "token": "dummy-recorded",
        })

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

    def test_non_loopback_requires_explicit_nondefault_token(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        for symlink in (False, True):
            for host in ("0.0.0.0", "192.0.2.10", ""):
                for token in (None, "ollama"):
                    with self.subTest(symlink=symlink, host=host, token=token):
                        if self.installed.exists() or self.installed.is_symlink():
                            self.installed.unlink()
                        if symlink:
                            self.installed.symlink_to(target)
                        else:
                            self.installed.write_bytes(b"previous plist")
                        self.capture.write_text("")
                        self.env["QWEN38_COMPAT_HOST"] = host
                        if token is None:
                            self.env.pop("QWEN38_COMPAT_TOKEN", None)
                        else:
                            self.env["QWEN38_COMPAT_TOKEN"] = token
                        result = self.run_script("start.sh")
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("explicit non-default", result.stderr)
                        self.assertEqual(self.installed.is_symlink(), symlink)
                        self.assertEqual(self.installed.read_bytes(), b"previous plist")
                        self.assertEqual(target.read_bytes(), b"previous plist")
                        self.assertNotIn(COMPAT_LABEL, self.capture.read_text())
                        self.assertEqual(list(self.installed.parent.glob(".launchd-*")), [])

    def test_internal_cr_lf_tokens_are_rejected_before_compat_replacement(self):
        import http.client

        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        for symlink in (False, True):
            for token in ("dummy\nwrapped", "dummy\rwrapped", "dummy\r\nwrapped"):
                with self.subTest(symlink=symlink, token=token):
                    # The real HTTP client rejects these values before connecting.
                    connection = http.client.HTTPConnection("127.0.0.1", 9)
                    connection.putrequest("GET", "/api/version")
                    with self.assertRaises(ValueError):
                        connection.putheader("x-api-key", token)
                    connection.close()
                    if self.installed.exists() or self.installed.is_symlink():
                        self.installed.unlink()
                    if symlink:
                        self.installed.symlink_to(target)
                    else:
                        self.installed.write_bytes(b"previous plist")
                    self.capture.write_text("")
                    self.env["QWEN38_COMPAT_TOKEN"] = token
                    result = self.run_script("start.sh")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("CR or LF", result.stderr)
                    self.assertNotIn("dummy", result.stderr)
                    self.assertEqual(self.installed.is_symlink(), symlink)
                    self.assertEqual(self.installed.read_bytes(), b"previous plist")
                    self.assertEqual(target.read_bytes(), b"previous plist")
                    self.assertNotIn(COMPAT_LABEL, self.capture.read_text())

    def test_new_terminal_client_uses_installed_values_and_explicit_overrides(self):
        for host, endpoint in (("192.0.2.10", "192.0.2.10"), ("0.0.0.0", "127.0.0.1")):
            with self.subTest(host=host):
                self.env.update({"QWEN38_COMPAT_HOST": host, "QWEN38_COMPAT_TOKEN": "dummy-persisted"})
                self.start()
                del self.env["QWEN38_COMPAT_HOST"], self.env["QWEN38_COMPAT_TOKEN"]
                client = self.run_script("claude-code.sh")
                self.assertEqual(client.returncode, 0, client.stderr)
                self.assertEqual(json.loads(client.stdout), {
                    "base_url": f"http://{endpoint}:11440", "token": "dummy-persisted",
                })
                self.env.update({"QWEN38_COMPAT_HOST": "localhost", "QWEN38_COMPAT_TOKEN": ""})
                client = self.run_script("claude-code.sh")
                self.assertEqual(client.returncode, 0, client.stderr)
                self.assertEqual(json.loads(client.stdout), {
                    "base_url": "http://localhost:11440", "token": "",
                })

    def test_status_uses_effective_host_without_exposing_persisted_token(self):
        self.write_command("launchctl", "#!/bin/zsh\nexit 0\n")
        self.write_command(
            "curl", f"#!{sys.executable}\nimport json, os, sys\n"
            'with open(os.environ["TEST_CURL_CAPTURE"], "a") as handle:\n'
            '    handle.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'url = sys.argv[-1]\n'
            'if ":11440/" in url:\n'
            '    if url != os.environ["TEST_COMPAT_URL"]: sys.exit(1)\n'
            '    if "-H" in sys.argv and sys.argv[sys.argv.index("-H") + 1] != "x-api-key: dummy-status": sys.exit(1)\n'
            'print(json.dumps({"version": "dummy-version", "models": []}))\n',
        )
        for host, endpoint in (("192.0.2.10", "192.0.2.10"), ("0.0.0.0", "127.0.0.1")):
            self.env.update({"QWEN38_COMPAT_HOST": host, "QWEN38_COMPAT_TOKEN": "dummy-status"})
            self.env["TEST_COMPAT_URL"] = f"http://{endpoint}:11440/api/version"
            self.start()
            del self.env["QWEN38_COMPAT_HOST"], self.env["QWEN38_COMPAT_TOKEN"]
            for override in (None, "localhost"):
                with self.subTest(host=host, override=override):
                    if override is not None:
                        self.env["QWEN38_COMPAT_HOST"] = override
                    effective = override or endpoint
                    self.env["TEST_COMPAT_URL"] = f"http://{effective}:11440/api/version"
                    result = self.run_script("status.sh")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    status = json.loads(result.stdout)
                    self.assertEqual(status["claude_api"], f"http://{effective}:11440")
                    self.assertTrue(status["claude_compat_running"])
                    self.assertNotIn("dummy-status", result.stdout + result.stderr)

    def test_malformed_installed_plist_fails_client_and_status(self):
        self.installed.parent.mkdir(parents=True)
        self.installed.write_bytes(b"invalid current plist")
        for script in ("claude-code.sh", "status.sh"):
            with self.subTest(script=script):
                result = self.run_script(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("InvalidFileException", result.stderr)
                self.assertEqual(result.stdout, "")

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
                client = self.run_script("claude-code.sh")
                self.assertEqual(client.returncode, 0, client.stderr)
                self.assertEqual(json.loads(client.stdout)["base_url"], f"http://{probe_host}:11440")

    def test_install_summary_reports_effective_bind_and_client_endpoint(self):
        self.write_command("uname", '#!/bin/zsh\nif [[ "$1" == -s ]]; then echo Darwin; else echo arm64; fi\n')
        self.write_command("sysctl", "#!/bin/zsh\necho 34359738368\n")
        self.write_command("df", "#!/bin/zsh\nprintf 'Filesystem blocks used available capacity mounted\nfixture 100000000 0 100000000 0 /\n'\n")
        self.write_command("lsof", "#!/bin/zsh\nexit 1\n")
        self.write_command(
            "curl", f"#!{sys.executable}\nimport json, os, sys\n"
            'url = next(arg for arg in sys.argv[1:] if arg.startswith("http://"))\n'
            'expected = os.environ["TEST_COMPAT_URL"]\n'
            'if ":11440/" in url and url != expected: sys.exit(1)\n'
            'print(json.dumps({"choices": [{"message": {"content": "OK"}}], "version": "dummy-version"}))\n',
        )
        self.env.update({"QWEN38_SIM_FRESH": "1", "QWEN38_COMPAT_TOKEN": "dummy-summary"})
        Path(self.env["HOME"]).mkdir()
        for host, bind_host, client_host in (
            (None, "127.0.0.1", "127.0.0.1"), ("localhost", "localhost", "localhost"),
            ("192.0.2.10", "192.0.2.10", "192.0.2.10"),
            ("0.0.0.0", "0.0.0.0", "127.0.0.1"), ("", "0.0.0.0", "127.0.0.1"),
        ):
            with self.subTest(host=host):
                if host is None:
                    self.env.pop("QWEN38_COMPAT_HOST", None)
                else:
                    self.env["QWEN38_COMPAT_HOST"] = host
                self.env["TEST_COMPAT_URL"] = f"http://{client_host}:11440/api/version"
                result = subprocess.run(
                    [str(ROOT / "install.sh")], cwd=ROOT, env=self.env,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = result.stdout.split("==> 安装完成", 1)[1]
                self.assertIn(f"http://{client_host}:11440", summary)
                self.assertIn(f"监听 {bind_host}", summary)
                self.assertIn("监听 127.0.0.1", summary)
                self.assertNotIn("只监听 127.0.0.1，不暴露局域网", summary)
                self.assertNotIn("dummy-summary", summary)

    def test_padded_hosts_are_rejected_before_replacing_or_bootstrapping_compat(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-host"
        for symlink in (False, True):
            for host in (" 127.0.0.1 ", "\tlocalhost\n", " 192.0.2.10 ", " 0.0.0.0 ", "   "):
                with self.subTest(host=host, symlink=symlink):
                    if self.installed.exists() or self.installed.is_symlink():
                        self.installed.unlink()
                    if symlink:
                        self.installed.symlink_to(target)
                    else:
                        self.installed.write_bytes(b"previous plist")
                    self.capture.write_text("")
                    self.env["QWEN38_COMPAT_HOST"] = host
                    self.env["QWEN38_COMPAT_TOKEN"] = "" if host.strip() in ("127.0.0.1", "localhost") else "dummy-host"
                    result = self.run_script("start.sh")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("QWEN38_COMPAT_HOST", result.stderr)
                    self.assertIn("whitespace", result.stderr)
                    self.assertEqual(self.installed.is_symlink(), symlink)
                    self.assertEqual(self.installed.read_bytes(), b"previous plist")
                    self.assertEqual(target.read_bytes(), b"previous plist")
                    self.assertNotIn(COMPAT_LABEL, self.capture.read_text())
                    self.assertEqual(list(self.installed.parent.glob(".launchd-*")), [])

    def test_ipv6_is_rejected_before_replacing_or_bootstrapping_compat(self):
        target = self.temp / "original.plist"
        target.write_bytes(b"previous plist")
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(target)
        for host in ("::1", "[::1]"):
            with self.subTest(host=host):
                # This is the server class used by the proxy, with its AF_INET default.
                with self.assertRaises(OSError):
                    ThreadingHTTPServer((host, 0), BaseHTTPRequestHandler)
                self.capture.write_text("")
                self.env["QWEN38_COMPAT_HOST"] = host
                result = self.run_script("start.sh")
                self.assertEqual(result.returncode, 1)
                self.assertIn("IPv6", result.stderr)
                self.assertTrue(self.installed.is_symlink())
                self.assertEqual(target.read_bytes(), b"previous plist")
                self.assertNotIn(COMPAT_LABEL, self.capture.read_text())

    def test_padded_tokens_are_rejected_without_replacing_compat(self):
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-original"
        self.start()
        original = self.installed.read_bytes()
        for token in (" dummy-padded ", "\tdummy-padded\n", "   "):
            with self.subTest(token=token):
                self.capture.write_text("")
                self.env["QWEN38_COMPAT_TOKEN"] = token
                result = self.run_script("start.sh")
                self.assertEqual(result.returncode, 1)
                self.assertIn("whitespace", result.stderr)
                self.assertNotIn("dummy-padded", result.stderr)
                self.assertEqual(self.installed.read_bytes(), original)
                self.assertNotIn(COMPAT_LABEL, self.capture.read_text())

    def test_launcher_reaches_real_loopback_proxy_and_auth_stays_enforced(self):
        self.env.update({"QWEN38_COMPAT_HOST": "localhost", "QWEN38_COMPAT_TOKEN": "dummy-loopback"})
        self.start()
        self.env["TEST_COMPAT_PLIST"] = str(self.installed)
        del self.env["QWEN38_COMPAT_HOST"], self.env["QWEN38_COMPAT_TOKEN"]
        self.write_command("claude", f"#!{sys.executable}\n" + r'''import http.client
import json
import os
import plistlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

with open(os.environ["TEST_COMPAT_PLIST"], "rb") as handle:
    settings = plistlib.load(handle)
client_token = os.environ["ANTHROPIC_AUTH_TOKEN"]
endpoint = urlsplit(os.environ["ANTHROPIC_BASE_URL"])
assert endpoint.hostname == "localhost", "launcher ignored configured host"
os.environ.update(settings["EnvironmentVariables"])
sys.path.insert(0, settings["WorkingDirectory"])
import anthropic_proxy as proxy

class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"version":"dummy-version"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as upstream, \
     ThreadingHTTPServer((proxy.LISTEN_HOST, 0), proxy.CompatHandler) as compat:
    proxy.UPSTREAM_HOST = "127.0.0.1"
    proxy.UPSTREAM_PORT = upstream.server_port
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (upstream, compat)]
    for thread in threads:
        thread.start()
    try:
        statuses = []
        configured_token = proxy.COMPAT_TOKEN
        for expected, provided in ((configured_token, client_token), (configured_token, ""),
                                   (" " + configured_token + " ", client_token)):
            proxy.COMPAT_TOKEN = expected
            connection = http.client.HTTPConnection(endpoint.hostname, compat.server_port, timeout=3)
            connection.request("GET", "/api/version", headers={"x-api-key": provided})
            response = connection.getresponse()
            statuses.append(response.status)
            response.read()
            connection.close()
        print(json.dumps(statuses))
    finally:
        for server in (compat, upstream):
            server.shutdown()
        for thread in threads:
            thread.join()
''')
        client = self.run_script("claude-code.sh")
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(json.loads(client.stdout), [200, 401, 401])

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
        self.assertIn("HTTP header", result.stderr)
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
            'if len(sys.argv) > 1:\n'
            '    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n'
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

    def test_fresh_meter_reads_installed_normalized_path_and_empty_setting(self):
        self.env["QWEN38_METER_FILE"] = "meter.json"
        settings = self.start()
        meter_file = Path(settings["EnvironmentVariables"]["QWEN38_METER_FILE"])
        meter_file.write_text(json.dumps({"last_completed": {"output_tokens": 73}}))
        del self.env["QWEN38_METER_FILE"]
        snapshot = self.prepare_meter()
        result = self.run_script("meter.sh", cwd=self.temp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("73 tok", result.stdout)
        self.assertNotIn("9876.5", result.stdout)
        self.env["QWEN38_METER_FILE"] = str(snapshot)
        result = self.run_script("meter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("9876.5", result.stdout)
        self.env["QWEN38_METER_FILE"] = ""
        self.start()
        del self.env["QWEN38_METER_FILE"]
        result = self.run_script("meter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已关闭", result.stdout)
        self.assertNotIn("9876.5", result.stdout)
        self.env["QWEN38_METER_FILE"] = str(snapshot)
        result = self.run_script("meter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("9876.5", result.stdout)

    def test_relative_meter_path_matches_proxy_from_another_directory(self):
        self.env["QWEN38_METER_FILE"] = "meter.json"
        settings = self.start()
        working_dir = Path(settings["WorkingDirectory"])
        # The installed proxy's real meter writes from the launchd working directory.
        published = subprocess.run(
            [sys.executable, "-c", r'''from meter import TurnMeter
m = TurnMeter()
m.feed(b'data: {"type":"content_block_delta"}\n')
m.feed(b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n')
m.finish()
'''],
            cwd=working_dir, env={"HOME": self.env["HOME"], **settings["EnvironmentVariables"]},
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        self.prepare_meter()
        caller_dir = self.temp / "caller"
        caller_dir.mkdir()
        result = self.run_script("meter.sh", cwd=caller_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("7 tok", result.stdout)
        self.assertNotIn("暂无数据", result.stderr)
        self.assertEqual(settings["EnvironmentVariables"]["QWEN38_METER_FILE"], str(working_dir / "meter.json"))
        self.assertFalse((caller_dir / "meter.json").exists())

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
