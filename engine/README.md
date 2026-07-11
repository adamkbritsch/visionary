# Engine components (Mac-side)

Runs on the Mac that hosts Topaz + DaVinci Resolve. Buildable/testable now;
the actual Topaz/Resolve stage runners wait on hardware.

## scratch.py — scratch-volume manager
Pre-flight storage setup for each run. The pipeline writes ~300 GB ProRes
intermediates, so it needs the external **"2TB SSD"** as scratch. That drive
occasionally "ghosts" on macOS (device attached, mount dropped), so before
processing the manager **force-cycles it (unmount → remount)** to recover the
mount, verifies it's writable, and returns `/Volumes/2TB SSD/topaz-scratch`.
If the drive can't be brought back writable, it **falls back to
`~/Downloads/topaz-scratch`** so the engine keeps running.

- Targets the drive **by volume name**, never device node (the node, e.g.
  `disk4s2`, changes across reconnects; the name doesn't).
- Unmount is best-effort and escalates to `diskutil unmount force`; a failed
  unmount never aborts the remount.
- Caveat: the fallback lives on the internal disk (~83 GB free) and **cannot
  hold a full 300 GB intermediate** — it's a reduced-capacity mode the
  engine's disk-watermark check must respect (use ProRes HQ / smaller jobs).

```
python3 scratch.py            # cycles the drive, prints chosen scratch path
python3 -m unittest test_scratch    # 5 unit tests (FakeOps, no hardware)
```

Validated live on this Mac: real unmount+remount of "2TB SSD" (~6 s, drive
returns writable) and real fallback to Downloads when the volume is absent.
