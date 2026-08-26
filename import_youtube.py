#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yt_dlp
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

from youtube_search import Release, ResolveError, resolve

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "yt-cache"
MUSIC = ROOT / "beets-shit"
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


def download(url: str, dest: Path, on_log=None) -> list[Path]:
    dest.mkdir(parents=True)
    opts = {
        "quiet": True,
        "noprogress": True,
        "ignoreerrors": True,
        "outtmpl": str(dest / "%(title)s [%(id)s].%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
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
        if match:
            files.append(match)
    return files


def stamp(files: list[Path], release: Release, artist: str | None) -> None:
    for i, path in enumerate(files, 1):
        audio = MutagenFile(path)
        if isinstance(audio, MP4):
            audio["trkn"] = [(i, len(files))]
            if artist:
                audio["aART"] = [artist]
                audio["\xa9ART"] = [artist]
            if release.release_type != "single":
                audio["\xa9alb"] = [release.title]
        else:
            audio["trkn"] = [(i, len(files))]
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
        cmd += ["--set", f"albumartist={artist}", "--set", f"artist={artist}"]
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
            artist = (release.albumartist or albumartist or "").split(",")[0].strip() or None
            stamp(files, release, artist)
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
