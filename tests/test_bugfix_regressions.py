"""Regression tests for bugs found in the 2026-08 code review: s-expression
escape round-trips, line-based file edits on files without a trailing newline,
intraword emphasis in the markdown renderer, tool-batch-safe compaction,
local->local provider re-selection, and turn-limit reporting for sub-agents."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from tether.core.codebase_model.sexpr import Symbol, read, write
from tether.core.config import DEFAULT_CONTEXT_SIZE, TetherConfig, apply_provider_selection
from tether.core.models import Message, ToolCall
from tether.engine.query_engine import QueryEngine
from tether.tools.file_edit import FileEditTool
from tether.ui.markdown import to_runs


class SexprRoundTrip(unittest.TestCase):
    def test_backslash_before_escape_letter_round_trips(self):
        for s in ["C:\\new", 'say "hi"\\n', "tab\\there", "plain", "\\\\", "a\nb\tc"]:
            with self.subTest(s=s):
                self.assertEqual(read(write(s)), s)

    def test_inf_nan_are_symbols_not_floats(self):
        forms = read("(inf nan Infinity -3e2 .5 +4 7)")
        self.assertEqual(forms[:3], [Symbol("inf"), Symbol("nan"), Symbol("Infinity")])
        self.assertEqual(forms[3:], [-300.0, 0.5, 4, 7])


class FileEditNoTrailingNewline(unittest.TestCase):
    def setUp(self):
        self.tool = FileEditTool.__new__(FileEditTool)

    def test_replace_middle_line_keeps_following_line_separate(self):
        self.assertEqual(self.tool._replace_lines("a\nb\nc", 2, 2, "X")[0], "a\nX\nc")

    def test_replace_last_line_keeps_no_trailing_newline(self):
        self.assertEqual(self.tool._replace_lines("a\nb\nc", 3, 3, "X")[0], "a\nb\nX")

    def test_insert_before_first_line(self):
        self.assertEqual(self.tool._insert_at_line("a\nb", 1, "c", before=True)[0], "c\na\nb")

    def test_insert_after_last_line_without_newline(self):
        self.assertEqual(self.tool._insert_at_line("a\nb", 2, "c", before=False)[0], "a\nb\nc")

    def test_insert_after_last_line_with_newline(self):
        self.assertEqual(self.tool._insert_at_line("a\nb\n", 2, "c", before=False)[0], "a\nb\nc\n")


class MarkdownIntrawordEmphasis(unittest.TestCase):
    def test_identifiers_and_arithmetic_are_left_alone(self):
        runs = to_runs("use my_var_name and 2*3*4 here")
        self.assertEqual("".join(text for text, _ in runs), "use my_var_name and 2*3*4 here")

    def test_real_emphasis_still_parses(self):
        texts = [text for text, _ in to_runs("*real* and _also_")]
        self.assertIn("real", texts)
        self.assertIn("also", texts)


class CompactionKeepsToolBatchesIntact(unittest.TestCase):
    def test_kept_window_never_starts_with_orphaned_tool_result(self):
        for pad in range(4):  # shift the split point across every batch position
            with self.subTest(pad=pad):
                self._check(pad)

    def _check(self, pad: int):
        engine = object.__new__(QueryEngine)
        engine._compact_attempts = 1  # keep = 20
        msgs = [Message(role="system", content="sys")]
        # 30 turns of: user, assistant(tool_calls), tool, tool
        for i in range(30):
            msgs.append(Message(role="user", content=f"u{i}"))
            msgs.append(Message(role="assistant", tool_calls=[
                ToolCall(id=f"c{i}", name="bash", arguments={}),
                ToolCall(id=f"d{i}", name="bash", arguments={}),
            ]))
            msgs.append(Message(role="tool", content="r", tool_call_id=f"c{i}", name="bash"))
            msgs.append(Message(role="tool", content="r", tool_call_id=f"d{i}", name="bash"))
        # Tail padding moves the (from-the-end) split point across the batch.
        msgs += [Message(role="user", content=f"pad{i}") for i in range(pad)]
        engine.messages = msgs
        engine._compact()
        # First message after system (+ optional digest/ack pair) must not be a tool result.
        rest = [m for m in engine.messages[1:] if m.role != "user" or not m.content.startswith("[Earlier")]
        first_real = next(m for m in rest if m.role in ("assistant", "tool", "user") and m.content != "Understood. I have the context from the compacted conversation and will continue from where we left off.")
        self.assertNotEqual(first_real.role, "tool")
        # Every tool result still has its parent assistant tool_calls message before it.
        seen_ids: set[str] = set()
        for m in engine.messages:
            if m.role == "assistant":
                seen_ids.update(tc.id for tc in m.tool_calls)
            elif m.role == "tool":
                self.assertIn(m.tool_call_id, seen_ids)


class LocalToLocalProviderSelection(unittest.TestCase):
    def test_reselecting_local_keeps_custom_context_size(self):
        config = TetherConfig()
        config.provider = "local"
        config.context_size = DEFAULT_CONTEXT_SIZE * 2
        config.local_context_size = 0
        apply_provider_selection(config, "local")
        self.assertEqual(config.context_size, DEFAULT_CONTEXT_SIZE * 2)

    def test_local_to_local_with_stash_hands_over_new_model_context(self):
        # The desktop bridge stashes a newly selected model's context length in
        # local_context_size before re-selecting "local"; that must win.
        config = TetherConfig()
        config.provider = "local"
        config.context_size = 32768
        config.local_context_size = 131072
        apply_provider_selection(config, "local")
        self.assertEqual(config.context_size, 131072)
        self.assertEqual(config.local_context_size, 0)


if __name__ == "__main__":
    unittest.main()


class LiveModelDiscovery(unittest.TestCase):
    """Providers' /models listings are merged into the catalog, newest first,
    and unknown ids become selectable synthesized entries."""

    def setUp(self):
        from tether.core import config as cfg
        cfg._discovered_models.clear()
        self.cfg = cfg
        self.config = TetherConfig()
        self.config.api_keys = {"glm": "test-key"}

    def tearDown(self):
        self.cfg._discovered_models.clear()

    def _patch_urlopen(self, ids):
        import io, json
        from unittest import mock

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        body = json.dumps({"data": [{"id": i} for i in ids]}).encode()
        return mock.patch.object(self.cfg.urllib.request, "urlopen", return_value=_Resp(body))

    def test_discovered_ids_merge_newest_first_and_are_selectable(self):
        with self._patch_urlopen(["glm-4.5", "glm-9.0", "glm-5.3"]):
            models = self.cfg.provider_models("glm", self.config)
            ids = [m["id"] for m in models]
            self.assertEqual(ids[0], "glm-9.0")
            self.assertLess(ids.index("glm-5.3"), ids.index("glm-4.5"))
            synthesized = self.cfg.provider_model("glm", "glm-9.0", self.config)
        self.assertIsNotNone(synthesized)
        self.assertTrue(synthesized["discovered"])
        self.assertIn("thinking_modes", synthesized)  # inherited from the preset default
        # And it can actually be applied as the active model.
        with self._patch_urlopen(["glm-9.0"]):
            self.cfg._discovered_models.clear()
            self.cfg.apply_provider_selection(self.config, "glm", "glm-9.0")
        self.assertEqual(self.config.api_model, "glm-9.0")

    def test_discovery_failure_degrades_to_static_catalog(self):
        from unittest import mock
        with mock.patch.object(self.cfg.urllib.request, "urlopen", side_effect=OSError("offline")):
            ids = [m["id"] for m in self.cfg.provider_models("glm", self.config)]
        self.assertIn("glm-5.3", ids)
        self.assertIsNone(self.cfg.provider_model("glm", "glm-9.0", self.config))

    def test_no_key_means_no_network(self):
        from unittest import mock
        self.config.api_keys = {}
        with mock.patch.object(self.cfg.urllib.request, "urlopen") as urlopen:
            self.assertEqual(self.cfg.discover_provider_models(self.config, "glm"), [])
            urlopen.assert_not_called()

    def test_openai_filter_drops_non_chat_ids(self):
        self.config.api_keys = {"openai": "k"}
        with self._patch_urlopen(["gpt-5.6", "text-embedding-3-large", "gpt-5.5-2026-04-23", "whisper-1", "sol"]):
            ids = self.cfg.discover_provider_models(self.config, "openai")
        self.assertEqual(ids, ["gpt-5.6", "sol"])

    def test_version_sort(self):
        k = self.cfg._version_sort_key
        order = sorted(["gpt-5.4-mini", "gpt-5.5", "gpt-5.4", "gpt-5.10"], key=k, reverse=True)
        self.assertEqual(order, ["gpt-5.10", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"])


class CodexCatalogFromCliCache(unittest.TestCase):
    """The Codex picker mirrors the Codex CLI's own models_cache.json."""

    def setUp(self):
        import json, tempfile
        from tether.core import config as cfg
        self.cfg = cfg
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"models": [
            {"slug": "gpt-9.1-nova", "display_name": "GPT-9.1-Nova", "priority": 1, "visibility": "list",
             "context_window": 300000, "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "ultra"}]},
            {"slug": "hidden-thing", "priority": 0, "visibility": "hide"},
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "priority": 7, "visibility": "list",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        ]}, self.tmp)
        self.tmp.close()
        self.orig = cfg.CODEX_MODELS_CACHE
        cfg.CODEX_MODELS_CACHE = __import__("pathlib").Path(self.tmp.name)
        cfg._codex_models_cache.clear()

    def tearDown(self):
        import os
        self.cfg.CODEX_MODELS_CACHE = self.orig
        self.cfg._codex_models_cache.clear()
        os.unlink(self.tmp.name)

    def test_cache_drives_order_metadata_and_selection(self):
        config = TetherConfig()
        models = self.cfg.provider_models("codex", config)
        ids = [m["id"] for m in models]
        self.assertEqual(ids[:2], ["gpt-9.1-nova", "gpt-5.5"])   # CLI priority order
        self.assertNotIn("hidden-thing", ids)
        nova = models[0]
        self.assertEqual(nova["reasoning_efforts"], ["low", "medium", "ultra"])
        self.assertEqual(nova["default_reasoning_effort"], "medium")
        self.assertEqual(nova["context_size"], 300000)
        self.cfg.apply_provider_selection(config, "codex", "gpt-9.1-nova", reasoning_effort="ultra")
        self.assertEqual((config.api_model, config.reasoning_effort, config.context_size),
                         ("gpt-9.1-nova", "ultra", 300000))

    def test_codex_backend_passes_reasoning_effort(self):
        from tether.engine.codex_backend import CodexExecBackend as CodexBackend
        config = TetherConfig()
        self.cfg.apply_provider_selection(config, "codex", "gpt-9.1-nova", reasoning_effort="ultra")
        backend = CodexBackend.__new__(CodexBackend)
        backend.config = config
        backend.cwd = "."
        _, cmd, _, _ = backend._build_exec_cmd("codex", json_mode=True)
        self.assertIn("gpt-9.1-nova", cmd)
        self.assertIn('model_reasoning_effort="ultra"', cmd)


