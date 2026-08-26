#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yt_dlp
from mutagen import File as MutagenFile

from youtube_search import Release, ResolveError, resolve

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "yt-cache"
MUSIC_DIR = SCRIPT_DIR / "beets-shit"
AUDIO_EXTS = {".flac", ".m4a", ".opus", ".mp3", ".ogg"}
TRACK_TAG_KEYS = {"track", "tracknumber", "tracktotal", "totaltracks"}
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
    raise RuntimeError(
        "deno not found. YouTube downloads require Deno 2.3+. "
        "Install: curl -fsSL https://deno.land/install.sh | sh"
    )


def has_audio(directory: Path) -> bool:
    return any(directory.rglob(f"*.{ext}") for ext in ("flac", "m4a", "opus", "mp3", "ogg"))


class _YtdlpLogger:
    def __init__(self, on_log=None):
        self.on_log = on_log

    def debug(self, msg):
        return None

    def info(self, msg):
        return None

    def warning(self, msg):
        text = str(msg)
        if "SABR" in text:
            return None
        if self.on_log:
            self.on_log(text)

    def error(self, msg):
        if self.on_log:
            self.on_log(str(msg))


def download(url: str, dest: Path, on_log=None) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "ignoreerrors": True,
        "outtmpl": str(dest / "%(title)s [%(id)s].%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "MetadataFromField",
                "when": "pre_process",
                "formats": [
                    "%(playlist_autonumber)s:%(track_number)s",
                    "%(playlist_autonumber)s:%(track)s",
                    "%(n_entries)s:%(track_total)s",
                ],
            },
            {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "remote_components": ["ejs:github"],
        "js_runtimes": {"deno": {"path": find_deno()}},
        "logger": _YtdlpLogger(on_log),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return ordered_audio_files(info or {}, dest)


def ordered_audio_files(info: dict, dest: Path) -> list[Path]:
    entries = info.get("entries")
    if not entries:
        entries = [info]
    files: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        path = _entry_audio_path(entry, dest)
        if path is None or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _entry_audio_path(entry: dict | None, dest: Path) -> Path | None:
    if not entry:
        return None
    candidates: list[Path] = []
    for download in entry.get("requested_downloads") or []:
        for key in ("filepath", "filename"):
            if download.get(key):
                candidates.append(Path(download[key]))
    for key in ("filepath", "filename", "_filename"):
        if entry.get(key):
            candidates.append(Path(entry[key]))
    for path in candidates:
        if path.suffix.lower() not in AUDIO_EXTS:
            path = path.with_suffix(".flac")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            return path
    video_id = entry.get("id")
    if not video_id or not dest.is_dir():
        return None
    marker = f"[{video_id}]"
    for path in dest.iterdir():
        if path.suffix.lower() in AUDIO_EXTS and marker in path.name:
            return path
    return None


def _tag_text(audio, *keys: str) -> str | None:
    tags = getattr(audio, "tags", None) or audio
    for key in keys:
        values = tags.get(key) if hasattr(tags, "get") else None
        if not values:
            continue
        text = str(values[0] if isinstance(values, list) else values).strip()
        if text:
            return text
    return None


def clean_albumartist(name: str) -> str:
    name = name.split(",")[0].strip()
    if name.endswith(" - Topic"):
        name = name[: -len(" - Topic")].strip()
    return name


def infer_albumartist(files: list[Path], explicit: str | None) -> str | None:
    if explicit:
        return clean_albumartist(explicit)
    if not files:
        return None
    audio = MutagenFile(files[0])
    if audio is None:
        return None
    raw = _tag_text(audio, "albumartist", "ALBUM ARTIST", "artist")
    return clean_albumartist(raw) if raw else None


def stamp_release_tags(files: list[Path], release: Release, albumartist: str | None) -> None:
    total = len(files)
    for index, path in enumerate(files, start=1):
        audio = MutagenFile(path)
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()
        for key in list(audio.tags.keys()):
            if key.lower() in TRACK_TAG_KEYS:
                del audio.tags[key]
        audio.tags["tracknumber"] = str(index)
        audio.tags["tracktotal"] = str(total)
        if release.release_type != "single":
            audio.tags["album"] = release.title
            audio.tags["compilation"] = "0"
        if albumartist:
            audio.tags["albumartist"] = albumartist
            audio.tags["albumartists"] = albumartist
            audio.tags["ALBUM ARTIST"] = albumartist
            audio.tags["artist"] = albumartist
            audio.tags["artists"] = albumartist
        audio.tags["albumtype"] = release.release_type
        if release.year:
            audio.tags["date"] = str(release.year)
            audio.tags["year"] = str(release.year)
        audio.save()


def beet_import(dest: Path, release: Release, albumartist: str | None) -> None:
    cmd = ["beet", "import", "-A", "-q", "--quiet-fallback=asis"]
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
        cmd.extend(
            [
                "--set",
                f"albumartist={albumartist}",
                "--set",
                f"artist={albumartist}",
            ]
        )
    if release.release_type != "single":
        cmd.extend(["--set", f"album={release.title}"])
    if release.year:
        cmd.extend(["--set", f"year={release.year}"])
    cmd.append(str(dest))
    result = subprocess.run(
        cmd,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"beet import failed ({result.returncode})")
    if has_audio(dest):
        raise RuntimeError(output or "beet did not import the downloaded files")
    relabel_for_navidrome()


def relabel_for_navidrome() -> None:
    """Beets writes host-labeled files; Navidrome's volume needs container_file_t."""
    if not MUSIC_DIR.is_dir():
        return
    subprocess.run(
        ["chcon", "-R", "--reference", str(MUSIC_DIR), str(MUSIC_DIR)],
        check=False,
        capture_output=True,
    )


def cleanup(dest: Path) -> None:
    for path in dest.iterdir():
        if path.suffix.lower() not in {".flac", ".m4a", ".opus", ".mp3", ".ogg"}:
            path.unlink(missing_ok=True)
    try:
        dest.rmdir()
    except OSError:
        pass


def run_import(query: str, on_event=None) -> dict:
    def emit(event: str, **payload) -> None:
        if on_event:
            on_event(event, payload)

    emit("resolving", query=query)
    albumartist, releases = resolve(query)
    if not releases:
        raise ResolveError("No albums or singles found.")
    emit(
        "resolved",
        artist=albumartist,
        total=len(releases),
        titles=[release.title for release in releases],
    )

    CACHE_DIR.mkdir(exist_ok=True)
    downloaded = 0
    failed = 0
    for index, release in enumerate(releases, start=1):
        emit(
            "release_start",
            index=index,
            total=len(releases),
            title=release.title,
            release_type=release.release_type,
        )
        dest = CACHE_DIR / "incoming"
        if dest.exists():
            shutil.rmtree(dest)
        try:
            files = download(release.url, dest, on_log=lambda msg: emit("log", message=msg))
        except Exception as exc:
            failed += 1
            emit(
                "release_failed",
                index=index,
                title=release.title,
                downloaded=downloaded,
                failed=failed,
                error=str(exc),
            )
            continue
        if not files:
            failed += 1
            emit(
                "release_failed",
                index=index,
                title=release.title,
                downloaded=downloaded,
                failed=failed,
                error="no audio files downloaded",
            )
            continue
        artist = infer_albumartist(files, release.albumartist or albumartist)
        stamp_release_tags(files, release, artist)
        try:
            beet_import(dest, release, artist)
        except Exception as exc:
            failed += 1
            emit(
                "release_failed",
                index=index,
                title=release.title,
                downloaded=downloaded,
                failed=failed,
                error=str(exc),
            )
            continue
        cleanup(dest)
        downloaded += 1
        emit(
            "release_done",
            index=index,
            title=release.title,
            downloaded=downloaded,
            failed=failed,
        )

    summary = {
        "artist": albumartist,
        "total": len(releases),
        "downloaded": downloaded,
        "failed": failed,
    }
    emit("finished", **summary)
    return summary


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: import_youtube.py QUERY_OR_ARTIST_URL")
    query = " ".join(sys.argv[1:])

    def log(event: str, payload: dict) -> None:
        if event == "resolved":
            print(f"Downloading {payload['total']} release(s)", file=sys.stderr)
        elif event == "release_start":
            print(
                f"[{payload['index']}/{payload['total']}] {payload['release_type']} {payload['title']}",
                file=sys.stderr,
            )
        elif event == "release_failed":
            print(f"No audio files downloaded for: {payload['title']}", file=sys.stderr)
            if payload.get("error"):
                print(payload["error"], file=sys.stderr)
        elif event == "log":
            print(payload.get("message", ""), file=sys.stderr)

    try:
        run_import(query, on_event=log)
    except (ResolveError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
