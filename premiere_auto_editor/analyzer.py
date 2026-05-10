from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


Progress = Callable[[str], None]
ShouldStop = Callable[[], bool]
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov"}
BUILT_IN_TRAVEL_TERMS = [
    ("きよみずでら", "清水寺"),
    ("いよみずでら", "清水寺"),
    ("清水でら", "清水寺"),
    ("ふしみいなり", "伏見稲荷大社"),
    ("伏見稲荷", "伏見稲荷大社"),
    ("あらし山", "嵐山"),
    ("きんかくじ", "金閣寺"),
    ("ぎんかくじ", "銀閣寺"),
    ("とうだいじ", "東大寺"),
    ("せんそうじ", "浅草寺"),
    ("スカイツリー", "東京スカイツリー"),
    ("どうとんぼり", "道頓堀"),
    ("いつくしま", "厳島神社"),
    ("いつくしま神社", "厳島神社"),
    ("ひめじじょう", "姫路城"),
    ("まつもとじょう", "松本城"),
    ("けごんのたき", "華厳の滝"),
    ("あしかがフラワーパーク", "あしかがフラワーパーク"),
    ("ちゅらうみ", "美ら海水族館"),
    ("美ら海", "美ら海水族館"),
]


class AnalysisCancelled(Exception):
    def __init__(self, save_partial: bool) -> None:
        super().__init__("解析が中止されました。")
        self.save_partial = save_partial


@dataclass
class ClipInfo:
    file_id: str
    file_name: str
    path: Path
    duration: float
    fps: float
    width: int
    height: int
    has_audio: bool
    audio_state: str = "未解析"


@dataclass
class TranscriptSegment:
    file_name: str
    path: Path
    start: float
    end: float
    text: str
    raw_text: str = ""
    correction_reason: str = ""
    correction_confidence: str = ""


@dataclass
class CutCandidate:
    file_name: str
    path: Path
    start: float
    end: float
    duration: float
    reason: str
    confidence: str


def run_analysis(
    input_dir: Path,
    output_dir: Path,
    terms_path: Path | None = None,
    enable_transcription: bool = True,
    enable_ai_correction: bool = False,
    ai_model: str = "gpt-5-nano",
    silence_threshold_db: int = -38,
    min_silence_duration: float = 2.0,
    progress: Progress | None = None,
    should_stop: ShouldStop | None = None,
    save_partial_on_stop: Callable[[], bool] | None = None,
) -> dict:
    progress = progress or (lambda _message: None)
    should_stop = should_stop or (lambda: False)
    save_partial_on_stop = save_partial_on_stop or (lambda: True)
    input_path = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise ValueError(f"素材が見つかりません: {input_path}")

    clips_paths = collect_clip_paths(input_path)
    if not clips_paths:
        raise ValueError("指定した素材にMP4/MOVファイルが見つかりませんでした。")

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    whisper = shutil.which("whisper")
    terms = load_terms(terms_path)

    progress(f"MP4/MOVを{len(clips_paths)}件見つけました。")
    clips: list[ClipInfo] = []
    transcripts: list[TranscriptSegment] = []
    cuts: list[CutCandidate] = []

    def flush_outputs(status: str) -> dict:
        sequence_offsets = compute_sequence_offsets(clips)
        write_clips_csv(output_dir / "clips.csv", clips)
        write_transcript_csv(output_dir / "transcript.csv", transcripts)
        write_cut_candidates_csv(output_dir / "cut_candidates.csv", cuts)
        write_summary_csv(output_dir / "summary.csv", clips, transcripts, cuts)
        write_srt(output_dir / "subtitles.srt", transcripts, sequence_offsets)
        write_fcp_xml(output_dir / "premiere_auto_editor.xml", clips, cuts)
        write_manifest(
            output_dir / "manifest.json",
            input_path,
            output_dir,
            clips,
            ffmpeg,
            ffprobe,
            whisper,
            status,
        )
        return analysis_result(input_path, output_dir, clips, transcripts, cuts, ffmpeg, ffprobe, whisper, status)

    for index, path in enumerate(clips_paths, start=1):
        if should_stop():
            progress("停止要求を受け取りました。")
            if save_partial_on_stop():
                progress("ここまでの結果を書き出しました。")
                return flush_outputs("stopped_partial")
            raise AnalysisCancelled(save_partial=False)

        progress(f"[{index}/{len(clips_paths)}] メタ情報を解析中: {path.name}")
        clip = probe_clip(path, f"file-{index}", ffprobe, should_stop)
        clips.append(clip)

        if not clip.has_audio:
            clip.audio_state = "音声なし"
            cuts.append(
                CutCandidate(
                    file_name=clip.file_name,
                    path=clip.path,
                    start=0,
                    end=clip.duration,
                    duration=clip.duration,
                    reason="音声トラックなし",
                    confidence="high",
                )
            )
            flush_outputs("running")
            continue

        progress(f"[{index}/{len(clips_paths)}] 無音区間を検出中: {path.name}")
        silence_ranges = detect_silence(
            path,
            clip.duration,
            ffmpeg,
            should_stop,
            silence_threshold_db=silence_threshold_db,
            min_silence_duration=min_silence_duration,
        )
        for start, end in silence_ranges:
            duration = max(0, end - start)
            if duration > 0:
                cuts.append(
                    CutCandidate(
                        file_name=clip.file_name,
                        path=clip.path,
                        start=start,
                        end=end,
                        duration=duration,
                        reason="ほぼ無音のためカット候補",
                        confidence="medium",
                    )
                )

        progress(f"[{index}/{len(clips_paths)}] 書き起こし中: {path.name}")
        segments: list[TranscriptSegment] = []
        if enable_transcription:
            segments = transcribe_clip(path, whisper, should_stop, terms)
            segments = exclude_silent_transcript_segments(segments, silence_ranges)
            segments = apply_terms_to_segments(segments, terms)
            if enable_ai_correction:
                progress(f"[{index}/{len(clips_paths)}] AI補完中: {path.name}")
                segments = correct_segments_with_ai(segments, ai_model, should_stop)
            transcripts.extend(segments)

        silence_total = sum(max(0, end - start) for start, end in silence_ranges)
        speech_text = " ".join(segment.text.strip() for segment in segments).strip()
        clip.audio_state = classify_audio(clip, silence_total, speech_text)
        flush_outputs("running")

    progress("CSV/SRT/XMLを書き出し中。")
    result = flush_outputs("completed")
    progress("完了しました。")
    return result