class AutoLearnFromTurn(unittest.TestCase):
    """learn_from_turn records only cited, resolvable claims; never raises."""

    def _model(self):
        import tempfile
        from pathlib import Path
        from tether.core.codebase_model.service import CodebaseModel
        root = Path(__file__).resolve().parent.parent
        return CodebaseModel(root, db_path=Path(tempfile.mkdtemp()) / "m.db")

    def _turn(self):
        return [
            Message(role="user", content="explain beliefs"),
            Message(role="assistant", tool_calls=[
                ToolCall(id="1", name="file_read", arguments={"file_path": "core/codebase_model/beliefs.py"}),
                ToolCall(id="2", name="grep", arguments={"pattern": "demote"}),
            ]),
            Message(role="tool", content="...", tool_call_id="1", name="file_read"),
            Message(role="tool", content="...", tool_call_id="2", name="grep"),
            Message(role="assistant", content="BeliefManager owns demotion."),
        ]

    def test_records_cited_claims_and_drops_uncited(self):
        from tether.core.codebase_model.learn import learn_from_turn

        class FakeBackend:
            def chat_completion(self, messages, tools=None, max_tokens=0):
                return Message(role="assistant", content='''Here you go:
[{"kind":"belief","claim":"BeliefManager in core/codebase_model/beliefs.py owns belief demotion and eviction","citations":["core/codebase_model/beliefs.py @ BeliefManager.demote"],"confidence":0.8},
 {"kind":"belief","claim":"This claim has no evidence at all","citations":[],"confidence":0.9},
 {"kind":"belief","claim":"This one cites a file that does not exist","citations":["nope/missing.py @ X"]},
 {"kind":"invariant","claim":"tools/ must not import ui/","citations":["tools/base.py"],"confidence":0.7}]'''), {}
        model = self._model()
        report = learn_from_turn(model, FakeBackend(), self._turn())
        self.assertEqual(len(report["recorded"]), 2, report)
        self.assertEqual(report["skipped"], 2)
        beliefs = model.beliefs.all()
        self.assertEqual(len(beliefs), 1)
        self.assertTrue(beliefs[0].justified_by[0].startswith("core/codebase_model/beliefs.py @ BeliefManager.demote"))
        self.assertEqual(beliefs[0].source, "learned")
        self.assertEqual(len(model.store.all_invariants()), 1)

    def test_small_turns_and_backend_errors_are_safe(self):
        from tether.core.codebase_model.learn import learn_from_turn

        class Boom:
            def chat_completion(self, *a, **k):
                raise RuntimeError("offline")
        model = self._model()
        self.assertEqual(learn_from_turn(model, Boom(), [Message(role="user", content="hi")])["reason"], "turn too small")
        report = learn_from_turn(model, Boom(), self._turn())
        self.assertIn("learning skipped", report["reason"])
        self.assertEqual(model.beliefs.all(), [])


