#!/usr/bin/env python3
"""OpenKB integration used by the OpenClassNotes CLI.

The application keeps credentials in ``~/.class-notes/config.json`` and passes
them to OpenKB in memory.  The generated knowledge base is local to this
project under ``knowledge/`` and is ignored by Git.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import tempfile
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Iterable

import config as app_config


WORK = Path(__file__).resolve().parent.parent
NOTES = WORK / "notes"
KNOWLEDGE = WORK / "knowledge"

_RULES_START = "<!-- OPENCLASSNOTES_RULES_START -->"
_RULES_END = "<!-- OPENCLASSNOTES_RULES_END -->"
_COMPILER_MARKER = "OPENCLASSNOTES_STRICT_GROUNDING_V1"
_PRODUCT_RULES = f"""{_RULES_START}

## OpenClassNotes source-grounding rules

These rules override any conflicting instruction above:

- Treat the supplied class note as the only source of factual content. Never
  add definitions, background, corrections, derivations, examples, or related
  knowledge that the teacher did not state.
- Preserve the source language. A compiled page should use the same language
  as its source note.
- Formulae, equations, transforms, and theorems are represented by their
  spoken names only. Do not reproduce or infer expressions or derivations.
  Preserve a stated use, condition, warning, or exam signal, but do not fill
  in missing details from model knowledge.
- Keep ``[unclear]`` terms unchanged. Do not guess a correction or expansion.
- Prefer high-signal concepts, teacher emphasis, exam signals, and mistakes.
  Do not create pages for concepts merely inferred from general knowledge.
- Keep summaries compact. Do not turn a lightweight review note into a
  textbook chapter.

{_RULES_END}
"""

_STRICT_COMPILER_POLICY = f"""

