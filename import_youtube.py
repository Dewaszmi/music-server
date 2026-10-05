#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yt_dlp
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

from youtube_search import Release, ResolveError, artist_credit, primary_first, resolve, track_artists

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "yt-cache"
MUSIC = ROOT / "music"
AUDIO = {".m4a", ".mp4", ".aac", ".opus", ".ogg", ".webm", ".mp3", ".flac"}
os.environ["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")


class _Log:
    def __init__(self, fn=None):
        self.fn = fn or (lambda _m: None)

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if "SABR" not in str(msg):
            self.fn(str(msg))

    def error(self, msg):
        self.fn(str(msg))


def _playable(path: Path) -> bool:
    audio = MutagenFile(path)
    return bool(audio and getattr(getattr(audio, "info", None), "length", 0))


def download(url: str, dest: Path, on_log=None) -> list[Path]:
    dest.mkdir(parents=True)
    opts = {
        "quiet": True,
        "noprogress": True,
        "ignoreerrors": True,
        "outtmpl": str(dest / "%(title)s [%(id)s].%(ext)s"),
        # Adaptive audio 403s on a normal GET, and on a second range, but one
        # request covering the whole file succeeds. The android client's only
        # fully-downloadable format for some tracks is a cover-art stub.
        "format": "bestaudio[abr>=32]/best[abr>=32]/bestaudio/best",
        "http_chunk_size": 100 * 1024 * 1024,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "extractor_args": {"youtube": {"player_client": ["web_embedded", "android"]}},
        "remote_components": ["ejs:github"],
        "logger": _Log(on_log),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    files = []
    for entry in info.get("entries") or [info]:
        if not entry:
            continue
        match = next(
            (p for p in dest.iterdir() if p.suffix.lower() in AUDIO and f"[{entry['id']}]" in p.name),
            None,
        )
        if not match:
            continue
        if not _playable(match):
            match.unlink(missing_ok=True)
            if on_log:
                on_log(f"Skipped {entry.get('title') or entry['id']}: downloaded file has no audio")
            continue
        files.append(match)
    return files


def _video_id(path: Path) -> str | None:
    match = re.search(r"\[([^\[\]]+)\]", path.stem)
    return match.group(1) if match else None


def write_albumtype(path: Path, kind: str) -> None:
    if not path.is_file():
        return
    audio = MutagenFile(path)
    if audio is None:
        return
    if isinstance(audio, MP4):
        audio["----:com.apple.iTunes:MusicBrainz Album Type"] = [kind.encode("utf-8")]
    else:
        audio["MUSICBRAINZ_ALBUMTYPE"] = [kind]
        audio["RELEASETYPE"] = [kind]
    audio.save()


def stamp(files: list[Path], release: Release, artist: str | None, guests: dict[str, list[str]] | None = None) -> None:
    guests = guests or {}
    album = release.title if release.release_type != "single" else None
    for i, path in enumerate(files, 1):
        audio = MutagenFile(path)
        names = primary_first(guests.get(_video_id(path) or "") or ([artist] if artist else []), artist)
        display = artist_credit(names, artist)
        if isinstance(audio, MP4):
            audio["trkn"] = [(i, len(files))]
            if album:
                audio["\xa9alb"] = [album]
            if artist:
                audio["aART"] = [artist]
                audio["----:com.apple.iTunes:ALBUMARTISTS"] = [artist.encode("utf-8")]
            if display:
                audio["\xa9ART"] = [display]
            if names:
                audio["----:com.apple.iTunes:ARTISTS"] = [n.encode("utf-8") for n in names]
            audio["----:com.apple.iTunes:MusicBrainz Album Type"] = [release.release_type.encode("utf-8")]
        else:
            audio["tracknumber"] = str(i)
            audio["tracktotal"] = str(len(files))
            if album:
                audio["album"] = album
            if artist:
                audio["albumartist"] = artist
                audio["albumartists"] = [artist]
            if display:
                audio["artist"] = display
            if names:
                audio["artists"] = names
            audio["MUSICBRAINZ_ALBUMTYPE"] = [release.release_type]
            audio["RELEASETYPE"] = [release.release_type]
        audio.save()


def beet_import(dest: Path, release: Release, artist: str | None) -> None:
    cmd = [
        "beet", "import", "-A", "-q", "--quiet-fallback=asis",
        "--set", f"albumtype={release.release_type}",
        "--set", "comp=0",
    ]
    if release.release_type == "single":
        cmd.append("-s")
    if artist:
        cmd += ["--set", f"albumartist={artist}"]
    if release.release_type != "single":
        cmd += ["--set", f"album={release.title}"]
    if release.year:
        cmd += ["--set", f"year={release.year}"]
    result = subprocess.run(cmd + [str(dest)], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    err = ((result.stdout or "") + (result.stderr or "")).strip()
    leftover = any(p.suffix.lower() in AUDIO for p in dest.rglob("*") if p.is_file())
    if result.returncode or leftover:
        raise RuntimeError(err or "beet import failed")
    subprocess.run(["chcon", "-R", "--reference", str(MUSIC), str(MUSIC)], capture_output=True)


def run_import(query: str, on_event=None) -> dict:
    def emit(event, **payload):
        if on_event:
            on_event(event, payload)

    emit("resolving", query=query)
    albumartist, releases = resolve(query)
    if not releases:
        raise ResolveError("No albums or singles found.")
    emit("resolved", artist=albumartist, total=len(releases), titles=[r.title for r in releases])

    downloaded = failed = 0
    for i, release in enumerate(releases, 1):
        emit("release_start", index=i, total=len(releases), title=release.title, release_type=release.release_type)
        dest = CACHE / "incoming"
        shutil.rmtree(dest, ignore_errors=True)
        try:
            files = download(release.url, dest, on_log=lambda m: emit("log", message=m))
            if not files:
                raise RuntimeError("no audio files downloaded")
            artist = release.albumartist or albumartist
            stamp(files, release, artist, track_artists(release.url, artist))
            beet_import(dest, release, artist)
        except Exception as exc:
            failed += 1
            emit("release_failed", index=i, title=release.title, downloaded=downloaded, failed=failed, error=str(exc))
            continue
        shutil.rmtree(dest, ignore_errors=True)
        downloaded += 1
        emit("release_done", index=i, title=release.title, downloaded=downloaded, failed=failed)

    summary = {"artist": albumartist, "total": len(releases), "downloaded": downloaded, "failed": failed}
    emit("finished", **summary)
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: import_youtube.py QUERY_OR_ARTIST_URL")

    def log(event, payload):
        if event == "resolved":
            print(f"Downloading {payload['total']} release(s)", file=sys.stderr)
        elif event == "release_start":
            print(f"[{payload['index']}/{payload['total']}] {payload['release_type']} {payload['title']}", file=sys.stderr)
        elif event == "release_failed":
            print(f"Failed: {payload['title']}\n{payload.get('error', '')}", file=sys.stderr)
        elif event == "log":
            print(payload.get("message", ""), file=sys.stderr)

    try:
        run_import(" ".join(sys.argv[1:]), on_event=log)
    except (ResolveError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
