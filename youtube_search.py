import re
import sys
from dataclasses import dataclass

from ytmusicapi import YTMusic


class ResolveError(Exception):
    pass


@dataclass
class Release:
    url: str
    title: str
    release_type: str
    year: str | None = None
    albumartist: str | None = None


def _playlist(pid: str) -> str:
    return f"https://www.youtube.com/playlist?list={pid}"


def _type(section: str, raw: str | None) -> str:
    value = (raw or "").lower()
    if value in {"album", "ep", "single"}:
        return value
    return "album" if section == "albums" else "single"


def _artist(item: dict) -> str | None:
    artists = item.get("artists") or []
    if not artists:
        return None
    first = artists[0]
    return first.get("name") if isinstance(first, dict) else first


def _section(yt: YTMusic, artist: dict, key: str) -> list[dict]:
    block = artist.get(key) or {}
    if block.get("browseId") and block.get("params"):
        return yt.get_artist_albums(block["browseId"], block["params"], limit=None)
    return block.get("results") or []


def discography(yt: YTMusic, channel_id: str) -> tuple[str, list[Release]]:
    artist = yt.get_artist(channel_id)
    name = artist.get("name", channel_id)
    releases, seen = [], set()
    for section in ("albums", "singles"):
        for item in _section(yt, artist, section):
            pid = item.get("playlistId") or item.get("audioPlaylistId")
            browse_id = item.get("browseId") or ""
            if not pid and browse_id.startswith("MPRE"):
                pid = yt.get_album(browse_id).get("audioPlaylistId")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            releases.append(
                Release(
                    _playlist(pid),
                    item.get("title") or pid,
                    _type(section, item.get("type")),
                    item.get("year"),
                    name,
                )
            )
    return name, releases


def resolve(query: str) -> tuple[str | None, list[Release]]:
    yt = YTMusic()
    if match := re.search(r"youtube\.com/channel/(UC[\w-]+)", query):
        name, releases = discography(yt, match.group(1))
        print(f"Artist: {name}", file=sys.stderr)
        return name, releases
    if match := re.search(r"youtube\.com/@([^/?#]+)", query):
        results = yt.search(match.group(1), filter="artists", ignore_spelling=True)
        if not results:
            raise ResolveError(f"No artist found for @{match.group(1)}")
        name, releases = discography(yt, results[0]["browseId"])
        print(f"Artist: {name}", file=sys.stderr)
        return name, releases

    results = yt.search(query, ignore_spelling=True)
    if not results:
        raise ResolveError("No results found.")
    result = results[0]
    title = result.get("title") or query
    year = result.get("year")
    pid = result.get("playlistId")
    artist = _artist(result)
    browse_id = result.get("browseId") or ""
    if browse_id.startswith("MPRE"):
        album = yt.get_album(browse_id)
        title = album.get("title") or title
        year = album.get("year") or year
        pid = album.get("audioPlaylistId") or pid
        artist = _artist(album) or artist
    if pid:
        kind = _type("albums", result.get("type") or result.get("resultType"))
        return artist, [Release(_playlist(pid), title, kind, year, artist)]
    return artist, [Release(f"https://www.youtube.com/watch?v={result['videoId']}", title, "single", year, artist)]


if __name__ == "__main__":
    albumartist, releases = resolve(" ".join(sys.argv[1:]))
    if albumartist:
        print(f"albumartist:{albumartist}")
    for release in releases:
        print(f"{release.release_type}\t{release.year or ''}\t{release.title}\t{release.url}")