{_COMPILER_MARKER}
OPENCLASSNOTES STRICT GROUNDING (overrides requests above for a "clear
explanation", "definition", "key details", or "synthesis"):
- Every factual clause, including the description, must be directly entailed
  by words in the supplied source document. General model knowledge is banned.
- Do not define a term unless the source defines it. A title plus one to three
  short source-grounded bullets is a valid and preferred concept page.
- Do not add causes, consequences, mechanisms, examples, comparisons,
  implications, corrections, related facts, or technical vocabulary absent
  from the source.
- Formulae/theorems/transforms: retain the spoken name only, plus only a use,
  condition, warning, or exam signal explicitly present in the source. Never
  infer an expression, derivation, definition, or property.
- Before returning, remove every clause that cannot be traced to the source.
Keep the result compact even when the earlier template asks for full content.
"""

_STRICT_QUERY_POLICY = f"""

{_COMPILER_MARKER}
OPENCLASSNOTES STRICT ANSWERING:
- Report only claims explicitly present in the wiki or its source note.
- Do not use general model knowledge to explain, define, correct, extend, or
  infer. If the requested detail is absent, say that the class note did not
  provide it.
- Keep the answer compact and preserve [unclear] text exactly.
"""


def _openkb_import_error(exc: Exception) -> RuntimeError:
    error = RuntimeError(
        "OpenKB 未安装或加载失败，请先运行 setup.ps1（Windows）或 setup.sh。"
    )
    error.__cause__ = exc
    return error


def _api_settings() -> tuple[str, str, str]:
    cfg = app_config.load_config()
    api = cfg.get("api", {})
    key = str(api.get("api_key", "")).strip()
    base_url = str(api.get("base_url", "")).strip()
    raw_model = str(api.get("model", "")).strip()
    if not key:
        raise RuntimeError("尚未配置 API key，请先使用菜单 5。")
    if not raw_model:
        raise RuntimeError("模型名称为空，请重新配置 API。")

    if "/" in raw_model:
        model = raw_model
    elif "deepseek" in base_url.lower():
        model = f"deepseek/{raw_model}"
    else:
        # LiteLLM uses the openai/ prefix for arbitrary OpenAI-compatible APIs.
        model = f"openai/{raw_model}"
    return key, base_url, model


@contextlib.contextmanager
def _openkb_environment():
    """Expose app credentials only for the duration of an OpenKB call.

    OpenKB 0.4.5 reads credentials from environment variables.  Newer builds
    accept an in-memory bundle.  This compatibility context restores every
    provider key that OpenKB may populate before returning to the menu.
    """
    key, base_url, _ = _api_settings()
    tracked = {
        name
        for name in os.environ
        if name.endswith("_API_KEY") or name in {"LLM_API_KEY", "OPENAI_API_BASE"}
    }
    tracked.update(
        {
            "LLM_API_KEY",
            "OPENAI_API_BASE",
            "OPENAI_AGENTS_DISABLE_TRACING",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
        }
    )
    previous = {name: os.environ.get(name) for name in tracked}
    old_litellm_key = None
    litellm_module = None
    try:
        import litellm

        litellm_module = litellm
        old_litellm_key = getattr(litellm, "api_key", None)
    except Exception:
        pass
    os.environ["LLM_API_KEY"] = key
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    if base_url:
        os.environ["OPENAI_API_BASE"] = base_url
    try:
        yield
    finally:
        for name in tracked | {
            name for name in os.environ if name.endswith("_API_KEY")
        }:
            old = previous.get(name)
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        if litellm_module is not None:
            litellm_module.api_key = old_litellm_key


def _write_product_rules(agents_path: Path) -> None:
    text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    pattern = re.compile(
        re.escape(_RULES_START) + r".*?" + re.escape(_RULES_END) + r"\n?",
        re.DOTALL,
    )
    # Put product constraints first so they frame the generic OpenKB schema,
    # while removing any previous copy left at the end by older releases.
    without_rules = pattern.sub("", text).lstrip()
    updated = _PRODUCT_RULES.rstrip() + "\n\n" + without_rules
    if updated != text:
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        agents_path.write_text(updated, encoding="utf-8")


def _apply_compiler_policy() -> None:
    """Strengthen OpenKB's generic authoring prompts for class-note fidelity."""
    from openkb.agent import compiler

    prompt_names = (
        "_SUMMARY_USER",
        "_CONCEPTS_PLAN_USER",
        "_CONCEPT_PAGE_USER",
        "_CONCEPT_UPDATE_USER",
        "_ENTITY_PAGE_USER",
        "_ENTITY_UPDATE_USER",
        "_SUMMARY_REWRITE_USER",
        "_LONG_DOC_SUMMARY_USER",
    )
    for name in prompt_names:
        prompt = getattr(compiler, name, None)
        if isinstance(prompt, str) and _COMPILER_MARKER not in prompt:
            setattr(compiler, name, prompt + _STRICT_COMPILER_POLICY)


def _apply_query_policy() -> None:
    from openkb.agent import query

    prompt = getattr(query, "_QUERY_INSTRUCTIONS_TEMPLATE", "")
    if isinstance(prompt, str) and _COMPILER_MARKER not in prompt:
        query._QUERY_INSTRUCTIONS_TEMPLATE = prompt + _STRICT_QUERY_POLICY


def ensure_knowledge_base(kb_dir: Path | None = None) -> Path:
    """Initialize OpenKB and keep its model/schema aligned with app settings."""
    kb_dir = (kb_dir or KNOWLEDGE).resolve()
    try:
        from openkb.config import load_config, save_config
        from openkb.locks import atomic_write_json, atomic_write_text
        from openkb.schema import AGENTS_MD, INDEX_SEED
    except Exception as exc:
        raise _openkb_import_error(exc)

    _, _, model = _api_settings()
    if not (kb_dir / ".openkb").exists():
        # Use OpenKB's bundled schema and atomic writers. Credentials
        # deliberately stay in the app config and are never copied here.
        (kb_dir / "raw").mkdir(parents=True, exist_ok=True)
        (kb_dir / "wiki" / "sources" / "images").mkdir(parents=True, exist_ok=True)
        for subdir in ("summaries", "concepts", "entities"):
            (kb_dir / "wiki" / subdir).mkdir(parents=True, exist_ok=True)
        atomic_write_text(kb_dir / "wiki" / "AGENTS.md", AGENTS_MD)
        atomic_write_text(kb_dir / "wiki" / "index.md", INDEX_SEED)
        atomic_write_text(kb_dir / "wiki" / "log.md", "# Operations Log\n\n")
        (kb_dir / ".openkb").mkdir(parents=True)
        save_config(
            kb_dir / ".openkb" / "config.yaml",
            {"model": model, "language": "the source language", "pageindex_threshold": 20},
        )
        atomic_write_json(kb_dir / ".openkb" / "hashes.json", {})

    config_path = kb_dir / ".openkb" / "config.yaml"
    current = load_config(config_path)
    desired = dict(current)
    desired.update(
        {
            "model": model,
            "language": "the source or question language as appropriate",
            "entity_types": [
                "person",
                "organization",
                "course",
                "formula",
                "theorem",
                "method",
                "other",
            ],
        }
    )
    if desired != current:
        save_config(config_path, desired)
    _write_product_rules(kb_dir / "wiki" / "AGENTS.md")
    return kb_dir


def sync_notes(
    note_paths: Iterable[Path] | None = None,
    *,
    kb_dir: Path | None = None,
) -> dict:
    """Incrementally compile Markdown notes into OpenKB.

    Exact content is skipped.  A changed file is added first and only then is
    its old OpenKB document removed, so a failed recompile does not destroy the
    last good knowledge-base version.
    """
    try:
        from openkb.cli import add_single_file
        from openkb.state import HashRegistry
    except Exception as exc:
        raise _openkb_import_error(exc)

    kb_dir = ensure_knowledge_base(kb_dir)
    _apply_compiler_policy()
    paths = sorted(
        (Path(p).resolve() for p in (note_paths if note_paths is not None else NOTES.glob("*.md"))),
        key=lambda p: p.name.lower(),
    )
    added: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    warnings: list[str] = []

    for note in paths:
        if not note.is_file() or note.suffix.lower() != ".md":
            failed.append(note.name)
            continue

        registry = HashRegistry(kb_dir / ".openkb" / "hashes.json")
        new_hash = registry.hash_file(note)
        entries = registry.all_entries()
        if new_hash in entries:
            skipped.append(note.name)
            continue

        old_doc_names = [
            meta.get("doc_name")
            for file_hash, meta in entries.items()
            if file_hash != new_hash
            and meta.get("name") == note.name
            and meta.get("doc_name")
        ]
        add_kwargs = {"stage": True}
        if "bundle" in inspect.signature(add_single_file).parameters:
            from openkb.config import LlmCredentialBundle

            key, base_url, _ = _api_settings()
            add_kwargs["bundle"] = LlmCredentialBundle(
                api_key=key, base_url=base_url or None
            )
        with _openkb_environment():
            result = add_single_file(note, kb_dir, **add_kwargs)
        if result == "failed":
            failed.append(note.name)
            continue
        if result == "skipped":
            skipped.append(note.name)
            continue

        cleanup_ok = True
        for doc_name in old_doc_names:
            removal = _remove_document(kb_dir, doc_name)
            if removal.get("status") not in {"removed", "not_found"}:
                cleanup_ok = False
                warnings.append(f"{note.name}: 旧版本 {doc_name} 清理未完成")
        if cleanup_ok:
            added.append(note.name)
        else:
            failed.append(note.name)

    # OpenKB's concept planner and graph remain native, while page bodies are
    # reduced to exact source-note excerpts. This deterministic final pass is
    # the hard guarantee against textbook-style model elaboration.
    _ground_wiki_pages(kb_dir)

    return {
        "available": True,
        "added": len(added),
        "skipped": len(skipped),
        "failed": len(failed),
        "synced_files": added + skipped,
        "added_files": added,
        "failed_files": failed,
        "warnings": warnings,
        "kb_dir": str(kb_dir),
    }


def _remove_document(kb_dir: Path, doc_name: str) -> dict:
    """Remove one OpenKB document through the best API available."""
    from openkb import cli as openkb_cli

    api_helper = getattr(openkb_cli, "run_remove_for_api", None)
    if api_helper is not None:
        return api_helper(kb_dir, doc_name)

    # OpenKB 0.4.5 exposes removal as a supported Click command only.
    from click.testing import CliRunner

    result = CliRunner().invoke(
        openkb_cli.cli,
        ["--kb-dir", str(kb_dir), "remove", doc_name, "--yes"],
    )
    if result.exit_code != 0:
        message = str(result.exception or result.output).strip()
        return {"status": "failed", "message": message}
    if "No document matching" in result.output:
        return {"status": "not_found", "message": result.output.strip()}
    return {"status": "removed", "message": result.output.strip()}


def _source_text_for_summary(wiki_dir: Path, summary_path: Path) -> str:
    from openkb import frontmatter

    meta = frontmatter.parse(summary_path.read_text(encoding="utf-8"))
    relative = meta.get("full_text")
    if not isinstance(relative, str) or not relative.strip():
        return ""
    wiki_root = wiki_dir.resolve()
    source = (wiki_dir / relative).resolve()
    if not source.is_relative_to(wiki_root) or not source.is_file():
        return ""
    return source.read_text(encoding="utf-8")


_TOKEN_STOPWORDS = {"and", "the", "for", "with", "from"}
_CJK_CONNECTORS = set("与和及的之")


def _semantic_tokens(text: str) -> set[str]:
    """Return conservative English words and Chinese bigrams for matching."""
    lowered = text.lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", lowered)
        if len(token) >= 3 and token not in _TOKEN_STOPWORDS
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(
            run[index : index + 2]
            for index in range(len(run) - 1)
            if not set(run[index : index + 2]) & _CJK_CONNECTORS
        )
    return tokens


def _clean_markdown_line(raw_line: str) -> str:
    line = raw_line.strip()
    line = re.sub(r"^[-*+>]\s+", "", line)
    line = re.sub(r"^\d+[.)]\s*", "", line)
    return line.strip()


def _source_excerpts(title: str, source_text: str) -> list[str]:
    """Return ranked verbatim Markdown lines that support a page title."""
    normalized_title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title.lower()).strip()
    title_tokens = _semantic_tokens(title)
    source_lines: list[tuple[int, str, str, set[str]]] = []
    for index, raw_line in enumerate(source_text.splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("#") or line in {"---"}:
            continue
        cleaned = _clean_markdown_line(line)
        normalized = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+", " ", cleaned.lower()
        ).strip()
        if not normalized:
            continue
        source_lines.append((index, cleaned, normalized, _semantic_tokens(cleaned)))

    frequencies = Counter(
        token for _, _, _, line_tokens in source_lines for token in line_tokens
    )
    candidates: list[tuple[tuple[float, ...], str]] = []
    seen: set[str] = set()
    for index, cleaned, normalized, line_tokens in source_lines:
        exact = bool(normalized_title and normalized_title in normalized)
        matched = title_tokens & line_tokens
        if not exact and not matched:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        rarity = sum(1 / frequencies[token] for token in matched)
        coverage = len(matched) / max(len(title_tokens), 1)
        term_mentions = sum(normalized.count(token) for token in matched)
        is_meta = bool(re.match(r"(?i)^\*{0,2}(?:exam signal|TL;?DR)", cleaned))
        score = (
            float(not is_meta),
            float(exact),
            rarity,
            coverage,
            float(term_mentions),
            float(len(matched)),
            -float(index),
        )
        candidates.append((score, cleaned))

    candidates.sort(key=lambda item: item[0], reverse=True)
    excerpts = [cleaned for _, cleaned in candidates[:8]]
    specific = [
        excerpt
        for excerpt in excerpts
        if not re.match(r"^\*{0,2}TL;DR", excerpt, flags=re.IGNORECASE)
    ]
    return specific or excerpts


