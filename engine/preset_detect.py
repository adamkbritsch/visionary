"""Orchestrate Topaz-preset auto-detection for a title. TMDb decides whether it's animation and,
if so, the technique (flat-2D / anime → animation2d, CGI → animation3d, stop-motion → digital);
shotonwhat decides live-action FILM vs DIGITAL. Returns a preset key, or None = no confident match
(the caller then falls back to the manual PresetChooser). Both sources are cached + never raise."""
from __future__ import annotations
import shotonwhat
import tmdb


def detect_preset(title, year, kind):
    """'film' | 'digital' | 'animation2d' | 'animation3d' | None. `kind` is 'tv' or 'movie'."""
    if not title:
        return None
    info = tmdb.lookup(title, year, kind)
    if tmdb.is_animation(info):
        return tmdb.technique(info)        # animation2d / animation3d / digital(stop-motion) / None(unclear)
    # live-action, or TMDb unavailable → shotonwhat film/digital; a miss returns None → manual
    return shotonwhat.film_or_digital(title, year)
