import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kyiv_alert.config import Config
from tools.check_secrets import main, private_path, scan_content


class SecretChecksTests(unittest.TestCase):
    def test_detects_telegram_token_without_returning_value(self):
        token = "123456:" + "A" * 35
        result = scan_content(("example\n" + token).encode())
        self.assertEqual(result, [(2, "Telegram bot token")])
        self.assertNotIn(token, repr(result))

    def test_detects_private_keys_and_provider_tokens(self):
        examples = (
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "ghp_" + "B" * 36,
            "github_pat_" + "C" * 60,
            "AKIA" + "D" * 16,
        )
        for body in examples:
            with self.subTest(prefix=body[:4]):
                self.assertTrue(scan_content(body.encode()))

    def test_blocks_local_config_and_key_paths(self):
        for name in (".env", ".env.local", ".env.backup", "nested/.env.production",
                     "server.key", "server.pem", "key.p12", "key.pfx", "id_rsa.pub",
                     "secrets/token.txt", "credentials/service.json", ".aws/config",
                     "data/state.sqlite3", "cache.db", "cache.db-wal", "app.log"):
            with self.subTest(name=name):
                self.assertTrue(private_path(name))

    def test_allows_template_code_and_documentation(self):
        for name in (".env.example", "kyiv_alert/config.py", "docs/OPERATIONS.md",
                     "tools/check_secrets.py", "tests/test_security.py"):
            with self.subTest(name=name):
                self.assertFalse(private_path(name))

    def test_empty_template_and_env_references_are_allowed(self):
        body = b'TELEGRAM_BOT_TOKEN=\nTELEGRAM_BOT_TOKEN=""\nTELEGRAM_BOT_TOKEN=${TOKEN}\n'
        self.assertEqual(scan_content(body), [])

    def test_populated_env_setting_is_blocked_even_with_short_value(self):
        self.assertEqual(scan_content(b"TELEGRAM_BOT_TOKEN=some_value\n"),
                         [(1, "populated Telegram token setting")])

    def test_cli_reports_locations_but_does_not_echo_secret(self):
        token = "123456:" + "Z" * 35
        with patch("tools.check_secrets.files", return_value=iter([
                ("index", "config.txt", token.encode())])):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(["--staged"]), 1)
        self.assertIn("config.txt", output.getvalue())
        self.assertNotIn(token, output.getvalue())

    def test_cli_rejects_private_file_even_if_empty(self):
        with patch("tools.check_secrets.files", return_value=iter([
                ("index", ".env", b"")])):
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(["--staged"]), 1)

    def test_cli_fails_closed_when_git_is_unavailable(self):
        with patch("tools.check_secrets.files", side_effect=OSError("details")):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main([]), 2)
        self.assertNotIn("details", output.getvalue())

    def test_filenames_cannot_inject_workflow_commands(self):
        token = "123456:" + "X" * 35
        with patch("tools.check_secrets.files", return_value=iter([
                ("index", "file\n::warning::injected", token.encode())])):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main([]), 1)
        self.assertNotIn("\n::warning::", output.getvalue())


class ConfigRedactionTests(unittest.TestCase):
    def test_invalid_numeric_settings_do_not_echo_input(self):
        secret = "123456:" + "V" * 35
        for name in ("POLL_INTERVAL_SECONDS", "HTTP_TIMEOUT_SECONDS",
                     "NOTIFICATION_MAX_AGE_SECONDS", "HEALTH_MAX_AGE_SECONDS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: secret}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    Config.from_env()
                self.assertNotIn(secret, str(caught.exception))

    def test_custom_source_url_is_not_in_config_repr(self):
        url = "https://example.invalid/alerts?token=private-value"
        with patch.dict(os.environ, {"ALERTS_URL": url}, clear=True):
            self.assertNotIn("private-value", repr(Config.from_env()))


@unittest.skipUnless(shutil.which("git"), "Git is required for index/history integration tests")
class GitSecretChecksTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=", "-c",
                                        "commit.gpgsign=false", "-c", "user.name=Test",
                                        "-c", "user.email=test@example.invalid", *args],
                                       cwd=root, stderr=subprocess.PIPE)

    def test_staged_secret_cannot_be_hidden_by_clean_working_copy(self):
        token = "123456:" + "Q" * 35
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.git(root, "init")
            path = root / "settings.txt"
            path.write_text(token, encoding="utf-8")
            self.git(root, "add", "settings.txt")
            path.write_text("clean working copy", encoding="utf-8")
            with patch("tools.check_secrets.ROOT", root):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main([]), 0)
                    self.assertEqual(main(["--staged"]), 1)
            self.assertNotIn(token, output.getvalue())

    def test_history_detects_secret_removed_from_current_files(self):
        token = "123456:" + "R" * 35
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.git(root, "init")
            path = root / "settings.txt"
            path.write_text(token, encoding="utf-8")
            self.git(root, "add", "settings.txt")
            self.git(root, "commit", "-m", "Synthetic secret fixture")
            path.write_text("clean current file", encoding="utf-8")
            self.git(root, "add", "settings.txt")
            self.git(root, "commit", "-m", "Clean fixture")
            with patch("tools.check_secrets.ROOT", root):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main([]), 0)
                    self.assertEqual(main(["--history"]), 1)
            self.assertNotIn(token, output.getvalue())


if __name__ == "__main__":
    unittest.main()
