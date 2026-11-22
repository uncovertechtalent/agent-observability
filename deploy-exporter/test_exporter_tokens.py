"""Which token deploy-exporter sends: a file per owner wins, then the owner's variable, then GITHUB_TOKEN.

    cd deploy-exporter && python3 -m unittest test_exporter_tokens
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import exporter


class TokenFor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dir = os.path.join(self.root, "tokens")
        os.mkdir(self.dir)
        exporter._UNREADABLE_WARNED.clear()

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, owner, value):
        with open(os.path.join(self.dir, owner), "w") as f:
            f.write(value + "\n")

    def test_file_wins_over_variables(self):
        self.write("uncovertechtalent-git", "from-file")
        env = {"GITHUB_TOKEN_UNCOVERTECHTALENT_GIT": "from-var"}
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir), mock.patch.object(exporter, "TOKEN", "default"), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(exporter.token_for("repos/uncovertechtalent-git/site/actions/runs"), "from-file")

    def test_owner_name_is_matched_in_lower_case(self):
        self.write("uncovertechtalent-git", "from-file")
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir):
            self.assertEqual(exporter.token_for("/repos/Uncovertechtalent-Git/site/actions/runs"), "from-file")

    def test_renewed_file_is_read_on_the_next_request(self):
        self.write("uncovertechtalent", "first")
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir):
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "first")
            self.write("uncovertechtalent", "second")
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "second")

    def test_missing_file_falls_back_to_variables(self):
        env = {"GITHUB_TOKEN_UNCOVERTECHTALENT_GIT": "from-var"}
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir), mock.patch.object(exporter, "TOKEN", "default"), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(exporter.token_for("repos/uncovertechtalent-git/site/actions/runs"), "from-var")
            self.assertEqual(exporter.token_for("repos/someone-else/site/actions/runs"), "default")

    def test_empty_file_falls_back(self):
        self.write("uncovertechtalent", "")
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir), mock.patch.object(exporter, "TOKEN", "default"):
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "default")

    def test_no_dir_keeps_the_old_behaviour(self):
        with mock.patch.object(exporter, "TOKEN_DIR", ""), mock.patch.object(exporter, "TOKEN", "default"):
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "default")
            self.assertEqual(exporter.token_for("rate_limit"), "default")

    def test_owner_cannot_escape_the_dir(self):
        with open(os.path.join(self.root, "outside"), "w") as f:
            f.write("outside")
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir), mock.patch.object(exporter, "TOKEN", "default"):
            self.assertEqual(exporter.token_for("repos/../outside/actions/runs"), "default")

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_unreadable_file_falls_back_and_warns_once(self):
        self.write("uncovertechtalent", "secret")
        os.chmod(os.path.join(self.dir, "uncovertechtalent"), 0)
        with mock.patch.object(exporter, "TOKEN_DIR", self.dir), mock.patch.object(exporter, "TOKEN", "default"), \
                mock.patch.object(exporter, "log") as log:
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "default")
            self.assertEqual(exporter.token_for("repos/uncovertechtalent/x/actions/runs"), "default")
        self.assertEqual(log.call_count, 1)
        self.assertIn("cannot be read", log.call_args[0][0])
        self.assertNotIn("secret", log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
