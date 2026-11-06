#!/usr/bin/env python3
"""Un ciclo Drive -> Kaggle/Whisper -> materia dichiarata nel nome -> lezioni.

CONTRATTI OPERATIVI (da predisporre separatamente; questo file non modifica il workflow):
* Il workflow deve serializzare TUTTI gli esecutori della pipeline, anche manuali.
  Drive non offre qui un lock atomico né un vincolo di unicità: accessi concorrenti
  esterni possono creare duplicati e richiedono revisione. Non promettiamo exactly-once.
* Il kernel deve leggere un solo audio in FOLDER_PROCESSING_KAGGLE, usare Whisper e
  pubblicare una sola trascrizione UTF-8 con righe timestamp [MM:SS - MM:SS] testo
  (anche HH:MM:SS), senza alterare il testo trascritto. Deve deduplicare per ID audio
  e creare appProperties: orpheus_source_id=<ID audio>, orpheus_kind=raw_transcript,
  orpheus_original_name=<nome originale audio>. Stessa app Drive/identità per vedere
  le appProperties private. Il worker attualmente nel repository NON implementa il
  contratto, e la metadata Kaggle contiene segnaposto: aggiornare/verificare entrambi
  nel repository; la flag ORPHEUS_WORKER_CONTRACT_VERIFIED resta obbligatoria prima
  di qualsiasi push. Non tentiamo di dedurre l'ID dal nome.
* Le credenziali Drive sono GOOGLE_AUTHORIZED_USER_JSON (JSON OAuth authorized_user)
  oppure GOOGLE_SERVICE_ACCOUNT_KEY (JSON service_account). Il workflow deve esporre
  la variabile scelta, GEMINI_API_KEY, credenziali Kaggle, gli ID FOLDER_* originali,
  KAGGLE_KERNEL_SLUG e KAGGLE_KERNEL_DIR. OAuth e worker devono avere accesso alle
  stesse cartelle; non si presume accesso MyDrive per un service account.
* Nessuna chiamata Gemini senza CLASSIFIER_MODE=gemini e, separatamente,
  ALLOW_GEMINI_IF_TERMS_MET=true. Questi flag NON attestano automaticamente i
  termini di servizio, la liceità dell'uso personale o il consenso degli interessati:
  chi configura il sistema deve verificarli preventivamente. Per default la
  classificazione resta in attesa; la sola trascrizione non richiede Gemini.
* Ogni lezione singola deve avere nel nome data, ora iniziale e materia, es.
  2026-11-06_08-00_biologia.m4a. Sono ammessi solo i sei nomi esatti. Una registrazione
  che contiene più lezioni/giorno senza questa informazione non è smistabile e resta
  in revisione; non inventiamo orari o materie. Non si usa ORARIO_PATH.
* MATERIE_FOLDER_IDS, opzionale, è un oggetto JSON con i sei nomi esatti e gli ID
  cartella. Altrimenti si cercano biologia/chimica/fisica/inglese direttamente nella
  radice e letteratura/purgatorio direttamente sotto italiano; ogni materia deve
  contenere una sola sottocartella esistente di nome lezioni. Nessuna cartella
  preesistente viene creata, rinominata o modificata.
* Il piano classificatore è un indice di righe, non una trascrizione. Viene validato
  e memorizzato sulla trascrizione prima di qualsiasi creazione, e riutilizzato per
  riparare le interruzioni. Gemini è disattivato salvo due flag espliciti e solo se
  chi lo autorizza ha verificato termini/licenza e il caso d'uso: non è disponibile
  per uso consumer/personale o dati di minori dove i termini lo escludano. Non fare
  chiamate reali di prova. I file lezione sono verificati rileggendone i byte.
  Errori ambigui o duplicati non vengono risolti scegliendo un candidato a caso.
* Nessuna cancellazione o pulizia automatica Drive. La pulizia futura richiederebbe
  una funzione esplicitamente attivata e una verifica separata; qui non esiste.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime

LOG = logging.getLogger("orchestratore")
FOLDER_MIME = "application/vnd.google-apps.folder"
SUBJECTS = ("biologia", "chimica", "fisica", "inglese", "letteratura", "purgatorio")
DIRECT_SUBJECTS = SUBJECTS[:4]
DRIVE_KW = {"supportsAllDrives": True}
LIST_KW = {"supportsAllDrives": True, "includeItemsFromAllDrives": True, "corpora": "allDrives"}
FIELDS = "id,name,mimeType,parents,trashed,appProperties"
DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})(?:-(\d{2}))?(?![\d-])")
LINE_RE = re.compile(r"^\[(\d{2,}:\d{2}(?::\d{2})?) - (\d{2,}:\d{2}(?::\d{2})?)\] (\S.*)$")
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".webm", ".mp4", ".amr", ".aiff"}
PLAN_CHUNKS = 16  # limite conservativo anche per i limiti delle proprietà private Drive
PLAN_CHUNK_SIZE = 100


class ReviewRequired(Exception):
    """Condizione ambigua o dati insufficienti: nessun completamento automatico."""


class WaitRequired(Exception):
    """Prerequisito temporaneamente mancante, senza consumare tentativi."""


@dataclass(frozen=True)
class Config:
    incoming: str
    processing: str
    transcripts: str
    processed: str
    completed: str
    errors: str
    root: str
    kernel_slug: str
    kernel_dir: str = "kaggle_kernel"
    model: str = "gemini-2.5-flash-lite"
    subject_ids: str = ""
    worker_verified: bool = False
    max_attempts: int = 3
    retry_after: int = 1800
    classifier_mode: str = ""
    allow_gemini: bool = False

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        def required(key):
            value = e.get(key, "").strip()
            if not value:
                raise ValueError("Configurazione mancante: " + key)
            return value
        count = int(e.get("MAX_ATTEMPTS", "3"))
        delay = int(e.get("RETRY_AFTER_SEC", "1800"))
        if count < 1 or delay < 1:
            raise ValueError("MAX_ATTEMPTS e RETRY_AFTER_SEC devono essere positivi")
        cfg = cls(required("FOLDER_AUDIO_INCOMING"), required("FOLDER_PROCESSING_KAGGLE"),
                  required("FOLDER_TRASCRIZIONI"), required("FOLDER_TRASCRIZIONI_ELABORATE"),
                  required("FOLDER_AUDIO_COMPLETATI"), required("FOLDER_ERRORI"),
                  required("FOLDER_RADICE_MATERIE"), required("KAGGLE_KERNEL_SLUG"),
                  e.get("KAGGLE_KERNEL_DIR", "kaggle_kernel"),
                  e.get("GEMINI_MODEL", "gemini-2.5-flash-lite"), e.get("MATERIE_FOLDER_IDS", ""),
                  e.get("ORPHEUS_WORKER_CONTRACT_VERIFIED") == "1", count, delay,
                  e.get("CLASSIFIER_MODE", ""), e.get("ALLOW_GEMINI_IF_TERMS_MET") == "true")
        if len({cfg.incoming, cfg.processing, cfg.transcripts, cfg.processed, cfg.completed, cfg.errors, cfg.root}) != 7:
            raise ValueError("Le cartelle di pipeline devono avere ID distinti")
        return cfg


class Drive:
    """Adapter Drive: import delle librerie solo quando utilizzate."""
    def __init__(self, service):
        self.service = service

    @classmethod
    def from_env(cls):
        from googleapiclient.discovery import build
        scopes = ["https://www.googleapis.com/auth/drive"]
        if os.environ.get("GOOGLE_AUTHORIZED_USER_JSON"):
            from google.oauth2.credentials import Credentials
            info = json.loads(os.environ["GOOGLE_AUTHORIZED_USER_JSON"])
            if info.get("type") != "authorized_user":
                raise ValueError("GOOGLE_AUTHORIZED_USER_JSON non è authorized_user")
            creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY"):
            from google.oauth2 import service_account
            info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
            if info.get("type") != "service_account":
                raise ValueError("GOOGLE_SERVICE_ACCOUNT_KEY non è service_account")
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            raise ValueError("Credenziali Drive mancanti")
        return cls(build("drive", "v3", credentials=creds, cache_discovery=False))

    def list(self, parent):
        escaped = parent.replace("\\", "\\\\").replace("'", "\\'")
        result, token = [], None
        while True:
            response = self.service.files().list(
                q=f"'{escaped}' in parents and trashed=false", fields=f"nextPageToken,incompleteSearch,files({FIELDS})",
                pageSize=200, pageToken=token, **LIST_KW).execute(num_retries=4)
            if response.get("incompleteSearch"):
                raise ReviewRequired("Ricerca Drive incompleta")
            result.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return result

    def get(self, file_id):
        return self.service.files().get(fileId=file_id, fields=FIELDS, **DRIVE_KW).execute(num_retries=4)

    def set_props(self, file_id, updates):
        current = self.get(file_id).get("appProperties") or {}
        body = {"appProperties": {**current, **updates}}
        self.service.files().update(fileId=file_id, body=body, fields="id", **DRIVE_KW).execute(num_retries=4)

    def move(self, file_id, parent):
        current = self.get(file_id)
        parents = current.get("parents") or []
        if parents == [parent]:
            return
        if not parents or parent in parents:
            raise ReviewRequired("Parent Drive ambiguo")
        self.service.files().update(fileId=file_id, addParents=parent, removeParents=",".join(parents),
                                    fields="id", **DRIVE_KW).execute(num_retries=4)
        if self.get(file_id).get("parents") != [parent]:
            raise ReviewRequired("Spostamento non verificato")

    def read(self, file_id):
        from googleapiclient.http import MediaIoBaseDownload
        req = self.service.files().get_media(fileId=file_id, **DRIVE_KW)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=4)
        return buf.getvalue()

    def create_folder(self, parent, name, props):
        body = {"name": name, "parents": [parent], "mimeType": FOLDER_MIME, "appProperties": props}
        # Nessun retry automatico su create: timeout può significare creazione riuscita.
        return self.service.files().create(body=body, fields=FIELDS, **DRIVE_KW).execute(num_retries=0)

    def create_text(self, parent, name, data, props):
        from googleapiclient.http import MediaIoBaseUpload
        body = {"name": name, "parents": [parent], "mimeType": "text/plain", "appProperties": props}
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="text/plain", resumable=False)
        return self.service.files().create(body=body, media_body=media, fields=FIELDS, **DRIVE_KW).execute(num_retries=0)


class Services:
    def __init__(self, cfg):
        self.cfg = cfg
        self._drive = self._kaggle = self._gemini = None

    def drive(self):
        if self._drive is None:
            self._drive = Drive.from_env()
        return self._drive

    def kaggle(self):
        if self._kaggle is None:
            from kaggle.api.kaggle_api_extended import KaggleApi
            self._kaggle = KaggleApi()
            self._kaggle.authenticate()
        return self._kaggle

    def gemini(self):
        if self._gemini is None:
            from google import genai
            self._gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        return self._gemini


def props(item):
    return item.get("appProperties") or {}


def is_audio(item):
    mime = (item.get("mimeType") or "").lower()
    ext = os.path.splitext(item.get("name", ""))[1].lower()
    return mime.startswith("audio/") or (mime in ("application/octet-stream", "video/mp4", "video/webm") and ext in AUDIO_EXTENSIONS)


def recording_start(name):
    matches = list(DATE_RE.finditer(name))
    if len(matches) != 1:
        raise ReviewRequired("Data e ora della registrazione assenti o ambigue")
    m = matches[0]
    try:
        return datetime.strptime(m.group(1) + "_" + m.group(2) + "-" + m.group(3) + "-" + (m.group(4) or "00"), "%Y-%m-%d_%H-%M-%S")
    except ValueError as exc:
        raise ReviewRequired("Data e ora non valide") from exc


def subject_from_name(name):
    """Accetta solo YYYY-MM-DD_HH-MM[-SS]_<subject>.<ext>, senza token liberi."""
    stem = os.path.splitext(os.path.basename(name))[0]
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)_(biologia|chimica|fisica|inglese|letteratura|purgatorio)", stem, re.IGNORECASE)
    if not m:
        raise ReviewRequired("Il nome deve terminare con timestamp e una materia esatta")
    recording_start(m.group(1))
    return m.group(2).lower()


def slugify(value):
    value = re.sub(r"[^a-z0-9àèéìòù]+", "_", value.lower().strip()).strip("_")
    return value[:40].strip("_") or "senza_titolo"


def _one_folder(items, name):
    found = [f for f in items if f.get("mimeType") == FOLDER_MIME and f.get("name", "").casefold() == name.casefold() and not f.get("trashed")]
    if len(found) != 1:
        raise ReviewRequired("Cartella richiesta mancante o ambigua")
    return found[0]["id"]


def build_subject_map(drive, cfg):
    """Ritorna materia -> ID della sottocartella lezioni già esistente."""
    if cfg.subject_ids:
        try:
            mapping = json.loads(cfg.subject_ids)
        except (ValueError, TypeError) as exc:
            raise ReviewRequired("Mapping materie non valido") from exc
        if not isinstance(mapping, dict) or set(mapping) != set(SUBJECTS):
            raise ReviewRequired("Mapping materie incompleto o inatteso")
        if any(not isinstance(i, str) or not i.strip() for i in mapping.values()) or len(set(mapping.values())) != len(SUBJECTS):
            raise ReviewRequired("ID materia non validi o duplicati")
    else:
        root_items = drive.list(cfg.root)
        mapping = {s: _one_folder(root_items, s) for s in DIRECT_SUBJECTS}
        italian_id = _one_folder(root_items, "italiano")
        italian_items = drive.list(italian_id)
        mapping.update({s: _one_folder(italian_items, s) for s in ("letteratura", "purgatorio")})
        if len(set(mapping.values())) != len(SUBJECTS):
            raise ReviewRequired("Cartelle materia duplicate")
    lessons = {}
    for subject, fid in mapping.items():
        folder = drive.get(fid)
        if (folder.get("id") != fid or folder.get("mimeType") != FOLDER_MIME or folder.get("trashed")
                or folder.get("name", "").casefold() != subject):
            raise ReviewRequired("ID materia non è una cartella accessibile")
        lessons[subject] = _one_folder(drive.list(fid), "lezioni")
    if len(set(lessons.values())) != len(SUBJECTS):
        raise ReviewRequired("Cartelle lezioni duplicate")
    return lessons


def timestamp_seconds(value):
    parts = [int(p) for p in value.split(":")]
    if len(parts) == 2:
        m, s = parts
        if s > 59:
            raise ValueError("Timestamp non valido")
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        if m > 59 or s > 59:
            raise ValueError("Timestamp non valido")
        return h * 3600 + m * 60 + s
    raise ValueError("Timestamp non valido")


def transcript_lines(raw):
    try:
        text = raw.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise ReviewRequired("Trascrizione non UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if not lines or not text.strip():
        raise ReviewRequired("Trascrizione vuota")
    times = []
    last_start = -1
    for line in lines:
        content = line.rstrip("\r\n")
        match = LINE_RE.fullmatch(content)
        if not match or not match.group(3).strip():
            raise ReviewRequired("Riga trascrizione senza timestamp o testo")
        try:
            start, end = timestamp_seconds(match.group(1)), timestamp_seconds(match.group(2))
        except ValueError as exc:
            raise ReviewRequired("Timestamp non valido") from exc
        if start < last_start or end < start:
            raise ReviewRequired("Timestamp non ordinati")
        times.append((start, end))
        last_start = start
    return lines, times


def validate_plan(data, lines, times=None, schedule=None, start=None):
    """Validazione generica del piano (non usata nel routing manuale)."""
    if not isinstance(data, list) or not data:
        raise ReviewRequired("Indice vuoto o non valido")
    expected = 0
    out = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"start_line", "end_line", "materia", "argomento"}:
            raise ReviewRequired("Elemento indice non valido")
        first, last = item["start_line"], item["end_line"]
        if type(first) is not int or type(last) is not int or first != expected or last < first or last >= len(lines):
            raise ReviewRequired("Intervalli non validi, incompleti o sovrapposti")
        subject, topic = item["materia"], item["argomento"]
        if not isinstance(subject, str) or subject not in SUBJECTS:
            raise ReviewRequired("Materia incerta o non ammessa")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 120:
            raise ReviewRequired("Argomento incerto")
        out.append({"start_line": first, "end_line": last, "materia": subject, "argomento": topic})
        expected = last + 1
    if expected != len(lines):
        raise ReviewRequired("Indice non copre tutta la trascrizione")
    return out


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def encode_plan(plan):
    raw = canonical(plan)
    encoded = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    chunks = [encoded[i:i + PLAN_CHUNK_SIZE] for i in range(0, len(encoded), PLAN_CHUNK_SIZE)]
    if len(chunks) > PLAN_CHUNKS:
        raise ReviewRequired("Piano troppo grande per le proprietà Drive")
    result = {"orpheus_plan_count": str(len(chunks)), "orpheus_plan_hash": digest(raw)}
    result.update({f"orpheus_plan_{i:02d}": chunk for i, chunk in enumerate(chunks)})
    return result


def decode_plan(p):
    if not p.get("orpheus_plan_count"):
        if any(k.startswith("orpheus_plan_") for k in p):
            raise ReviewRequired("Piano parziale")
        return None
    try:
        count = int(p["orpheus_plan_count"])
        if not 1 <= count <= PLAN_CHUNKS:
            raise ValueError()
        data = "".join(p[f"orpheus_plan_{i:02d}"] for i in range(count))
        raw = zlib.decompress(base64.b64decode(data, validate=True))
        if digest(raw) != p["orpheus_plan_hash"]:
            raise ValueError()
        plan = json.loads(raw.decode("ascii"))
    except (ValueError, KeyError, TypeError, zlib.error, UnicodeError) as exc:
        raise ReviewRequired("Piano persistito non valido") from exc
    if not isinstance(plan, dict):
        raise ReviewRequired("Piano persistito non valido")
    return plan


def ask_gemini(client, model, lines):
    prompt = ("Classifica le righe della trascrizione Whisper, senza trascrivere, riassumere o riscrivere il testo. "
              "Rispondi solo con un array JSON di oggetti con esattamente start_line, end_line, materia, argomento. "
              "Gli indici interi sono 0-based, estremi inclusivi; copri ogni riga una sola volta senza sovrapposizioni. "
              "Materie ammesse: biologia, chimica, fisica, inglese, letteratura, purgatorio. "
              "Non inventare: se incerto usa materia null e argomento null. Italiano generico non indica letteratura o purgatorio. "
              "Italiano generico non indica letteratura o purgatorio.\n" +
              json.dumps({"lines": [{"index": i, "text": line} for i, line in enumerate(lines)]}, ensure_ascii=False))
    response = client.models.generate_content(model=model, contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.1, "max_output_tokens": 8192})
    candidates = getattr(response, "candidates", None)
    if not candidates or "STOP" not in str(getattr(candidates[0], "finish_reason", "")).upper():
        raise ValueError("Risposta Gemini incompleta")
    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Risposta Gemini vuota")
    return json.loads(text)


def kernel_state(kaggle, slug):
    try:
        state = kaggle.kernels_status(slug)
        value = str(getattr(state, "status", state)).strip().lower()
    except Exception:
        return "unknown"
    # Escludere sottostringhe ambigue e risposte non riconosciute, inclusi 404.
    word = value.rsplit(".", 1)[-1].strip()
    if word in {"running", "queued", "pending"}:
        return "running"
    if word in {"error", "failed", "cancelled", "canceled"}:
        return "failed"
    if word in {"complete", "completed"}:
        return "complete"
    return "unknown"


def int_prop(p, key, default=0):
    try:
        value = int(p.get(key, str(default)))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


class Pipeline:
    def __init__(self, cfg, services, now=time.time):
        self.cfg, self.services, self.now = cfg, services, now
        self.drive = services.drive()
        self.errors = 0

    def _mark_review(self, file_id):
        self.drive.set_props(file_id, {"orpheus_status": "review"})
        LOG.warning("Elemento richiede revisione manuale; nessuna eliminazione o completamento.")

    def _find_raw(self, source_id):
        candidates = []
        for parent in (self.cfg.transcripts, self.cfg.processed):
            candidates.extend(f for f in self.drive.list(parent) if props(f).get("orpheus_source_id") == source_id)
        if len(candidates) > 1:
            raise ReviewRequired("Più trascrizioni per lo stesso ID")
        return candidates[0] if candidates else None

    def _check_raw(self, audio, raw):
        p = props(raw)
        original = props(audio).get("orpheus_original_name", audio.get("name"))
        if p.get("orpheus_kind") != "raw_transcript" or p.get("orpheus_source_id") != audio["id"] or p.get("orpheus_original_name") != original:
            raise ReviewRequired("Metadati di provenienza non validi")
        if raw.get("mimeType") == FOLDER_MIME or not raw.get("name", "").lower().endswith(".txt"):
            raise ReviewRequired("Trascrizione di tipo non valido")
        if audio.get("name") != original:
            raise ReviewRequired("Nome audio modificato dopo l'avvio")

    def _plan(self, audio, raw, data, lines, subject_map, start):
        p = props(self.drive.get(raw["id"]))
        plan = decode_plan(p)
        subject = subject_from_name(audio["name"])
        context = {"source_id": audio["id"], "raw_id": raw["id"], "raw_sha256": digest(data),
                   "start": start.isoformat(), "subject": subject, "targets": subject_map}
        if plan is None:
            # Un audio per lezione: materia dal suffisso filename, nessun classificatore.
            segments = [{"start_line": 0, "end_line": len(lines) - 1,
                         "materia": subject, "argomento": subject}]
            plan = {**context, "segments": segments}
            encoded = encode_plan(plan)
            self.drive.set_props(raw["id"], encoded)
            persisted = decode_plan(props(self.drive.get(raw["id"])))
            if persisted != plan:
                raise ReviewRequired("Piano non persistito correttamente")
        else:
            if any(plan.get(k) != v for k, v in context.items()):
                raise ReviewRequired("Trascrizione, orario o destinazioni modificate")
            if plan.get("segments") != [{"start_line": 0, "end_line": len(lines) - 1,
                                          "materia": subject, "argomento": subject}]:
                raise ReviewRequired("Piano deterministico non coerente")
        return plan, digest(canonical(plan))

    def _inventory(self, subject_map, source_id):
        inventory = {}
        for subject, parent in subject_map.items():
            files = self.drive.list(parent)
            inventory[subject] = files
            for item in files:
                p = props(item)
                if p.get("orpheus_source_id") == source_id and p.get("orpheus_kind") != "lesson_folder":
                    raise ReviewRequired("Metadati cartella lezione incoerenti")
        return inventory

    def _route(self, source_id, raw, plan, plan_hash, lines, subject_map):
        inventory = self._inventory(subject_map, source_id)
        expected_keys = {f"{s['start_line']}:{s['end_line']}": s["materia"] for s in plan["segments"]}
        for subject, files in inventory.items():
            for f in files:
                p = props(f)
                if p.get("orpheus_source_id") == source_id:
                    if p.get("orpheus_segment_key") not in expected_keys or expected_keys[p["orpheus_segment_key"]] != subject:
                        raise ReviewRequired("Cartella lezione inattesa per questa sorgente")
        for segment in plan["segments"]:
            subject = segment["materia"]
            key = f"{segment['start_line']}:{segment['end_line']}"
            found = [f for f in inventory[subject] if props(f).get("orpheus_source_id") == source_id and props(f).get("orpheus_segment_key") == key]
            if len(found) > 1:
                raise ReviewRequired("Cartelle duplicate")
            ident = {"orpheus_source_id": source_id, "orpheus_segment_key": key,
                     "orpheus_plan_hash": plan_hash, "orpheus_kind": "lesson_folder"}
            suffix = f"_{slugify(segment['argomento'])}_{plan['start'][:10]}"
            if found:
                folder = found[0]
                if folder.get("mimeType") != FOLDER_MIME or any(props(folder).get(k) != v for k, v in ident.items()):
                    raise ReviewRequired("Cartella lezione non corrispondente")
                match = re.fullmatch(r"(\d+)" + re.escape(suffix), folder.get("name", ""))
                if not match or int(match.group(1)) < 1:
                    raise ReviewRequired("Nome cartella lezione incoerente")
                number = int(match.group(1))
            else:
                numbers = [int(m.group(1)) for f in inventory[subject] if f.get("mimeType") == FOLDER_MIME
                           and (m := re.match(r"^(\d+)_", f.get("name", "")))]
                number = max(numbers, default=0) + 1
                folder = self.drive.create_folder(subject_map[subject], f"{number}{suffix}", ident)
                inventory[subject].append(folder)
            file_ident = {**ident, "orpheus_kind": "lesson_txt"}
            expected_name = f"lezione{number}.txt"
            content = "".join(lines[segment["start_line"]:segment["end_line"] + 1]).encode("utf-8")
            children = self.drive.list(folder["id"])
            matching = [f for f in children if props(f).get("orpheus_source_id") == source_id and props(f).get("orpheus_segment_key") == key]
            if len(matching) > 1:
                raise ReviewRequired("File lezione duplicati")
            if matching:
                f = matching[0]
                if f.get("name") != expected_name or any(props(f).get(k) != v for k, v in file_ident.items()):
                    raise ReviewRequired("File lezione non corrispondente")
            else:
                if any(f.get("name") == expected_name for f in children):
                    raise ReviewRequired("Nome file già occupato")
                f = self.drive.create_text(folder["id"], expected_name, content, file_ident)
            # Controlla parent, proprietà, nome e contenuto riletti, non solo l'esito upload.
            checked = self.drive.get(f["id"])
            if (checked.get("parents") != [folder["id"]] or checked.get("name") != expected_name
                or checked.get("mimeType") != "text/plain" or checked.get("trashed")
                or any(props(checked).get(k) != v for k, v in file_ident.items())
                or self.drive.read(f["id"]) != content):
                raise ReviewRequired("Upload non verificato")
        # Rileva duplicati apparsi prima della verifica finale, senza eliminarli.
        final = self._inventory(subject_map, source_id)
        for key, subject in expected_keys.items():
            matching = [f for f in final[subject] if props(f).get("orpheus_source_id") == source_id and props(f).get("orpheus_segment_key") == key]
            if len(matching) != 1:
                raise ReviewRequired("Verifica finale cartelle fallita")
            folder = self.drive.get(matching[0]["id"])
            if folder.get("parents") != [subject_map[subject]]:
                raise ReviewRequired("Parent lezione non verificato")
            children = [f for f in self.drive.list(folder["id"]) if props(f).get("orpheus_source_id") == source_id and props(f).get("orpheus_segment_key") == key]
            if len(children) != 1:
                raise ReviewRequired("Verifica finale file fallita")

    def process_transcript(self, audio, raw, subject_map):
        self._check_raw(audio, raw)
        if props(audio).get("orpheus_status") == "review" or props(raw).get("orpheus_status") == "review":
            raise WaitRequired("Revisione manuale richiesta")
        start = recording_start(audio["name"])
        data = self.drive.read(raw["id"])
        lines, times = transcript_lines(data)
        plan, plan_hash = self._plan(audio, raw, data, lines, subject_map, start)
        self._route(audio["id"], raw, plan, plan_hash, lines, subject_map)
        self.drive.set_props(raw["id"], {"orpheus_route_done": "1"})
        if props(self.drive.get(raw["id"])).get("orpheus_route_done") != "1":
            raise ReviewRequired("Stato routing non verificato")
        self.drive.move(raw["id"], self.cfg.processed)
        marker = {"orpheus_kind": "completed_audio", "orpheus_source_id": audio["id"],
                  "orpheus_routing_complete": "true"}
        self.drive.set_props(audio["id"], marker)
        if any(props(self.drive.get(audio["id"])).get(k) != v for k, v in marker.items()):
            raise ReviewRequired("Stato audio non verificato")
        self.drive.move(audio["id"], self.cfg.completed)
        LOG.info("Trascrizione verificata e audio completato.")

    def _launch(self, audio, attempt):
        updates = {"orpheus_attempts": str(attempt), "orpheus_started_at": str(int(self.now())),
                   "orpheus_launch_state": "intent", "orpheus_seen_running": "0",
                   "orpheus_original_name": props(audio).get("orpheus_original_name", audio["name"])}
        self.drive.set_props(audio["id"], updates)
        if self.drive.get(audio["id"]).get("parents") != [self.cfg.processing]:
            self.drive.move(audio["id"], self.cfg.processing)
        try:
            self.services.kaggle().kernels_push(self.cfg.kernel_dir)
        except Exception:
            # Timeout/errore può seguire una push riuscita: non ripetere né rollback.
            self.drive.set_props(audio["id"], {"orpheus_launch_state": "uncertain"})
            raise ReviewRequired("Esito push Kaggle incerto")
        self.drive.set_props(audio["id"], {"orpheus_launch_state": "launched"})
        LOG.info("Kernel avviato per un solo audio.")

    def _retry_or_wait(self, audio):
        p = props(self.drive.get(audio["id"]))
        if p.get("orpheus_status") == "review":
            return
        state = kernel_state(self.services.kaggle(), self.cfg.kernel_slug)
        if state == "unknown":
            LOG.warning("Stato Kaggle sconosciuto: nessuna nuova push.")
            return
        if state == "running":
            if p.get("orpheus_launch_state") == "launched":
                self.drive.set_props(audio["id"], {"orpheus_seen_running": "1"})
            return
        if p.get("orpheus_launch_state") != "launched" or p.get("orpheus_seen_running") != "1":
            # Uno stato terminale vecchio non dimostra che l'ultima push sia terminata.
            return
        attempts, started = int_prop(p, "orpheus_attempts"), int_prop(p, "orpheus_started_at")
        if not attempts or not started:
            raise ReviewRequired("Stato avvio incompleto")
        backoff = self.cfg.retry_after * (2 ** min(attempts - 1, 6))
        if int(self.now()) - started < backoff:
            return
        if attempts >= self.cfg.max_attempts:
            raise ReviewRequired("Tentativi Kaggle esauriti")
        if not self.cfg.worker_verified:
            return
        self._launch(audio, attempts + 1)

    def cycle(self):
        cfg = self.cfg
        processing = self.drive.list(cfg.processing)
        # I raw sono associati solo tramite ID, anche se vi sono nomi uguali.
        # Un audio già trascritto può essere stato rimesso in incoming manualmente:
        # recuperarlo evita di reinviare la stessa sorgente al worker.
        incoming = self.drive.list(cfg.incoming)
        for audio in [f for f in processing + incoming if is_audio(f)]:
            try:
                raw = self._find_raw(audio["id"])
                if raw is not None:
                    self._check_raw(audio, raw)
                    subject_from_name(audio["name"])
                    self.process_transcript(audio, raw, build_subject_map(self.drive, cfg))
            except WaitRequired:
                LOG.info("Elemento in attesa di prerequisiti o backoff.")
            except ReviewRequired:
                self._mark_review(audio["id"])
            except Exception as exc:
                self.errors += 1
                LOG.error("Elaborazione non completata (%s); dati conservati.", type(exc).__name__)
        # Rileggi: process_transcript può avere completato un audio.
        processing = self.drive.list(cfg.processing)
        if processing:
            # Il worker legge TUTTI i file in processing: non lanciare con file extra.
            if len(processing) != 1 or not is_audio(processing[0]):
                LOG.warning("Processing ambiguo: nessuna push Kaggle.")
                return self.errors
            audio = processing[0]
            try:
                if self._find_raw(audio["id"]) is None:
                    self._retry_or_wait(audio)
            except ReviewRequired:
                self._mark_review(audio["id"])
            except Exception as exc:
                self.errors += 1
                LOG.error("Recupero non completato (%s).", type(exc).__name__)
            return self.errors
        if not cfg.worker_verified:
            LOG.warning("Worker non attestato: nessuna push Kaggle.")
            return self.errors
        try:
            state = kernel_state(self.services.kaggle(), cfg.kernel_slug)
            if state not in {"complete", "failed"}:
                LOG.info("Kaggle attivo o stato ignoto: nessuna push.")
                return self.errors
            for audio in self.drive.list(cfg.incoming):
                if not is_audio(audio) or props(audio).get("orpheus_status") == "review":
                    continue
                try:
                    subject_from_name(audio["name"])
                    if self._find_raw(audio["id"]) is not None:
                        continue
                    if int_prop(props(audio), "orpheus_attempts") >= cfg.max_attempts:
                        raise ReviewRequired("Tentativi esauriti")
                    self._launch(audio, int_prop(props(audio), "orpheus_attempts") + 1)
                except WaitRequired:
                    return self.errors
                except ReviewRequired:
                    self._mark_review(audio["id"])
                break
        except Exception as exc:
            self.errors += 1
            LOG.error("Avvio non completato (%s); dati conservati.", type(exc).__name__)
        return self.errors


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.from_env()
        return 1 if Pipeline(cfg, Services(cfg)).cycle() else 0
    except Exception as exc:
        # Mai esporre il messaggio di errori SDK: può includere testo o credenziali.
        LOG.error("Ciclo non completato (%s).", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
