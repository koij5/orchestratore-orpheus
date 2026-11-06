"""Classificazione locale e conservativa di trascrizioni italiane timestampate.

Il classificatore è un candidato basato su zero-shot NLI (modello MIT
``MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7``), non una
valutazione della qualità garantita. Non invia testo a provider/API: la
pipeline Transformers viene caricata localmente solo su richiesta. Il testo è
classificato in finestre temporali, mai come singola sequenza di durata
illimitata. Le righe devono avere formato ``[HH:MM:SS - HH:MM:SS] testo``.
"""
from __future__ import annotations

import re
from typing import Any, Callable

SOURCE_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
WINDOW_SECONDS = 60
MIN_SCORE = 0.55
MIN_MARGIN = 0.15
OUTPUT_VERSION = 1

# Le definizioni sono intenzionalmente in italiano e distinguono le materie
# scolastiche dal tema letterario specifico del Purgatorio dantesco.
LABEL_DEFINITIONS = {
    "biologia": "Biologia: esseri viventi, cellule, organismi, anatomia, genetica, ecologia o processi biologici.",
    "chimica": "Chimica: elementi, atomi, molecole, reazioni, sostanze, legami e fenomeni chimici.",
    "fisica": "Fisica: materia ed energia, forze, moto, elettricità, onde, termodinamica e fenomeni fisici.",
    "inglese": "Inglese: lezione o esercizi sulla lingua inglese, grammatica, lessico, pronuncia o comprensione in inglese; non letteratura italiana.",
    "letteratura": "Letteratura: analisi, storia, autori e opere letterarie in generale, soprattutto italiane; escludere la trattazione specifica del Purgatorio di Dante.",
    "purgatorio": "Purgatorio: studio della cantica Purgatorio della Divina Commedia di Dante, dei suoi canti, personaggi, versi, struttura o temi. Non il generico concetto religioso di purgatorio.",
    "altro": "Altro o non classificabile: matematica, disegno, informatica, arte, storia, filosofia, religione, scienze motorie, conversazioni, rumore o contenuto non pertinente/insufficiente.",
}
SUBJECTS = tuple(k for k in LABEL_DEFINITIONS if k != "altro")
_TIMESTAMP_LINE = re.compile(r"^\[(\d{2,}):(\d{2}):(\d{2})\s+-\s+(\d{2,}):(\d{2}):(\d{2})\]\s*(.*)$")


def load_classifier(device: str | int = "cpu") -> Any:
    """Carica la pipeline NLI Transformers locale sul device richiesto.

    Il modello dichiarato ha licenza MIT. Questa funzione non scarica o
    contatta servizi esterni esplicitamente; Transformers può aver bisogno
    di asset già presenti in cache, e ogni eventuale download dipende dalla
    configurazione dell'ambiente chiamante.
    """
    from transformers import pipeline
    return pipeline("zero-shot-classification", model=SOURCE_MODEL, device=device)


def _seconds(h: str, m: str, s: str) -> int:
    hour, minute, second = int(h), int(m), int(s)
    if minute > 59 or second > 59:
        raise ValueError("invalid timestamp")
    return hour * 3600 + minute * 60 + second


def _parse(text: str) -> list[dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("transcript must be non-empty text")
    lines = text.splitlines()
    parsed = []
    current_interval = None
    for line_number, raw in enumerate(lines, 1):
        match = _TIMESTAMP_LINE.fullmatch(raw.strip())
        if match:
            start = _seconds(*match.groups()[:3])
            end = _seconds(*match.groups()[3:6])
            if end < start:
                raise ValueError(f"invalid interval on line {line_number}")
            current_interval = (start, end)
            content = match.group(7).strip()
        elif current_interval is not None:
            start, end = current_interval
            content = raw.strip()
        else:
            raise ValueError(f"invalid timestamped line {line_number}")
        parsed.append({"line": line_number, "start": start, "end": end, "text": content})
    if not parsed:
        raise ValueError("transcript contains no timestamped lines")
    if not any(row["text"] for row in parsed):
        raise ValueError("transcript contains no recognized text")
    return parsed


def _classify(classifier: Any, text: str) -> tuple[str, float, float]:
    result = classifier(text, candidate_labels=list(LABEL_DEFINITIONS), multi_label=False,
                        hypothesis_template="Questo testo riguarda {}.")
    if not isinstance(result, dict) or not isinstance(result.get("labels"), (list, tuple)) or not isinstance(result.get("scores"), (list, tuple)):
        raise ValueError("classifier returned invalid result")
    scores = {}
    for label, score in zip(result["labels"], result["scores"]):
        if label not in LABEL_DEFINITIONS or isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        score = float(score)
        if score != score or score < 0 or score > 1:
            raise ValueError("classifier returned invalid score")
        scores[label] = max(score, scores.get(label, 0.0))
    if not scores:
        raise ValueError("classifier returned no usable labels")
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    top_label, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return top_label, top_score, top_score - runner_up


def classify_transcript(text: str, classifier: Callable[..., Any] | None = None,
                        window_seconds: int = WINDOW_SECONDS,
                        min_score: float = MIN_SCORE, min_margin: float = MIN_MARGIN) -> dict[str, Any]:
    """Restituisce segmenti contigui e inclusivi che coprono tutte le righe.

    ``classifier`` è un hook compatibile con la pipeline HF, utile per test
    offline. Ogni finestra è scelta dal timestamp iniziale della riga e
    contiene solo testo di quella finestra. Ogni segmento ha uno ``status``:
    ``classified`` per una materia target sopra entrambe le soglie,
    ``unsupported`` per ``altro`` sopra entrambe le soglie (con
    ``materia: unsupported``), oppure ``uncertain`` se score o margine sono
    sotto soglia (con ``materia: null``). I segmenti adiacenti si fondono solo
    se coincidono sia ``materia`` sia ``status``.
    """
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds < 1:
        raise ValueError("window_seconds must be a positive integer")
    for value in (min_score, min_margin):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("thresholds must be between zero and one")
    rows = _parse(text)
    if classifier is None:
        classifier = load_classifier()

    windows: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        windows.setdefault(row["start"] // window_seconds, []).append(row)

    decisions = {}
    for key, members in windows.items():
        label, score, margin = _classify(classifier, "\n".join(r["text"] for r in members))
        if score < min_score or margin < min_margin:
            subject, status = None, "uncertain"
        elif label == "altro":
            subject, status = "unsupported", "unsupported"
        else:
            subject, status = label, "classified"
        decisions[key] = (subject, status, score, margin)

    segments: list[dict[str, Any]] = []
    for row in rows:
        subject, status, confidence, margin = decisions[row["start"] // window_seconds]
        line_no = row["line"]
        if (segments and segments[-1]["materia"] == subject and
                segments[-1]["status"] == status and segments[-1]["end_line"] + 1 == line_no):
            seg = segments[-1]
            seg["end_line"] = line_no
            seg["confidence"] = max(seg["confidence"], confidence)
            seg["runner_up_margin"] = min(seg["runner_up_margin"], margin)
        else:
            segments.append({"start_line": line_no, "end_line": line_no,
                             "materia": subject, "status": status, "confidence": confidence,
                             "runner_up_margin": margin})
    return {"source_model": SOURCE_MODEL, "window_seconds": window_seconds,
            "thresholds": {"min_score": float(min_score), "min_margin": float(min_margin)},
            "version": OUTPUT_VERSION, "segments": segments}