def collect_clip_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS else []
    return sorted(
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
    )


def probe_clip(
    path: Path, file_id: str, ffprobe: str | None, should_stop: ShouldStop | None = None
) -> ClipInfo:
    if not ffprobe:
        return ClipInfo(file_id, path.name, path, 0, 30, 0, 0, False)

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run_command(command, should_stop)
    if result.returncode != 0:
        return ClipInfo(file_id, path.name, path, 0, 30, 0, 0, False)

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    fps = parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1")
    return ClipInfo(
        file_id=file_id,
        file_name=path.name,
        path=path,
        duration=duration,
        fps=fps,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        has_audio=audio is not None,
    )


def parse_fps(value: str) -> float:
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_float = float(denominator)
            return float(numerator) / denominator_float if denominator_float else 30
        except ValueError:
            return 30
    try:
        return float(value)
    except ValueError:
        return 30


def detect_silence(
    path: Path,
    duration: float,
    ffmpeg: str | None,
    should_stop: ShouldStop | None = None,
    silence_threshold_db: int = -30,
    min_silence_duration: float = 0.7,
) -> list[tuple[float, float]]:
    if not ffmpeg:
        return []
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
        "-f",
        "null",
        "-",
    ]
    result = run_command(command, should_stop)
    text = f"{result.stdout}\n{result.stderr}"
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    ranges: list[tuple[float, float]] = []
    for offset, start in enumerate(starts):
        end = ends[offset] if offset < len(ends) else duration
        ranges.append((max(0, start), max(start, min(duration, end))))
    return ranges


def transcribe_clip(
    path: Path,
    whisper: str | None,
    should_stop: ShouldStop | None = None,
    terms: list[tuple[str, str]] | None = None,
) -> list[TranscriptSegment]:
    if not whisper:
        return []
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            whisper,
            str(path),
            "--model",
            "base",
            "--language",
            "Japanese",
            "--output_format",
            "json",
            "--output_dir",
            temp_dir,
        ]
        initial_prompt = build_whisper_prompt(terms or [])
        if initial_prompt:
            command.extend(["--initial_prompt", initial_prompt])
        result = run_command(command, should_stop)
        if result.returncode != 0:
            return []
        json_path = Path(temp_dir) / f"{path.stem}.json"
        if not json_path.exists():
            return []
        data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = []
    for item in data.get("segments", []):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                file_name=path.name,
                path=path,
                start=float(item.get("start") or 0),
                end=float(item.get("end") or 0),
                text=text,
                raw_text=text,
            )
        )
    return segments


