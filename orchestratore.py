#!/usr/bin/env python3
"""
Orchestratore pipeline lezioni: Drive -> Kaggle (Whisper) -> Gemini -> Drive.
Esegue UN SOLO ciclo (Fase 0 recupero, Fase 1 audio->Kaggle, Fase 2 testo->Gemini->routing)
e termina. Pensato per GitHub Actions con cron. Vedi SPECIFICA.md.
"""
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

from google import genai
from google.genai import types as genai_types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from kaggle.api.kaggle_api_extended import KaggleApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orchestratore")

# ==========================================
# 1. CONFIGURAZIONE
# ==========================================
FOLDER_AUDIO_INCOMING = os.environ["FOLDER_AUDIO_INCOMING"]
FOLDER_PROCESSING_KAGGLE = os.environ["FOLDER_PROCESSING_KAGGLE"]
FOLDER_TRASCRIZIONI = os.environ["FOLDER_TRASCRIZIONI"]
FOLDER_TRASCRIZIONI_ELABORATE = os.environ["FOLDER_TRASCRIZIONI_ELABORATE"]
FOLDER_AUDIO_COMPLETATI = os.environ["FOLDER_AUDIO_COMPLETATI"]
FOLDER_ERRORI = os.environ["FOLDER_ERRORI"]
FOLDER_RADICE_MATERIE = os.environ["FOLDER_RADICE_MATERIE"]

KAGGLE_KERNEL_SLUG = os.environ["KAGGLE_KERNEL_SLUG"]
KAGGLE_KERNEL_DIR = os.environ.get("KAGGLE_KERNEL_DIR", "kaggle_kernel")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
MATERIE = ["biologia", "chimica", "fisica", "italiano", "inglese", "arte"]
# Opzionale: mappa statica {"biologia": "ID", ...}. Se assente viene costruita scansionando la radice.
MATERIE_FOLDER_IDS_ENV = os.environ.get("MATERIE_FOLDER_IDS")

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
# Dopo quanti secondi un audio in _processing_kaggle, con kernel non attivo e senza .txt, viene rimesso in coda
RETRY_AFTER_SEC = int(os.environ.get("RETRY_AFTER_SEC", str(30 * 60)))

FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_KW = {"supportsAllDrives": True}
LIST_KW = {"supportsAllDrives": True, "includeItemsFromAllDrives": True, "corpora": "allDrives"}

errors_count = 0  # errori complessivi del giro -> exit code 1


def init_services():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    kaggle = KaggleApi()
    kaggle.authenticate()
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return drive, kaggle, gemini_client


drive_service, api_kaggle, client = init_services()


# ==========================================
# 2. FUNZIONI DRIVE
# ==========================================
def esc(s):
    """Escape per le query Drive."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def list_files(folder_id, only_folders=False, only_files=False):
    """Elenca (con paginazione) i figli non cestinati di una cartella."""
    q = f"'{folder_id}' in parents and trashed=false"
    if only_folders:
        q += f" and mimeType='{FOLDER_MIME}'"
    if only_files:
        q += f" and mimeType!='{FOLDER_MIME}'"
    out, token = [], None
    while True:
        res = drive_service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, mimeType, createdTime, appProperties)",
            pageSize=200,
            pageToken=token,
            **LIST_KW,
        ).execute(num_retries=4)
        out.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            return out


def move_file(file_id, new_parent_id, app_props=None):
    """Sposta un file (e opzionalmente aggiorna appProperties) in UNA sola chiamata."""
    f = drive_service.files().get(fileId=file_id, fields="parents", **DRIVE_KW).execute(num_retries=4)
    prev = ",".join(f.get("parents", []))
    body = {"appProperties": app_props} if app_props else None
    drive_service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=prev,
        body=body,
        fields="id",
        **DRIVE_KW,
    ).execute(num_retries=4)


def set_props(file_id, props):
    drive_service.files().update(
        fileId=file_id, body={"appProperties": props}, fields="id", **DRIVE_KW
    ).execute(num_retries=4)


def download_text(file_id):
    req = drive_service.files().get_media(fileId=file_id, **DRIVE_KW)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk(num_retries=4)
    return buf.getvalue().decode("utf-8-sig", errors="replace")


def create_folder(name, parent_id):
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    return drive_service.files().create(body=meta, fields="id", **DRIVE_KW).execute(num_retries=4)["id"]


def upload_text_file(name, content, parent_id):
    meta = {"name": name, "parents": [parent_id]}
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/plain")
    return drive_service.files().create(
        body=meta, media_body=media, fields="id", **DRIVE_KW
    ).execute(num_retries=4)["id"]


def trash_file(file_id):
    """Cestina (recuperabile) invece di eliminare definitivamente."""
    drive_service.files().update(
        fileId=file_id, body={"trashed": True}, fields="id", **DRIVE_KW
    ).execute(num_retries=4)


# ==========================================
# 3. FUNZIONI DI SUPPORTO
# ==========================================
def stem(name):
    return os.path.splitext(name)[0]


def slugify(text, max_len=40):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9àèéìòù]+", "_", text).strip("_")
    return (text or "senza_titolo")[:max_len].strip("_")


def extract_date(name, created_time):
    """Data della lezione: dal nome file (YYYY-MM-DD), altrimenti createdTime del file."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    if m:
        return m.group(1)
    return (created_time or datetime.now(timezone.utc).isoformat())[:10]


