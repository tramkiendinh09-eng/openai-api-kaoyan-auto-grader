#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

try:
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "output" / "auto_grading"
DEFAULT_MINERU_DIR = ROOT / "tmp" / "mineru_grading"
DEFAULT_VISUAL_CACHE_DIR = ROOT / "tmp" / "auto_grading_visual_pages"
DEFAULT_VISUAL_FOCUS_DIR = ROOT / "tmp" / "auto_grading_visual_focus"
DEFAULT_POLICY_PATH = ROOT / "config" / "kaoyan_math_grading_policy.md"
DEFAULT_CACHE_DIR = DEFAULT_OUTPUT_DIR / "cache"
DEFAULT_RESULT_CACHE_DIR = DEFAULT_OUTPUT_DIR / "question_result_cache"
QUESTION_RESULT_CACHE_VERSION = "2026-05-23-strict-solution-v3-discrete-scores"
LOG_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
RESULT_CACHE_LOCK = threading.Lock()

PROMPT_CACHE_PRIMER = """
【考研数学阅卷固定协议】
你只做考研数学阅卷，不做普通作业点评。评分必须遵循以下稳定规则：
1. 依据优先级为：本次输入的参考答案/考试分析/评分标准 > 明确标注的推导评分点 > 学生卷面视觉证据 > MinerU OCR 草稿。
2. MinerU OCR 只作为草稿。凡是提供 PDF 页图或局部裁剪图，必须先看视觉证据，定位题号、作答区域、最终答案、涂改和空白，再与 OCR 交叉核对。
3. OCR 与卷面冲突时，以卷面视觉为主；卷面不可辨、页码不覆盖本题、关键符号不清时，设置 needs_human_review=true。
4. 选择题只读取学生最终 A/B/C/D，不解题、不按过程给分；参考答案一致给满分，不一致给 0 分。
5. 填空题按最终结果及数学等价形式判分，不能把印刷横线、题目文本或参考答案当作学生答案。
6. 解答题必须按步骤给分，不能只看最终答案；方法不同但逻辑严谨、数学等价的，应按评分点给分。
7. 大题采用严格阅卷口径：没有可见推导证据的结论不得高分；关键步骤缺失、定理条件未验、边界/端点/定义域/分布条件/矩阵性质未说明时必须扣关键分。
8. 过程错误但结论偶然正确，不能给满分；中间步骤正确但最后计算错，应给相应部分分。
9. 对证明题和多问大题，必须检查逻辑闭合与前后问依赖；前问主线错误会影响后问得分。
10. 未给出官方细则时，只能从本次参考解析推导评分点，并在结果中标明“推导评分点”，不得伪装成官方评分标准。
11. 输出必须是可解析 JSON；分数字段必须是数字或 null；不要输出 Markdown、自然语言前后缀或多余解释。
12. 置信度反映证据质量：卷面清楚且参考答案充分时较高；字迹不清、OCR 缺失、页图覆盖不足或推导评分点较多时降低。
""".strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_optional_text(path: Path | None, limit: int = 12000) -> str:
    if path is None or not path.exists():
        return ""
    return compact_text(read_text(path), limit=limit)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(repair_text_encoding(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(repair_text_encoding(payload), ensure_ascii=False) + "\n")


def guess_pdf_for_markdown(path: Path) -> Path | None:
    candidates = [
        path.with_suffix(".pdf"),
        Path(str(path).replace("\\tmp\\mineru_grading\\", "\\")).with_suffix(".pdf"),
    ]
    filename = path.with_suffix(".pdf").name
    search_roots = [ROOT / "tmp" / "mineru_grading", *extra_search_dirs()]
    for root in search_roots:
        if root.exists():
            candidates.extend(root.rglob(filename))
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate.resolve()
    return None


def extra_search_dirs() -> list[Path]:
    raw = os.getenv("GRADER_EXTRA_SEARCH_DIRS", "")
    if not raw.strip():
        return []
    return [Path(part).expanduser() for part in re.split(r"[;\n]", raw) if part.strip()]


def safe_stem_for_path(path: Path) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._")
    suffix = hashlib.sha1(str(path).encode("utf-8", errors="replace")).hexdigest()[:8]
    if not safe:
        safe = "document"
    return f"{safe[:70]}_{suffix}"


def find_existing_markdown_for_pdf(pdf_path: Path) -> Path | None:
    filename = f"{safe_stem_for_path(pdf_path)}.md"
    legacy_filename = pdf_path.with_suffix(".md").name
    candidates = [
        pdf_path.with_suffix(".md"),
        DEFAULT_MINERU_DIR / filename,
        DEFAULT_MINERU_DIR / legacy_filename,
        ROOT / "output" / "markdown" / filename,
        ROOT / "output" / "markdown" / legacy_filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    search_roots = [DEFAULT_MINERU_DIR, ROOT / "output" / "markdown"]
    for root in search_roots:
        if not root.exists():
            continue
        for name in {filename, legacy_filename}:
            for candidate in root.rglob(name):
                if candidate.exists():
                    return candidate.resolve()
    run_markdown_pattern = f"*{safe_stem_for_path(pdf_path)}.md"
    output_root = ROOT / "output" / "auto_grading"
    if output_root.exists():
        for candidate in output_root.rglob(run_markdown_pattern):
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate.resolve()
    return None


def extract_text_from_mcp_content(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        return "\n".join(parts)
    with contextlib.suppress(Exception):
        payload = result.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False)
    return str(result)


def parse_mineru_tool_response(text: str, pdf_path: Path) -> tuple[str, Path | None]:
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return text, None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return text, None
    contents: list[str] = []
    saved_path: Path | None = None
    for result in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(result, dict) or result.get("status") != "success":
            continue
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            contents.append(content)
        extract_path = result.get("extract_path")
        if isinstance(extract_path, str) and extract_path.strip():
            candidate = Path(extract_path)
            if candidate.exists():
                saved_path = candidate.resolve()
    if saved_path is not None:
        return read_text(saved_path), saved_path
    if contents:
        return "\n\n".join(contents), None
    return text, None


async def run_mineru_mcp_parse_async(pdf_path: Path, role: str, output_dir: Path) -> tuple[str, Path | None]:
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="uvx", args=["mineru-open-mcp"], cwd=str(ROOT))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "parse_documents",
                {
                    "file_sources": [str(pdf_path)],
                    "enable_ocr": True,
                    "language": "ch",
                    "output_dir": str(output_dir),
                },
            )
    raw_text = extract_text_from_mcp_content(result)
    return parse_mineru_tool_response(raw_text, pdf_path)