def run_command(command: list[str], should_stop: ShouldStop | None = None) -> subprocess.CompletedProcess:
    should_stop = should_stop or (lambda: False)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        if should_stop():
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            raise AnalysisCancelled(save_partial=True)
        try:
            stdout, stderr = process.communicate(timeout=0.5)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


def load_terms(path: Path | None) -> list[tuple[str, str]]:
    terms = list(BUILT_IN_TRAVEL_TERMS)
    if not path:
        return terms
    path = path.expanduser()
    if not path.exists():
        return terms
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        terms.extend(
            (row.get("before", ""), row.get("after", ""))
            for row in reader
            if row.get("before") is not None and row.get("after") is not None
        )
    return terms


def build_whisper_prompt(terms: list[tuple[str, str]]) -> str:
    proper_nouns = []
    for before, after in terms:
        if after and after not in proper_nouns:
            proper_nouns.append(after)
    if not proper_nouns:
        return ""
    limited = "、".join(proper_nouns[:80])
    return f"旅行Vlogの音声です。地名、寺社、施設名、観光スポット名を正確に書き起こしてください。候補語: {limited}"


def apply_terms_to_segments(
    segments: Iterable[TranscriptSegment], terms: list[tuple[str, str]]
) -> list[TranscriptSegment]:
    updated = []
    for segment in segments:
        text = segment.text
        for before, after in terms:
            if before:
                text = text.replace(before, after)
        updated.append(
            TranscriptSegment(
                segment.file_name,
                segment.path,
                segment.start,
                segment.end,
                text,
                segment.raw_text or segment.text,
                segment.correction_reason,
                segment.correction_confidence,
            )
        )
    return updated


def exclude_silent_transcript_segments(
    segments: list[TranscriptSegment], silence_ranges: list[tuple[float, float]]
) -> list[TranscriptSegment]:
    if not silence_ranges:
        return segments
    trimmed = []
    for segment in segments:
        remaining = subtract_ranges(segment.start, segment.end, silence_ranges)
        for start, end in remaining:
            if end - start >= 0.05:
                trimmed.append(
                    TranscriptSegment(
                        segment.file_name,
                        segment.path,
                        start,
                        end,
                        segment.text,
                        segment.raw_text or segment.text,
                        segment.correction_reason,
                        segment.correction_confidence,
                    )
                )
    return trimmed


def subtract_ranges(
    start: float, end: float, remove_ranges: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    remaining = [(start, end)]
    for remove_start, remove_end in remove_ranges:
        next_remaining = []
        for current_start, current_end in remaining:
            if remove_end <= current_start or remove_start >= current_end:
                next_remaining.append((current_start, current_end))
                continue
            if remove_start > current_start:
                next_remaining.append((current_start, min(remove_start, current_end)))
            if remove_end < current_end:
                next_remaining.append((max(remove_end, current_start), current_end))
        remaining = next_remaining
    return remaining


def correct_segments_with_ai(
    segments: list[TranscriptSegment],
    model: str,
    should_stop: ShouldStop | None = None,
) -> list[TranscriptSegment]:
    if not segments:
        return segments
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return [
            TranscriptSegment(
                segment.file_name,
                segment.path,
                segment.start,
                segment.end,
                segment.text,
                segment.raw_text or segment.text,
                "OPENAI_API_KEY未設定のためAI補完なし",
                "",
            )
            for segment in segments
        ]

    payload = {
        "model": model or "gpt-5-nano",
        "instructions": (
            "あなたは旅行Vlogの日本語文字起こし補正者です。"
            "聞こえていない内容を追加せず、明らかな誤変換、地名、寺社、施設名、景勝地名だけを自然に補正してください。"
            "タイムスタンプは変更しません。"
        ),
        "input": json.dumps(
            {
                "segments": [
                    {
                        "index": index,
                        "start": segment.start,
                        "end": segment.end,
                        "text": segment.text,
                        "raw_text": segment.raw_text or segment.text,
                    }
                    for index, segment in enumerate(segments)
                ]
            },
            ensure_ascii=False,
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "transcript_corrections",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "segments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "index": {"type": "integer"},
                                    "corrected_text": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "confidence": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high"],
                                    },
                                },
                                "required": ["index", "corrected_text", "reason", "confidence"],
                            },
                        }
                    },
                    "required": ["segments"],
                },
            }
        },
    }
    response_text = post_openai_response(payload, api_key, should_stop)
    if not response_text:
        return segments
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return segments
    corrections = {int(item["index"]): item for item in data.get("segments", [])}
    updated = []
    for index, segment in enumerate(segments):
        correction = corrections.get(index)
        if not correction:
            updated.append(segment)
            continue
        corrected_text = str(correction.get("corrected_text") or segment.text).strip()
        updated.append(
            TranscriptSegment(
                segment.file_name,
                segment.path,
                segment.start,
                segment.end,
                corrected_text or segment.text,
                segment.raw_text or segment.text,
                str(correction.get("reason") or ""),
                str(correction.get("confidence") or ""),
            )
        )
    return updated


