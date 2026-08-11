"""AI border extension (4:3 -> 16:9) through the locally installed ComfyUI.

The extend stage outpaints a 4:3 episode's left/right borders with WAN 2.1 VACE,
driven HEADLESSLY: Visionary spawns Comfy Desktop's own ComfyUI checkout (its venv
python, a dedicated port) and talks to the standard HTTP API — the Electron app is
never involved. The workflow mirrors ComfyUI's bundled `video_wan_vace_outpainting`
template (1.3B + CausVid path: 3 steps, cfg 1.0, uni_pc/simple, shift 8.0 — verified
against the installed 0.30.2 template and node schemas).

QUALITY PRINCIPLE: only the borders are AI. Each 81-frame chunk is outpainted at a
480p-class working resolution; then ONLY the generated side strips are cropped out,
upscaled to the source height, and hstacked around the ORIGINAL full-resolution
frames. Source pixels ship untouched; audio never passes through any of this.

Everything here is either PURE (geometry, chunk plan, graph builder, ffmpeg argv
builders — unit-tested) or a thin subprocess/HTTP wrapper (ComfyBackend/ComfyClient).
The pipeline wiring lives in stages._extend.
"""
from __future__ import annotations
import atexit
import json
import os
import plistlib
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

DESKTOP_SETTINGS = os.path.expanduser(
    "~/Library/Application Support/Comfy Desktop/settings.json")
DESKTOP_APP_PLIST = "/Applications/Comfy Desktop.app/Contents/Info.plist"
CONFIG_FILE = os.path.expanduser("~/.topaz-pipeline/config.json")
PACE_FILE = os.path.expanduser("~/.topaz-pipeline/borders_model.json")

COMFY_PORT = 8189               # DEDICATED — the Desktop app's own 8188 is never touched
CHUNK_FRAMES = 81               # the template's window (WanVaceToVideo length)
MIN_TAIL_FRAMES = 9             # a shorter remainder folds into the previous chunk
WORK_HEIGHT = 480               # 1.3B is a 480p-class model; strips are upscaled after
ASPECT_TOLERANCE = 0.05         # |DAR - 4/3| acceptance band (catches anamorphic DV)

DEFAULT_PROMPT = ("the scene continues naturally to the sides, consistent lighting "
                  "and setting, seamless extension of the original footage")
DEFAULT_NEGATIVE = "bad quality, blurry, messy, chaotic"   # the template's negative

# Sampler parameters lifted VERBATIM from the bundled template's 1.3B/CausVid path.
STEPS, CFG, SHIFT = 3, 1.0, 8.0
SAMPLER, SCHEDULER = "uni_pc", "simple"
LORA_STRENGTH_MODEL, LORA_STRENGTH_CLIP = 0.7, 1.0

MODELS = {
    "borders_vace": {
        "rel": "diffusion_models/wan2.1_vace_1.3B_fp16.safetensors",
        "url": ("https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/"
                "main/split_files/diffusion_models/wan2.1_vace_1.3B_fp16.safetensors"),
        "expected_bytes": 4309519800,    # pinned from the HF Content-Length (2026-08-10)
        "label": "WAN 2.1 VACE 1.3B",
    },
    "borders_umt5": {
        "rel": "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "url": ("https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/"
                "main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
        "expected_bytes": 6735906897,    # from the Desktop's own .dl-meta sidecar
        "label": "UMT5-XXL text encoder",
    },
    "borders_causvid": {
        "rel": "loras/Wan21_CausVid_bidirect2_T2V_1_3B_lora_rank32.safetensors",
        "url": ("https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/"
                "Wan21_CausVid_bidirect2_T2V_1_3B_lora_rank32.safetensors"),
        "expected_bytes": 91233416,      # pinned from the HF Content-Length (2026-08-10)
        "label": "CausVid 1.3B LoRA",
    },
    # Usually already on disk (Comfy Desktop pulls it with any WAN template), but a
    # fresh install without it must be able to complete from Setup like the rest.
    "borders_vae": {
        "rel": "vae/wan_2.1_vae.safetensors",
        "url": ("https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/"
                "main/split_files/vae/wan_2.1_vae.safetensors"),
        "expected_bytes": 253815318,     # pinned from the HF Content-Length (2026-08-10)
        "label": "WAN 2.1 VAE",
    },
}
VAE_REL = MODELS["borders_vae"]["rel"]


