"""Build or serve documentation for both languages (RU default, EN).

Usage:
    python scripts/build_docs.py            # build both to site/ru and site/en
    python scripts/build_docs.py --serve    # serve RU on :8000, EN on :8001
    python scripts/build_docs.py --lang ru  # only one language
    python scripts/build_docs.py --lang en
    python scripts/build_docs.py --clean    # rm site/ first

The site_dir layout is:
    site/
    ├── index.html         <- redirect to /ru/
    ├── ru/
    └── en/
"""

from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
LANGS = ("ru", "en")
PORT_DEFAULT = {"ru": 8000, "en": 8001}


def clean() -> None:
    if SITE.exists():
        import shutil
        shutil.rmtree(SITE)
        print(f"[clean] removed {SITE}")


def build(lang: str) -> None:
    config = ROOT / f"mkdocs.{lang}.yml"
    if not config.exists():
        raise SystemExit(f"missing config: {config}")
    print(f"[build:{lang}] {config.name}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--config-file",
            str(config),
        ],
        cwd=ROOT,
        check=True,
    )


def write_root_index() -> None:
    """Drop a tiny index.html that points visitors to /ru/."""
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(
        """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>protoprompt</title>
<meta http-equiv="refresh" content="0; url=ru/">
<link rel="canonical" href="ru/">
</head>
<body>
<p><a href="ru/">Русская документация</a></p>
<p><a href="en/">English documentation</a></p>
</body>
</html>
""",
        encoding="utf-8",
    )


def serve(lang: str, port: int) -> socketserver.TCPServer:
    site_dir = SITE / lang
    if not site_dir.exists():
        raise SystemExit(f"build site/{lang} first (run without --serve)")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(site_dir), **kwargs)

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
            print(f"[serve:{lang}:{port}] {fmt % args}")

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    print(f"[serve:{lang}] http://127.0.0.1:{port}/")
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lang",
        choices=LANGS,
        help="build/serve only this language (default: both)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the built site(s) instead of building (builds first)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove site/ before building",
    )
    parser.add_argument(
        "--port-ru", type=int, default=PORT_DEFAULT["ru"],
    )
    parser.add_argument(
        "--port-en", type=int, default=PORT_DEFAULT["en"],
    )
    args = parser.parse_args()

    langs = (args.lang,) if args.lang else LANGS

    if args.clean:
        clean()

    for lang in langs:
        build(lang)
    write_root_index()

    if args.serve:
        servers = [serve(lang, args.port_ru if lang == "ru" else args.port_en) for lang in langs]
        if len(servers) == 1:
            servers[0].serve_forever()
            return
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            for s in servers:
                s.shutdown()


if __name__ == "__main__":
    main()