def post_openai_response(
    payload: dict,
    api_key: str,
    should_stop: ShouldStop | None = None,
) -> str:
    should_stop = should_stop or (lambda: False)
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if should_stop():
        raise AnalysisCancelled(save_partial=True)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    texts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts)


def classify_audio(clip: ClipInfo, silence_total: float, transcript_text: str) -> str:
    if not clip.has_audio:
        return "音声なし"
    if clip.duration > 0 and silence_total / clip.duration >= 0.9:
        return "ほぼ無音"
    if transcript_text:
        return "発話あり"
    return "環境音中心"


def compute_sequence_offsets(clips: list[ClipInfo]) -> dict[str, float]:
    offsets: dict[str, float] = {}
    cursor = 0.0
    for clip in clips:
        offsets[str(clip.path)] = cursor
        cursor += clip.duration
    return offsets


def write_clips_csv(path: Path, clips: list[ClipInfo]) -> None:
    write_csv(
        path,
        ["file_name", "path", "duration_sec", "fps", "width", "height", "has_audio", "audio_state"],
        [
            {
                "file_name": clip.file_name,
                "path": str(clip.path),
                "duration_sec": seconds(clip.duration),
                "fps": round(clip.fps, 3),
                "width": clip.width,
                "height": clip.height,
                "has_audio": clip.has_audio,
                "audio_state": clip.audio_state,
            }
            for clip in clips
        ],
    )


def write_transcript_csv(path: Path, transcripts: list[TranscriptSegment]) -> None:
    write_csv(
        path,
        [
            "file_name",
            "path",
            "start",
            "end",
            "text",
            "raw_text",
            "correction_reason",
            "correction_confidence",
        ],
        [
            {
                "file_name": item.file_name,
                "path": str(item.path),
                "start": seconds(item.start),
                "end": seconds(item.end),
                "text": item.text,
                "raw_text": item.raw_text or item.text,
                "correction_reason": item.correction_reason,
                "correction_confidence": item.correction_confidence,
            }
            for item in transcripts
        ],
    )


def write_cut_candidates_csv(path: Path, cuts: list[CutCandidate]) -> None:
    write_csv(
        path,
        ["file_name", "path", "start", "end", "duration", "reason", "confidence"],
        [
            {
                "file_name": item.file_name,
                "path": str(item.path),
                "start": seconds(item.start),
                "end": seconds(item.end),
                "duration": seconds(item.duration),
                "reason": item.reason,
                "confidence": item.confidence,
            }
            for item in cuts
        ],
    )


def write_summary_csv(
    path: Path,
    clips: list[ClipInfo],
    transcripts: list[TranscriptSegment],
    cuts: list[CutCandidate],
) -> None:
    transcript_count = count_by_path(transcripts)
    cut_count = count_by_path(cuts)
    cut_duration = duration_by_path(cuts)
    rows = []
    for clip in clips:
        total_cut = cut_duration.get(str(clip.path), 0.0)
        rows.append(
            {
                "file_name": clip.file_name,
                "path": str(clip.path),
                "duration_sec": seconds(clip.duration),
                "audio_state": clip.audio_state,
                "transcript_segments": transcript_count.get(str(clip.path), 0),
                "cut_candidate_count": cut_count.get(str(clip.path), 0),
                "cut_candidate_duration_sec": seconds(total_cut),
                "note": "無音は削除せず、編集判断用の候補として出力",
            }
        )
    write_csv(
        path,
        [
            "file_name",
            "path",
            "duration_sec",
            "audio_state",
            "transcript_segments",
            "cut_candidate_count",
            "cut_candidate_duration_sec",
            "note",
        ],
        rows,
    )


