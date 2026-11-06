#!/usr/bin/env python3
"""Generate the private kernel metadata in the ephemeral Actions checkout."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*$")
BUNDLE_POINT = "# ORPHEUS_CLASSIFIER_BUNDLE_POINT"
BUNDLE_SIGNATURE = "# ORPHEUS_GENERATED_CLASSIFIER_BUNDLE"


def required_slug(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not SLUG.fullmatch(value):
        raise ValueError(f"{name} deve avere formato utente/nome valido")
    return value


def main() -> None:
    directory = Path(os.environ.get("KAGGLE_KERNEL_DIR", "kaggle_kernel"))
    path = directory / "kernel-metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["id"] = required_slug("KAGGLE_KERNEL_SLUG")
    metadata["dataset_sources"] = [required_slug("KAGGLE_DATASET_SLUG")]
    if metadata.get("is_private") is not True:
        raise ValueError("Il notebook Kaggle deve essere privato")
    script = directory / metadata.get("code_file", "")
    if not script.is_file() or script.parent.resolve() != directory.resolve():
        raise ValueError("Il file del kernel non è presente nella cartella prevista")
    script_source = script.read_text(encoding="utf-8")
    classifier_path = directory / "subject_classifier.py"
    if not classifier_path.is_file():
        raise ValueError("Modulo classifier non presente nella cartella del kernel")
    classifier_source = classifier_path.read_text(encoding="utf-8")
    if script_source.count(BUNDLE_POINT) != 1 or BUNDLE_SIGNATURE in script_source:
        raise ValueError("Punto di bundle mancante, duplicato o script già preparato")
    bundle = "\n".join((
        BUNDLE_SIGNATURE,
        "import sys as _orpheus_sys",
        "import types as _orpheus_types",
        "_orpheus_classifier = _orpheus_types.ModuleType('subject_classifier')",
        "exec(compile(" + repr(classifier_source) + ", '<orpheus embedded subject_classifier>', 'exec'), _orpheus_classifier.__dict__)",
        "_orpheus_sys.modules['subject_classifier'] = _orpheus_classifier",
        "",
    ))
    script.write_text(script_source.replace(BUNDLE_POINT, bundle + BUNDLE_POINT), encoding="utf-8")
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Metadati e script Kaggle standalone preparati nel checkout temporaneo.")


if __name__ == "__main__":
    main()
