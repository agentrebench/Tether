"""Tests for the REPL Tab-completion (slash commands + path args)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from prompt_toolkit.document import Document
    from tether.ui.repl import TetherCompleter, SLASH_COMMANDS
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - prompt_toolkit may be absent
    TetherCompleter = None
    _IMPORT_ERR = e


def _complete(text):
    c = TetherCompleter()
    doc = Document(text, len(text))
    return [comp.text for comp in c.get_completions(doc, None)]


@unittest.skipIf(TetherCompleter is None, f"prompt_toolkit unavailable: {_IMPORT_ERR}")
class Completer(unittest.TestCase):
    def test_slash_prefix_lists_matches(self):
        out = _complete("/p")
        self.assertIn("/plan", out)
        self.assertIn("/provider", out)
        self.assertNotIn("/help", out)

    def test_full_slash_completes_to_self(self):
        self.assertEqual(_complete("/why"), ["/why"])

    def test_prose_yields_nothing(self):
        self.assertEqual(_complete("build me a thing"), [])

    def test_unknown_slash_yields_nothing(self):
        self.assertEqual(_complete("/zzz"), [])

    def test_cd_path_completion(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "subdir")
            os.mkdir(sub)
            out = _complete(f"/cd {d}/")
            self.assertIn("subdir", out)

    def test_all_commands_complete_from_slash(self):
        # Every advertised command should be reachable by completing "/".
        out = set(_complete("/"))
        self.assertTrue(set(SLASH_COMMANDS).issubset(out))


if __name__ == "__main__":
    unittest.main()
