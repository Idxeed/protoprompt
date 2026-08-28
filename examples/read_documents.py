"""Safely read one local document and print content-free provenance."""

from __future__ import annotations

import argparse
from pathlib import Path

from protoprompt.readers import LocalDocumentReader, ReaderLimits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("README.md"),
        help="local document path (default: ./README.md)",
    )
    parser.add_argument("--password", help="password for an encrypted PDF")
    args = parser.parse_args()
    path = args.path.resolve(strict=True)
    reader = LocalDocumentReader(
        allowed_root=path.parent,
        limits=ReaderLimits(max_bytes=10 * 1024 * 1024, max_pages=500),
    )
    document = reader.read(path, password=args.password)
    print("doc_id:", document.doc_id)
    print("characters:", len(document.text))
    print("provenance:", document.metadata)


if __name__ == "__main__":
    main()