# ---- discovery -----------------------------------------------------------------------

def _config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def discover() -> dict:
    """Where ComfyUI lives — zero-config from Comfy Desktop's own settings.json (the
    Shuttle-token pattern), with `comfy_dir`/`comfy_port` config overrides. Never raises.

    COMFY DESKTOP IS REQUIRED, not merely convenient: the paths below are its managed
    layout (the doubled `ComfyUI/ComfyUI` checkout and a `.venv` beside it), and the
    `comfy_dir` override only relocates that layout — it does not accept a hand-cloned
    ComfyUI, whose main.py sits at the root and whose interpreter is wherever you made it.

    VideoHelperSuite is required too and is NOT bundled with Comfy Desktop (its custom_nodes
    ships only websocket_image_save.py): the graph writes its result through
    VHS_VideoCombine, because core SaveVideo's DynamicCombo codec input does not
    hand-serialize into an API-format prompt. Detected here so a missing node is named in
    Setup up front, rather than failing the first chunk of an overnight run."""
    cfg = _config()
    out = {"ok": False, "install_dir": "", "checkout": "", "venv_python": "",
           "models_dir": "", "desktop_version": "", "comfy_version": "", "vhs": False,
           "port": int(cfg.get("comfy_port") or COMFY_PORT), "missing": []}
    try:
        with open(DESKTOP_SETTINGS) as f:
            ds = json.load(f) or {}
    except (OSError, ValueError):
        ds = {}
    install = str(cfg.get("comfy_dir") or ds.get("installDir") or "")
    if not install:
        out["missing"].append("Comfy Desktop (settings.json not found — install and run it once)")
        return out
    checkout = os.path.join(install, "ComfyUI", "ComfyUI")
    if not os.path.exists(os.path.join(checkout, "main.py")):
        out["missing"].append(f"ComfyUI checkout at {checkout}")
        return out
    venv_python = os.path.join(checkout, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        out["missing"].append("ComfyUI's venv python (run Comfy Desktop once to install)")
        return out
    models = (ds.get("modelsDirs") or [""])[0] or os.path.join(install, "models")
    out.update(install_dir=install, checkout=checkout, venv_python=venv_python,
               models_dir=models)
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"',
                      open(os.path.join(checkout, "comfyui_version.py")).read())
        out["comfy_version"] = m.group(1) if m else ""
    except OSError:
        pass
    try:
        with open(DESKTOP_APP_PLIST, "rb") as f:
            out["desktop_version"] = plistlib.load(f).get("CFBundleShortVersionString", "")
    except Exception:
        pass
    try:                                  # dir name varies by how it was installed
        out["vhs"] = any("videohelpersuite" in n.lower()
                         for n in os.listdir(os.path.join(checkout, "custom_nodes")))
    except OSError:
        pass
    if not out["vhs"]:
        out["missing"].append("ComfyUI-VideoHelperSuite (install it from ComfyUI Manager)")
    out["ok"] = True                      # ComfyUI itself is usable — see `vhs` for the node
    return out


def env_ready(env=None, models_dir=None):
    """(ready, [missing]) for the WHOLE extender: ComfyUI + the VideoHelperSuite node +
    every model. The per-show row and the stage both gate on this, so they can never
    disagree about whether outpainting is actually possible."""
    env = discover() if env is None else env
    if not env.get("ok"):
        return False, list(env.get("missing") or ["ComfyUI not found"])
    missing = [m for m in (env.get("missing") or [])]          # e.g. VideoHelperSuite
    ok, model_missing = models_ready(models_dir or env.get("models_dir") or "")
    missing += model_missing
    return (not missing), missing


# ---- models --------------------------------------------------------------------------

def model_status(models_dir: str) -> dict:
    """Per-model {label, rel, present, bytes, expected, state ok|truncated|missing}.
    `truncated` resumes in place (curl -C -)."""
    out = {}
    for what, m in MODELS.items():
        path = os.path.join(models_dir, m["rel"])
        size = os.path.getsize(path) if os.path.exists(path) else 0
        exp = m["expected_bytes"]
        if size <= 0:
            state = "missing"
        elif exp and size < exp:
            state = "truncated"
        else:
            state = "ok"
        out[what] = {"label": m["label"], "rel": m["rel"], "present": size > 0,
                     "bytes": size, "expected": exp, "state": state}
    return out


