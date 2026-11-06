#!/usr/bin/env python3
"""Worker Kaggle: audio Drive -> Whisper large-v3 -> transcript grezzo Drive.

Non esegue operazioni di rete all'importazione. Il contratto di configurazione è
in README.md; le dipendenze vengono importate solo all'avvio effettivo.
"""
from __future__ import annotations

import importlib
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


def run_worker(drive: Any, config: Config, model_factory: Callable[[], Any],
               working_dir: str = WORKING_DIR,
               downloader: Callable[..., None] = download_audio,
               uploader: Callable[..., str] = upload_transcript) -> int:
    """Restituisce 1 se almeno un file fallisce; non modifica mai gli audio Drive."""
    query = f"'{escape_query(config.processing_folder_id)}' in parents and trashed = false"
    items = list_drive_files(drive, query, config.shared_drive_id)
    candidates = [f for f in items if is_audio(f)]
    LOG.info("Scansione completata: %d audio ammessi.", len(candidates))
    errors = 0
    model = None
    seen = set()
    for item in candidates:
        stage = "validazione"
        try:
            file_id, name, extension = validate_audio(item)
            if file_id in seen:
                continue
            stage = "verifica duplicati"
            if existing_transcript(drive, file_id, config.shared_drive_id):
                seen.add(file_id)
                LOG.info("Transcript già presente: audio ignorato.")
                continue
            stage = "inizializzazione modello"
            if model is None:
                model = model_factory()
            stage = "download"
            # La directory viene creata con un nome casuale e cancellata SOLO
            # dal suo context manager; il nome remoto non entra nel percorso.
            with tempfile.TemporaryDirectory(prefix="orpheus-", dir=working_dir) as temp_dir:
                local_path = str(Path(temp_dir) / ("audio" + extension))
                downloader(drive, file_id, local_path)
                stage = "trascrizione"
                result = model.transcribe(local_path, language=config.language,
                                          verbose=False, fp16=(getattr(model, "device", None) is not None
                                                               and str(model.device).startswith("cuda")))
                text = format_transcript(result)
                stage = "upload"
                uploader(drive, config, file_id, name, text)
            seen.add(file_id)
            LOG.info("Transcript grezzo caricato.")
        except Exception:
            errors += 1
            # Niente exception text/traceback: possono contenere path, URL o segreti.
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
        return run_worker(drive, config, lambda: whisper.load_model("large-v3", device=device))
    except Exception:
        # Anche la configurazione e i dettagli delle dipendenze possono contenere secret.
        LOG.error("Avvio o scansione del worker falliti; nessun successo dichiarato.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
