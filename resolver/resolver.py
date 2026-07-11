"""Plex TV-show -> NAS file resolver (first version).

Given a show title in Plex, locate its episode files on the NAS host
filesystem and emit an ordered replacement manifest.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, asdict

_MEDIA_PREFIX = "/media/"
# Plex container path -> NAS host path. Recorded mapping:
#   /media/<rest>       -> /volume1/Media/<rest>
#   /media/vol2/<rest>  -> /volume2/MediaVolume2/<rest>
#   /media/vol3/<rest>  -> /volume3/MediaVolume3/<rest>
_VOL_MAP = {
    "vol2": "/volume2/MediaVolume2/",
    "vol3": "/volume3/MediaVolume3/",
}
_VOLN = re.compile(r"^vol\d+/")


class UnmappedPathError(ValueError):
    """A Plex container path has no known NAS host mapping."""


class ShowNotFoundError(LookupError):
    """No show in Plex matched the query."""


class AmbiguousShowError(LookupError):
    """The query matched more than one show; be more specific."""


def container_to_host(path: str) -> str:
    if not path.startswith(_MEDIA_PREFIX):
        raise UnmappedPathError(f"not a Plex /media path: {path!r}")
    rest = path[len(_MEDIA_PREFIX):]
    m = _VOLN.match(rest)
    if m:
        vol = m.group(0)[:-1]  # strip trailing '/'
        base = _VOL_MAP.get(vol)
        if base is None:
            raise UnmappedPathError(f"unmapped volume {vol!r} in {path!r}")
        return base + rest[len(vol) + 1:]
    return "/volume1/Media/" + rest


@dataclass
class EpisodeTarget:
    season: int
    episode: int
    title: str
    container_path: str
    host_path: str
    state: str = "PENDING"

    def to_dict(self) -> dict:
        return asdict(self)


def build_targets(episodes) -> list[EpisodeTarget]:
    """episodes: iterable of dicts with season, episode, title, file."""
    targets = [
        EpisodeTarget(
            season=ep["season"],
            episode=ep["episode"],
            title=ep["title"],
            container_path=ep["file"],
            host_path=container_to_host(ep["file"]),
        )
        for ep in episodes
    ]
    targets.sort(key=lambda t: (t.season, t.episode))
    return targets


def pick_show(candidates, query: str):
    """Choose one show from Plex search results.

    Prefer an exact (case-insensitive) title match; otherwise accept a
    single fuzzy candidate. Raise on none or genuine ambiguity.
    """
    if not candidates:
        raise ShowNotFoundError(f"no show matched {query!r}")
    exact = [c for c in candidates if c.title.lower() == query.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousShowError(f"{query!r} matched multiple exact titles")
    if len(candidates) == 1:
        return candidates[0]
    titles = ", ".join(c.title for c in candidates)
    raise AmbiguousShowError(f"{query!r} matched several shows: {titles}")


def verify_targets(targets, exists=os.path.exists) -> list[EpisodeTarget]:
    for t in targets:
        t.state = "LOCATED" if exists(t.host_path) else "MISSING"
    return targets


# ---------------------------------------------------------------------------
# Live integration (Plex) + CLI.  Pure logic above is unit-tested; this thin
# glue is exercised by an acceptance run against the real server.
# ---------------------------------------------------------------------------

def load_env(path=None) -> dict:
    """Read PLEX_URL / PLEX_TOKEN from a dotenv-style file. Default: $RESOLVER_ENV_FILE,
    falling back to ~/.plex-resolver.env on the machine that runs this (usually the NAS)."""
    path = path or os.environ.get("RESOLVER_ENV_FILE") or os.path.expanduser("~/.plex-resolver.env")
    d = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k] = v.strip().strip('"').strip("'")
    return d


def connect(env=None):
    from plexapi.server import PlexServer
    env = env or load_env()
    return PlexServer(env["PLEX_URL"], env["PLEX_TOKEN"])


def _find_candidates(plex, query):
    cands = []
    for sec in (s for s in plex.library.sections() if s.type == "show"):
        try:
            cands.extend(sec.search(title=query))
        except Exception:
            pass
    if not cands:  # contains-fallback
        for sec in (s for s in plex.library.sections() if s.type == "show"):
            cands.extend(sh for sh in sec.all() if query.lower() in sh.title.lower())
    # de-dup by ratingKey
    seen, uniq = set(), []
    for c in cands:
        if c.ratingKey not in seen:
            seen.add(c.ratingKey); uniq.append(c)
    return uniq


def _episode_records(show):
    recs, skipped = [], []
    for ep in show.episodes():
        parts = [p for m in ep.media for p in m.parts]
        if not parts:
            skipped.append((ep.seasonNumber, ep.episodeNumber, "no media part"))
            continue
        m0 = ep.media[0]
        recs.append({
            "season": ep.seasonNumber,
            "episode": ep.episodeNumber,
            "title": ep.title or "",
            "file": parts[0].file,
            "resolution": getattr(m0, "videoResolution", None),
            "parts": len(parts),
        })
    return recs, skipped


def resolve_show(plex, query: str):
    show = pick_show(_find_candidates(plex, query), query)
    recs, skipped = _episode_records(show)
    targets = verify_targets(build_targets(recs))
    # carry resolution onto targets for reporting
    by_key = {(r["season"], r["episode"]): r for r in recs}
    for t in targets:
        r = by_key.get((t.season, t.episode), {})
        t_dict_extra = {"resolution": r.get("resolution"), "parts": r.get("parts", 1)}
        for k, v in t_dict_extra.items():
            setattr(t, k, v)
    return show, targets, skipped


def main(argv=None):
    import argparse, json, sys
    ap = argparse.ArgumentParser(description="Resolve a Plex TV show to NAS episode files.")
    ap.add_argument("title", help="Show title (exact or contains match)")
    ap.add_argument("-o", "--out", help="Write manifest JSON to this path")
    args = ap.parse_args(argv)

    plex = connect()
    try:
        show, targets, skipped = resolve_show(plex, args.title)
    except (ShowNotFoundError, AmbiguousShowError) as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

    located = [t for t in targets if t.state == "LOCATED"]
    missing = [t for t in targets if t.state == "MISSING"]
    print(f"Show: {show.title} ({getattr(show,'year','?')})  ratingKey={show.ratingKey}")
    print(f"Episodes: {len(targets)}   located: {len(located)}   MISSING: {len(missing)}")
    res = sorted({getattr(t, 'resolution', None) for t in targets}, key=lambda x: str(x))
    print(f"Resolutions present: {res}")
    if missing:
        print("\nMISSING on NAS (Plex path did not resolve to an existing file):")
        for t in missing[:20]:
            print(f"  S{t.season:02d}E{t.episode:02d}  {t.host_path}")
    print("\nFirst located episodes (replacement order):")
    for t in located[:5]:
        print(f"  S{t.season:02d}E{t.episode:02d}  [{getattr(t,'resolution',None)}]  {t.host_path}")

    manifest = {
        "show": show.title,
        "year": getattr(show, "year", None),
        "ratingKey": show.ratingKey,
        "total": len(targets),
        "located": len(located),
        "missing": len(missing),
        "episodes": [t.to_dict() | {"resolution": getattr(t, "resolution", None)} for t in targets],
    }
    if args.out:
        with open(args.out, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\nManifest written: {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
