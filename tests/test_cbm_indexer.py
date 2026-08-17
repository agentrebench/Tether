import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import subprocess
import unittest
from pathlib import Path

from tether.core.codebase_model.indexer import Indexer
from tether.core.codebase_model.store import ModelStore
from tether.core.codebase_model.model import node_id


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class IndexerTestBase(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.dbdir = Path(tempfile.mkdtemp())
        self.store = ModelStore(self.dbdir / "model.db")
        self.idx = Indexer(self.store, self.repo)

    def tearDown(self):
        self.store.close()

    def _build_chain(self):
        # base <- mid <- top, across three files (call graph for blast radius)
        _write(self.repo, "a.py", "def base():\n    return 1\n")
        _write(self.repo, "b.py", "def mid():\n    return base()\n")
        _write(self.repo, "c.py", "def top():\n    return mid()\n")
        return self.idx.cold_build()


class TestDiscovery(IndexerTestBase):
    def test_discovers_py_skips_junk(self):
        _write(self.repo, "pkg/mod.py", "x = 1\n")
        _write(self.repo, "top.py", "y = 2\n")
        _write(self.repo, "__pycache__/cached.py", "z = 3\n")
        _write(self.repo, ".venv/lib/dep.py", "w = 4\n")
        _write(self.repo, "thing.egg-info/meta.py", "v = 5\n")
        _write(self.repo, "notes.txt", "not python\n")
        files = self.idx.discover_python_files()
        self.assertEqual(files, ["pkg/mod.py", "top.py"])

    def test_paths_are_relative_posix(self):
        _write(self.repo, "deep/nested/x.py", "a = 1\n")
        self.assertIn("deep/nested/x.py", self.idx.discover_python_files())


class TestIndexFile(IndexerTestBase):
    def test_index_then_reindex_unchanged_is_noop(self):
        _write(self.repo, "a.py", "def f():\n    return 1\n")
        self.assertTrue(self.idx.index_file("a.py"))
        # second pass: content hash matches -> skipped
        self.assertFalse(self.idx.index_file("a.py"))
        self.assertEqual(self.store.count_nodes(), 2)  # module + function

    def test_reindex_changed_replaces_nodes(self):
        _write(self.repo, "a.py", "def f():\n    return 1\n")
        self.idx.index_file("a.py")
        _write(self.repo, "a.py", "def g():\n    return 2\n")
        self.assertTrue(self.idx.index_file("a.py"))
        names = {n.name for n in self.store.nodes_in_file("a.py")}
        self.assertIn("g", names)
        self.assertNotIn("f", names)  # old node deleted

    def test_index_file_explicit_content(self):
        # no file on disk; pass content directly
        self.assertTrue(self.idx.index_file("v.py", content="def h():\n    pass\n"))
        self.assertIsNotNone(self.store.get_node(node_id("v.py", "h")))

    def test_index_missing_file_returns_false(self):
        self.assertFalse(self.idx.index_file("does_not_exist.py"))

    def test_remove_file(self):
        _write(self.repo, "a.py", "def f():\n    pass\n")
        self.idx.index_file("a.py")
        self.assertTrue(self.store.nodes_in_file("a.py"))
        self.idx.remove_file("a.py")
        self.assertEqual(self.store.nodes_in_file("a.py"), [])
        self.assertIsNone(self.store.get_file_hash("a.py"))


class TestColdBuildAndUpdate(IndexerTestBase):
    def test_cold_build_counts(self):
        res = self._build_chain()
        self.assertEqual(res, {"indexed": 3, "files": 3, "skipped": 0})
        # rebuilding over an up-to-date store reindexes nothing
        res2 = self.idx.cold_build()
        self.assertEqual(res2["indexed"], 0)
        self.assertEqual(res2["files"], 3)

    def test_update_is_incremental(self):
        self._build_chain()
        # change only b.py
        _write(self.repo, "b.py", "def mid():\n    return base() + 1\n")
        res = self.idx.update(["a.py", "b.py", "c.py"])
        # only b.py actually changed
        self.assertEqual(res["indexed"], 1)
        self.assertEqual(res["removed"], 0)

    def test_update_removes(self):
        self._build_chain()
        _write(self.repo, "d.py", "def gone():\n    pass\n")
        self.idx.index_file("d.py")
        res = self.idx.update([], removed=["d.py"])
        self.assertEqual(res, {"indexed": 0, "removed": 1})
        self.assertEqual(self.store.nodes_in_file("d.py"), [])

    def test_cold_build_explicit_paths(self):
        _write(self.repo, "a.py", "def f():\n    pass\n")
        _write(self.repo, "b.py", "def g():\n    pass\n")
        res = self.idx.cold_build(paths=["a.py"])
        self.assertEqual(res, {"indexed": 1, "files": 1, "skipped": 0})
        self.assertEqual(self.store.nodes_in_file("b.py"), [])


class TestChangedSinceIndex(IndexerTestBase):
    def test_detects_modified_and_new_and_removed(self):
        self._build_chain()
        # modify a.py on disk
        _write(self.repo, "a.py", "def base():\n    return 99\n")
        # add a brand-new file
        _write(self.repo, "e.py", "def fresh():\n    pass\n")
        # delete c.py from disk (still in store)
        (self.repo / "c.py").unlink()
        changed, removed = self.idx.changed_since_index()
        self.assertIn("a.py", changed)   # modified
        self.assertIn("e.py", changed)   # new
        self.assertNotIn("b.py", changed)  # untouched
        self.assertEqual(removed, ["c.py"])

    def test_clean_tree_has_no_changes(self):
        self._build_chain()
        changed, removed = self.idx.changed_since_index()
        self.assertEqual(changed, [])
        self.assertEqual(removed, [])


class TestBlastRadius(IndexerTestBase):
    def test_transitive_callers_by_name(self):
        self._build_chain()
        radius = set(self.idx.blast_radius("base"))
        self.assertEqual(radius, {node_id("b.py", "mid"), node_id("c.py", "top")})

    def test_blast_radius_excludes_target(self):
        self._build_chain()
        self.assertNotIn(node_id("a.py", "base"), self.idx.blast_radius("base"))

    def test_blast_radius_by_node_id(self):
        self._build_chain()
        radius = set(self.idx.blast_radius(node_id("a.py", "base")))
        self.assertEqual(radius, {node_id("b.py", "mid"), node_id("c.py", "top")})

    def test_depth_cap(self):
        self._build_chain()
        # depth 1 only reaches direct callers of base (mid), not top
        radius = self.idx.blast_radius("base", max_depth=1)
        self.assertEqual(radius, [node_id("b.py", "mid")])

    def test_leaf_caller_has_empty_radius(self):
        self._build_chain()
        self.assertEqual(self.idx.blast_radius("top"), [])

    def test_method_simple_name_binding(self):
        # call to a method by simple name still binds best-effort
        _write(self.repo, "svc.py",
               "class Svc:\n    def run(self):\n        return 1\n")
        _write(self.repo, "use.py",
               "def caller(s):\n    return s.run()\n")
        self.idx.cold_build()
        radius = set(self.idx.blast_radius("Svc.run"))
        self.assertIn(node_id("use.py", "caller"), radius)

    def test_unknown_target_empty(self):
        self._build_chain()
        self.assertEqual(self.idx.blast_radius("nonexistent_symbol"), [])


class TestAffectedFiles(IndexerTestBase):
    def test_affected_files_union(self):
        self._build_chain()
        affected = self.idx.affected_files("a.py")
        self.assertEqual(affected, ["b.py", "c.py"])

    def test_affected_files_leaf(self):
        self._build_chain()
        self.assertEqual(self.idx.affected_files("c.py"), [])


class TestCurrentCommit(IndexerTestBase):
    def test_no_git_returns_empty(self):
        # mkdtemp dir is not a git repo
        self.assertEqual(self.idx.current_commit(), "")

    def test_with_git_returns_short_hash(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("git not available")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a],
                                        capture_output=True, env=env)
        run("init")
        _write(self.repo, "a.py", "x = 1\n")
        run("add", "-A")
        run("commit", "-m", "init")
        commit = self.idx.current_commit()
        self.assertTrue(commit)
        self.assertLessEqual(len(commit), 12)


if __name__ == "__main__":
    unittest.main()
