#!/usr/bin/env python3
"""Worker Kaggle: audio Drive -> Whisper large-v3 -> transcript grezzo Drive.

Non esegue operazioni di rete all'importazione. Il contratto di configurazione è
in README.md; le dipendenze vengono importate solo all'avvio effettivo.
"""
from __future__ import annotations

import importlib
import hashlib
import gc
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

CONFIG_ENV = "ORPHEUS_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/kaggle/input/orpheus-private-config/config.json"
WORKING_DIR = "/kaggle/working"
LOG_NAME = "orpheus-worker.log"
SCOPE = "https://www.googleapis.com/auth/drive"
KIND = "raw_transcript"
PLAN_KIND = "subject_plan"
SOURCE_KEY = "orpheus_source_id"
KIND_KEY = "orpheus_kind"
NAME_KEY = "orpheus_original_name"
# MIME generico ammesso soltanto con una delle estensioni audio esplicite.
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".webm", ".aac", ".aiff", ".aif", ".amr"})
AUDIO_MIMES = frozenset({
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/m4a",
    "audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave", "audio/flac",
    "audio/x-flac", "audio/ogg", "audio/opus", "audio/webm", "audio/aac",
    "audio/aiff", "audio/x-aiff", "audio/amr", "application/octet-stream",
})
DEPENDENCIES = (
    ("google.oauth2.service_account", "google-auth==2.38.0"),
    ("googleapiclient.discovery", "google-api-python-client==2.167.0"),
    ("whisper", "openai-whisper==20240930"),
    ("transformers", "transformers==4.49.0"),
    ("sentencepiece", "sentencepiece==0.2.0"),
)
LOG = logging.getLogger("orpheus.kaggle")


@dataclass(frozen=True)
class Config:
    credentials_path: str
    processing_folder_id: str
    transcripts_folder_id: str
    shared_drive_id: str | None = None
    language: str | None = "it"
    require_gpu: bool = True
    device: str = "cuda"
    bootstrap_dependencies: bool = True


def load_config(path: str | None = None) -> Config:
    """Carica il JSON privato; non copia né emette il suo contenuto."""
    path = path or os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as stream:
        obj = json.load(stream)
    if not isinstance(obj, dict):
        raise ValueError("config must be a JSON object")
    allowed = set(Config.__dataclass_fields__)
    if set(obj) - allowed:
        raise ValueError("unknown configuration fields")
    for key in ("credentials_path", "processing_folder_id", "transcripts_folder_id"):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise ValueError("missing required configuration field")
    if not os.path.isabs(obj["credentials_path"]):
        raise ValueError("credentials path must be absolute")
    if obj["processing_folder_id"] == obj["transcripts_folder_id"]:
        raise ValueError("processing and destination folders must differ")
    for key in ("require_gpu", "bootstrap_dependencies"):
        if key in obj and type(obj[key]) is not bool:
            raise ValueError("invalid boolean configuration field")
    if obj.get("device", "cuda") not in ("cuda", "cpu", "auto"):
        raise ValueError("invalid device")
    if obj.get("require_gpu", True) and obj.get("device", "cuda") == "cpu":
        raise ValueError("CPU cannot satisfy require_gpu")
    for key in ("shared_drive_id", "language"):
        value = obj.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("invalid optional configuration field")
    return Config(**obj)


def configure_logging(working_dir: str = WORKING_DIR) -> None:
    """Log esclusivamente di messaggi fissi, senza dati di configurazione o eccezioni."""
    os.makedirs(working_dir, exist_ok=True)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), logging.FileHandler(
        os.path.join(working_dir, LOG_NAME), encoding="utf-8"
    )):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def ensure_dependencies(bootstrap: bool) -> None:
    """Installa esplicitamente solo i pacchetti assenti, mai valori segreti."""
    missing = []
    for module, package in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return
    if not bootstrap:
        raise RuntimeError("required dependencies unavailable and bootstrap disabled")
    # Non emettere stdout/stderr di pip né un'eventuale eccezione con dettagli.
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *missing],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    importlib.invalidate_caches()
    for module, _ in DEPENDENCIES:
        importlib.import_module(module)