def models_ready(models_dir: str):
    """(ready, [missing labels])"""
    st = model_status(models_dir)
    missing = [v["label"] for v in st.values() if v["state"] != "ok"]
    return (not missing, missing)


def model_download_argv(what: str, models_dir: str):
    """curl argv for one model — ENGINE-computed (the setup_jobs argv pattern: nothing
    user-supplied). -C - resumes a truncated file in place."""
    m = MODELS.get(what)
    if not m or not models_dir:
        return None
    dest = os.path.join(models_dir, m["rel"])
    # -sS: no progress meter (its \r updates never reach a line-buffered tail — the
    # Setup row polls the on-disk byte count instead), errors still print.
    return ["/usr/bin/curl", "-fsSL", "-C", "-", "--create-dirs", "-o", dest, m["url"]]


# ---- geometry (PURE) -----------------------------------------------------------------

def _round_to(v: float, mult: int, up: bool = False) -> int:
    n = int(v // mult) * mult
    if up and n < v:
        n += mult
    elif not up:
        n = int(round(v / mult)) * mult
    return max(mult, n)


def plan_geometry(src_w: int, src_h: int, sar_num: int = 1, sar_den: int = 1):
    """4:3 -> 16:9 outpaint geometry, SAR-aware. Returns a dict or {"error", "detail"}.

    Full-res: the shipped frame is [gen_left | ORIGINAL | gen_right] at source height —
    strips sized so the DISPLAY aspect lands on 16:9. Working res: the model sees a
    mult-of-16 canvas at ~480p with mult-of-8 pads; strip crops are INNER-ALIGNED so the
    pixels adjacent to the seam are the generated ones that continue the real edge."""
    if not (src_w > 0 and src_h > 0 and sar_num > 0 and sar_den > 0):
        return {"error": "bad-dims", "detail": f"unusable dimensions {src_w}x{src_h}"}
    display_w = src_w * sar_num / sar_den
    dar = display_w / src_h
    if abs(dar - 16 / 9) < ASPECT_TOLERANCE:
        return {"error": "not-4x3", "detail": "already 16:9 — nothing to extend"}
    if abs(dar - 4 / 3) > ASPECT_TOLERANCE:
        return {"error": "not-4x3",
                "detail": f"not close enough to 4:3 (measured {dar:.2f}:1)"}
    # Full-res strips: bring the DISPLAY width to 16:9 at source height. The original is
    # rendered at its display width (a no-op for square pixels; anamorphic normalizes).
    disp_w = int(round(display_w / 2) * 2)
    strip_w = int(round((src_h * 16 / 9 - disp_w) / 2 / 2) * 2)     # even
    final_w = disp_w + 2 * strip_w
    # Working res: 4:3 core at WORK_HEIGHT (bounded by the source height), pads rounded
    # UP to 8 with a safety margin, canvas bumped to a multiple of 16.
    work_h = _round_to(min(WORK_HEIGHT, src_h), 16)
    work_core_w = _round_to(work_h * 4 / 3, 16)
    pad_work = _round_to(strip_w * work_h / src_h, 8, up=True) + 8
    canvas_w = work_core_w + 2 * pad_work
    if canvas_w % 16:
        pad_work += (16 - canvas_w % 16) // 2 + (8 if (16 - canvas_w % 16) % 16 else 0)
        pad_work = _round_to(pad_work, 8, up=True)
        canvas_w = work_core_w + 2 * pad_work
    if canvas_w % 16:                       # belt: force it
        canvas_w = _round_to(canvas_w, 16, up=True)
        pad_work = (canvas_w - work_core_w) // 2
    # Inner-aligned crops: take the strip pixels ADJACENT to the original's edges.
    crop_w_work = max(2, int(round(strip_w * work_h / src_h / 2) * 2))
    crop_left_x = pad_work - crop_w_work
    crop_right_x = pad_work + work_core_w
    return {"src_w": src_w, "src_h": src_h, "disp_w": disp_w, "dar": dar,
            "strip_w": strip_w, "final_w": final_w,
            "work_h": work_h, "work_core_w": work_core_w, "pad_work": pad_work,
            "canvas_w": canvas_w, "canvas_h": work_h,
            "crop_w_work": crop_w_work, "crop_left_x": max(0, crop_left_x),
            "crop_right_x": crop_right_x}


# ---- show-aspect book + the per-episode gate -----------------------------------------

ASPECT_FILE = os.path.expanduser("~/.topaz-pipeline/show_aspects.json")


def parse_sar(s):
    """"8:9" -> (8, 9); ""/"0:1"/"N/A"/junk -> (1, 1) (square pixels)."""
    try:
        n, d = str(s or "").replace("/", ":").split(":")
        n, d = int(n), int(d)
        if n > 0 and d > 0:
            return n, d
    except (ValueError, AttributeError):
        pass
    return 1, 1


def aspect_label(width, height, sar=""):
    """"4:3" / "16:9" / "other" from probe fields, or None when unusable. Drives the
    per-show row's VISIBILITY (hide-inert-UI: the option only renders on 4:3 shows)."""
    n, d = parse_sar(sar)
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    dar = w * n / d / h
    if abs(dar - 4 / 3) <= ASPECT_TOLERANCE:
        return "4:3"
    if abs(dar - 16 / 9) <= ASPECT_TOLERANCE:
        return "16:9"
    return "other"


def record_show_aspect(show, label) -> None:
    """Best-effort book write ({show: "4:3"|...}) — filled by every source probe and by
    the server's eager head-probe, read by the settings rows. Never raises.

    "4:3" IS STICKY. Real shows mix aspects — It's Always Sunny is 4:3 through S05 and
    16:9 from S06 (verified against the live library) — and the book holds ONE label per
    show, so last-probe-wins made the row VANISH mid-show the moment a wide episode was
    probed (with the option still on underneath). A show that has ever probed 4:3 HAS 4:3
    content: the row stays offered, and the per-episode gate already makes every wide
    episode skip itself, so a sticky label can never extend the wrong picture."""
    if not show or label not in ("4:3", "16:9", "other"):
        return
    try:
        book = all_show_aspects()
        if book.get(show) == label or book.get(show) == "4:3":
            return                          # sticky: 4:3 is never downgraded
        book[str(show)] = label
        os.makedirs(os.path.dirname(ASPECT_FILE), exist_ok=True)
        tmp = ASPECT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(book, f)
        os.replace(tmp, ASPECT_FILE)
    except Exception:
        pass


def all_show_aspects() -> dict:
    try:
        with open(ASPECT_FILE) as f:
            book = json.load(f)
        return book if isinstance(book, dict) else {}
    except Exception:
        return {}


def show_aspect(show):
    """"4:3" / "16:9" / "other" if any of this show's episodes was ever probed, else None."""
    v = all_show_aspects().get(str(show or ""))
    return v if v in ("4:3", "16:9", "other") else None


def extend_gate(p) -> dict:
    """Does THIS item's extend stage have real work? {"needed", "reason", "geom"}.
    TV episodes only (v1); movies/YouTube/combine no-op. The probe reads the ORIGINAL
    source (authoritative SAR — the CFR pass preserves storage geometry), falling back
    to the CFR file. Fails OPEN (not needed): extend is an enhancement and must never
    park an episode over a probe hiccup — the 4:3 picture then ships as before."""
    if getattr(p, "combine", False) or getattr(p, "movie", False) \
            or getattr(p, "youtube", False):
        return {"needed": False, "reason": "TV episodes only", "geom": None}
    import settings
    if not settings.get_show_extend_borders(p.series):
        return {"needed": False, "reason": "extend borders is off for this show",
                "geom": None}
    src = p.source if os.path.exists(getattr(p, "source", "") or "") \
        else getattr(p, "source_cfr", "") or ""
    if not src or not os.path.exists(src):
        return {"needed": False, "reason": "no local source yet", "geom": None}
    import plan as plan_mod
    info = plan_mod.probe_input(src)
    if not info.get("width"):
        return {"needed": False, "reason": "source not probeable", "geom": None}
    label = aspect_label(info["width"], info["height"], info.get("sar"))
    if label:
        record_show_aspect(p.series, label)
    if info.get("is_hdr"):
        return {"needed": False, "reason": "HDR source — the outpaint model is SDR-only",
                "geom": None}
    n, d = parse_sar(info.get("sar"))
    g = plan_geometry(info["width"], info["height"], n, d)
    if "error" in g:
        return {"needed": False, "reason": g["detail"], "geom": None}
    return {"needed": True, "reason": "", "geom": g}


# ---- chunk plan (PURE) ---------------------------------------------------------------

def plan_chunks(total_frames: int, chunk_len: int = CHUNK_FRAMES) -> list:
    """[(start_frame, n_frames), ...] — sequential windows; a tail shorter than
    MIN_TAIL_FRAMES folds into the previous chunk (WanVaceToVideo takes any length)."""
    if total_frames <= 0:
        return []
    out, start = [], 0
    while start < total_frames:
        n = min(chunk_len, total_frames - start)
        out.append((start, n))
        start += n
    if len(out) >= 2 and out[-1][1] < MIN_TAIL_FRAMES:
        s, n = out[-2]
        out[-2] = (s, n + out[-1][1])
        out.pop()
    return out


# ---- workflow graph (PURE) -----------------------------------------------------------

def build_outpaint_graph(*, input_name: str, canvas_w: int, canvas_h: int,
                         pad_left: int, pad_right: int, length: int, fps: float,
                         seed: int, filename_prefix: str,
                         prompt: str = DEFAULT_PROMPT,
                         negative: str = DEFAULT_NEGATIVE) -> dict:
    """The API-format prompt graph — the bundled template's 1.3B/CausVid path with the
    demo I/O swapped for LoadVideo -> ... -> VHS_VideoCombine (flat widgets; core
    SaveVideo's DynamicCombo codec input is hostile to hand-serialization).
    ImagePadForOutpaint emits ONE 2D mask regardless of batch — hence the
    MaskToImage -> RepeatImageBatch(length) -> ImageToMask chain (as the template does)."""
    vace = MODELS["borders_vace"]["rel"].split("/")[-1]
    umt5 = MODELS["borders_umt5"]["rel"].split("/")[-1]
    lora = MODELS["borders_causvid"]["rel"].split("/")[-1]
    vae = VAE_REL.split("/")[-1]
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": input_name}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "ImagePadForOutpaint",
              "inputs": {"image": ["2", 0], "left": pad_left, "top": 0,
                         "right": pad_right, "bottom": 0, "feathering": 0}},
        "4": {"class_type": "MaskToImage", "inputs": {"mask": ["3", 1]}},
        "5": {"class_type": "RepeatImageBatch", "inputs": {"image": ["4", 0],
                                                           "amount": length}},
        "6": {"class_type": "ImageToMask", "inputs": {"image": ["5", 0],
                                                      "channel": "red"}},
        "7": {"class_type": "UNETLoader", "inputs": {"unet_name": vace,
                                                     "weight_dtype": "default"}},
        "8": {"class_type": "CLIPLoader", "inputs": {"clip_name": umt5, "type": "wan",
                                                     "device": "default"}},
        "9": {"class_type": "LoraLoader",
              "inputs": {"model": ["7", 0], "clip": ["8", 0], "lora_name": lora,
                         "strength_model": LORA_STRENGTH_MODEL,
                         "strength_clip": LORA_STRENGTH_CLIP}},
        "10": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["9", 0],
                                                            "shift": SHIFT}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["9", 1],
                                                          "text": prompt}},
        "12": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["9", 1],
                                                          "text": negative}},
        "13": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "14": {"class_type": "WanVaceToVideo",
               "inputs": {"positive": ["11", 0], "negative": ["12", 0],
                          "vae": ["13", 0], "width": canvas_w, "height": canvas_h,
                          "length": length, "batch_size": 1, "strength": 1.0,
                          "control_video": ["3", 0], "control_masks": ["6", 0]}},
        "15": {"class_type": "KSampler",
               "inputs": {"model": ["10", 0], "seed": seed, "steps": STEPS, "cfg": CFG,
                          "sampler_name": SAMPLER, "scheduler": SCHEDULER,
                          "positive": ["14", 0], "negative": ["14", 1],
                          "latent_image": ["14", 2], "denoise": 1.0}},
        "16": {"class_type": "TrimVideoLatent", "inputs": {"samples": ["15", 0],
                                                           "trim_amount": ["14", 3]}},
        "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0],
                                                     "vae": ["13", 0]}},
        "18": {"class_type": "VHS_VideoCombine",
               "inputs": {"images": ["17", 0], "frame_rate": fps, "loop_count": 0,
                          "filename_prefix": filename_prefix,
                          "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 10,
                          "save_metadata": False, "pingpong": False,
                          "save_output": True}},
    }


