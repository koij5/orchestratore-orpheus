"""Operazioni locali su PDF: nessun download e nessuna modifica in-place."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF 1.24.0 e versioni precedenti al rinomino del modulo
    import fitz


class BookError(ValueError):
    """Input non valido o operazione non effettuabile."""


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BookError(f"Chiave JSON duplicata: {key}")
        result[key] = value
    return result


def _pdf(path):
    try:
        doc = fitz.open(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BookError(f"Impossibile aprire il PDF: {path}") from exc
    if not doc.is_pdf or doc.needs_pass or len(doc) == 0:
        doc.close()
        raise BookError(f"Il file non è un PDF leggibile senza password o è vuoto: {path}")
    if not (doc.permissions & fitz.PDF_PERM_COPY):
        doc.close()
        raise BookError(f"Il PDF non consente la copia del contenuto: {path}")
    return doc


def _range(start, end, count):
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= count:
        raise BookError(f"Intervallo non valido: {start!r}-{end!r}; pagine PDF disponibili: 1-{count}")


def ensure_fresh(paths):
    """Rifiuta qualunque file esistente, inclusi link simbolici non risolti."""
    paths = [Path(p) for p in paths]
    normalized = [os.path.normcase(str(p.resolve(strict=False))) for p in paths]
    if len(normalized) != len(set(normalized)):
        raise BookError("I percorsi di output devono essere distinti")
    for path in paths:
        if os.path.lexists(path):
            raise BookError(f"Output già esistente, non verrà sovrascritto: {path}")
        if not path.parent.is_dir():
            raise BookError(f"Cartella di destinazione inesistente: {path.parent}")


def write_new(path, data):
    """Scrittura esclusiva anche in presenza di creazione concorrente."""
    path = Path(path)
    try:
        out = path.open("xb")
    except FileExistsError as exc:
        raise BookError(f"Output già esistente, non verrà sovrascritto: {path}") from exc
    try:
        with out:
            out.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def split_manifest(manifest_path, output_dir):
    manifest_path, output_dir = Path(manifest_path), Path(output_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BookError(f"Manifest JSON non leggibile: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("pdf"), str) or not manifest["pdf"].strip():
        raise BookError("Il manifest deve contenere 'pdf' come percorso non vuoto")
    chapters = manifest.get("capitoli")
    if not isinstance(chapters, list) or not chapters:
        raise BookError("Il manifest deve contenere almeno un elemento in 'capitoli'")
    source = Path(manifest["pdf"])
    if not source.is_absolute():
        source = manifest_path.parent / source
    with _pdf(source) as doc:
        names, ranges = set(), []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                raise BookError("Ogni capitolo deve essere un oggetto con nome, da e a")
            name, start, end = chapter.get("nome"), chapter.get("da"), chapter.get("a")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", name, re.IGNORECASE):
                raise BookError("Nome capitolo non valido: usare un nome file .pdf senza cartelle")
            if name in names:
                raise BookError(f"Nome capitolo duplicato: {name}")
            names.add(name)
            _range(start, end, len(doc))
            ranges.append((start, end, name))
        ordered = sorted(ranges)
        for prev, current in zip(ordered, ordered[1:]):
            if current[0] <= prev[1]:
                raise BookError(f"Intervalli sovrapposti: {prev[2]} e {current[2]}")
        # Controlli completi prima di creare qualsiasi file.
        targets = [output_dir / name for _, _, name in ranges]
        if output_dir.exists() and not output_dir.is_dir():
            raise BookError(f"Destinazione non è una cartella: {output_dir}")
        if output_dir.is_dir():
            ensure_fresh(targets)
        else:
            output_dir.mkdir(parents=True)
            ensure_fresh(targets)
        created = []
        try:
            for (start, end, _), target in zip(ranges, targets):
                with fitz.open() as part:
                    part.insert_pdf(doc, from_page=start - 1, to_page=end - 1, links=True, annots=True)
                    data = part.tobytes(garbage=3, deflate=True)
                write_new(target, data)
                created.append(target)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
    return targets


def extract_pages(pdf_path, start=1, end=None):
    """Restituisce solo testo estratto integralmente, con numeri pagina PDF reali."""
    with _pdf(pdf_path) as doc:
        end = len(doc) if end is None else end
        _range(start, end, len(doc))
        pages = []
        empty = []
        for n in range(start, end + 1):
            text = doc[n - 1].get_text("text", sort=False)
            if not text.strip():
                empty.append(n)
            pages.append((n, text))
    if empty:
        raise BookError("Pagine senza testo estraibile: " + ", ".join(map(str, empty)) + ". OCR non incluso; selezionare pagine con testo.")
    return pages


def _exact_rectangles(page, quote):
    """Solo corrispondenze esatte, su singola riga orizzontale, dai caratteri PyMuPDF.

    Non usiamo search_for come verifica: ignora maiuscole e normalizza spazi.
    """
    if not quote.strip() or "\n" in quote or "\r" in quote:
        return []
    if quote not in page.get_text("text", sort=False):
        return []
    rects = []
    for block in page.get_text("rawdict", sort=False)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = line.get("dir", (1, 0))
            if abs(direction[0] - 1) > 0.001 or abs(direction[1]) > 0.001 or line.get("wmode", 0) != 0:
                continue
            chars, boxes = [], []
            for span in line.get("spans", []):
                for item in span.get("chars", []):
                    for ch in item.get("c", ""):
                        chars.append(ch)
                        boxes.append(item["bbox"])
            text = "".join(chars)
            index = 0
            while True:
                index = text.find(quote, index)
                if index < 0:
                    break
                rect = fitz.Rect(boxes[index])
                for box in boxes[index + 1:index + len(quote)]:
                    rect |= fitz.Rect(box)
                if not rect.is_empty and not rect.is_infinite:
                    rects.append(rect)
                index += len(quote)
    return rects


def annotate_quotes(pdf_path, output_path, report_path, quotes, start=1, end=None):
    """Annota solo citazioni verificate; crea copia PDF e rapporto JSON locali."""
    if not isinstance(quotes, list):
        raise BookError("La risposta deve contenere una lista 'citazioni'")
    for item in quotes:
        if not isinstance(item, dict) or set(item) != {"pagina", "testo"} or type(item["pagina"]) is not int or not isinstance(item["testo"], str):
            raise BookError("Ogni citazione deve contenere soltanto 'pagina' intera e 'testo' stringa")
    output_path, report_path = Path(output_path), Path(report_path)
    ensure_fresh([output_path, report_path])
    annotated, missing, seen = [], [], set()
    with _pdf(pdf_path) as doc:
        end = len(doc) if end is None else end
        _range(start, end, len(doc))
        for item in quotes:
            page_no, quote = item["pagina"], item["testo"]
            key = (page_no, quote)
            if key in seen:
                continue
            seen.add(key)
            if not start <= page_no <= end:
                missing.append({**item, "motivo": "pagina fuori dalla sezione selezionata"})
                continue
            page = doc[page_no - 1]
            rects = _exact_rectangles(page, quote)
            if not rects:
                missing.append({**item, "motivo": "non trovata esattamente su una singola riga orizzontale"})
                continue
            for rect in rects:
                page.add_underline_annot(rect)
            annotated.append({**item, "occorrenze": len(rects)})
        report = {"sezione": {"da": start, "a": end}, "annotate": annotated, "non_trovate": missing}
        pdf_data = doc.tobytes(garbage=3, deflate=True)
    created = []
    try:
        write_new(output_path, pdf_data)
        created.append(output_path)
        write_new(report_path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        created.append(report_path)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return report