def select_device(config: Config, cuda_available: bool) -> str:
    if (config.require_gpu or config.device == "cuda") and not cuda_available:
        raise RuntimeError("CUDA GPU required but unavailable")
    if config.device == "cpu":
        return "cpu"
    return "cuda" if cuda_available else "cpu"


def create_drive(config: Config) -> Any:
    from google.oauth2 import credentials as user_credentials, service_account
    from googleapiclient.discovery import build
    with open(config.credentials_path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    credential_type = data.get("type") if isinstance(data, dict) else None
    if credential_type == "service_account":
        credentials = service_account.Credentials.from_service_account_file(
            config.credentials_path, scopes=[SCOPE]
        )
    elif credential_type == "authorized_user":
        credentials = user_credentials.Credentials.from_authorized_user_file(
            config.credentials_path, scopes=[SCOPE]
        )
    else:
        raise ValueError("unsupported credentials type")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_drive_files(drive: Any, query: str, shared_drive_id: str | None) -> list[dict]:
    """Lista completa o errore: incompleteSearch non autorizza una deduplica."""
    common = {
        "q": query,
        "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,appProperties)",
        "pageSize": 200,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }
    if shared_drive_id:
        common.update(corpora="drive", driveId=shared_drive_id)
    else:
        common["corpora"] = "allDrives"
    result: list[dict] = []
    token = None
    seen_tokens = set()
    while True:
        response = drive.files().list(pageToken=token, **common).execute(num_retries=4)
        if response.get("incompleteSearch"):
            raise RuntimeError("Drive search was incomplete")
        files = response.get("files", [])
        if not isinstance(files, list):
            raise RuntimeError("invalid Drive listing")
        result.extend(files)
        token = response.get("nextPageToken")
        if not token:
            return result
        if token in seen_tokens:
            raise RuntimeError("repeated Drive page token")
        seen_tokens.add(token)


def is_audio(item: dict) -> bool:
    name = item.get("name")
    mime = item.get("mimeType")
    return (isinstance(name, str) and isinstance(mime, str)
            and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS
            and mime.lower() in AUDIO_MIMES)


def validate_audio(item: dict) -> tuple[str, str, str]:
    """Il nome remoto non diventa mai un percorso sul filesystem locale."""
    if not is_audio(item):
        raise ValueError("unsupported audio file")
    file_id, name = item.get("id"), item.get("name")
    if not isinstance(file_id, str) or not file_id or not isinstance(name, str):
        raise ValueError("missing source id or name")
    if any(c in name for c in ("/", "\\")) or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("unsafe source name")
    stem, extension = os.path.splitext(name)
    if not stem or stem in (".", ".."):
        raise ValueError("invalid source name")
    for key, value in ((SOURCE_KEY, file_id), (KIND_KEY, KIND), (NAME_KEY, name)):
        # Limite Drive: 124 byte UTF-8 per chiave + valore di una proprietà.
        if len((key + value).encode("utf-8")) > 124:
            raise ValueError("Drive appProperty exceeds byte limit")
    return file_id, name, extension.lower()


def existing_transcript(drive: Any, source_id: str, shared_drive_id: str | None) -> bool:
    source = escape_query(source_id)
    query = ("trashed = false and "
             f"appProperties has {{ key='{SOURCE_KEY}' and value='{source}' }} and "
             f"appProperties has {{ key='{KIND_KEY}' and value='{KIND}' }}")
    # Ricerca globale nel corpus accessibile, non solo nella cartella di output:
    # il transcript potrebbe essere stato spostato nell'archivio.
    matches = list_drive_files(drive, query, shared_drive_id)
    return any((f.get("appProperties") or {}).get(SOURCE_KEY) == source_id
               and (f.get("appProperties") or {}).get(KIND_KEY) == KIND
               for f in matches)


def format_time(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("invalid segment timestamp")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid segment timestamp")
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_transcript(result: Any) -> str:
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        raise ValueError("invalid Whisper result")
    lines = []
    has_text = False
    for seg in result["segments"]:
        if not isinstance(seg, dict) or not isinstance(seg.get("text"), str):
            raise ValueError("invalid Whisper segment")
        start, end = seg.get("start"), seg.get("end")
        start_text, end_text = format_time(start), format_time(end)
        if end < start:
            raise ValueError("invalid segment interval")
        text = seg["text"]
        has_text = has_text or bool(text.strip())
        # Il testo del segmento è riportato integralmente, senza strip o riscritture.
        lines.append(f"[{start_text} - {end_text}] {text}\n")
    if not has_text:
        raise ValueError("Whisper returned no text")
    return "".join(lines)


def download_audio(drive: Any, file_id: str, destination: str) -> None:
    from googleapiclient.http import MediaIoBaseDownload
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destination, "wb") as stream:
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=4)


