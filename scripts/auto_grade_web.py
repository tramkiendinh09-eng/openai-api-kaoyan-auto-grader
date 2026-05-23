#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "auto_grade_exam.py"
MINERU_DIR = ROOT / "tmp" / "mineru_grading"
OUTPUT_DIR = ROOT / "output" / "auto_grading"
DEFAULT_POLICY_PATH = ROOT / "config" / "kaoyan_math_grading_policy.md"


TASKS: dict[str, "TaskState"] = {}
TASK_LOCK = threading.Lock()


@dataclass
class TaskState:
    run_id: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    command_preview: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    output_dir: str = ""
    error: str | None = None
    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, status: int = 200, content_type: str = "text/plain") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_request_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def list_md_files() -> list[dict[str, str]]:
    roots = [MINERU_DIR, ROOT / "考情分析", ROOT / "output" / "markdown"]
    rows: list[dict[str, str]] = []
    for base in roots:
        if not base.exists():
            continue
        for path in sorted(base.glob("*.md")):
            rows.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "group": str(base.relative_to(ROOT)) if base.is_relative_to(ROOT) else str(base),
                }
            )
    return rows


def list_pdf_files() -> list[dict[str, str]]:
    roots = [MINERU_DIR, *extra_search_dirs()]
    rows: list[dict[str, str]] = []
    for base in roots:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.pdf")):
            rows.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "group": str(base.relative_to(ROOT)) if base.is_relative_to(ROOT) else str(base),
                }
            )
    return rows


def extra_search_dirs() -> list[Path]:
    raw = os.getenv("GRADER_EXTRA_SEARCH_DIRS", "")
    if not raw.strip():
        return []
    return [Path(part).expanduser() for part in re.split(r"[;\n]", raw) if part.strip()]


