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
    value = re.sub(r"[-_]+", " ", (raw or "").lower()).strip()
    value = " ".join(value.split())
    if value in {"album", "lp"}:
        return "album"
    if value in {"ep", "mini album", "minialbum"}:
        return "ep"
    if value in {"single", "singiel"}:
        return "single"
    return "album" if section == "albums" else "single"


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


_ROLE = re.compile(r"\s*(?:,\s*)?(?:&|and|feat\.?|ft\.?)\s+", re.I)
_TITLE_FEAT = re.compile(r"\((?:feat\.?|ft\.?)\s+([^)]+)\)", re.I)


def _label(item: dict | str | None) -> str | None:
    if isinstance(item, dict):
        return item.get("name")
    return item


def _parts(name: str, albumartist: str | None = None) -> list[str]:
    listed = bool(_ROLE.search(name))
    if not listed and albumartist and name.casefold().startswith(albumartist.casefold() + ","):
        listed = True
    if not listed:
        return [name]
    out = []
    for chunk in _ROLE.split(name):
        out.extend(p.strip() for p in chunk.split(",") if p.strip())
    return out


def primary_first(names: list[str], albumartist: str | None = None) -> list[str]:
    if not names or not albumartist:
        return list(names)
    key = albumartist.casefold()
    main = next((n for n in names if n.casefold() == key), None)
    rest = [n for n in names if n.casefold() != key]
    return ([main] if main else []) + rest


def _names(item: dict, albumartist: str | None = None) -> list[str]:
    seen, names = set(), []
    for artist in item.get("artists") or []:
        label = _label(artist)
        if not label:
            continue
        for name in _parts(label, albumartist):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
    if match := _TITLE_FEAT.search(item.get("title") or ""):
        for name in _parts(match.group(1), albumartist):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
    return primary_first(names, albumartist)


def artist_credit(names: list[str], albumartist: str | None = None) -> str:
    if not names:
        return albumartist or ""
    main = next((n for n in names if albumartist and n.casefold() == albumartist.casefold()), names[0])
    rest = [n for n in names if n.casefold() != main.casefold()]
    if not rest:
        return main
    featured = rest[0] if len(rest) == 1 else f"{', '.join(rest[:-1])} & {rest[-1]}"
    return f"{main} feat. {featured}"


def _artist(item: dict) -> str | None:
    names = _names(item)
    return names[0] if names else None


def track_artists(url: str, albumartist: str | None = None) -> dict[str, list[str]]:
    try:
        yt = YTMusic()
        tracks: list[dict] = []
        if match := re.search(r"[?&]list=([\w-]+)", url):
            pid = match.group(1)
            try:
                tracks = yt.get_playlist(pid, limit=None).get("tracks") or []
            except Exception:
                tracks = []
            if not tracks and pid.startswith("OLAK5uy_"):
                browse = yt.get_album_browse_id(pid)
                if browse:
                    tracks = yt.get_album(browse).get("tracks") or []
        elif match := re.search(r"(?:youtu\.be/|v=)([\w-]{11})", url):
            details = yt.get_song(match.group(1)).get("videoDetails") or {}
            author = details.get("author")
            tracks = [
                {
                    "videoId": match.group(1),
                    "title": details.get("title"),
                    "artists": [{"name": author}] if author else [],
                }
            ]
        out = {}
        for track in tracks:
            vid = track.get("videoId")
            names = _names(track, albumartist)
            if vid and names:
                out[vid] = names
        return out
    except Exception:
        return {}


def _section(yt: YTMusic, artist: dict, key: str) -> list[dict]:
    block = artist.get(key) or {}
    if block.get("browseId") and block.get("params"):
        return yt.get_artist_albums(block["browseId"], block["params"], limit=None)
    return block.get("results") or []


