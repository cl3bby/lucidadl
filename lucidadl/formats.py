"""User-configurable file/folder name templates.

Opt-in per slot: every unset slot keeps the built-in ``Artists/<Artist>/<Album>/``
behavior, so nothing changes until a slot is explicitly configured (config.json
``"formats": {slot: template}`` plus a global ``"zfill"`` toggle). Templates use
``{variable}`` placeholders rendered from the download's metadata; ``/`` splits a
folder template into nested path segments, each sanitized like any other folder name.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional

from . import utils

# The configurable slots and their suggested shapes (shown by --format-show and the
# TUI; nothing is written to config unless the user sets a slot themselves).
SLOTS = ("album_folder", "ep_folder", "single_folder", "track_folder", "track_file",
         "playlist_folder", "playlist_file")

SLOT_SUGGESTIONS = {
    "album_folder": "{artist}/Albums/{release_year} - {name}",
    "ep_folder": "{artist}/EPs/{release_year} - {name}",
    "single_folder": "{artist}/Singles/{release_year} - {name}",
    "track_folder": "{artist}",
    "track_file": "{track_number} - {name}",
    "playlist_folder": "Playlists/{name}",
    "playlist_file": "{track_number} - {artist} - {name}",
}

# Release-kind -> which folder slot names its destination (files always use track_file).
KIND_DIR_SLOT = {"album": "album_folder", "ep": "ep_folder", "single": "single_folder"}

# Variables a template may reference. Unknown names are dropped from the output with a
# one-time warning; missing values render as '' (identity variables fall back to
# 'Unknown …' — see _FALLBACKS).
VARIABLES = ("artist", "artist_id", "artist_initials", "album_artist", "name", "album",
             "track_number", "total_tracks", "disc_number", "total_discs",
             "playlist_position", "id", "album_id", "label", "catalog_number", "isrc",
             "upc", "release_year", "explicit", "quality", "creator", "creator_id",
             "tracks")

_FALLBACKS = {
    "artist": "Unknown Artist",
    "album_artist": "Unknown Artist",
    "name": "Unknown Name",
    "album": "Unknown Album",
    "release_year": "Unknown Year",
}

_VAR = re.compile(r"\{([a-z_]+)\}")
_FRACTION = re.compile(r"^(\d+)\s*/\s*\d+$")
_warned_vars: set = set()


# --- release kind ------------------------------------------------------------

def release_kind(n_tracks: int, durations_ms: Optional[List[int]] = None) -> Optional[str]:
    """Classify a release from its track count (and per-track durations when known):
    album when it is substantial (>= 7 tracks or over 30 minutes), single when it is
    small and short (<= 3 tracks, under 30 minutes, no 10-minute track), EP otherwise.
    Returns None for an empty release."""
    if not n_tracks or n_tracks < 1:
        return None
    if durations_ms:
        total_s = sum(d for d in durations_ms if isinstance(d, (int, float))) / 1000
        longest_s = max((d for d in durations_ms if isinstance(d, (int, float))),
                        default=0) / 1000
        if n_tracks >= 7 or total_s > 30 * 60:
            return "album"
        if n_tracks <= 3 and total_s < 30 * 60 and longest_s < 10 * 60:
            return "single"
        return "ep"
    if n_tracks >= 7:
        return "album"
    if n_tracks <= 3:
        return "single"
    return "ep"


def _first(*values) -> str:
    """First non-empty value, as a stripped string ('' when nothing is usable)."""
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, (dict, list)):
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _first_key(source: Dict, *keys) -> str:
    """First non-empty value among alternative spellings of one field. lucida copies
    the source service's JSON verbatim and the casing differs (Qobuz uses camelCase
    like releaseDate/trackNumber; other services may use snake_case), so every lookup
    is defensive."""
    return _first(*(source.get(k) for k in keys if isinstance(source, dict)))


def kind_for_info(info: Dict) -> Optional[str]:
    """Release kind for a lucida page-data `info` dict. Only album-type pages are
    classified; a standalone track is not a 'release' (None)."""
    if not isinstance(info, dict) or info.get("type") != "album":
        return None
    tracks = info.get("tracks") or []
    n = (_first_key(info, "trackCount", "track_count", "numberOfTracks")
         or (str(len(tracks)) if tracks else ""))
    try:
        n_tracks = int(float(n))
    except (TypeError, ValueError):
        n_tracks = len(tracks)
    durations = []
    if isinstance(tracks, list):
        for t in tracks:
            if isinstance(t, dict):
                d = _first_key(t, "durationMs", "duration_ms", "duration")
                try:
                    durations.append(float(d))
                except (TypeError, ValueError):
                    pass
    return release_kind(n_tracks, durations or None)


# --- value assembly ------------------------------------------------------------

def _bare_int(value) -> Optional[int]:
    """The leading number of a tag value; understands '6', ' 06 ', and '6/12'."""
    s = _first(value)
    m = _FRACTION.match(s)
    if m:
        s = m.group(1)
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


def _artist_names(source: Dict) -> str:
    """'A, B' from a lucida artists list of {name} dicts ('' when unusable)."""
    artists = source.get("artists") if isinstance(source, dict) else None
    if isinstance(artists, list):
        return ", ".join(str(a.get("name") or "").strip() for a in artists
                         if isinstance(a, dict) and a.get("name"))
    return ""


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(w[0].upper() for w in words[:8])


def assemble_values(*, collection: str = "", track_no: str = "",
                    meta: Optional[Dict] = None, tags: Optional[Dict] = None,
                    kind: Optional[str] = None, zfill: bool = True) -> Dict[str, str]:
    """Every template variable, best-effort. Priority per variable: the download's own
    API metadata (`meta`, built by downloader._track_meta from the raw service JSON)
    wins, embedded `tags` fill the blanks, then friendly fallbacks for identity
    variables and '' for decorative ones. `kind`/`collection`/`track_no` add the
    playlist/release context. With `zfill`, track_number and playlist_position are
    padded to the release's total width (at least 2)."""
    meta = meta or {}
    tags = tags or {}

    artist = _first(meta.get("artist"), tags.get("artist"), _FALLBACKS["artist"])
    album_artist = _first(meta.get("albumartist"), meta.get("artist"),
                          tags.get("albumartist"), tags.get("artist"),
                          _FALLBACKS["album_artist"])
    album = _first(meta.get("album"), tags.get("album"), _FALLBACKS["album"])
    # year_of accepts a bare year or a full date (API releaseDate / tag date forms)
    year = _first(utils.year_of(meta.get("year")), utils.year_of(tags.get("year")),
                  utils.year_of(tags.get("originaldate")),
                  utils.year_of(tags.get("date")), _FALLBACKS["release_year"])

    position = _bare_int(track_no)
    # A playlist track's number IS its position: the tag/meta track_number (from the
    # source album) is preferred, but playlist placement falls back to track_no so the
    # default '{track_number} - …' playlist template keeps working.
    track_number = (_bare_int(_first(meta.get("track_number"), tags.get("tracknumber")))
                    or position)
    total_tracks = _bare_int(_first(meta.get("total_tracks"), tags.get("totaltracks"),
                                    tags.get("tracktotal")))
    disc_number = _bare_int(_first(meta.get("disc_number"), tags.get("discnumber")))
    total_discs = _bare_int(_first(meta.get("total_discs"), tags.get("totaldiscs"),
                                   tags.get("disctotal")))

    # zero-pad positions so files sort by number instead of alphabetically ('10' <
    # '2'); the width covers the release's real total, minimum two digits.
    if zfill:
        width = max(2, len(str(total_tracks or position or "")) or 2)
        track_number_s = f"{track_number:0{width}d}" if track_number is not None else ""
        position_s = f"{position:0{width}d}" if position is not None else _first(track_no)
        disc_s = f"{disc_number:02d}" if disc_number is not None else ""
    else:
        track_number_s = str(track_number) if track_number is not None else ""
        position_s = _first(track_no)
        disc_s = str(disc_number) if disc_number is not None else ""

    values = {
        "artist": artist,
        "artist_id": _first(meta.get("artist_id"), tags.get("artist_id")),
        "artist_initials": _initials(
            _first(meta.get("artist"), tags.get("artist"))),
        "album_artist": album_artist,
        "name": _first(meta.get("title"), tags.get("title")),
        "album": album,
        "track_number": track_number_s,
        "total_tracks": str(total_tracks) if total_tracks else "",
        "disc_number": disc_s,
        "total_discs": str(total_discs) if total_discs else "",
        "playlist_position": position_s,
        "id": _first(meta.get("track_id"), tags.get("id")),
        "album_id": _first(meta.get("album_id"), tags.get("album_id")),
        "label": _first(meta.get("label"), tags.get("label")),
        "catalog_number": _first(meta.get("catalog_number"), meta.get("upc"),
                                 tags.get("catalognumber")),
        "isrc": _first(meta.get("isrc"), tags.get("isrc")),
        "upc": _first(meta.get("upc"), tags.get("upc")),
        "release_year": year,
        "explicit": "Explicit" if _first(meta.get("explicit"),
                                         tags.get("explicit")).lower() in
                    ("explicit", "true", "1", "yes") else "",
        "quality": _first(meta.get("quality"), tags.get("quality")),
        "creator": _first(meta.get("creator"), meta.get("artist")),
        "creator_id": _first(meta.get("creator_id"), meta.get("artist_id")),
        "tracks": str(total_tracks) if total_tracks else "",
    }
    return values


