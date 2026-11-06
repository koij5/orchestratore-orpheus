#!/usr/bin/env python3
"""Generate the private kernel metadata in the ephemeral Actions checkout."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*$")


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
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Metadati Kaggle preparati nel checkout temporaneo.")


if __name__ == "__main__":
    main()
