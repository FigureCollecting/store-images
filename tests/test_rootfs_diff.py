"""Tests for scripts/rootfs_diff.py: which paths differ between two container file-system exports?"""
import io
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import rootfs_diff  # noqa: E402


def member(name, data=b"", mode=0o644, uid=0, gid=0, kind=tarfile.REGTYPE, link="", mtime=0):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.uid, info.gid, info.type, info.linkname, info.mtime = len(data), mode, uid, gid, kind, link, mtime
    return info, data


class RootfsDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tar(self, name, *members):
        path = os.path.join(self.tmp.name, name)
        with tarfile.open(path, "w") as tar:
            for info, data in members:
                tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
        return path

    def run_diff(self, old, new):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rootfs_diff.main([old, new])
        return rc, out.getvalue()

    def test_identical_trees_print_nothing_whatever_the_times_and_the_name_prefix(self):
        old = self.tar("a.tar", member("bin/", kind=tarfile.DIRTYPE, mode=0o755), member("bin/x", b"one", mtime=1))
        new = self.tar("b.tar", member("./bin/", kind=tarfile.DIRTYPE, mode=0o755), member("./bin/x", b"one", mtime=9))
        self.assertEqual(self.run_diff(old, new), (0, ""))

    def test_content_mode_owner_kind_and_link_target_each_count_as_a_change(self):
        base = [member("a", b"1"), member("b", b"1"), member("c", b"1"), member("d", b"1"),
                member("e", kind=tarfile.SYMTYPE, link="a"), member("f", b"same")]
        other = [member("a", b"2"), member("b", b"1", mode=0o4755), member("c", b"1", uid=7), member("d", b"1", gid=7),
                 member("e", kind=tarfile.SYMTYPE, link="b"), member("f", b"same")]
        rc, out = self.run_diff(self.tar("a.tar", *base), self.tar("b.tar", *other))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "changed a\nchanged b\nchanged c\nchanged d\nchanged e\n")

    def test_a_file_that_becomes_a_link_is_a_change(self):
        old = self.tar("a.tar", member("lib/z", b"1"))
        new = self.tar("b.tar", member("lib/z", kind=tarfile.SYMTYPE, link="z.1"))
        self.assertEqual(self.run_diff(old, new), (0, "changed lib/z\n"))

    def test_added_and_removed_paths_are_named_in_path_order(self):
        old = self.tar("a.tar", member("lib/libz.so.1.3.2", b"old"), member("keep", b"k"))
        new = self.tar("b.tar", member("keep", b"k"), member("lib/libz.so.1.4", b"new"), member("a-new", b""))
        self.assertEqual(self.run_diff(old, new),
                         (0, "added a-new\nremoved lib/libz.so.1.3.2\nadded lib/libz.so.1.4\n"))

    def test_a_hard_link_is_compared_by_its_target(self):
        old = self.tar("a.tar", member("x", b"1"), member("y", kind=tarfile.LNKTYPE, link="x"))
        new = self.tar("b.tar", member("x", b"1"), member("y", kind=tarfile.LNKTYPE, link="./x"))
        self.assertEqual(self.run_diff(old, new), (0, ""))

    def test_an_unreadable_archive_is_an_error_not_an_empty_diff(self):
        bad = os.path.join(self.tmp.name, "bad.tar")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("not a tar")
        good = self.tar("a.tar", member("x", b"1"))
        for pair in ((bad, good), (good, bad), (good, os.path.join(self.tmp.name, "missing.tar"))):
            with self.subTest(pair=pair):
                rc, out = self.run_diff(*pair)
                self.assertEqual(rc, 2)
                self.assertIn("ROOTFS DIFF ERROR", out)

    def test_an_empty_archive_is_an_error(self):
        empty = self.tar("e.tar")
        good = self.tar("a.tar", member("x", b"1"))
        rc, out = self.run_diff(good, empty)
        self.assertEqual(rc, 2)
        self.assertIn("ROOTFS DIFF ERROR", out)
        self.assertIn("holds no files", out)

    def test_usage_needs_two_archives(self):
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
            with open(os.devnull, "w", encoding="utf-8") as devnull:
                stderr, sys.stderr = sys.stderr, devnull
                try:
                    rootfs_diff.main(["only-one"])
                finally:
                    sys.stderr = stderr
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
