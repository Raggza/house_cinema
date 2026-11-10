"""Main pipeline entry point wiring the workflow modules together.

Usage:
    python -m source.pipeline --path "E:\\Movies" [--path "F:\\Shows"]          # start the hub
    python -m source.pipeline --path "E:\\Movies" --check-config                # validate config
    python -m source.pipeline scan [--dry-run] [--path "E:\\Movies"]            # scan, update the index
    python -m source.pipeline list                                              # show the index, no scan
    python -m source.pipeline info <id>                                         # one video's details
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from source.modules.library import (
    LibraryIndex,
    LibraryIndexError,
    read_known_root_paths,
    scan_library,
)
from source.image.thumbnails import thumb_path
from source.modules.library.media_info import process_media
from source.modules.library.scanner import locate
from source.modules.subtitles import find_subtitles
from source.modules.library.report import (
    format_diff,
    format_index,
    format_info,
    format_media_progress,
    format_media_report,
    format_progress,
    format_scan,
)
from source.modules.server import ServerStartError, run_server
from source.schemas.settings import Settings
from source.utils.config import ConfigError, describe, load_settings
from source.utils.ffmpeg import find_tools
from source.utils.progress import ProgressLine, stream_is_terminal

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_SERVER_ERROR = 3
EXIT_INDEX_ERROR = 4
EXIT_NOT_FOUND = 5

logger = logging.getLogger("house_cinema")


def _add_common_options(parser: argparse.ArgumentParser, *, in_subcommand: bool) -> None:
    # In a subcommand, unset options must not overwrite values given before the subcommand.
    default = argparse.SUPPRESS if in_subcommand else None
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        metavar="FOLDER",
        default=default,
        help="Video library folder. Repeat for several folders. Replaces library.paths from config.",
    )
    parser.add_argument("--host", default=default, help="Address to bind (default from config: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=default,
                        help="Port to listen on (default from config: 8000).")
    parser.add_argument("--log-level", default=default, help="DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    parser.add_argument("--config", type=Path, default=default, help="Path to config.yml.")
    parser.add_argument("--env-file", type=Path, default=default, help="Path to .env file.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m source.pipeline",
        description="LAN video hub: serve local video folders to devices on the same network.",
    )
    _add_common_options(parser, in_subcommand=False)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the configuration, print it and exit.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    scan = sub.add_parser("scan", help="Scan the library folders and list the videos found.")
    _add_common_options(scan, in_subcommand=True)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change, but do not update the index.",
    )
    scan.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show a live progress line on stderr. Default: only when stderr is a terminal.",
    )
    scan.add_argument(
        "--no-media",
        action="store_true",
        help="Skip reading video details and making thumbnails (ffprobe/ffmpeg).",
    )
    scan.add_argument(
        "--refresh-media",
        action="store_true",
        help="Read video details and remake thumbnails for every video, not only new/changed ones.",
    )
    listing = sub.add_parser("list", help="List the videos in the index, without scanning.")
    _add_common_options(listing, in_subcommand=True)
    info = sub.add_parser("info", help="Show details of one video (duration, resolution, codecs).")
    _add_common_options(info, in_subcommand=True)
    info.add_argument("media_id", metavar="ID", help="Video ID as shown by scan/list (a unique prefix is enough).")
    return parser


def _use_utf8_console() -> None:
    # Vietnamese folder names must print correctly in the Windows console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )


def _scan(settings: Settings, index: LibraryIndex, args: argparse.Namespace) -> int:
    if not settings.library_paths:
        # Scanning "no folder" would empty the index: refuse and say where folders are set.
        print(f"Config error: no library folder yet. Start the hub and add folders at "
              f"http://localhost:{settings.port}/admin on this computer, or pass --path.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    _run_scan(settings, index, args)
    return EXIT_OK


def _run_scan(settings: Settings, index: LibraryIndex, args: argparse.Namespace) -> None:
    show_progress = args.progress if args.progress is not None else stream_is_terminal(sys.stderr)
    with ProgressLine(sys.stderr, enabled=show_progress) as line:
        result = scan_library(
            settings.library_paths,
            settings.video_extensions,
            on_progress=lambda p: line.update(format_progress(p), tail=p.current_folder),
            known_files=index.known_files(),
        )
    diff = index.apply_scan(result, dry_run=args.dry_run)
    if not diff.is_empty():
        logger.info("Index %s: %s", "dry run" if args.dry_run else "updated", diff.summary())
    print(format_scan(result))
    print()
    print(format_diff(diff, dry_run=args.dry_run))
    if args.dry_run:
        print("(dry run: index not changed)")
        return
    if args.no_media:
        return
    with ProgressLine(sys.stderr, enabled=show_progress) as line:
        media = process_media(
            index, find_tools(settings), settings.cache_dir,
            workers=settings.media_workers, renamed=diff.renamed, force=args.refresh_media,
            on_progress=lambda p: line.update(format_media_progress(p), tail=p.current),
        )
    print(format_media_report(media))


def _info(settings: Settings, index: LibraryIndex, media_id: str) -> int:
    matches = index.find(media_id)
    if not matches:
        print(f"No video with ID '{media_id}' in the index. IDs are shown by `scan` and `list`.",
              file=sys.stderr)
        return EXIT_NOT_FOUND
    if len(matches) > 1:
        print(f"ID '{media_id}' matches {len(matches)} videos; use more characters:", file=sys.stderr)
        for item in matches[:10]:
            print(f"  {item.id}  {item.relative_path}", file=sys.stderr)
        return EXIT_NOT_FOUND
    item = matches[0]
    thumb = thumb_path(settings.cache_dir, item.id)
    subtitles = (find_subtitles(locate(item.library_root, item.relative_path), settings.video_extensions)
                 if item.available else None)
    print(format_info(item, str(thumb) if thumb.is_file() else None, subtitles))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_console()
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(
            config_file=args.config,
            env_file=args.env_file,
            cli_paths=args.paths,
            cli_overrides={"host": args.host, "port": args.port, "log_level": args.log_level},
            known_roots_reader=read_known_root_paths,
        )
    except ConfigError as exc:
        for line in exc.lines:
            print(f"Config error: {line}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.check_config:
        print("Configuration OK")
        print(describe(settings))
        return EXIT_OK

    _setup_logging(settings.log_level)

    if args.command in ("scan", "list", "info"):
        try:
            with LibraryIndex(settings.index_path) as index:
                if index.recovered_from is not None:
                    print(f"Warning: the index was damaged ({index.recovery_reason}); it was kept as "
                          f"{index.recovered_from.name} and a new index was started.", file=sys.stderr)
                if args.command == "list":
                    print(format_index(index.items(), index.roots(), str(settings.index_path)))
                elif args.command == "info":
                    return _info(settings, index, args.media_id)
                else:
                    return _scan(settings, index, args)
        except LibraryIndexError as exc:
            print(f"Index error: {exc}", file=sys.stderr)
            return EXIT_INDEX_ERROR
        return EXIT_OK

    logger.info("Effective configuration:\n%s", describe(settings))
    try:
        run_server(settings)
    except ServerStartError as exc:
        print(f"Server error: {exc}", file=sys.stderr)
        return EXIT_SERVER_ERROR
    except LibraryIndexError as exc:
        print(f"Index error: {exc}", file=sys.stderr)
        return EXIT_INDEX_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
