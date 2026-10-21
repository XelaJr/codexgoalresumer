import unittest

import helpers  # noqa: F401
import os
from unittest import mock

from codex_goals.paths import is_within, norm, pretty


class NormTest(unittest.TestCase):
    def test_windows_drive_case_and_slashes(self):
        self.assertEqual(norm("C:\\x", "linux"), "c:/x")
        self.assertEqual(norm("c:\\X", "linux"), "c:/x")
        self.assertEqual(norm("C:/Users/Ana/Proj/", "linux"), "c:/users/ana/proj")

    def test_wsl_mount_equals_drive(self):
        self.assertEqual(norm("/mnt/c/x", "linux"), norm("C:\\x", "linux"))
        self.assertEqual(norm("/mnt/c/Users/Ana", "linux"), "c:/users/ana")
        self.assertEqual(norm("/mnt/c", "linux"), "c:/")

    def test_extended_length_prefix(self):
        self.assertEqual(norm("\\\\?\\C:\\Users\\ana\\proj", "linux"), "c:/users/ana/proj")
        self.assertEqual(norm("\\\\?\\UNC\\server\\share\\x", "linux"), "//server/share/x")

    def test_wsl_unc_paths(self):
        self.assertEqual(norm("\\\\wsl.localhost\\Ubuntu\\home\\ana\\proj", "linux"), "/home/ana/proj")
        self.assertEqual(norm("\\\\wsl$\\Ubuntu\\home\\ana", "linux"), "/home/ana")

    def test_trailing_and_repeated_slashes(self):
        self.assertEqual(norm("/home/ana//proj///", "linux"), "/home/ana/proj")
        self.assertEqual(norm("/", "linux"), "/")
        self.assertEqual(norm("C:\\", "linux"), "c:/")

    def test_macos_private(self):
        self.assertEqual(norm("/private/var/folders/x", "darwin"), "/var/folders/x")
        self.assertEqual(norm("/private/tmp/x", "darwin"), "/tmp/x")
        self.assertEqual(norm("/private/other", "darwin"), "/private/other")

    def test_case_sensitivity_by_platform(self):
        self.assertNotEqual(norm("/home/Ana", "linux"), norm("/home/ana", "linux"))
        self.assertEqual(norm("/Users/Ana", "darwin"), norm("/users/ana", "darwin"))

    def test_empty(self):
        self.assertEqual(norm(None), "")
        self.assertEqual(norm(""), "")


class WithinTest(unittest.TestCase):
    def test_same_and_child(self):
        self.assertTrue(is_within("/a/b", "/a/b", "linux"))
        self.assertTrue(is_within("/a/b/c", "/a/b/", "linux"))
        self.assertTrue(is_within("C:\\Users\\Ana\\proj\\sub", "/mnt/c/users/ana/proj", "linux"))

    def test_sibling_prefix_is_not_child(self):
        self.assertFalse(is_within("/a/bc", "/a/b", "linux"))
        self.assertFalse(is_within("/a", "/a/b", "linux"))

    def test_root(self):
        self.assertTrue(is_within("/a", "/", "linux"))
        self.assertTrue(is_within("C:\\a", "c:\\", "linux"))

    def test_empty(self):
        self.assertFalse(is_within("", "/a"))
        self.assertFalse(is_within("/a", None))


class PrettyTest(unittest.TestCase):
    def test_home_shortened_case_kept(self):
        with mock.patch.object(os.path, "expanduser", return_value="/home/Ana"):
            self.assertEqual(pretty("/home/Ana/Proj/X"), "~/Proj/X")
            self.assertEqual(pretty("/home/Ana"), "~")
            self.assertEqual(pretty("/srv/Other"), "/srv/Other")

    def test_extended_prefix_removed(self):
        with mock.patch.object(os.path, "expanduser", return_value="/home/ana"):
            self.assertEqual(pretty("\\\\?\\D:\\a\\_temp\\proj"), "D:\\a\\_temp\\proj")


if __name__ == "__main__":
    unittest.main()
