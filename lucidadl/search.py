"""Provider catalogue search — Qobuz and Amazon Music, over plain public HTTP.

Lucida's former /search frontend route is gone (404), and it was always the fragile
part of the pipeline: a SvelteKit page blob that changed shape whenever the site
redesigned. Discovery now talks to the catalogues directly and lucida.to is used only
for what it is good at — turning a provider item URL into audio.

Mechanisms (both login-free, no browser, no private app secrets):
  Qobuz   www.qobuz.com/api.json/0.2/catalog/search with the web player's public
          X-App-Id — the same open endpoint family the playlist importer already uses
          (api.qobuz_tracklist). Unofficial but stable and long-lived.
  Amazon  Amazon has no public catalogue API reachable without a browser session (the
          web player generates its session client-side). The retail digital-music
          search results page (www.amazon.com/s?i=digital-music) is still served to
          plain HTTP clients, so it is parsed for candidate ASINs. That page is an
          unofficial surface: its shape is pinned by fixtures and any change raises a
          visible SearchError rather than silently returning nothing. Album results
          are verified against music.amazon.com/albums/<ASIN> before being offered.

Result items keep the shape the resolver and matcher have always consumed
({url, title, artist, album, context}) plus optional richer keys (year, track_count,
version) that the matcher and the interactive listing may ignore or use.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from .api import QOBUZ_APP_ID, LucidaError, _plain_html_text, _public_get


class SearchError(LucidaError):
    """A catalogue search could not be performed (blocked, changed shape, or the
    service is not searchable). Deliberately distinct from 'no results', which is an
    empty result set, not an error."""


# Amazon's retail pages only answer lightweight clients with a full browser UA; the
# neutral "Mozilla/5.0" used for playlist sources receives a reduced page.
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_GOOGLEBOT_UA = "Googlebot"


# --- Qobuz -------------------------------------------------------------------

async def qobuz_search(query: str, limit: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """Search Qobuz's public catalogue. Empty query results are an empty set, never an
    error, so the resolver's fallback chain can move on to Amazon."""
    import json

    url = ("https://www.qobuz.com/api.json/0.2/catalog/search"
           f"?query={quote_plus(query)}&limit={limit}")
    response = await _public_get(url, {
        "X-App-Id": QOBUZ_APP_ID, "Referer": "https://open.qobuz.com/",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        data = response.json()
    except Exception as exc:
        raise SearchError(f"Qobuz search returned unreadable data: {exc}") from exc
    return _qobuz_items(data)


def _qobuz_items(data: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Normalize a catalog/search payload into the resolver's result contract."""
    out: Dict[str, List[Dict[str, Any]]] = {"tracks": [], "albums": []}
    if not isinstance(data, dict):
        return out
    for album in (data.get("albums") or {}).get("items") or []:
        if not isinstance(album, dict) or not album.get("id"):
            continue
        title = " ".join(str(album.get("title") or "").split())
        version = " ".join(str(album.get("version") or "").split())
        if version and version.casefold() not in title.casefold():
            title = f"{title} ({version})"
        artists = [str(a.get("name") or "").strip() for a in (album.get("artists") or [])
                   if isinstance(a, dict) and a.get("name")]
        primary = album.get("artist") if isinstance(album.get("artist"), dict) else {}
        artist = ", ".join(dict.fromkeys(artists)) or " ".join(str(primary.get("name") or "").split())
        if not title or not artist:
            continue
        slug = album.get("slug") or "album"
        item = {
            "url": f"https://www.qobuz.com/us-en/album/{slug}/{album['id']}",
            "title": title, "artist": artist, "album": "",
            "context": f"{title} {artist}",
            "year": _qobuz_year(album),
            "track_count": album.get("tracks_count") or album.get("media_count") or "",
        }
        out["albums"].append(item)
    for track in (data.get("tracks") or {}).get("items") or []:
        if not isinstance(track, dict) or not track.get("id"):
            continue
        title = " ".join(str(track.get("title") or "").split())
        version = " ".join(str(track.get("version") or "").split())
        if version and version.casefold() not in title.casefold():
            title = f"{title} ({version})"
        performer = track.get("performer") if isinstance(track.get("performer"), dict) else {}
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        album_artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
        artist = (" ".join(str(performer.get("name") or "").split())
                  or " ".join(str(album_artist.get("name") or "").split()))
        album_title = " ".join(str(album.get("title") or "").split())
        if not title or not artist:
            continue
        out["tracks"].append({
            "url": f"https://open.qobuz.com/track/{track['id']}",
            "title": title, "artist": artist, "album": album_title,
            "context": f"{title} {artist} {album_title}".strip(),
            "year": _qobuz_year(track),
            "track_count": "",
        })
    return out


def _qobuz_year(item: Dict[str, Any]) -> str:
    for key in ("release_date_original", "release_date_stream",
                "release_date_download", "released_at", "release_date"):
        value = str(item.get(key) or "")
        if re.match(r"^\d{4}", value):
            return value[:4]
    return ""


# --- Amazon ------------------------------------------------------------------

async def amazon_search(query: str) -> Dict[str, List[Dict[str, Any]]]:
    """Search Amazon Music via the retail digital-music results page (see module
    docstring). Splits 'the search legitimately found nothing' (empty set) from 'the
    page could not be read' (SearchError) so fallback never masks a real outage."""
    response = await _public_get(
        f"https://www.amazon.com/s?k={quote_plus(query)}&i=digital-music",
        {"User-Agent": _CHROME_UA, "Accept-Language": "en-US,en;q=0.8"},
    )
    albums, tracks = _amazon_items(response.text)
    # Album candidates are verified before being offered: a /albums/<ASIN> request for
    # something that is not an album returns Amazon's generic app shell (no <h1>).
    # Verification is bounded to the top candidates, and a check that could not run
    # (network trouble) keeps the candidate rather than silently dropping it.
    verified = []
    for item in albums[:2]:
        if await _album_page_exists(item["url"]) is not False:
            verified.append(item)
    verified.extend(albums[2:])
    return {"tracks": tracks, "albums": verified}


def _amazon_items(body: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse the retail SERP into (albums, tracks). Raises SearchError when the page
    is not a readable results page at all (bot wall, markup change)."""
    if not re.search(r'data-asin="[A-Z0-9]{10}"', body or ""):
        if "no results for" in (body or "").lower():
            return [], []  # a legitimate, recognizable empty results page
        raise SearchError(
            "Amazon search is unavailable (blocked or the page changed) — "
            "try `-s qobuz` or paste a direct music.amazon.com URL")
    albums: List[Dict[str, Any]] = []
    tracks: List[Dict[str, Any]] = []
    spans = [(match.start(), match.group(1))
             for match in re.finditer(r'data-asin="([A-Z0-9]{10})"', body)]
    for position, (start, asin) in enumerate(spans):
        end = spans[position + 1][0] if position + 1 < len(spans) else len(body)
        block = body[start:end]
        heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", block, re.I | re.S)
        title = _plain_html_text(heading.group(1)) if heading else ""
        texts = [t.strip() for t in re.findall(r">([^<>]{2,80})<", block)]
        artist = ""
        for index, text in enumerate(texts):
            if text.lower() == "by" and index + 1 < len(texts):
                artist = texts[index + 1]
                break
        if not title or not artist or artist.startswith(("(", "[", "4.", "5.")):
            continue  # ratings or page furniture, not a music result
        lowered = block.lower()
        item = {
            "asin": asin, "title": title, "artist": artist,
            "year": "", "track_count": "", "version": "",
        }
        if "audio cd" in lowered or "vinyl" in lowered:
            item.update({"url": f"https://music.amazon.com/albums/{asin}",
                         "album": "", "context": f"{title} {artist}"})
            albums.append(item)
        else:
            item.update({"url": f"https://music.amazon.com/tracks/{asin}",
                         "album": "", "context": f"{title} {artist}"})
            tracks.append(item)
    return albums, tracks


async def _album_page_exists(url: str) -> Optional[bool]:
    """True = a real server-rendered album page (has an <h1>). False = Amazon's generic
    app shell, i.e. this ASIN is not an album. None = the check could not run."""
    try:
        response = await _public_get(url, {"User-Agent": _GOOGLEBOT_UA,
                                           "Accept-Language": "en-US,en;q=0.8"})
    except Exception:
        return None
    return bool(re.search(r"<h1\b", response.text or "", re.I))


# --- shared ------------------------------------------------------------------

async def catalog_search(query: str, service: str, log=print) -> Dict[str, List[Dict[str, Any]]]:
    """One query against one provider ('qobuz' or 'amazon'). The 'auto' tier is the
    resolver's fallback loop, not a provider — it lives in downloader._resolve_url."""
    from .api import normalize_service

    svc = normalize_service(service)
    if svc == "qobuz":
        log(f"  searching: {query!r} on qobuz")
        return await qobuz_search(query)
    if svc == "amazon":
        log(f"  searching: {query!r} on amazon")
        return await amazon_search(query)
    raise SearchError(
        f"{service!r} is not searchable — lucida's own /search is gone; "
        "use auto, qobuz or amazon (other services remain playlist sources)")


def merge_entries(qobuz_res: Dict[str, List[Dict[str, Any]]],
                  amazon_res: Dict[str, List[Dict[str, Any]]]
                  ) -> Dict[str, List[Dict[str, Any]]]:
    """Interactive-search merge: Qobuz rows first, every row tagged with its service so
    the CLI/TUI listing can label Amazon-only finds."""
    out: Dict[str, List[Dict[str, Any]]] = {"albums": [], "tracks": []}
    for bucket in ("albums", "tracks"):
        for item in (qobuz_res.get(bucket) or []):
            out[bucket].append({**item, "service": "qobuz"})
        for item in (amazon_res.get(bucket) or []):
            out[bucket].append({**item, "service": "amazon"})
    return out