def write_srt(
    path: Path, transcripts: list[TranscriptSegment], sequence_offsets: dict[str, float]
) -> None:
    lines = []
    for index, segment in enumerate(transcripts, start=1):
        offset = sequence_offsets.get(str(segment.path), 0)
        lines.extend(
            [
                str(index),
                f"{srt_time(offset + segment.start)} --> {srt_time(offset + segment.end)}",
                segment.text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_fcp_xml(path: Path, clips: list[ClipInfo], cuts: list[CutCandidate]) -> None:
    root = ET.Element("xmeml", {"version": "4"})
    sequence = ET.SubElement(root, "sequence", {"id": "sequence-1"})
    ET.SubElement(sequence, "name").text = "Premiere Auto Editor MVP"
    add_rate(sequence, 30)
    total_frames = sum(to_frames(clip.duration, 30) for clip in clips)
    ET.SubElement(sequence, "duration").text = str(total_frames)
    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    video_track = ET.SubElement(video, "track")
    audio = ET.SubElement(media, "audio")
    audio_track = ET.SubElement(audio, "track")

    timeline_cursor = 0
    clipitem_index = 1
    audioitem_index = 1
    for clip in clips:
        boundaries = segment_boundaries(clip, cuts)
        for start_sec, end_sec in boundaries:
            start_frame = timeline_cursor
            duration_frames = max(1, to_frames(end_sec - start_sec, 30))
            end_frame = start_frame + duration_frames
            clipitem = ET.SubElement(video_track, "clipitem", {"id": f"clipitem-{clipitem_index}"})
            ET.SubElement(clipitem, "name").text = f"{clip.file_name} [{seconds(start_sec)}-{seconds(end_sec)}]"
            ET.SubElement(clipitem, "enabled").text = "TRUE"
            ET.SubElement(clipitem, "duration").text = str(duration_frames)
            add_rate(clipitem, 30)
            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)
            ET.SubElement(clipitem, "in").text = str(to_frames(start_sec, 30))
            ET.SubElement(clipitem, "out").text = str(to_frames(end_sec, 30))
            add_media_file(clipitem, clip, clip.file_id)
            add_link(clipitem, f"clipitem-{clipitem_index}", "video", 1, 1)
            if clip.has_audio:
                add_link(clipitem, f"audioitem-{audioitem_index}", "audio", 1, 1)
                audioitem = ET.SubElement(audio_track, "clipitem", {"id": f"audioitem-{audioitem_index}"})
                ET.SubElement(audioitem, "name").text = f"{clip.file_name} audio [{seconds(start_sec)}-{seconds(end_sec)}]"
                ET.SubElement(audioitem, "enabled").text = "TRUE"
                ET.SubElement(audioitem, "duration").text = str(duration_frames)
                add_rate(audioitem, 30)
                ET.SubElement(audioitem, "start").text = str(start_frame)
                ET.SubElement(audioitem, "end").text = str(end_frame)
                ET.SubElement(audioitem, "in").text = str(to_frames(start_sec, 30))
                ET.SubElement(audioitem, "out").text = str(to_frames(end_sec, 30))
                add_media_file(audioitem, clip, clip.file_id)
                add_link(audioitem, f"clipitem-{clipitem_index}", "video", 1, 1)
                add_link(audioitem, f"audioitem-{audioitem_index}", "audio", 1, 1)
                audioitem_index += 1
            timeline_cursor = end_frame
            clipitem_index += 1

    indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def add_media_file(parent: ET.Element, clip: ClipInfo, file_id: str) -> None:
    file_el = ET.SubElement(parent, "file", {"id": file_id})
    ET.SubElement(file_el, "name").text = clip.file_name
    ET.SubElement(file_el, "pathurl").text = path_url(clip.path)
    ET.SubElement(file_el, "duration").text = str(to_frames(clip.duration, 30))
    add_rate(file_el, 30)
    media_el = ET.SubElement(file_el, "media")
    video_el = ET.SubElement(media_el, "video")
    sample = ET.SubElement(video_el, "samplecharacteristics")
    add_rate(sample, 30)
    ET.SubElement(sample, "width").text = str(clip.width or 1920)
    ET.SubElement(sample, "height").text = str(clip.height or 1080)
    audio_el = ET.SubElement(media_el, "audio")
    sample = ET.SubElement(audio_el, "samplecharacteristics")
    ET.SubElement(sample, "depth").text = "16"
    ET.SubElement(sample, "samplerate").text = "48000"
    ET.SubElement(audio_el, "channelcount").text = "2"


def add_link(parent: ET.Element, clipitem_id: str, media_type: str, trackindex: int, clipindex: int) -> None:
    link = ET.SubElement(parent, "link")
    ET.SubElement(link, "linkclipref").text = clipitem_id
    ET.SubElement(link, "mediatype").text = media_type
    ET.SubElement(link, "trackindex").text = str(trackindex)
    ET.SubElement(link, "clipindex").text = str(clipindex)


def segment_boundaries(clip: ClipInfo, cuts: list[CutCandidate]) -> list[tuple[float, float]]:
    boundaries = {0.0, clip.duration}
    for cut in cuts:
        if cut.path == clip.path:
            boundaries.add(max(0.0, min(clip.duration, cut.start)))
            boundaries.add(max(0.0, min(clip.duration, cut.end)))
    sorted_points = sorted(boundary for boundary in boundaries if boundary >= 0)
    return [
        (sorted_points[index], sorted_points[index + 1])
        for index in range(len(sorted_points) - 1)
        if sorted_points[index + 1] - sorted_points[index] > 0.05
    ]


def write_manifest(
    path: Path,
    input_path: Path,
    output_dir: Path,
    clips: list[ClipInfo],
    ffmpeg: str | None,
    ffprobe: str | None,
    whisper: str | None,
    status: str = "completed",
) -> None:
    data = {
        "app": "Premiere Auto Editor MVP",
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "status": status,
        "clip_count": len(clips),
        "dependencies": {
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "whisper": whisper,
        },
        "premiere_xml_note": "FCP 7 XML互換のMVP出力です。.prprojは直接生成・編集しません。",
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def analysis_result(
    input_path: Path,
    output_dir: Path,
    clips: list[ClipInfo],
    transcripts: list[TranscriptSegment],
    cuts: list[CutCandidate],
    ffmpeg: str | None,
    ffprobe: str | None,
    whisper: str | None,
    status: str,
) -> dict:
    return {
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "status": status,
        "clip_count": len(clips),
        "transcript_segments": len(transcripts),
        "cut_candidates": len(cuts),
        "files": [
            "clips.csv",
            "transcript.csv",
            "cut_candidates.csv",
            "summary.csv",
            "subtitles.srt",
            "premiere_auto_editor.xml",
            "manifest.json",
        ],
        "dependencies": {
            "ffmpeg": bool(ffmpeg),
            "ffprobe": bool(ffprobe),
            "whisper": bool(whisper),
        },
    }


def add_rate(parent: ET.Element, fps: int) -> None:
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "FALSE"


def count_by_path(items: Iterable[TranscriptSegment | CutCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.path)
        counts[key] = counts.get(key, 0) + 1
    return counts


def duration_by_path(items: Iterable[CutCandidate]) -> dict[str, float]:
    durations: dict[str, float] = {}
    for item in items:
        key = str(item.path)
        durations[key] = durations.get(key, 0) + item.duration
    return durations


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def to_frames(seconds_value: float, fps: float) -> int:
    return int(round(max(0.0, seconds_value) * fps))


def seconds(value: float) -> str:
    return f"{value:.3f}"


def srt_time(value: float) -> str:
    value = max(0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds_part = int(value % 60)
    milliseconds = int(round((value - math.floor(value)) * 1000))
    if milliseconds == 1000:
        seconds_part += 1
        milliseconds = 0
    return f"{hours:02}:{minutes:02}:{seconds_part:02},{milliseconds:03}"


def path_url(path: Path) -> str:
    return "file://localhost" + urllib.parse.quote(os.fspath(path.resolve()))


def indent(element: ET.Element, level: int = 0) -> None:
    whitespace = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = whitespace + "  "
        for child in element:
            indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = whitespace
    if level and (not element.tail or not element.tail.strip()):
        element.tail = whitespace