def int_prop(props, key, default=0):
    try:
        return int((props or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def kernel_state():
    """Ritorna 'running', 'failed', 'complete' o 'unknown' per il kernel Kaggle."""
    try:
        st = api_kaggle.kernels_status(KAGGLE_KERNEL_SLUG)
        s = str(getattr(st, "status", st)).lower()
    except Exception as e:
        # Se il kernel non esiste (404) o c'è un errore di rete, consideralo fallito per far scattare il retry
        if "404" in str(e) or "not found" in str(e).lower():
            return "failed"
        log.warning("Impossibile leggere lo stato del kernel Kaggle: %s", e)
        return "unknown"
    
    # QUESTA PARTE VA MANTENUTA ASSOLUTAMENTE
    if "running" in s or "queued" in s:
        return "running"
    if "error" in s or "cancel" in s:
        return "failed"
    if "complete" in s:
        return "complete"
    return "unknown"


def load_orario():
    path = os.environ.get("ORARIO_PATH", "orario.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    log.warning("File orario '%s' non trovato: Gemini lavorerà senza orario.", path)
    return "(orario non fornito)"


def build_subject_map():
    """Mappa materia -> ID cartella. Preferisce la mappa statica da env; altrimenti scansiona la radice."""
    if MATERIE_FOLDER_IDS_ENV:
        mapping = json.loads(MATERIE_FOLDER_IDS_ENV)
    else:
        mapping, found, stack = {}, {}, [FOLDER_RADICE_MATERIE]
        while stack:
            parent = stack.pop()
            for f in list_files(parent, only_folders=True):
                key = f["name"].lower()
                if key in MATERIE:
                    found.setdefault(key, []).append(f["id"])
                stack.append(f["id"])
        for k, ids in found.items():
            if len(ids) > 1:
                raise RuntimeError(f"Cartella materia '{k}' ambigua ({len(ids)} corrispondenze): usa MATERIE_FOLDER_IDS")
            mapping[k] = ids[0]
    missing = [m for m in MATERIE if m not in mapping]
    if missing:
        raise RuntimeError(f"Cartelle materia mancanti: {missing}")
    return mapping


# ==========================================
# 4. FASE 0 - RECUPERO FILE BLOCCATI
# ==========================================
def phase0_recover():
    log.info("== Fase 0: recupero ==")
    processing = list_files(FOLDER_PROCESSING_KAGGLE, only_files=True)
    if not processing:
        return
    done_stems = {stem(f["name"]) for f in list_files(FOLDER_TRASCRIZIONI, only_files=True)}
    done_stems |= {stem(f["name"]) for f in list_files(FOLDER_TRASCRIZIONI_ELABORATE, only_files=True)}
    state = kernel_state()
    now = int(time.time())

    for f in processing:
        try:
            # 1) La trascrizione esiste: audio completato
            if stem(f["name"]) in done_stems:
                move_file(f["id"], FOLDER_AUDIO_COMPLETATI)
                log.info("Audio completato: %s", f["name"])
                continue
            # 2) Kernel ancora attivo: aspetta
            if state == "running":
                continue
            # 3) Kernel non attivo e nessun .txt: riprova o scarta dopo il periodo di grazia
            props = f.get("appProperties") or {}
            started = int_prop(props, "started_at", 0)
            attempts = int_prop(props, "attempts", 1)
            if state != "failed" and now - started < RETRY_AFTER_SEC:
                continue
            if attempts >= MAX_ATTEMPTS:
                move_file(f["id"], FOLDER_ERRORI)
                log.error("Audio %s scartato dopo %d tentativi -> _errori", f["name"], attempts)
            else:
                move_file(f["id"], FOLDER_AUDIO_INCOMING, app_props={**props, "attempts": str(attempts)})
                log.warning("Audio %s rimesso in coda (tentativo %d/%d)", f["name"], attempts, MAX_ATTEMPTS)
        except Exception as e:
            fail(f"Fase 0 su {f['name']}: {e}")


# ==========================================
# 5. FASE 1 - AUDIO -> KAGGLE
# ==========================================
def phase1_audio_to_kaggle():
    log.info("== Fase 1: audio -> Kaggle ==")
    audio_files = list_files(FOLDER_AUDIO_INCOMING, only_files=True)
    if not audio_files:
        return
    if kernel_state() == "running":
        log.info("Kernel Kaggle già in esecuzione: i nuovi audio restano in coda.")
        return

    # Prima sposta in _processing_kaggle (il notebook legge da lì), poi lancia Kaggle UNA volta.
    moved = []  # (id, attempts_precedenti, props_originali)
    for f in audio_files:
        try:
            props = f.get("appProperties") or {}
            prev_attempts = int_prop(props, "attempts", 0)
            new_props = {**props, "started_at": str(int(time.time())), "attempts": str(prev_attempts + 1)}
            move_file(f["id"], FOLDER_PROCESSING_KAGGLE, app_props=new_props)
            moved.append((f["id"], f["name"], props))
        except Exception as e:
            fail(f"Fase 1 spostamento {f['name']}: {e}")

    if not moved:
        return
    try:
        api_kaggle.kernels_push(KAGGLE_KERNEL_DIR)
        log.info("Kaggle avviato per %d file.", len(moved))
    except Exception as e:
        fail(f"Avvio Kaggle fallito: {e}. Rollback di {len(moved)} file.")
        for fid, name, props in moved:  # rollback: riporta in coda con il contatore originale
            try:
                move_file(fid, FOLDER_AUDIO_INCOMING, app_props=props or {"attempts": "0"})
            except Exception as e2:
                fail(f"Rollback {name}: {e2}")


# ==========================================
# 6. FASE 2 - TESTO -> GEMINI -> ROUTING
# ==========================================
def ask_gemini(content, orario):
    prompt = (
        "Sei un assistente scolastico. Ti fornisco la trascrizione di una giornata di lezioni.\n"
        "Orario scolastico (per capire le materie):\n" + orario + "\n\n"
        "Trascrizione con timestamp:\n" + content + "\n\n"
        "Compito:\n"
        "1. Dividi la trascrizione per materia in base ai timestamp e all'orario.\n"
        "2. IGNORA le materie non presenti in questo elenco: " + ", ".join(MATERIE) + ".\n"
        "3. Per ogni materia identifica l'argomento principale (max 5 parole).\n"
        "4. Restituisci SOLO un array JSON di oggetti con le chiavi "
        '"materia", "argomento", "testo" (il testo completo della parte di lezione, senza riassumere).'
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=65536,
            temperature=0.1,
        )
    )
    if not resp.candidates:
        raise RuntimeError("Gemini non ha restituito candidati (risposta bloccata?)")
    if "MAX_TOKENS" in str(resp.candidates[0].finish_reason).upper():
        raise RuntimeError("Output di Gemini troncato (MAX_TOKENS)")
    if not resp.text:
        raise RuntimeError("Risposta di Gemini vuota")
    data = json.loads(resp.text)
    if not isinstance(data, list):
        raise ValueError("La risposta di Gemini non è un array JSON")
    valid = []
    for item in data:
        if not isinstance(item, dict) or not all(k in item for k in ("materia", "argomento", "testo")):
            raise ValueError(f"Elemento JSON non valido: {str(item)[:100]}")
        materia = str(item["materia"]).strip().lower()
        if materia not in MATERIE:
            log.info("Materia ignorata: %s", materia)
            continue
        if not str(item["testo"]).strip():
            continue
        valid.append({"materia": materia, "argomento": str(item["argomento"]), "testo": str(item["testo"])})
    return valid


def save_lesson(materia_id, argomento, data_lezione, testo):
    """Crea N_argomento_data/lezioneN.txt. Idempotente: se esiste già una cartella uguale, salta."""
    slug = slugify(argomento)
    existing = list_files(materia_id, only_folders=True)
    suffix = f"_{slug}_{data_lezione}"
    if any(re.fullmatch(r"\d+" + re.escape(suffix), f["name"]) for f in existing):
        log.info("Già presente, salto: %s", suffix)
        return
    numbers = [int(m.group(1)) for f in existing if (m := re.match(r"^(\d+)_", f["name"]))]
    n = (max(numbers) + 1) if numbers else 1
    folder_id = create_folder(f"{n}{suffix}", materia_id)
    upload_text_file(f"lezione{n}.txt", testo, folder_id)
    log.info("Salvato %d%s", n, suffix)


def phase2_transcripts(subject_map, orario):
    log.info("== Fase 2: trascrizioni -> Gemini ==")
    for txt in list_files(FOLDER_TRASCRIZIONI, only_files=True):
        if not txt["name"].lower().endswith(".txt"):
            continue
        log.info("Trascrizione: %s", txt["name"])
        props = txt.get("appProperties") or {}
        attempts = int_prop(props, "attempts", 0) + 1
        try:
            content = download_text(txt["id"])
            lezioni = ask_gemini(content, orario)
            data_lezione = extract_date(txt["name"], txt.get("createdTime"))
            for l in lezioni:
                save_lesson(subject_map[l["materia"]], l["argomento"], data_lezione, l["testo"])
            # Tutto salvato: archivia (non eliminare)
            move_file(txt["id"], FOLDER_TRASCRIZIONI_ELABORATE)
            log.info("Trascrizione %s elaborata e archiviata (%d lezioni).", txt["name"], len(lezioni))
        except Exception as e:
            fail(f"Fase 2 su {txt['name']} (tentativo {attempts}/{MAX_ATTEMPTS}): {e}")
            try:
                if attempts >= MAX_ATTEMPTS:
                    move_file(txt["id"], FOLDER_ERRORI, app_props={**props, "attempts": str(attempts)})
                else:
                    set_props(txt["id"], {**props, "attempts": str(attempts)})
            except Exception as e2:
                fail(f"Aggiornamento stato errore {txt['name']}: {e2}")


# ==========================================
# 7. MAIN
# ==========================================
def fail(msg):
    global errors_count
    errors_count += 1
    log.error(msg)


def main():
    log.info("--- Avvio orchestratore ---")
    subject_map = build_subject_map()
    orario = load_orario()
    for step in (phase0_recover, phase1_audio_to_kaggle, lambda: phase2_transcripts(subject_map, orario)):
        try:
            step()
        except Exception as e:  # una fase che crolla non blocca le altre
            fail(f"Errore non gestito in {getattr(step, '__name__', 'fase')}: {e}")
    log.info("--- Fine orchestratore (errori: %d) ---", errors_count)
    sys.exit(1 if errors_count else 0)


if __name__ == "__main__":
    main()
