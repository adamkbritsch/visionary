"""The one writer for ~/.topaz-pipeline/config.json — the file that holds every
credential the pipeline uses (NAS FTP, Plex token, TMDb key, youtarr, Shuttle relay,
YouTube OAuth). Grown out of ytdata._save (which is now an alias over write()) for the
in-app Setup section: the app edits config through POST /api/config instead of the user
hand-editing JSON.

Three layers:
  - write(updates): trusted merge — atomic tmp+replace, 0600 file, 0700 dir (a fresh
    machine has no ~/.topaz-pipeline at all). No filtering; callers are the engine
    itself (ytdata's OAuth flow, the Setup API's save()).
  - save(updates): the API path — filtered to ALLOWED_KEYS, values normalized
    (ftp_hosts accepts a list or a comma string; ftp_port is int-coerced), and an
    empty string DELETES the key (the UI's explicit "clear" affordance).
  - read_redacted(): what GET /api/config returns — secret VALUES never leave this
    module; the UI only learns whether each secret is set.

NEVER log, print, or echo values from this file (CLAUDE.md hard rule).
"""
from __future__ import annotations
import json
import os

CONFIG = os.path.expanduser("~/.topaz-pipeline/config.json")

# Keys the Setup API may write. youtube_* stay OUT — ytdata owns those via its own
# OAuth flow (they still pass through write(), just not through save()).
ALLOWED_KEYS = ("ftp_hosts", "ftp_port", "ftp_user", "ftp_pass",
                "plex_url", "plex_token", "tmdb_api_key",
                "youtarr_url", "youtarr_user", "youtarr_pass",
                "shuttle_relay_url", "shuttle_relay_token",
                # Media-folder routing (Plex-detected, user-overridable). Both are dicts,
                # not strings: media_lib_kinds = {plex section key: kind},
                # media_roots = {kind: [ftp paths]} — the APPLIED answer transfer reads.
                "media_lib_kinds", "media_roots")

# Allowed but NOT part of the flat text-field surface: structured (dict) values the app
# edits through their own endpoint, so `fields` stays a clean [String: String] for Swift.
STRUCTURED_KEYS = frozenset({"media_lib_kinds", "media_roots"})

# Values that must never be echoed back to any reader of the API.
SECRET_KEYS = frozenset({"ftp_pass", "plex_token", "tmdb_api_key", "youtarr_pass",
                         "shuttle_relay_token", "youtube_client_secret",
                         "youtube_refresh_token"})


def read() -> dict:
    """Tolerant load — {} on missing/corrupt (mirrors every module's own _config())."""
    try:
        with open(CONFIG) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write(updates: dict) -> None:
    """Merge keys into config.json — atomic, 0600, dir created 0700 (a drop-in .app's
    first run has no ~/.topaz-pipeline). A key whose value is None is REMOVED."""
    cfg = read()
    for k, v in updates.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    d = os.path.dirname(CONFIG)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    os.chmod(CONFIG, 0o600)


def save(updates: dict) -> dict:
    """The API write path: allowlisted, normalized, ""-deletes. Returns read_redacted()
    plus the list of ignored keys (so a typo'd client shows itself in tests)."""
    clean, ignored = {}, []
    for k, v in (updates or {}).items():
        if k not in ALLOWED_KEYS:
            ignored.append(k)
            continue
        if v is None or v == "" or v == []:
            clean[k] = None                       # delete
        elif k == "ftp_hosts":
            hosts = v if isinstance(v, list) else str(v).split(",")
            hosts = [h.strip() for h in hosts if h and h.strip()]
            clean[k] = hosts if hosts else None
        elif k == "ftp_port":
            try:
                clean[k] = int(v)
            except (TypeError, ValueError):
                ignored.append(k)                 # unparseable port — refuse silently
        elif k == "media_lib_kinds":
            # {plex section key: kind|None}. Dropping a key means "back to the default".
            if not isinstance(v, dict):
                ignored.append(k); continue
            import medialibs
            m = {str(sk): (str(kind) if kind else None) for sk, kind in v.items()
                 if not kind or str(kind) in medialibs.KINDS}
            clean[k] = {sk: kind for sk, kind in m.items() if kind} or None
        elif k == "media_roots":
            # {kind: [absolute ftp paths]} — anything relative or non-absolute is refused
            # rather than half-applied, since a bad root silently empties a library.
            if not isinstance(v, dict):
                ignored.append(k); continue
            import medialibs
            m = {}
            for kind, paths in v.items():
                if str(kind) not in medialibs.KINDS:
                    continue
                lst = paths if isinstance(paths, list) else [paths]
                good = [str(p).rstrip("/") for p in lst if str(p or "").startswith("/")]
                if good:
                    m[str(kind)] = good
            clean[k] = m or None
        else:
            clean[k] = str(v).strip() or None
    if clean:
        write(clean)
    out = read_redacted()
    if ignored:
        out["ignored"] = ignored
    return out


def read_redacted() -> dict:
    """What the app may see: non-secret ALLOWED keys verbatim (stringified — Swift
    decodes [String:String] trivially), secrets only as set/unset booleans."""
    cfg = read()
    fields, secrets_set = {}, {}
    for k in ALLOWED_KEYS:
        if k in STRUCTURED_KEYS:
            continue          # dicts, not text fields — served by /api/media-libraries
        if k in SECRET_KEYS:
            fields[k] = ""
            secrets_set[k] = bool(cfg.get(k))
        else:
            v = cfg.get(k)
            if isinstance(v, list):
                fields[k] = ", ".join(str(x) for x in v)
            else:
                fields[k] = "" if v is None else str(v)
    return {"fields": fields, "secrets_set": secrets_set, "path": CONFIG}
