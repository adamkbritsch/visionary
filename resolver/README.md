# Plex → NAS show resolver (v1)

Given a TV show title in Plex, locate every episode's file on the NAS host
filesystem and emit an ordered replacement manifest. This is stage 0 of the
overnight upscaling pipeline (see `../docs/superpowers/specs/`): it answers
"which exact files do I replace, and in what order."

## Runs on
The NAS host (it needs the Plex token + direct filesystem access to verify
files exist). Copy this folder anywhere on the NAS and run it with a Python 3.11
venv that has `plexapi` (tested with plexapi 4.17). Plex URL/token are read from
a dotenv file — `$RESOLVER_ENV_FILE`, defaulting to `~/.plex-resolver.env`:
```
PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=<your token>
```

## Usage
```
python resolver.py "Brooklyn Nine-Nine" -o manifests/brooklyn-nine-nine.json
```
Prints a summary (episode count, located vs MISSING, resolutions present,
first-five replacement order) and optionally writes the JSON manifest.
Exit 2 on not-found or ambiguous title.

## Tests
```
python -m unittest test_resolver        # 12 unit tests, no Plex needed
```
Pure logic (path translation, ordering, show selection, fs verification) is
unit-tested. The live Plex glue is validated by acceptance runs against the
real server (verified across vol1/vol2/vol3).

## Container → host path map
- `/media/<rest>`      → `/volume1/Media/<rest>`
- `/media/vol2/<rest>` → `/volume2/MediaVolume2/<rest>`
- `/media/vol3/<rest>` → `/volume3/MediaVolume3/<rest>`
- `vol4` and other volumes are unmapped → `UnmappedPathError` (vol4 pending).

## Manifest shape
`{ show, year, ratingKey, total, located, missing, episodes: [ {season,
episode, title, container_path, host_path, state, resolution}, ... ] }`
where `state` ∈ {LOCATED, MISSING}. `resolution` (e.g. "1080", "4k") lets the
engine skip already-4K content.

## Next
- `--upscale-only` filter: mark episodes already ≥4K as SKIP.
- Hand the manifest to the replacement engine (Topaz → Resolve → remux).
