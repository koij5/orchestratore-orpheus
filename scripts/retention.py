#!/usr/bin/env python3
"""Retention Drive esplicita, solo metadati; nessun delete permanente.

Il contratto dei marker e le limitazioni sono in retention_README.md. Le
funzioni accettano un service Drive v3 iniettato, così i test restano offline.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, TextIO

SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER = "application/vnd.google-apps.folder"
FIELDS = "id,mimeType,parents,trashed,createdTime,version,appProperties,capabilities(canTrash)"
LIST_FIELDS = "nextPageToken,incompleteSearch,files(" + FIELDS + ")"
ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
SEGMENT_RE = re.compile(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
ETAG_RE = re.compile(r'"[\x21\x23-\x7e]*"\Z')


class SafetyError(Exception):
    """Errore controllato: non stampare mai stringhe d'eccezione da Drive."""


def valid_id(value: Any) -> bool:
    # Un ID esplicito, non l'alias speciale My Drive "root".
    return isinstance(value, str) and len(value) <= 256 and value != "root" and bool(ID_RE.fullmatch(value))


def valid_version(item: dict) -> bool:
    v = item.get("version")
    return isinstance(v, str) and bool(re.fullmatch(r"[1-9][0-9]*", v))


def valid_props(item: dict) -> dict | None:
    props = item.get("appProperties")
    if not isinstance(props, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in props.items()):
        return None
    # Qualunque stato non espressamente riconosciuto è ambiguo.
    if props.get("orpheus_status") not in (None, "complete", "approved"):
        return None
    return props


def valid_segment(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    match = SEGMENT_RE.fullmatch(value)
    return bool(match and int(match.group(1)) <= int(match.group(2)))


def identity(props: dict | None, kind: str) -> tuple[str, str, str] | None:
    if not props or props.get("orpheus_kind") != kind:
        return None
    source, segment, plan = (props.get(k) for k in
                             ("orpheus_source_id", "orpheus_segment_key", "orpheus_plan_hash"))
    if not valid_id(source) or not valid_segment(segment) or not isinstance(plan, str) or not HASH_RE.fullmatch(plan):
        return None
    return source, segment, plan


def base_file(item: Any, parent: str) -> bool:
    return (isinstance(item, dict) and valid_id(item.get("id"))
            and item.get("parents") == [parent] and item.get("trashed") is False
            and valid_version(item))


def can_trash(item: dict) -> bool:
    caps = item.get("capabilities")
    return isinstance(caps, dict) and caps.get("canTrash") is True


def valid_root(item: Any, expected: str) -> bool:
    return (isinstance(item, dict) and item.get("id") == expected
            and item.get("mimeType") == FOLDER and item.get("trashed") is False
            and valid_version(item))


def valid_lesson_folder(item: Any, root: str) -> bool:
    return (base_file(item, root) and item.get("mimeType") == FOLDER
            and identity(valid_props(item), "lesson_folder") is not None)


def parse_created(value: Any) -> datetime | None:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def valid_lesson_text(item: Any, folder: dict, now: datetime) -> bool:
    if not base_file(item, folder["id"]) or item.get("mimeType") != "text/plain" or not can_trash(item):
        return False
    file_identity = identity(valid_props(item), "lesson_txt")
    folder_identity = identity(valid_props(folder), "lesson_folder")
    created = parse_created(item.get("createdTime"))
    return (file_identity is not None and file_identity == folder_identity and created is not None
            and created <= now - timedelta(days=30))


def valid_completed_audio(item: Any, parent: str) -> bool:
    if not base_file(item, parent) or not can_trash(item):
        return False
    mime = item.get("mimeType")
    props = valid_props(item)
    return (isinstance(mime, str) and mime.startswith("audio/") and len(mime) > len("audio/")
            and props is not None
            and props.get("orpheus_kind") == "completed_audio"
            and props.get("orpheus_source_id") == item["id"]
            and props.get("orpheus_routing_complete") == "true"
            and props.get("orpheus_review_status") == "approved"
            and props.get("orpheus_results_verified") == "true")


def get_item(service: Any, file_id: str) -> tuple[dict, str | None]:
    request = service.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True)
    headers: list[Any] = []
    request.add_response_callback(headers.append)
    item = request.execute(num_retries=4)
    if not isinstance(item, dict) or item.get("id") != file_id or not headers:
        raise SafetyError("Lettura metadati non verificabile")
    response = headers[-1]
    etag = response.get("etag") or response.get("ETag")
    return item, etag if isinstance(etag, str) else None