# --- rendering -----------------------------------------------------------------

def any_configured(formats_cfg) -> bool:
    """True when at least one slot has a usable template — i.e. the user opted in and
    the template path should run at all."""
    return isinstance(formats_cfg, dict) and any(
        _first(formats_cfg.get(slot)) for slot in SLOTS)


def render(template: str, values: Dict[str, str], source: str = "template") -> str:
    """Substitute {variables}; unknown names are dropped (with a one-time warning) so
    a typo can never wedge a download, and missing values render empty (identity
    variables were already given 'Unknown …' fallbacks in assemble_values)."""
    out: List[str] = []
    for chunk, var in _walk(template):
        if var is None:
            out.append(chunk)
            continue
        if var not in VARIABLES:
            _warn_unknown(var, source)
            continue
        out.append(str(values.get(var, "") or ""))
    return "".join(out)


def _walk(template: str):
    """Yield (literal, var) pairs: literal text chunks with the following {var} (None
    for a chunk that is the trailing literal)."""
    pos = 0
    for m in _VAR.finditer(template):
        if m.start() > pos:
            yield template[pos:m.start()], None
        yield None, m.group(1)
        pos = m.end()
    if pos < len(template):
        yield template[pos:], None


def _warn_unknown(var: str, source: str) -> None:
    if var in _warned_vars:
        return
    _warned_vars.add(var)
    sys.stderr.write(
        f"⚠ format template ({source}): unknown variable {{{var}}} — removed from the "
        f"output. See `lucida config --format-show` for the available names.\n")


