from __future__ import annotations

import json
import os
import csv
import io
import re
import subprocess
import shutil
import tempfile
import threading
import traceback
import uuid
import zipfile
from datetime import datetime
from time import monotonic
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from premiere_auto_editor.analyzer import AnalysisCancelled, run_analysis


ROOT = Path(__file__).parent.resolve()
STATIC_DIR = ROOT / "static"
JOBS: dict[str, dict] = {}
LOCAL_BIN_DIRS = [ROOT / ".venv312" / "bin", ROOT / ".venv" / "bin", Path("/opt/homebrew/bin")]


def configure_local_path() -> None:
    paths = [str(path) for path in LOCAL_BIN_DIRS if path.exists()]
    if paths:
        import os

        os.environ["PATH"] = ":".join(paths + [os.environ.get("PATH", "")])


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/system":
            self.send_json(
                {
                    "ffmpeg": shutil.which("ffmpeg"),
                    "ffprobe": shutil.which("ffprobe"),
                    "whisper": shutil.which("whisper"),
                }
            )
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/download"):
            job_id = unquote(parsed.path.split("/")[-2])
            self.serve_job_zip(job_id)
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.send_json(public_job(job_id), status=200 if job_id in JOBS else 404)
            return
        if parsed.path.startswith("/static/"):
            target = STATIC_DIR / parsed.path.replace("/static/", "", 1)
            content_type = "text/css" if target.suffix == ".css" else "application/javascript"
            self.serve_file(target, content_type)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/dialog":
            self.send_json(open_dialog(self.read_json()))
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/export"):
            job_id = unquote(parsed.path.split("/")[-2])
            self.send_json(export_job(job_id, self.read_json()))
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/apply-transcript"):
            job_id = unquote(parsed.path.split("/")[-2])
            self.send_json(apply_transcript_corrections(job_id, self.read_json()))
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/stop"):
            job_id = unquote(parsed.path.split("/")[-2])
            payload = self.read_json()
            self.send_json(stop_job(job_id, str(payload.get("mode") or "partial")))
            return
        if parsed.path != "/api/analyze":
            self.send_error(404)
            return
        payload = self.read_json()
        job_id = str(uuid.uuid4())
        JOBS[job_id] = {
            "status": "running",
            "messages": [],
            "result": None,
            "error": None,
            "progress": {"percent": 0, "label": "待機中"},
            "started_at": monotonic(),
            "last_progress_at": monotonic(),
            "cancel_event": threading.Event(),
            "stop_mode": "partial",
            "output_root": "",
            "work_dir": "",
        }

        thread = threading.Thread(target=run_job, args=(job_id, payload), daemon=True)
        thread.start()
        self.send_json({"job_id": job_id})

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_job_zip(self, job_id: str) -> None:
        archive = build_job_zip(job_id)
        if not archive or not archive.exists():
            self.send_error(404)
            return
        body = archive.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{archive.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_job(job_id: str, payload: dict) -> None:
    def progress(message: str) -> None:
        JOBS[job_id]["messages"].append(message)
        update_progress(job_id, message)

    try:
        input_path = Path(str(payload.get("input_path") or payload.get("input_dir") or ""))
        output_root = Path(str(payload.get("output_dir") or default_output_dir(input_path)))
        output_dir = create_work_output_dir(job_id)
        JOBS[job_id]["output_root"] = str(output_root.expanduser().resolve())
        JOBS[job_id]["work_dir"] = str(output_dir)
        terms_value = str(payload.get("terms_path") or "").strip()
        terms_path = Path(terms_value) if terms_value else None
        JOBS[job_id]["progress"] = {"percent": 1, "label": "解析を開始しました"}
        result = run_analysis(
            input_dir=input_path,
            output_dir=output_dir,
            terms_path=terms_path,
            enable_transcription=bool(payload.get("enable_transcription", True)),
            enable_ai_correction=False,
            ai_model="",
            silence_threshold_db=int(payload.get("silence_threshold_db") or -38),
            min_silence_duration=float(payload.get("min_silence_duration") or 2.0),
            progress=progress,
            should_stop=JOBS[job_id]["cancel_event"].is_set,
            save_partial_on_stop=lambda: JOBS[job_id].get("stop_mode") == "partial",
        )
        result["preview"] = build_csv_previews(output_dir)
        result["chatgpt_prompt"] = build_chatgpt_prompt(output_dir)
        result["ready_to_export"] = True
        result["suggested_output_root"] = JOBS[job_id]["output_root"]
        JOBS[job_id]["status"] = "stopped" if result.get("status") == "stopped_partial" else "completed"
        JOBS[job_id]["result"] = result
        JOBS[job_id]["progress"] = {"percent": 100, "label": "完了"}
    except AnalysisCancelled as exc:
        JOBS[job_id]["status"] = "aborted"
        JOBS[job_id]["error"] = "解析を中止しました。" if not exc.save_partial else None
        JOBS[job_id]["progress"] = {"percent": 0, "label": "中止"}
    except Exception as exc:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = f"{exc}"
        JOBS[job_id]["traceback"] = traceback.format_exc()
        JOBS[job_id]["progress"] = {"percent": 0, "label": "失敗"}


def default_output_dir(input_path: Path) -> Path:
    if input_path.suffix.lower() in {".mp4", ".mov"}:
        return input_path.parent / "premiere_auto_editor_output"
    return input_path / "premiere_auto_editor_output"


def create_run_output_dir(output_root: Path, input_path: Path) -> Path:
    output_root = output_root.expanduser().resolve()
    label_source = input_path.stem if input_path.suffix else input_path.name
    label = sanitize_name(label_source or "materials")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = output_root / f"premiere_auto_editor_{label}_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def create_work_output_dir(job_id: str) -> Path:
    output_dir = Path(tempfile.gettempdir()) / "premiere-auto-editor" / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def sanitize_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:48] or "materials"