def list_children(service: Any, parent: str) -> list[dict]:
    if not valid_id(parent):
        raise SafetyError("ID cartella non valido")
    # valid_id esclude apici, slash e ogni operatore della query.
    query = f"'{parent}' in parents and trashed = false"
    token, seen_tokens, seen_ids, items = None, set(), set(), []
    while True:
        response = service.files().list(q=query, fields=LIST_FIELDS, pageSize=200,
            pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
            corpora="allDrives").execute(num_retries=4)
        if not isinstance(response, dict) or response.get("incompleteSearch", False) is not False:
            raise SafetyError("Ricerca Drive incompleta")
        page = response.get("files")
        if not isinstance(page, list):
            raise SafetyError("Elenco Drive non verificabile")
        for item in page:
            if not isinstance(item, dict) or not valid_id(item.get("id")) or item["id"] in seen_ids:
                raise SafetyError("Elenco Drive incoerente o duplicato")
            seen_ids.add(item["id"])
            items.append(item)
        token = response.get("nextPageToken")
        if token is None or token == "":
            return items
        if not isinstance(token, str) or token in seen_tokens:
            raise SafetyError("Paginazione Drive incoerente")
        seen_tokens.add(token)


@dataclass(frozen=True)
class Candidate:
    kind: str
    root: dict
    file: dict
    folder: dict | None = None


@dataclass
class Result:
    candidates: int = 0
    trashed: int = 0
    skipped: int = 0
    changed: int = 0


def same_metadata(left: dict, right: dict) -> bool:
    # Nessun nome o contenuto: confronto completo dei campi ricevuti e della
    # versione, includendo proprietà, parent, età, cestino e permesso.
    keys = ("id", "mimeType", "parents", "trashed", "createdTime", "version", "appProperties", "capabilities")
    return all(left.get(k) == right.get(k) for k in keys)


def discover(service: Any, lesson_roots: list[str], completed: str | None,
             now: datetime) -> tuple[list[Candidate], int]:
    ids = list(lesson_roots) + ([completed] if completed else [])
    if not ids or any(not valid_id(x) for x in ids) or len(set(ids)) != len(ids):
        raise SafetyError("Specificare ID di cartelle distinti e validi")
    if now.tzinfo is None or now.utcoffset() is None:
        raise SafetyError("Ora corrente senza fuso")
    targets = set(ids)
    roots: dict[str, dict] = {}
    for root_id in ids:
        root, _ = get_item(service, root_id)
        if not valid_root(root, root_id):
            raise SafetyError("Cartella configurata non verificabile")
        roots[root_id] = root
    candidates: list[Candidate] = []
    skipped = 0
    seen_folders: set[str] = set()
    seen_files: set[str] = set()
    # L'inventario è completato prima della prima modifica, inclusa la paginazione.
    for root_id in lesson_roots:
        for folder in list_children(service, root_id):
            folder_id = folder["id"]
            if folder_id in targets or not valid_lesson_folder(folder, root_id):
                skipped += 1
                continue
            if folder_id in seen_folders:
                raise SafetyError("Cartella lezione duplicata")
            if folder_id in seen_files:
                raise SafetyError("ID cartella presente anche come file")
            seen_folders.add(folder_id)
            for item in list_children(service, folder_id):
                if item["id"] in targets or item["id"] in seen_files or item["id"] in seen_folders:
                    raise SafetyError("File duplicato o cartella configurata nell'inventario")
                seen_files.add(item["id"])
                if valid_lesson_text(item, folder, now):
                    candidates.append(Candidate("lesson_txt", roots[root_id], item, folder))
                else:
                    skipped += 1
    if completed:
        for item in list_children(service, completed):
            if item["id"] in targets or item["id"] in seen_files or item["id"] in seen_folders:
                raise SafetyError("File duplicato o cartella configurata nell'inventario")
            seen_files.add(item["id"])
            if valid_completed_audio(item, completed):
                candidates.append(Candidate("completed_audio", roots[completed], item))
            else:
                skipped += 1
    return candidates, skipped


def double_check(service: Any, candidate: Candidate, now: datetime) -> str | None:
    """Due letture fresche indipendenti, confrontate con inventario e versione.

    Restituisce l'ETag forte dell'ultima lettura del file, indispensabile per
    If-Match. Le letture dei parent non sono un lock né una transazione.
    """
    proof_etags = []
    for _ in range(2):
        root, _ = get_item(service, candidate.root["id"])
        if not valid_root(root, candidate.root["id"]) or not same_metadata(root, candidate.root):
            return None
        folder = None
        if candidate.folder is not None:
            folder, _ = get_item(service, candidate.folder["id"])
            if (not same_metadata(folder, candidate.folder)
                    or not valid_lesson_folder(folder, root["id"])):
                return None
        item, etag = get_item(service, candidate.file["id"])
        if not same_metadata(item, candidate.file):
            return None
        if folder is None:
            if not valid_completed_audio(item, root["id"]):
                return None
        elif not valid_lesson_text(item, folder, now):
            return None
        if not isinstance(etag, str) or not ETAG_RE.fullmatch(etag):
            return None
        proof_etags.append(etag)
    return proof_etags[1] if proof_etags[0] == proof_etags[1] else None


