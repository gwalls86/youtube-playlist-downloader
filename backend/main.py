"""
YouTube Video Manager — FastAPI Backend
Descarga playlists, normaliza audio (EBU R128) y convierte a H.265.

Uso:
    pip install fastapi uvicorn
    python main.py

Puerto: 8004
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import tkinter as tk
from tkinter import filedialog
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = "yt_manager_config.json"

DEFAULT_VIDEO_EXTENSIONS = [".mp4", ".mkv", ".avi", ".mov"]
DEFAULT_VIDEO_FORMAT = (
    "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=720][ext=mp4]/best[height<=720]"
)
H265_PRESETS = ["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"]

if sys.platform == "win32":
    SUBPROCESS_FLAGS = 0x08000000
else:
    SUBPROCESS_FLAGS = 0

# ─────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DownloadConfig:
    playlist_url:   str = ""
    download_folder: str = ""
    error_log_path:  str = "errores.json"
    manifest_path:   str = "manifest.json"
    video_format:    str = DEFAULT_VIDEO_FORMAT

@dataclass
class NormalizeConfig:
    root_dir:                str   = ""
    output_subfolder:        str   = "_normalized"
    process_log_path:        str   = "log-normalized.json"
    error_log_path:          str   = "errors-normalized.json"
    target_i:                str   = "-14"
    target_lra:              str   = "11"
    target_tp:               str   = "-1.5"
    normalization_threshold: float = 1.0
    audio_codec:             str   = "aac"
    audio_bitrate:           str   = "128k"
    audio_sample_rate:       str   = "44100"
    skip_existing:           bool  = True
    delete_original:         bool  = False
    video_extensions:        List[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS))

@dataclass
class H265Config:
    root_dir:                str   = ""
    output_subfolder:        str   = "_normalized_h265"
    process_log_path:        str   = "log-normalized-h265.json"
    error_log_path:          str   = "errors-normalized-h265.json"
    target_i:                str   = "-14"
    target_lra:              str   = "11"
    target_tp:               str   = "-1.5"
    normalization_threshold: float = 1.0
    h265_crf:                int   = 28
    h265_preset:             str   = "slow"
    audio_codec:             str   = "aac"
    audio_bitrate:           str   = "128k"
    audio_sample_rate:       str   = "44100"
    skip_existing:           bool  = True
    delete_original:         bool  = False
    video_extensions:        List[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS))

@dataclass
class AppConfig:
    ffmpeg_path:  str = "ffmpeg"
    yt_dlp_path:  str = "yt-dlp"
    download:   DownloadConfig   = field(default_factory=DownloadConfig)
    normalize:  NormalizeConfig  = field(default_factory=NormalizeConfig)
    h265:       H265Config       = field(default_factory=H265Config)


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    task:         str   # "download" | "normalize" | "h265"
    ffmpeg_path:  str   = "ffmpeg"
    yt_dlp_path:  str   = "yt-dlp"
    # download
    playlist_url:    str = ""
    download_folder: str = ""
    error_log_path:  str = "errores.json"
    manifest_path:   str = "manifest.json"
    video_format:    str = DEFAULT_VIDEO_FORMAT
    # normalize / h265 shared
    root_dir:                str   = ""
    output_subfolder:        str   = "_normalized"
    process_log_path:        str   = "log-normalized.json"
    norm_error_log_path:     str   = "errors-normalized.json"
    target_i:                str   = "-14"
    target_lra:              str   = "11"
    target_tp:               str   = "-1.5"
    normalization_threshold: float = 1.0
    audio_codec:             str   = "aac"
    audio_bitrate:           str   = "128k"
    audio_sample_rate:       str   = "44100"
    skip_existing:           bool  = True
    delete_original:         bool  = False
    video_extensions:        List[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS))
    # h265 only
    h265_crf:     int = 28
    h265_preset:  str = "slow"

class ConfigSaveRequest(BaseModel):
    config: dict


# ─────────────────────────────────────────────────────────────────────────────
# WORKER BRIDGE
# ─────────────────────────────────────────────────────────────────────────────

class OperationCancelled(Exception):
    pass

class CancellationToken:
    def __init__(self):
        self._cancelled = threading.Event()
        self.active_process: Optional[subprocess.Popen] = None

    def cancel(self):
        self._cancelled.set()
        proc = self.active_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                for _ in range(10):
                    if proc.poll() is not None: break
                    time.sleep(0.1)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    def is_cancelled(self): return self._cancelled.is_set()
    def check(self):
        if self._cancelled.is_set():
            raise OperationCancelled()

class WorkerBridge:
    def __init__(self):
        self.q: queue.Queue[Tuple[str, Any]] = queue.Queue()
        self.cancel_token = CancellationToken()

    def log(self, message: str, level: str = "INFO"):
        self.q.put(("log", {"message": message, "level": level,
                             "ts": datetime.now().strftime("%H:%M:%S")}))

    def progress(self, current: int, total: int, label: str = ""):
        self.q.put(("progress", {"current": current, "total": total, "label": label}))

    def stats(self, data: dict):
        self.q.put(("stats", data))

    def status(self, text: str):
        self.q.put(("status", {"text": text}))

    def done(self, success: bool, summary: str = ""):
        self.q.put(("done", {"success": success, "summary": summary}))

    def drain(self):
        events = []
        while True:
            try: events.append(self.q.get_nowait())
            except queue.Empty: break
        return events


# ─────────────────────────────────────────────────────────────────────────────
# SUBPROCESS HELPER
# ─────────────────────────────────────────────────────────────────────────────

def run_subprocess_streaming(executable, args, bridge, *, log_output=False, capture_stderr=True):
    cmd = [executable] + args
    stderr_target = subprocess.STDOUT if capture_stderr else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_target,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1, creationflags=SUBPROCESS_FLAGS)
    except FileNotFoundError:
        raise RuntimeError(f"No se encontró el ejecutable: {executable}")

    bridge.cancel_token.active_process = proc
    output_lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                output_lines.append(line)
                if log_output:
                    bridge.log(f"  {line}", "DIM")
            if bridge.cancel_token.is_cancelled():
                break
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bridge.log("  ⚠ El proceso no respondió al terminar — forzando cierre", "WARNING")
            proc.kill()
            proc.wait()
    finally:
        bridge.cancel_token.active_process = None

    return proc.returncode, "\n".join(output_lines)


# ─────────────────────────────────────────────────────────────────────────────
# TAREA 1: DESCARGA
# ─────────────────────────────────────────────────────────────────────────────

def _load_json_safe(path):
    if not Path(path).exists(): return None
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except: return None

def _save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def _slugify(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    name = re.sub(r"[_\s]+", " ", name).strip()
    return name[:60].rstrip()

def _extract_playlist_id(url):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None

def _get_playlist_info(playlist_url, yt_dlp_path, bridge):
    bridge.log("Obteniendo lista de videos de la playlist…", "INFO")
    args = ["--flat-playlist", "--dump-json", "--no-warnings", playlist_url]
    exit_code, output = run_subprocess_streaming(yt_dlp_path, args, bridge)
    if exit_code != 0:
        raise RuntimeError(f"yt-dlp falló al obtener la playlist (código {exit_code})")

    videos = []
    playlist_id = _extract_playlist_id(playlist_url) or "desconocido"
    playlist_title = "Sin título"

    for line in output.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")): continue
        try:
            v = json.loads(line)
            if v.get("id") and v.get("title"):
                if not playlist_title or playlist_title == "Sin título":
                    pt = v.get("playlist_title") or v.get("playlist") or ""
                    if pt: playlist_title = pt
                    pid = v.get("playlist_id") or v.get("playlist") or ""
                    if pid and playlist_id == "desconocido": playlist_id = pid
                videos.append({
                    "id": v["id"], "title": v["title"],
                    "url": f"https://www.youtube.com/watch?v={v['id']}"
                })
        except Exception:
            continue

    bridge.log(f"✓ {len(videos)} videos encontrados — playlist: {playlist_title}", "SUCCESS")
    return videos, playlist_id, playlist_title

def _load_manifest(path):
    data = _load_json_safe(path)
    if isinstance(data, dict) and "videos" in data: return data
    return {"playlistId": None, "playlistTitle": None, "videos": {}}

def _save_manifest(manifest, path):
    _save_json(path, manifest)

def _get_next_number(manifest):
    nums = [v.get("numero", 0) for v in manifest["videos"].values()]
    return (max(nums) + 1) if nums else 1

def _get_existing_files(folder):
    result = {}
    for f in Path(folder).iterdir():
        if f.is_file():
            m = re.search(r'\[([A-Za-z0-9_-]{11})\]', f.name)
            if m: result[m.group(1)] = f
    return result

def _find_downloaded_file(folder, video_id):
    for f in Path(folder).iterdir():
        if f.is_file() and video_id in f.name:
            return f
    return None

def _append_download_error(error_log_path, numero, video_id, url, exit_code, error_output):
    try:
        entry = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "numero": numero, "videoId": video_id, "url": url,
                 "exitCode": exit_code, "errorOutput": error_output[-800:]}
        errors = []
        existing = _load_json_safe(error_log_path)
        if isinstance(existing, list): errors = existing
        errors.append(entry)
        _save_json(error_log_path, errors)
    except Exception:
        pass

def run_download(req: StartRequest, bridge: WorkerBridge):
    yt_dlp = req.yt_dlp_path
    if not (Path(yt_dlp).is_file() or shutil.which(yt_dlp)):
        raise FileNotFoundError(f"yt-dlp no encontrado: {yt_dlp}")
    if not req.playlist_url.strip():
        raise ValueError("Debes ingresar la URL de la playlist")

    bridge.log("Verificando yt-dlp…", "INFO")
    rc, ver = run_subprocess_streaming(yt_dlp, ["--version"], bridge)
    if rc == 0:
        bridge.log(f"✓ yt-dlp {ver.strip()}", "SUCCESS")

    playlist_videos, playlist_id, playlist_title = _get_playlist_info(req.playlist_url, yt_dlp, bridge)
    bridge.cancel_token.check()

    download_folder = Path(req.download_folder)
    if not download_folder.exists():
        download_folder.mkdir(parents=True, exist_ok=True)

    # Sub-carpeta por playlist
    folder_name = _slugify(playlist_title)
    playlist_folder = download_folder / f"{folder_name} [{playlist_id}]"
    playlist_folder.mkdir(parents=True, exist_ok=True)

    manifest_path = Path(req.manifest_path)
    manifest = _load_manifest(str(manifest_path))

    if not manifest["videos"]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        manifest["playlistId"] = playlist_id
        manifest["playlistTitle"] = playlist_title
        for i, v in enumerate(playlist_videos, 1):
            manifest["videos"][v["id"]] = {"numero": i, "titulo": v["title"], "fechaAgregado": now}
        _save_manifest(manifest, str(manifest_path))
        bridge.log(f"Manifest creado con {len(playlist_videos)} videos", "SUCCESS")
    else:
        agregados = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for v in playlist_videos:
            if v["id"] not in manifest["videos"]:
                numero = _get_next_number(manifest)
                manifest["videos"][v["id"]] = {"numero": numero, "titulo": v["title"], "fechaAgregado": now}
                agregados += 1
                bridge.log(f"Nuevo video #{numero}: {v['title']}", "INFO")
        if agregados:
            bridge.log(f"Se agregaron {agregados} videos nuevos al manifest", "SUCCESS")
            _save_manifest(manifest, str(manifest_path))

    existing_files = _get_existing_files(str(playlist_folder))
    bridge.log(f"Archivos ya descargados: {len(existing_files)}", "INFO")

    to_download = []
    for v in playlist_videos:
        if v["id"] not in existing_files:
            info = manifest["videos"].get(v["id"], {})
            to_download.append({"numero": int(info.get("numero", 0)), "id": v["id"],
                                 "titulo": v["title"], "url": v["url"]})
    to_download.sort(key=lambda x: x["numero"])

    bridge.log(f"Videos a descargar: {len(to_download)} de {len(playlist_videos)}", "SUCCESS")
    if not to_download:
        bridge.log("¡Todos los videos ya están descargados!", "SUCCESS")
        return

    ffmpeg_extra = []
    if req.ffmpeg_path and (Path(req.ffmpeg_path).is_file() or shutil.which(req.ffmpeg_path)):
        ffmpeg_extra = ["--ffmpeg-location", req.ffmpeg_path]

    stats = {"ok": 0, "skip": 0, "fail": 0}
    total = len(to_download)
    error_log_path = Path(req.error_log_path)

    for i, video in enumerate(to_download, 1):
        bridge.cancel_token.check()
        numero = video["numero"]
        video_id = video["id"]
        numero_str = f"{numero:04d}"
        output_template = str(playlist_folder / f"{numero_str} - %(title)s [{video_id}].%(ext)s")

        bridge.progress(i, total, f"#{numero} — {video['titulo']}")
        bridge.log(f"[{i}/{total}] Descargando #{numero}: {video['titulo']}", "INFO")

        dl_args = ["-f", req.video_format, "--merge-output-format", "mp4",
                   "-o", output_template, "--no-playlist", "--no-warnings",
                   "--windows-filenames"] + ffmpeg_extra + [video["url"]]

        try:
            exit_code, output = run_subprocess_streaming(yt_dlp, dl_args, bridge)
            if bridge.cancel_token.is_cancelled(): raise OperationCancelled()
            if exit_code == 0:
                time.sleep(1.0)
                downloaded = _find_downloaded_file(str(playlist_folder), video_id)
                if downloaded and Path(downloaded).exists():
                    size_mb = Path(downloaded).stat().st_size / (1024 * 1024)
                    if "has already been downloaded" in output:
                        stats["skip"] += 1
                        bridge.log(f"  ⊙ Ya existía ({size_mb:.2f} MB)", "WARNING")
                    else:
                        stats["ok"] += 1
                        bridge.log(f"  ✓ OK ({size_mb:.2f} MB)", "SUCCESS")
                else:
                    stats["skip"] += 1
                    bridge.log("  ⊙ Saltado", "WARNING")
            else:
                stats["fail"] += 1
                bridge.log(f"  ✗ Error (código: {exit_code})", "ERROR")
                _append_download_error(str(error_log_path), numero, video_id, video["url"], exit_code, output)
        except OperationCancelled:
            raise
        except Exception as e:
            stats["fail"] += 1
            bridge.log(f"  ✗ {e}", "ERROR")

        bridge.stats({"ok": stats["ok"], "skip": stats["skip"], "fail": stats["fail"],
                      "total": total, "current": i})
        time.sleep(0.3)

    bridge.log(f"Playlist: {playlist_title}  ·  Descargados: {stats['ok']}  ·  Omitidos: {stats['skip']}  ·  Fallidos: {stats['fail']}", "SUCCESS")


# ─────────────────────────────────────────────────────────────────────────────
# TAREAS 2 & 3: NORMALIZACIÓN Y H.265
# ─────────────────────────────────────────────────────────────────────────────

def _get_loudnorm_measurements(file_path, ffmpeg_path, bridge):
    args = ["-hide_banner", "-i", str(file_path),
            "-af", "loudnorm=print_format=json:dual_mono=true", "-f", "null", "-"]
    try:
        exit_code, output = run_subprocess_streaming(ffmpeg_path, args, bridge)
    except Exception as e:
        return {"success": False, "error": str(e)}

    match = re.search(r'\{(?:[^{}]|(?:\{[^{}]*\}))*"input_i"[^}]*\}', output)
    if not match:
        return {"success": False, "error": "No se pudo parsear JSON de loudnorm"}
    try:
        s = json.loads(match.group(0))
        return {"success": True, "input_i": float(s["input_i"]), "input_tp": float(s["input_tp"]),
                "input_lra": float(s["input_lra"]), "input_thresh": float(s["input_thresh"]),
                "target_offset": float(s["target_offset"])}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _process_single_video(*, input_path, output_path, ffmpeg_path, target_i, target_lra,
                           target_tp, normalization_threshold, audio_codec, audio_bitrate,
                           audio_sample_rate, h265_crf, h265_preset, bridge):
    file_name = Path(input_path).name
    total_steps = 3 if h265_crf is not None else 2

    bridge.log(f"Procesando: {file_name}", "INFO")
    temp_name = f"ffmpeg_{uuid.uuid4()}.mp4"
    temp_output = Path(os.environ.get("TEMP", "/tmp")) / temp_name

    try:
        bridge.log(f"  [1/{total_steps}] Analizando niveles de audio…", "INFO")
        m = _get_loudnorm_measurements(input_path, ffmpeg_path, bridge)
        bridge.cancel_token.check()

        needs_norm = True
        if m["success"]:
            diff = abs(m["input_i"] - float(target_i))
            bridge.log(f"  Loudness: {m['input_i']:.2f} LUFS → target {target_i} LUFS (Δ{diff:.2f})", "INFO")
            needs_norm = diff > normalization_threshold
        else:
            bridge.log("  ⚠ No se pudo medir audio — se aplicará normalización por defecto", "WARNING")

        loudnorm_filter = None
        if needs_norm:
            bridge.log(f"  [2/{total_steps}] {'Normalizando audio…' if h265_crf is None else 'Audio requiere normalización'}", "INFO")
            if m["success"]:
                loudnorm_filter = (f"loudnorm=linear=true:i={target_i}:lra={target_lra}:tp={target_tp}:"
                                   f"measured_i={m['input_i']}:measured_lra={m['input_lra']}:"
                                   f"measured_tp={m['input_tp']}:measured_thresh={m['input_thresh']}:"
                                   f"offset={m['target_offset']}:print_format=summary")
            else:
                loudnorm_filter = f"loudnorm=i={target_i}:lra={target_lra}:tp={target_tp}:print_format=summary"
        else:
            bridge.log(f"  [2/{total_steps}] Audio ya normalizado — {'copiando' if h265_crf is None else 'omitiendo normalización'}", "INFO")

        if h265_crf is not None:
            bridge.log(f"  [3/{total_steps}] Convirtiendo a H.265 (CRF {h265_crf}, preset {h265_preset})…", "INFO")

        args = ["-hide_banner", "-i", str(input_path)]
        if h265_crf is not None:
            args += ["-c:v", "libx265", "-crf", str(h265_crf), "-preset", str(h265_preset), "-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]

        if loudnorm_filter:
            args += ["-af", loudnorm_filter, "-c:a", audio_codec, "-b:a", audio_bitrate, "-ar", audio_sample_rate]
        else:
            if h265_crf is not None:
                args += ["-c:a", audio_codec, "-b:a", audio_bitrate, "-ar", audio_sample_rate]
            else:
                args += ["-c:a", "copy"]

        args += ["-c:s", "copy", "-map", "0", "-movflags", "+faststart", "-y", str(temp_output)]

        start = datetime.now()
        exit_code, output = run_subprocess_streaming(ffmpeg_path, args, bridge)
        elapsed = (datetime.now() - start).total_seconds() / 60

        if bridge.cancel_token.is_cancelled(): raise OperationCancelled()
        if exit_code != 0:
            raise RuntimeError(f"ffmpeg falló con código {exit_code}. {output[-300:]}")
        if not temp_output.exists():
            raise RuntimeError("El archivo de salida no se creó")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if Path(output_path).exists(): Path(output_path).unlink()
        try:
            temp_output.rename(output_path)
        except OSError:
            shutil.move(str(temp_output), str(output_path))

        orig_mb = Path(input_path).stat().st_size / (1024*1024)
        new_mb  = Path(output_path).stat().st_size / (1024*1024)
        comp    = round((1 - new_mb/orig_mb)*100, 1) if orig_mb > 0 else 0

        bridge.log(f"  ✓ {orig_mb:.1f} MB → {new_mb:.1f} MB ({comp:+.1f}%) en {elapsed:.1f} min", "SUCCESS")
        result = {"success": True, "originalSize": orig_mb, "newSize": new_mb,
                  "durationMinutes": round(elapsed, 2), "normalized": needs_norm}
        if h265_crf is not None: result["compression"] = comp
        return result

    except OperationCancelled:
        if temp_output.exists():
            try: temp_output.unlink()
            except: pass
        raise
    except Exception as e:
        bridge.log(f"  ✗ {e}", "ERROR")
        if temp_output.exists():
            try: temp_output.unlink()
            except: pass
        return {"success": False, "error": str(e)}

def _run_normalize_pipeline(*, req: StartRequest, bridge: WorkerBridge,
                              h265_crf, h265_preset, pipeline_title, stage_label):
    ffmpeg = req.ffmpeg_path
    if not (Path(ffmpeg).is_file() or shutil.which(ffmpeg)):
        raise RuntimeError(f"ffmpeg no encontrado: {ffmpeg}")
    bridge.log(f"✓ ffmpeg: {ffmpeg}", "SUCCESS")

    root = Path(req.root_dir)
    if not root.exists():
        raise RuntimeError(f"Carpeta no existe: {root}")

    # Verificar loudnorm disponible
    try:
        _, filters = run_subprocess_streaming(ffmpeg, ["-hide_banner", "-filters"], bridge)
        if "loudnorm" not in filters:
            raise RuntimeError("Tu versión de ffmpeg no tiene el filtro 'loudnorm'. Actualiza ffmpeg.")
    except OperationCancelled:
        raise
    except RuntimeError:
        raise
    except Exception as e:
        bridge.log(f"⚠ No se pudo verificar filtros de ffmpeg: {e}", "WARNING")

    exts = {e.lower() for e in req.video_extensions}
    videos = sorted([f for f in root.iterdir() if f.is_file() and f.suffix.lower() in exts])
    bridge.log(f"✓ {len(videos)} videos encontrados en {root}", "SUCCESS")

    if not videos:
        bridge.log("No se encontraron videos para procesar", "WARNING")
        return

    output_dir = root / req.output_subfolder
    output_dir.mkdir(parents=True, exist_ok=True)

    to_process = []
    for v in videos:
        out = output_dir / v.name
        if req.skip_existing and out.exists():
            bridge.log(f"⊙ Omitiendo (ya existe): {v.name}", "DIM")
            continue
        to_process.append({"inputPath": v, "outputPath": out, "fileName": v.name})

    bridge.log(f"Videos a procesar: {len(to_process)} de {len(videos)}", "SUCCESS")
    if not to_process:
        bridge.log("¡No hay videos pendientes!", "SUCCESS")
        return

    process_log = {}
    if Path(req.process_log_path).exists():
        try: process_log = json.loads(Path(req.process_log_path).read_text(encoding="utf-8"))
        except: pass

    total_stats = {"ok": 0, "fail": 0, "total_orig": 0.0, "total_new": 0.0, "total_time": 0.0}
    total = len(to_process)

    for i, item in enumerate(to_process, 1):
        bridge.cancel_token.check()
        bridge.progress(i, total, item["fileName"])
        try:
            result = _process_single_video(
                input_path=item["inputPath"], output_path=item["outputPath"],
                ffmpeg_path=ffmpeg, target_i=req.target_i, target_lra=req.target_lra,
                target_tp=req.target_tp, normalization_threshold=req.normalization_threshold,
                audio_codec=req.audio_codec, audio_bitrate=req.audio_bitrate,
                audio_sample_rate=req.audio_sample_rate, h265_crf=h265_crf,
                h265_preset=h265_preset, bridge=bridge)
        except OperationCancelled:
            raise

        if result.get("success"):
            total_stats["ok"] += 1
            total_stats["total_orig"] += result["originalSize"]
            total_stats["total_new"]  += result["newSize"]
            total_stats["total_time"] += result["durationMinutes"]
            process_log[item["fileName"]] = {
                "processedDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **{k: result[k] for k in ("originalSize","newSize","durationMinutes","normalized") if k in result},
                **({"compression": result["compression"]} if "compression" in result else {}),
            }
            try: Path(req.process_log_path).write_text(json.dumps(process_log, indent=2), encoding="utf-8")
            except: pass
            if req.delete_original:
                try: item["inputPath"].unlink(); bridge.log("  🗑 Original eliminado", "WARNING")
                except Exception as e: bridge.log(f"  ⚠ No se pudo eliminar: {e}", "WARNING")
        else:
            total_stats["fail"] += 1

        bridge.stats({
            "ok": total_stats["ok"], "fail": total_stats["fail"],
            "total": total, "current": i,
            "saved_mb": round(total_stats["total_orig"] - total_stats["total_new"], 2),
            "total_time": round(total_stats["total_time"], 1),
        })
        time.sleep(0.3)

    saved = total_stats["total_orig"] - total_stats["total_new"]
    avg_comp = round((1 - total_stats["total_new"]/total_stats["total_orig"])*100, 1) if total_stats["total_orig"] > 0 else 0
    bridge.log(f"Completado · {total_stats['ok']} ok · {total_stats['fail']} errores · "
               f"{saved:.1f} MB ahorrados ({avg_comp}%) · {total_stats['total_time']:.1f} min", "SUCCESS")


# ─────────────────────────────────────────────────────────────────────────────
# ESTADO GLOBAL
# ─────────────────────────────────────────────────────────────────────────────

_worker_bridge: Optional[WorkerBridge] = None
_worker_thread: Optional[threading.Thread] = None
CONFIG_PATH = Path(__file__).parent / CONFIG_FILE

def _load_config():
    if CONFIG_PATH.exists():
        try: return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except: pass
    return {}

def _save_config(data):
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="YouTube Manager API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/config")
def get_config(): return _load_config()

@app.post("/api/config")
def save_config(req: ConfigSaveRequest):
    _save_config(req.config); return {"ok": True}

@app.get("/api/presets")
def get_presets(): return {"h265_presets": H265_PRESETS, "default_video_format": DEFAULT_VIDEO_FORMAT}

@app.post("/api/start")
def start_task(req: StartRequest):
    global _worker_bridge, _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return {"ok": False, "error": "Ya hay una tarea en curso"}

    _worker_bridge = WorkerBridge()
    bridge = _worker_bridge

    def worker():
        try:
            if req.task == "download":
                run_download(req, bridge)
            elif req.task == "normalize":
                _run_normalize_pipeline(req=req, bridge=bridge, h265_crf=None, h265_preset=None,
                                        pipeline_title="NORMALIZACIÓN DE AUDIO (video sin cambios)",
                                        stage_label="audio_normalization")
            elif req.task == "h265":
                _run_normalize_pipeline(req=req, bridge=bridge, h265_crf=req.h265_crf,
                                        h265_preset=req.h265_preset,
                                        pipeline_title="NORMALIZACIÓN + CONVERSIÓN H.265",
                                        stage_label="processing")
            if not bridge.cancel_token.is_cancelled():
                bridge.done(True, "Tarea completada")
        except OperationCancelled:
            bridge.log("Operación cancelada", "WARNING")
            bridge.done(False, "Cancelado")
        except Exception as exc:
            bridge.log(f"Error: {exc}", "ERROR")
            bridge.log(traceback.format_exc(), "DIM")
            bridge.done(False, str(exc))

    _worker_thread = threading.Thread(target=worker, daemon=True)
    _worker_thread.start()
    return {"ok": True}

@app.post("/api/stop")
def stop_task():
    if _worker_bridge:
        _worker_bridge.cancel_token.cancel()
        return {"ok": True}
    return {"ok": False, "error": "Sin tarea activa"}

@app.get("/api/status")
def get_status():
    return {"running": bool(_worker_thread and _worker_thread.is_alive())}

@app.get("/api/events")
async def event_stream(request: Request):
    async def generator():
        while True:
            if await request.is_disconnected(): break
            bridge = _worker_bridge
            if bridge:
                for t, p in bridge.drain():
                    yield f"data: {json.dumps({'type': t, 'payload': p})}\n\n"
            await asyncio.sleep(0.04)
    return StreamingResponse(generator(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/shutdown")
def shutdown():
    import os
    import signal
    import threading
    
    def kill_soon():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
        
    threading.Thread(target=kill_soon).start()
    return {"ok": True}

@app.get("/api/select-folder")
def select_folder():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder_path = filedialog.askdirectory()
    root.destroy()
    return {"folder": folder_path}

@app.get("/")
def read_root():
    return FileResponse("../frontend/index.html")

@app.get("/icon.png")
def read_icon():
    return FileResponse("../frontend/icon.png")

if __name__ == "__main__":
    import sys
    with open("server.log", "w", encoding="utf-8") as f:
        f.write("Iniciando servidor...\n")
    try:
        print("=" * 55)
        print("  YouTube Manager — Backend local")
        print("  http://localhost:8005")
        print("  Abre frontend/index.html en tu navegador")
        print("=" * 55)
        uvicorn.run(app, host="127.0.0.1", port=8005, log_level="info")
    except Exception as e:
        with open("server.log", "a", encoding="utf-8") as f:
            f.write(f"Error en el servidor: {str(e)}\n")
            import traceback
            traceback.print_exc(file=f)
        print(f"Error al iniciar el servidor: {e}")
