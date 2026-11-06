"""Interfaccia CLI di book_tools."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pdf_tools import BookError, annotate_quotes, ensure_fresh, split_manifest, write_new
from .study import SUBJECTS, prepare_material, prepare_quotes, read_lessons


def _common(parser):
    parser.add_argument("--pdf", required=True, help="PDF locale autorizzato, per esempio un capitolo")
    parser.add_argument("--materia", required=True, choices=sorted(SUBJECTS))
    parser.add_argument("--lezione", action="append", default=[], help="file UTF-8; ripetibile")
    parser.add_argument("--da", type=int, help="prima pagina PDF 1-indexed (richiede --a)")
    parser.add_argument("--a", type=int, help="ultima pagina PDF inclusa (richiede --da)")
    parser.add_argument("--modello", default="gemini-3.8-flash", help="modello Gemini disponibile nel proprio account")
    parser.add_argument("--invia-a-gemini", action="store_true", help="consenti esplicitamente l'invio dei testi a Google")
    parser.add_argument("--conferma-uso-consentito", action="store_true", help="confermi 18+, uso professionale/non consumer e rispetto dei termini Gemini, inclusa la restrizione sui minori")
    parser.add_argument("--output", required=True, help="nuovo percorso di output; non sovrascrive")


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m book_tools", description="PDF autorizzati e bozze di studio; gli originali non vengono sovrascritti.")
    commands = parser.add_subparsers(dest="command", required=True)
    split = commands.add_parser("dividi", help="crea PDF di capitolo dal manifest JSON")
    split.add_argument("--manifest", required=True)
    split.add_argument("--output-dir", required=True)
    material = commands.add_parser("materiali", help="genera una bozza di studio con Gemini")
    _common(material)
    highlight = commands.add_parser("sottolinea", help="verifica citazioni e annota una copia del PDF")
    _common(highlight)
    highlight.add_argument("--rapporto", required=True, help="nuovo file JSON con citazioni annotate e non trovate")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "dividi":
            paths = split_manifest(args.manifest, args.output_dir)
            for path in paths:
                print(path)
            return 0
        if not args.invia_a_gemini:
            raise BookError("Invio non autorizzato: usa --invia-a-gemini solo se acconsenti a trasmettere PDF estratto e lezioni a Google")
        if not args.conferma_uso_consentito:
            raise BookError("Gemini API richiede 18+, uso professionale/non consumer e rispetto delle limitazioni sui minori: leggere il README e confermare con --conferma-uso-consentito solo se applicabile")
        if (args.da is None) != (args.a is None):
            raise BookError("Specificare sia --da sia --a, oppure nessuno dei due")
        start = 1 if args.da is None else args.da
        outputs = [args.output]
        if args.command == "sottolinea":
            outputs.append(args.rapporto)
        ensure_fresh(outputs)  # Prima di inviare dati o creare file.
        lessons = read_lessons(args.lezione)
        if args.command == "materiali":
            if Path(args.output).suffix.lower() not in {".md", ".txt"}:
                raise BookError("L'output dei materiali deve essere .md o .txt")
            result = prepare_material(args.pdf, args.materia, lessons, start=start, end=args.a, model=args.modello)
            write_new(args.output, result.encode("utf-8"))
            print(f"Bozza da verificare salvata: {args.output}")
        else:
            if Path(args.output).suffix.lower() != ".pdf" or Path(args.rapporto).suffix.lower() != ".json":
                raise BookError("Servono un output .pdf e un rapporto .json")
            quotes = prepare_quotes(args.pdf, args.materia, lessons, start=start, end=args.a, model=args.modello)
            report = annotate_quotes(args.pdf, args.output, args.rapporto, quotes, start=start, end=args.a)
            print(f"Copia PDF: {args.output}; rapporto: {args.rapporto}; citazioni non trovate: {len(report['non_trovate'])}")
        return 0
    except (BookError, OSError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