class BlastRadiusBinding(unittest.TestCase):
    """Callers of an ambiguous simple name (e.g. `execute`) only count when
    they can see the callee: same file, or an import from its module."""

    def test_ambiguous_name_is_bound_by_imports(self):
        import tempfile
        from pathlib import Path
        from tether.core.codebase_model.service import CodebaseModel
        root = Path(tempfile.mkdtemp())
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "a.py").write_text("class A:\n    def run(self):\n        return 1\n")
        (root / "pkg" / "b.py").write_text("class B:\n    def run(self):\n        return 2\n")
        (root / "pkg" / "uses_a.py").write_text("from pkg.a import A\n\ndef go():\n    return A().run()\n")
        (root / "pkg" / "uses_b.py").write_text("from .b import B\n\ndef go():\n    return B().run()\n")
        (root / "pkg" / "unrelated.py").write_text("def go(x):\n    return x.run()\n")
        model = CodebaseModel(root, db_path=root / "m.db")
        model.build()
        a_callers = model.indexer.blast_radius("pkg/a.py::A.run", max_depth=1)
        b_callers = model.indexer.blast_radius("pkg/b.py::B.run", max_depth=1)
        self.assertEqual(a_callers, ["pkg/uses_a.py::go"])
        self.assertEqual(b_callers, ["pkg/uses_b.py::go"])


