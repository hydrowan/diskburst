"""CLI entry point: start the scan and serve the visualizer on localhost."""

from __future__ import annotations

import argparse
import os

from .cache import load_cache
from .config import DEFAULT_CONFIG_PATH, load_config
from .scanner import DEFAULT_BIG_FILE_BYTES, DEFAULT_WORKERS, Scanner
from .server import App, make_server

DEFAULT_ROOTS = ["~", "/Applications", "/Library", "/opt/homebrew", "/private/var"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diskburst",
        description="Read-only disk usage scanner with a sunburst UI on localhost.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"TOML config file (default: {DEFAULT_CONFIG_PATH}; flags override it)",
    )
    parser.add_argument("--roots", nargs="+", default=DEFAULT_ROOTS, help="directories to scan")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--min-file-mb",
        type=float,
        default=DEFAULT_BIG_FILE_BYTES / 1e6,
        help="retain individual files at least this large (smaller ones still count in totals)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore any saved scan; start from a fresh live scan",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="disable FSEvents live updates (also off when watchdog is not installed)",
    )
    return parser


def resolve_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args with config-file values as defaults (CLI wins)."""
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config")
    pre, _ = bootstrap.parse_known_args(argv)
    cfg = load_config(pre.config)
    parser = build_parser()
    defaults = {k: cfg[k] for k in ("roots", "port", "workers", "min_file_mb") if k in cfg}
    if cfg.get("cache") is False:
        defaults["no_cache"] = True
    if cfg.get("live") is False:
        defaults["no_live"] = True
    parser.set_defaults(**defaults)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = resolve_args(argv)
    roots = [r for r in args.roots if os.path.isdir(os.path.expanduser(r))]
    big_file_bytes = int(args.min_file_mb * 1e6)
    cached = None if args.no_cache else load_cache(roots, big_file_bytes, args.workers)
    app = App(
        lambda: Scanner(roots, big_file_bytes=big_file_bytes, workers=args.workers),
        cached=cached,
        live=not args.no_live,
    )
    server = make_server(app, port=args.port)  # bind before scanning so a busy port fails fast
    app.start()
    if cached is not None:
        print(f"Loaded cached scan ({cached.cache_age / 60:.0f} min old); rescanning in background")
    print(f"Scanning {', '.join(app.scanner.root_paths)}")
    print(f"Serving http://127.0.0.1:{args.port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