def update_progress(job_id: str, message: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    match = re.search(r"\[(\d+)/(\d+)\]\s*(.+)", message)
    if match:
        current = max(1, int(match.group(1)))
        total = max(1, int(match.group(2)))
        phase = match.group(3)
        phase_offset = 0.15
        if "無音区間" in phase:
            phase_offset = 0.45
        elif "書き起こし" in phase:
            phase_offset = 0.75
        percent = int((((current - 1) + phase_offset) / total) * 100)
        job["progress"] = {"percent": max(1, min(99, percent)), "label": message}
        job["last_progress_at"] = monotonic()
    elif "見つけました" in message:
        job["progress"] = {"percent": 1, "label": message}
        job["last_progress_at"] = monotonic()
    elif "書き出し" in message:
        job["progress"] = {"percent": 98, "label": message}
        job["last_progress_at"] = monotonic()


def public_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {
        "status": job.get("status"),
        "messages": job.get("messages", []),
        "result": job.get("result"),
        "error": job.get("error"),
        "progress": estimated_progress(job),
    }


def estimated_progress(job: dict) -> dict:
    progress = dict(job.get("progress", {"percent": 0, "label": ""}))
    if job.get("status") != "running":
        return progress
    base_percent = float(progress.get("percent") or 0)
    label = str(progress.get("label") or "")
    elapsed = max(0.0, monotonic() - float(job.get("started_at") or monotonic()))
    elapsed_percent = 99 * (1 - pow(2.718281828, -elapsed / 180))
    progress["percent"] = min(99, int(max(base_percent, elapsed_percent)))
    progress["label"] = f"{label or '解析中'} / 経過 {int(elapsed)}秒"
    return progress


def export_job(job_id: str, payload: dict) -> dict:
    job = JOBS.get(job_id)
    if not job or not job.get("result"):
        return {"ok": False, "error": "出力できる解析結果がありません。"}
    source_dir = Path(str(job["result"].get("output_dir") or job.get("work_dir") or ""))
    if not source_dir.exists():
        return {"ok": False, "error": "一時出力フォルダが見つかりません。"}

    output_value = str(payload.get("output_dir") or job.get("output_root") or "").strip()
    if not output_value:
        return {"ok": False, "error": "出力フォルダを指定してください。"}

    input_path = Path(str(job["result"].get("input_path") or "materials"))
    destination = create_run_output_dir(Path(output_value), input_path)
    for file_name in job["result"].get("files", []):
        source = source_dir / file_name
        if source.exists():
            shutil.copy2(source, destination / file_name)

    job["result"]["exported_output_dir"] = str(destination)
    job["result"]["output_dir"] = str(destination)
    job["result"]["ready_to_export"] = False
    return {"ok": True, "output_dir": str(destination), "download_url": f"/api/jobs/{job_id}/download"}


def apply_transcript_corrections(job_id: str, payload: dict) -> dict:
    job = JOBS.get(job_id)
    if not job or not job.get("result"):
        return {"ok": False, "error": "補完を適用できる解析結果がありません。"}
    output_dir = Path(str(job["result"].get("output_dir") or job.get("work_dir") or ""))
    if not output_dir.exists():
        return {"ok": False, "error": "一時出力フォルダが見つかりません。"}

    csv_text = strip_csv_fence(str(payload.get("csv_text") or ""))
    if not csv_text.strip():
        return {"ok": False, "error": "補完済みCSVを貼り付けてください。"}

    try:
        rows = parse_transcript_rows(csv_text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not rows:
        return {"ok": False, "error": "CSVとして読み込める行がありません。"}

    transcript_path = output_dir / "transcript.csv"
    write_transcript_rows(transcript_path, rows)
    regenerate_srt(output_dir)
    refresh_summary_transcript_counts(output_dir, rows)
    job["result"]["preview"] = build_csv_previews(output_dir)
    job["result"]["chatgpt_prompt"] = build_chatgpt_prompt(output_dir)
    return {
        "ok": True,
        "preview": job["result"]["preview"],
        "chatgpt_prompt": job["result"]["chatgpt_prompt"],
        "rows": len(rows),
    }


def build_job_zip(job_id: str) -> Path | None:
    job = JOBS.get(job_id)
    if not job or not job.get("result"):
        return None
    source_dir = Path(
        str(job["result"].get("exported_output_dir") or job["result"].get("output_dir") or job.get("work_dir") or "")
    )
    if not source_dir.exists():
        return None
    archive = Path(tempfile.gettempdir()) / "premiere-auto-editor" / f"{job_id}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(source_dir.iterdir()):
            if path.is_file():
                zip_file.write(path, arcname=path.name)
    return archive


def build_csv_previews(output_dir: Path) -> dict:
    previews = {}
    for name in ["clips.csv", "summary.csv", "cut_candidates.csv", "transcript.csv"]:
        previews[name] = read_csv_preview(output_dir / name)
    return previews


def read_csv_preview(path: Path, max_rows: int = 100) -> dict:
    if not path.exists():
        return {"headers": [], "rows": [], "total_rows": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < max_rows:
                rows.append(row)
    return {"headers": reader.fieldnames or [], "rows": rows, "total_rows": total}


TRANSCRIPT_HEADERS = [
    "file_name",
    "path",
    "start",
    "end",
    "text",
    "raw_text",
    "correction_reason",
    "correction_confidence",
]


def build_chatgpt_prompt(output_dir: Path) -> str:
    transcript_path = output_dir / "transcript.csv"
    if not transcript_path.exists():
        return ""
    transcript_text = transcript_path.read_text(encoding="utf-8-sig").strip()
    if not transcript_text:
        return "この解析では書き起こしセグメントがありません。補完は不要です。"
    return "\n".join(
        [
            "以下は旅行動画の文字起こしCSVです。",
            "目的は、明らかな誤変換、地名、施設名、寺社名、景勝地名、店名だけを自然に補正することです。",
            "聞こえていない内容を新しく追加しないでください。",
            "start/end/path/file_name は絶対に変更しないでください。",
            "返答はCSVのみ。説明文やMarkdownコードブロックは不要です。",
            "列は必ず次の順にしてください: file_name,path,start,end,text,raw_text,correction_reason,correction_confidence",
            "textには補正後の文字、raw_textには元の文字を残してください。",
            "correction_reasonには補正理由、correction_confidenceにはlow/medium/highのいずれかを入れてください。",
            "",
            "CSV:",
            transcript_text,
        ]
    )


def strip_csv_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_transcript_rows(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    required = {"file_name", "path", "start", "end", "text"}
    missing = required.difference(headers)
    if missing:
        raise ValueError(f"必須列が足りません: {', '.join(sorted(missing))}")
    rows = []
    for row in reader:
        if not any((value or "").strip() for value in row.values()):
            continue
        normalized = {header: str(row.get(header) or "") for header in TRANSCRIPT_HEADERS}
        normalized["raw_text"] = normalized["raw_text"] or normalized["text"]
        rows.append(normalized)
    return rows


def write_transcript_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=TRANSCRIPT_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def regenerate_srt(output_dir: Path) -> None:
    clips = read_csv_rows(output_dir / "clips.csv")
    offsets: dict[str, float] = {}
    cursor = 0.0
    for clip in clips:
        offsets[clip.get("path", "")] = cursor
        cursor += to_float(clip.get("duration_sec", "0"))

    lines = []
    for index, row in enumerate(read_csv_rows(output_dir / "transcript.csv"), start=1):
        offset = offsets.get(row.get("path", ""), 0.0)
        start = offset + to_float(row.get("start", "0"))
        end = offset + to_float(row.get("end", "0"))
        lines.extend([str(index), f"{srt_time(start)} --> {srt_time(end)}", row.get("text", ""), ""])
    (output_dir / "subtitles.srt").write_text("\n".join(lines), encoding="utf-8")


def refresh_summary_transcript_counts(output_dir: Path, transcript_rows: list[dict[str, str]]) -> None:
    summary_path = output_dir / "summary.csv"
    rows = read_csv_rows(summary_path)
    if not rows:
        return
    counts: dict[str, int] = {}
    for row in transcript_rows:
        counts[row.get("path", "")] = counts.get(row.get("path", ""), 0) + 1
    headers = list(rows[0].keys())
    for row in rows:
        row["transcript_segments"] = str(counts.get(row.get("path", ""), 0))
    with summary_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def srt_time(value: float) -> str:
    value = max(0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    milliseconds = int(round((value - int(value)) * 1000))
    if milliseconds == 1000:
        seconds += 1
        milliseconds = 0
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def stop_job(job_id: str, mode: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "ジョブが見つかりません。"}
    if job.get("status") != "running":
        return {"ok": False, "error": "実行中のジョブではありません。"}
    job["stop_mode"] = "abort" if mode == "abort" else "partial"
    job["cancel_event"].set()
    if job["stop_mode"] == "partial":
        job["messages"].append("ここまでを書き出して停止します。")
        job["progress"] = {"percent": job.get("progress", {}).get("percent", 0), "label": "ここまでを書き出して停止します"}
    else:
        job["messages"].append("解析を中止します。")
        job["progress"] = {"percent": job.get("progress", {}).get("percent", 0), "label": "解析を中止します"}
    return {"ok": True}


def open_dialog(payload: dict) -> dict:
    kind = str(payload.get("kind") or "file")
    path = open_dialog_with_osascript(kind)
    if path is None:
        return {"path": "", "error": "macOSの選択ダイアログを開けませんでした。パスを直接貼り付けてください。"}
    return {"path": path}


def open_dialog_with_osascript(kind: str) -> str | None:
    if not shutil.which("osascript"):
        return None
    if kind == "input_file":
        script = 'POSIX path of (choose file with prompt "MP4/MOVファイルを選択" of type {"mp4", "MP4", "mov", "MOV"})'
    elif kind == "input_dir":
        script = 'POSIX path of (choose folder with prompt "素材フォルダを選択")'
    elif kind == "output":
        script = 'POSIX path of (choose folder with prompt "出力フォルダを選択")'
    elif kind == "terms":
        script = 'POSIX path of (choose file with prompt "terms.csvを選択" of type {"csv", "CSV"})'
    else:
        return ""

    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if "User canceled" in result.stderr:
        return ""
    return None


def main() -> None:
    configure_local_path()
    port = int(os.environ.get("PORT", "8765"))
    server = LocalServer(("127.0.0.1", port), Handler)
    print(f"Premiere Auto Editor MVP: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
