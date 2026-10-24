import contextlib
import io
import os
from unittest.mock import patch

from boss.cli import main
from boss.common import Redactor
from boss.config import Config
from tests.support import HermeticTestCase


class SecretDiscoveryTest(HermeticTestCase):
    def config(self):
        redact = Redactor()
        Config(self.home, redact)
        return redact

    def test_env_flags_and_short_passwords_do_not_redact_common_output(self):
        token = "Ab9_" * 10
        with patch.dict(os.environ, {"HERMES_REDACT_SECRETS": "true", "SOME_PASSWORD": "word",
                                     "SUDO_PASSWORD": "abc", "REAL_API_KEY": token}):
            redact = self.config()
            self.assertEqual(redact("true false word abc"), "true false word abc")
            self.assertEqual(redact(token), "[REDACTED]")
            output = io.StringIO()
            with patch.dict(os.environ, {"BOSS_HOME": str(self.home)}), contextlib.redirect_stdout(output):
                code = main(["decide", "add", "true false word abc " + token])
            self.assertEqual(code, 0, output.getvalue())
            record = next((self.home / "decisions").glob("*.json")).read_text()
            self.assertIn("true false word abc [REDACTED]", record)
            self.assertNotIn(token, record)

    def test_dotenv_flags_short_values_and_literals_are_not_secrets(self):
        directory = self.user_home / ".hermes"
        directory.mkdir()
        token = "real-private-token_" * 3
        (directory / ".env").write_text("HERMES_REDACT_SECRETS=true\nSOME_PASSWORD=word\n"
                                       "PASSWORD_ENABLED=configuration-value\nAPI_SECRETS=another-config-value\n"
                                       "TOKEN_COUNT=1234567890123456\nREAL_API_KEY=" + token + "\n")
        redact = self.config()
        text = "true word configuration-value another-config-value 1234567890123456"
        self.assertEqual(redact(text), text)
        self.assertEqual(redact(token), "[REDACTED]")

    def test_long_boolean_and_number_literals_and_config_flags_are_skipped(self):
        values = {
            "FEATURE_TOKEN": "  TrUe        ",
            "SOME_PASSWORD": "12345678901234567890",
            "API_KEY_TOTAL": "-1234567890.125",
            "TOKEN_ENABLED": "configuration-value",
            "TOKEN_SECRETS": "configuration-value-two",
            "HERMES_REDACT_API_KEY": "configuration-value-three",
        }
        with patch.dict(os.environ, values):
            redact = self.config()
        for value in values.values():
            self.assertEqual(redact(value), value)

    def test_real_environment_token_is_redacted_even_in_usage_errors(self):
        token = "Aa9_" * 10
        with patch.dict(os.environ, {"REAL_API_KEY": token}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--invented=" + token])
        self.assertEqual(code, 1)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_explicit_host_tokens_are_registered_regardless_of_length_or_key(self):
        (self.home / "config.toml").write_text('[hosts.custom]\nurl="http://127.0.0.1:3773/mcp"\n'
                                              'token_env="HERMES_REDACT_SECRETS"\n')
        with patch.dict(os.environ, {"HERMES_REDACT_SECRETS": "true"}):
            self.assertEqual(self.config()("true"), "[REDACTED]")

    def test_overridden_explicit_dotenv_host_token_stays_protected(self):
        directory = self.user_home / ".hermes"
        directory.mkdir()
        (directory / ".env").write_text("T3_ORACLE_MCP_TOKEN=tiny\n")
        with patch.dict(os.environ, {"T3_ORACLE_MCP_TOKEN": "replacement-host-token-value"}):
            redact = self.config()
        self.assertEqual(redact("tiny replacement-host-token-value"), "[REDACTED] [REDACTED]")

    def test_test_fixture_has_an_empty_home_and_no_inherited_credentials(self):
        self.assertEqual(os.environ["HOME"], str(self.user_home))
        self.assertFalse((self.user_home / ".hermes/.env").exists())
        self.assertEqual(set(os.environ), {"PATH", "HOME", "LC_ALL", "GIT_CONFIG_NOSYSTEM"})