class GroundingGuard(unittest.TestCase):
    """A substantive answer about the project with zero tool calls gets one nudge."""

    def test_fires_only_for_ungrounded_project_answers(self):
        need = QueryEngine._needs_grounding
        long = "Tether seems like a thoughtful local-first harness. " * 8
        self.assertTrue(need("What do you think of this project?", long, "question", []))
        self.assertTrue(need("Review the codebase and tell me what stands out", long, "review", []))
        self.assertFalse(need("What do you think of this project?", long, "question", ["file_read"]))
        self.assertFalse(need("What is a Python decorator?", long, "question", []))
        self.assertFalse(need("What do you think of this project?", "Which part?", "question", []))
        self.assertFalse(need("fix the bug in this repo", long, "edit", []))


class LiveCodeDecoding(unittest.TestCase):
    """The engine decodes a write call's JSON string argument incrementally."""

    def test_partial_json_string_handles_escapes_and_truncation(self):
        from tether.engine.query_engine import _partial_json_string, _json_string_closed
        buf = '{"file_path": "m.py", "content": "def f():\\n    return \\"x\\"'
        self.assertEqual(_partial_json_string(buf, ("content",)), 'def f():\n    return "x"')
        self.assertTrue(_json_string_closed(buf, "file_path"))
        self.assertFalse(_json_string_closed(buf, "content"))
        # dangling backslash / incomplete \\u escape: wait for more
        self.assertEqual(_partial_json_string(buf + "\\", ("content",)), 'def f():\n    return "x"')
        self.assertEqual(_partial_json_string(buf + "\\u00", ("content",)), 'def f():\n    return "x"')
        self.assertEqual(_partial_json_string(buf + '\\u00e9"}', ("content",)), 'def f():\n    return "x"é')
        self.assertIsNone(_partial_json_string('{"file_path": "a', ("content",)))


class Attachments(unittest.TestCase):
    """Composer attachments are folded into the prompt by the bridge."""

    def test_resolve_attachments_pastes_files_images_and_failures(self):
        import tempfile
        from pathlib import Path
        from tether.app_bridge import resolve_attachments
        d = Path(tempfile.mkdtemp())
        (d / "a.py").write_text("print(1)\nprint(2)\n")
        (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        prompt, images, notes = resolve_attachments("look", [
            {"kind": "paste", "name": "Pasted text", "text": "l1\nl2\nl3"},
            {"kind": "file", "name": "a.py", "path": str(d / "a.py")},
            {"kind": "file", "name": "img.png", "path": str(d / "img.png")},
            {"kind": "file", "name": "missing.txt", "path": str(d / "nope.txt")},
        ])
        self.assertIn("[Pasted text 1 — 3 lines]", prompt)
        self.assertIn("```py\nprint(1)", prompt)
        self.assertIn("[Attached image 3: img.png]", prompt)
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))
        self.assertEqual([n["ok"] for n in notes], [True, True, True, False])
        self.assertIn("could not be attached", prompt)

    def test_user_message_with_images_serialises_as_content_parts(self):
        m = Message(role="user", content="what is this?", images=["data:image/png;base64,AAAA"])
        d = m.to_api_dict()
        self.assertEqual(d["content"][0], {"type": "text", "text": "what is this?"})
        self.assertEqual(d["content"][1]["type"], "image_url")
        self.assertEqual(Message.from_dict(m.to_dict()).images, m.images)
        # No images → plain string content, unchanged for every provider.
        self.assertEqual(Message(role="user", content="hi").to_api_dict()["content"], "hi")
