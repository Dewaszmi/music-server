import argparse
import re
import sys
from dataclasses import dataclass

from ytmusicapi import YTMusic

CHANNEL_RE = re.compile(r"(?:music\.)?youtube\.com/channel/(UC[\w-]+)")
HANDLE_RE = re.compile(r"(?:music\.)?youtube\.com/@([^/?#]+)")


class ResolveError(Exception):
    pass


@dataclass
class Release:
    url: str
    title: str
    release_type: str  # album, ep, single
    year: str | None = None
    albumartist: str | None = None


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def normalize_type(section: str, raw_type: str | None) -> str:
    value = (raw_type or "").strip().lower()
    if value in {"album", "ep", "single"}:
        return value
    return "album" if section == "albums" else "single"


def _playlist_id(yt: YTMusic, item: dict) -> str | None:
    pid = item.get("playlistId") or item.get("audioPlaylistId")
    browse_id = item.get("browseId") or ""
    if not pid and browse_id.startswith("MPRE"):
        pid = yt.get_album(browse_id).get("audioPlaylistId")
    return pid


def _section_items(yt: YTMusic, artist: dict, key: str) -> list[dict]:
    block = artist.get(key) or {}
    if block.get("browseId") and block.get("params"):
        return yt.get_artist_albums(block["browseId"], block["params"], limit=None)
    return block.get("results") or []


def discography(yt: YTMusic, channel_id: str) -> tuple[str, list[Release]]:
    artist = yt.get_artist(channel_id)
    name = artist.get("name", channel_id)
    releases, seen = [], set()
    for section in ("albums", "singles"):
        for item in _section_items(yt, artist, section):
            pid = _playlist_id(yt, item)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            title = item.get("title") or pid
            releases.append(
                Release(
                    url=playlist_url(pid),
                    title=title,
                    release_type=normalize_type(section, item.get("type")),
                    year=item.get("year"),
                    albumartist=name,
                )
            )
    return name, releases


def resolve_channel_id(yt: YTMusic, query: str) -> str | None:
    match = CHANNEL_RE.search(query)
    if match:
        return match.group(1)
    match = HANDLE_RE.search(query)
    if not match:
        return None
    handle = match.group(1)
    results = yt.search(handle, filter="artists", ignore_spelling=True)
    if not results:
        raise ResolveError(f"No artist found for @{handle}")
    print(f"Resolved @{handle} -> {results[0].get('artist', results[0].get('browseId'))}", file=sys.stderr)
    return results[0]["browseId"]


def _primary_artist(item: dict | None) -> str | None:
    if not item:
        return None
    artists = item.get("artists") or []
    if artists:
        first = artists[0]
        if isinstance(first, dict) and first.get("name"):
            return first["name"]
        if isinstance(first, str) and first.strip():
            return first.strip()
    for key in ("artist", "author"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def search_release(yt: YTMusic, query: str) -> Release:
    results = yt.search(query, ignore_spelling=True)
    print(f"Found {len(results)} elements", file=sys.stderr)
    if not results:
        raise ResolveError("No results found.")
    result = results[0]
    raw_type = result.get("type") or result.get("resultType")
    title = result.get("title") or query
    year = result.get("year")
    playlist_id = result.get("playlistId")
    albumartist = _primary_artist(result)
    browse_id = result.get("browseId") or ""
    if browse_id.startswith("MPRE"):
        album = yt.get_album(browse_id)
        title = album.get("title") or title
        year = album.get("year") or year
        playlist_id = album.get("audioPlaylistId") or playlist_id
        albumartist = _primary_artist(album) or albumartist
    if playlist_id:
        url = playlist_url(playlist_id)
        release_type = normalize_type("albums", raw_type)
    else:
        url = f"https://www.youtube.com/watch?v={result['videoId']}"
        release_type = "single"
    return Release(
        url=url,
        title=title,
        release_type=release_type,
        year=year,
        albumartist=albumartist,
    )


def resolve(query: str) -> tuple[str | None, list[Release]]:
    yt = YTMusic()
    channel_id = resolve_channel_id(yt, query)
    if channel_id:
        name, releases = discography(yt, channel_id)
        print(f"Artist: {name}", file=sys.stderr)
        return name, releases
    release = search_release(yt, query)
    return release.albumartist, [release]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="search query or YouTube Music artist URL")
    args = parser.parse_args()
    try:
        albumartist, releases = resolve(args.query)
    except ResolveError as exc:
        raise SystemExit(str(exc)) from exc
    if not releases:
        raise SystemExit("No albums or singles found.")
    print(f"{len(releases)} release(s)", file=sys.stderr)
    if albumartist:
        print(f"albumartist:{albumartist}")
    for release in releases:
        print(f"{release.release_type}\t{release.year or ''}\t{release.title}\t{release.url}")


if __name__ == "__main__":
    main()