def _title_is_grounded(title: str, excerpts: list[str]) -> bool:
    """Require every meaningful title token to occur in the selected evidence."""
    title_tokens = _semantic_tokens(title)
    if not title_tokens:
        return False
    evidence_tokens: set[str] = set()
    for excerpt in excerpts:
        evidence_tokens.update(_semantic_tokens(excerpt))
    return title_tokens <= evidence_tokens


def _order_page_excerpts(title: str, excerpts: list[str]) -> list[str]:
    """Put a definition/use first, except for pages explicitly about variants."""
    variant_page = bool(
        re.search(r"(?i)\b(?:variants?|types?)\b", title)
        or re.search(r"变体|类型|种类", title)
    )
    ranked = []
    for index, excerpt in enumerate(excerpts):
        plain = excerpt.replace("**", "")
        is_exam = bool(re.search(r"(?i)exam signal|考试|考点|必考", plain))
        is_comparison = bool(
            re.search(r"(?i)\bcompare|comparison|batch|stochastic|mini-batch", plain)
            or re.search(r"比较|对比|判断.*还是", plain)
        )
        is_explanatory = bool(
            re.search(r"(?i)\b(?:is|are|means|refers|updates|used for)\b", plain)
            or re.search(r"用于|用途|指的是|表示|说明", plain)
        )
        if variant_page:
            score = (not is_exam, is_comparison, is_explanatory, -index)
        else:
            score = (not is_exam, is_explanatory, not is_comparison, -index)
        ranked.append((score, excerpt))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [excerpt for _, excerpt in ranked]


