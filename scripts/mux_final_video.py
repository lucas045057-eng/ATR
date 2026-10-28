from __future__ import annotations

import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
NARRATION_LINES = [
    "这是一套本地运行的 SRE 故障诊断运行时 ATR。",
    "上传业务源码后，系统静态解析生成可溯源的业务图谱，经过人工审核确认依赖关系。",
    "接入 Mock 隔离目标后，操作员精确授权调查范围，无关对象保持未授权。",
    "发生故障时，模型只能从 Runtime 提供的只读动作里选择，不能自己执行命令。",
    "证据采集完成后，确定性证据门给出结论：直接故障得到支持，全程零次模型调用。",
    "ATR 的原则：永远是证据说话。模型不修复、不越权、不虚构根因。",
]


def load_imageio_ffmpeg() -> tuple[Any | None, str | None]:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return get_ffmpeg_exe(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def event_time(marks: dict[str, Any], name: str, default: float) -> float:
    for item in marks.get("events", []):
        if item.get("name") == name:
            return float(item.get("elapsed_seconds", default))
    return default


def narration_timeline(marks: dict[str, Any]) -> list[dict[str, Any]]:
    lines = marks.get("narration_lines") or NARRATION_LINES
    anchors = marks.get("narration_anchors") or []
    if len(anchors) >= len(lines):
        return [
            {
                "index": index + 1,
                "text": text,
                "start_seconds": round(
                    float(anchors[index].get("start_seconds", 0.8)), 3
                ),
            }
            for index, text in enumerate(lines)
        ]

    caption = {
        item.get("name"): float(item.get("elapsed_seconds", 0.0))
        for item in marks.get("captions", [])
    }
    end_card = event_time(marks, "end_card_started", marks.get("recorded_seconds", 70.0))
    desired_offsets = [
        0.8,
        caption.get("caption_1", 4.8) + 0.9,
        caption.get("caption_3", 30.0) + 1.0,
        caption.get("caption_4", 48.0) + 1.0,
        caption.get("caption_5", caption.get("caption_4", 48.0) + 10.0) + 1.0,
        end_card + 0.9,
    ]
    offsets: list[float] = []
    for offset in desired_offsets:
        # Dry runs can omit a later caption. Keep every narration line ordered
        # even when one of its visual anchors is absent.
        if offsets:
            offset = max(offset, offsets[-1] + 3.0)
        offsets.append(offset)
    return [
        {"index": index + 1, "text": text, "start_seconds": round(offsets[index], 3)}
        for index, text in enumerate(lines)
    ]


def read_wav_mono_48k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())

    if width == 1:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        samples = (samples - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        values = (
            data[:, 0].astype(np.int32)
            | (data[:, 1].astype(np.int32) << 8)
            | (data[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float32) / 8388608.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {width}")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 48_000 and samples.size:
        old_x = np.arange(samples.size, dtype=np.float64) / rate
        duration = samples.size / rate
        new_size = max(1, int(round(duration * 48_000)))
        new_x = np.arange(new_size, dtype=np.float64) / 48_000
        samples = np.interp(new_x, old_x, samples).astype(np.float32)
    return samples


def synthesize_with_system_speech(
    *,
    lines: list[str],
    output_dir: Path,
    voice_name: str,
    rate: int,
) -> tuple[list[Path], str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("part-*.wav"):
        old.unlink()

    items = [
        {
            "path": str((output_dir / f"part-{index + 1:02d}.wav").resolve()),
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
        for index, text in enumerate(lines)
    ]
    with tempfile.TemporaryDirectory(prefix="atr-tts-") as temp:
        temp_dir = Path(temp)
        items_path = temp_dir / "items.json"
        script_path = temp_dir / "synthesize.ps1"
        items_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 '{items_path}' | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = {rate}
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    44100,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)
foreach ($item in $items) {{
    $text = [System.Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($item.text_b64)
    )
    $synth.SetOutputToWaveFile($item.path, $format)
    $synth.SelectVoice('{voice_name}')
    $synth.Speak($text)
    $synth.SetOutputToNull()
}}
$synth.Dispose()
"""
        script_path.write_text(script, encoding="utf-8-sig")
        proc = subprocess.run(
            [
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
    parts = [Path(item["path"]) for item in items]
    if proc.returncode != 0 or not all(path.is_file() and path.stat().st_size > 1000 for path in parts):
        detail = (proc.stderr or proc.stdout or "Speech synthesis did not produce all WAV parts.").strip()
        return [], detail
    return parts, None


def ambient_track(duration: float, *, level: float) -> np.ndarray:
    sample_rate = 48_000
    count = max(1, int(math.ceil(duration * sample_rate)))
    t = np.arange(count, dtype=np.float64) / sample_rate
    pad = np.zeros(count, dtype=np.float64)
    for frequency, amplitude, phase in (
        (55.0, 0.30, 0.0),
        (82.5, 0.18, 1.1),
        (110.0, 0.15, 2.2),
        (164.8, 0.08, 0.4),
        (220.0, 0.035, 2.8),
    ):
        pad += amplitude * np.sin(2 * np.pi * frequency * t + phase)

    breath = 0.72 + 0.18 * np.sin(2 * np.pi * 0.055 * t + 0.7)
    pad *= breath
    fade_in = np.clip(t / 2.5, 0.0, 1.0)
    fade_out = np.clip((duration - t) / 3.5, 0.0, 1.0)
    pad *= fade_in * fade_out
    peak = np.max(np.abs(pad)) or 1.0
    return (pad / peak * level).astype(np.float32)


def write_audio_track(
    *,
    path: Path,
    parts: list[Path],
    timeline: list[dict[str, Any]],
    duration: float,
    ambient_level: float,
) -> dict[str, Any]:
    sample_rate = 48_000
    track = ambient_track(duration, level=ambient_level)
    placed: list[dict[str, Any]] = []
    for segment, part in zip(timeline, parts, strict=False):
        samples = read_wav_mono_48k(part)
        start = max(0, int(round(float(segment["start_seconds"]) * sample_rate)))
        end = min(track.size, start + samples.size)
        if end <= start:
            continue
        track[start:end] += samples[: end - start] * 0.92
        placed.append(
            {
                "index": segment["index"],
                "start_seconds": segment["start_seconds"],
                "duration_seconds": round(samples.size / sample_rate, 3),
                "text": segment["text"],
            }
        )

    peak = float(np.max(np.abs(track))) if track.size else 0.0
    if peak > 0.92:
        track *= 0.92 / peak
    pcm = np.clip(track * 32767.0, -32768, 32767).astype("<i2")
    stereo = np.column_stack((pcm, pcm)).reshape(-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(stereo.tobytes())
    return {
        "path": str(path),
        "sample_rate": sample_rate,
        "channels": 2,
        "duration_seconds": round(track.size / sample_rate, 3),
        "segments": placed,
    }


def prepare_audio(
    *,
    output_dir: Path,
    marks: dict[str, Any],
    voice_name: str,
    speech_rate: int,
    narration_override: Path | None,
    no_tts: bool,
) -> tuple[Path, dict[str, Any]]:
    timeline = narration_timeline(marks)
    duration = max(
        float(marks.get("recorded_seconds", 0.0)) + 0.8,
        timeline[-1]["start_seconds"] + 8.0,
        12.0,
    )
    narration_path = output_dir / "narration.wav"
    status_path = output_dir / "narration-status.json"

    if narration_override:
        if not narration_override.is_file():
            raise FileNotFoundError(narration_override)
        shutil.copy2(narration_override, narration_path)
        status = {
            "kind": "external_narration",
            "speech_available": True,
            "source": str(narration_override),
            "voice": None,
            "timeline": timeline,
            "error": None,
        }
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return narration_path, status

    parts: list[Path] = []
    tts_error: str | None = None
    if not no_tts:
        parts, tts_error = synthesize_with_system_speech(
            lines=[item["text"] for item in timeline],
            output_dir=output_dir / "narration-parts",
            voice_name=voice_name,
            rate=speech_rate,
        )

    if parts:
        metadata = write_audio_track(
            path=narration_path,
            parts=parts,
            timeline=timeline,
            duration=duration,
            ambient_level=0.035,
        )
        status = {
            "kind": "spoken_narration",
            "speech_available": True,
            "voice": voice_name,
            "rate": speech_rate,
            "timeline": timeline,
            "audio": metadata,
            "error": None,
        }
    else:
        if no_tts:
            tts_error = "TTS disabled with --no-tts."
        metadata = write_audio_track(
            path=narration_path,
            parts=[],
            timeline=timeline,
            duration=duration,
            ambient_level=0.055,
        )
        status = {
            "kind": "ambient_fallback",
            "speech_available": False,
            "voice": voice_name,
            "rate": speech_rate,
            "timeline": timeline,
            "audio": metadata,
            "error": tts_error,
            "note": (
                "The host speech engine did not return a usable WAV. "
                "The video keeps the on-screen narration captions and a subtle audio bed."
            ),
        }
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return narration_path, status


def run_ffmpeg_mux(
    ffmpeg: str,
    *,
    video: Path,
    narration: Path,
    output: Path,
    silent_output: Path,
    end_hold_seconds: float,
) -> None:
    common = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video),
    ]
    video_filter = (
        [
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={end_hold_seconds:g}",
        ]
        if end_hold_seconds > 0
        else []
    )
    subprocess.run(
        common
        + [
            "-i",
            str(narration),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            *video_filter,
            "-preset",
            "slow",
            "-crf",
            "18",
            "-r",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    subprocess.run(
        common
        + [
            "-an",
            "-c:v",
            "libx264",
            *video_filter,
            "-preset",
            "slow",
            "-crf",
            "18",
            "-r",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(silent_output),
        ],
        check=True,
    )


MUX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; background: #07131b; color: white; }
  video { width: 100vw; height: auto; }
</style>
</head>
<body>
<video id="source" muted playsinline preload="auto"></video>
<script>
window.__muxBlob = null;
window.__muxInfo = null;

function waitForMetadata(video) {
  if (video.readyState >= 1) return Promise.resolve();
  return new Promise((resolve, reject) => {
    video.addEventListener("loadedmetadata", resolve, { once: true });
    video.addEventListener("error", () => reject(new Error("Video metadata failed")), { once: true });
  });
}

window.__muxVideo = async ({
  videoUrl,
  audioUrl,
  withAudio,
  fps,
  mime,
  endHoldSeconds,
}) => {
  const video = document.getElementById("source");
  video.src = videoUrl;
  video.load();
  await waitForMetadata(video);
  const holdMs = Math.max(0, Number(endHoldSeconds || 0) * 1000);

  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) throw new Error("Canvas 2D context unavailable");

  const canvasStream = canvas.captureStream(fps);
  const tracks = [...canvasStream.getVideoTracks()];
  let audioContext = null;
  let sourceNode = null;
  let destination = null;

  if (withAudio) {
    audioContext = new AudioContext({ sampleRate: 48000 });
    const response = await fetch(audioUrl);
    if (!response.ok) throw new Error(`Audio fetch failed: ${response.status}`);
    const bytes = await response.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(bytes.slice(0));
    destination = audioContext.createMediaStreamDestination();
    sourceNode = audioContext.createBufferSource();
    sourceNode.buffer = audioBuffer;
    sourceNode.connect(destination);
    tracks.push(...destination.stream.getAudioTracks());
  }

  const stream = new MediaStream(tracks);
  if (!MediaRecorder.isTypeSupported(mime)) {
    throw new Error(`Unsupported MediaRecorder MIME: ${mime}`);
  }
  const recorder = new MediaRecorder(stream, {
    mimeType: mime,
    videoBitsPerSecond: 6_500_000,
    audioBitsPerSecond: 192_000,
  });
  const chunks = [];
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  };

  let animationFrame = 0;
  let drawing = true;
  const draw = () => {
    if (!drawing) return;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    animationFrame = requestAnimationFrame(draw);
  };

  const ended = new Promise((resolve, reject) => {
    video.addEventListener("ended", resolve, { once: true });
    video.addEventListener("error", () => reject(new Error("Video playback failed")), { once: true });
  });

  recorder.start(250);
  draw();
  await audioContext?.resume();
  sourceNode?.start(audioContext.currentTime);
  await video.play();
  await ended;
  // Playwright can omit trailing frames on a static final card. Repainting the
  // last frame through canvas capture keeps the end card visible in the MP4.
  await new Promise((resolve) => setTimeout(resolve, holdMs));
  drawing = false;
  cancelAnimationFrame(animationFrame);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  await new Promise((resolve) => setTimeout(resolve, 250));
  recorder.stop();
  await new Promise((resolve) => {
    if (recorder.state === "inactive") return resolve();
    recorder.addEventListener("stop", resolve, { once: true });
  });
  sourceNode?.stop();
  await audioContext?.close();
  canvasStream.getTracks().forEach((track) => track.stop());

  const blob = new Blob(chunks, { type: mime });
  window.__muxBlob = blob;
  window.__muxInfo = {
    duration_seconds: video.duration + holdMs / 1000 + 0.25,
    source_duration_seconds: video.duration,
    end_hold_seconds: holdMs / 1000,
    width: canvas.width,
    height: canvas.height,
    with_audio: withAudio,
    mime: recorder.mimeType,
    bytes: blob.size,
  };
  return window.__muxInfo;
};

window.__triggerMuxDownload = (filename) => {
  if (!window.__muxBlob) throw new Error("No mux blob available");
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(window.__muxBlob);
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
};
</script>
</body>
</html>
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__mux.html":
            data = MUX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def log_message(self, format, *args):
        return


def start_static_server(directory: Path) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_address[1])


def browser_mux(
    *,
    video: Path,
    narration: Path,
    output: Path,
    silent_output: Path,
    browser_channel: str,
    fps: int,
    end_hold_seconds: float,
) -> dict[str, Any]:
    server, thread, port = start_static_server(video.parent)
    info: dict[str, Any] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                channel=browser_channel or None,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.goto(f"http://127.0.0.1:{port}/__mux.html", wait_until="domcontentloaded")
            base_url = f"http://127.0.0.1:{port}"
            mime = "video/mp4;codecs=avc1.42E01E,mp4a.40.2"
            for with_audio, target in ((True, output), (False, silent_output)):
                info[target.name] = page.evaluate(
                    """async ({args}) => window.__muxVideo(args)""",
                    {
                        "args": {
                            "videoUrl": f"{base_url}/{video.name}",
                            "audioUrl": f"{base_url}/{narration.name}",
                            "withAudio": with_audio,
                            "fps": fps,
                            "mime": mime if with_audio else "video/mp4;codecs=avc1.42E01E",
                            "endHoldSeconds": end_hold_seconds,
                        }
                    },
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                with page.expect_download(timeout=180_000) as download_info:
                    page.evaluate(
                        "__filename => window.__triggerMuxDownload(__filename)",
                        target.name,
                    )
                download = download_info.value
                download.save_as(str(target))
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return info


def scan_mp4(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    moov_offset = data.find(b"moov")
    return {
        "bytes": len(data),
        "has_ftyp": data[:32].find(b"ftyp") >= 0,
        "has_moov": moov_offset >= 0,
        "has_avc1": data.find(b"avc1") >= 0,
        "has_mp4a": data.find(b"mp4a") >= 0,
    }


def browser_video_metadata(path: Path, *, browser_channel: str) -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            channel=browser_channel or None,
            args=["--allow-file-access-from-files", "--autoplay-policy=no-user-gesture-required"],
        )
        page = browser.new_page()
        try:
            page.goto(path.as_uri(), wait_until="domcontentloaded")
            page.wait_for_function(
                """() => {
                    const video = document.querySelector("video");
                    return video && Number.isFinite(video.duration) && video.readyState >= 2;
                }""",
                timeout=30_000,
            )
            metadata = page.evaluate(
                """async () => {
                    const video = document.querySelector("video");
                    video.muted = true;
                    let audioTracks = null;
                    try {
                        await video.play();
                        await new Promise((resolve) => setTimeout(resolve, 350));
                        audioTracks = video.captureStream().getAudioTracks().length;
                        video.pause();
                    } catch (error) {
                        audioTracks = `probe-error: ${error}`;
                    }
                    return {
                        duration_seconds: video.duration,
                        width: video.videoWidth,
                        height: video.videoHeight,
                        ready_state: video.readyState,
                        media_error: video.error ? video.error.code : null,
                        audio_tracks_via_capture_stream: audioTracks,
                        can_play_h264_aac: video.canPlayType(
                            'video/mp4; codecs="avc1.42E01E, mp4a.40.2"'
                        ),
                    };
                }"""
            )
            return metadata
        finally:
            browser.close()


def parse_ffmpeg_metadata(ffmpeg: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    text = proc.stderr or proc.stdout
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    video_match = re.search(r"Video:\s*([^,\s]+).*?(\d{2,5})x(\d{2,5})", text)
    audio_match = re.search(r"Audio:\s*([^,\s]+)", text)
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "duration_seconds": duration,
        "video_codec": video_match.group(1) if video_match else None,
        "width": int(video_match.group(2)) if video_match else None,
        "height": int(video_match.group(3)) if video_match else None,
        "audio_codec": audio_match.group(1) if audio_match else None,
    }


def verify_outputs(
    *,
    output: Path,
    silent_output: Path,
    narration: Path,
    ffmpeg: str | None,
    browser_channel: str,
    end_hold_seconds: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "end_hold_seconds": end_hold_seconds,
        "files": {},
    }
    for path in (output, silent_output, narration):
        result["files"][path.name] = {
            "path": str(path),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "exists": path.is_file(),
        }
    result["main_boxes"] = scan_mp4(output)
    result["silent_boxes"] = scan_mp4(silent_output)
    if ffmpeg:
        result["main_ffmpeg"] = parse_ffmpeg_metadata(ffmpeg, output)
        result["silent_ffmpeg"] = parse_ffmpeg_metadata(ffmpeg, silent_output)
    else:
        result["main_browser"] = browser_video_metadata(
            output, browser_channel=browser_channel
        )
        result["silent_browser"] = browser_video_metadata(
            silent_output, browser_channel=browser_channel
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the ATR showcase MP4.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument("--marks", type=Path, default=None)
    parser.add_argument("--narration", type=Path, default=None)
    parser.add_argument("--narration-input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--silent-output", type=Path, default=None)
    parser.add_argument("--browser-channel", default="chrome")
    parser.add_argument("--voice-name", default="Microsoft Huihui Desktop")
    parser.add_argument("--speech-rate", type=int, default=-2)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--end-hold-seconds", type=float, default=3.6)
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--mp4-only", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    video = (args.video or output_dir / "atr-showcase-capture.webm").resolve()
    marks_path = (args.marks or output_dir / "showcase-marks.json").resolve()
    output = (args.output or output_dir / "atr-hackathon-demo-final.mp4").resolve()
    silent_output = (
        args.silent_output or output_dir / "atr-hackathon-demo-final-silent.mp4"
    ).resolve()
    if not video.is_file():
        raise SystemExit(f"Video capture not found: {video}")
    if not marks_path.is_file():
        raise SystemExit(f"Capture marks not found: {marks_path}")

    marks = json.loads(marks_path.read_text(encoding="utf-8"))
    narration, narration_status = prepare_audio(
        output_dir=output_dir,
        marks=marks,
        voice_name=args.voice_name,
        speech_rate=args.speech_rate,
        narration_override=args.narration_input,
        no_tts=args.no_tts,
    )
    print(f"Narration mode: {narration_status['kind']}")
    if not narration_status.get("speech_available"):
        print(f"Narration warning: {narration_status.get('error')}")

    ffmpeg, ffmpeg_error = load_imageio_ffmpeg()
    mux_mode = "ffmpeg"
    if ffmpeg:
        run_ffmpeg_mux(
            ffmpeg,
            video=video,
            narration=narration,
            output=output,
            silent_output=silent_output,
            end_hold_seconds=args.end_hold_seconds,
        )
    else:
        mux_mode = "browser_mediarecorder"
        print(f"imageio-ffmpeg unavailable: {ffmpeg_error}")
        browser_mux(
            video=video,
            narration=narration,
            output=output,
            silent_output=silent_output,
            browser_channel=args.browser_channel,
            fps=args.fps,
            end_hold_seconds=args.end_hold_seconds,
        )

    verification = verify_outputs(
        output=output,
        silent_output=silent_output,
        narration=narration,
        ffmpeg=ffmpeg,
        browser_channel=args.browser_channel,
        end_hold_seconds=args.end_hold_seconds,
    )
    verification["mux_mode"] = mux_mode
    verification["ffmpeg_error"] = ffmpeg_error
    verification["narration_status"] = narration_status
    verification_path = output_dir / "mux-verification.json"
    verification_path.write_text(
        json.dumps(verification, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    main_boxes = verification["main_boxes"]
    if not main_boxes.get("has_avc1") or not main_boxes.get("has_mp4a"):
        raise SystemExit(
            f"MP4 verification failed: expected avc1 + mp4a tracks, got {main_boxes}"
        )
    silent_boxes = verification["silent_boxes"]
    if not silent_boxes.get("has_avc1") or silent_boxes.get("has_mp4a"):
        raise SystemExit(
            f"Silent MP4 verification failed: expected video-only MP4, got {silent_boxes}"
        )

    print(f"Main video: {output}")
    print(f"Silent video: {silent_output}")
    print(f"Narration: {narration}")
    print(f"Verification: {verification_path}")
    print(f"Mux mode: {mux_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