def upload_transcript(drive: Any, config: Config, file_id: str, original_name: str, text: str) -> str:
    from googleapiclient.http import MediaIoBaseUpload
    metadata = {
        "name": os.path.splitext(original_name)[0] + ".txt",
        "mimeType": "text/plain",
        "parents": [config.transcripts_folder_id],
        "appProperties": {SOURCE_KEY: file_id, KIND_KEY: KIND, NAME_KEY: original_name},
    }
    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain", resumable=False)
    # Non ritentare automaticamente una create ambigua: potrebbe aver creato il file.
    result = drive.files().create(body=metadata, media_body=media, fields="id",
                                  supportsAllDrives=True).execute(num_retries=0)
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("Drive create returned no id")
    return result["id"]


def _find_kind(drive: Any, source_id: str, kind: str, shared_drive_id: str | None) -> list[dict]:
    query = ("trashed = false and "
             f"appProperties has {{ key='{SOURCE_KEY}' and value='{escape_query(source_id)}' }} and "
             f"appProperties has {{ key='{KIND_KEY}' and value='{escape_query(kind)}' }}")
    matches = list_drive_files(drive, query, shared_drive_id)
    return [f for f in matches if (f.get("appProperties") or {}).get(SOURCE_KEY) == source_id
            and (f.get("appProperties") or {}).get(KIND_KEY) == kind]


