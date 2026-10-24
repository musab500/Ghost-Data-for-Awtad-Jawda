"""Loader for dataset/evidence_files.csv."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    path: Path          # absolute path on disk
    rel_path: str       # path as written in the manifest
    origin: str
    captured_at: str

    @property
    def kind(self) -> str:
        return self.path.suffix.lower().lstrip(".")


def load_manifest(dataset_dir: Path) -> list[EvidenceFile]:
    dataset_dir = Path(dataset_dir).resolve()
    out = []
    with open(dataset_dir / "evidence_files.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            p = (dataset_dir / row["file_path"]).resolve()
            if dataset_dir not in p.parents:
                raise ValueError(f"evidence path escapes dataset dir: {row['file_path']}")
            if not p.exists():
                raise FileNotFoundError(f"{row['file_id']}: missing {p}")
            out.append(EvidenceFile(row["file_id"], p, row["file_path"],
                                    row["origin"], row["captured_at"]))
    return out