def require_local_path(raw: str) -> Path:
    if not raw:
        raise ValueError("missing path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    else:
        path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def sanitize_run_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return value[:80] or f"web_{uuid.uuid4().hex[:8]}"


def start_grading_task(payload: dict) -> TaskState:
    submission_raw = str(payload.get("submission") or "").strip()
    submission = require_local_path(submission_raw) if submission_raw else None
    submission_pdf_raw = str(payload.get("submission_pdf") or "").strip()
    submission_pdf = require_local_path(submission_pdf_raw) if submission_pdf_raw else None
    question_paper_raw = str(payload.get("question_paper") or "").strip()
    question_paper = require_local_path(question_paper_raw) if question_paper_raw else None
    question_paper_pdf_raw = str(payload.get("question_paper_pdf") or "").strip()
    question_paper_pdf = require_local_path(question_paper_pdf_raw) if question_paper_pdf_raw else None
    reference_raw = str(payload.get("reference") or "").strip()
    reference = require_local_path(reference_raw) if reference_raw else None
    reference_pdf_raw = str(payload.get("reference_pdf") or "").strip()
    reference_pdf = require_local_path(reference_pdf_raw) if reference_pdf_raw else None
    api_url = str(payload.get("api_url") or "").strip()
    model = str(payload.get("model") or "gpt-5.5").strip()
    api_key = str(payload.get("api_key") or "").strip()
    questions = str(payload.get("questions") or "").strip()
    paper_id = str(payload.get("paper_id") or "local-web-paper").strip()
    candidate_name = str(payload.get("candidate_name") or "local-candidate").strip()
    parse_only = bool(payload.get("parse_only"))
    api_mode = str(payload.get("api_mode") or "responses").strip()
    use_cache = bool(payload.get("use_cache", True))
    reasoning_effort = str(payload.get("reasoning_effort") or "xhigh").strip()
    objective_reasoning_effort = str(payload.get("objective_reasoning_effort") or "high").strip()
    blank_reasoning_effort = str(payload.get("blank_reasoning_effort") or "high").strip()
    solution_reasoning_effort = str(payload.get("solution_reasoning_effort") or "high").strip()
    layout_scan = bool(payload.get("layout_scan", True))
    parallel_visual_rounds = bool(payload.get("parallel_visual_rounds", True))
    objective_batch_mode = bool(payload.get("objective_batch_mode", True))
    single_review = bool(payload.get("single_review", True))
    use_stream = bool(payload.get("use_stream", True))
    max_retries = int(payload.get("max_retries") or 3)
    timeout_seconds = int(payload.get("timeout_seconds") or 180)
    max_output_tokens = int(payload.get("max_output_tokens") or 100000)
    concurrency = int(payload.get("concurrency") or 10)
    blank_concurrency = int(payload.get("blank_concurrency") or 3)
    solution_concurrency = int(payload.get("solution_concurrency") or 6)
    reference_is_official = bool(payload.get("reference_is_official"))
    strict_official = bool(payload.get("strict_official"))
    policy_path = require_local_path(str(payload.get("policy_path") or str(DEFAULT_POLICY_PATH)))

    if submission is None and submission_pdf is None:
        raise ValueError("请至少选择一份学生答卷 PDF")
    if reference is None and reference_pdf is None:
        raise ValueError("请至少选择一份参考答案 PDF")

    only_objective_questions = re.fullmatch(r"\s*(?:10|[1-9])(?:\s*(?:,|-)\s*(?:10|[1-9]))*\s*", questions or "")
    objective_visual_required = bool(only_objective_questions and submission_pdf)
    model_required = not parse_only and (not only_objective_questions or objective_visual_required)
    if model_required and not api_url:
        raise ValueError("api_url is required unless parse_only is enabled or objective questions can be graded from a trusted answer line without PDF vision")
    if model_required and not api_key:
        raise ValueError("api_key is required unless parse_only is enabled or objective questions can be graded from a trusted answer line without PDF vision")

    run_id = sanitize_run_id(f"web_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--paper-id",
        paper_id,
        "--candidate-name",
        candidate_name,
        "--api-url",
        api_url,
        "--model",
        model,
        "--api-mode",
        api_mode,
        "--reasoning-effort",
        reasoning_effort,
        "--objective-reasoning-effort",
        objective_reasoning_effort,
        "--blank-reasoning-effort",
        blank_reasoning_effort,
        "--solution-reasoning-effort",
        solution_reasoning_effort,
        "--max-retries",
        str(max_retries),
        "--timeout-seconds",
        str(timeout_seconds),
        "--max-output-tokens",
        str(max_output_tokens),
        "--concurrency",
        str(concurrency),
        "--blank-concurrency",
        str(blank_concurrency),
        "--solution-concurrency",
        str(solution_concurrency),
        "--run-id",
        run_id,
        "--policy-path",
        str(policy_path),
    ]
    if submission is not None:
        command.extend(["--submission", str(submission)])
    if submission_pdf is not None:
        command.extend(["--submission-pdf", str(submission_pdf)])
    if question_paper is not None:
        command.extend(["--question-paper", str(question_paper)])
    if question_paper_pdf is not None:
        command.extend(["--question-paper-pdf", str(question_paper_pdf)])
    if reference is not None:
        command.extend(["--reference", str(reference)])
    if reference_pdf is not None:
        command.extend(["--reference-pdf", str(reference_pdf)])
    if questions:
        command.extend(["--questions", questions])
    if parse_only:
        command.append("--parse-only")
    if use_cache:
        command.append("--use-cache")
    else:
        command.append("--no-cache")
    if reference_is_official:
        command.append("--reference-is-official")
    if strict_official:
        command.append("--strict-official")
    if parallel_visual_rounds:
        command.append("--parallel-visual-rounds")
    else:
        command.append("--sequential-visual-rounds")
    if objective_batch_mode:
        command.append("--objective-batch-mode")
    else:
        command.append("--per-question-objective")
    if single_review:
        command.append("--single-review")
    else:
        command.append("--double-review")
    if use_stream:
        command.append("--stream")
    else:
        command.append("--no-stream")
    if layout_scan:
        command.append("--layout-scan")
    else:
        command.append("--no-layout-scan")

    env = os.environ.copy()
    if api_key:
        env["GRADER_API_KEY"] = api_key
    env["PYTHONIOENCODING"] = "utf-8"

    state = TaskState(
        run_id=run_id,
        command_preview=["python", "scripts/auto_grade_exam.py", "...", "--run-id", run_id],
        output_dir=str(OUTPUT_DIR / run_id),
    )
    with TASK_LOCK:
        TASKS[run_id] = state
    thread = threading.Thread(target=run_command, args=(state, command, env), daemon=True)
    thread.start()
    return state


def run_command(state: TaskState, command: list[str], env: dict[str, str]) -> None:
    state.status = "running"
    state.started_at = time.time()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        state.process = process
        stdout, stderr = process.communicate()
        state.returncode = process.returncode
        state.stdout = (stdout or "")[-12000:]
        state.stderr = (stderr or "")[-12000:]
        if state.status == "stopping":
            state.status = "failed"
            state.error = "任务已手动停止"
            return
        state.status = "finished" if process.returncode == 0 else "failed"
    except Exception as exc:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        state.process = None
        state.finished_at = time.time()


def stop_grading_task(run_id: str) -> TaskState:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("bad run id")
    with TASK_LOCK:
        state = TASKS.get(run_id)
    if state is None:
        raise ValueError("task not found")
    if state.status not in {"queued", "running"}:
        return state
    state.status = "stopping"
    process = state.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return state


def load_report(run_id: str, filename: str) -> object:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("bad run id")
    allowed = {"grading_report.json", "partial_report.json", "parsed_answers.json", "reference_index.json"}
    if filename not in allowed:
        raise ValueError("bad report filename")
    path = OUTPUT_DIR / run_id / filename
    if not path.exists():
        return {"available": False, "path": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def load_audit_tail(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("bad run id")
    path = OUTPUT_DIR / run_id / "audit_log.jsonl"
    if not path.exists():
        return {"available": False, "path": str(path), "events": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line})
    return {"available": True, "path": str(path), "events": events, "progress": audit_progress(events)}


def audit_progress(events: list[dict]) -> dict:
    latest_question = None
    latest_round = None
    latest_event = None
    latest_elapsed = None
    latest_error = None
    completed_calls = 0
    active_calls = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        name = event.get("event")
        if name in {"visual_evidence_attached", "local_objective_grade"} and event.get("question_no"):
            latest_question = str(event.get("question_no"))
            latest_round = "准备视觉阅卷" if name == "visual_evidence_attached" else "本地客观题判分"
            latest_event = name
        elif name == "shared_objective_answer_line_parsed":
            latest_question = "1-10"
            latest_round = "选择题答案行已拆分"
            latest_event = name
        elif name == "objective_batch_local_short_circuit":
            latest_question = "1-10"
            latest_round = "选择题本地确定判分"
            latest_event = name
        elif name == "question_result_cache_hit":
            latest_question = str(event.get("question_no") or latest_question or "")
            latest_round = "单题结果缓存命中"
            latest_event = name
        elif name == "answer_layout_scan_started":
            latest_question = "整卷"
            latest_round = "xhigh 视觉定位作答顺序已发起"
            latest_event = name
        elif name == "answer_layout_scan_finished":
            latest_question = "整卷"
            latest_round = f"视觉定位完成，定位 {event.get('located_question_count', 0)} 题"
            latest_event = name
        elif name == "answer_layout_scan_failed":
            latest_question = "整卷"
            latest_round = "视觉定位失败，改用旧的页码启发式"
            latest_event = name
            latest_error = str(event.get("error") or "")
        elif name in {"model_cache_hit"}:
            call_name = str(event.get("call_name") or "")
            match = re.search(r"q(\d+)_(?:round(\d+)|single)", call_name)
            latest_question = match.group(1) if match else ("整卷" if call_name == "answer_layout_scan" else "1-10" if call_name.startswith("objective_batch_") else latest_question)
            latest_round = "模型响应缓存命中"
            latest_event = name
        elif name in {"model_call_started", "model_call", "model_call_error"}:
            call_name = str(event.get("call_name") or "")
            if name == "model_call_started":
                active_calls += 1
            else:
                completed_calls += 1
                active_calls = max(0, active_calls - 1)
            match = re.search(r"q(\d+)_(?:round(\d+)|single)", call_name)
            if match:
                latest_question = match.group(1)
                round_no = match.group(2)
                latest_round = f"第 {round_no} 轮模型评分" if round_no else "单评模型评分"
                if name == "model_call_started":
                    latest_round += "已发起"
            elif call_name.startswith("objective_batch_"):
                latest_question = "1-10"
                if call_name == "objective_batch_single":
                    latest_round = "选择题视觉读选项并本地判分"
                else:
                    round_no = call_name.replace("objective_batch_round", "")
                    latest_round = f"选择题视觉读选项第 {round_no} 轮"
                if name == "model_call_started":
                    latest_round += "已发起"
            elif call_name == "answer_layout_scan":
                latest_question = "整卷"
                latest_round = "xhigh 视觉定位作答顺序"
                if name == "model_call_started":
                    latest_round += "已发起"
            latest_event = name
            if event.get("elapsed_seconds") is not None:
                latest_elapsed = event.get("elapsed_seconds")
            if event.get("header_elapsed_seconds") is not None and event.get("elapsed_seconds") is not None:
                latest_round = (latest_round or "模型调用") + f"（首包 {event.get('header_elapsed_seconds')}s）"
            if event.get("error"):
                latest_error = str(event.get("error"))
    text = "等待任务开始"
    if latest_question:
        text = f"最近处理第 {latest_question} 题"
        if latest_round:
            text += f"，{latest_round}"
        if latest_elapsed is not None:
            text += f"，上次调用 {latest_elapsed}s"
    if latest_error:
        text += f"，最近错误：{latest_error[:120]}"
    if completed_calls:
        text += f"，模型调用完成 {completed_calls} 次"
    if active_calls:
        text += f"，活跃调用约 {active_calls} 次"
    return {
        "latest_question": latest_question,
        "latest_round": latest_round,
        "latest_event": latest_event,
        "latest_elapsed_seconds": latest_elapsed,
        "latest_error": latest_error,
        "completed_model_calls": completed_calls,
        "active_model_calls": active_calls,
        "text": text,
    }


def latest_runs() -> list[dict[str, str]]:
    if not OUTPUT_DIR.exists():
        return []
    rows = []
    for path in sorted(OUTPUT_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)[:20]:
        if not path.is_dir():
            continue
        if path.name == "web_server":
            continue
        report = path / "grading_report.json"
        rows.append(
            {
                "run_id": path.name,
                "path": str(path),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
                "has_report": report.exists(),
            }
        )
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                text_response(self, INDEX_HTML, content_type="text/html")
            elif parsed.path == "/api/files":
                json_response(
                    self,
                    {
                        "files": list_md_files(),
                        "pdf_files": list_pdf_files(),
                        "root": str(ROOT),
                        "default_policy_path": str(DEFAULT_POLICY_PATH),
                        "policy_exists": DEFAULT_POLICY_PATH.exists(),
                    },
                )
            elif parsed.path == "/api/runs":
                json_response(self, {"runs": latest_runs(), "tasks": [task_to_dict(task) for task in TASKS.values()]})
            elif parsed.path == "/api/task":
                query = parse_qs(parsed.query)
                run_id = query.get("run_id", [""])[0]
                with TASK_LOCK:
                    task = TASKS.get(run_id)
                if not task:
                    json_response(self, {"found": False}, status=404)
                else:
                    json_response(self, {"found": True, "task": task_to_dict(task)})
            elif parsed.path == "/api/report":
                query = parse_qs(parsed.query)
                run_id = query.get("run_id", [""])[0]
                filename = query.get("file", ["grading_report.json"])[0]
                json_response(self, load_report(run_id, filename))
            elif parsed.path == "/api/audit":
                query = parse_qs(parsed.query)
                run_id = query.get("run_id", [""])[0]
                json_response(self, load_audit_tail(run_id))
            else:
                text_response(self, "Not found", status=404)
        except Exception as exc:  # noqa: BLE001
            json_response(self, {"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/stop":
                payload = read_request_json(self)
                run_id = str(payload.get("run_id") or "").strip()
                state = stop_grading_task(run_id)
                json_response(self, {"ok": True, "task": task_to_dict(state)})
                return
            if parsed.path != "/api/grade":
                text_response(self, "Not found", status=404)
                return
            payload = read_request_json(self)
            state = start_grading_task(payload)
            json_response(self, {"ok": True, "task": task_to_dict(state)})
        except Exception as exc:  # noqa: BLE001
            json_response(self, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)


def task_to_dict(task: TaskState) -> dict:
    return {
        "run_id": task.run_id,
        "status": task.status,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "returncode": task.returncode,
        "command_preview": task.command_preview,
        "stdout": task.stdout,
        "stderr": task.stderr,
        "output_dir": task.output_dir,
        "error": task.error,
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>考研数学自动阅卷本地原型</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #202124;
      --muted: #626a73;
      --line: #d9dee5;
      --panel: #ffffff;
      --bg: #f5f7fa;
      --accent: #0f766e;
      --accent-dark: #0b5f59;
      --danger: #b42318;
      --warn: #946200;
      --ok: #16794c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
      font-size: 14px;
      line-height: 1.45;
    }
    header {
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    main {
      padding: 20px 24px 32px;
      display: grid;
      grid-template-columns: minmax(360px, 480px) minmax(520px, 1fr);
      gap: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    label { display: block; font-weight: 600; margin: 12px 0 6px; }
    input, select, textarea, button {
      font: inherit;
    }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: #fff;
      min-height: 38px;
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .row.triple { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .row.triple label { margin-top: 0; }
    .checks { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
    .checks label { margin: 0; display: flex; align-items: center; gap: 6px; font-weight: 500; }
    .checks input { width: auto; min-height: 0; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      min-height: 38px;
    }
    button:hover { background: var(--accent-dark); }
    button.secondary { background: #e7eef4; color: var(--ink); }
    button.secondary:hover { background: #d9e4ed; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .actions { display: flex; gap: 10px; margin-top: 16px; }
    button.danger { background: var(--danger); }
    button.danger:hover { background: #8f1d14; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      background: #fff;
      color: var(--muted);
      white-space: nowrap;
    }
    .status.running { color: var(--warn); border-color: #f1c56b; background: #fff8e5; }
    .status.finished { color: var(--ok); border-color: #95d5b2; background: #effaf4; }
    .status.failed { color: var(--danger); border-color: #f0a3a0; background: #fff1f0; }
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }
    .report-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(90px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfd;
    }
    .metric strong { display: block; font-size: 18px; }
    .metric span { color: var(--muted); font-size: 12px; }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      margin-top: 8px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px;
      text-align: left;
      vertical-align: top;
      word-break: break-word;
    }
    th { background: #f8fafc; font-weight: 700; }
    pre {
      margin: 0;
      padding: 12px;
      background: #111827;
      color: #eef2ff;
      border-radius: 8px;
      overflow: auto;
      max-height: 360px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .tabs { display: flex; gap: 8px; margin: 14px 0 10px; }
    .tabs button { background: #e7eef4; color: var(--ink); }
    .tabs button.active { background: var(--accent); color: white; }
    .run-list {
      margin-top: 14px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .run-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid #edf0f4;
    }
    .run-item button { padding: 6px 9px; min-height: 30px; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; padding: 14px; }
      .report-grid { grid-template-columns: repeat(2, 1fr); }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>考研数学自动阅卷本地原型</h1>
      <div class="hint" id="rootHint">正在读取本地文件...</div>
    </div>
    <div class="status" id="statusBadge">未运行</div>
  </header>
  <main>
    <section>
      <h2>阅卷配置</h2>
      <label for="submissionPdf">学生答卷 PDF</label>
      <select id="submissionPdf"></select>
      <label for="questionPaperPdf">试卷题目 PDF</label>
      <select id="questionPaperPdf"></select>
      <label for="referencePdf">参考答案 / 评分依据 PDF</label>
      <select id="referencePdf"></select>
      <div class="hint">只需要选择 PDF；系统会在后台优先复用 MinerU 解析结果，缺失时自动抽取 PDF 文本并把卷面图片交给 GPT 视觉复核。</div>
      <div class="row">
        <div>
          <label for="questions">题号</label>
          <input id="questions" value="1-22" placeholder="例如 1-10,11-16,17-22" />
        </div>
        <div>
          <label for="model">模型</label>
          <input id="model" value="gpt-5.5" />
        </div>
      </div>
      <label for="apiUrl">API URL</label>
      <input id="apiUrl" value="" placeholder="例如 https://your-gateway.example.com/v1" />
      <label for="apiMode">调用方式</label>
      <select id="apiMode">
        <option value="responses" selected>/v1/responses</option>
        <option value="chat">/v1/chat/completions</option>
      </select>
      <div class="hint">Responses 作为默认主通道；解答题会结合自动解析文本与原始 PDF 页图进行视觉复核。</div>
      <label>分题型推理强度</label>
      <div class="row triple">
        <div>
          <label for="objectiveReasoning">选择题</label>
          <select id="objectiveReasoning">
            <option value="high" selected>high</option>
            <option value="xhigh">xhigh</option>
            <option value="medium">medium</option>
            <option value="low">low</option>
          </select>
        </div>
        <div>
          <label for="blankReasoning">填空题</label>
          <select id="blankReasoning">
            <option value="high" selected>high</option>
            <option value="xhigh">xhigh</option>
            <option value="medium">medium</option>
            <option value="low">low</option>
          </select>
        </div>
        <div>
          <label for="solutionReasoning">解答题</label>
          <select id="solutionReasoning">
            <option value="high" selected>high</option>
            <option value="xhigh">xhigh</option>
            <option value="medium">medium</option>
            <option value="low">low</option>
          </select>
        </div>
      </div>
      <div class="hint">默认先用 1 次 xhigh 视觉总览定位每题作答顺序和区域；后续每题用 high 结合 MinerU OCR、局部视觉图和 PDF skill 阅卷。</div>
      <label for="apiKey">API Key</label>
      <input id="apiKey" type="password" placeholder="只在本地进程中用于本次调用，不写入文件" />
      <label for="policyPath">全局阅卷规范</label>
      <input id="policyPath" value="" />
      <div class="hint">默认使用项目内蒸馏的考研数学阅卷规范，作为每次评分的固定上下文。</div>
      <div class="row">
        <div>
          <label for="paperId">试卷 ID</label>
          <input id="paperId" value="2026-05-fudan-math" />
        </div>
        <div>
          <label for="candidate">考生名</label>
          <input id="candidate" value="本地测试" />
        </div>
      </div>
      <div class="row">
        <div>
          <label for="timeout">单次超时秒数</label>
          <input id="timeout" type="number" value="300" min="20" max="900" />
        </div>
        <div>
          <label for="retries">重试次数</label>
          <input id="retries" type="number" value="3" min="1" max="6" />
        </div>
      </div>
      <label for="maxOutputTokens">单次最大输出 tokens</label>
      <input id="maxOutputTokens" type="number" value="100000" min="200" max="200000" />
      <label for="concurrency">题目并发数</label>
      <input id="concurrency" type="number" value="10" min="1" max="10" />
      <div class="row">
        <div>
          <label for="blankConcurrency">填空并发</label>
          <input id="blankConcurrency" type="number" value="3" min="1" max="6" />
        </div>
        <div>
          <label for="solutionConcurrency">大题并发</label>
          <input id="solutionConcurrency" type="number" value="6" min="1" max="6" />
        </div>
      </div>
      <div class="hint">当前为 10 路单评：选择题批任务 1 路，填空最多 3 路，大题最多 6 路。</div>
      <div class="checks">
        <label><input id="parseOnly" type="checkbox" /> 只解析不调用模型</label>
        <label><input id="useCache" type="checkbox" checked /> 调试缓存</label>
        <label><input id="useStream" type="checkbox" checked /> 流式接收</label>
        <label><input id="layoutScan" type="checkbox" checked /> 先整卷视觉定位</label>
        <label><input id="objectiveBatchMode" type="checkbox" checked /> 选择题10题批量</label>
        <label><input id="singleReview" type="checkbox" checked /> 单评极速</label>
        <label><input id="parallelVisualRounds" type="checkbox" /> 双评时同题两轮并发</label>
        <label><input id="refOfficial" type="checkbox" /> 参考材料标为官方</label>
        <label><input id="strictOfficial" type="checkbox" /> 严格官方依据</label>
      </div>
      <div class="actions">
        <button id="startBtn">开始阅卷</button>
        <button class="danger" id="stopBtn" disabled>停止任务</button>
        <button class="secondary" id="refreshBtn">刷新文件</button>
      </div>
      <div class="hint">全卷默认 1-22；先做整卷视觉导航，再按选择题批任务、填空题、大题三组并发推进。</div>
      <div class="run-list">
        <h2>最近结果</h2>
        <div id="runs"></div>
      </div>
    </section>
    <section>
      <div class="toolbar">
        <h2>运行结果</h2>
        <div id="runId" class="hint"></div>
      </div>
      <div class="report-grid">
        <div class="metric"><strong id="totalScore">-</strong><span>总分</span></div>
        <div class="metric"><strong id="gradedCount">-</strong><span>已评题数</span></div>
        <div class="metric"><strong id="reviewCount">-</strong><span>需复核</span></div>
        <div class="metric"><strong id="arbCount">-</strong><span>仲裁次数</span></div>
      </div>
      <div id="progressBox" class="hint">等待任务开始</div>
      <div id="summary"></div>
      <div class="tabs">
        <button class="active" data-tab="table">得分表</button>
        <button data-tab="json">报告 JSON</button>
        <button data-tab="audit">日志摘要</button>
      </div>
      <div id="tablePanel"></div>
      <pre id="jsonPanel" style="display:none"></pre>
      <pre id="auditPanel" style="display:none"></pre>
    </section>
  </main>
  <script>
    let currentRunId = null;
    let pollTimer = null;
    let activeTab = "table";

    const $ = (id) => document.getElementById(id);

    function setStatus(text, cls) {
      const el = $("statusBadge");
      el.textContent = text;
      el.className = "status" + (cls ? " " + cls : "");
    }

    async function api(path, options) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    async function loadFiles() {
      const data = await api("/api/files");
      $("rootHint").textContent = "项目目录：" + data.root;
      $("policyPath").value = data.default_policy_path || "";
      fillSelect($("submissionPdf"), data.pdf_files || [], "2702124-数学-复旦大学.pdf");
      fillSelect($("questionPaperPdf"), data.pdf_files || [], "五月数学模考(3).pdf");
      fillSelect($("referencePdf"), data.pdf_files || [], "五月数学模考答案(1).pdf");
      await loadRuns();
    }

    function fillSelect(select, files, preferredName) {
      const old = select.value;
      select.innerHTML = "";
      for (const file of files) {
        const option = document.createElement("option");
        option.value = file.path;
        option.textContent = `[${file.group}] ${file.name}`;
        select.appendChild(option);
        if (file.name === preferredName) option.selected = true;
      }
      if (old) select.value = old;
    }

    async function loadRuns() {
      const data = await api("/api/runs");
      const box = $("runs");
      box.innerHTML = "";
      for (const run of data.runs.slice(0, 8)) {
        const row = document.createElement("div");
        row.className = "run-item";
        row.innerHTML = `<div><strong>${escapeHtml(run.run_id)}</strong><div class="hint">${escapeHtml(run.mtime)} ${run.has_report ? "有报告" : "无完整报告"}</div></div>`;
        const btn = document.createElement("button");
        btn.className = "secondary";
        btn.textContent = "查看";
        btn.onclick = () => showRun(run.run_id);
        row.appendChild(btn);
        box.appendChild(row);
      }
    }

    async function startGrade() {
      const payload = {
        submission_pdf: $("submissionPdf").value,
        question_paper_pdf: $("questionPaperPdf").value,
        reference_pdf: $("referencePdf").value,
        questions: $("questions").value,
        api_url: $("apiUrl").value,
        api_mode: $("apiMode").value,
        reasoning_effort: $("solutionReasoning").value,
        objective_reasoning_effort: $("objectiveReasoning").value,
        blank_reasoning_effort: $("blankReasoning").value,
        solution_reasoning_effort: $("solutionReasoning").value,
        model: $("model").value,
        api_key: $("apiKey").value,
        paper_id: $("paperId").value,
        candidate_name: $("candidate").value,
        timeout_seconds: Number($("timeout").value || 300),
        max_retries: Number($("retries").value || 3),
        max_output_tokens: Number($("maxOutputTokens").value || 100000),
        concurrency: Number($("concurrency").value || 10),
        blank_concurrency: Number($("blankConcurrency").value || 3),
        solution_concurrency: Number($("solutionConcurrency").value || 6),
        parse_only: $("parseOnly").checked,
        use_cache: $("useCache").checked,
        use_stream: $("useStream").checked,
        layout_scan: $("layoutScan").checked,
        objective_batch_mode: $("objectiveBatchMode").checked,
        single_review: $("singleReview").checked,
        parallel_visual_rounds: $("parallelVisualRounds").checked,
        reference_is_official: $("refOfficial").checked,
        strict_official: $("strictOfficial").checked,
        policy_path: $("policyPath").value
      };
      $("startBtn").disabled = true;
      setStatus("运行中", "running");
      const data = await api("/api/grade", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      currentRunId = data.task.run_id;
      $("runId").textContent = currentRunId;
      $("stopBtn").disabled = false;
      pollTask();
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollTask, 2500);
    }

    async function pollTask() {
      if (!currentRunId) return;
      try {
        const data = await api("/api/task?run_id=" + encodeURIComponent(currentRunId));
        const task = data.task;
        setStatus(statusText(task.status), task.status);
        $("runId").textContent = `${task.run_id} | ${task.output_dir}`;
        await loadReportOrPartial(task.run_id);
        if (task.status === "finished" || task.status === "failed") {
          $("startBtn").disabled = false;
          $("stopBtn").disabled = true;
          clearInterval(pollTimer);
          pollTimer = null;
          await loadRuns();
        }
      } catch (err) {
        setStatus("轮询失败", "failed");
        $("auditPanel").textContent = String(err);
      }
    }

    async function loadReportOrPartial(runId) {
      let report = await api(`/api/report?run_id=${encodeURIComponent(runId)}&file=grading_report.json`);
      if (!report.available && !report.score_summary) {
        report = await api(`/api/report?run_id=${encodeURIComponent(runId)}&file=partial_report.json`);
      }
      renderReport(report);
      const audit = await api(`/api/audit?run_id=${encodeURIComponent(runId)}`);
      $("auditPanel").textContent = JSON.stringify(audit, null, 2);
      renderProgress(report, audit);
    }

    async function showRun(runId) {
      currentRunId = runId;
      $("runId").textContent = runId;
      setStatus("查看结果", "finished");
      $("stopBtn").disabled = true;
      await loadReportOrPartial(runId);
    }

    async function stopTask() {
      if (!currentRunId) return;
      $("stopBtn").disabled = true;
      setStatus("停止中", "running");
      const data = await api("/api/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({run_id: currentRunId})
      });
      setStatus(statusText(data.task.status), data.task.status);
      await loadReportOrPartial(currentRunId);
      $("startBtn").disabled = false;
    }

    function renderReport(report) {
      $("jsonPanel").textContent = JSON.stringify(report, null, 2);
      const summary = report.score_summary || {};
      $("totalScore").textContent = summary.total_score_for_graded_questions ?? "-";
      const graded = summary.graded_question_count ?? report.completed_question_count ?? (report.question_scores || []).length ?? "-";
      const total = report.total_question_count ? ` / ${report.total_question_count}` : "";
      $("gradedCount").textContent = `${graded}${total}`;
      $("reviewCount").textContent = summary.human_review_count ?? (report.question_scores || []).filter(row => row.needs_human_review).length ?? "-";
      $("arbCount").textContent = report.audit_log_summary?.arbitration_count ?? "-";
      renderTable(report.question_scores || []);
    }

    function renderProgress(report, audit) {
      const completed = report.completed_question_count ?? report.score_summary?.graded_question_count ?? (report.question_scores || []).length ?? 0;
      const total = report.total_question_count ?? "";
      const text = audit.progress?.text || "等待任务开始";
      const countText = total ? `已完成 ${completed}/${total} 题。` : "";
      $("progressBox").textContent = `${countText}${text}`;
    }

    function renderTable(rows) {
      if (!rows.length) {
        $("tablePanel").innerHTML = '<div class="hint">暂无得分表，任务运行中或尚未生成报告。</div>';
        return;
      }
      let html = `<table><thead><tr>
        <th style="width:58px">题号</th>
        <th style="width:80px">得分</th>
        <th style="width:90px">复核</th>
        <th style="width:220px">视觉复核作答</th>
        <th>得分点</th>
        <th>扣分点 / 原因</th>
      </tr></thead><tbody>`;
      for (const row of rows) {
        const score = row.final_score === null || row.final_score === undefined ? "待复核" : `${row.final_score} / ${row.full_score}`;
        const source = row.student_answer_source || {};
        const recognized = source.recognized_student_answer || row.recognized_student_answer || row.grading_round_1?.recognized_student_answer || row.grading_round_1?.student_choice || "";
        const visualSummary = source.visual_reading_summary || row.visual_reading_summary || row.grading_round_1?.visual_reading_summary || "";
        const evidenceUsed = source.evidence_used || row.evidence_used || row.grading_round_1?.evidence_used || "";
        const ocrDraft = source.student_answer_ocr || "";
        html += `<tr>
          <td>${escapeHtml(row.question_no)}</td>
          <td>${escapeHtml(score)}</td>
          <td>${row.needs_human_review ? "是" : "否"}${row.third_arbitration_triggered ? "<br>已仲裁" : ""}</td>
          <td>
            <div>${escapeHtml(compactCell(recognized || "未形成视觉复核文本"))}</div>
            <div class="hint">${escapeHtml(compactCell(visualSummary))}</div>
            <div class="hint">${escapeHtml(evidenceUsed ? "证据：" + evidenceUsed : "")}</div>
            <div class="hint">${escapeHtml(ocrDraft ? "MinerU草稿：" + compactCell(ocrDraft) : "")}</div>
          </td>
          <td>${listHtml(row.main_earned_points || row.valid_student_steps || [])}</td>
          <td>${listHtml(row.main_deducted_points || row.wrong_or_missing_steps || [])}<div class="hint">${escapeHtml(row.review_reason || "")}</div></td>
        </tr>`;
      }
      html += "</tbody></table>";
      $("tablePanel").innerHTML = html;
    }

    function listHtml(items) {
      if (!items.length) return '<span class="hint">无</span>';
      return items.slice(0, 4).map(item => `<div>${escapeHtml(item)}</div>`).join("");
    }

    function compactCell(value) {
      value = String(value || "").replace(/\s+/g, " ").trim();
      return value.length > 120 ? value.slice(0, 119) + "…" : value;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[ch]));
    }

    function statusText(status) {
      return {
        queued: "排队中",
        running: "运行中",
        stopping: "停止中",
        finished: "已完成",
        failed: "失败"
      }[status] || status || "未知";
    }

    document.querySelectorAll(".tabs button").forEach(btn => {
      btn.onclick = () => {
        activeTab = btn.dataset.tab;
        document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("active", b === btn));
        $("tablePanel").style.display = activeTab === "table" ? "" : "none";
        $("jsonPanel").style.display = activeTab === "json" ? "" : "none";
        $("auditPanel").style.display = activeTab === "audit" ? "" : "none";
      };
    });

    $("startBtn").onclick = () => startGrade().catch(err => {
      $("startBtn").disabled = false;
      $("stopBtn").disabled = true;
      setStatus("启动失败", "failed");
      $("auditPanel").textContent = String(err);
      document.querySelector('[data-tab="audit"]').click();
    });
    $("stopBtn").onclick = () => stopTask().catch(err => {
      $("stopBtn").disabled = false;
      setStatus("停止失败", "failed");
      $("auditPanel").textContent = String(err);
      document.querySelector('[data-tab="audit"]').click();
    });
    $("refreshBtn").onclick = () => loadFiles().catch(err => alert(err));
    loadFiles().catch(err => {
      setStatus("初始化失败", "failed");
      $("auditPanel").textContent = String(err);
    });
  </script>
</body>
</html>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web UI for the automatic math grader prototype.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Local grader UI: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
