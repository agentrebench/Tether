"""Skills system: scanning + mtime cache, metadata fields, write round-trip,
builtin content, and skill_manage metadata passthrough."""
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.skills import Skill, SkillStore, BUILTIN_SKILLS_DIR


def _write_skill(root: Path, slug: str, frontmatter: str, body: str = "Body.") -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}")
    return p


class MetadataFields(unittest.TestCase):
    def test_when_to_use_and_argument_hint_parsed(self):
        with TemporaryDirectory() as tmp:
            p = _write_skill(Path(tmp), "deploy",
                             "name: Deploy\ndescription: ship it\n"
                             "when_to_use: user asks to deploy\n"
                             "argument_hint: <env>")
            sk = Skill.from_file(p, "user", True)
            self.assertEqual(sk.when_to_use, "user asks to deploy")
            self.assertEqual(sk.argument_hint, "<env>")

    def test_kebab_case_keys_accepted(self):
        with TemporaryDirectory() as tmp:
            p = _write_skill(Path(tmp), "x",
                             "name: X\nwhen-to-use: on demand\nargument-hint: <f>")
            sk = Skill.from_file(p, "user", True)
            self.assertEqual(sk.when_to_use, "on demand")
            self.assertEqual(sk.argument_hint, "<f>")

    def test_write_round_trips_metadata(self):
        with TemporaryDirectory() as tmp:
            store = SkillStore(extra_dirs=[])
            with mock.patch("tether.core.skills.USER_SKILLS_DIR", Path(tmp)):
                store.roots = [(Path(tmp), "user", True)]
                store.write("My Skill", "Do the thing.",
                            description="d", when_to_use="w", argument_hint="<a>")
                sk = store.get("my-skill")
            self.assertIsNotNone(sk)
            self.assertEqual(sk.when_to_use, "w")
            self.assertEqual(sk.argument_hint, "<a>")
            self.assertEqual(sk.body, "Do the thing.")


class MtimeCache(unittest.TestCase):
    def test_unchanged_files_not_reparsed(self):
        with TemporaryDirectory() as tmp:
            _write_skill(Path(tmp), "alpha", "name: Alpha\ndescription: a")
            store = SkillStore(extra_dirs=[str(tmp)])
            store.all()  # warm
            with mock.patch.object(Skill, "from_file",
                                   side_effect=AssertionError("reparsed")) as ff:
                skills = store.all()  # served from cache — from_file not called
            self.assertIn("alpha", {s.slug for s in skills})

    def test_changed_file_reparsed(self):
        with TemporaryDirectory() as tmp:
            p = _write_skill(Path(tmp), "alpha", "name: Alpha\ndescription: old")
            store = SkillStore(extra_dirs=[str(tmp)])
            self.assertEqual(store.get("alpha").description, "old")
            p.write_text("---\nname: Alpha\ndescription: new\n---\nBody.")
            os.utime(p, (time.time() + 5, time.time() + 5))  # force mtime change
            self.assertEqual(store.get("alpha").description, "new")

    def test_deleted_file_disappears(self):
        with TemporaryDirectory() as tmp:
            p = _write_skill(Path(tmp), "alpha", "name: Alpha")
            store = SkillStore(extra_dirs=[str(tmp)])
            self.assertIsNotNone(store.get("alpha"))
            p.unlink()
            p.parent.rmdir()
            self.assertIsNone(store.get("alpha"))


class BuiltinSkills(unittest.TestCase):
    def test_all_builtins_parse_with_full_metadata(self):
        found = sorted(p.parent.name for p in BUILTIN_SKILLS_DIR.glob("*/SKILL.md"))
        self.assertIn("code-review", found)
        self.assertIn("write-tests", found)
        self.assertIn("refactor-safely", found)
        self.assertGreaterEqual(len(found), 5)
        for p in BUILTIN_SKILLS_DIR.glob("*/SKILL.md"):
            sk = Skill.from_file(p, "builtin", False)
            self.assertIsNotNone(sk, p)
            self.assertTrue(sk.description, f"{p} missing description")
            self.assertTrue(sk.body.strip(), f"{p} empty body")
            self.assertIn("## Procedure", sk.body, p)

    def test_new_builtins_have_triggers(self):
        for slug in ("code-review", "write-tests", "refactor-safely"):
            sk = Skill.from_file(BUILTIN_SKILLS_DIR / slug / "SKILL.md", "builtin", False)
            self.assertTrue(sk.when_to_use, slug)
            self.assertTrue(sk.argument_hint, slug)

    def test_no_diagnostics_on_builtins(self):
        store = SkillStore(extra_dirs=[])
        store.roots = [(BUILTIN_SKILLS_DIR, "builtin", False)]
        problems = [p for p in store.diagnostics()
                    if str(BUILTIN_SKILLS_DIR) in p and "bundle" not in p]
        self.assertEqual(problems, [], problems)


class SkillManageMetadata(unittest.TestCase):
    def test_create_passes_metadata_through(self):
        from tether.tools.skill_manage import SkillManageTool
        store = mock.Mock()
        store.get.return_value = None
        store.write.return_value = Path("/tmp/x/SKILL.md")
        tool = SkillManageTool(store=store)
        res = tool.execute({
            "action": "create", "name": "Ship It", "content": "Steps.",
            "description": "d", "when_to_use": "w", "argument_hint": "<a>",
        })
        self.assertFalse(res.is_error, res.content)
        kwargs = store.write.call_args.kwargs
        self.assertEqual(kwargs["when_to_use"], "w")
        self.assertEqual(kwargs["argument_hint"], "<a>")


if __name__ == "__main__":
    unittest.main()