# ---- ffmpeg builders (PURE) ----------------------------------------------------------

def chunk_extract_cmd(src: str, dst: str, start: int, nframes: int, geom: dict,
                      ffmpeg: str = FFMPEG) -> list:
    """One chunk of the source, downscaled to the model's 4:3 working core. No audio."""
    vf = (f"trim=start_frame={start}:end_frame={start + nframes},setpts=PTS-STARTPTS,"
          f"scale={geom['work_core_w']}:{geom['work_h']}:flags=lanczos,setsar=1")
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", src,
            "-vf", vf, "-an", "-c:v", "libx264", "-crf", "10", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", dst]


def composite_cmd(src: str, gen: str, dst: str, start: int, nframes: int, geom: dict,
                  ffmpeg: str = FFMPEG) -> list:
    """[generated LEFT strip | ORIGINAL full-res frames | generated RIGHT strip].
    The original passes through at its display size (setsar=1 normalizes anamorphic);
    only the strips are upscaled from working res. Inner-aligned crops keep the
    seam-adjacent generated pixels."""
    g = geom
    fc = (
        f"[0:v]trim=start_frame={start}:end_frame={start + nframes},"
        f"setpts=PTS-STARTPTS,scale={g['disp_w']}:{g['src_h']}:flags=lanczos,setsar=1[orig];"
        f"[1:v]crop={g['crop_w_work']}:{g['canvas_h']}:{g['crop_left_x']}:0,"
        f"scale={g['strip_w']}:{g['src_h']}:flags=lanczos,setsar=1[L];"
        f"[1:v]crop={g['crop_w_work']}:{g['canvas_h']}:{g['crop_right_x']}:0,"
        f"scale={g['strip_w']}:{g['src_h']}:flags=lanczos,setsar=1[R];"
        f"[L][orig][R]hstack=inputs=3,format=yuv420p[out]"
    )
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", src, "-i", gen, "-filter_complex", fc, "-map", "[out]",
            "-c:v", "libx264", "-crf", "12", "-preset", "medium", dst]


