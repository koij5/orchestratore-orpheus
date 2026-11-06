"""Test offline: PDF generati localmente e tutte le chiamate di rete simulate."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from book_tools import cli
from book_tools.pdf_tools import BookError, annotate_quotes, extract_pages, split_manifest
from book_tools.study import build_prompt, gemini_generate, prepare_material, prepare_quotes


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_pdf(path):
    with fitz.open() as doc:
        for n in range(1, 5):
            page = doc.new_page(width=400, height=500)
            page.insert_text((40, 60), f"Pagina {n}: Testo originale esatto.")
            page.insert_text((40, 90), f"Esercizio {n}: risolvere 2 + 3.")
            if n == 2:
                # Un'immagine vera incorporata, non solo grafica vettoriale.
                pix = fitz.Pixmap(fitz.csRGB, (0, 0, 8, 8), False)
                pix.clear_with(170)
                page.insert_image(fitz.Rect(40, 120, 120, 200), stream=pix.tobytes("png"))
                page.draw_rect(fitz.Rect(150, 120, 200, 170), color=(1, 0, 0), fill=(1, 1, 0))
        doc.save(path)


class BookToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "libro.pdf"
        create_pdf(self.pdf)
        self.original_hash = digest(self.pdf)

    def tearDown(self):
        self.assertEqual(digest(self.pdf), self.original_hash, "PDF originale modificato")
        self.tmp.cleanup()

    def manifest(self, chapters, source="libro.pdf"):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"pdf": source, "capitoli": chapters}), encoding="utf-8")
        return path

    def test_split_preserves_full_pages_text_image_and_rendering(self):
        manifest = self.manifest([
            {"nome": "cap1.pdf", "da": 1, "a": 2},
            {"nome": "cap2.pdf", "da": 4, "a": 4},
        ])
        targets = split_manifest(manifest, self.root / "capitoli")
        self.assertEqual([p.name for p in targets], ["cap1.pdf", "cap2.pdf"])
        with fitz.open(self.pdf) as original, fitz.open(targets[0]) as first, fitz.open(targets[1]) as second:
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 1)
            self.assertIn("Esercizio 2", first[1].get_text())
            self.assertIn("Pagina 4", second[0].get_text())
            self.assertEqual(len(first[1].get_images()), 1)
            for i in range(2):
                self.assertEqual(original[i].get_pixmap().samples, first[i].get_pixmap().samples)

    def test_split_rejects_overlaps_and_invalid_ranges_before_outputs(self):
        cases = [
            [{"nome": "a.pdf", "da": 1, "a": 2}, {"nome": "b.pdf", "da": 2, "a": 3}],
            [{"nome": "a.pdf", "da": 0, "a": 1}],
            [{"nome": "a.pdf", "da": 2, "a": 5}],
            [{"nome": "a.pdf", "da": 3, "a": 2}],
            [{"nome": "a.pdf", "da": True, "a": 2}],
            [{"nome": "../libro.pdf", "da": 1, "a": 1}],
            [{"nome": "a.pdf", "da": 1, "a": 1}, {"nome": "a.pdf", "da": 2, "a": 2}],
        ]
        for chapters in cases:
            with self.subTest(chapters=chapters):
                with self.assertRaises(BookError):
                    split_manifest(self.manifest(chapters), self.root / "out")
                self.assertFalse((self.root / "out").exists())

    def test_split_existing_outputs_and_original_are_never_overwritten(self):
        out = self.root / "out"
        out.mkdir()
        existing = out / "b.pdf"
        existing.write_bytes(b"existing")
        manifest = self.manifest([{"nome": "a.pdf", "da": 1, "a": 1}, {"nome": "b.pdf", "da": 2, "a": 2}])
        with self.assertRaises(BookError):
            split_manifest(manifest, out)
        self.assertFalse((out / "a.pdf").exists())
        self.assertEqual(existing.read_bytes(), b"existing")
        manifest = self.manifest([{"nome": "libro.pdf", "da": 1, "a": 1}])
        with self.assertRaises(BookError):
            split_manifest(manifest, self.root)
        dangling = out / "dangling.pdf"
        dangling.symlink_to(out / "nonexistent")
        with self.assertRaises(BookError):
            split_manifest(self.manifest([{"nome": "dangling.pdf", "da": 1, "a": 1}]), out)
        self.assertTrue(dangling.is_symlink())

    def test_extract_respects_page_numbers_and_rejects_empty_page(self):
        pages = extract_pages(self.pdf, 2, 3)
        self.assertEqual([n for n, _ in pages], [2, 3])
        self.assertIn("Esercizio 2", pages[0][1])
        self.assertNotIn("Pagina 1", pages[0][1])
        blank = self.root / "blank.pdf"
        with fitz.open() as doc:
            doc.new_page()
            doc.save(blank)
        with self.assertRaisesRegex(BookError, "senza testo estraibile"):
            extract_pages(blank)

    def test_locked_and_copy_restricted_pdfs_are_rejected(self):
        restricted = self.root / "restricted.pdf"
        locked = self.root / "locked.pdf"
        for path, password in [(restricted, ""), (locked, "password")]:
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((40, 40), "Testo")
                doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw=password, permissions=fitz.PDF_PERM_PRINT)
            with self.subTest(path=path), self.assertRaises(BookError):
                extract_pages(path)
            with self.subTest(path=path), self.assertRaises(BookError):
                split_manifest(self.manifest([{"nome": "cap.pdf", "da": 1, "a": 1}], source=path.name), self.root / "restricted-out")
            self.assertFalse((self.root / "restricted-out").exists())

    def test_manifest_duplicate_json_key_is_rejected(self):
        manifest = self.root / "manifest.json"
        manifest.write_text('{"pdf":"libro.pdf", "capitoli":[], "capitoli":[{"nome":"cap.pdf","da":1,"a":1}]}', encoding="utf-8")
        with self.assertRaisesRegex(BookError, "Chiave JSON duplicata"):
            split_manifest(manifest, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_annotations_only_exact_page_case_and_single_line(self):
        quotes = [
            {"pagina": 2, "testo": "Testo originale esatto."},
            {"pagina": 2, "testo": "testo originale esatto."},
            {"pagina": 1, "testo": "non presente"},
            {"pagina": 3, "testo": "Testo originale esatto."},
            {"pagina": 2, "testo": "originale esatto.\nEsercizio 2"},
            {"pagina": 2, "testo": "Testo originale esatto."},
        ]
        out, report = self.root / "annotato.pdf", self.root / "report.json"
        result = annotate_quotes(self.pdf, out, report, quotes, start=1, end=2)
        self.assertEqual(len(result["annotate"]), 1)
        self.assertEqual(result["annotate"][0]["occorrenze"], 1)
        self.assertEqual(len(result["non_trovate"]), 4)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)
        with fitz.open(out) as doc, fitz.open(self.pdf) as original:
            self.assertEqual(len(doc), 4)
            self.assertEqual(list(doc[0].annots() or []), [])
            annotated_page = doc[1]
            annotations = list(annotated_page.annots() or [])
            self.assertEqual(len(annotations), 1)
            self.assertEqual(annotations[0].type[1], "Underline")
            self.assertEqual(list(original[1].annots() or []), [])
            self.assertEqual(doc[1].get_text(), original[1].get_text())
            self.assertEqual(len(doc[1].get_images()), 1)

    def test_annotations_multiple_occurrences_and_fabricated_quote(self):
        repeated = self.root / "ripetuto.pdf"
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((20, 40), "Frase esatta. Frase esatta.")
            doc.save(repeated)
        output, report_path = self.root / "out.pdf", self.root / "rapporto.json"
        report = annotate_quotes(repeated, output, report_path, [
            {"pagina": 1, "testo": "Frase esatta."},
            {"pagina": 1, "testo": "Frase inventata."},
        ])
        self.assertEqual(report["annotate"][0]["occorrenze"], 2)
        self.assertEqual(report["non_trovate"][0]["testo"], "Frase inventata.")
        with fitz.open(output) as doc:
            self.assertEqual(len(list(doc[0].annots())), 2)

    def test_annotations_reject_bad_structures_and_existing_outputs(self):
        for quotes in [None, [{"pagina": True, "testo": "x"}], [{"pagina": 1, "testo": "x", "extra": 1}], [{"pagina": "1", "testo": "x"}]]:
            with self.subTest(quotes=quotes):
                with self.assertRaises(BookError):
                    annotate_quotes(self.pdf, self.root / "copy.pdf", self.root / "r.json", quotes)
                self.assertFalse((self.root / "copy.pdf").exists())
        with self.assertRaises(BookError):
            annotate_quotes(self.pdf, self.pdf, self.root / "r.json", [])
        self.assertFalse((self.root / "r.json").exists())
        with self.assertRaises(BookError):
            annotate_quotes(self.pdf, self.root / "same", self.root / "same", [])

    def test_prompts_subjects_reference_lessons_and_missing_data(self):
        for subject in ["scienze", "fisica", "letteratura", "inglese", "purgatorio"]:
            prompt = build_prompt(subject, [(2, "PDF esempio")], ["lezione esempio"])
            self.assertIn(subject, prompt)
            self.assertIn("PAGINA PDF 2", prompt)
            self.assertIn("lezione esempio", prompt)
            self.assertIn("Non inventare", prompt)
            self.assertIn("dato non fornito", prompt)
        self.assertIn("Nessuna lezione fornita", build_prompt("fisica", [(1, "esempio")], []))

    def test_material_and_quotes_use_mock_without_network(self):
        sent = []
        def fake(prompt, **kwargs):
            sent.append((prompt, kwargs))
            if kwargs["json_mode"]:
                return json.dumps({"citazioni": [{"pagina": 2, "testo": "Testo originale esatto."}]})
            return "## Appunti\nDato non fornito."
        with patch("urllib.request.urlopen", side_effect=AssertionError("Rete vietata")):
            text = prepare_material(self.pdf, "fisica", ["Appunti lezione"], start=2, end=2, generate=fake)
            quotes = prepare_quotes(self.pdf, "scienze", [], start=2, end=2, generate=fake)
        self.assertIn("da verificare", text)
        self.assertEqual(quotes[0]["pagina"], 2)
        self.assertIn("Appunti lezione", sent[0][0])
        self.assertIn("PAGINA PDF 2", sent[1][0])
        self.assertIn("solo citazioni letterali", sent[1][0])
        self.assertNotIn("PAGINA PDF 1", sent[1][0])
        self.assertEqual([kw["json_mode"] for _, kw in sent], [False, True])

    def test_bad_model_quote_response_is_rejected(self):
        for response in ["non json", "[]", '{"citazioni": [], "extra": 1}', '{"citazioni": "x"}', '{"citazioni": [], "citazioni": []}']:
            with self.subTest(response=response):
                with self.assertRaises(BookError):
                    prepare_quotes(self.pdf, "fisica", [], generate=lambda *a, **k: response)

    def test_gemini_http_is_mocked_key_not_in_url_and_errors_fail_closed(self):
        body = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "testo"}]}}]}
        def fake_open(request, timeout):
            self.assertIn("models/gemini-3.8-flash:generateContent", request.full_url)
            self.assertNotIn("fake-secret", request.full_url)
            self.assertEqual(request.get_header("X-goog-api-key"), "fake-secret")
            payload = json.loads(request.data)
            self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
            self.assertEqual(payload["contents"][0]["parts"][0]["text"], "prompt sintetico")
            return io.BytesIO(json.dumps(body).encode())
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-secret"}), patch("urllib.request.urlopen", side_effect=fake_open):
            self.assertEqual(gemini_generate("prompt sintetico", json_mode=True), "testo")
            body["candidates"][0]["finishReason"] = "MAX_TOKENS"
            with self.assertRaisesRegex(BookError, "incompleta"):
                gemini_generate("prompt sintetico", json_mode=True)
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen", side_effect=AssertionError("Rete vietata")):
            with self.assertRaisesRegex(BookError, "GEMINI_API_KEY"):
                gemini_generate("prompt sintetico")

    def test_cli_opt_in_terms_and_output_validation_before_any_network(self):
        args = ["materiali", "--pdf", str(self.pdf), "--materia", "fisica", "--output", str(self.root / "out.md")]
        with patch("urllib.request.urlopen", side_effect=AssertionError("Rete vietata")), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 1)
            self.assertEqual(cli.main(args + ["--invia-a-gemini"]), 1)
            self.assertEqual(cli.main(args + ["--invia-a-gemini", "--conferma-uso-consentito", "--da", "1"]), 1)
        self.assertFalse((self.root / "out.md").exists())

    def test_cli_complete_offline_flow(self):
        manifest = self.manifest([{"nome": "cap.pdf", "da": 2, "a": 2}])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["dividi", "--manifest", str(manifest), "--output-dir", str(self.root / "cap")]), 0)
        cap = self.root / "cap" / "cap.pdf"
        out, report = self.root / "annotated.pdf", self.root / "report.json"
        with patch("book_tools.cli.prepare_quotes", return_value=[{"pagina": 1, "testo": "Testo originale esatto."}]) as mocked, patch("urllib.request.urlopen", side_effect=AssertionError("Rete vietata")), redirect_stdout(io.StringIO()):
            rc = cli.main(["sottolinea", "--pdf", str(cap), "--materia", "scienze", "--output", str(out), "--rapporto", str(report), "--invia-a-gemini", "--conferma-uso-consentito"])
            self.assertEqual(rc, 0)
            mocked.assert_called_once()
        self.assertTrue(out.exists())
        self.assertEqual(len(json.loads(report.read_text())["annotate"]), 1)


if __name__ == "__main__":
    unittest.main()
