import unittest

import helpers  # noqa: F401
from codex_goals.redact import MASK, redact, redact_obj

POSITIVE = [
    "key sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4 here",
    "sk-ant-api03-abcdefghijklmnopqrstuv",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "xoxb-1234567890-abcdefghij",
    "Authorization: Bearer abc.def.ghi-1234567890",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "postgres://admin:hunter22@db.example.com/app",
    '{"access_token": "abcd1234efgh", "refresh_token": "zzzz9999"}',
    "OPENAI_API_KEY=abcd1234",
    "export AWS_SECRET_ACCESS_KEY='wJalrXUtnFEMI/K7MDENG'",
    "DB_PASSWORD: s3cr3t!",
    "password=hunter2x",
    "mysql --password hunter2x",
    "contraseña Foo123$",
    "la contrasena es Foo123$",
    "password is Hunter#2",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
]

SECRETS = ["A1b2C3d4E5f6", "abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuvwxyz0123456789", "IOSFODNN7",
           "1234567890abcdefghijklmnopqrstuv", "abcdefghij", "1234567890", "dozjgNryP4", "hunter22",
           "abcd1234efgh", "zzzz9999", "abcd1234", "wJalrXUtnFEMI", "s3cr3t", "hunter2x", "hunter2x",
           "Foo123", "Foo123", "Hunter#2", "MIIEow"]

NEGATIVE = [
    "the password reset flow is broken",
    "contraseña olvidada",
    "MAX_TOKENS=8000",
    "--max-tokens 8000",
    "token usage was 12k",
    "sk-short",
    "Bearer of bad news",
    "https://example.com/path@v2",
    "PermissionError: not allowed",
    "git commit -m 'fix password field'",
    "pass the test",
]


class RedactTest(unittest.TestCase):
    def test_positive(self):
        for text, secret in zip(POSITIVE, SECRETS):
            with self.subTest(text=text):
                out = redact(text)
                self.assertIn(MASK, out)
                self.assertNotIn(secret, out)

    def test_negative(self):
        for text in NEGATIVE:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_keeps_variable_names(self):
        self.assertEqual(redact("GITHUB_TOKEN=abcd1234"), "GITHUB_TOKEN=" + MASK)
        self.assertEqual(redact("https://u:pa55word@x.com"), "https://u:%s@x.com" % MASK)

    def test_secret_is_masked_everywhere(self):
        data = {"a": "server pass: contraseña CanadasGB10$", "b": ["ssh with CanadasGB10 now"]}
        out = redact_obj(data)
        self.assertNotIn("CanadasGB10", str(out))

    def test_plain_words_are_not_propagated(self):
        data = {"a": "password=Permission", "b": "PermissionError raised"}
        out = redact_obj(data)
        self.assertEqual(out["b"], "PermissionError raised")
        self.assertNotIn("Permission", out["a"])

    def test_non_strings_pass_through(self):
        self.assertEqual(redact_obj({"n": 3, "l": [None, True]}), {"n": 3, "l": [None, True]})


if __name__ == "__main__":
    unittest.main()