def trash(service: Any, candidate: Candidate, etag: str) -> None:
    # Nessun delete, rename, move, aggiornamento proprietà o retry mutante.
    request = service.files().update(fileId=candidate.file["id"], body={"trashed": True},
                                     fields="id,trashed", supportsAllDrives=True)
    request.headers["If-Match"] = etag
    response = request.execute(num_retries=0)
    if not isinstance(response, dict) or response.get("id") != candidate.file["id"] or response.get("trashed") is not True:
        raise SafetyError("Esito cestinamento non verificabile; non ripetere automaticamente")


def run(service: Any, lesson_roots: list[str], completed: str | None = None,
        *, apply: bool = False, exclusive: bool = False,
        now: datetime | None = None, out: TextIO = sys.stdout) -> Result:
    if apply and not exclusive:
        raise SafetyError("--apply richiede anche --confirm-exclusive")
    now = now if now is not None else datetime.now(timezone.utc)
    candidates, skipped = discover(service, lesson_roots, completed, now)
    result = Result(skipped=skipped)
    print("Modalità: " + ("APPLY (solo cestino)" if apply else "DRY RUN (nessuna modifica)"), file=out)
    counts: Counter = Counter()
    for candidate in candidates:
        etag = double_check(service, candidate, now)
        if not etag:
            result.changed += 1
            print("ESCLUSO: metadati cambiati o ETag forte non verificabile", file=out)
            continue
        result.candidates += 1
        counts[candidate.kind] += 1
        # Gli ID sono indispensabili per revisione manuale; mai nomi o testo.
        print(f"CANDIDATO {candidate.kind} id={candidate.file['id']}", file=out)
        if not apply:
            continue
        trash(service, candidate, etag)
        result.trashed += 1
        print(f"CESTINATO id={candidate.file['id']}", file=out)
    print(f"Candidati: {result.candidates} (lezioni={counts['lesson_txt']}, audio={counts['completed_audio']}); "
          f"esclusi={result.skipped}, prove fallite={result.changed}, cestinati={result.trashed}.", file=out)
    print("Elementi non marcati o con prove incomplete sono esclusi. "
          "Audio: servono revisione approvata e risultati verificati; la sola cartella completati non basta.", file=out)
    return result


def create_service(credentials_path: str | None) -> Any:
    try:
        if credentials_path:
            info = json.loads(Path(credentials_path).expanduser().read_text(encoding="utf-8"))
        else:
            info = json.loads(os.environ.get("GOOGLE_AUTHORIZED_USER_JSON", ""))
        if not isinstance(info, dict) or info.get("type") != "authorized_user":
            raise SafetyError("Serve JSON OAuth authorized_user")
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_info(info, scopes=[SCOPE])
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("Credenziali OAuth o librerie non disponibili") from None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Retention Drive limitata agli ID indicati: default dry run, mai delete permanente.")
    p.add_argument("--lesson-root", action="append", default=[], metavar="ID", help="Cartella genitore delle lezioni generate; ripetibile")
    p.add_argument("--completed-audio", metavar="ID", help="Cartella degli audio completati")
    p.add_argument("--credentials-json", metavar="PERCORSO", help="JSON OAuth authorized_user privato; in alternativa GOOGLE_AUTHORIZED_USER_JSON")
    p.add_argument("--apply", action="store_true", help="Cestina solo candidati con doppia prova e If-Match")
    p.add_argument("--confirm-exclusive", action="store_true", help="Attesta che pipeline e modifiche concorrenti sono sospese")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ids = args.lesson_root + ([args.completed_audio] if args.completed_audio else [])
    if not ids or any(not valid_id(x) for x in ids) or len(ids) != len(set(ids)):
        print("Errore: fornire ID di cartelle espliciti, validi e distinti.", file=sys.stderr)
        return 2
    if args.apply and not args.confirm_exclusive:
        print("Errore: --apply richiede --confirm-exclusive; sospendere tutti gli scrittori.", file=sys.stderr)
        return 2
    try:
        service = create_service(args.credentials_json)
        result = run(service, args.lesson_root, args.completed_audio, apply=args.apply,
                     exclusive=args.confirm_exclusive)
        return 1 if result.changed else 0
    except Exception:
        # L'esito di un update fallito può essere ambiguo: mai stampare eccezioni
        # (potrebbero contenere nomi o token) e mai ritentare automaticamente.
        print("Errore di configurazione, metadati o Drive. Eventuali modifiche precedenti possono essere riuscite; verificare il cestino prima di riprovare.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