def _channel_from_handle(yt: YTMusic, handle: str) -> str | None:
    page = yt._send_get_request(f"https://www.youtube.com/@{handle}").text
    match = re.search(
        r'(?:rel="canonical" href="https://www\.youtube\.com/channel/|itemprop="identifier" content=")(UC[\w-]+)',
        page,
    )
    return match.group(1) if match else None


def discography(yt: YTMusic, channel_id: str) -> tuple[str, list[Release]]:
    artist = yt.get_artist(channel_id)
    name = artist.get("name", channel_id)
    releases, seen = [], set()
    for section in ("albums", "singles"):
        for item in _section(yt, artist, section):
            pid = item.get("playlistId") or item.get("audioPlaylistId")
            browse_id = item.get("browseId") or ""
            album = None
            if not pid and browse_id.startswith("MPRE"):
                album = yt.get_album(browse_id)
                pid = album.get("audioPlaylistId")
            raw_type = item.get("type")
            if not raw_type and browse_id.startswith("MPRE"):
                if album is None:
                    album = yt.get_album(browse_id)
                    pid = pid or album.get("audioPlaylistId")
                raw_type = album.get("type")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            releases.append(
                Release(
                    _playlist(pid),
                    item.get("title") or pid,
                    _type(section, raw_type),
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
        handle = match.group(1)
        channel_id = _channel_from_handle(yt, handle)
        if not channel_id:
            results = yt.search(handle, filter="artists", ignore_spelling=True)
            if not results:
                raise ResolveError(f"No artist found for @{handle}")
            channel_id = results[0]["browseId"]
        name, releases = discography(yt, channel_id)
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
    raw_type = result.get("type")
    if browse_id.startswith("MPRE"):
        album = yt.get_album(browse_id)
        title = album.get("title") or title
        year = album.get("year") or year
        pid = album.get("audioPlaylistId") or pid
        artist = _artist(album) or artist
        raw_type = album.get("type") or raw_type
    if pid:
        kind = _type("albums", raw_type)
        return artist, [Release(_playlist(pid), title, kind, year, artist)]
    return artist, [Release(f"https://www.youtube.com/watch?v={result['videoId']}", title, "single", year, artist)]


def lookup_release_type(albumartist: str, title: str) -> str | None:
    yt = YTMusic()
    results = yt.search(f"{albumartist} {title}", filter="albums", ignore_spelling=True) or []
    want_title, want_artist = _norm(title), _norm(albumartist)
    match = None
    for result in results:
        if _norm(result.get("title") or "") != want_title:
            continue
        artists = _names(result)
        if artists and want_artist and not any(want_artist in _norm(name) for name in artists):
            continue
        match = result
        break
    if not match and results and _norm(results[0].get("title") or "") == want_title:
        match = results[0]
    if not match:
        return None
    raw = match.get("type")
    browse_id = match.get("browseId") or ""
    if browse_id.startswith("MPRE"):
        try:
            raw = yt.get_album(browse_id).get("type") or raw
        except Exception:
            pass
    return _type("albums", raw)


def artist_release_types(artist: str) -> dict[str, str]:
    yt = YTMusic()
    results = yt.search(artist, filter="artists", ignore_spelling=True) or []
    if not results:
        return {}
    _, releases = discography(yt, results[0]["browseId"])
    return {_norm(release.title): release.release_type for release in releases}


def _base(value: str) -> str:
    return re.split(r"feat", _norm(value), 1)[0] or _norm(value)


def match_release_type(title: str, types: dict[str, str]) -> str | None:
    key = _norm(title)
    if key in types:
        return types[key]
    base = _base(title)
    hits = [kind for name, kind in types.items() if name == key or _base(name) == base]
    return hits[0] if len(set(hits)) == 1 else None


if __name__ == "__main__":
    albumartist, releases = resolve(" ".join(sys.argv[1:]))
    if albumartist:
        print(f"albumartist:{albumartist}")
    for release in releases:
        print(f"{release.release_type}\t{release.year or ''}\t{release.title}\t{release.url}")
