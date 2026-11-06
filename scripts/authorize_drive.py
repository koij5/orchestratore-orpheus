#!/usr/bin/env python3
"""Authorize a Desktop OAuth client locally; never print credentials to stdout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera un token OAuth Drive su questo computer.")
    parser.add_argument("client_json", type=Path, help="JSON del client OAuth desktop scaricato da Google Cloud")
    parser.add_argument("--output", required=True, type=Path, help="File privato per il token, FUORI dalla repository")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    root = Path(__file__).resolve().parent.parent
    if output == root or root in output.parents:
        parser.error("Il token deve essere salvato fuori dalla repository Git.")
    if output.exists():
        parser.error("Il file di destinazione esiste già: non lo sovrascrivo.")
    if not output.parent.is_dir():
        parser.error("La cartella di destinazione non esiste.")
    try:
        info = json.loads(args.client_json.expanduser().read_text(encoding="utf-8"))
        if "installed" not in info:
            parser.error("Serve un client OAuth di tipo 'App desktop', non un client Android o Web.")
    except (OSError, ValueError) as exc:
        parser.error(f"Impossibile leggere il JSON del client: {exc}")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        parser.error("Installa prima le dipendenze dal file requirements.txt.")
    print("Attenzione: l'autorizzazione richiesta permette l'accesso ai file del tuo Drive.")
    print("Autorizza solo se comprendi il permesso e usa un account/app dedicati se possibile.")
    flow = InstalledAppFlow.from_client_config(info, scopes=[DRIVE_SCOPE])
    creds = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google non ha restituito un refresh token: nessun file salvato.", file=sys.stderr)
        return 1
    data = json.loads(creds.to_json())
    data["type"] = "authorized_user"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(output), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except BaseException:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    print(f"Token salvato in: {output}")
    print("È una credenziale privata: non caricarla nella repository e non inviarla in chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