def try_mineru_mcp_markdown(pdf_path: Path, role: str, run_dir: Path, audit_log: Path) -> Path | None:
    output_dir = run_dir / "mineru_mcp"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        content, saved_path = asyncio.run(run_mineru_mcp_parse_async(pdf_path, role, output_dir))
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            audit_log,
            {
                "event": "mineru_mcp_parse_error",
                "time": utc_now(),
                "role": role,
                "pdf_path": str(pdf_path),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None
    if saved_path is not None and saved_path.exists():
        cache_path = DEFAULT_MINERU_DIR / f"{safe_stem_for_path(pdf_path)}.md"
        with contextlib.suppress(Exception):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(read_text(saved_path), encoding="utf-8")
        append_jsonl(
            audit_log,
            {
                "event": "mineru_mcp_markdown_saved",
                "time": utc_now(),
                "role": role,
                "pdf_path": str(pdf_path),
                "markdown_path": str(saved_path),
                "source": "mineru_mcp_extract_path",
            },
        )
        return saved_path
    if not content.strip():
        append_jsonl(
            audit_log,
            {
                "event": "mineru_mcp_parse_empty",
                "time": utc_now(),
                "role": role,
                "pdf_path": str(pdf_path),
            },
        )
        return None
    output_path = output_dir / f"{role}_{safe_stem_for_path(pdf_path)}.md"
    output_path.write_text(content, encoding="utf-8")
    cache_path = DEFAULT_MINERU_DIR / f"{safe_stem_for_path(pdf_path)}.md"
    with contextlib.suppress(Exception):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(content, encoding="utf-8")
    append_jsonl(
        audit_log,
        {
            "event": "mineru_mcp_markdown_written",
            "time": utc_now(),
            "role": role,
            "pdf_path": str(pdf_path),
            "markdown_path": str(output_path),
            "cache_path": str(cache_path),
            "chars": len(content),
        },
    )
    return output_path


def extract_pdf_text(pdf_path: Path, audit_log: Path) -> tuple[str, str]:
    try:
        import pdfplumber  # type: ignore

        pages: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                pages.append(f"## Page {idx}\n\n{page_text.strip()}")
        text = "\n\n".join(part for part in pages if part.strip())
        append_jsonl(
            audit_log,
            {
                "event": "pdf_text_extract",
                "time": utc_now(),
                "pdf_path": str(pdf_path),
                "engine": "pdfplumber",
                "chars": len(text),
            },
        )
        if text.strip():
            return text, "pdfplumber"
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            audit_log,
            {
                "event": "pdf_text_extract_error",
                "time": utc_now(),
                "pdf_path": str(pdf_path),
                "engine": "pdfplumber",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            pages.append(f"## Page {idx}\n\n{page_text.strip()}")
        text = "\n\n".join(part for part in pages if part.strip())
        append_jsonl(
            audit_log,
            {
                "event": "pdf_text_extract",
                "time": utc_now(),
                "pdf_path": str(pdf_path),
                "engine": "pypdf",
                "chars": len(text),
            },
        )
        return text, "pypdf"
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            audit_log,
            {
                "event": "pdf_text_extract_error",
                "time": utc_now(),
                "pdf_path": str(pdf_path),
                "engine": "pypdf",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    return "", "unavailable"


def markdown_for_pdf(pdf_path: Path, role: str, run_dir: Path, audit_log: Path) -> Path:
    existing = find_existing_markdown_for_pdf(pdf_path)
    if existing is not None:
        append_jsonl(
            audit_log,
            {
                "event": "mineru_markdown_reused",
                "time": utc_now(),
                "role": role,
                "pdf_path": str(pdf_path),
                "markdown_path": str(existing),
            },
        )
        return existing

    mineru_path = try_mineru_mcp_markdown(pdf_path, role=role, run_dir=run_dir, audit_log=audit_log)
    if mineru_path is not None:
        return mineru_path

    text, engine = extract_pdf_text(pdf_path, audit_log)
    internal_dir = run_dir / "internal_markdown"
    internal_dir.mkdir(parents=True, exist_ok=True)
    output_path = internal_dir / f"{role}_{safe_stem_for_path(pdf_path)}.md"
    if text.strip():
        body = (
            f"# Auto Extracted Markdown\n\n"
            f"- role: {role}\n"
            f"- source_pdf: {pdf_path}\n"
            f"- extraction_engine: {engine}\n"
            f"- note: This is a local PDF text fallback. Prefer MinerU Markdown when available; use rendered PDF pages for visual verification.\n\n"
            f"{text.strip()}\n"
        )
    else:
        body = (
            f"# Auto Extracted Markdown\n\n"
            f"- role: {role}\n"
            f"- source_pdf: {pdf_path}\n"
            f"- extraction_engine: unavailable\n"
            f"- note: No reliable text was extracted. The grader must rely on rendered PDF visual evidence and mark uncertain questions for human review.\n"
        )
    output_path.write_text(body, encoding="utf-8")
    append_jsonl(
        audit_log,
        {
            "event": "pdf_text_fallback_markdown_written",
            "time": utc_now(),
            "role": role,
            "pdf_path": str(pdf_path),
            "markdown_path": str(output_path),
            "engine": engine,
            "chars": len(text),
        },
    )
    return output_path


def resolve_markdown_inputs(
    markdown_paths: Iterable[str],
    pdf_paths: Iterable[str],
    role: str,
    run_dir: Path,
    audit_log: Path,
) -> tuple[list[Path], list[Path]]:
    resolved_markdown = [Path(path).resolve() for path in markdown_paths if str(path).strip()]
    resolved_pdfs = [Path(path).resolve() for path in pdf_paths if str(path).strip()]
    for pdf_path in resolved_pdfs:
        resolved_markdown.append(markdown_for_pdf(pdf_path, role=role, run_dir=run_dir, audit_log=audit_log))
    deduped_markdown: list[Path] = []
    seen_markdown: set[str] = set()
    for path in resolved_markdown:
        key = str(path).lower()
        if key not in seen_markdown:
            seen_markdown.add(key)
            deduped_markdown.append(path)
    deduped_pdfs: list[Path] = []
    seen_pdfs: set[str] = set()
    for path in resolved_pdfs:
        key = str(path).lower()
        if key not in seen_pdfs:
            seen_pdfs.add(key)
            deduped_pdfs.append(path)
    return deduped_markdown, deduped_pdfs


def compact_text(text: str, limit: int = 12000) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return head + "\n\n...[TRUNCATED]...\n\n" + tail


def repair_text_encoding(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [repair_text_encoding(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_text_encoding(item) for key, item in value.items()}
    return value


def repair_mojibake(text: str) -> str:
    if not text:
        return text
    markers = ("Ã", "Â", "â", "æ", "ç", "å", "è", "é", "ï¼", "ã", "\x80", "\x81", "\x82", "\x83")
    text = repair_common_latin1_fragments(text)
    if not any(marker in text for marker in markers):
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    if count_cjk(repaired) > count_cjk(text):
        return repaired
    return text


def repair_common_latin1_fragments(text: str) -> str:
    replacements = {
        "ï¼": "：",
        "ï¼": "，",
        "ï¼": "；",
        "ï¼": "（",
        "ï¼": "）",
        "ã": "。",
        "ã": "、",
        "â": "“",
        "â": "”",
        "â": "‘",
        "â": "’",
        "â": "-",
        "â¦": "…",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def count_cjk(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def normalize_question_no(raw: str) -> str:
    return str(int(raw))


def infer_question_type(question_no: str) -> str:
    no = int(question_no)
    if 1 <= no <= 10:
        return "objective"
    if 11 <= no <= 16:
        return "blank"
    return "solution"


def infer_full_score(question_no: str) -> float:
    no = int(question_no)
    if 1 <= no <= 16:
        return 5.0
    if no == 17:
        return 10.0
    return 12.0


def parse_question_filter(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    selected: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            for no in range(int(left), int(right) + 1):
                selected.add(str(no))
        else:
            selected.add(str(int(part)))
    return selected


@dataclass
class AnswerItem:
    question_no: str
    question_type: str
    full_score: float
    text: str
    source_path: str
    ocr_confidence: float
    needs_ocr_review: bool
    ocr_issues: list[str]


@dataclass
class VisualPage:
    source_pdf: str
    page_no: int
    image_path: str
    width: int
    height: int


@dataclass
class AnswerLayout:
    items: dict[str, dict[str, Any]]
    global_notes: str
    source: str


@dataclass
class ReferenceItem:
    question_no: str
    text: str
    source_path: str
    source_type: str
    is_official: bool


@dataclass
class ModelConfig:
    api_url: str
    api_key: str
    model: str
    timeout_seconds: int
    max_retries: int
    temperature: float
    api_mode: str
    use_cache: bool
    cache_dir: str
    max_output_tokens: int
    reasoning_effort: str
    objective_reasoning_effort: str
    blank_reasoning_effort: str
    solution_reasoning_effort: str
    parallel_visual_rounds: bool
    objective_batch_mode: bool
    single_review: bool
    use_stream: bool


class MineruMarkdownParser:
    CHOICE_RANGE_RE = re.compile(
        r"(?im)(?<!\d)(\d{1,2})\s*(?:[-~至]|\\sim)\s*\{?(\d{1,2})\}?\s*[:：]\s*\{?([A-DＡ-Ｄa-d\s]+)\}?"
    )
    QUESTION_HEADING_RE = re.compile(
        r"(?m)^(?:#{1,6}\s*)?(\d{1,2})[\.．、]\s*(.*)$"
    )

    def parse_submission(self, path: Path) -> list[AnswerItem]:
        text = read_text(path)
        items: dict[str, AnswerItem] = {}

        for match in self.CHOICE_RANGE_RE.finditer(text):
            match_line = line_at_offset(text, match.start())
            if match_line and not is_plausible_objective_answer_line(match_line):
                continue
            start = int(match.group(1))
            end = int(match.group(2))
            letters = re.sub(r"[^A-Da-d]", "", match.group(3)).upper()
            if len(letters) < end - start + 1:
                continue
            for offset, no in enumerate(range(start, end + 1)):
                question_no = str(no)
                answer = letters[offset]
                items[question_no] = AnswerItem(
                    question_no=question_no,
                    question_type=infer_question_type(question_no),
                    full_score=infer_full_score(question_no),
                    text=f"OCR objective answer: {answer}",
                    source_path=str(path),
                    ocr_confidence=0.92,
                    needs_ocr_review=False,
                    ocr_issues=[],
                )

        for question_no, block in split_numbered_blocks(text).items():
            cleaned = compact_text(block, limit=9000)
            if not cleaned:
                continue
            confidence, issues = estimate_ocr_quality(cleaned)
            items[question_no] = AnswerItem(
                question_no=question_no,
                question_type=infer_question_type(question_no),
                full_score=infer_full_score(question_no),
                text=cleaned,
                source_path=str(path),
                ocr_confidence=confidence,
                needs_ocr_review=confidence < 0.68 or bool(issues),
                ocr_issues=issues,
            )

        return sorted(items.values(), key=lambda item: int(item.question_no))


def split_numbered_blocks(text: str) -> dict[str, str]:
    matches = list(MineruMarkdownParser.QUESTION_HEADING_RE.finditer(text))
    blocks: dict[str, str] = {}
    for idx, match in enumerate(matches):
        no = normalize_question_no(match.group(1))
        if not (1 <= int(no) <= 30):
            continue
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if len(block) < 8:
            continue
        existing = blocks.get(no)
        blocks[no] = (existing + "\n\n" + block).strip() if existing else block
    return blocks


def line_at_offset(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, max(0, offset)) + 1
    end = text.find("\n", offset)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def render_pdf_pages(pdf_paths: list[Path], run_dir: Path, audit_log: Path, dpi: int = 130) -> list[VisualPage]:
    pages: list[VisualPage] = []
    run_image_dir = run_dir / "visual_pages"
    run_image_dir.mkdir(parents=True, exist_ok=True)
    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            continue
        safe_stem = safe_stem_for_path(pdf_path)
        image_dir = DEFAULT_VISUAL_CACHE_DIR / safe_stem / f"dpi_{dpi}"
        image_dir.mkdir(parents=True, exist_ok=True)
        image_paths = sorted(image_dir.glob("page-*.png"))
        if image_paths:
            append_jsonl(
                audit_log,
                {
                    "event": "pdf_render_reused",
                    "time": utc_now(),
                    "pdf_path": str(pdf_path),
                    "image_dir": str(image_dir),
                    "page_count": len(image_paths),
                    "dpi": dpi,
                },
            )
        else:
            prefix = image_dir / "page"
            cmd = [
                "pdftoppm",
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(prefix),
            ]
            try:
                import subprocess

                completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
                append_jsonl(
                    audit_log,
                    {
                        "event": "pdf_render",
                        "time": utc_now(),
                        "pdf_path": str(pdf_path),
                        "image_dir": str(image_dir),
                        "returncode": completed.returncode,
                        "stderr": compact_text(completed.stderr or "", limit=1000),
                    },
                )
                if completed.returncode != 0:
                    continue
            except Exception as exc:  # noqa: BLE001
                append_jsonl(
                    audit_log,
                    {
                        "event": "pdf_render_error",
                        "time": utc_now(),
                        "pdf_path": str(pdf_path),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue
            image_paths = sorted(image_dir.glob("page-*.png"))
        for image_path in image_paths:
            page_no_match = re.search(r"-(\d+)\.png$", image_path.name)
            page_no = int(page_no_match.group(1)) if page_no_match else len(pages) + 1
            width = 0
            height = 0
            if Image is not None:
                try:
                    with Image.open(image_path) as image:
                        width, height = image.size
                except Exception:  # noqa: BLE001
                    pass
            pages.append(
                VisualPage(
                    source_pdf=str(pdf_path),
                    page_no=page_no,
                    image_path=str(image_path),
                    width=width,
                    height=height,
                )
            )
            with contextlib.suppress(Exception):
                run_copy = run_image_dir / f"{safe_stem}-{page_no}.png"
                if not run_copy.exists():
                    run_copy.write_bytes(image_path.read_bytes())
    return sorted(pages, key=lambda page: (page.source_pdf, page.page_no))


def select_visual_pages(item: AnswerItem, pages: list[VisualPage], max_pages: int | None = None) -> list[VisualPage]:
    if not pages:
        return []
    page_count = len(pages)
    question_no = int(item.question_no)
    if max_pages is None:
        if item.question_type == "objective":
            max_pages = 1
        elif item.question_type == "blank":
            max_pages = 2
        else:
            max_pages = 3
    if page_count <= max_pages:
        return pages[:max_pages]
    if question_no <= 10:
        center = 1
    elif question_no <= 16:
        if item.question_type == "blank":
            center = 2 if page_count >= 2 else 1
        else:
            center = min(page_count, max(2, round(page_count * 0.45)))
    else:
        solution_start = 3 if page_count >= 3 else 2
        solution_span = max(1, page_count - solution_start + 1)
        offset = max(0, question_no - 17)
        center = min(page_count, solution_start + round(offset * solution_span / 5))
    start = max(1, center - max_pages // 2)
    end = min(page_count, start + max_pages - 1)
    start = max(1, end - max_pages + 1)
    selected = [page for page in pages if start <= page.page_no <= end]
    return selected[:max_pages]


def select_visual_pages_from_layout(
    item: AnswerItem,
    pages: list[VisualPage],
    layout: AnswerLayout | None,
    max_pages: int | None = None,
) -> list[VisualPage]:
    if layout is None or not pages:
        return select_visual_pages(item, pages, max_pages=max_pages)
    entry = layout.items.get(item.question_no)
    if not isinstance(entry, dict):
        return select_visual_pages(item, pages, max_pages=max_pages)
    page_numbers = normalized_layout_page_numbers(entry)
    selected = [page for page in pages if page.page_no in page_numbers]
    if selected:
        if max_pages is not None:
            selected = selected[:max_pages]
        return selected
    return select_visual_pages(item, pages, max_pages=max_pages)


def normalized_layout_page_numbers(entry: dict[str, Any]) -> list[int]:
    raw_values: list[Any] = []
    for key in ("page_numbers", "pages", "page_no"):
        value = entry.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value is not None:
            raw_values.append(value)
    page_numbers: list[int] = []
    for value in raw_values:
        try:
            page_no = int(value)
        except (TypeError, ValueError):
            continue
        if page_no > 0 and page_no not in page_numbers:
            page_numbers.append(page_no)
    return page_numbers


def image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def pdf_data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:application/pdf;base64,{data}"


def visual_focus_pages_for_item(
    item: AnswerItem,
    selected_pages: list[VisualPage],
    layout: AnswerLayout | None = None,
) -> list[dict[str, Any]]:
    if Image is None or not selected_pages:
        return []
    try:
        question_no = int(item.question_no)
    except ValueError:
        return []
    if item.question_type == "objective":
        return []
    focus_specs = focus_box_specs_for_item(item, layout)
    if not focus_specs:
        return []
    focus_pages: list[dict[str, Any]] = []
    for idx, spec in enumerate(focus_specs, start=1):
        preferred_page_no = spec.get("page_no")
        page = None
        if preferred_page_no is not None:
            page = next((candidate for candidate in selected_pages if candidate.page_no == preferred_page_no), None)
        if page is None:
            page = selected_pages[0]
        page_path = Path(page.image_path)
        if not page_path.exists():
            continue
        box_ratio = spec.get("box")
        if not isinstance(box_ratio, tuple) or len(box_ratio) != 4:
            continue
        try:
            with Image.open(page_path) as image:
                width, height = image.size
                left = int(width * box_ratio[0])
                top = int(height * box_ratio[1])
                right = int(width * box_ratio[2])
                bottom = int(height * box_ratio[3])
                crop = image.crop((max(0, left), max(0, top), min(width, right), min(height, bottom)))
                scale = 2 if max(crop.size) < 1800 else 1
                if scale > 1:
                    crop = crop.resize((crop.width * scale, crop.height * scale))
                out_dir = DEFAULT_VISUAL_FOCUS_DIR / safe_stem_for_path(Path(page.source_pdf))
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"q{item.question_no}_p{page.page_no}_{idx}.png"
                crop.save(out_path)
                focus_pages.append(
                    {
                        "source_pdf": page.source_pdf,
                        "page_no": page.page_no,
                        "image_path": str(out_path),
                        "width": crop.width,
                        "height": crop.height,
                        "focus_question_no": item.question_no,
                        "focus_role": str(spec.get("role") or "answer_area_crop"),
                        "layout_source": str(spec.get("layout_source") or ""),
                        "layout_confidence": spec.get("layout_confidence"),
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    return focus_pages


def focus_box_specs_for_item(item: AnswerItem, layout: AnswerLayout | None = None) -> list[dict[str, Any]]:
    layout_specs = focus_box_specs_from_layout(item, layout)
    if layout_specs:
        return layout_specs
    return focus_box_specs_for_question(int(item.question_no), item.question_type)


def focus_box_specs_from_layout(item: AnswerItem, layout: AnswerLayout | None) -> list[dict[str, Any]]:
    if layout is None:
        return []
    entry = layout.items.get(item.question_no)
    if not isinstance(entry, dict):
        return []
    raw_boxes = entry.get("answer_boxes")
    if not isinstance(raw_boxes, list):
        return []
    confidence = layout_entry_confidence(entry)
    specs: list[dict[str, Any]] = []
    for idx, raw_box in enumerate(raw_boxes[:3], start=1):
        if not isinstance(raw_box, dict):
            continue
        box = normalize_ratio_box(raw_box.get("box"))
        if box is None:
            box = normalize_ratio_box([raw_box.get("x1"), raw_box.get("y1"), raw_box.get("x2"), raw_box.get("y2")])
        if box is None:
            continue
        page_no = raw_box.get("page_no") or entry.get("page_no")
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            page_numbers = normalized_layout_page_numbers(entry)
            page_no = page_numbers[0] if page_numbers else None
        specs.append(
            {
                "page_no": page_no,
                "box": box,
                "role": "layout_scan_answer_area_crop" if idx == 1 else "layout_scan_continued_answer_crop",
                "layout_source": layout.source,
                "layout_confidence": confidence,
            }
        )
    return specs


def normalize_ratio_box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if max(abs(left), abs(top), abs(right), abs(bottom)) > 1.5:
        left, top, right, bottom = left / 1000, top / 1000, right / 1000, bottom / 1000
    left, right = sorted((max(0.0, min(1.0, left)), max(0.0, min(1.0, right))))
    top, bottom = sorted((max(0.0, min(1.0, top)), max(0.0, min(1.0, bottom))))
    if right - left < 0.03 or bottom - top < 0.02:
        return None
    pad_x = 0.015
    pad_y = 0.012
    return (
        max(0.0, left - pad_x),
        max(0.0, top - pad_y),
        min(1.0, right + pad_x),
        min(1.0, bottom + pad_y),
    )


def layout_entry_confidence(entry: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(entry.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def focus_box_specs_for_question(question_no: int, question_type: str) -> list[dict[str, Any]]:
    if 11 <= question_no <= 16:
        bands = {
            11: (0.63, 0.70),
            12: (0.68, 0.75),
            13: (0.64, 0.72),
            14: (0.76, 0.83),
            15: (0.80, 0.88),
            16: (0.85, 0.93),
        }
        top, bottom = bands[question_no]
        return [{"page_no": 2, "box": (0.02, top, 0.98, bottom), "role": "blank_answer_area_crop"}]
    if question_type == "solution":
        solution_boxes = {
            17: [
                {"page_no": 4, "box": (0.02, 0.02, 0.98, 0.34), "role": "solution_work_crop"},
            ],
            18: [
                {"page_no": 4, "box": (0.02, 0.30, 0.98, 0.78), "role": "solution_work_crop"},
            ],
            19: [
                {"page_no": 4, "box": (0.02, 0.70, 0.98, 0.98), "role": "solution_work_crop"},
                {"page_no": 5, "box": (0.02, 0.00, 0.98, 0.34), "role": "continued_solution_work_crop"},
            ],
            20: [
                {"page_no": 5, "box": (0.02, 0.22, 0.98, 0.94), "role": "solution_work_crop"},
            ],
            21: [
                {"page_no": 6, "box": (0.02, 0.02, 0.98, 0.54), "role": "solution_work_crop"},
            ],
            22: [
                {"page_no": 6, "box": (0.02, 0.48, 0.98, 0.98), "role": "solution_work_crop"},
                {"page_no": 7, "box": (0.02, 0.02, 0.98, 0.34), "role": "continued_solution_work_crop"},
            ],
        }
        return solution_boxes.get(question_no, [])
    return []


def visual_payload_for_item(
    item: AnswerItem,
    pages: list[VisualPage],
    layout: AnswerLayout | None = None,
) -> dict[str, Any]:
    selected = select_visual_pages_from_layout(item, pages, layout)
    payload_pages = [asdict(page) for page in selected]
    focus_pages = visual_focus_pages_for_item(item, selected, layout=layout)
    layout_entry = layout.items.get(item.question_no) if layout else None
    used_layout = isinstance(layout_entry, dict) and bool(normalized_layout_page_numbers(layout_entry) or layout_entry.get("answer_boxes"))
    return {
        "enabled": bool(selected),
        "selection_reason": (
            "layout_scan_guided_pdf_vision"
            if used_layout
            else "pdf_vision_required_for_grading"
            if selected
            else "not_required_or_unavailable"
        ),
        "focus_pages": focus_pages,
        "pages": payload_pages,
        "stable_pages": stable_visual_pages(payload_pages),
        "stable_focus_pages": stable_visual_pages(focus_pages),
        "pdf_sources": sorted({page.source_pdf for page in selected}),
        "attach_pdf_file": False,
        "focus_only": bool(focus_pages),
        "layout_scan": stable_layout_entry(item.question_no, layout_entry) if isinstance(layout_entry, dict) else None,
    }


def focused_visual_payload(visual_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not visual_payload or not visual_payload.get("enabled"):
        return visual_payload
    focus_pages = list(visual_payload.get("focus_pages") or [])
    if not focus_pages:
        return visual_payload
    pdf_sources = sorted({str(page.get("source_pdf") or "") for page in focus_pages if page.get("source_pdf")})
    return {
        "enabled": True,
        "selection_reason": "focused_retry_visual_evidence",
        "focus_pages": focus_pages,
        "pages": [],
        "stable_pages": [],
        "stable_focus_pages": stable_visual_pages(focus_pages),
        "pdf_sources": pdf_sources,
        "attach_pdf_file": False,
        "layout_scan": visual_payload.get("layout_scan"),
    }


def stable_layout_entry(question_no: str, entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    boxes = []
    for raw_box in ensure_list_of_dicts(entry.get("answer_boxes"))[:3]:
        box = normalize_ratio_box(raw_box.get("box"))
        if box is None:
            box = normalize_ratio_box([raw_box.get("x1"), raw_box.get("y1"), raw_box.get("x2"), raw_box.get("y2")])
        if box is None:
            continue
        boxes.append(
            {
                "page_no": raw_box.get("page_no") or entry.get("page_no"),
                "box": [round(value, 4) for value in box],
            }
        )
    return {
        "question_no": question_no,
        "page_numbers": normalized_layout_page_numbers(entry),
        "answer_order_index": entry.get("answer_order_index"),
        "confidence": layout_entry_confidence(entry),
        "is_out_of_order": bool(entry.get("is_out_of_order")),
        "answer_boxes": boxes,
        "notes": truncate_str(str(entry.get("notes") or ""), 180),
    }


def ensure_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def visual_payload_for_objective_batch(items: list[AnswerItem], pages: list[VisualPage]) -> dict[str, Any]:
    if not pages:
        return {"enabled": False, "selection_reason": "not_required_or_unavailable", "pages": []}
    selected = [page for page in pages if page.page_no in {1, 2}]
    if not selected:
        selected = pages[:2]
    payload_pages = [asdict(page) for page in selected[:2]]
    return {
        "enabled": bool(selected),
        "selection_reason": "objective_batch_choice_reading",
        "pages": payload_pages,
        "stable_pages": stable_visual_pages(payload_pages),
        "pdf_sources": sorted({page.source_pdf for page in selected[:2]}),
        "attach_pdf_file": False,
    }


def visual_payload_for_layout_scan(pages: list[VisualPage]) -> dict[str, Any]:
    if not pages:
        return {"enabled": False, "selection_reason": "not_required_or_unavailable", "pages": []}
    payload_pages = [asdict(page) for page in pages]
    return {
        "enabled": True,
        "selection_reason": "whole_submission_answer_layout_scan",
        "pages": payload_pages,
        "focus_pages": [],
        "stable_pages": stable_visual_pages(payload_pages),
        "stable_focus_pages": [],
        "pdf_sources": sorted({page.source_pdf for page in pages}),
        "attach_pdf_file": False,
    }


def estimate_ocr_quality(text: str) -> tuple[float, list[str]]:
    issues: list[str] = []
    weird_tokens = len(re.findall(r"[�]|myg|均证|中任念|格轴|海米|\\frac\s*\{?\s*\d\s+\d", text))
    split_digits = len(re.findall(r"\d\s+\d", text))
    image_count = text.count("![](")
    length = max(len(text), 1)
    penalty = min(0.42, weird_tokens * 0.06 + split_digits * 0.01 + image_count * 0.04)
    confidence = max(0.2, round(0.9 - penalty, 2))
    if weird_tokens:
        issues.append("OCR contains suspicious math/text artifacts")
    if split_digits >= 6:
        issues.append("OCR may have split multi-digit numbers")
    if image_count:
        issues.append("Answer contains image-only regions")
    if length < 20:
        issues.append("Answer text is very short")
    return confidence, issues


class ReferenceBank:
    def __init__(self, references: list[ReferenceItem], fallback_text: str = "") -> None:
        self.references = references
        self.fallback_text = fallback_text
        self.by_question: dict[str, list[ReferenceItem]] = {}
        for ref in references:
            self.by_question.setdefault(ref.question_no, []).append(ref)

    @classmethod
    def from_paths(cls, paths: Iterable[Path], source_type: str, is_official: bool) -> "ReferenceBank":
        references: list[ReferenceItem] = []
        fallback_parts: list[str] = []
        for path in paths:
            text = read_text(path)
            fallback_parts.append(f"# Source: {path}\n\n{compact_text(text, limit=20000)}")
            references.extend(parse_answer_key_references(text, path, source_type, is_official))
            blocks = split_numbered_blocks(text)
            for question_no, block in blocks.items():
                references.append(
                    ReferenceItem(
                        question_no=question_no,
                        text=compact_text(block, limit=12000),
                        source_path=str(path),
                        source_type=source_type,
                        is_official=is_official,
                    )
                )
        return cls(references, fallback_text="\n\n".join(fallback_parts))

    def get(self, question_no: str, limit: int = 16000) -> dict[str, Any]:
        refs = self.by_question.get(question_no, [])
        if refs:
            source_types = sorted({ref.source_type for ref in refs})
            return {
                "available": True,
                "is_official": all(ref.is_official for ref in refs),
                "source_types": source_types,
                "sources": [
                    {
                        "source_path": ref.source_path,
                        "source_type": ref.source_type,
                        "is_official": ref.is_official,
                    }
                    for ref in refs
                ],
                "reference_text": compact_text("\n\n".join(ref.text for ref in refs), limit=limit),
            }
        return {
            "available": False,
            "is_official": False,
            "source_types": [],
            "sources": [],
            "reference_text": "",
        }


def parse_answer_key_references(
    text: str,
    path: Path,
    source_type: str,
    is_official: bool,
) -> list[ReferenceItem]:
    references: list[ReferenceItem] = []
    references.extend(parse_html_choice_answer_table(text, path, source_type, is_official))
    for match in MineruMarkdownParser.CHOICE_RANGE_RE.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2))
        letters = re.sub(r"[^A-Da-d]", "", match.group(3)).upper()
        if len(letters) < end - start + 1:
            continue
        for offset, no in enumerate(range(start, end + 1)):
            references.append(
                ReferenceItem(
                    question_no=str(no),
                    text=f"{no}. 参考答案：{letters[offset]}",
                    source_path=str(path),
                    source_type=f"{source_type}:objective_answer_key",
                    is_official=is_official,
                )
            )

    blank_section = extract_between(text, "二", "三")
    if blank_section:
        starts = list(re.finditer(r"(?<!\d)(1[1-6])\s*[\.．、]\s*", blank_section))
        for idx, match in enumerate(starts):
            no = match.group(1)
            start = match.end()
            end = starts[idx + 1].start() if idx + 1 < len(starts) else len(blank_section)
            answer_text = blank_section[start:end].strip()
            answer_text = re.sub(r"\s+", " ", answer_text).strip(" ，,;；")
            if answer_text:
                references.append(
                    ReferenceItem(
                        question_no=no,
                        text=f"{no}. 参考答案：{answer_text}",
                        source_path=str(path),
                        source_type=f"{source_type}:blank_answer_key",
                        is_official=is_official,
                    )
                )
    return references


def parse_html_choice_answer_table(
    text: str,
    path: Path,
    source_type: str,
    is_official: bool,
) -> list[ReferenceItem]:
    references: list[ReferenceItem] = []
    for table_match in re.finditer(r"<table.*?</table>", text, flags=re.I | re.S):
        rows = re.findall(r"<tr.*?>(.*?)</tr>", table_match.group(0), flags=re.I | re.S)
        if len(rows) < 2:
            continue
        number_cells = re.findall(r"<td.*?>(.*?)</td>", rows[0], flags=re.I | re.S)
        answer_cells = re.findall(r"<td.*?>(.*?)</td>", rows[1], flags=re.I | re.S)
        numbers = [re.sub(r"\D", "", cell) for cell in number_cells]
        answers = [normalize_choice_letter(re.sub(r"[^A-DＡ-Ｄa-d]", "", cell)) for cell in answer_cells]
        if not numbers or len(numbers) != len(answers):
            continue
        if not all(no.isdigit() and 1 <= int(no) <= 10 for no in numbers):
            continue
        if not all(answer in {"A", "B", "C", "D"} for answer in answers):
            continue
        for no, answer in zip(numbers, answers):
            references.append(
                ReferenceItem(
                    question_no=str(int(no)),
                    text=f"{int(no)}. 参考答案：{answer}",
                    source_path=str(path),
                    source_type=f"{source_type}:objective_answer_key_table",
                    is_official=is_official,
                )
            )
    return references


def extract_between(text: str, start_marker: str, end_marker: str) -> str:
    start_match = re.search(rf"(?m)^[#\s]*{re.escape(start_marker)}[、.．，,：:\s]", text)
    if not start_match:
        return ""
    end_match = re.search(rf"(?m)^[#\s]*{re.escape(end_marker)}[、.．，,：:\s]", text[start_match.end() :])
    start = start_match.end()
    end = start + end_match.start() if end_match else len(text)
    return text[start:end]


def rubric_for(item: AnswerItem, reference: dict[str, Any], strict_official: bool) -> dict[str, Any]:
    source_label = "official" if reference.get("is_official") else "provided_or_inferred"
    if item.question_type == "objective":
        points = [
            {
                "point_id": f"{item.question_no}-final-answer",
                "score": item.full_score,
                "description": "Objective question: award full credit only if the final option matches the reference answer.",
                "source_type": source_label,
            }
        ]
    elif item.question_type == "blank":
        points = [
            {
                "point_id": f"{item.question_no}-equivalent-result",
                "score": item.full_score,
                "description": "Blank question: award full credit only when the result is mathematically equivalent to the reference answer.",
                "source_type": source_label,
            }
        ]
    else:
        points = [
            {
                "point_id": f"{item.question_no}-step-rubric",
                "score": item.full_score,
                "description": (
                    "Solution question: infer strict stepwise scoring points from the reference solution. "
                    "Mark every inferred point as inferred. Award high scores only when visible student work supports the core reasoning chain."
                ),
                "source_type": "inferred_from_reference" if not reference.get("is_official") else "inferred_from_official_analysis",
                "must_label": "推导评分点",
                "strict_solution_caps": {
                    "final_answer_only": "cap_at_30_percent_unless official rubric explicitly gives more",
                    "fragmentary_formula_pile": "cap_at_50_percent_unless it establishes the core chain",
                    "one_core_scoring_point_missing": "cap_below_80_percent",
                    "any_core_scoring_point_wrong": "cap_below_70_percent unless later work is independently correct",
                    "correct_main_idea_missing_key_conditions": "usually cap_at_70_to_80_percent",
                    "wrong_setup_or_wrong_model": "do_not_award_downstream_computation_as_if_setup_were_correct",
                    "proof_without_logical_closure": "cannot_receive_full_credit",
                },
            }
        ]

    return {
        "question_no": item.question_no,
        "question_type": item.question_type,
        "full_score": item.full_score,
        "strict_official_required": strict_official,
        "reference_available": reference.get("available", False),
        "reference_is_official": reference.get("is_official", False),
        "rubric_items": points,
        "policy_notes": [
            "Do not invent official scoring rules.",
            "If a step score is inferred from a solution, label it as 推导评分点.",
            "For solution questions, do not award full credit for a correct final answer with invalid or missing reasoning.",
            "For solution questions, apply a strict Beijing-style grading scale as a non-official strict rubric: high scores require visible key steps, condition checks, and logical closure.",
            "If the student skips a key derivation, boundary/domain/condition check, proof of existence/uniqueness, distribution derivation, likelihood construction, or matrix-property justification, cap the score and name the missing point.",
            "Do point-by-point scoring before choosing a numeric score; do not grade by overall impression.",
            "For unclear handwriting/OCR, reduce confidence and set needs_human_review when needed.",
        ],
    }


class ModelGateway:
    def __init__(self, config: ModelConfig, audit_log: Path) -> None:
        self.config = config
        self.audit_log = audit_log

    @property
    def endpoint(self) -> str:
        api_url = self.config.api_url.rstrip("/")
        if self.config.api_mode == "responses":
            if api_url.endswith("/responses"):
                return api_url
            if api_url.endswith("/v1"):
                return api_url + "/responses"
            return api_url + "/v1/responses"
        if api_url.endswith("/chat/completions"):
            return api_url
        if api_url.endswith("/v1"):
            return api_url + "/chat/completions"
        return api_url + "/v1/chat/completions"

    def cache_key(self, body: dict[str, Any]) -> str:
        cache_body = json.loads(json.dumps(body, ensure_ascii=False))
        if isinstance(cache_body, dict):
            cache_body.pop("stream", None)
        payload = {
            "api_mode": self.config.api_mode,
            "endpoint": self.endpoint,
            "model": self.config.model,
            "temperature": self.config.temperature,
            "reasoning_effort": self.config.reasoning_effort,
            "body": cache_body,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def prompt_cache_key_for_body(self, body: dict[str, Any], call_name: str) -> str:
        seed = {
            "model": self.config.model,
            "api_mode": self.config.api_mode,
            "reasoning_effort": self.config.reasoning_effort,
            "call_group": re.sub(r"q\d+", "q", call_name),
        }
        digest = hashlib.sha1(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        return f"kaoyan-grader-{digest}"

    def for_reasoning_effort(self, reasoning_effort: str) -> "ModelGateway":
        reasoning_effort = (reasoning_effort or "").strip()
        if reasoning_effort == self.config.reasoning_effort:
            return self
        return ModelGateway(
            ModelConfig(
                api_url=self.config.api_url,
                api_key=self.config.api_key,
                model=self.config.model,
                timeout_seconds=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                temperature=self.config.temperature,
                api_mode=self.config.api_mode,
                use_cache=self.config.use_cache,
                cache_dir=self.config.cache_dir,
                max_output_tokens=self.config.max_output_tokens,
                reasoning_effort=reasoning_effort,
                objective_reasoning_effort=self.config.objective_reasoning_effort,
                blank_reasoning_effort=self.config.blank_reasoning_effort,
                solution_reasoning_effort=self.config.solution_reasoning_effort,
                parallel_visual_rounds=self.config.parallel_visual_rounds,
                objective_batch_mode=self.config.objective_batch_mode,
                single_review=self.config.single_review,
                use_stream=self.config.use_stream,
            ),
            self.audit_log,
        )

    def cache_path(self, cache_key: str) -> Path:
        return Path(self.config.cache_dir) / f"{cache_key}.json"

    def read_cache(self, cache_key: str, call_name: str) -> dict[str, Any] | None:
        if not self.config.use_cache:
            return None
        path = self.cache_path(cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            append_jsonl(
                self.audit_log,
                {
                    "event": "model_cache_hit",
                    "time": utc_now(),
                    "call_name": call_name,
                    "cache_key": cache_key,
                    "cache_path": str(path),
                    "api_mode": self.config.api_mode,
                    "model": self.config.model,
                },
            )
            return payload.get("parsed_json") if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001
            append_jsonl(
                self.audit_log,
                {
                    "event": "model_cache_read_error",
                    "time": utc_now(),
                    "call_name": call_name,
                    "cache_key": cache_key,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return None

    def write_cache(self, cache_key: str, call_name: str, body: dict[str, Any], parsed_json: dict[str, Any], raw_payload: Any) -> None:
        if not self.config.use_cache:
            return
        path = self.cache_path(cache_key)
        record = {
            "created_at": utc_now(),
            "cache_key": cache_key,
            "api_mode": self.config.api_mode,
            "endpoint": self.endpoint,
            "model": self.config.model,
            "call_name": call_name,
            "request": scrub_request(body),
            "parsed_json": parsed_json,
            "raw_response_preview": compact_text(json.dumps(raw_payload, ensure_ascii=False), limit=12000),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with CACHE_LOCK:
            tmp_path.write_text(json.dumps(repair_text_encoding(record), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)

    def build_body(
        self,
        messages: list[dict[str, Any]],
        json_mode: bool,
        call_name: str = "",
        include_temperature: bool = True,
        include_max_output: bool = True,
        include_reasoning: bool = True,
        include_images: bool = True,
        include_files: bool = True,
    ) -> dict[str, Any]:
        prepared_messages = prepare_messages_for_transport(
            messages,
            include_images=include_images,
            include_files=include_files and self.config.api_mode == "responses",
        )
        if self.config.api_mode == "responses":
            system_text = "\n\n".join(
                stringify_message_content(message.get("content"))
                for message in prepared_messages
                if message.get("role") == "system"
            )
            response_input = []
            for message in prepared_messages:
                if message.get("role") == "system":
                    continue
                response_input.append(
                    {
                        "role": "assistant" if message.get("role") == "assistant" else "user",
                        "content": response_content_parts(message.get("content")),
                    }
                )
            if system_text:
                response_input.insert(0, {"role": "user", "content": [{"type": "input_text", "text": "SYSTEM:\n" + system_text}]})
            body: dict[str, Any] = {
                "model": self.config.model,
                "input": response_input,
            }
            if include_max_output and self.config.max_output_tokens > 0:
                body["max_output_tokens"] = self.config.max_output_tokens
            if include_temperature:
                body["temperature"] = self.config.temperature
            if include_reasoning and self.config.reasoning_effort:
                body["reasoning"] = {"effort": self.config.reasoning_effort}
            if json_mode:
                body["text"] = {"format": {"type": "json_object"}}
            body["prompt_cache_key"] = self.prompt_cache_key_for_body(body, call_name or "responses")
            body["prompt_cache_retention"] = "24h"
            if self.config.use_stream:
                body["stream"] = True
            return body
        body = {
            "model": self.config.model,
            "messages": prepared_messages,
        }
        if include_temperature:
            body["temperature"] = self.config.temperature
        if include_reasoning and self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        if include_max_output and self.config.max_output_tokens > 0:
            body["max_tokens"] = self.config.max_output_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.config.use_stream:
            body["stream"] = True
        return body

    def call_json(self, messages: list[dict[str, str]], call_name: str) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = [
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=True, include_images=True),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=True, include_max_output=True, include_reasoning=True, include_images=True),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=True, include_images=True, include_files=False),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=True, include_max_output=True, include_reasoning=True, include_images=True, include_files=False),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=False, include_images=True),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=True, include_images=False),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=False, include_images=False, include_files=False),
            self.build_body(messages, json_mode=True, call_name=call_name, include_temperature=False, include_max_output=False, include_reasoning=False, include_images=True),
            self.build_body(messages, json_mode=False, call_name=call_name, include_temperature=False, include_max_output=False, include_reasoning=False, include_images=True),
            self.build_body(messages, json_mode=False, call_name=call_name, include_temperature=False, include_max_output=True, include_reasoning=False, include_images=False, include_files=False),
        ]
        if self.config.use_stream:
            expanded_attempts: list[dict[str, Any]] = []
            for body in attempts:
                expanded_attempts.append(body)
                if body.get("stream"):
                    non_stream_body = json.loads(json.dumps(body, ensure_ascii=False))
                    non_stream_body.pop("stream", None)
                    expanded_attempts.append(non_stream_body)
            attempts = expanded_attempts
        for body in attempts:
            cached = self.read_cache(self.cache_key(body), call_name)
            if cached is not None:
                return cached
        if not self.config.api_url:
            raise RuntimeError("GRADER_API_URL is not configured")
        if not self.config.api_key:
            raise RuntimeError("GRADER_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        last_error: str | None = None
        degradation_limit = len(attempts)
        network_retry_limit = max(1, self.config.max_retries)
        for degradation_no, body in enumerate(attempts[:degradation_limit], start=1):
            cache_key = self.cache_key(body)
            cached = self.read_cache(cache_key, call_name)
            if cached is not None:
                return cached
            for retry_no in range(1, network_retry_limit + 1):
                started = time.time()
                append_jsonl(
                    self.audit_log,
                    {
                        "event": "model_call_started",
                        "time": utc_now(),
                        "call_name": call_name,
                        "attempt_no": degradation_no,
                        "retry_no": retry_no,
                        "endpoint": self.endpoint,
                        "model": self.config.model,
                        "reasoning_effort": self.config.reasoning_effort,
                        "stream": bool(body.get("stream")),
                    },
                )
                try:
                    request_started = time.time()
                    response = requests.post(
                        self.endpoint,
                        headers=headers,
                        json=body,
                        timeout=self.config.timeout_seconds,
                        stream=bool(body.get("stream")),
                    )
                    header_elapsed = round(time.time() - request_started, 3)
                    payload = safe_response_payload(response, stream=bool(body.get("stream")))
                    elapsed = round(time.time() - started, 3)
                    append_jsonl(
                        self.audit_log,
                        {
                            "event": "model_call",
                            "time": utc_now(),
                            "call_name": call_name,
                            "attempt_no": degradation_no,
                            "retry_no": retry_no,
                            "endpoint": self.endpoint,
                            "model": self.config.model,
                            "status_code": response.status_code,
                            "elapsed_seconds": elapsed,
                            "header_elapsed_seconds": header_elapsed,
                            "usage": extract_usage_summary(payload),
                            "request": scrub_request(body),
                            "response_preview": compact_text(json.dumps(payload, ensure_ascii=False), limit=6000),
                            "stream": bool(body.get("stream")),
                        },
                    )
                    if response.status_code >= 400:
                        last_error = f"HTTP {response.status_code}: {payload}"
                        if is_retryable_status(response.status_code) and retry_no < network_retry_limit:
                            sleep_for_retry(retry_no, degradation_no)
                            continue
                        break
                    content = extract_message_content(payload, self.config.api_mode)
                    parsed_json = parse_json_object(content)
                    self.write_cache(cache_key, call_name, body, parsed_json, payload)
                    return parsed_json
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    append_jsonl(
                        self.audit_log,
                        {
                            "event": "model_call_error",
                            "time": utc_now(),
                            "call_name": call_name,
                            "attempt_no": degradation_no,
                            "retry_no": retry_no,
                            "error": last_error,
                            "stream": bool(body.get("stream")),
                        },
                    )
                    if retry_no < network_retry_limit:
                        sleep_for_retry(retry_no, degradation_no)
                        continue
                    break
            sleep_for_retry(1, degradation_no, cap=4.0)
        raise RuntimeError(last_error or "model call failed")


def safe_response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {"text": response.text[:4000]}


def extract_usage_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
        if key in usage:
            summary[key] = usage.get(key)
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if cached is not None:
            summary["cached_tokens"] = cached
    return summary


def safe_response_payload(response: requests.Response, stream: bool = False) -> Any:
    if not stream:
        return safe_response_json(response)
    text = read_streaming_response_text(response)
    if response.status_code >= 400:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(text)
        return {"text": text[:4000]}
    parsed = parse_streaming_response_payload(text)
    if parsed is not None:
        return parsed
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(text)
    return {"output_text": text}


def read_streaming_response_text(response: requests.Response) -> str:
    chunks: list[str] = []
    try:
        for line in response.iter_lines(decode_unicode=False):
            if line is None:
                continue
            if isinstance(line, bytes):
                chunks.append(line.decode("utf-8", errors="replace"))
            else:
                chunks.append(str(line))
    finally:
        response.close()
    return "\n".join(chunks)


def parse_streaming_response_payload(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    output_text_parts: list[str] = []
    done_text_parts: list[str] = []
    last_completed: dict[str, Any] | None = None
    last_usage: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            continue
        with contextlib.suppress(json.JSONDecodeError):
            event = json.loads(line)
            if isinstance(event, dict):
                event_type = str(event.get("type") or "")
                if event_type in {"response.output_text.delta", "response.refusal.delta"} and isinstance(event.get("delta"), str):
                    output_text_parts.append(event["delta"])
                elif event_type in {"response.completed", "response.response.completed"} and isinstance(event.get("response"), dict):
                    last_completed = event["response"]
                    usage = last_completed.get("usage")
                    if isinstance(usage, dict):
                        last_usage = usage
                elif isinstance(event.get("usage"), dict):
                    last_usage = event["usage"]
                elif event_type == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict):
                        content = item.get("content")
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and isinstance(part.get("text") or part.get("output_text"), str):
                                    done_text_parts.append(str(part.get("text") or part.get("output_text")))
                choices = event.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        message = choice.get("message") or {}
                        content = delta.get("content") or message.get("content")
                        if isinstance(content, str):
                            output_text_parts.append(content)
    if output_text_parts:
        payload: dict[str, Any] = {"output_text": "".join(output_text_parts)}
        if last_usage is not None:
            payload["usage"] = last_usage
        return payload
    if done_text_parts:
        payload = {"output_text": "".join(done_text_parts)}
        if last_usage is not None:
            payload["usage"] = last_usage
        return payload
    return last_completed


def is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def sleep_for_retry(retry_no: int, degradation_no: int, cap: float = 12.0) -> None:
    base = min(cap, 0.8 * (2 ** max(0, retry_no - 1)) + 0.4 * max(0, degradation_no - 1))
    time.sleep(base + random.uniform(0.2, 1.3))


def scrub_request(body: dict[str, Any]) -> dict[str, Any]:
    scrubbed = json.loads(json.dumps(body, ensure_ascii=False))
    for message in scrubbed.get("messages", []):
        content = message.get("content")
        if isinstance(content, str) and len(content) > 8000:
            message["content"] = compact_text(content, limit=8000)
        elif isinstance(content, list):
            message["content"] = scrub_content_parts(content)
    if isinstance(scrubbed.get("input"), str) and len(scrubbed["input"]) > 8000:
        scrubbed["input"] = compact_text(scrubbed["input"], limit=8000)
    elif isinstance(scrubbed.get("input"), list):
        for item in scrubbed["input"]:
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                item["content"] = scrub_content_parts(item["content"])
    return scrubbed


def scrub_content_parts(parts: list[Any]) -> list[Any]:
    scrubbed_parts: list[Any] = []
    for part in parts:
        if not isinstance(part, dict):
            scrubbed_parts.append(part)
            continue
        clean = dict(part)
        if "image_url" in clean:
            value = clean.get("image_url")
            if isinstance(value, dict):
                value = value.get("url")
            clean["image_url"] = scrub_image_value(value)
        if "image_url" in clean and isinstance(clean.get("image_url"), str):
            clean["image_url"] = scrub_image_value(clean["image_url"])
        if clean.get("type") == "input_file" and isinstance(clean.get("file_data"), str):
            clean["file_data"] = scrub_file_value(clean["file_data"])
        if "text" in clean and isinstance(clean["text"], str) and len(clean["text"]) > 8000:
            clean["text"] = compact_text(clean["text"], limit=8000)
        scrubbed_parts.append(clean)
    return scrubbed_parts


def scrub_image_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"[base64_image_redacted len={len(value)}]"
    return value


def scrub_file_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("data:application/pdf"):
        return f"[base64_pdf_redacted len={len(value)}]"
    return value


def prepare_messages_for_transport(messages: list[dict[str, Any]], include_images: bool, include_files: bool = True) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            prepared.append(dict(message))
            continue
        parts = []
        for part in content:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            if part.get("type") in {"image_url", "input_image"} and not include_images:
                continue
            if part.get("type") == "input_file" and not include_files:
                continue
            parts.append(dict(part))
        prepared.append({**message, "content": parts})
    return prepared


def stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    return str(content)


def response_content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content)}]
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
        elif isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
            parts.append({"type": "input_text", "text": str(part.get("text", ""))})
        elif isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            parts.append({"type": "input_image", "image_url": str(image_url or "")})
        elif isinstance(part, dict) and part.get("type") == "input_file":
            file_part = {"type": "input_file"}
            if part.get("filename"):
                file_part["filename"] = str(part.get("filename"))
            if part.get("file_id"):
                file_part["file_id"] = str(part.get("file_id"))
            elif part.get("file_data"):
                file_part["file_data"] = str(part.get("file_data"))
            parts.append(file_part)
    return parts


def extract_message_content(payload: Any, api_mode: str = "chat") -> str:
    if api_mode == "responses":
        if isinstance(payload, dict):
            output_text = payload.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text
            output = payload.get("output")
            if isinstance(output, list):
                parts: list[str] = []
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                text = part.get("text") or part.get("output_text")
                                if isinstance(text, str):
                                    parts.append(text)
                            elif isinstance(part, str):
                                parts.append(part)
                if parts:
                    return "".join(parts)
        raise ValueError(f"responses payload has no text output: {payload}")
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise ValueError(f"response has no choices: {payload}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str):
        raise ValueError(f"message content is not text: {message}")
    return content


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("model JSON output must be an object")
    return payload


def shrink_reference(reference: dict[str, Any], limit: int) -> dict[str, Any]:
    if not reference:
        return reference
    shrunk = dict(reference)
    if "reference_text" in shrunk:
        shrunk["reference_text"] = compact_text(str(shrunk.get("reference_text") or ""), limit=limit)
    sources = shrunk.get("sources")
    if isinstance(sources, list) and len(sources) > 2:
        shrunk["sources"] = sources[:2]
    return shrunk


def visual_attention_protocol(question_label: str, visual_payload: dict[str, Any] | None) -> dict[str, Any]:
    pages = (visual_payload or {}).get("pages", [])
    focus_pages = (visual_payload or {}).get("focus_pages", [])
    stable_pages = stable_visual_pages(pages)
    stable_focus_pages = stable_visual_pages(focus_pages)
    pdf_sources = (visual_payload or {}).get("pdf_sources", [])
    layout_scan = (visual_payload or {}).get("layout_scan")
    return {
        "enabled": bool(pages or focus_pages),
        "question_label": question_label,
        "priority_rule": "When visual pages or PDF files are attached, read visual/PDF evidence first; MinerU OCR is only a draft.",
        "mandatory_steps": [
            "1. 若提供局部聚焦图，先查看聚焦图；再查看PDF整页图和PDF文件证据，定位本题题号、答题区域、手写/涂改/空白位置。",
            "2. 将卷面可见学生作答转写到 recognized_student_answer，公式、上下标、正负号、分式、区间端点要谨慎。",
            "3. 再与 MinerU OCR 草稿逐项比较；若不一致，以PDF视觉为主，并在 visual_reading_summary 说明差异。",
            "4. 若所附页图没有覆盖本题、题号错位、 handwriting 不清或关键符号不可辨，evidence_used=insufficient 且 needs_human_review=true。",
            "5. 只有完成视觉转写后，才能依据参考答案和评分标准判分。",
        ],
        "anti_hallucination_rules": [
            "不得把题目印刷内容、参考答案、空白横线当作学生作答。",
            "不得因为 MinerU 显示“未识别到”就直接判空白；必须先检查PDF图像。",
            "不得跳过题号定位；如果图中没有该题作答区域，必须说明页面不匹配。",
        ],
        "attached_focus_pages": stable_focus_pages,
        "attached_pages": stable_pages,
        "attached_pdf_sources": pdf_sources,
        "layout_scan_guidance": layout_scan,
    }


def stable_visual_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stable: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        source_pdf = str(page.get("source_pdf") or "")
        stable.append(
            {
                "source_pdf_name": Path(source_pdf).name if source_pdf else "",
                "page_no": page.get("page_no"),
                "width": page.get("width"),
                "height": page.get("height"),
                **({"focus_question_no": page.get("focus_question_no")} if page.get("focus_question_no") else {}),
                **({"focus_role": page.get("focus_role")} if page.get("focus_role") else {}),
            }
        )
    return stable


def append_visual_attention_parts(
    user_parts: list[dict[str, Any]],
    visual_payload: dict[str, Any] | None,
    question_label: str,
) -> None:
    pages = (visual_payload or {}).get("pages", [])
    focus_pages = (visual_payload or {}).get("focus_pages", [])
    if focus_pages and (visual_payload or {}).get("focus_only", True):
        pages = []
    if not pages and not focus_pages:
        return
    user_parts.append(
        {
            "type": "text",
            "text": (
                f"【强制视觉注意力】下面是 {question_label} 的原始PDF页图；若后面同时附PDF文件证据，也必须一起核对。"
                "若有局部聚焦图，先看局部聚焦图，再看整页图/PDF定位题号和作答区域，先转写卷面，再看 MinerU OCR 草稿；"
                "卷面与 OCR 冲突时以卷面为准。"
            ),
        }
    )
    attached_pdfs: set[str] = set()
    for page in focus_pages:
        image_path = Path(str(page.get("image_path") or ""))
        if image_path.exists():
            user_parts.append(
                {
                    "type": "text",
                    "text": f"【局部聚焦图】优先查看：{question_label} 的答题区域裁剪图。",
                }
            )
            user_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url(image_path), "detail": "high"},
                }
            )
    for page in pages:
        image_path = Path(str(page.get("image_path") or ""))
        if image_path.exists():
            user_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url(image_path), "detail": "high"},
                }
            )
        pdf_path = Path(str(page.get("source_pdf") or ""))
        attach_pdf_file = bool((visual_payload or {}).get("attach_pdf_file", True))
        if attach_pdf_file and pdf_path.exists() and str(pdf_path) not in attached_pdfs:
            attached_pdfs.add(str(pdf_path))
            user_parts.append(
                {
                    "type": "input_file",
                    "filename": pdf_path.name,
                    "file_data": pdf_data_url(pdf_path),
                }
            )


def build_grade_messages(
    item: AnswerItem,
    question_reference: dict[str, Any],
    reference: dict[str, Any],
    rubric: dict[str, Any],
    paper_id: str,
    round_name: str,
    grading_policy: str,
    visual_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    system = (
        PROMPT_CACHE_PRIMER
        + "\n\n"
        "你是考研数学阅卷专家。你只能依据输入中的参考答案、考试分析、评分标准和明确标记的推导评分点评分。"
        "不得把推导评分点伪装成官方标准。若依据不足、OCR不清或无法判断，必须设置 needs_human_review=true。"
        "选择题按最终选项判分；填空题按结果及数学等价性判分；解答题必须按步骤给分，不能只看最终答案。"
        "大题必须采用严格口径：高分需要卷面可见的关键推导、条件检查和逻辑闭合；只写答案、跳过核心步骤或边界条件未检时必须封顶扣分。"
        "输出必须是合法 JSON，不要输出 Markdown。"
    )
    compact_policy = grading_policy
    compact_question_reference = question_reference
    compact_reference = reference
    if item.question_type in {"objective", "blank"}:
        compact_policy = (
            "选择题按最终选项判分；填空题按最终结果及数学等价形式判分。"
            "OCR或答案依据不清时标记 needs_human_review=true。输出合法JSON。"
        )
        compact_question_reference = shrink_reference(question_reference, 1200)
        compact_reference = shrink_reference(reference, 1500)
    user_payload = {
        "task": "grade_one_math_exam_question",
        "round_name": round_name,
        "question": {
            "question_no": item.question_no,
            "question_type": item.question_type,
            "full_score": item.full_score,
            "question_text_reference": compact_question_reference,
        },
        "student_answer_ocr": {
            "text": item.text,
            "ocr_confidence": item.ocr_confidence,
            "needs_ocr_review": item.needs_ocr_review,
            "ocr_issues": item.ocr_issues,
        },
        "student_answer_visual_evidence": visual_payload or {"enabled": False, "pages": []},
        "visual_attention_protocol": visual_attention_protocol(f"第 {item.question_no} 题", visual_payload),
        "reference": compact_reference,
        "rubric": rubric,
        "global_grading_policy": compact_policy,
        "visual_review_instruction": (
            "Visual evidence and MinerU OCR must be cross-checked for every graded question. "
            "Inspect the rendered PDF page image directly; treat MinerU OCR as a draft, not as final evidence. "
            "If OCR and handwriting/image disagree, use the image as primary evidence and set needs_human_review=true "
            "when the handwriting is not clear enough. If MinerU says the answer is missing but the image contains handwriting, "
            "read the handwriting from the image and grade it. If the attached image pages do not contain this question, "
            "say so in visual_reading_summary and set needs_human_review=true. "
            "For blank questions, printed answer lines are not part of the student's answer; do not treat the underline "
            "before or under a handwritten expression as a minus sign."
        ),
        "strict_solution_grading_protocol": (
            "Only applies to solution questions. Before scoring, list the visible key steps in the student's work and compare them with inferred scoring points. "
            "A correct final answer without visible derivation cannot receive high credit. Missing theorem conditions, boundary/domain/endpoint checks, "
            "existence-uniqueness proof, integral region/limits, distribution/likelihood derivation, or matrix-property justification must be deducted. "
            "First create a point-by-point checklist with status seen/missing/wrong/unclear. Score only from seen-and-correct points. "
            "If any core scoring point is missing, the score should normally be below 80%; if any core setup/model/region/distribution/matrix point is wrong, it should normally be below 70%. "
            "If giving more than 80% credit, valid_student_steps/main_earned_points must show a complete core reasoning chain. "
            "If a score is capped because evidence is incomplete, state the cap reason in main_deducted_points. "
            "Return executable exam scores, not fuzzy averages: solution-question scores must be integer points only; when unsure between two adjacent scores, choose the lower one."
        ),
        "required_output_schema": {
            "question_no": "string",
            "full_score": "number",
            "score": "number|null",
            "recognized_student_answer": "string",
            "visual_reading_summary": "string",
            "evidence_used": "visual|mineru_ocr|both|insufficient",
            "earned_points": ["string"],
            "deducted_points": ["string"],
            "main_earned_points": ["string"],
            "main_deducted_points": ["string"],
            "valid_student_steps": ["string"],
            "wrong_or_missing_steps": ["string"],
            "strict_score_cap_reason": "string|null",
            "strict_scoring_checklist": [{"point": "string", "status": "seen|missing|wrong|unclear", "impact": "string"}],
            "needs_human_review": "boolean",
            "review_reason": "string|null",
            "evidence_sources": ["string"],
            "confidence": "number between 0 and 1",
            "reason": "string",
        },
        "output_constraints": {
            "max_items_per_list": 3,
            "max_chars_per_list_item": 80,
            "max_reason_chars": 160,
            "style": "Concise Chinese. Do not repeat the same point in several fields.",
            "solution_question_style": "Strict. Name missing inferred scoring points explicitly; do not give comfort points for unwritten reasoning.",
            "score_granularity": "objective: 0/full score; blank: integer score; solution: integer score only, prefer lower adjacent score when evidence is incomplete.",
        },
    }
    if visual_payload and "stable_pages" in visual_payload:
        stable_pages_for_payload = [] if visual_payload.get("focus_pages") and visual_payload.get("focus_only", True) else visual_payload.get("stable_pages", [])
        user_payload["student_answer_visual_evidence"] = {
            **{key: value for key, value in visual_payload.items() if key not in {"pages", "focus_pages"}},
            "pages": stable_pages_for_payload,
            "focus_pages": visual_payload.get("stable_focus_pages", []),
        }
    user_parts: list[dict[str, Any]] = []
    append_visual_attention_parts(user_parts, visual_payload, f"第 {item.question_no} 题")
    user_parts.append({"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def build_objective_batch_messages(
    items: list[AnswerItem],
    references: dict[str, dict[str, Any]],
    paper_id: str,
    round_name: str,
    visual_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    system = (
        PROMPT_CACHE_PRIMER
        + "\n\n"
        "你是考研数学答卷视觉识别员。你的唯一任务是从PDF页图中读取第1-10题学生最终选择的 A/B/C/D。"
        "不要解题，不要判分，不要根据参考答案反推。看不清就返回 null 并说明。输出必须是合法 JSON。"
    )
    questions = []
    for item in items:
        questions.append(
            {
                "question_no": item.question_no,
                "ocr_hint": compact_text(objective_student_text_for_model(item), limit=120),
            }
        )
    user_payload = {
        "task": "read_objective_choices_from_visual_batch",
        "round_name": round_name,
        "questions": questions,
        "student_answer_visual_evidence": visual_payload or {"enabled": False, "pages": []},
        "visual_attention_protocol": visual_attention_protocol("第 1-10 题选择题", visual_payload),
        "rules": [
            "只读取学生卷面上每题最终圈选/书写/标注的选项。",
            "不要把题干中印刷的(A)(B)(C)(D)当成学生答案。",
            "若一题有多处演算，只以靠近题号或题目末尾的最终大写选项/圈选为准。",
            "若无法确认，student_choice=null，needs_human_review=true。",
        ],
        "required_output_schema": {
            "items": [
                {
                    "question_no": "string",
                    "student_choice": "A|B|C|D|null",
                    "recognized_student_answer": "string",
                    "visual_reading_summary": "string",
                    "evidence_used": "visual|both|insufficient",
                    "needs_human_review": "boolean",
                    "review_reason": "string|null",
                    "confidence": "number between 0 and 1",
                }
            ]
        },
        "output_constraints": {
            "max_chars_per_item": 120,
            "no_scoring": True,
        },
    }
    if visual_payload and "stable_pages" in visual_payload:
        stable_pages_for_payload = [] if visual_payload.get("focus_pages") and visual_payload.get("focus_only", True) else visual_payload.get("stable_pages", [])
        user_payload["student_answer_visual_evidence"] = {
            **{key: value for key, value in visual_payload.items() if key not in {"pages", "focus_pages"}},
            "pages": stable_pages_for_payload,
            "focus_pages": visual_payload.get("stable_focus_pages", []),
        }
    user_parts: list[dict[str, Any]] = []
    append_visual_attention_parts(user_parts, visual_payload, "第 1-10 题选择题")
    user_parts.append({"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def build_objective_batch_arbitration_messages(
    items: list[AnswerItem],
    references: dict[str, dict[str, Any]],
    paper_id: str,
    first_by_no: dict[str, dict[str, Any]],
    second_by_no: dict[str, dict[str, Any]],
    visual_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    system = (
        PROMPT_CACHE_PRIMER
        + "\n\n"
        "你是考研数学选择题仲裁阅卷员。你需要比较前两轮选择题评分差异，"
        "重新核对学生最终选项、参考答案和卷面视觉证据。只输出合法 JSON。"
    )
    questions = []
    for item in items:
        questions.append(
            {
                "question_no": item.question_no,
                "full_score": item.full_score,
                "student_answer_ocr": objective_student_text_for_model(item),
                "reference": shrink_reference(references.get(item.question_no, {}), 420),
                "round_1": first_by_no.get(item.question_no),
                "round_2": second_by_no.get(item.question_no),
            }
        )
    user_payload = {
        "task": "arbitrate_objective_questions_batch",
        "questions": questions,
        "student_answer_visual_evidence": visual_payload or {"enabled": False, "pages": []},
        "visual_attention_protocol": visual_attention_protocol("第 1-10 题选择题仲裁", visual_payload),
        "rules": [
            "选择题只按最终选项判分。",
            "选项与参考答案一致给5分，不一致给0分。",
            "若卷面或参考答案无法确认，score=null 并 needs_human_review=true。",
        ],
        "required_output_schema": {"items": [{"question_no": "string", "score": "number|null", "recognized_student_answer": "string", "visual_reading_summary": "string", "evidence_used": "visual|mineru_ocr|both|insufficient", "needs_human_review": "boolean", "confidence": "number", "reason": "string"}]},
    }
    user_parts: list[dict[str, Any]] = []
    append_visual_attention_parts(user_parts, visual_payload, "第 1-10 题选择题仲裁")
    user_parts.append({"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def build_answer_layout_scan_messages(
    items: list[AnswerItem],
    visual_payload: dict[str, Any],
    paper_id: str,
) -> list[dict[str, Any]]:
    system = (
        PROMPT_CACHE_PRIMER
        + "\n\n"
        "你是考研数学答卷视觉定位员。你的任务不是阅卷，也不是判断正误，"
        "只从整份学生答卷 PDF 页图中找出每道题的学生作答位置、作答顺序和大致裁剪框。"
        "考生可能不按题号顺序作答，例如先写18再写17；你必须按卷面真实位置记录。"
        "输出必须是合法 JSON。"
    )
    questions = [
        {
            "question_no": item.question_no,
            "question_type": item.question_type,
            "mineru_answer_hint": compact_text(item.text, limit=280),
        }
        for item in items
    ]
    user_payload = {
        "task": "scan_student_answer_layout_before_grading",
        "paper_id": paper_id,
        "questions": questions,
        "student_answer_visual_evidence": {
            **{key: value for key, value in visual_payload.items() if key not in {"pages", "focus_pages"}},
            "pages": visual_payload.get("stable_pages", []),
            "focus_pages": [],
        },
        "rules": [
            "只做定位和视觉转写辅助，不给分、不解题。",
            "每题先找题号或学生写出的题号，再框出该题主要作答区域；跨页续写时给多个框。",
            "answer_boxes 使用相对坐标 [x1,y1,x2,y2]，范围 0-1，覆盖学生作答，不要只框题号。",
            "若题号顺序与卷面顺序不同，is_out_of_order=true，并在 notes 说明。",
            "看不清或不能确定位置时仍返回该题，但 page_numbers=[]、confidence 低、needs_human_review=true。",
            "不要把题目 PDF 或参考答案当成学生作答；这里只看学生答卷。",
        ],
        "required_output_schema": {
            "items": [
                {
                    "question_no": "string",
                    "question_type": "objective|blank|solution",
                    "page_numbers": ["number"],
                    "answer_order_index": "number|null",
                    "answer_boxes": [{"page_no": "number", "box": ["x1", "y1", "x2", "y2"]}],
                    "recognized_answer_brief": "string",
                    "is_out_of_order": "boolean",
                    "needs_human_review": "boolean",
                    "confidence": "number between 0 and 1",
                    "notes": "string",
                }
            ],
            "global_notes": "string",
        },
        "output_constraints": {
            "max_boxes_per_question": 3,
            "max_notes_chars": 120,
            "coordinate_precision": 3,
        },
    }
    user_parts: list[dict[str, Any]] = []
    append_visual_attention_parts(user_parts, visual_payload, "整份答卷作答顺序定位")
    user_parts.append({"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def scan_answer_layout(
    gateway: ModelGateway,
    items: list[AnswerItem],
    visual_pages: list[VisualPage],
    paper_id: str,
    audit_log: Path,
) -> AnswerLayout | None:
    if not visual_pages or not items:
        return None
    visual_payload = visual_payload_for_layout_scan(visual_pages)
    layout_gateway = gateway.for_reasoning_effort("xhigh")
    append_jsonl(
        audit_log,
        {
            "event": "answer_layout_scan_started",
            "time": utc_now(),
            "question_count": len(items),
            "page_count": len(visual_pages),
            "reasoning_effort": layout_gateway.config.reasoning_effort,
        },
    )
    try:
        raw = layout_gateway.call_json(
            build_answer_layout_scan_messages(items, visual_payload, paper_id),
            "answer_layout_scan",
        )
        layout = normalize_answer_layout(raw, items)
        append_jsonl(
            audit_log,
            {
                "event": "answer_layout_scan_finished",
                "time": utc_now(),
                "located_question_count": sum(1 for entry in layout.items.values() if normalized_layout_page_numbers(entry)),
                "low_confidence_questions": [
                    question_no
                    for question_no, entry in layout.items.items()
                    if layout_entry_confidence(entry) < 0.55 or entry.get("needs_human_review")
                ],
                "global_notes": layout.global_notes,
            },
        )
        return layout
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            audit_log,
            {
                "event": "answer_layout_scan_failed",
                "time": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


def normalize_answer_layout(raw: dict[str, Any], items: list[AnswerItem]) -> AnswerLayout:
    raw = repair_text_encoding(raw)
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raw_items = raw.get("questions")
    if not isinstance(raw_items, list):
        raw_items = []
    wanted = {item.question_no: item for item in items}
    layout_items: dict[str, dict[str, Any]] = {}
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        question_no = str(row.get("question_no") or "").strip()
        if question_no not in wanted:
            continue
        row_page_numbers = normalized_layout_page_numbers(row)
        answer_boxes = []
        for raw_box in ensure_list_of_dicts(row.get("answer_boxes"))[:3]:
            box = normalize_ratio_box(raw_box.get("box"))
            if box is None:
                box = normalize_ratio_box([raw_box.get("x1"), raw_box.get("y1"), raw_box.get("x2"), raw_box.get("y2")])
            if box is None:
                continue
            page_no = raw_box.get("page_no") or row.get("page_no")
            try:
                page_no = int(page_no)
            except (TypeError, ValueError):
                page_no = row_page_numbers[0] if row_page_numbers else None
            if page_no is None:
                continue
            answer_boxes.append({"page_no": page_no, "box": list(box)})
        entry = {
            "question_no": question_no,
            "question_type": wanted[question_no].question_type,
            "page_numbers": row_page_numbers,
            "answer_order_index": safe_int_or_none(row.get("answer_order_index")),
            "answer_boxes": answer_boxes,
            "recognized_answer_brief": truncate_str(str(row.get("recognized_answer_brief") or ""), 300),
            "is_out_of_order": bool(row.get("is_out_of_order")),
            "needs_human_review": bool(row.get("needs_human_review", False)),
            "confidence": layout_entry_confidence(row),
            "notes": truncate_str(str(row.get("notes") or ""), 220),
        }
        if not entry["page_numbers"]:
            entry["page_numbers"] = sorted({box["page_no"] for box in answer_boxes})
        layout_items[question_no] = entry
    for item in items:
        layout_items.setdefault(
            item.question_no,
            {
                "question_no": item.question_no,
                "question_type": item.question_type,
                "page_numbers": [],
                "answer_order_index": None,
                "answer_boxes": [],
                "recognized_answer_brief": "",
                "is_out_of_order": False,
                "needs_human_review": True,
                "confidence": 0.0,
                "notes": "视觉预读未返回该题位置",
            },
        )
    return AnswerLayout(
        items=layout_items,
        global_notes=truncate_str(str(raw.get("global_notes") or ""), 500),
        source="model_whole_submission_visual_scan",
    )


def safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_arbitration_messages(
    item: AnswerItem,
    question_reference: dict[str, Any],
    reference: dict[str, Any],
    rubric: dict[str, Any],
    paper_id: str,
    first: dict[str, Any],
    second: dict[str, Any],
    grading_policy: str,
    visual_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    system = (
        PROMPT_CACHE_PRIMER
        + "\n\n"
        "你是考研数学第三次仲裁阅卷员。你的任务是重新检查学生答案、评分依据和前两次评分差异。"
        "不要简单折中或投票；必须按评分点重新判断。大题按严格口径仲裁：没有可见关键步骤不能高分，关键条件或证明闭合缺失必须扣分。输出必须是合法 JSON。"
    )
    compact_policy = grading_policy
    compact_question_reference = question_reference
    compact_reference = reference
    if item.question_type in {"objective", "blank"}:
        compact_policy = "客观题按最终答案判分；填空题按数学等价结果判分。输出合法JSON。"
        compact_question_reference = shrink_reference(question_reference, 1200)
        compact_reference = shrink_reference(reference, 1500)
    user_payload = {
        "task": "arbitrate_math_exam_question",
        "question": {
            "question_no": item.question_no,
            "question_type": item.question_type,
            "full_score": item.full_score,
            "question_text_reference": compact_question_reference,
        },
        "student_answer_ocr": {
            "text": item.text,
            "ocr_confidence": item.ocr_confidence,
            "needs_ocr_review": item.needs_ocr_review,
            "ocr_issues": item.ocr_issues,
        },
        "student_answer_visual_evidence": visual_payload or {"enabled": False, "pages": []},
        "visual_attention_protocol": visual_attention_protocol(f"第 {item.question_no} 题仲裁", visual_payload),
        "reference": compact_reference,
        "rubric": rubric,
        "global_grading_policy": compact_policy,
        "visual_review_instruction": (
            "Visual evidence and MinerU OCR must be cross-checked. Inspect the rendered PDF page image directly; "
            "MinerU OCR is only a draft. If OCR and image disagree, use image as primary evidence and explain the conflict. "
            "If the attached image pages do not contain this question, set evidence_used=insufficient and needs_human_review=true. "
            "For blank questions, printed answer lines are not part of the student's answer; do not treat the underline "
            "before or under a handwritten expression as a minus sign."
        ),
        "strict_solution_arbitration_protocol": (
            "Only applies to solution questions. Re-score from evidence, not from the average of the first two scores. "
            "Prefer the lower score if the higher score depends on unwritten reasoning, missing key derivation, or unverified conditions. "
            "If both prior rounds overlooked a missing boundary/domain/distribution/likelihood/matrix/proof condition, correct it and cap the score. "
            "Use a point-by-point checklist and do not exceed 80% when a core scoring point is missing. "
            "Return integer solution-question scores only; when the previous average is fractional, convert it to the lower justified integer unless arbitration evidence clearly supports the higher integer."
        ),
        "round_1": first,
        "round_2": second,
        "required_output_schema": {
            "question_no": "string",
            "full_score": "number",
            "score": "number|null",
            "recognized_student_answer": "string",
            "visual_reading_summary": "string",
            "evidence_used": "visual|mineru_ocr|both|insufficient",
            "earned_points": ["string"],
            "deducted_points": ["string"],
            "main_earned_points": ["string"],
            "main_deducted_points": ["string"],
            "valid_student_steps": ["string"],
            "wrong_or_missing_steps": ["string"],
            "strict_score_cap_reason": "string|null",
            "strict_scoring_checklist": [{"point": "string", "status": "seen|missing|wrong|unclear", "impact": "string"}],
            "needs_human_review": "boolean",
            "review_reason": "string|null",
            "evidence_sources": ["string"],
            "confidence": "number between 0 and 1",
            "reason": "string",
            "difference_analysis": "string",
        },
        "output_constraints": {
            "max_items_per_list": 3,
            "max_chars_per_list_item": 80,
            "max_reason_chars": 180,
            "style": "Concise Chinese. Focus only on score-changing differences.",
            "score_granularity": "solution: integer score only; no scores like 7.8, 9.36, or 6.5.",
        },
    }
    user_parts: list[dict[str, Any]] = []
    append_visual_attention_parts(user_parts, visual_payload, f"第 {item.question_no} 题仲裁")
    user_parts.append({"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def extract_objective_choice(text: str, question_no: str | None = None) -> str | None:
    text = repair_mojibake(text or "")
    patterns = [
        r"OCR objective answer\s*[:：]\s*([A-DＡ-Ｄa-d])",
        r"参考答案\s*[:：]\s*([A-DＡ-Ｄa-d])",
        r"答案(?:为|是)?\s*[:：]?\s*([A-DＡ-Ｄa-d])",
    ]
    if question_no:
        patterns.insert(0, rf"(?<!\d){re.escape(question_no)}\s*[\.．、]?\s*参考答案\s*[:：]\s*([A-DＡ-Ｄa-d])")
        patterns.insert(1, rf"(?<!\d){re.escape(question_no)}\s*[\.．、]\s*([A-DＡ-Ｄa-d])(?:\b|[。；;，,])")
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return normalize_choice_letter(match.group(1))
    letters = re.findall(r"\b([A-DＡ-Ｄa-d])\b", text)
    if len(letters) == 1:
        return normalize_choice_letter(letters[0])
    return None


def normalize_choice_letter(value: str) -> str:
    table = str.maketrans({"Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D"})
    return value.translate(table).upper()


OBJECTIVE_ANSWER_LINE_BAD_PATTERNS = re.compile(
    r"<!--|!\[\]|\$|\\(?:frac|sin|cos|tan|ln|log|lim|int|sum|begin|operatorname)|"
    r"[=<>≤≥∫∑√∞]|设|函数|方程|区域|收敛|间断|微分|积分|证明|求|则|为|处|点|"
    r"\([A-DＡ-Ｄ]\)|（[A-DＡ-Ｄ]）",
    flags=re.I,
)


def is_plausible_objective_answer_line(line: str) -> bool:
    line = repair_mojibake(line or "").replace("\u3000", " ").strip()
    if not line:
        return False
    compact = re.sub(r"\s+", " ", line)
    if len(compact) > 180:
        return False
    if OBJECTIVE_ANSWER_LINE_BAD_PATTERNS.search(compact):
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", compact))
    if cjk_count > 8:
        return False
    explicit_answer_hint = bool(re.search(r"(答案|选择题|客观题|ans|answer)", compact, flags=re.I))
    token_pattern = re.compile(r"(?<!\d)(10|[1-9])\s*[\.．、:：]?\s*([A-DＡ-Ｄa-d])?(?![A-Za-z])")
    matches = [m for m in token_pattern.finditer(compact) if 1 <= int(m.group(1)) <= 10]
    if len(matches) < 2:
        return False
    choices = sum(1 for match in matches if match.group(2))
    blanks = len(matches) - choices
    if choices >= 5:
        return True
    if choices >= 3 and explicit_answer_hint:
        return True
    if len(matches) >= 8 and choices >= 2 and blanks <= 6 and explicit_answer_hint:
        return True
    range_match = MineruMarkdownParser.CHOICE_RANGE_RE.search(compact)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        letters = re.sub(r"[^A-DＡ-Ｄa-d]", "", range_match.group(3))
        return 1 <= start <= end <= 10 and len(letters) >= end - start + 1
    return False


def objective_answer_evidence_quality(text: str) -> str:
    text = repair_mojibake(text or "")
    if re.search(r"OCR objective answer\s*[:：]\s*[A-DＡ-Ｄa-d]", text, flags=re.I):
        return "deterministic"
    shared_match = re.search(r"Shared objective line\s*[:：]\s*(.+)", text, flags=re.I | re.S)
    if shared_match and is_plausible_objective_answer_line(shared_match.group(1).strip()):
        return "deterministic"
    if has_explicit_objective_blank(text):
        shared_match = re.search(r"Shared objective line\s*[:：]\s*(.+)", text, flags=re.I | re.S)
        if shared_match and is_plausible_objective_answer_line(shared_match.group(1).strip()):
            return "deterministic"
    return "uncertain"


def objective_answer_prelude(text: str, limit: int = 1200) -> str:
    text = repair_mojibake(text or "").replace("\u3000", " ")
    prefix = text[: max(limit, 200)]
    marker_patterns = [
        r"(?m)^\s*(?:#{1,6}\s*)?二\s*[、.．，,：:\s]*$",
        r"(?m)^\s*(?:#{1,6}\s*)?二[、.．，,：:].*$",
        r"(?m)^\s*(?:#{1,6}\s*)?三\s*[、.．，,：:\s]*$",
        r"(?m)^\s*(?:#{1,6}\s*)?(?:填空题|解答题)\b.*$",
    ]
    cut = len(prefix)
    for pattern in marker_patterns:
        match = re.search(pattern, prefix)
        if match and match.start() > 0:
            cut = min(cut, match.start())
    return prefix[:cut].strip()


def objective_answer_line_candidates(text: str) -> list[str]:
    prelude = objective_answer_prelude(text)
    lines = [line.strip() for line in prelude.splitlines() if line.strip()]
    candidates: list[str] = []
    for idx, line in enumerate(lines[:8]):
        question_hits = re.findall(r"(?<!\d)(10|[1-9])\s*[\.．、:：]?\s*[A-DＡ-Ｄa-d]?", line)
        if len(question_hits) >= 2 and is_plausible_objective_answer_line(line):
            candidates.append(line)
            if idx + 1 < len(lines):
                combined = f"{line} {lines[idx + 1].strip()}"
                if is_plausible_objective_answer_line(combined):
                    candidates.append(combined)
    if lines:
        joined = " ".join(lines[:4])
        if is_plausible_objective_answer_line(joined):
            candidates.append(joined)
    return candidates


def parse_objective_answer_line(line: str) -> dict[str, str | None]:
    line = repair_mojibake(line or "").replace("\u3000", " ")
    if not is_plausible_objective_answer_line(line):
        return {}
    parsed: dict[str, str | None] = {}
    range_spans: list[tuple[int, int]] = []

    for match in MineruMarkdownParser.CHOICE_RANGE_RE.finditer(line):
        start = int(match.group(1))
        end = int(match.group(2))
        if not (1 <= start <= end <= 10):
            continue
        letters = re.sub(r"[^A-DＡ-Ｄa-d]", "", match.group(3))
        if len(letters) < end - start + 1:
            continue
        range_spans.append(match.span())
        for offset, no in enumerate(range(start, end + 1)):
            parsed[str(no)] = normalize_choice_letter(letters[offset])

    token_pattern = re.compile(r"(?<!\d)(10|[1-9])\s*[\.．、:：]?\s*([A-DＡ-Ｄa-d])?(?![A-Za-z])")
    matches = list(token_pattern.finditer(line))
    if len(matches) < 2:
        return parsed
    for match in matches:
        if any(start <= match.start() < end or start < match.end() <= end for start, end in range_spans):
            continue
        no = int(match.group(1))
        if not (1 <= no <= 10):
            continue
        choice = match.group(2)
        if choice:
            parsed[str(no)] = normalize_choice_letter(choice)
        elif str(no) not in parsed:
            parsed[str(no)] = None
    return parsed


def score_objective_answer_map(mapping: dict[str, str | None]) -> float:
    score = 0.0
    for no, choice in mapping.items():
        if not (no.isdigit() and 1 <= int(no) <= 10):
            continue
        score += 2.0 if choice else 0.5
    if "1" in mapping:
        score += 0.5
    if "10" in mapping:
        score += 0.5
    return score


def extract_shared_objective_answers(items: list[AnswerItem]) -> tuple[dict[str, str | None], str]:
    best_mapping: dict[str, str | None] = {}
    best_line = ""
    best_score = 0.0
    for item in sorted((row for row in items if row.question_type == "objective"), key=lambda row: int(row.question_no)):
        if item.text.startswith("[未在 MinerU Markdown") or item.text.startswith("OCR objective answer:"):
            continue
        for line in objective_answer_line_candidates(item.text):
            mapping = parse_objective_answer_line(line)
            score = score_objective_answer_map(mapping)
            if score > best_score:
                best_mapping = mapping
                best_line = compact_text(line, limit=500)
                best_score = score
    return best_mapping, best_line


def enrich_objective_items_from_shared_answer_lines(items: list[AnswerItem], audit_log: Path | None = None) -> list[AnswerItem]:
    shared_answers, shared_line = extract_shared_objective_answers(items)
    if not shared_answers:
        return items
    updated: list[AnswerItem] = []
    for item in items:
        if item.question_type != "objective" or item.question_no not in shared_answers:
            updated.append(item)
            continue
        choice = shared_answers[item.question_no]
        text = (
            f"OCR objective answer: {choice}\nShared objective line: {shared_line}"
            if choice
            else f"OCR objective answer: [blank]\nShared objective line: {shared_line}"
        )
        updated.append(
            AnswerItem(
                question_no=item.question_no,
                question_type=item.question_type,
                full_score=item.full_score,
                text=text,
                source_path=item.source_path,
                ocr_confidence=max(float(item.ocr_confidence or 0.0), 0.88),
                needs_ocr_review=False,
                ocr_issues=[],
            )
        )
    if audit_log is not None:
        append_jsonl(
            audit_log,
            {
                "event": "shared_objective_answer_line_parsed",
                "time": utc_now(),
                "line": shared_line,
                "choices": {no: choice for no, choice in shared_answers.items() if choice},
                "blank_or_missing": [no for no, choice in shared_answers.items() if choice is None],
            },
        )
    return sorted(updated, key=lambda item: int(item.question_no))


def has_explicit_objective_blank(text: str) -> bool:
    return bool(re.search(r"OCR objective answer\s*[:：]\s*(?:\[blank\]|blank|未填|空白|无)", text or "", flags=re.I))


def objective_student_text_for_model(item: AnswerItem) -> str:
    prelude = objective_answer_prelude(item.text, limit=900)
    if prelude:
        return compact_text(prelude, limit=900)
    return compact_text(item.text, limit=900)


def local_objective_grade(
    item: AnswerItem,
    reference: dict[str, Any],
    strict_official: bool,
    single_review: bool = False,
) -> dict[str, Any] | None:
    if item.question_type != "objective":
        return None
    evidence_quality = objective_answer_evidence_quality(item.text)
    if evidence_quality != "deterministic":
        return None
    student_choice = extract_objective_choice(item.text, item.question_no)
    reference_choice = extract_objective_choice(str(reference.get("reference_text") or ""), item.question_no)
    explicit_blank = has_explicit_objective_blank(item.text)
    if (not student_choice and not explicit_blank) or not reference_choice:
        return None
    score = item.full_score if student_choice and student_choice == reference_choice else 0.0
    correct = score == item.full_score
    review_reason = None
    needs_review = bool(item.needs_ocr_review)
    if strict_official and not reference.get("is_official"):
        needs_review = True
        review_reason = "Reference is not marked as official"
    if explicit_blank and not student_choice:
        needs_review = bool(strict_official and not reference.get("is_official"))
    round_payload = {
        "question_no": item.question_no,
        "full_score": item.full_score,
        "score": score,
        "student_choice": student_choice,
        "reference_choice": reference_choice,
        "recognized_student_answer": student_choice or "未填写选项",
        "visual_reading_summary": "选择题由客观答案行识别；如有视觉页，可与PDF卷面交叉核对。",
        "evidence_used": "both",
        "earned_points": [f"选择 {student_choice}，与参考答案 {reference_choice} 一致"] if correct else [],
        "deducted_points": [] if correct else [f"{'未填写选项' if not student_choice else f'选择 {student_choice}'}，参考答案为 {reference_choice}"],
        "main_earned_points": ["最终选项正确"] if correct else [],
        "main_deducted_points": [] if correct else ["最终选项空缺" if not student_choice else "最终选项错误"],
        "valid_student_steps": [f"识别到学生最终选项：{student_choice}"] if student_choice else ["识别到本题未填写选择题选项"],
        "wrong_or_missing_steps": [] if correct else [f"应选 {reference_choice}"],
        "needs_human_review": needs_review,
        "review_reason": review_reason,
        "evidence_sources": [str(source.get("source_path", "")) for source in reference.get("sources", []) if source.get("source_path")][:2],
        "confidence": 0.96 if not item.needs_ocr_review else 0.72,
        "reason": "客观题本地判分：按最终选项与参考答案一致性给分；未填写选项按0分。",
        "grading_method": "local_objective_exact_choice",
        "answer_evidence_quality": evidence_quality,
    }
    if single_review:
        result = combine_single_grade(item, round_payload)
        result["grading_round_1"] = dict(round_payload)
        result["grading_round_2"] = None
        result["grading_round_3"] = None
        result["scoring_engine"] = "local_objective"
        return result
    result = combine_grades(item, round_payload, dict(round_payload), None)
    result["grading_round_1"] = dict(round_payload)
    result["grading_round_2"] = dict(round_payload)
    result["grading_round_3"] = None
    result["scoring_engine"] = "local_objective"
    return result


def fallback_objective_batch_grade(
    items: list[AnswerItem],
    reference_bank: ReferenceBank,
    strict_official: bool,
    single_review: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    items = enrich_objective_items_from_shared_answer_lines(items)
    for item in items:
        reference = reference_bank.get(item.question_no)
        result = local_objective_grade(item, reference, strict_official=bool(strict_official), single_review=single_review)
        if result is None:
            result = {
                "question_no": item.question_no,
                "question_type": item.question_type,
                "full_score": item.full_score,
                "third_arbitration_triggered": False,
                "grading_round_1": None,
                "grading_round_2": None,
                "grading_round_3": None,
                "final_score": None,
                "needs_human_review": True,
                "review_reason": "选择题选项或参考答案无法稳定识别",
                "confidence": 0.0,
                "main_earned_points": [],
                "main_deducted_points": [],
                "valid_student_steps": [],
                "wrong_or_missing_steps": [],
            }
        result["student_answer_source"] = {
            "source_path": item.source_path,
            "ocr_confidence": item.ocr_confidence,
            "ocr_issues": item.ocr_issues,
            "student_answer_ocr": objective_student_text_for_model(item) if item.question_type == "objective" else item.text,
            "recognized_student_answer": result.get("recognized_student_answer") or "; ".join(result.get("valid_student_steps") or []),
            "visual_reading_summary": result.get("visual_reading_summary") or "选择题由答案行确定；必要时可对照PDF视觉页。",
            "evidence_used": result.get("evidence_used") or "both",
        }
        result["reference_sources"] = reference.get("sources", [])
        result["question_sources"] = []
        result["visual_sources"] = {"enabled": False, "selection_reason": "not_required_or_unavailable", "pages": []}
        results.append(result)
    return results


def objective_fallback_review_results(
    items: list[AnswerItem],
    reference_bank: ReferenceBank,
    visual_payload: dict[str, Any] | None,
    reason: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        reference = reference_bank.get(item.question_no)
        result = {
            "question_no": item.question_no,
            "question_type": item.question_type,
            "full_score": item.full_score,
            "third_arbitration_triggered": False,
            "grading_round_1": None,
            "grading_round_2": None,
            "grading_round_3": None,
            "final_score": None,
            "recognized_student_answer": "",
            "visual_reading_summary": "选择题需要PDF视觉确认；本次模型视觉批量调用失败，未使用可疑OCR文本判定。",
            "evidence_used": "insufficient",
            "needs_human_review": True,
            "review_reason": reason,
            "confidence": 0.0,
            "main_earned_points": [],
            "main_deducted_points": [],
            "valid_student_steps": [],
            "wrong_or_missing_steps": ["未能稳定确认学生最终选项"],
            "scoring_engine": "objective_visual_review_required",
            "student_answer_source": {
                "source_path": item.source_path,
                "ocr_confidence": item.ocr_confidence,
                "ocr_issues": item.ocr_issues,
                "student_answer_ocr": objective_student_text_for_model(item),
                "recognized_student_answer": "",
                "visual_reading_summary": "模型视觉调用失败；OCR仅作草稿，未作为最终答案。",
                "evidence_used": "insufficient",
            },
            "reference_sources": reference.get("sources", []),
            "question_sources": [],
            "visual_sources": visual_payload or {"enabled": False, "selection_reason": "not_required_or_unavailable", "pages": []},
        }
        results.append(result)
    return results


def normalize_grade(raw: dict[str, Any], item: AnswerItem, fallback_reason: str = "") -> dict[str, Any]:
    raw = repair_text_encoding(raw)
    score = raw.get("score")
    try:
        score = None if score is None else float(score)
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(0.0, min(float(item.full_score), score))
    confidence = raw.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence_used = str(raw.get("evidence_used") or "insufficient").strip().lower()
    recognized_student_answer = truncate_str(str(raw.get("recognized_student_answer") or ""), 500)
    visual_resolves_ocr = (
        item.needs_ocr_review
        and evidence_used in {"visual", "both"}
        and bool(recognized_student_answer.strip())
        and score is not None
        and confidence >= 0.65
        and not bool(raw.get("needs_human_review", False))
    )
    needs_review = bool(raw.get("needs_human_review", score is None))
    if item.needs_ocr_review and not visual_resolves_ocr:
        needs_review = True
    result = {
        "question_no": str(raw.get("question_no") or item.question_no),
        "full_score": item.full_score,
        "score": score,
        "recognized_student_answer": recognized_student_answer,
        "visual_reading_summary": truncate_str(str(raw.get("visual_reading_summary") or ""), 260),
        "evidence_used": evidence_used,
        "earned_points": limit_list(ensure_list(raw.get("earned_points"))),
        "deducted_points": limit_list(ensure_list(raw.get("deducted_points"))),
        "main_earned_points": limit_list(ensure_list(raw.get("main_earned_points"))),
        "main_deducted_points": limit_list(ensure_list(raw.get("main_deducted_points"))),
        "valid_student_steps": limit_list(ensure_list(raw.get("valid_student_steps"))),
        "wrong_or_missing_steps": limit_list(ensure_list(raw.get("wrong_or_missing_steps"))),
        "strict_score_cap_reason": raw.get("strict_score_cap_reason"),
        "strict_scoring_checklist": normalize_strict_checklist(raw.get("strict_scoring_checklist")),
        "needs_human_review": needs_review,
        "review_reason": raw.get("review_reason") or (("; ".join(item.ocr_issues)) if item.needs_ocr_review and not visual_resolves_ocr else None),
        "evidence_sources": limit_list(ensure_list(raw.get("evidence_sources"))),
        "confidence": confidence,
        "reason": truncate_str(raw.get("reason") or fallback_reason, 260),
    }
    apply_strict_solution_caps(result, item)
    return result


def apply_strict_solution_caps(result: dict[str, Any], item: AnswerItem) -> None:
    if item.question_type != "solution":
        return
    score = result.get("score")
    if score is None:
        return
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        return
    full_score = float(item.full_score or 0)
    if full_score <= 0:
        return
    evidence_text = "；".join(
        ensure_list(result.get("deducted_points"))
        + ensure_list(result.get("main_deducted_points"))
        + ensure_list(result.get("wrong_or_missing_steps"))
        + ensure_list(result.get("strict_score_cap_reason"))
        + ensure_list(result.get("reason"))
    )
    recognized = str(result.get("recognized_student_answer") or "")
    valid_steps = "；".join(ensure_list(result.get("valid_student_steps")) + ensure_list(result.get("main_earned_points")))
    checklist = result.get("strict_scoring_checklist") if isinstance(result.get("strict_scoring_checklist"), list) else []
    missing_core_count = sum(1 for row in checklist if str(row.get("status") or "").lower() in {"missing", "wrong", "unclear"} and is_core_checklist_point(row))
    seen_core_count = sum(1 for row in checklist if str(row.get("status") or "").lower() == "seen" and is_core_checklist_point(row))
    cap_ratio: float | None = None
    cap_reason = ""
    severe_patterns = [
        "只有最终答案",
        "仅有最终答案",
        "只写答案",
        "无推导",
        "没有推导",
        "缺少推导",
        "过程缺失",
        "未见过程",
        "空白",
    ]
    key_missing_patterns = [
        "关键步骤缺失",
        "缺少关键",
        "未证明",
        "没有证明",
        "未讨论",
        "没有讨论",
        "边界未",
        "未检边界",
        "未查边界",
        "缺少边界",
        "没有比较边界",
        "端点未",
        "未检端点",
        "缺少端点",
        "定义域未",
        "缺少定义域",
        "条件未",
        "未说明条件",
        "积分区域未",
        "缺少积分区域",
        "上下限未",
        "缺少上下限",
        "漏乘雅可比",
        "缺少雅可比",
        "法向未",
        "方向未",
        "收敛性未",
        "缺少收敛性",
        "未证存在唯一",
        "缺少存在唯一",
        "充分必要性未",
        "未构造似然函数",
        "缺少似然函数",
        "未推导分布函数",
        "缺少分布函数",
        "未写概率密度",
        "独立同分布未",
        "未说明独立同分布",
        "未证明特征值",
        "缺少特征值",
        "未求特征向量",
        "正定性未",
        "合同条件未",
        "相似条件未",
    ]
    wrong_setup_patterns = [
        "主线错误",
        "建模错误",
        "设定错误",
        "公式错误",
        "区域错误",
        "上下限错误",
        "似然函数不正确",
        "分布函数不正确",
        "矩阵性质错误",
        "概念混淆",
    ]
    compact_answer = re.sub(r"\s+", "", recognized)
    compact_valid = re.sub(r"\s+", "", valid_steps)
    if any(pattern in evidence_text for pattern in severe_patterns) or (len(compact_answer) <= 24 and not compact_valid and score_value > full_score * 0.3):
        cap_ratio = 0.3
        cap_reason = "严格大题封顶：卷面只有结论或关键推导不可见。"
    elif missing_core_count >= 2:
        cap_ratio = 0.65
        cap_reason = "严格大题封顶：评分点清单显示多个核心推导点评为缺失、错误或无法辨认。"
    elif missing_core_count == 1 and score_value > full_score * 0.8:
        cap_ratio = 0.8
        cap_reason = "严格大题封顶：评分点清单显示仍有一个核心推导点未被可靠写出。"
    elif score_value > full_score * 0.85 and seen_core_count < 3:
        cap_ratio = 0.8
        cap_reason = "严格大题封顶：高分缺少足够数量的可见核心评分点支撑。"
    elif any(pattern in evidence_text for pattern in wrong_setup_patterns):
        cap_ratio = 0.5
        cap_reason = "严格大题封顶：建模、定限、分布、矩阵性质或主线设置存在关键错误。"
    elif any(pattern in evidence_text for pattern in key_missing_patterns):
        cap_ratio = 0.78
        cap_reason = "严格大题封顶：缺少关键条件检查、证明闭合、边界/端点/区域/分布等推导评分点。"
    if cap_ratio is None:
        return
    cap_score = round(full_score * cap_ratio, 2)
    if score_value <= cap_score:
        return
    result["score"] = cap_score
    result["confidence"] = min(float(result.get("confidence") or 0.0), 0.78)
    result["main_deducted_points"] = limit_list(
        ensure_list(result.get("main_deducted_points")) + [cap_reason],
        max_items=4,
        max_chars=180,
    )
    result["wrong_or_missing_steps"] = limit_list(
        ensure_list(result.get("wrong_or_missing_steps")) + ["按严格解答题口径，高分必须有可见关键步骤支撑。"],
        max_items=4,
        max_chars=180,
    )
    result["reason"] = truncate_str((str(result.get("reason") or "") + " " + cap_reason).strip(), 260)


def normalize_final_score(score: Any, item: AnswerItem) -> float | None:
    if score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    value = max(0.0, min(float(item.full_score), value))
    if item.question_type == "objective":
        return float(item.full_score) if value >= float(item.full_score) * 0.999 else 0.0
    if item.question_type == "blank":
        return float(item.full_score) if value >= float(item.full_score) * 0.999 else 0.0
    if item.question_type == "solution":
        return float(math.floor(value + 1e-9))
    return round(value, 2)


def normalize_result_final_score(result: dict[str, Any], item: AnswerItem) -> dict[str, Any]:
    raw_score = result.get("final_score")
    normalized = normalize_final_score(raw_score, item)
    if raw_score is not None and normalized is not None and abs(float(raw_score) - normalized) > 1e-9:
        result["raw_final_score_before_discrete_rounding"] = round(float(raw_score), 4)
        result["score_discretization_policy"] = (
            "解答题按严格考研阅卷口径向下收为整数分；不使用 7.8、9.36、6.5 等碎小数。"
            if item.question_type == "solution"
            else "客观/填空题按题型规则收为可执行分值，不保留碎小数。"
        )
        if item.question_type == "solution":
            result["main_deducted_points"] = limit_list(
                ensure_list(result.get("main_deducted_points")) + ["最终分按严格阅卷口径向下收整，避免模型平均分产生虚高小数。"],
                max_items=4,
                max_chars=180,
            )
    result["final_score"] = normalized
    return result


def normalize_strict_checklist(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"seen", "missing", "wrong", "unclear"}:
            status = "unclear"
        rows.append(
            {
                "point": truncate_str(str(item.get("point") or ""), 120),
                "status": status,
                "impact": truncate_str(str(item.get("impact") or ""), 120),
            }
        )
    return rows


def is_core_checklist_point(row: dict[str, Any]) -> bool:
    text = f"{row.get('point', '')} {row.get('impact', '')}"
    core_markers = [
        "核心",
        "关键",
        "推导",
        "证明",
        "条件",
        "边界",
        "端点",
        "定义域",
        "区域",
        "上下限",
        "分布",
        "密度",
        "似然",
        "独立",
        "特征值",
        "特征向量",
        "正定",
        "合同",
        "相似",
        "主线",
        "结论",
    ]
    return any(marker in text for marker in core_markers)


def normalize_objective_batch_payload(raw: dict[str, Any], items: list[AnswerItem]) -> dict[str, dict[str, Any]]:
    raw = repair_text_encoding(raw)
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raw_items = raw.get("questions")
    if not isinstance(raw_items, list):
        raw_items = []
    by_no = {item.question_no: item for item in items}
    normalized: dict[str, dict[str, Any]] = {}
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        question_no = str(row.get("question_no") or "").strip()
        if question_no not in by_no:
            continue
        item = by_no[question_no]
        score = row.get("score")
        try:
            score = None if score is None else float(score)
        except (TypeError, ValueError):
            score = None
        if score is not None:
            score = max(0.0, min(item.full_score, score))
        student_choice = normalize_choice_letter(str(row.get("student_choice") or "")) if row.get("student_choice") else None
        reference_choice = normalize_choice_letter(str(row.get("reference_choice") or "")) if row.get("reference_choice") else None
        if student_choice not in {"A", "B", "C", "D"}:
            student_choice = None
        if reference_choice not in {"A", "B", "C", "D"}:
            reference_choice = None
        confidence = row.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized[question_no] = {
            "question_no": question_no,
            "full_score": item.full_score,
            "score": score,
            "recognized_student_answer": truncate_str(str(row.get("recognized_student_answer") or (student_choice or "")), 300),
            "visual_reading_summary": truncate_str(str(row.get("visual_reading_summary") or ""), 220),
            "evidence_used": str(row.get("evidence_used") or "both"),
            "earned_points": limit_list(ensure_list(row.get("earned_points") or row.get("main_earned_points"))),
            "deducted_points": limit_list(ensure_list(row.get("deducted_points") or row.get("main_deducted_points"))),
            "main_earned_points": limit_list(ensure_list(row.get("main_earned_points"))),
            "main_deducted_points": limit_list(ensure_list(row.get("main_deducted_points"))),
            "valid_student_steps": limit_list(ensure_list(row.get("valid_student_steps") or ([f"识别到学生最终选项：{student_choice}"] if student_choice else []))),
            "wrong_or_missing_steps": limit_list(ensure_list(row.get("wrong_or_missing_steps"))),
            "needs_human_review": bool(row.get("needs_human_review", score is None)),
            "review_reason": row.get("review_reason"),
            "evidence_sources": limit_list(ensure_list(row.get("evidence_sources")), max_items=3),
            "confidence": confidence,
            "reason": truncate_str(str(row.get("reason") or "选择题批量判分。"), 220),
            "student_choice": student_choice,
            "reference_choice": reference_choice,
        }
    return normalized


def grade_objective_choices_from_reading(
    choice_by_no: dict[str, dict[str, Any]],
    items: list[AnswerItem],
    references: dict[str, dict[str, Any]],
    strict_official: bool,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for item in items:
        row = choice_by_no.get(item.question_no, {})
        reference = references.get(item.question_no, {})
        student_choice = normalize_choice_letter(str(row.get("student_choice") or "")) if row.get("student_choice") else None
        if student_choice not in {"A", "B", "C", "D"}:
            student_choice = None
        reference_choice = extract_objective_choice(str(reference.get("reference_text") or ""), item.question_no)
        score = item.full_score if student_choice and reference_choice and student_choice == reference_choice else 0.0
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        visual_resolved = bool(
            student_choice
            and str(row.get("evidence_used") or "").strip().lower() in {"visual", "both"}
            and confidence >= 0.65
            and not bool(row.get("needs_human_review"))
        )
        read_review = bool(row.get("needs_human_review") or not student_choice)
        needs_review = bool(read_review or not reference_choice)
        review_reason = row.get("review_reason")
        if not student_choice:
            review_reason = review_reason or "无法稳定识别学生最终选项"
        if not reference_choice:
            review_reason = ((review_reason + "; ") if review_reason else "") + "参考答案无法识别"
        if strict_official and not reference.get("is_official"):
            needs_review = True
            review_reason = ((review_reason + "; ") if review_reason else "") + "Reference is not marked as official"
        correct = bool(student_choice and reference_choice and student_choice == reference_choice)
        results[item.question_no] = {
            "question_no": item.question_no,
            "full_score": item.full_score,
            "score": None if not student_choice or not reference_choice else score,
            "student_choice": student_choice,
            "reference_choice": reference_choice,
            "recognized_student_answer": truncate_str(str(row.get("recognized_student_answer") or (student_choice or "")), 120),
            "visual_reading_summary": truncate_str(str(row.get("visual_reading_summary") or "选择题最终选项由PDF视觉读取。"), 180),
            "evidence_used": str(row.get("evidence_used") or "visual"),
            "earned_points": [f"最终选项 {student_choice} 与参考答案 {reference_choice} 一致"] if correct else [],
            "deducted_points": [] if correct else [f"{'未能确认学生选项' if not student_choice else f'选择 {student_choice}'}；参考答案为 {reference_choice or '未识别'}"],
            "main_earned_points": ["最终选项正确"] if correct else [],
            "main_deducted_points": [] if correct else ["无法确认最终选项" if not student_choice else "最终选项错误"],
            "valid_student_steps": [f"视觉读取学生最终选项：{student_choice}"] if student_choice else [],
            "wrong_or_missing_steps": [] if correct else [f"应选 {reference_choice}"] if reference_choice else [],
            "needs_human_review": bool(needs_review and not visual_resolved),
            "review_reason": review_reason,
            "evidence_sources": limit_list(ensure_list(row.get("evidence_sources")), max_items=3),
            "confidence": confidence if student_choice else min(confidence, 0.45),
            "reason": "选择题客观题：模型只读卷面选项，本地按参考答案精确判分。",
        }
    return results


def ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def limit_list(values: list[str], max_items: int = 4, max_chars: int = 180) -> list[str]:
    return [truncate_str(value, max_chars) for value in values[:max_items]]


def truncate_str(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def round_resolves_ocr_issue(round_payload: dict[str, Any] | None, item: AnswerItem) -> bool:
    if not item.needs_ocr_review or not isinstance(round_payload, dict):
        return not item.needs_ocr_review
    evidence_used = str(round_payload.get("evidence_used") or "").strip().lower()
    recognized = str(round_payload.get("recognized_student_answer") or "").strip()
    score = round_payload.get("score")
    try:
        confidence = float(round_payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        evidence_used in {"visual", "both"}
        and bool(recognized)
        and score is not None
        and confidence >= 0.65
        and not bool(round_payload.get("needs_human_review"))
    )


def stable_question_payload_fingerprint(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def question_result_cache_key(
    item: AnswerItem,
    question_reference: dict[str, Any],
    reference: dict[str, Any],
    rubric: dict[str, Any],
    paper_id: str,
    grading_policy: str,
    strict_official: bool,
    visual_payload: dict[str, Any] | None,
    gateway: ModelGateway,
) -> str:
    payload = {
        "cache_version": QUESTION_RESULT_CACHE_VERSION,
        "question_no": item.question_no,
        "question_type": item.question_type,
        "full_score": item.full_score,
        "student_answer_text": item.text,
        "student_answer_source": item.source_path,
        "question_reference": shrink_reference(question_reference, 2200),
        "reference": shrink_reference(reference, 5000),
        "rubric": rubric,
        "policy_hash": hashlib.sha1((grading_policy or "").encode("utf-8", errors="replace")).hexdigest(),
        "strict_official": strict_official,
        "model": gateway.config.model,
        "api_mode": gateway.config.api_mode,
        "reasoning_effort": gateway.config.reasoning_effort,
        "single_review": gateway.config.single_review,
        "visual_sources": {
            "stable_pages": (visual_payload or {}).get("stable_pages", []),
            "stable_focus_pages": (visual_payload or {}).get("stable_focus_pages", []),
            "selection_reason": (visual_payload or {}).get("selection_reason", ""),
            "layout_scan": (visual_payload or {}).get("layout_scan"),
        },
    }
    return stable_question_payload_fingerprint(payload)


def question_result_cache_path(cache_key: str) -> Path:
    return DEFAULT_RESULT_CACHE_DIR / f"{cache_key}.json"


def read_question_result_cache(cache_key: str, audit_log: Path | None, item: AnswerItem) -> dict[str, Any] | None:
    path = question_result_cache_path(cache_key)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        result = record.get("result") if isinstance(record, dict) else None
        if not isinstance(result, dict):
            return None
        append_jsonl(
            audit_log,
            {
                "event": "question_result_cache_hit",
                "time": utc_now(),
                "question_no": item.question_no,
                "cache_key": cache_key,
                "cache_path": str(path),
            },
        ) if audit_log else None
        return result
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            audit_log,
            {
                "event": "question_result_cache_read_error",
                "time": utc_now(),
                "question_no": item.question_no,
                "cache_key": cache_key,
                "error": f"{type(exc).__name__}: {exc}",
            },
        ) if audit_log else None
        return None


def write_question_result_cache(cache_key: str, result: dict[str, Any], audit_log: Path | None, item: AnswerItem) -> None:
    if result.get("needs_human_review") or result.get("final_score") is None:
        return
    path = question_result_cache_path(cache_key)
    record = {
        "created_at": utc_now(),
        "cache_key": cache_key,
        "cache_version": QUESTION_RESULT_CACHE_VERSION,
        "question_no": item.question_no,
        "result": result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with RESULT_CACHE_LOCK:
        tmp_path.write_text(json.dumps(repair_text_encoding(record), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    append_jsonl(
        audit_log,
        {
            "event": "question_result_cache_write",
            "time": utc_now(),
            "question_no": item.question_no,
            "cache_key": cache_key,
            "cache_path": str(path),
        },
    ) if audit_log else None


def grade_question(
    gateway: ModelGateway,
    item: AnswerItem,
    question_reference: dict[str, Any],
    reference: dict[str, Any],
    rubric: dict[str, Any],
    paper_id: str,
    grading_policy: str,
    visual_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if gateway.config.single_review:
        try:
            first = normalize_grade(
                gateway.call_json(
                    build_grade_messages(item, question_reference, reference, rubric, paper_id, "single_review_fast_mode", grading_policy, visual_payload),
                    f"q{item.question_no}_single",
                ),
                item,
            )
        except Exception as exc:  # noqa: BLE001
            retry_payload = focused_visual_payload(visual_payload)
            if retry_payload is None or retry_payload is visual_payload:
                raise
            append_jsonl(
                gateway.audit_log,
                {
                    "event": "focused_visual_retry_started",
                    "time": utc_now(),
                    "question_no": item.question_no,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "focus_page_count": len(retry_payload.get("focus_pages") or []),
                },
            )
            first = normalize_grade(
                gateway.call_json(
                    build_grade_messages(item, question_reference, reference, rubric, paper_id, "single_review_focused_retry", grading_policy, retry_payload),
                    f"q{item.question_no}_single_focus_retry",
                ),
                item,
            )
        result = combine_single_grade(item, first)
        if should_retry_with_focused_visual(result, visual_payload):
            retry_payload = focused_visual_payload(visual_payload)
            if retry_payload is not None and retry_payload is not visual_payload:
                append_jsonl(
                    gateway.audit_log,
                    {
                        "event": "focused_visual_retry_started",
                        "time": utc_now(),
                        "question_no": item.question_no,
                        "reason": result.get("review_reason") or "first pass needs review",
                        "focus_page_count": len(retry_payload.get("focus_pages") or []),
                    },
                )
                try:
                    retry_first = normalize_grade(
                        gateway.call_json(
                            build_grade_messages(item, question_reference, reference, rubric, paper_id, "single_review_focused_retry", grading_policy, retry_payload),
                            f"q{item.question_no}_single_focus_retry",
                        ),
                        item,
                    )
                    retry_result = combine_single_grade(item, retry_first)
                    retry_result["grading_round_1"] = retry_first
                    retry_result["grading_round_2"] = None
                    retry_result["grading_round_3"] = None
                    retry_result["focused_visual_retry"] = True
                    if focused_retry_is_better(result, retry_result):
                        return retry_result
                except Exception as exc:  # noqa: BLE001
                    append_jsonl(
                        gateway.audit_log,
                        {
                            "event": "focused_visual_retry_error",
                            "time": utc_now(),
                            "question_no": item.question_no,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
        result["grading_round_1"] = first
        result["grading_round_2"] = None
        result["grading_round_3"] = None
        return result
    parallel_rounds = not (visual_payload and visual_payload.get("enabled")) or gateway.config.parallel_visual_rounds
    if parallel_rounds:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                gateway.call_json,
                build_grade_messages(item, question_reference, reference, rubric, paper_id, "first_independent_review", grading_policy, visual_payload),
                f"q{item.question_no}_round1",
            )
            second_future = executor.submit(
                gateway.call_json,
                build_grade_messages(item, question_reference, reference, rubric, paper_id, "second_independent_review", grading_policy, visual_payload),
                f"q{item.question_no}_round2",
            )
            first = normalize_grade(first_future.result(), item)
            second = normalize_grade(second_future.result(), item)
    else:
        first = normalize_grade(
            gateway.call_json(
                build_grade_messages(item, question_reference, reference, rubric, paper_id, "first_independent_review", grading_policy, visual_payload),
                f"q{item.question_no}_round1",
            ),
            item,
        )
        second = normalize_grade(
            gateway.call_json(
                build_grade_messages(item, question_reference, reference, rubric, paper_id, "second_independent_review", grading_policy, visual_payload),
                f"q{item.question_no}_round2",
            ),
            item,
        )
    result = combine_grades(item, first, second, None)
    if result["third_arbitration_triggered"]:
        third = normalize_grade(
            gateway.call_json(build_arbitration_messages(item, question_reference, reference, rubric, paper_id, first, second, grading_policy, visual_payload), f"q{item.question_no}_round3"),
            item,
        )
        result = combine_grades(item, first, second, third)
    result["grading_round_1"] = first
    result["grading_round_2"] = second
    return result


def combine_single_grade(item: AnswerItem, first: dict[str, Any]) -> dict[str, Any]:
    score = first.get("score")
    ocr_resolved = round_resolves_ocr_issue(first, item)
    result = {
        "question_no": item.question_no,
        "question_type": item.question_type,
        "full_score": item.full_score,
        "third_arbitration_triggered": False,
        "grading_round_3": None,
        "final_score": None if score is None else round(float(score), 2),
        "recognized_student_answer": first.get("recognized_student_answer") or "",
        "visual_reading_summary": first.get("visual_reading_summary") or "",
        "evidence_used": first.get("evidence_used") or "",
        "needs_human_review": bool(first.get("needs_human_review") or (item.needs_ocr_review and not ocr_resolved) or score is None),
        "review_reason": first.get("review_reason"),
        "confidence": float(first.get("confidence", 0.0)),
        "main_earned_points": merge_lists(first.get("main_earned_points")),
        "main_deducted_points": merge_lists(first.get("main_deducted_points")),
        "valid_student_steps": merge_lists(first.get("valid_student_steps")),
        "wrong_or_missing_steps": merge_lists(first.get("wrong_or_missing_steps")),
        "strict_scoring_checklist": first.get("strict_scoring_checklist") or [],
    }
    return normalize_result_final_score(result, item)


def should_retry_with_focused_visual(result: dict[str, Any], visual_payload: dict[str, Any] | None) -> bool:
    if not visual_payload or not visual_payload.get("focus_pages"):
        return False
    if result.get("final_score") is None:
        return True
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence_used = str(result.get("evidence_used") or "").strip().lower()
    if result.get("needs_human_review") and confidence < 0.8:
        return True
    return evidence_used == "insufficient" or confidence < 0.58


def focused_retry_is_better(original: dict[str, Any], retry_result: dict[str, Any]) -> bool:
    if retry_result.get("final_score") is not None and original.get("final_score") is None:
        return True
    if original.get("needs_human_review") and not retry_result.get("needs_human_review"):
        return True
    try:
        old_confidence = float(original.get("confidence", 0.0))
        new_confidence = float(retry_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        return False
    return new_confidence >= old_confidence + 0.12 and retry_result.get("final_score") is not None


def attach_common_result_metadata(
    result: dict[str, Any],
    item: AnswerItem,
    reference: dict[str, Any],
    question_reference: dict[str, Any] | None = None,
    visual_sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result["student_answer_source"] = {
        "source_path": item.source_path,
        "ocr_confidence": item.ocr_confidence,
        "ocr_issues": item.ocr_issues,
        "student_answer_ocr": objective_student_text_for_model(item) if item.question_type == "objective" else item.text,
        "recognized_student_answer": result.get("recognized_student_answer") or "",
        "visual_reading_summary": result.get("visual_reading_summary") or "",
        "evidence_used": result.get("evidence_used") or ("visual" if visual_sources and visual_sources.get("enabled") else "mineru_ocr"),
    }
    result["reference_sources"] = reference.get("sources", [])
    result["question_sources"] = (question_reference or {}).get("sources", [])
    if visual_sources is not None:
        result["visual_sources"] = visual_sources
    return result


def grade_objective_batch_for_run(
    items: list[AnswerItem],
    gateway: ModelGateway,
    reference_bank: ReferenceBank,
    paper_id: str,
    strict_official: bool,
    visual_pages: list[VisualPage],
) -> list[dict[str, Any]]:
    if not items:
        return []
    visual_payload = visual_payload_for_objective_batch(items, visual_pages)
    local_results = fallback_objective_batch_grade(
        items,
        reference_bank,
        strict_official,
        single_review=gateway.config.single_review,
    )
    local_ready = all(not result.get("needs_human_review") for result in local_results)
    if local_ready and not visual_payload.get("enabled"):
        append_jsonl(
            gateway.audit_log,
            {
                "event": "objective_batch_local_short_circuit",
                "time": utc_now(),
                "question_nos": [item.question_no for item in items],
                "reason": "All objective choices were deterministically parsed before model review.",
            },
        )
        return local_results
    if not gateway.config.objective_batch_mode:
        return local_results
    batch_gateway = gateway.for_reasoning_effort(gateway.config.objective_reasoning_effort or gateway.config.reasoning_effort)
    references = {item.question_no: reference_bank.get(item.question_no) for item in items}
    append_jsonl(
        batch_gateway.audit_log,
        {
            "event": "objective_batch_started",
            "time": utc_now(),
            "question_nos": [item.question_no for item in items],
            "question_count": len(items),
            "reasoning_effort": batch_gateway.config.reasoning_effort,
            "visual_enabled": bool(visual_payload.get("enabled")),
        },
    )
    try:
        if batch_gateway.config.single_review:
            choice_by_no = normalize_objective_batch_payload(
                batch_gateway.call_json(
                    build_objective_batch_messages(items, references, paper_id, "single_review_fast_mode", visual_payload),
                    "objective_batch_single",
                ),
                items,
            )
            first_by_no = grade_objective_choices_from_reading(choice_by_no, items, references, strict_official)
            second_by_no: dict[str, dict[str, Any]] = {}
        else:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    batch_gateway.call_json,
                    build_objective_batch_messages(items, references, paper_id, "first_independent_review", visual_payload),
                    "objective_batch_round1",
                )
                second_future = executor.submit(
                    batch_gateway.call_json,
                    build_objective_batch_messages(items, references, paper_id, "second_independent_review", visual_payload),
                    "objective_batch_round2",
                )
                first_choices_by_no = normalize_objective_batch_payload(first_future.result(), items)
                second_choices_by_no = normalize_objective_batch_payload(second_future.result(), items)
                first_by_no = grade_objective_choices_from_reading(first_choices_by_no, items, references, strict_official)
                second_by_no = grade_objective_choices_from_reading(second_choices_by_no, items, references, strict_official)
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            batch_gateway.audit_log,
            {
                "event": "objective_batch_failed",
                "time": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
                "fallback_policy": "use deterministic local answer lines only; otherwise require visual human review",
            },
        )
        fallback_results = fallback_objective_batch_grade(items, reference_bank, strict_official, single_review=gateway.config.single_review)
        uncertain_items = [item for item, result in zip(items, fallback_results) if result.get("needs_human_review")]
        if not uncertain_items:
            return fallback_results
        review_by_no = {
            row["question_no"]: row
            for row in objective_fallback_review_results(
                uncertain_items,
                reference_bank,
                visual_payload,
                reason=f"选择题视觉批量模型调用失败，且未发现可信客观答案行: {type(exc).__name__}: {exc}",
            )
        }
        return [review_by_no.get(result["question_no"], result) for result in fallback_results]

    results: list[dict[str, Any]] = []
    for item in items:
        first = first_by_no.get(item.question_no)
        reference = references.get(item.question_no, {})
        if first is None:
            fallback = fallback_objective_batch_grade([item], reference_bank, strict_official, single_review=gateway.config.single_review)[0]
            results.append(fallback)
            continue
        if batch_gateway.config.single_review:
            result = combine_single_grade(item, first)
            result["grading_round_1"] = first
            result["grading_round_2"] = None
            result["grading_round_3"] = None
        else:
            second = second_by_no.get(item.question_no)
            if second is None:
                fallback = fallback_objective_batch_grade([item], reference_bank, strict_official, single_review=False)[0]
                results.append(fallback)
                continue
            result = combine_grades(item, first, second, None)
            if result["third_arbitration_triggered"]:
                try:
                    third_by_no = normalize_objective_batch_payload(
                        batch_gateway.call_json(
                            build_objective_batch_arbitration_messages(
                                [item],
                                {item.question_no: reference},
                                paper_id,
                                {item.question_no: first},
                                {item.question_no: second},
                                visual_payload,
                            ),
                            f"q{item.question_no}_objective_batch_round3",
                        ),
                        [item],
                    )
                    third = third_by_no.get(item.question_no)
                    if third is not None:
                        result = combine_grades(item, first, second, third)
                    else:
                        result["needs_human_review"] = True
                        result["review_reason"] = result.get("review_reason") or "选择题批量仲裁未返回本题结果"
                except Exception as exc:  # noqa: BLE001
                    result["needs_human_review"] = True
                    result["review_reason"] = result.get("review_reason") or f"选择题批量仲裁失败: {type(exc).__name__}: {exc}"
            result["grading_round_1"] = first
            result["grading_round_2"] = second
            result.setdefault("grading_round_3", None)
        result["scoring_engine"] = "model_objective_batch"
        result["student_answer_source"] = {
            "source_path": item.source_path,
            "ocr_confidence": item.ocr_confidence,
            "ocr_issues": item.ocr_issues,
            "student_answer_ocr": objective_student_text_for_model(item),
        }
        result["reference_sources"] = reference.get("sources", [])
        result["question_sources"] = []
        result["visual_sources"] = visual_payload
        if strict_official and not reference.get("is_official"):
            result["needs_human_review"] = True
            result["review_reason"] = ((result.get("review_reason") + "; ") if result.get("review_reason") else "") + "Reference is not marked as official"
        results.append(result)
    append_jsonl(
        batch_gateway.audit_log,
        {
            "event": "objective_batch_finished",
            "time": utc_now(),
            "question_nos": [item.question_no for item in items],
            "result_count": len(results),
        },
    )
    return results


def combine_grades(
    item: AnswerItem,
    first: dict[str, Any],
    second: dict[str, Any],
    third: dict[str, Any] | None,
) -> dict[str, Any]:
    s1 = first.get("score")
    s2 = second.get("score")
    base: dict[str, Any] = {
        "question_no": item.question_no,
        "question_type": item.question_type,
        "full_score": item.full_score,
        "third_arbitration_triggered": False,
        "grading_round_3": None,
        "final_score": None,
        "needs_human_review": False,
        "review_reason": None,
        "confidence": min(float(first.get("confidence", 0)), float(second.get("confidence", 0))),
        "recognized_student_answer": first.get("recognized_student_answer") or second.get("recognized_student_answer") or "",
        "visual_reading_summary": first.get("visual_reading_summary") or second.get("visual_reading_summary") or "",
        "evidence_used": first.get("evidence_used") or second.get("evidence_used") or "",
        "main_earned_points": merge_lists(first.get("main_earned_points"), second.get("main_earned_points")),
        "main_deducted_points": merge_lists(first.get("main_deducted_points"), second.get("main_deducted_points")),
        "valid_student_steps": merge_lists(first.get("valid_student_steps"), second.get("valid_student_steps")),
        "wrong_or_missing_steps": merge_lists(first.get("wrong_or_missing_steps"), second.get("wrong_or_missing_steps")),
        "strict_scoring_checklist": merge_checklists(first.get("strict_scoring_checklist"), second.get("strict_scoring_checklist")),
    }
    if s1 is None or s2 is None:
        base["third_arbitration_triggered"] = True
        if third is None:
            return base
    elif abs(s1 - s2) <= 2:
        base["final_score"] = round((s1 + s2) / 2, 2)
        ocr_resolved = round_resolves_ocr_issue(first, item) or round_resolves_ocr_issue(second, item)
        base["needs_human_review"] = bool(first.get("needs_human_review") or second.get("needs_human_review") or (item.needs_ocr_review and not ocr_resolved))
        base["review_reason"] = first.get("review_reason") or second.get("review_reason")
        return normalize_result_final_score(base, item)
    else:
        base["third_arbitration_triggered"] = True
        if third is None:
            return base

    assert third is not None
    base["grading_round_3"] = third
    s3 = third.get("score")
    if s3 is None:
        base["needs_human_review"] = True
        base["review_reason"] = third.get("review_reason") or "Third arbitration returned no score"
        return base
    candidates: list[tuple[float, float, str]] = []
    if s1 is not None:
        candidates.append((abs(s3 - s1), (s3 + s1) / 2, "round_1"))
    if s2 is not None:
        candidates.append((abs(s3 - s2), (s3 + s2) / 2, "round_2"))
    close = [candidate for candidate in candidates if candidate[0] <= 2]
    if not close:
        base["needs_human_review"] = True
        base["review_reason"] = "Third arbitration differs from both previous scores by more than 2 points"
        return base
    close.sort(key=lambda row: row[0])
    base["final_score"] = round(close[0][1], 2)
    base["confidence"] = min(base["confidence"], float(third.get("confidence", 0)))
    base["needs_human_review"] = bool(
        first.get("needs_human_review")
        or second.get("needs_human_review")
        or third.get("needs_human_review")
        or (item.needs_ocr_review and not (round_resolves_ocr_issue(first, item) or round_resolves_ocr_issue(second, item) or round_resolves_ocr_issue(third, item)))
    )
    base["review_reason"] = third.get("review_reason") or first.get("review_reason") or second.get("review_reason")
    base["main_earned_points"] = merge_lists(base["main_earned_points"], third.get("main_earned_points"))
    base["main_deducted_points"] = merge_lists(base["main_deducted_points"], third.get("main_deducted_points"))
    base["valid_student_steps"] = merge_lists(base["valid_student_steps"], third.get("valid_student_steps"))
    base["wrong_or_missing_steps"] = merge_lists(base["wrong_or_missing_steps"], third.get("wrong_or_missing_steps"))
    base["strict_scoring_checklist"] = merge_checklists(base.get("strict_scoring_checklist"), third.get("strict_scoring_checklist"))
    return normalize_result_final_score(base, item)


def merge_checklists(*values: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        for row in normalize_strict_checklist(value):
            key = (row.get("point") or "").strip()
            if key and key not in seen:
                seen.add(key)
                rows.append(row)
            if len(rows) >= 8:
                return rows
    return rows


def merge_lists(*values: Any) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in ensure_list(value):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def build_report(
    run_id: str,
    paper_id: str,
    candidate_name: str,
    submission_paths: list[Path],
    reference_paths: list[Path],
    parsed_items: list[AnswerItem],
    question_results: list[dict[str, Any]],
) -> dict[str, Any]:
    total = sum(float(item.get("final_score") or 0) for item in question_results)
    full_total = sum(float(item.full_score) for item in parsed_items if any(r["question_no"] == item.question_no for r in question_results))
    review_questions = [item["question_no"] for item in question_results if item.get("needs_human_review")]
    module_scores = summarize_modules(question_results)
    return {
        "run_id": run_id,
        "generated_at": utc_now(),
        "candidate": {"name": candidate_name},
        "paper": {
            "paper_id": paper_id,
            "submission_paths": [str(path) for path in submission_paths],
            "reference_paths": [str(path) for path in reference_paths],
            "total_full_score_for_graded_questions": full_total,
        },
        "score_summary": {
            "total_score_for_graded_questions": round(total, 2),
            "graded_question_count": len(question_results),
            "human_review_count": len(review_questions),
        },
        "module_scores": module_scores,
        "question_scores": question_results,
        "review_required_questions": review_questions,
        "audit_log_summary": {
            "ocr_low_confidence_count": sum(1 for item in parsed_items if item.needs_ocr_review),
            "arbitration_count": sum(1 for item in question_results if item.get("third_arbitration_triggered")),
        },
    }


def summarize_modules(question_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        "objective": {"score": 0.0, "full_score": 0.0},
        "blank": {"score": 0.0, "full_score": 0.0},
        "solution": {"score": 0.0, "full_score": 0.0},
    }
    for item in question_results:
        bucket = buckets.setdefault(item.get("question_type", "unknown"), {"score": 0.0, "full_score": 0.0})
        bucket["score"] += float(item.get("final_score") or 0)
        bucket["full_score"] += float(item.get("full_score") or 0)
    labels = {"objective": "选择题", "blank": "填空题", "solution": "解答题"}
    return [
        {
            "module": labels.get(key, key),
            "score": round(value["score"], 2),
            "full_score": round(value["full_score"], 2),
        }
        for key, value in buckets.items()
        if value["full_score"] > 0
    ]


def load_submission_items(paths: list[Path]) -> list[AnswerItem]:
    parser = MineruMarkdownParser()
    by_no: dict[str, AnswerItem] = {}
    for path in paths:
        for item in parser.parse_submission(path):
            existing = by_no.get(item.question_no)
            if existing is None or len(item.text) > len(existing.text):
                by_no[item.question_no] = item
    return sorted(by_no.values(), key=lambda item: int(item.question_no))


def add_missing_items_from_references(
    items: list[AnswerItem],
    question_bank: ReferenceBank,
    reference_bank: ReferenceBank,
    source_path: str,
) -> list[AnswerItem]:
    by_no = {item.question_no: item for item in items}
    all_nos = set(question_bank.by_question) | set(reference_bank.by_question)
    for question_no in sorted(all_nos, key=lambda no: int(no)):
        if question_no in by_no:
            continue
        by_no[question_no] = AnswerItem(
            question_no=question_no,
            question_type=infer_question_type(question_no),
            full_score=infer_full_score(question_no),
            text=f"[未在 MinerU Markdown 中识别到第 {question_no} 题学生作答；请优先查看原始PDF视觉证据。]",
            source_path=source_path,
            ocr_confidence=0.0,
            needs_ocr_review=True,
            ocr_issues=["Student answer was not segmented by MinerU; visual PDF review required"],
        )
    return sorted(by_no.values(), key=lambda item: int(item.question_no))


def grade_one_item_for_run(
    item: AnswerItem,
    gateway: ModelGateway,
    question_bank: ReferenceBank,
    reference_bank: ReferenceBank,
    paper_id: str,
    grading_policy: str,
    strict_official: bool,
    visual_pages: list[VisualPage],
    answer_layout: AnswerLayout | None = None,
) -> dict[str, Any]:
    item_gateway = gateway.for_reasoning_effort(reasoning_effort_for_item(gateway.config, item))
    reference = reference_bank.get(item.question_no)
    question_reference = question_bank.get(item.question_no, limit=9000)
    if strict_official and not reference.get("is_official"):
        reference["strict_official_warning"] = "Reference is missing or not marked official; score must be treated as review-required."
    rubric = rubric_for(item, reference, strict_official=bool(strict_official))
    local_result = local_objective_grade(item, reference, strict_official=bool(strict_official))
    item_visual_payload = visual_payload_for_item(item, visual_pages, layout=answer_layout)
    result_cache_key = None
    if item_gateway.config.use_cache:
        result_cache_key = question_result_cache_key(
            item,
            question_reference,
            reference,
            rubric,
            paper_id,
            grading_policy,
            bool(strict_official),
            item_visual_payload,
            item_gateway,
        )
        cached_result = read_question_result_cache(result_cache_key, item_gateway.audit_log, item)
        if cached_result is not None:
            attach_common_result_metadata(
                cached_result,
                item,
                reference,
                question_reference=question_reference,
                visual_sources=item_visual_payload,
            )
            return cached_result
    if local_result is not None and not item_visual_payload.get("enabled"):
        append_jsonl(
            item_gateway.audit_log,
            {
                "event": "local_objective_grade",
                "time": utc_now(),
                "question_no": item.question_no,
                "final_score": local_result.get("final_score"),
                "full_score": item.full_score,
                "reason": local_result.get("grading_round_1", {}).get("reason"),
            },
        )
        result = local_result
    else:
        visual_payload = item_visual_payload
        if visual_payload.get("enabled"):
            append_jsonl(
                item_gateway.audit_log,
                {
                    "event": "visual_evidence_attached",
                    "time": utc_now(),
                    "question_no": item.question_no,
                    "pages": visual_payload.get("pages", []),
                    "reasoning_effort": item_gateway.config.reasoning_effort,
                },
            )
        try:
            result = grade_question(item_gateway, item, question_reference, reference, rubric, paper_id, grading_policy, visual_payload)
        except Exception as exc:  # noqa: BLE001
            result = {
                "question_no": item.question_no,
                "question_type": item.question_type,
                "full_score": item.full_score,
                "third_arbitration_triggered": False,
                "grading_round_1": None,
                "grading_round_2": None,
                "grading_round_3": None,
                "final_score": None,
                "needs_human_review": True,
                "review_reason": f"Model grading failed: {type(exc).__name__}: {exc}",
                "confidence": 0.0,
                "main_earned_points": [],
                "main_deducted_points": [],
                "valid_student_steps": [],
                "wrong_or_missing_steps": [],
            }
    if strict_official and not reference.get("is_official"):
        result["needs_human_review"] = True
        result["review_reason"] = (
            (result.get("review_reason") + "; ") if result.get("review_reason") else ""
        ) + "Reference is not marked as official"
    attach_common_result_metadata(
        result,
        item,
        reference,
        question_reference=question_reference,
        visual_sources=item_visual_payload,
    )
    if result_cache_key:
        write_question_result_cache(result_cache_key, result, item_gateway.audit_log, item)
    return result


def reasoning_effort_for_item(config: ModelConfig, item: AnswerItem) -> str:
    if item.question_type == "objective":
        return config.objective_reasoning_effort or config.reasoning_effort
    if item.question_type == "blank":
        return config.blank_reasoning_effort or config.reasoning_effort
    return config.solution_reasoning_effort or config.reasoning_effort


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype automatic grader for scanned postgraduate math papers.")
    parser.add_argument("--submission-pdf", action="append", default=[], help="Student submission PDF. Can be repeated.")
    parser.add_argument("--question-paper-pdf", action="append", default=[], help="Question paper PDF. Converted/reused internally. Can be repeated.")
    parser.add_argument("--reference-pdf", action="append", default=[], help="Reference/solution PDF. Converted/reused internally. Can be repeated.")
    parser.add_argument("--reference-source-type", default="provided_reference", help="Source type label for references.")
    parser.add_argument("--reference-is-official", action="store_true", help="Mark references as official sources.")
    parser.add_argument("--paper-id", default="unknown-paper")
    parser.add_argument("--candidate-name", default="unknown-candidate")
    parser.add_argument("--questions", default=None, help="Question filter, e.g. 1-3,17,18.")
    parser.add_argument("--limit", type=int, default=None, help="Grade only the first N parsed questions after filtering.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--api-url", default=os.getenv("GRADER_API_URL", ""))
    parser.add_argument("--model", default=os.getenv("GRADER_MODEL", "gpt-5.5"))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("GRADER_TIMEOUT_SECONDS", "420")))
    parser.add_argument("--max-retries", type=int, default=int(os.getenv("GRADER_MAX_RETRIES", "3")))
    parser.add_argument("--temperature", type=float, default=float(os.getenv("GRADER_TEMPERATURE", "0.1")))
    parser.add_argument("--api-mode", choices=["chat", "responses"], default=os.getenv("GRADER_API_MODE", "responses"), help="Model API style. Use responses when the gateway supports /v1/responses.")
    parser.add_argument("--max-output-tokens", type=int, default=int(os.getenv("GRADER_MAX_OUTPUT_TOKENS", "100000")), help="Maximum output tokens per model call. Use 0 to omit the limit.")
    parser.add_argument("--reasoning-effort", default=os.getenv("GRADER_REASONING_EFFORT", "xhigh"), help="Reasoning effort for supported GPT-5.5 gateways, default xhigh.")
    parser.add_argument("--objective-reasoning-effort", default=os.getenv("GRADER_OBJECTIVE_REASONING_EFFORT", "high"), help="Reasoning effort for objective questions if model review is needed.")
    parser.add_argument("--blank-reasoning-effort", default=os.getenv("GRADER_BLANK_REASONING_EFFORT", "high"), help="Reasoning effort for blank questions.")
    parser.add_argument("--solution-reasoning-effort", default=os.getenv("GRADER_SOLUTION_REASONING_EFFORT", "high"), help="Reasoning effort for solution questions.")
    default_parallel_visual = os.getenv("GRADER_PARALLEL_VISUAL_ROUNDS", "1").lower() not in {"0", "false", "no", "off"}
    parser.add_argument("--parallel-visual-rounds", dest="parallel_visual_rounds", action="store_true", default=default_parallel_visual, help="Run the two independent visual review rounds concurrently.")
    parser.add_argument("--sequential-visual-rounds", dest="parallel_visual_rounds", action="store_false", help="Run visual review rounds sequentially to reduce gateway pressure.")
    default_objective_batch = os.getenv("GRADER_OBJECTIVE_BATCH_MODE", "1").lower() not in {"0", "false", "no", "off"}
    parser.add_argument("--objective-batch-mode", dest="objective_batch_mode", action="store_true", default=default_objective_batch, help="Grade objective questions as one batch task with two independent model rounds.")
    parser.add_argument("--per-question-objective", dest="objective_batch_mode", action="store_false", help="Grade objective questions individually or locally instead of using one batch model task.")
    default_single_review = os.getenv("GRADER_SINGLE_REVIEW", "1").lower() not in {"0", "false", "no", "off"}
    parser.add_argument("--single-review", dest="single_review", action="store_true", default=default_single_review, help="Use one model review per task for fast debugging.")
    parser.add_argument("--double-review", dest="single_review", action="store_false", help="Use two independent reviews plus arbitration.")
    default_stream = os.getenv("GRADER_USE_STREAM", "1").lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--stream", dest="use_stream", action="store_true", default=default_stream, help="Use streaming HTTP responses when the gateway supports it.")
    parser.add_argument("--no-stream", dest="use_stream", action="store_false", help="Disable streaming HTTP responses.")
    parser.add_argument("--cache-dir", default=os.getenv("GRADER_CACHE_DIR", str(DEFAULT_CACHE_DIR)), help="Directory for reusable model response cache.")
    parser.add_argument("--use-cache", action="store_true", default=os.getenv("GRADER_USE_CACHE", "1").lower() in {"1", "true", "yes", "on"}, help="Enable local model response cache for repeated debugging.")
    parser.add_argument("--no-cache", action="store_true", help="Disable local model response cache.")
    parser.add_argument("--strict-official", action="store_true", help="Require official references; otherwise mark missing/non-official cases for review.")
    parser.add_argument("--parse-only", action="store_true", help="Only parse inputs and write parsed JSON; do not call the model.")
    parser.add_argument("--run-id", default=None, help="Optional stable run id. Used by the local web UI.")
    parser.add_argument("--policy-path", default=str(DEFAULT_POLICY_PATH), help="Global grading policy distilled from exam analysis.")
    default_layout_scan = os.getenv("GRADER_LAYOUT_SCAN", "1").lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--layout-scan", dest="layout_scan", action="store_true", default=default_layout_scan, help="Run one xhigh whole-paper visual pass to locate each student's answer order and answer boxes before grading.")
    parser.add_argument("--no-layout-scan", dest="layout_scan", action="store_false", help="Disable the whole-paper visual layout pre-scan and use heuristic page selection.")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("GRADER_CONCURRENCY", "10")), help="Total grading task concurrency.")
    parser.add_argument("--blank-concurrency", type=int, default=int(os.getenv("GRADER_BLANK_CONCURRENCY", "3")), help="Maximum blank questions to grade concurrently.")
    parser.add_argument("--solution-concurrency", type=int, default=int(os.getenv("GRADER_SOLUTION_CONCURRENCY", "6")), help="Maximum solution questions to grade concurrently.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    run_id = args.run_id or (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("--run-id may contain only letters, numbers, underscore, dot, and hyphen")
    run_dir = Path(args.output_dir) / run_id
    audit_log = run_dir / "audit_log.jsonl"
    if not args.submission_pdf:
        raise ValueError("Provide at least one --submission-pdf PDF.")
    if not args.question_paper_pdf:
        raise ValueError("Provide at least one --question-paper-pdf PDF.")
    if not args.reference_pdf:
        raise ValueError("Provide at least one --reference-pdf PDF.")
    for raw_path in args.submission_pdf + args.question_paper_pdf + args.reference_pdf:
        if not Path(raw_path).resolve().exists():
            raise FileNotFoundError(Path(raw_path).resolve())
    submission_paths, submission_pdf_paths = resolve_markdown_inputs(
        [],
        args.submission_pdf,
        role="submission",
        run_dir=run_dir,
        audit_log=audit_log,
    )
    question_paper_paths, question_paper_pdf_paths = resolve_markdown_inputs(
        [],
        args.question_paper_pdf,
        role="question_paper",
        run_dir=run_dir,
        audit_log=audit_log,
    )
    reference_paths, reference_pdf_paths = resolve_markdown_inputs(
        [],
        args.reference_pdf,
        role="reference",
        run_dir=run_dir,
        audit_log=audit_log,
    )
    if not submission_paths:
        raise ValueError("Unable to parse any student submission PDF.")
    if not question_paper_paths:
        raise ValueError("Unable to parse any question paper PDF.")
    if not reference_paths:
        raise ValueError("Unable to parse any reference/solution PDF.")
    for path in submission_paths + submission_pdf_paths + question_paper_paths + question_paper_pdf_paths + reference_paths + reference_pdf_paths:
        if not path.exists():
            raise FileNotFoundError(path)

    append_jsonl(
        audit_log,
        {
            "event": "run_started",
            "time": utc_now(),
            "run_id": run_id,
            "paper_id": args.paper_id,
            "submission_paths": [str(path) for path in submission_paths],
            "submission_pdf_paths": [str(path) for path in submission_pdf_paths],
            "question_paper_paths": [str(path) for path in question_paper_paths],
            "question_paper_pdf_paths": [str(path) for path in question_paper_pdf_paths],
            "reference_paths": [str(path) for path in reference_paths],
            "reference_pdf_paths": [str(path) for path in reference_pdf_paths],
        },
    )

    reference_bank = ReferenceBank.from_paths(
        reference_paths,
        source_type=args.reference_source_type,
        is_official=bool(args.reference_is_official),
    )
    question_bank = ReferenceBank.from_paths(
        question_paper_paths,
        source_type="question_paper",
        is_official=False,
    )
    parsed_items = load_submission_items(submission_paths)
    parsed_items = add_missing_items_from_references(
        parsed_items,
        question_bank=question_bank,
        reference_bank=reference_bank,
        source_path="; ".join(str(path) for path in submission_paths + submission_pdf_paths),
    )
    parsed_items = enrich_objective_items_from_shared_answer_lines(parsed_items, audit_log=audit_log)
    selected = parse_question_filter(args.questions)
    if selected is not None:
        parsed_items = [item for item in parsed_items if item.question_no in selected]
    if args.limit is not None:
        parsed_items = parsed_items[: args.limit]
    write_json(run_dir / "parsed_answers.json", [asdict(item) for item in parsed_items])
    visual_pages = render_pdf_pages(submission_pdf_paths, run_dir=run_dir, audit_log=audit_log) if submission_pdf_paths else []
    write_json(run_dir / "visual_pages.json", [asdict(page) for page in visual_pages])
    policy_path = Path(args.policy_path) if args.policy_path else None
    grading_policy = read_optional_text(policy_path, limit=12000)
    cache_enabled = bool(args.use_cache) and not bool(args.no_cache)
    write_json(
        run_dir / "reference_index.json",
        {
            "reference_count": len(reference_bank.references),
            "questions": sorted(reference_bank.by_question.keys(), key=lambda no: int(no)),
            "paths": [str(path) for path in reference_paths],
            "submission_pdf_paths": [str(path) for path in submission_pdf_paths],
            "visual_page_count": len(visual_pages),
            "question_paper_paths": [str(path) for path in question_paper_paths],
            "question_paper_pdf_paths": [str(path) for path in question_paper_pdf_paths],
            "reference_pdf_paths": [str(path) for path in reference_pdf_paths],
            "source_type": args.reference_source_type,
            "is_official": bool(args.reference_is_official),
            "policy_path": str(policy_path) if policy_path else "",
            "policy_loaded": bool(grading_policy),
            "api_mode": args.api_mode,
            "max_output_tokens": args.max_output_tokens,
            "reasoning_effort": args.reasoning_effort,
            "objective_reasoning_effort": args.objective_reasoning_effort,
            "blank_reasoning_effort": args.blank_reasoning_effort,
            "solution_reasoning_effort": args.solution_reasoning_effort,
            "parallel_visual_rounds": bool(args.parallel_visual_rounds),
            "objective_batch_mode": bool(args.objective_batch_mode),
            "single_review": bool(args.single_review),
            "layout_scan": bool(args.layout_scan),
            "blank_concurrency": args.blank_concurrency,
            "solution_concurrency": args.solution_concurrency,
            "cache_enabled": cache_enabled,
            "cache_dir": args.cache_dir,
            "question_result_cache_dir": str(DEFAULT_RESULT_CACHE_DIR),
            "question_result_cache_version": QUESTION_RESULT_CACHE_VERSION,
        },
    )

    if args.parse_only:
        print(str(run_dir))
        return 0

    api_key = os.getenv("GRADER_API_KEY", "")

    gateway = ModelGateway(
        ModelConfig(
            api_url=args.api_url,
            api_key=api_key,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_retries=args.max_retries,
            temperature=args.temperature,
            api_mode=args.api_mode,
            use_cache=cache_enabled,
            cache_dir=args.cache_dir,
            max_output_tokens=args.max_output_tokens,
            reasoning_effort=args.reasoning_effort,
            objective_reasoning_effort=args.objective_reasoning_effort,
            blank_reasoning_effort=args.blank_reasoning_effort,
            solution_reasoning_effort=args.solution_reasoning_effort,
            parallel_visual_rounds=bool(args.parallel_visual_rounds),
            objective_batch_mode=bool(args.objective_batch_mode),
            single_review=bool(args.single_review),
            use_stream=bool(args.use_stream),
        ),
        audit_log=audit_log,
    )

    answer_layout: AnswerLayout | None = None
    if bool(args.layout_scan) and visual_pages:
        answer_layout = scan_answer_layout(
            gateway=gateway,
            items=parsed_items,
            visual_pages=visual_pages,
            paper_id=args.paper_id,
            audit_log=audit_log,
        )
    write_json(
        run_dir / "answer_layout.json",
        {
            "enabled": bool(args.layout_scan),
            "available": answer_layout is not None,
            "source": answer_layout.source if answer_layout else "",
            "global_notes": answer_layout.global_notes if answer_layout else "",
            "items": answer_layout.items if answer_layout else {},
        },
    )

    question_results: list[dict[str, Any]] = []
    max_workers = max(1, int(args.concurrency))
    objective_items = [item for item in parsed_items if item.question_type == "objective"]
    blank_items = [item for item in parsed_items if item.question_type == "blank"]
    solution_items = [item for item in parsed_items if item.question_type == "solution"]
    if args.objective_batch_mode:
        extra_objective_items: list[AnswerItem] = []
    else:
        extra_objective_items = objective_items
    grading_task_count = len(extra_objective_items) + len(blank_items) + len(solution_items) + (1 if args.objective_batch_mode and objective_items else 0)
    review_multiplier = 1 if args.single_review else (2 if args.parallel_visual_rounds else 1)
    effective_task_concurrency = min(
        max_workers,
        (1 if args.objective_batch_mode and objective_items else 0)
        + min(max(1, int(args.blank_concurrency)), len(blank_items))
        + min(max(1, int(args.solution_concurrency)), len(solution_items))
        + len(extra_objective_items),
    )
    estimated_peak_model_calls = effective_task_concurrency * review_multiplier
    append_jsonl(
        audit_log,
        {
            "event": "grading_dispatch_started",
            "time": utc_now(),
            "question_count": len(parsed_items),
            "grading_task_count": grading_task_count,
            "question_concurrency": max_workers,
            "blank_concurrency": args.blank_concurrency,
            "solution_concurrency": args.solution_concurrency,
            "single_review": bool(args.single_review),
            "rounds_per_question_parallel": 1 if args.single_review else 2,
            "estimated_peak_model_calls": estimated_peak_model_calls,
            "api_mode": args.api_mode,
            "max_output_tokens": args.max_output_tokens,
            "reasoning_effort": args.reasoning_effort,
            "objective_reasoning_effort": args.objective_reasoning_effort,
            "blank_reasoning_effort": args.blank_reasoning_effort,
            "solution_reasoning_effort": args.solution_reasoning_effort,
            "parallel_visual_rounds": bool(args.parallel_visual_rounds),
            "objective_batch_mode": bool(args.objective_batch_mode),
            "single_review": bool(args.single_review),
            "layout_scan": bool(args.layout_scan),
            "layout_scan_available": answer_layout is not None,
            "cache_enabled": cache_enabled,
            "cache_dir": args.cache_dir,
            "question_result_cache_dir": str(DEFAULT_RESULT_CACHE_DIR),
            "stream_enabled": bool(args.use_stream),
            "visual_page_count": len(visual_pages),
        },
    )
    all_futures: dict[Any, AnswerItem | list[AnswerItem]] = {}
    objective_executor = ThreadPoolExecutor(max_workers=1)
    blank_executor = ThreadPoolExecutor(max_workers=min(max(1, int(args.blank_concurrency)), max(1, len(blank_items))))
    solution_executor = ThreadPoolExecutor(max_workers=min(max(1, int(args.solution_concurrency)), max(1, len(solution_items))))
    extra_objective_executor = ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(extra_objective_items))))
    executors = [objective_executor, blank_executor, solution_executor, extra_objective_executor]
    try:
        if args.objective_batch_mode and objective_items:
            all_futures[
                objective_executor.submit(
                    grade_objective_batch_for_run,
                    objective_items,
                    gateway,
                    reference_bank,
                    args.paper_id,
                    bool(args.strict_official),
                    visual_pages,
                )
            ] = objective_items
        for item in extra_objective_items:
            all_futures[
                extra_objective_executor.submit(
                    grade_one_item_for_run,
                    item,
                    gateway,
                    question_bank,
                    reference_bank,
                    args.paper_id,
                    grading_policy,
                    bool(args.strict_official),
                    visual_pages,
                    answer_layout,
                )
            ] = item
        for item in blank_items:
            all_futures[
                blank_executor.submit(
                    grade_one_item_for_run,
                    item,
                    gateway,
                    question_bank,
                    reference_bank,
                    args.paper_id,
                    grading_policy,
                    bool(args.strict_official),
                    visual_pages,
                    answer_layout,
                )
            ] = item
        for item in solution_items:
            all_futures[
                solution_executor.submit(
                    grade_one_item_for_run,
                    item,
                    gateway,
                    question_bank,
                    reference_bank,
                    args.paper_id,
                    grading_policy,
                    bool(args.strict_official),
                    visual_pages,
                    answer_layout,
                )
            ] = item
        for future in as_completed(all_futures):
            task_item = all_futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                if isinstance(task_item, list):
                    result = [
                        {
                            "question_no": item.question_no,
                            "question_type": item.question_type,
                            "full_score": item.full_score,
                            "final_score": None,
                            "needs_human_review": True,
                            "review_reason": f"Objective batch worker failed: {type(exc).__name__}: {exc}",
                            "confidence": 0.0,
                        }
                        for item in task_item
                    ]
                else:
                    result = {
                        "question_no": task_item.question_no,
                        "question_type": task_item.question_type,
                        "full_score": task_item.full_score,
                        "final_score": None,
                        "needs_human_review": True,
                        "review_reason": f"Question worker failed: {type(exc).__name__}: {exc}",
                        "confidence": 0.0,
                    }
            if isinstance(result, list):
                question_results.extend(result)
            else:
                question_results.append(result)
            question_results.sort(key=lambda row: int(row.get("question_no", 9999)))
            write_json(
                run_dir / "partial_report.json",
                {
                    "run_id": run_id,
                    "completed_question_count": len(question_results),
                    "total_question_count": len(parsed_items),
                    "question_scores": question_results,
                },
            )
    finally:
        for executor_item in executors:
            executor_item.shutdown(wait=True, cancel_futures=False)

    report = build_report(
        run_id=run_id,
        paper_id=args.paper_id,
        candidate_name=args.candidate_name,
        submission_paths=submission_paths,
        reference_paths=reference_paths,
        parsed_items=parsed_items,
        question_results=question_results,
    )
    write_json(run_dir / "grading_report.json", report)
    append_jsonl(audit_log, {"event": "run_finished", "time": utc_now(), "run_id": run_id})
    print(str(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
