"""Multi-language substrate, file cap, symbol-granular invalidation, DB GC."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model import substrate_generic
from tether.core.codebase_model.citations import Citation, format_citation, refetch
from tether.core.codebase_model.model import NodeKind
from tether.core.codebase_model.service import CodebaseModel, gc_models
from tether.core.codebase_model.store import ModelStore


GO_SRC = '''package billing

import (
\t"fmt"
\t"strings"
)

type RefundService struct {
\tstore Store
}

func Refund(id string) error {
\tfmt.Println(id)
\treturn nil
}

func helper(s string) string {
\treturn strings.ToUpper(s)
}
'''

TS_SRC = '''import { z } from "zod";
import util from "./util";

export interface Invoice {
  id: string;
}

export class BillingClient {
  send() {}
}

export const charge = async (amount: number) => {
  return amount * 100;
};

function internalHelper() {}
'''

RUST_SRC = '''use std::collections::HashMap;

pub struct Ledger {
    entries: HashMap<String, u64>,
}

pub fn post_entry(l: &mut Ledger, k: &str) {
    l.entries.insert(k.to_string(), 1);
}
'''


class GenericExtraction(unittest.TestCase):
    def _names(self, nodes, kind=None):
        return {n.name for n in nodes if kind is None or n.kind == kind}

    def test_go(self):
        nodes, edges = substrate_generic.extract("billing/refund.go", GO_SRC, "go")
        self.assertIn("Refund", self._names(nodes, NodeKind.FUNCTION))
        self.assertIn("helper", self._names(nodes, NodeKind.FUNCTION))
        self.assertIn("RefundService", self._names(nodes, NodeKind.CLASS))
        imports = {e.dst for e in edges if e.kind == "imports"}
        self.assertEqual({"fmt", "strings"}, imports)  # block imports parsed

    def test_typescript(self):
        nodes, edges = substrate_generic.extract("src/billing.ts", TS_SRC, "typescript")
        names = self._names(nodes)
        self.assertIn("BillingClient", names)
        self.assertIn("Invoice", names)
        self.assertIn("charge", names)       # arrow function
        self.assertIn("internalHelper", names)
        imports = {e.dst for e in edges if e.kind == "imports"}
        self.assertEqual({"zod", "./util"}, imports)

    def test_rust(self):
        nodes, _ = substrate_generic.extract("src/ledger.rs", RUST_SRC, "rust")
        self.assertIn("post_entry", self._names(nodes, NodeKind.FUNCTION))
        self.assertIn("Ledger", self._names(nodes, NodeKind.CLASS))

    def test_language_for(self):
        self.assertEqual(substrate_generic.language_for("a/b.tsx"), "typescript")
        self.assertEqual(substrate_generic.language_for("x.go"), "go")
        self.assertIsNone(substrate_generic.language_for("readme.md"))
        self.assertIsNone(substrate_generic.language_for("Makefile"))

    def test_nodes_carry_slice_hashes(self):
        nodes, _ = substrate_generic.extract("billing/refund.go", GO_SRC, "go")
        defs = [n for n in nodes if n.kind != NodeKind.MODULE]
        self.assertTrue(all(n.slice_hash for n in defs))

    def test_symbol_refetch_generic(self):
        src, lineno, end = substrate_generic.extract_symbol_source(GO_SRC, "Refund", "go")
        self.assertIn("func Refund", src)
        self.assertNotIn("func helper", src)  # span stops at the next def


class PolyglotIndexing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "svc.py").write_text("def entry():\n    return 1\n")
        (self.root / "svc.go").write_text(GO_SRC)
        (self.root / "web.ts").write_text(TS_SRC)
        (self.root / "notes.md").write_text("# not code\n")
        self.model = CodebaseModel(self.root, db_path=self.root / ".cbm.db")
        self.model.build()

    def tearDown(self):
        self.model.close()
        self.dir.cleanup()

    def test_all_languages_indexed(self):
        files = set(self.model.store.all_files())
        self.assertEqual({"svc.py", "svc.go", "web.ts"}, files)
        names = {n.name for n in self.model.store.all_nodes()}
        self.assertIn("entry", names)          # python AST
        self.assertIn("Refund", names)         # go regex
        self.assertIn("BillingClient", names)  # ts regex

    def test_generic_citation_refetches(self):
        cite = Citation(file="svc.go", symbol="Refund")
        res = refetch(cite, self.root)
        self.assertTrue(res.found)
        self.assertIn("func Refund", res.source)

    def test_file_cap_prefers_python(self):
        self.model.indexer.max_files = 2
        files = self.model.indexer.discover_source_files()
        self.assertEqual(len(files), 2)
        self.assertIn("svc.py", files)  # python survives the cap
        self.assertEqual(self.model.indexer.last_skipped, 1)


class SymbolGranularInvalidation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "svc.py").write_text(
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
        self.model = CodebaseModel(self.root, db_path=self.root / ".cbm.db")
        self.model.build()
        self.b_alpha = self.model.record_belief(
            "(alpha returns one)", justified_by=[format_citation("svc.py", "alpha")])
        self.b_beta = self.model.record_belief(
            "(beta returns two)", justified_by=[format_citation("svc.py", "beta")])
        self.b_file = self.model.record_belief(
            "(svc module is small)", justified_by=["svc.py"])

    def tearDown(self):
        self.model.close()
        self.dir.cleanup()

    def _stale(self, belief):
        return self.model.store.get_belief(belief.id).stale

    def test_only_changed_symbol_goes_stale(self):
        # touch beta only; alpha's slice is untouched
        (self.root / "svc.py").write_text(
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 222\n")
        self.model.on_edit("svc.py")
        self.assertFalse(self._stale(self.b_alpha))
        self.assertTrue(self._stale(self.b_beta))
        # whole-file citation always goes stale on any change
        self.assertTrue(self._stale(self.b_file))

    def test_sync_is_also_symbol_granular(self):
        (self.root / "svc.py").write_text(
            "def alpha():\n    return 111\n\n\ndef beta():\n    return 2\n")
        res = self.model.sync()
        self.assertTrue(self._stale(self.b_alpha))
        self.assertFalse(self._stale(self.b_beta))
        self.assertGreaterEqual(res["invalidated"], 1)

    def test_new_symbol_added_marks_nothing_extra(self):
        (self.root / "svc.py").write_text(
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n\n\n"
            "def gamma():\n    return 3\n")
        self.model.on_edit("svc.py")
        self.assertFalse(self._stale(self.b_alpha))
        self.assertFalse(self._stale(self.b_beta))


class ModelGC(unittest.TestCase):
    def test_gc_removes_orphans_keeps_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp) / "models"
            models_dir.mkdir()
            live_root = Path(tmp) / "live-repo"
            live_root.mkdir()
            dead_root = Path(tmp) / "dead-repo"

            live = ModelStore(models_dir / "live.db")
            live.set_meta("root", str(live_root))
            live.close()
            dead = ModelStore(models_dir / "dead.db")
            dead.set_meta("root", str(dead_root))  # directory never created
            dead.close()
            legacy = ModelStore(models_dir / "legacy.db")  # no root meta
            legacy.close()

            from tether.core.codebase_model import service
            orig = service.CONFIG_DIR
            service.CONFIG_DIR = Path(tmp)
            try:
                report = gc_models()
            finally:
                service.CONFIG_DIR = orig

            removed = {r["db"] for r in report["removed"]}
            self.assertEqual({"dead.db"}, removed)
            self.assertIn("live.db", report["kept"])
            self.assertIn("legacy.db", report["unknown"])
            self.assertFalse((models_dir / "dead.db").exists())
            self.assertTrue((models_dir / "live.db").exists())


if __name__ == "__main__":
    unittest.main()
