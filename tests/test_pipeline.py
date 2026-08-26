from __future__ import annotations

import builtins
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]

import cli
import llm_client
import openkb_backend
import processing_state
import summarize
import transcribe as transcribe_module


class PipelineTests(unittest.TestCase):
    def test_powershell_launchers_are_ascii_for_windows_powershell_51(self):
        for name in ("setup.ps1", "run.ps1"):
            raw = (ROOT / name).read_bytes()
            self.assertTrue(raw.isascii(), f"{name} must remain ASCII-compatible")

    def test_runtime_versions_remain_pinned(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("ctranslate2==4.6.0", requirements)
        self.assertIn("onnxruntime==1.20.1", requirements)
        self.assertIn("openkb==0.4.5", requirements)
        self.assertNotIn("kuzu", requirements.lower())

    def test_llm_failure_raises_instead_of_returning_error_note(self):
        with (
            mock.patch.object(llm_client.urllib.request, "urlopen", side_effect=OSError("offline")),
            mock.patch.object(llm_client.time, "sleep"),
        ):
            with self.assertRaises(llm_client.LLMRequestError):
                llm_client._api_chat("https://example.invalid/v1", "key", "model", [])

    def test_summarize_failure_preserves_existing_note(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            transcript = tmp / "lesson.txt"
            transcript.write_text("valid transcript", encoding="utf-8")
            existing = tmp / "lesson.md"
            existing.write_text("old good note", encoding="utf-8")
            with (
                mock.patch.object(summarize, "NOTES", tmp),
                mock.patch.object(summarize.llm_client, "generate", side_effect=RuntimeError("offline")),
            ):
                with self.assertRaises(RuntimeError):
                    summarize.summarize(transcript)
            self.assertEqual(existing.read_text(encoding="utf-8"), "old good note")

    def test_summarize_applies_safe_corrections_and_hides_raw_backup(self):
        response = """<OPENCLASSNOTES_CORRECTIONS>
[{"line": 1, "from": "付里叶变换", "to": "傅里叶变换", "type": "homophone", "confidence": 0.99},
 {"line": 1, "from": "实域问题", "to": "时域问题", "type": "homophone", "confidence": 0.98}]
</OPENCLASSNOTES_CORRECTIONS>
<OPENCLASSNOTES_NOTE>
# 傅里叶变换课堂笔记

- 傅里叶变换把时域问题转换到频率域。
</OPENCLASSNOTES_NOTE>"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            transcripts = tmp / "transcripts"
            notes = tmp / "notes"
            backups = tmp / "private" / "raw_transcripts"
            transcripts.mkdir()
            transcript = transcripts / "lesson.txt"
            raw = "[   12.2 -    19.6] 付里叶变换可以把实域问题转到频率域。\n"
            transcript.write_text(raw, encoding="utf-8")
            with (
                mock.patch.object(summarize, "NOTES", notes),
                mock.patch.object(summarize, "RAW_TRANSCRIPTS", backups),
                mock.patch.object(summarize.llm_client, "generate", return_value=response) as generate,
            ):
                note = summarize.summarize(transcript)
            backup_files = list(backups.glob("lesson.*.txt"))
            backup_text = backup_files[0].read_text(encoding="utf-8") if backup_files else ""
            transcript_text = transcript.read_text(encoding="utf-8")
            note_text = note.read_text(encoding="utf-8")
        self.assertEqual(len(backup_files), 1)
        self.assertEqual(backup_text, raw)
        self.assertIn("傅里叶变换", transcript_text)
        self.assertIn("时域问题", transcript_text)
        self.assertNotIn("付里叶变换", transcript_text)
        self.assertIn("# 傅里叶变换课堂笔记", note_text)
        self.assertEqual(generate.call_count, 1)
        sent = generate.call_args.args[1]
        self.assertIn("L00001 |", sent)

    def test_correction_validator_rejects_semantic_and_ambiguous_edits(self):
        raw = "[0.0 - 1.0] 付里叶变换不稳定，占30%。\n[1.0 - 2.0] 重复 重复\n"
        proposals = [
            {"line": 1, "from": "付里叶变换", "to": "傅里叶变换", "type": "homophone", "confidence": 0.99},
            {"line": 1, "from": "30%", "to": "40%", "type": "asr_fragment", "confidence": 0.99},
            {"line": 1, "from": "不稳定", "to": "稳定", "type": "technical_term", "confidence": 0.99},
            {"line": 2, "from": "重复", "to": "术语", "type": "asr_fragment", "confidence": 0.99},
            {"line": 1, "from": "占", "to": "这里加入一整段模型自行生成的解释", "type": "asr_fragment", "confidence": 0.99},
            {"line": 1, "from": "稳定", "to": "收敛", "type": "technical_term", "confidence": 0.7},
        ]
        cleaned, accepted, rejected = summarize._validated_edits(raw, proposals)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, 5)
        self.assertIn("傅里叶变换不稳定，占30%", cleaned)
        self.assertIn("重复 重复", cleaned)

    def test_malformed_correction_protocol_is_not_saved_as_a_note(self):
        response = "<OPENCLASSNOTES_CORRECTIONS>[]</OPENCLASSNOTES_CORRECTIONS>"
        with self.assertRaisesRegex(ValueError, "清洗协议不完整"):
            summarize._parse_model_response(response)

    def test_malformed_protocol_preserves_transcript_and_existing_note(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            transcript = tmp / "lesson.txt"
            transcript.write_text("[0.0 - 1.0] 原始转写\n", encoding="utf-8")
            note = tmp / "lesson.md"
            note.write_text("old good note", encoding="utf-8")
            with (
                mock.patch.object(summarize, "NOTES", tmp),
                mock.patch.object(summarize, "RAW_TRANSCRIPTS", tmp / "backups"),
                mock.patch.object(
                    summarize.llm_client,
                    "generate",
                    return_value=(
                        "<OPENCLASSNOTES_CORRECTIONS>[]"
                        "</OPENCLASSNOTES_CORRECTIONS>"
                    ),
                ),
            ):
                with self.assertRaises(ValueError):
                    summarize.summarize(transcript)
            self.assertEqual(transcript.read_text(encoding="utf-8"), "[0.0 - 1.0] 原始转写\n")
            self.assertEqual(note.read_text(encoding="utf-8"), "old good note")
            self.assertFalse((tmp / "backups").exists())

    def test_chunking_preserves_global_line_ids(self):
        chunks = summarize._chunk_numbered_transcript("first\nsecond\nthird\n", limit=25)
        joined = "\n".join(chunks)
        self.assertIn("L00001 | first", joined)
        self.assertIn("L00002 | second", joined)
        self.assertIn("L00003 | third", joined)
        self.assertEqual(joined.count("L000"), 3)

    def test_english_transcript_explicitly_requests_english_note(self):
        with mock.patch.object(summarize.llm_client, "generate", return_value="note") as generate:
            summarize.call_llm("[0.0 - 1.0] This lecture explains orbital mechanics.")
        self.assertIn("最终笔记必须使用 English", generate.call_args.args[1])

    def test_formula_policy_is_present_in_both_pipeline_stages(self):
        self.assertIn("不写表达式、推导", summarize.SYSTEM_PROMPT)
        self.assertIn("spoken names only", openkb_backend._PRODUCT_RULES)
        self.assertIn("Never", openkb_backend._PRODUCT_RULES)

    def test_openkb_compiler_policy_is_strict_and_idempotent(self):
        from openkb.agent import compiler

        openkb_backend._apply_compiler_policy()
        first = compiler._CONCEPT_PAGE_USER
        openkb_backend._apply_compiler_policy()
        self.assertEqual(first, compiler._CONCEPT_PAGE_USER)
        self.assertIn("General model knowledge is banned", first)
        self.assertEqual(first.count(openkb_backend._COMPILER_MARKER), 1)

    def test_all_conversion_failures_do_not_archive_or_claim_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "mp3").mkdir()
            (tmp / "mp3" / "broken.mp3").write_bytes(b"broken")
            output = io.StringIO()
            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(builtins, "input", return_value=""),
                mock.patch.object(cli, "transcribe", side_effect=RuntimeError("decode failed")),
                mock.patch.object(cli.openkb_backend, "sync_notes") as archive,
                contextlib.redirect_stdout(output),
            ):
                result = cli.cmd_convert()
            self.assertFalse(result)
            archive.assert_not_called()
            self.assertIn("转换失败", output.getvalue())

    def test_search_uses_openkb_query_once(self):
        output = io.StringIO()
        with (
            mock.patch.object(builtins, "input", return_value="query"),
            mock.patch.object(cli.openkb_backend, "ask", return_value="grounded answer") as ask,
            contextlib.redirect_stdout(output),
        ):
            result = cli.cmd_search()
        self.assertTrue(result)
        ask.assert_called_once_with("query")
        self.assertIn("grounded answer", output.getvalue())

    def test_openkb_init_uses_app_credentials_only_in_memory(self):
        from openkb.config import load_config

        with tempfile.TemporaryDirectory() as td:
            kb = Path(td) / "knowledge"
            with (
                mock.patch.object(
                    openkb_backend,
                    "_api_settings",
                    return_value=("secret-test-key", "https://api.deepseek.com/v1", "deepseek/deepseek-chat"),
                ),
            ):
                openkb_backend.ensure_knowledge_base(kb)
            config = load_config(kb / ".openkb" / "config.yaml")
            schema = (kb / "wiki" / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(config["model"], "deepseek/deepseek-chat")
            self.assertIn("OpenClassNotes source-grounding rules", schema)
            self.assertFalse((kb / ".env").exists())

    def test_openkb_environment_restores_credentials(self):
        keys = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_AGENTS_DISABLE_TRACING")
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(
                openkb_backend,
                "_api_settings",
                return_value=("temporary-key", "https://api.deepseek.com/v1", "deepseek/model"),
            ),
        ):
            before = {key: os.environ.get(key) for key in keys}
            with openkb_backend._openkb_environment():
                self.assertEqual(os.environ["LLM_API_KEY"], "temporary-key")
                self.assertEqual(os.environ["OPENAI_AGENTS_DISABLE_TRACING"], "1")
            self.assertEqual({key: os.environ.get(key) for key in keys}, before)

    def test_unchanged_note_is_skipped_without_llm_compile(self):
        from openkb.state import HashRegistry

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            kb = tmp / "knowledge"
            (kb / ".openkb").mkdir(parents=True)
            note = tmp / "lesson.md"
            note.write_text("# Lesson\n\nStable content.", encoding="utf-8")
            registry = HashRegistry(kb / ".openkb" / "hashes.json")
            registry.add(registry.hash_file(note), {"name": note.name, "doc_name": "lesson-old"})
            with (
                mock.patch.object(openkb_backend, "ensure_knowledge_base", return_value=kb),
                mock.patch.object(openkb_backend, "_api_settings", return_value=("key", "", "model")),
                mock.patch("openkb.cli.add_single_file") as add,
            ):
                result = openkb_backend.sync_notes([note], kb_dir=kb)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        add.assert_not_called()

    def test_changed_note_is_added_before_old_version_is_removed(self):
        from openkb.state import HashRegistry

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            kb = tmp / "knowledge"
            (kb / ".openkb").mkdir(parents=True)
            note = tmp / "lesson.md"
            note.write_text("# Lesson\n\nNew content.", encoding="utf-8")
            registry = HashRegistry(kb / ".openkb" / "hashes.json")
            registry.add("0" * 64, {"name": note.name, "doc_name": "lesson-old"})
            calls = []

            def add(*_args, **_kwargs):
                calls.append("add")
                return "added"

            def remove(*_args, **_kwargs):
                calls.append("remove")
                return {"status": "removed"}

            with (
                mock.patch.object(openkb_backend, "ensure_knowledge_base", return_value=kb),
                mock.patch.object(openkb_backend, "_api_settings", return_value=("key", "", "model")),
                mock.patch("openkb.cli.add_single_file", side_effect=add),
                mock.patch.object(openkb_backend, "_remove_document", side_effect=remove) as remove_call,
            ):
                result = openkb_backend.sync_notes([note], kb_dir=kb)
        self.assertEqual(calls, ["add", "remove"])
        self.assertEqual(result["added"], 1)
        remove_call.assert_called_once_with(kb, "lesson-old")

    def test_native_openkb_visualization_has_three_views(self):
        with tempfile.TemporaryDirectory() as td:
            kb = Path(td) / "knowledge"
            summaries = kb / "wiki" / "summaries"
            concepts = kb / "wiki" / "concepts"
            summaries.mkdir(parents=True)
            concepts.mkdir(parents=True)
            (summaries / "lesson.md").write_text(
                "---\ntype: Summary\ndescription: Lesson\n---\n\n"
                "# Signal Processing Lecture\n\n[[concepts/fourier-transform]]\n",
                encoding="utf-8",
            )
            (concepts / "fourier-transform.md").write_text(
                "---\ntype: Concept\ndescription: Spoken formula name\n---\n\n"
                "# Fourier Transform\n\n[[summaries/lesson]]\n",
                encoding="utf-8",
            )
            with mock.patch.object(openkb_backend, "ensure_knowledge_base", return_value=kb):
                result = openkb_backend.render_visualization(kb_dir=kb, open_browser=False)
            html = Path(result["path"]).read_text(encoding="utf-8")
        self.assertEqual(result["nodes"], 2)
        self.assertEqual(result["edges"], 1)
        self.assertIn('data-m="mindmap"', html)
        self.assertIn('data-m="radial"', html)
        self.assertIn('data-m="graph"', html)
        self.assertIn("Fourier Transform", html)
        self.assertIn("Signal Processing Lecture", html)

    def test_grounding_pass_removes_model_elaboration(self):
        with tempfile.TemporaryDirectory() as td:
            kb = Path(td) / "knowledge"
            wiki = kb / "wiki"
            for subdir in ("sources", "summaries", "concepts", "entities"):
                (wiki / subdir).mkdir(parents=True, exist_ok=True)
            source = "# Lesson\n\n- Large learning rate means unstable training.\n"
            (wiki / "sources" / "lesson.md").write_text(source, encoding="utf-8")
            (wiki / "summaries" / "lesson.md").write_text(
                "---\ntype: Summary\ndescription: generated\ndoc_type: short\n"
                "full_text: sources/lesson.md\n---\n\nGenerated summary.\n",
                encoding="utf-8",
            )
            (wiki / "concepts" / "learning-rate.md").write_text(
                "---\ntype: Concept\nsources: [\"summaries/lesson.md\"]\n"
                "description: generated\n---\n\n# Learning Rate\n\n"
                "A large value overshoots and oscillates.\n",
                encoding="utf-8",
            )
            openkb_backend._ground_wiki_pages(kb)
            concept = (wiki / "concepts" / "learning-rate.md").read_text(encoding="utf-8")
            summary = (wiki / "summaries" / "lesson.md").read_text(encoding="utf-8")
        self.assertNotIn("overshoots", concept)
        self.assertNotIn("oscillates", concept)
        self.assertIn("Large learning rate means unstable training", concept)
        self.assertIn("`summaries/lesson.md`", concept)
        self.assertNotIn("[[summaries/lesson]]", concept)
        self.assertIn(source.strip(), summary)
        self.assertIn("[[concepts/learning-rate]]", summary)

    def test_grounding_drops_concept_without_complete_title_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            kb = Path(td) / "knowledge"
            wiki = kb / "wiki"
            for subdir in ("sources", "summaries", "concepts", "entities"):
                (wiki / subdir).mkdir(parents=True, exist_ok=True)
            (wiki / "sources" / "lesson.md").write_text(
                "# Lesson\n\nGradient descent reduces loss.\n", encoding="utf-8"
            )
            (wiki / "summaries" / "lesson.md").write_text(
                "---\ntype: Summary\ndoc_type: short\n"
                "full_text: sources/lesson.md\n---\n",
                encoding="utf-8",
            )
            unsupported = wiki / "concepts" / "gradient-descent-variants.md"
            unsupported.write_text(
                "---\ntype: Concept\nsources: [\"summaries/lesson.md\"]\n---\n\n"
                "# Gradient Descent Variants\n",
                encoding="utf-8",
            )
            openkb_backend._ground_wiki_pages(kb)
            summary = (wiki / "summaries" / "lesson.md").read_text(encoding="utf-8")
        self.assertFalse(unsupported.exists())
        self.assertNotIn("gradient-descent-variants", summary)

    def test_variant_excerpt_prefers_variant_comparison_over_generic_definition(self):
        source = """# Lecture

- Gradient descent updates model parameters to reduce loss.
- Compare batch gradient descent, stochastic gradient descent, and mini-batch gradient descent.
- **Exam signal:** Comparison of gradient descent variants is required.
"""
        excerpts = openkb_backend._source_excerpts("Gradient Descent Variants", source)
        self.assertTrue(openkb_backend._title_is_grounded("Gradient Descent Variants", excerpts))
        variant_order = openkb_backend._order_page_excerpts(
            "Gradient Descent Variants", excerpts
        )
        base_order = openkb_backend._order_page_excerpts("Gradient Descent", excerpts)
        self.assertTrue(variant_order[0].startswith("Compare batch"))
        self.assertTrue(base_order[0].startswith("Gradient descent updates"))

    def test_each_summary_to_concept_edge_requires_its_own_evidence(self):
        from openkb import frontmatter

        with tempfile.TemporaryDirectory() as td:
            kb = Path(td) / "knowledge"
            wiki = kb / "wiki"
            for subdir in ("sources", "summaries", "concepts", "entities"):
                (wiki / subdir).mkdir(parents=True, exist_ok=True)
            (wiki / "sources" / "supported.md").write_text(
                "# Supported\n\nLarge learning rate means unstable training.\n",
                encoding="utf-8",
            )
            (wiki / "sources" / "unsupported.md").write_text(
                "# Unsupported\n\nGradient descent reduces loss.\n",
                encoding="utf-8",
            )
            for name in ("supported", "unsupported"):
                (wiki / "summaries" / f"{name}.md").write_text(
                    f"---\ntype: Summary\ndoc_type: short\n"
                    f"full_text: sources/{name}.md\n---\n",
                    encoding="utf-8",
                )
            concept = wiki / "concepts" / "learning-rate.md"
            concept.write_text(
                "---\ntype: Concept\n"
                "sources: [\"summaries/supported.md\", \"summaries/unsupported.md\"]\n"
                "---\n\n# Learning Rate\n",
                encoding="utf-8",
            )
            openkb_backend._ground_wiki_pages(kb)
            concept_meta = frontmatter.parse(concept.read_text(encoding="utf-8"))
            supported = (wiki / "summaries" / "supported.md").read_text(encoding="utf-8")
            unsupported = (wiki / "summaries" / "unsupported.md").read_text(encoding="utf-8")
        self.assertEqual(concept_meta["sources"], ["summaries/supported.md"])
        self.assertIn("[[concepts/learning-rate]]", supported)
        self.assertNotIn("[[concepts/learning-rate]]", unsupported)

    def test_sparse_long_vad_segment_triggers_no_vad_retry(self):
        primary = [
            SimpleNamespace(start=0.0, end=5.0, text="Opening words"),
            SimpleNamespace(start=5.0, end=45.0, text="Town"),
        ]
        fallback = [
            SimpleNamespace(start=0.0, end=5.0, text="Opening words"),
            SimpleNamespace(
                start=5.0,
                end=20.0,
                text="Recovered classroom explanation with several useful details",
            ),
        ]
        info = SimpleNamespace(language="en", language_probability=0.99)

        class FakeModel:
            calls = []

            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                self.calls.append(kwargs["vad_filter"])
                return iter(primary if kwargs["vad_filter"] else fallback), info

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audio = tmp / "lesson.wav"
            audio.write_bytes(b"audio")
            with (
                mock.patch.dict(os.environ, {"CLASSNOTES_DEVICE": "auto"}),
                mock.patch.object(transcribe_module, "TRANSCRIPTS", tmp),
                mock.patch.object(transcribe_module, "_cuda_runtime_available", return_value=True),
                mock.patch.object(transcribe_module, "WhisperModel", FakeModel),
            ):
                output = transcribe_module.transcribe(audio)
            self.assertEqual(FakeModel.calls, [True, False])
            self.assertIn("Recovered classroom explanation", output.read_text(encoding="utf-8"))

    def test_no_cuda_uses_cpu_automatically(self):
        info = SimpleNamespace(language="en", language_probability=0.99)
        devices = []

        class FakeModel:
            def __init__(self, *args, **kwargs):
                devices.append((kwargs["device"], kwargs["compute_type"]))

            def transcribe(self, path, **kwargs):
                segment = SimpleNamespace(start=0.0, end=2.0, text="CPU transcript")
                return iter([segment]), info

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audio = tmp / "lesson.wav"
            audio.write_bytes(b"audio")
            with (
                mock.patch.dict(os.environ, {"CLASSNOTES_DEVICE": "auto"}),
                mock.patch.object(transcribe_module, "TRANSCRIPTS", tmp),
                mock.patch.object(transcribe_module.ctranslate2, "get_cuda_device_count", return_value=0),
                mock.patch.object(transcribe_module, "WhisperModel", FakeModel),
            ):
                transcribe_module.transcribe(audio)
            self.assertEqual(devices, [("cpu", "int8")])

    def test_missing_windows_cuda_runtime_uses_cpu(self):
        devices = []

        class FakeModel:
            def __init__(self, *args, **kwargs):
                devices.append(kwargs["device"])

        with (
            mock.patch.dict(os.environ, {"CLASSNOTES_DEVICE": "auto"}),
            mock.patch.object(transcribe_module, "_cuda_runtime_available", return_value=False),
            mock.patch.object(transcribe_module, "WhisperModel", FakeModel),
        ):
            _, device, _ = transcribe_module._load_model("small")
        self.assertEqual(device, "cpu")
        self.assertEqual(devices, ["cpu"])

    def test_completed_audio_is_skipped_automatically(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for directory in ("mp3", "transcripts", "notes", "graphs"):
                (tmp / directory).mkdir()
            audio = tmp / "mp3" / "lesson.wav"
            transcript = tmp / "transcripts" / "lesson.txt"
            note = tmp / "notes" / "lesson.md"
            audio.write_bytes(b"audio")
            transcript.write_text("transcript", encoding="utf-8")
            note.write_text("note", encoding="utf-8")
            state = {"version": 1, "files": {}}
            processing_state.update_record(
                state, audio, "completed", transcript=transcript, note=note
            )
            processing_state.save_state(tmp / "graphs" / "processing_state.json", state)
            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(cli, "transcribe") as transcribe,
                mock.patch.object(cli, "summarize") as summarize_note,
            ):
                result = cli.cmd_convert()
            self.assertTrue(result)
            transcribe.assert_not_called()
            summarize_note.assert_not_called()

    def test_reprocess_menu_reruns_only_the_selected_completed_audio(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for directory in ("mp3", "transcripts", "notes", "graphs"):
                (tmp / directory).mkdir()
            audio = tmp / "mp3" / "lesson.wav"
            transcript = tmp / "transcripts" / "lesson.txt"
            note = tmp / "notes" / "lesson.md"
            audio.write_bytes(b"audio")
            transcript.write_text("old transcript", encoding="utf-8")
            note.write_text("old note", encoding="utf-8")
            state = {"version": 1, "files": {}}
            processing_state.update_record(
                state, audio, "completed", transcript=transcript, note=note
            )
            processing_state.save_state(tmp / "graphs" / "processing_state.json", state)
            archive_result = {
                "available": True,
                "added": 1,
                "skipped": 0,
                "failed": 0,
                "synced_files": ["lesson.md"],
                "failed_files": [],
                "warnings": [],
            }
            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(builtins, "input", return_value="1"),
                mock.patch.object(cli, "transcribe", return_value=transcript) as transcribe,
                mock.patch.object(cli, "summarize", return_value=note) as summarize_note,
                mock.patch.object(
                    cli.openkb_backend, "sync_notes", return_value=archive_result
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli.cmd_reprocess()
            self.assertTrue(result)
            transcribe.assert_called_once_with(audio)
            summarize_note.assert_called_once_with(transcript)

    def test_reprocess_empty_selection_cancels_without_api_work(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "mp3").mkdir()
            (tmp / "mp3" / "lesson.wav").write_bytes(b"audio")
            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(builtins, "input", return_value=""),
                mock.patch.object(cli, "transcribe") as transcribe,
                mock.patch.object(cli.openkb_backend, "sync_notes") as sync,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli.cmd_reprocess()
            self.assertTrue(result)
            transcribe.assert_not_called()
            sync.assert_not_called()

    def test_audio_signature_change_invalidates_completed_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audio = tmp / "lesson.wav"
            audio.write_bytes(b"first")
            state = {"version": 1, "files": {}}
            processing_state.update_record(state, audio, "completed")
            self.assertIsNotNone(processing_state.current_record(state, audio))
            audio.write_bytes(b"different-size")
            os.utime(audio, None)
            self.assertIsNone(processing_state.current_record(state, audio))

    def test_failed_summary_resumes_from_existing_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for directory in ("mp3", "transcripts", "notes", "graphs"):
                (tmp / directory).mkdir()
            audio = tmp / "mp3" / "lesson.wav"
            transcript = tmp / "transcripts" / "lesson.txt"
            note = tmp / "notes" / "lesson.md"
            audio.write_bytes(b"audio")

            def make_transcript(_):
                transcript.write_text("valid transcript", encoding="utf-8")
                return transcript

            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(builtins, "input", return_value=""),
                mock.patch.object(cli, "transcribe", side_effect=make_transcript) as first_transcribe,
                mock.patch.object(cli, "summarize", side_effect=RuntimeError("LLM offline")),
                mock.patch.object(cli.openkb_backend, "sync_notes") as first_sync,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(cli.cmd_convert())
            first_transcribe.assert_called_once()
            first_sync.assert_not_called()

            def make_note(_):
                note.write_text("# Note", encoding="utf-8")
                return note

            archive_result = {
                "available": True,
                "added": 1,
                "skipped": 0,
                "failed": 0,
                "synced_files": ["lesson.md"],
                "failed_files": [],
                "warnings": [],
            }
            with (
                mock.patch.object(cli, "WORK", tmp),
                mock.patch.object(builtins, "input", return_value=""),
                mock.patch.object(cli, "transcribe") as second_transcribe,
                mock.patch.object(cli, "summarize", side_effect=make_note),
                mock.patch.object(
                    cli.openkb_backend, "sync_notes", return_value=archive_result
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(cli.cmd_convert())
            second_transcribe.assert_not_called()
            state = processing_state.load_state(tmp / "graphs" / "processing_state.json")
            self.assertEqual(state["files"]["lesson.wav"]["stage"], "completed")


if __name__ == "__main__":
    unittest.main()