def concat_cmd(list_file: str, dst: str, ffmpeg: str = FFMPEG) -> list:
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", dst]


def count_frames(path: str, ffprobe: str = FFPROBE) -> int:
    try:
        r = subprocess.run([ffprobe, "-v", "quiet", "-select_streams", "v:0",
                            "-count_packets", "-show_entries", "stream=nb_read_packets",
                            "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=600)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


# ---- learned pace --------------------------------------------------------------------

def add_pace_sample(sec_per_chunk: float) -> None:
    try:
        d = {}
        if os.path.exists(PACE_FILE):
            with open(PACE_FILE) as f:
                d = json.load(f) or {}
        samples = [float(x) for x in d.get("spc", [])][-29:] + [float(sec_per_chunk)]
        os.makedirs(os.path.dirname(PACE_FILE), exist_ok=True)
        tmp = PACE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"spc": samples}, f)
        os.replace(tmp, PACE_FILE)
    except Exception:
        pass


def avg_sec_per_chunk():
    """Median of the recorded samples, or None before any run."""
    try:
        with open(PACE_FILE) as f:
            s = sorted(float(x) for x in (json.load(f).get("spc") or []))
        return s[len(s) // 2] if s else None
    except Exception:
        return None


# ---- backend + client ----------------------------------------------------------------

_LIVE_BACKENDS = []


def _atexit_kill():
    for b in list(_LIVE_BACKENDS):
        try:
            b.stop()
        except Exception:
            pass


atexit.register(_atexit_kill)


def reclaim_port(port) -> bool:
    """Kill whatever listens on OUR dedicated port. 8189 belongs to Visionary alone
    (the Desktop app's own 8188 is NEVER touched — callers must not pass it), so a
    listener there is a stale orphan of a hard-killed run (SIGKILL skips atexit).
    Returns True if the port came free."""
    if int(port) == 8188:
        return False
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-ti", f"tcp:{int(port)}"],
                           capture_output=True, text=True, timeout=10)
        for pid in {int(x) for x in r.stdout.split() if x.strip()}:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        for _ in range(10):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{int(port)}/system_stats", timeout=1):
                    time.sleep(0.5)                    # still answering — not dead yet
            except Exception:
                return True
    except Exception:
        pass
    return False


