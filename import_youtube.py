#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yt_dlp

from youtube_search import Release, resolve

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "yt-cache"
os.environ["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")


def find_deno() -> str:
    for candidate in (
        shutil.which("deno"),
        Path.home() / ".deno/bin/deno",
        "/usr/local/bin/deno",
        "/usr/bin/deno",
    ):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise SystemExit(
        "ERROR: deno not found. YouTube downloads require Deno 2.3+.\n"
        "Install: curl -fsSL https://deno.land/install.sh | sh"
    )


def has_audio(directory: Path) -> bool:
    return any(directory.glob(f"*.{ext}") for ext in ("flac", "m4a", "opus", "mp3", "ogg"))


def download(url: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True,
        "noprogress": True,
        "ignoreerrors": True,
        "paths": {"home": str(dest)},
        "outtmpl": "%(title)s [%(id)s].%(ext)s",
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
            {
                "key": "MetadataFromField",
                "formats": ["%(playlist_index)s:%(track_number)s"],
            },
        ],
        "writethumbnail": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "remote_components": ["ejs:github"],
        "js_runtimes": {"deno": {"path": find_deno()}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def beet_import(dest: Path, release: Release, albumartist: str | None) -> None:
    cmd = ["beet", "import", "-A"]
    if release.release_type == "single":
        cmd.append("-s")
    cmd.extend(
        [
            "--set",
            f"albumtype={release.release_type}",
            "--set",
            "comp=0",
        ]
    )
    if albumartist:
        cmd.extend(["--set", f"albumartist={albumartist}"])
        if release.release_type == "single":
            cmd.extend(["--set", f"artist={albumartist}"])
    if release.release_type != "single":
        cmd.extend(["--set", f"album={release.title}"])
    if release.year:
        cmd.extend(["--set", f"year={release.year}"])
    cmd.append(str(dest))
    subprocess.run(cmd, check=False)


def cleanup(dest: Path) -> None:
    for path in dest.iterdir():
        if path.suffix.lower() not in {".flac", ".m4a", ".opus", ".mp3", ".ogg"}:
            path.unlink(missing_ok=True)
    try:
        dest.rmdir()
    except OSError:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: import_youtube.py QUERY_OR_ARTIST_URL")
    query = " ".join(sys.argv[1:])
    albumartist, releases = resolve(query)
    if not releases:
        raise SystemExit("No albums or singles found.")
    print(f"Downloading {len(releases)} release(s)", file=sys.stderr)

    CACHE_DIR.mkdir(exist_ok=True)
    for index, release in enumerate(releases, start=1):
        print(f"[{index}/{len(releases)}] {release.release_type} {release.title}", file=sys.stderr)
        dest = CACHE_DIR / "incoming"
        if dest.exists():
            shutil.rmtree(dest)
        download(release.url, dest)
        if not has_audio(dest):
            print(f"No audio files downloaded for: {release.title}", file=sys.stderr)
            continue
        beet_import(dest, release, albumartist)
        cleanup(dest)


if __name__ == "__main__":
    main()
