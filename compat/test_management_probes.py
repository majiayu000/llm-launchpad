import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import anthropic_proxy


ROOT = Path(__file__).resolve().parents[1]


class FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        payload = {"version": "test-version"} if self.path == "/api/version" else {"models": []}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ManagementProbeTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp = Path(temp_dir.name)
        self.bin_dir = self.temp / "bin"
        self.bin_dir.mkdir()
        self.ollama = self.serve(FakeOllamaHandler)
        self.ollama.requests = []
        settings = patch.multiple(
            anthropic_proxy,
            UPSTREAM_HOST="127.0.0.1",
            UPSTREAM_PORT=self.ollama.server_port,
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.compat = self.serve(anthropic_proxy.CompatHandler)
        self.write_command(
            "curl",
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "args = [arg.replace('http://127.0.0.1:11439', os.environ['TEST_OLLAMA_URL'])"
            ".replace('http://127.0.0.1:11440', os.environ['TEST_COMPAT_URL']) for arg in sys.argv[1:]]\n"
            "os.execv(os.environ['TEST_CURL'], [os.environ['TEST_CURL'], *args])\n",
        )
        self.write_command("launchctl", "#!/bin/zsh\nexit 0\n")
        self.write_command("lsof", "#!/bin/zsh\nexit 0\n")
        self.write_command("sleep", "#!/bin/zsh\nexit 0\n")
        self.write_command("ollama", "#!/bin/zsh\nexit 0\n")
        self.write_command("uname", '#!/bin/zsh\n[[ "$1" == -s ]] && echo Darwin || echo arm64\n')
        self.write_command("sysctl", "#!/bin/zsh\necho 34359738368\n")
        self.write_command("df", "#!/bin/zsh\nprintf 'Filesystem Blocks Used Available\ntest 0 0 104857600\n'\n")
        self.env = os.environ.copy()
        self.env.pop("QWEN38_COMPAT_TOKEN", None)
        self.env.update({
            "HOME": str(self.temp / "home"),
            "PATH": f"{self.bin_dir}:{self.env['PATH']}",
            "QWEN38_OLLAMA_BIN": str(self.bin_dir / "ollama"),
            "QWEN38_PYTHON_BIN": sys.executable,
            "QWEN38_SIM_MEM_GB": "32",
            "QWEN38_SIM_DISK_GB": "100",
            "QWEN38_SIM_FRESH": "1",
            "TEST_CURL": shutil.which("curl"),
            "TEST_OLLAMA_URL": f"http://127.0.0.1:{self.ollama.server_port}",
            "TEST_COMPAT_URL": f"http://127.0.0.1:{self.compat.server_port}",
        })
        Path(self.env["HOME"]).mkdir()

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def write_command(self, name, content):
        command = self.bin_dir / name
        command.write_text(content)
        command.chmod(0o755)

    def run_script(self, script, *args):
        return subprocess.run(
            [str(ROOT / script), *args], cwd=ROOT, env=self.env,
            capture_output=True, text=True, timeout=15,
        )

    def test_start_observes_authenticated_compat(self):
        for token in (None, "", "dummy-test-token"):
            with self.subTest(token=token), patch.object(anthropic_proxy, "COMPAT_TOKEN", "ollama" if token is None else token):
                if token is None:
                    self.env.pop("QWEN38_COMPAT_TOKEN", None)
                else:
                    self.env["QWEN38_COMPAT_TOKEN"] = token
                result = self.run_script("scripts/start.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("独立服务已启动", result.stdout)

    def test_status_reports_authenticated_compat_running(self):
        for token in (None, "", "dummy-test-token"):
            with self.subTest(token=token), patch.object(anthropic_proxy, "COMPAT_TOKEN", "ollama" if token is None else token):
                if token is None:
                    self.env.pop("QWEN38_COMPAT_TOKEN", None)
                else:
                    self.env["QWEN38_COMPAT_TOKEN"] = token
                result = self.run_script("scripts/status.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["claude_compat_running"])

    def test_install_recognizes_authenticated_compat_listener(self):
        for token in (None, "", "dummy-test-token"):
            with self.subTest(token=token), patch.object(anthropic_proxy, "COMPAT_TOKEN", "ollama" if token is None else token):
                if token is None:
                    self.env.pop("QWEN38_COMPAT_TOKEN", None)
                else:
                    self.env["QWEN38_COMPAT_TOKEN"] = token
                result = self.run_script("install.sh", "--check")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("预检通过", result.stdout)
                self.assertTrue(all("x-api-key" not in headers for _, headers in self.ollama.requests))

    def test_mismatched_token_preserves_failure_results(self):
        self.env["QWEN38_COMPAT_TOKEN"] = "dummy-wrong-token"
        with patch.object(anthropic_proxy, "COMPAT_TOKEN", "dummy-test-token"):
            start = self.run_script("scripts/start.sh")
            self.assertEqual(start.returncode, 1)
            self.assertIn("Claude 兼容入口启动超时", start.stderr)
            status = self.run_script("scripts/status.sh")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertFalse(json.loads(status.stdout)["claude_compat_running"])
            install = self.run_script("install.sh", "--check")
            self.assertEqual(install.returncode, 1)
            self.assertIn("端口 11440 已被其他程序占用", install.stderr)

    def test_version_endpoint_still_rejects_unauthenticated_requests(self):
        with patch.object(anthropic_proxy, "COMPAT_TOKEN", "dummy-test-token"):
            result = subprocess.run(
                [self.env["TEST_CURL"], "-fsS", f"{self.env['TEST_COMPAT_URL']}/api/version"],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 22)
            self.assertIn("401", result.stderr)
            self.assertEqual(self.ollama.requests, [])


if __name__ == "__main__":
    unittest.main()