class ComfyBackend:
    """The headless ComfyUI server as a child process — per-job input/output dirs so
    ComfyUI-Shared stays untouched and outputs are deterministic."""

    def __init__(self, env: dict, in_dir: str, out_dir: str):
        self.env = env
        self.in_dir, self.out_dir = in_dir, out_dir
        self.base = f"http://127.0.0.1:{env['port']}"
        self.proc = None
        self.tail = []

    def port_free(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/system_stats", timeout=2):
                return False                       # something already answers
        except Exception:
            return True

    def start(self):
        os.makedirs(self.in_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)
        argv = [self.env["venv_python"], "main.py",
                "--port", str(self.env["port"]), "--listen", "127.0.0.1",
                "--models-directory", self.env["models_dir"],
                "--input-directory", self.in_dir,
                "--output-directory", self.out_dir,
                "--disable-auto-launch"]
        self.proc = subprocess.Popen(argv, cwd=self.env["checkout"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, start_new_session=True)
        _LIVE_BACKENDS.append(self)
        threading.Thread(target=self._pump, daemon=True,
                         name="comfy-backend-tail").start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.tail.append(line.rstrip("\n"))
                del self.tail[:-200]
        except Exception:
            pass

    def wait_ready(self, timeout: float = 180.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.proc and self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.base + "/system_stats", timeout=2):
                    return True
            except Exception:
                time.sleep(0.5)
        return False

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def stop(self):
        p, self.proc = self.proc, None
        if self in _LIVE_BACKENDS:
            _LIVE_BACKENDS.remove(self)
        if not p:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            for _ in range(20):
                if p.poll() is not None:
                    return
                time.sleep(0.5)
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class ComfyClient:
    def __init__(self, base: str):
        self.base = base
        self.client_id = str(uuid.uuid4())

    def submit(self, graph: dict) -> str:
        body = json.dumps({"prompt": graph, "client_id": self.client_id}).encode()
        req = urllib.request.Request(self.base + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())["prompt_id"]
        except urllib.error.HTTPError as e:
            detail = e.read(2000).decode("utf-8", "replace")
            raise RuntimeError(f"ComfyUI rejected the workflow: {detail[:400]}") from None

    def interrupt(self):
        try:
            req = urllib.request.Request(self.base + "/interrupt", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception:
            pass

    def wait(self, prompt_id: str, *, backend=None, abort=None,
             timeout_s: float = 3600.0) -> dict:
        """Poll history until the prompt completes. Raises on node errors, backend
        death (MPS OOM lands in the backend tail), abort, or timeout."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if abort is not None and abort.is_set():
                self.interrupt()
                raise RuntimeError("aborted")
            if backend is not None and not backend.alive():
                tail = "\n".join((backend.tail or [])[-6:])
                raise RuntimeError(f"ComfyUI backend died mid-generation:\n{tail}")
            try:
                with urllib.request.urlopen(f"{self.base}/history/{prompt_id}",
                                            timeout=10) as r:
                    hist = json.loads(r.read().decode()).get(prompt_id)
            except Exception:
                hist = None
            if hist:
                status = (hist.get("status") or {})
                if status.get("status_str") == "error":
                    msgs = [m for m in (status.get("messages") or [])
                            if m and m[0] == "execution_error"]
                    detail = (msgs[0][1].get("exception_message", "")
                              if msgs and len(msgs[0]) > 1 else "node error")
                    raise RuntimeError(f"ComfyUI execution error: {detail[:400]}")
                if status.get("completed"):
                    return hist
            time.sleep(1.0)
        raise RuntimeError("ComfyUI generation timed out")

    @staticmethod
    def output_file(hist: dict, out_dir: str):
        """The VHS_VideoCombine output path from a completed history entry."""
        for node_out in (hist.get("outputs") or {}).values():
            for key in ("gifs", "images", "video"):
                for f in node_out.get(key) or []:
                    name = f.get("filename")
                    if name and name.lower().endswith((".mp4", ".mkv", ".webm")):
                        sub = f.get("subfolder") or ""
                        return os.path.join(out_dir, sub, name)
        return None
