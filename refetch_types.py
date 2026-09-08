#!/usr/bin/env python3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from beets import config, util
from beets.library import Library

from import_youtube import MUSIC, write_albumtype
from youtube_search import artist_release_types, lookup_release_type, match_release_type


def _lib() -> Library:
    config.read()
    return Library(config["library"].as_path(), config["directory"].as_filename())


def _path(item) -> Path:
    return Path(util.displayable_path(item.path))


def _releases(lib: Library, needle: str):
    needle = needle.casefold()
    out = []
    for album in lib.albums():
        if needle and needle not in (album.albumartist or "").casefold() and needle not in (album.album or "").casefold():
            continue
        out.append((album, album.albumartist, album.album, album.albumtype or "", list(album.items())))
    groups = defaultdict(list)
    for item in lib.items("singleton:true"):
        if needle and needle not in (item.albumartist or "").casefold() and needle not in (item.album or item.title or "").casefold():
            continue
        groups[(item.albumartist, item.album or item.title)].append(item)
    for (artist, title), items in groups.items():
        out.append((None, artist, title, items[0].albumtype or "single", items))
    return out


def _apply(lib: Library, album, items, kind: str) -> None:
    if album is None and kind in {"album", "ep"}:
        album = lib.add_album(items)
    if album is not None:
        album.albumtype = kind
        album.store()
        items = list(album.items())
    for item in items:
        item.albumtype = kind
        item.store()
        path = _path(item)
        if path.is_file():
            write_albumtype(path, kind)
        else:
            print(f"missing\t{path}", file=sys.stderr)
    present = [item for item in items if _path(item).is_file()]
    if album is not None and present and len(present) == len(items):
        album.move()
    else:
        for item in present:
            item.move()


def main() -> None:
    dry = "--dry-run" in sys.argv[1:]
    needle = " ".join(a for a in sys.argv[1:] if a != "--dry-run")
    lib = _lib()
    cache: dict[str, dict[str, str]] = {}
    found = skipped = changed = 0
    for album, artist, title, current, items in _releases(lib, needle):
        found += 1
        if artist not in cache:
            cache[artist] = artist_release_types(artist)
        new = match_release_type(title, cache[artist]) or lookup_release_type(artist, title)
        if not new:
            skipped += 1
            print(f"skip\t{current or '?'}\t{artist}\t{title}")
            continue
        if new != current:
            changed += 1
        print(f"{current or '?'}->{new}\t{artist}\t{title}")
        if not dry:
            _apply(lib, album, items, new)
    if not dry:
        subprocess.run(["chcon", "-R", "--reference", str(MUSIC), str(MUSIC)], capture_output=True)
    print(f"{found} releases, {changed} type changes, {skipped} unmatched", file=sys.stderr)
    if dry:
        print("dry run, nothing written", file=sys.stderr)


if __name__ == "__main__":
    main()
