"""Parse whatever a YouTube link can express, so the app can accept a pasted URL.

PURE — no network, no state. Resolution (titles, playlist contents, handle -> channel id)
lives in ytdata.py; committing an import lives in youtube.py. Keeping the parse separate is
what lets the UI show "24 videos from <playlist>" and ask which interpretation the user
meant BEFORE anything is queued.

The video half deliberately delegates to youtube.parse_video_id (which already knows
watch / youtu.be / shorts / live / embed / bare-id) so the two can never disagree about
what a video link is; this module only adds the playlist, channel and handle shapes.
"""
from __future__ import annotations
import re

# list=<id>. Real playlist ids start with a known 2-char class (PL user, UU uploads,
# LL likes, FL favourites, RD radio, OL/PU/TL auto). WL/LM exist but are PRIVATE — they are
# matched here so the error comes from the API ("not accessible") rather than from a
# baffling "unknown link".
_LIST = re.compile(r"[?&;]list=([0-9A-Za-z_-]+)")
_BARE_LIST = re.compile(r"^(?:PL|UU|LL|FL|RD|OL|PU|TL|WL|LM)[0-9A-Za-z_-]*$")
_CHANNEL = re.compile(r"/channel/(UC[0-9A-Za-z_-]{22})")
_BARE_CHANNEL = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_HANDLE = re.compile(r"(?:^|/)@([A-Za-z0-9._-]{1,60})")
_LEGACY = re.compile(r"/(?:c|user)/([A-Za-z0-9._-]{1,60})")


def _blank() -> dict:
    return {"kind": "unknown", "video_id": "", "playlist_id": "",
            "channel_id": "", "handle": "", "ambiguous": False}


def parse_link(text) -> dict:
    """{kind, video_id, playlist_id, channel_id, handle, ambiguous}.

    kind is the PRIMARY interpretation: 'video' | 'playlist' | 'channel' | 'handle' |
    'unknown'. A `watch?v=…&list=…` link expresses BOTH, so it comes back kind='video'
    with playlist_id filled and ambiguous=True — the caller asks which was meant rather
    than guessing (user-dictated). 'handle' still needs a channels.list lookup to become a
    channel id; 'channel' does not.

    Tolerates a missing scheme, www./m./music. hosts, extra query params (si=, t=, index=)
    and trailing slashes. Anything it cannot place comes back 'unknown' — never a guess.
    """
    import youtube
    out = _blank()
    t = str(text or "").strip()
    if not t:
        return out
    t = t.split()[0]                      # a pasted line sometimes carries a trailing title

    vid = youtube.parse_video_id(t)
    m = _LIST.search(t)
    pid = m.group(1) if m else ""
    if not pid and not vid and _BARE_LIST.match(t):
        pid = t                           # someone pasted just the list id
    elif not pid and vid == t and _BARE_LIST.match(t):
        # TIE: a bare token that is BOTH a valid 11-char video id and starts with a playlist
        # class (e.g. "PLabc-123_x"). Video wins — 11 chars is EXACTLY a video id's length,
        # while real playlist ids are 2 (WL/LM) or 30+ — and the resolve step shows the user
        # the resolved title before anything is queued, so a wrong call here is caught there.
        pass

    if vid:
        out.update(kind="video", video_id=vid, playlist_id=pid, ambiguous=bool(pid))
        return out
    if pid:
        out.update(kind="playlist", playlist_id=pid)
        return out

    m = _CHANNEL.search(t)
    if m:
        out.update(kind="channel", channel_id=m.group(1))
        return out
    if _BARE_CHANNEL.match(t):
        out.update(kind="channel", channel_id=t)
        return out
    m = _HANDLE.search(t) or _LEGACY.search(t)
    if m:
        out.update(kind="handle", handle=m.group(1))
        return out
    return out