def _truncate_description(text: str, limit: int = 160) -> str:
    if len(text) <= limit:
        return text
    shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip()
    return (shortened or text[:limit]).rstrip(" ,;:") + "…"


def _markdown_h1(text: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else fallback


def _summary_description(source_text: str, fallback: str) -> str:
    """Pick the first real note sentence instead of a bare TL;DR marker."""
    for raw_line in source_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        cleaned = _clean_markdown_line(line).replace("**", "").strip()
        tldr = re.match(r"(?i)^TL;?DR\s*:?[\s]*(.*)$", cleaned)
        if tldr:
            cleaned = tldr.group(1).strip()
            if not cleaned:
                continue
        if cleaned:
            return _truncate_description(cleaned)
    return fallback


def _summary_sources(wiki_dir: Path, meta: dict) -> list[Path]:
    sources = meta.get("sources")
    if not isinstance(sources, list):
        return []
    result = []
    for marker in sources:
        if not isinstance(marker, str):
            continue
        path = wiki_dir / marker
        if path.is_file() and path.parent.name == "summaries":
            result.append(path)
    return result


def _ground_wiki_pages(kb_dir: Path) -> None:
    """Replace generated prose with verbatim note excerpts and stable links."""
    from openkb import frontmatter

    wiki_dir = kb_dir / "wiki"
    if not wiki_dir.exists():
        return

    page_records: dict[str, list[tuple[str, str, dict]]] = {
        "summaries": [],
        "concepts": [],
        "entities": [],
    }

    # Ground concept and entity pages first; their source lists determine the
    # links appended to each summary page below.
    for subdir in ("concepts", "entities"):
        directory = wiki_dir / subdir
        if not directory.exists():
            continue
        for page in sorted(directory.glob("*.md")):
            text = page.read_text(encoding="utf-8")
            parts = frontmatter.split(text)
            if parts is None:
                continue
            fm_block, body = parts
            meta = frontmatter.parse(text)
            heading = next(
                (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")),
                page.stem.replace("-", " ").title(),
            )
            excerpts: list[str] = []
            seen: set[str] = set()
            supported_sources: list[Path] = []
            for summary in _summary_sources(wiki_dir, meta):
                source_excerpts = _source_excerpts(
                    heading, _source_text_for_summary(wiki_dir, summary)
                )
                if not _title_is_grounded(heading, source_excerpts):
                    continue
                supported_sources.append(summary)
                for excerpt in source_excerpts:
                    key = excerpt.casefold()
                    if key not in seen:
                        seen.add(key)
                        excerpts.append(excerpt)
            excerpts = _order_page_excerpts(heading, excerpts)

            # OpenKB may propose a concept from model knowledge. Keep a page
            # only when its complete meaningful title is supported by exact
            # lines from the compiled class note.
            if not supported_sources:
                page.unlink()
                continue

            description = _truncate_description(excerpts[0])
            source_markers = [
                f"summaries/{source.stem}.md" for source in supported_sources
            ]
            fm_block = frontmatter.set_line(fm_block, "sources", source_markers)
            fm_block = frontmatter.set_line(fm_block, "description", description)
            lines = [f"# {heading}", "", "## Source-grounded excerpts", ""]
            lines.extend(f"- {excerpt}" for excerpt in excerpts)
            lines.extend(["", "## Source notes", ""])
            lines.extend(f"- `{marker}`" for marker in source_markers)
            grounded = fm_block.rstrip() + "\n\n" + "\n".join(lines).rstrip() + "\n"
            page.write_text(grounded, encoding="utf-8")
            meta["sources"] = source_markers
            page_records[subdir].append((page.stem, description, meta))

    summaries_dir = wiki_dir / "summaries"
    if summaries_dir.exists():
        for page in sorted(summaries_dir.glob("*.md")):
            text = page.read_text(encoding="utf-8")
            parts = frontmatter.split(text)
            if parts is None:
                continue
            fm_block, _ = parts
            meta = frontmatter.parse(text)
            source_text = _source_text_for_summary(wiki_dir, page).strip()
            description = _summary_description(source_text, page.stem)
            fm_block = frontmatter.set_line(fm_block, "description", description)
            related = []
            source_marker = f"summaries/{page.stem}.md"
            for subdir in ("concepts", "entities"):
                for slug, _, item_meta in page_records[subdir]:
                    if source_marker in item_meta.get("sources", []):
                        related.append(f"- [[{subdir}/{slug}]]")
            body = source_text
            if related:
                body += "\n\n## OpenKB links\n\n" + "\n".join(related)
            page.write_text(fm_block.rstrip() + "\n\n" + body.rstrip() + "\n", encoding="utf-8")
            page_records["summaries"].append((page.stem, description, meta))

    index_lines = ["# Knowledge Base Index", "", "## Documents"]
    for slug, description, meta in page_records["summaries"]:
        doc_type = meta.get("doc_type", "short")
        index_lines.append(f"- [[summaries/{slug}]] ({doc_type}) — {description}")
    index_lines.extend(["", "## Concepts"])
    for slug, description, _ in page_records["concepts"]:
        index_lines.append(f"- [[concepts/{slug}]] — {description}")
    index_lines.extend(["", "## Entities"])
    for slug, description, meta in page_records["entities"]:
        page_type = meta.get("type", "Entity")
        index_lines.append(f"- [[entities/{slug}]] ({page_type}) — {description}")
    index_lines.extend(["", "## Explorations", ""])
    (wiki_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")


def get_status(*, kb_dir: Path | None = None) -> dict:
    kb_dir = ensure_knowledge_base(kb_dir)
    try:
        from openkb.state import HashRegistry
    except Exception as exc:
        raise _openkb_import_error(exc)
    registry = HashRegistry(kb_dir / ".openkb" / "hashes.json")
    wiki_dir = kb_dir / "wiki"
    directories = {}
    for subdir in ("sources", "summaries", "concepts"):
        path = wiki_dir / subdir
        directories[subdir] = len(list(path.glob("*.md"))) if path.exists() else 0
    status = {"directories": directories, "total_indexed": len(registry.all_entries())}
    entities_dir = kb_dir / "wiki" / "entities"
    status["entities"] = len(list(entities_dir.glob("*.md"))) if entities_dir.exists() else 0
    status["kb_dir"] = str(kb_dir)
    return status


def ask(question: str, *, kb_dir: Path | None = None) -> str:
    """Ask OpenKB a grounded question using the configured model."""
    kb_dir = ensure_knowledge_base(kb_dir)
    status = get_status(kb_dir=kb_dir)
    if status.get("total_indexed", 0) == 0:
        raise RuntimeError("知识库还是空的，请先转换课程或使用菜单 4 同步已有笔记。")
    with _openkb_environment():
        try:
            from openkb.agent.query import run_query
            from openkb.config import load_config
        except Exception as exc:
            raise _openkb_import_error(exc)

        model = load_config(kb_dir / ".openkb" / "config.yaml")["model"]
        _apply_query_policy()
        kwargs = {"stream": False}
        signature = inspect.signature(run_query)
        if "bundle" in signature.parameters:
            from openkb.agent.query import build_run_config_from_bundle
            from openkb.config import LlmCredentialBundle

            key, base_url, _ = _api_settings()
            bundle = LlmCredentialBundle(api_key=key, base_url=base_url or None)
            kwargs["bundle"] = bundle
            kwargs["run_config"] = build_run_config_from_bundle(model, bundle)
        return asyncio.run(run_query(question, kb_dir, model, **kwargs))


def _prepare_visualization_graph(graph: dict, wiki_dir: Path) -> dict:
    """Apply display labels and remove reciprocal provenance edges locally."""
    nodes = graph.get("nodes", [])
    type_by_id = {node.get("id"): node.get("type") for node in nodes}
    wiki_root = wiki_dir.resolve()
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        page = (wiki_dir / f"{node_id}.md").resolve()
        if page.is_relative_to(wiki_root) and page.is_file():
            node["label"] = _markdown_h1(
                page.read_text(encoding="utf-8"), str(node.get("label", node_id))
            )

    raw_pairs = {
        (edge.get("source"), edge.get("target"))
        for edge in graph.get("edges", [])
        if edge.get("source") and edge.get("target")
    }
    edges = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.get("edges", []):
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        pair = (source, target)
        if pair in seen:
            continue
        # Concept/entity -> summary links are provenance backlinks. When the
        # summary already points to the page, rendering both directions makes
        # one relationship look like two separate facts.
        if (
            type_by_id.get(target) == "Summary"
            and type_by_id.get(source) in {"Concept", "Entity"}
            and (target, source) in raw_pairs
        ):
            continue
        seen.add(pair)
        edges.append(edge)
    graph["edges"] = edges

    incoming = Counter(edge["target"] for edge in edges)
    outgoing = Counter(edge["source"] for edge in edges)
    for node in nodes:
        node_id = node.get("id")
        node["in"] = incoming[node_id]
        node["out"] = outgoing[node_id]
    return graph


def render_visualization(
    *, kb_dir: Path | None = None, open_browser: bool = True
) -> dict:
    """Build OpenKB's native self-contained animated graph page."""
    kb_dir = ensure_knowledge_base(kb_dir)
    try:
        from openkb import visualize
    except Exception as exc:
        raise _openkb_import_error(exc)

    graph = visualize.build_graph(kb_dir / "wiki")
    graph = _prepare_visualization_graph(graph, kb_dir / "wiki")
    if not graph["nodes"]:
        return {"opened": False, "path": "", "nodes": 0, "edges": 0}

    output = kb_dir / "output" / "visualize" / "graph.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    html = visualize.render_html(graph)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=output.parent, prefix=".graph.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()

    opened = webbrowser.open(output.as_uri()) if open_browser else False
    return {
        "opened": bool(opened),
        "path": str(output),
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "types": graph.get("types", []),
    }