def path_segments(rendered: str, fallback: str = "untitled") -> List[str]:
    """A rendered folder template as sanitized path segments ('/' splits)."""
    segs = [utils.sanitize(seg) for seg in rendered.split("/") if seg.strip()]
    return segs or [fallback]


def target_paths(music_root: str, formats_cfg: Optional[Dict], values: Dict[str, str],
                 kind: Optional[str] = None, collection: str = "",
                 ext: str = "") -> Optional[tuple]:
    """The destination directory and file name from the configured templates, or None
    when neither relevant slot is set (caller falls back to the built-in layout).
    Directory templates render against a release/playlist view of the values ({name} =
    album/playlist title), file templates against the track view ({name} = track
    title)."""
    cfg = formats_cfg if isinstance(formats_cfg, dict) else {}
    if collection:
        dir_slot, file_slot = "playlist_folder", "playlist_file"
    elif kind in KIND_DIR_SLOT:
        dir_slot, file_slot = KIND_DIR_SLOT[kind], "track_file"
    else:
        dir_slot, file_slot = "track_folder", "track_file"
    dir_tpl = _first(cfg.get(dir_slot))
    file_tpl = _first(cfg.get(file_slot))
    if not dir_tpl and not file_tpl:
        return None

    if dir_tpl:
        dir_values = dict(values)
        # Directory templates name the RELEASE (or the playlist), not the track:
        # {name} in album_folder/ep_folder/single_folder is the album title, and in
        # playlist_folder the playlist name. File templates keep the track title.
        dir_values["name"] = _first(collection, values.get("album"),
                                    values.get("name"), _FALLBACKS["name"])
        segs = path_segments(render(dir_tpl, dir_values, source=dir_slot))
        dest_dir = os.path.join(music_root, *segs)
    else:
        dest_dir = None
    fname = (utils.sanitize_filename(render(file_tpl, values, source=file_slot) + ext)
             if file_tpl else None)
    return dest_dir, fname


def sample_values() -> Dict[str, str]:
    """A fixed demo context so `--format` edits and --format-show can render a live
    preview of what a template produces without downloading anything."""
    return {
        "artist": "Daft Punk", "artist_id": "36819", "artist_initials": "DP",
        "album_artist": "Daft Punk", "name": "One More Time",
        "album": "Discovery", "track_number": "06", "total_tracks": "14",
        "disc_number": "01", "total_discs": "1", "playlist_position": "06",
        "id": "1068442", "album_id": "0724384960650",
        "label": "Daft Life Ltd. - ADA France", "catalog_number": "0724384960650",
        "isrc": "GBDUW0000053", "upc": "0724384960650", "release_year": "2001",
        "explicit": "", "quality": "FLAC (Lossless)", "creator": "Daft Punk",
        "creator_id": "36819", "tracks": "14",
    }


def variable_reference() -> List[tuple]:
    """(variable, example value) pairs for every usable template variable — the single
    source the CLI's --format-show and the TUI's variable table both render, so the
    two can't drift apart."""
    sample = sample_values()
    return [(var, sample.get(var, "")) for var in VARIABLES]


def preview(slot: str, template: str) -> str:
    """What `template` in `slot` would produce for the sample context — folder slots
    render against the release/playlist view of {name}, file slots against the track
    view, exactly like target_paths does at download time. Folder segments are joined
    with the platform's separator so the preview looks like the real destination."""
    values = sample_values()
    if slot.endswith("_file"):
        return utils.sanitize_filename(render(template, values, source=slot) + ".flac")
    v = dict(values)
    if slot == "playlist_folder":
        v["name"] = "My Playlist"
    else:
        v["name"] = values["album"]
    return os.path.join(*path_segments(render(template, v, source=slot)))