def _download_bytes(drive: Any, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    stream = io.BytesIO()
    downloader = MediaIoBaseDownload(stream, drive.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=4)
    return stream.getvalue()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _upload_plan(drive: Any, cfg: Config, source_id: str, raw_id: str, raw_sha: str,
                 original_name: str, payload: dict) -> str:
    from googleapiclient.http import MediaIoBaseUpload
    content = _canonical(payload)
    plan_sha = hashlib.sha256(content).hexdigest()
    props = {SOURCE_KEY: source_id, KIND_KEY: PLAN_KIND, "orpheus_raw_id": raw_id,
             "orpheus_raw_sha256": raw_sha, "orpheus_plan_sha256": plan_sha,
             "orpheus_classifier_version": str(payload["version"]), NAME_KEY: original_name}
    metadata = {"name": os.path.splitext(original_name)[0] + ".subject-plan.json",
                "mimeType": "application/json", "parents": [cfg.transcripts_folder_id], "appProperties": props}
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json", resumable=False)
    result = drive.files().create(body=metadata, media_body=media, fields="id",
                                  supportsAllDrives=True).execute(num_retries=0)
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("Drive create returned no id")
    return result["id"]


def _verified_plan(drive: Any, cfg: Config, source_id: str, raw_id: str, raw_sha: str, original_name: str) -> bool:
    matches = _find_kind(drive, source_id, PLAN_KIND, cfg.shared_drive_id)
    if not matches: return False
    if len(matches) != 1: raise RuntimeError("ambiguous subject plan")
    item = matches[0]
    props = item.get("appProperties") or {}
    required = {SOURCE_KEY: source_id, KIND_KEY: PLAN_KIND, "orpheus_raw_id": raw_id,
                "orpheus_raw_sha256": raw_sha, NAME_KEY: original_name}
    if any(props.get(k) != v for k, v in required.items()): raise RuntimeError("subject plan metadata mismatch")
    try:
        content = _download_bytes(drive, item["id"])
        decoded = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("subject plan unreadable") from exc
    if hashlib.sha256(content).hexdigest() != props.get("orpheus_plan_sha256"):
        raise RuntimeError("subject plan hash mismatch")
    if decoded.get("source_id") != source_id or decoded.get("raw_id") != raw_id or decoded.get("raw_sha256") != raw_sha:
        raise RuntimeError("subject plan content mismatch")
    return True


def _read_raw(drive: Any, source_id: str, shared_drive_id: str | None) -> tuple[str, bytes] | None:
    matches = _find_kind(drive, source_id, KIND, shared_drive_id)
    if not matches: return None
    if len(matches) != 1: raise RuntimeError("ambiguous raw transcript")
    item = matches[0]
    raw = _download_bytes(drive, item["id"])
    if not raw: raise RuntimeError("empty raw transcript")
    raw.decode("utf-8")
    return item["id"], raw


def run_worker(drive: Any, config: Config, model_factory: Callable[[], Any],
               working_dir: str = WORKING_DIR, downloader: Callable[..., None] = download_audio,
               uploader: Callable[..., str] = upload_transcript,
               classifier_factory: Callable[..., Any] | None = None,
               plan_uploader: Callable[..., str] | None = None,
               classifier_device: str = "cpu") -> int:
    """Persist raw master prima della classificazione; non modifica/cancella audio Drive."""
    query = f"'{escape_query(config.processing_folder_id)}' in parents and trashed = false"
    candidates = [f for f in list_drive_files(drive, query, config.shared_drive_id) if is_audio(f)]
    LOG.info("Scansione completata: %d audio ammessi.", len(candidates))
    errors, model, seen = 0, None, set()
    for item in candidates:
        stage = "validazione"
        try:
            file_id, name, extension = validate_audio(item)
            if file_id in seen: continue
            stage = "verifica transcript raw"
            record = _read_raw(drive, file_id, config.shared_drive_id)
            if record is None:
                if model is None: model = model_factory()
                with tempfile.TemporaryDirectory(prefix="orpheus-", dir=working_dir) as td:
                    path = str(Path(td) / ("audio" + extension))
                    downloader(drive, file_id, path)
                    stage = "trascrizione"
                    result = model.transcribe(path, language=config.language, verbose=False,
                        fp16=(getattr(model, "device", None) is not None and str(model.device).startswith("cuda")))
                    text = format_transcript(result)
                raw_id = uploader(drive, config, file_id, name, text)
                raw_bytes = text.encode("utf-8")
            else:
                raw_id, raw_bytes = record
            raw_sha = hashlib.sha256(raw_bytes).hexdigest()
            if model is not None:
                model = None
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available(): torch.cuda.empty_cache()
                except Exception: pass
            stage = "verifica piano soggetto"
            if _verified_plan(drive, config, file_id, raw_id, raw_sha, name):
                seen.add(file_id); continue
            try:
                import subject_classifier
            except ModuleNotFoundError:
                from kaggle_kernel import subject_classifier
            classifier = (classifier_factory(device=classifier_device) if classifier_factory
                          else subject_classifier.load_classifier(device=classifier_device))
            result = subject_classifier.classify_transcript(raw_bytes.decode("utf-8"), classifier=classifier)
            payload = {"source_id": file_id, "raw_id": raw_id, "raw_sha256": raw_sha,
                       "source_model": result["source_model"], "version": result["version"],
                       "window_seconds": result["window_seconds"], "thresholds": result["thresholds"],
                       "segments": result["segments"]}
            stage = "persistenza piano soggetto"
            (plan_uploader or _upload_plan)(drive, config, file_id, raw_id, raw_sha, name, payload)
            seen.add(file_id)
            LOG.info("Transcript raw e piano soggetto elaborati.")
        except Exception:
            errors += 1
            LOG.error("Elaborazione fallita nella fase: %s.", stage)
    LOG.info("Fine ciclo: %d errori.", errors)
    return 1 if errors else 0


def main() -> int:
    try:
        configure_logging()
        config = load_config()
        ensure_dependencies(config.bootstrap_dependencies)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not available")
        import torch
        import whisper
        device = select_device(config, bool(torch.cuda.is_available()))
        drive = create_drive(config)
        return run_worker(drive, config, lambda: whisper.load_model("large-v3", device=device),
                          classifier_device=device)
    except Exception:
        # Anche la configurazione e i dettagli delle dipendenze possono contenere secret.
        LOG.error("Avvio o scansione del worker falliti; nessun successo dichiarato.")
        return 1


# ORPHEUS_CLASSIFIER_BUNDLE_POINT
if __name__ == "__main__":
    sys.exit(main())
