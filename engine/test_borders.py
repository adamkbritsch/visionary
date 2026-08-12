"""Tests for borders.py — the pure core (geometry, chunk plan, graph, ffmpeg argv,
discovery, model registry) plus a urllib-mocked ComfyClient. No network, no Comfy."""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

import borders


class Geometry(unittest.TestCase):
    def test_square_pixel_4x3_sources_accepted(self):
        for w, h in ((640, 480), (720, 540), (1440, 1080)):
            g = borders.plan_geometry(w, h)
            self.assertNotIn("error", g, f"{w}x{h} rejected: {g}")
            self.assertEqual(g["src_w"], w)
            self.assertEqual(g["disp_w"], w)          # square pixels: display == storage

    def test_anamorphic_dv_accepted_via_sar(self):
        # NTSC DV: 720x480 storage, SAR 8:9 -> 640x480 display = 4:3.
        g = borders.plan_geometry(720, 480, 8, 9)
        self.assertNotIn("error", g)
        self.assertEqual(g["disp_w"], 640)            # normalized to display width
        self.assertEqual(g["src_w"], 720)

    def test_16x9_rejected(self):
        g = borders.plan_geometry(1920, 1080)
        self.assertEqual(g["error"], "not-4x3")
        self.assertIn("already 16:9", g["detail"])

    def test_odd_aspect_rejected(self):
        g = borders.plan_geometry(1000, 480)          # ~2.08:1
        self.assertEqual(g["error"], "not-4x3")

    def test_bad_dims_rejected(self):
        self.assertEqual(borders.plan_geometry(0, 480)["error"], "bad-dims")
        self.assertEqual(borders.plan_geometry(640, 480, 0, 1)["error"], "bad-dims")

    def test_full_res_strips_land_on_16x9(self):
        g = borders.plan_geometry(720, 540)
        self.assertEqual(g["final_w"], g["disp_w"] + 2 * g["strip_w"])
        self.assertAlmostEqual(g["final_w"] / g["src_h"], 16 / 9, delta=0.02)
        self.assertEqual(g["strip_w"] % 2, 0)

    def test_canvas_multiples_and_inner_aligned_crops(self):
        for args in ((640, 480, 1, 1), (720, 540, 1, 1), (1440, 1080, 1, 1),
                     (720, 480, 8, 9), (704, 528, 1, 1), (960, 720, 1, 1)):
            g = borders.plan_geometry(*args)
            self.assertNotIn("error", g, f"{args} rejected: {g}")
            self.assertEqual(g["canvas_w"] % 16, 0, f"{args}: canvas_w {g['canvas_w']}")
            self.assertEqual(g["canvas_h"] % 16, 0)
            self.assertEqual(g["pad_work"] % 8, 0)
            self.assertEqual(g["canvas_w"],
                             g["work_core_w"] + 2 * g["pad_work"])
            # Inner alignment: the left crop ENDS at the core's left edge; the right
            # crop STARTS at the core's right edge; both stay inside the canvas.
            self.assertEqual(g["crop_left_x"] + g["crop_w_work"], g["pad_work"])
            self.assertEqual(g["crop_right_x"], g["pad_work"] + g["work_core_w"])
            self.assertGreaterEqual(g["crop_left_x"], 0)
            self.assertLessEqual(g["crop_right_x"] + g["crop_w_work"], g["canvas_w"])
            self.assertEqual(g["crop_w_work"] % 2, 0)

    def test_work_height_bounded_by_source(self):
        self.assertEqual(borders.plan_geometry(640, 480)["work_h"], 480)
        self.assertEqual(borders.plan_geometry(1440, 1080)["work_h"], 480)
        g = borders.plan_geometry(432, 320)           # below 480p: don't upscale to work
        self.assertLessEqual(g["work_h"], 320)


class ChunkPlan(unittest.TestCase):
    def test_exact_multiple(self):
        self.assertEqual(borders.plan_chunks(162), [(0, 81), (81, 81)])

    def test_remainder_kept(self):
        self.assertEqual(borders.plan_chunks(200), [(0, 81), (81, 81), (162, 38)])

    def test_short_tail_folds_into_previous(self):
        self.assertEqual(borders.plan_chunks(165), [(0, 81), (81, 84)])
        self.assertEqual(borders.plan_chunks(85), [(0, 85)])

    def test_single_short_clip_stays(self):
        self.assertEqual(borders.plan_chunks(5), [(0, 5)])

    def test_empty(self):
        self.assertEqual(borders.plan_chunks(0), [])
        self.assertEqual(borders.plan_chunks(-3), [])

    def test_coverage_is_gapless(self):
        for total in (81, 82, 100, 500, 1234):
            plan = borders.plan_chunks(total)
            self.assertEqual(plan[0][0], 0)
            self.assertEqual(sum(n for _, n in plan), total)
            for (s1, n1), (s2, _) in zip(plan, plan[1:]):
                self.assertEqual(s1 + n1, s2)


class GraphBuilder(unittest.TestCase):
    def graph(self, **over):
        kw = dict(input_name="chunk_000.mp4", canvas_w=880, canvas_h=480,
                  pad_left=120, pad_right=120, length=81, fps=23.976,
                  seed=1234, filename_prefix="out_000")
        kw.update(over)
        return borders.build_outpaint_graph(**kw)

    def test_all_template_nodes_present(self):
        types = {n["class_type"] for n in self.graph().values()}
        self.assertEqual(types, {
            "LoadVideo", "GetVideoComponents", "ImagePadForOutpaint", "MaskToImage",
            "RepeatImageBatch", "ImageToMask", "UNETLoader", "CLIPLoader",
            "LoraLoader", "ModelSamplingSD3", "CLIPTextEncode", "VAELoader",
            "WanVaceToVideo", "KSampler", "TrimVideoLatent", "VAEDecode",
            "VHS_VideoCombine"})

    def test_sampler_params_match_template(self):
        ks = self.graph()["15"]["inputs"]
        self.assertEqual(ks["steps"], 3)
        self.assertEqual(ks["cfg"], 1.0)
        self.assertEqual(ks["sampler_name"], "uni_pc")
        self.assertEqual(ks["scheduler"], "simple")
        self.assertEqual(ks["denoise"], 1.0)
        self.assertEqual(ks["seed"], 1234)
        g = self.graph()
        self.assertEqual(g["10"]["inputs"]["shift"], 8.0)
        self.assertEqual(g["9"]["inputs"]["strength_model"], 0.7)
        self.assertEqual(g["9"]["inputs"]["strength_clip"], 1.0)

    def test_pad_and_mask_chain(self):
        g = self.graph(pad_left=112, pad_right=112, length=42)
        pad = g["3"]["inputs"]
        self.assertEqual((pad["left"], pad["right"]), (112, 112))
        self.assertEqual((pad["top"], pad["bottom"]), (0, 0))
        self.assertEqual(pad["feathering"], 0)
        # ImagePadForOutpaint emits ONE 2D mask -> repeated to the batch length.
        self.assertEqual(g["5"]["inputs"]["amount"], 42)
        self.assertEqual(g["14"]["inputs"]["length"], 42)

    def test_canvas_and_io(self):
        g = self.graph(canvas_w=880, canvas_h=480, fps=29.97,
                       input_name="c7.mp4", filename_prefix="gen_007", seed=99)
        wan = g["14"]["inputs"]
        self.assertEqual((wan["width"], wan["height"]), (880, 480))
        self.assertEqual(g["1"]["inputs"]["file"], "c7.mp4")
        vc = g["18"]["inputs"]
        self.assertEqual(vc["filename_prefix"], "gen_007")
        self.assertEqual(vc["frame_rate"], 29.97)
        self.assertEqual(vc["format"], "video/h264-mp4")

    def test_model_filenames_and_clip_type(self):
        g = self.graph()
        self.assertEqual(g["7"]["inputs"]["unet_name"],
                         "wan2.1_vace_1.3B_fp16.safetensors")
        self.assertEqual(g["8"]["inputs"]["type"], "wan")
        self.assertIn("CausVid", g["9"]["inputs"]["lora_name"])
        self.assertEqual(g["13"]["inputs"]["vae_name"], "wan_2.1_vae.safetensors")

    def test_prompts(self):
        g = self.graph(prompt="pos text", negative="neg text")
        texts = {g["11"]["inputs"]["text"], g["12"]["inputs"]["text"]}
        self.assertEqual(texts, {"pos text", "neg text"})
        self.assertEqual(self.graph()["12"]["inputs"]["text"],
                         "bad quality, blurry, messy, chaotic")


class FfmpegBuilders(unittest.TestCase):
    def setUp(self):
        self.g = borders.plan_geometry(720, 540)

    def test_chunk_extract(self):
        cmd = borders.chunk_extract_cmd("/s.mp4", "/d.mp4", 81, 81, self.g)
        joined = " ".join(cmd)
        self.assertIn("trim=start_frame=81:end_frame=162", joined)
        self.assertIn(f"scale={self.g['work_core_w']}:{self.g['work_h']}", joined)
        self.assertIn("-an", cmd)
        self.assertEqual(cmd[-1], "/d.mp4")

    def test_composite(self):
        g = self.g
        cmd = borders.composite_cmd("/s.mp4", "/gen.mp4", "/w.mp4", 0, 81, g)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("hstack=inputs=3", fc)
        self.assertIn(f"scale={g['disp_w']}:{g['src_h']}", fc)          # original lane
        self.assertIn(f"crop={g['crop_w_work']}:{g['canvas_h']}:{g['crop_left_x']}:0",
                      fc)
        self.assertIn(f"crop={g['crop_w_work']}:{g['canvas_h']}:{g['crop_right_x']}:0",
                      fc)
        self.assertIn(f"scale={g['strip_w']}:{g['src_h']}", fc)          # strip upscale
        self.assertIn("[L][orig][R]", fc)
        self.assertIn("-map", cmd)

    def test_concat(self):
        cmd = borders.concat_cmd("/list.txt", "/out.mp4")
        self.assertIn("concat", cmd)
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[-1], "/out.mp4")


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-disc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = os.path.join(self.tmp, "settings.json")
        self.install = os.path.join(self.tmp, "install")
        self.checkout = os.path.join(self.install, "ComfyUI", "ComfyUI")
        os.makedirs(os.path.join(self.checkout, ".venv", "bin"))
        open(os.path.join(self.checkout, "main.py"), "w").close()
        open(os.path.join(self.checkout, ".venv", "bin", "python"), "w").close()
        with open(os.path.join(self.checkout, "comfyui_version.py"), "w") as f:
            f.write('__version__ = "0.30.2"\n')
        with open(self.settings, "w") as f:
            json.dump({"installDir": self.install,
                       "modelsDirs": [os.path.join(self.tmp, "shared-models")]}, f)
        patches = [
            mock.patch.object(borders, "DESKTOP_SETTINGS", self.settings),
            mock.patch.object(borders, "DESKTOP_APP_PLIST",
                              os.path.join(self.tmp, "no.plist")),
            mock.patch.object(borders, "CONFIG_FILE",
                              os.path.join(self.tmp, "no-config.json")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _add_vhs(self):
        os.makedirs(os.path.join(self.checkout, "custom_nodes",
                                 "comfyui-videohelpersuite"), exist_ok=True)

    def test_videohelpersuite_is_a_named_requirement(self):
        # NOT bundled with Comfy Desktop (its custom_nodes ships only
        # websocket_image_save.py), and the graph's output node needs it — so a missing
        # VHS must be named in Setup, not discovered at the first chunk of an overnight run.
        os.makedirs(os.path.join(self.checkout, "custom_nodes"), exist_ok=True)
        d = borders.discover()
        self.assertTrue(d["ok"], "ComfyUI itself is still usable")
        self.assertFalse(d["vhs"])
        self.assertTrue(any("VideoHelperSuite" in m for m in d["missing"]))
        ready, missing = borders.env_ready(d, models_dir=self.tmp)
        self.assertFalse(ready)
        self.assertTrue(any("VideoHelperSuite" in m for m in missing))
        self._add_vhs()
        self.assertTrue(borders.discover()["vhs"])

    def test_env_ready_needs_comfy_and_the_node_and_the_models(self):
        self._add_vhs()
        env = borders.discover()
        ready, missing = borders.env_ready(env, models_dir=self.tmp)
        self.assertFalse(ready)                       # node present, models absent
        self.assertTrue(all("VideoHelperSuite" not in m for m in missing))
        self.assertEqual(len(missing), len(borders.MODELS))
        # No ComfyUI at all -> the environment's own reasons, not model noise.
        os.remove(os.path.join(self.checkout, "main.py"))
        ready, missing = borders.env_ready(borders.discover(), models_dir=self.tmp)
        self.assertFalse(ready)
        self.assertTrue(any("checkout" in m for m in missing))

    def test_full_discovery(self):
        self._add_vhs()
        d = borders.discover()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["checkout"], self.checkout)
        self.assertTrue(d["venv_python"].endswith(".venv/bin/python"))
        self.assertEqual(d["models_dir"], os.path.join(self.tmp, "shared-models"))
        self.assertEqual(d["comfy_version"], "0.30.2")
        self.assertEqual(d["port"], borders.COMFY_PORT)

    def test_no_desktop_settings(self):
        os.remove(self.settings)
        d = borders.discover()
        self.assertFalse(d["ok"])
        self.assertTrue(any("Comfy Desktop" in m for m in d["missing"]))

    def test_missing_venv(self):
        os.remove(os.path.join(self.checkout, ".venv", "bin", "python"))
        d = borders.discover()
        self.assertFalse(d["ok"])
        self.assertTrue(any("venv" in m for m in d["missing"]))

    def test_config_overrides(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w") as f:
            json.dump({"comfy_dir": self.install, "comfy_port": 9911}, f)
        os.remove(self.settings)                     # override wins without settings
        with mock.patch.object(borders, "CONFIG_FILE", cfg):
            d = borders.discover()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["port"], 9911)
        # No modelsDirs from settings -> falls back to <install>/models.
        self.assertEqual(d["models_dir"], os.path.join(self.install, "models"))


class Models(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-models-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _put(self, rel, nbytes):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * nbytes)

    def test_sizes_are_pinned(self):
        for what, m in borders.MODELS.items():
            self.assertIsInstance(m["expected_bytes"], int, what)
            self.assertGreater(m["expected_bytes"], 0, what)
            self.assertTrue(m["url"].startswith("https://"), what)

    def test_states(self):
        with mock.patch.dict(borders.MODELS["borders_vace"], {"expected_bytes": 10}):
            st = borders.model_status(self.tmp)
            self.assertEqual(st["borders_vace"]["state"], "missing")
            self._put(borders.MODELS["borders_vace"]["rel"], 5)
            st = borders.model_status(self.tmp)
            self.assertEqual(st["borders_vace"]["state"], "truncated")
            self._put(borders.MODELS["borders_vace"]["rel"], 10)
            st = borders.model_status(self.tmp)
            self.assertEqual(st["borders_vace"]["state"], "ok")

    def test_vae_is_a_full_model_row(self):
        st = borders.model_status(self.tmp)
        self.assertEqual(st["borders_vae"]["state"], "missing")
        self._put(borders.VAE_REL, 4)                    # stub != the pinned 253.8 MB
        self.assertEqual(borders.model_status(self.tmp)["borders_vae"]["state"],
                         "truncated")
        self.assertTrue(borders.model_download_argv("borders_vae", self.tmp))

    def test_models_ready(self):
        ready, missing = borders.models_ready(self.tmp)
        self.assertFalse(ready)
        self.assertEqual(len(missing), 4)            # three models + the VAE

    def test_download_argv(self):
        argv = borders.model_download_argv("borders_causvid", self.tmp)
        self.assertEqual(argv[0], "/usr/bin/curl")
        self.assertIn("-C", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "-")
        dest = argv[argv.index("-o") + 1]
        self.assertEqual(dest, os.path.join(
            self.tmp, borders.MODELS["borders_causvid"]["rel"]))
        self.assertEqual(argv[-1], borders.MODELS["borders_causvid"]["url"])
        self.assertIsNone(borders.model_download_argv("nope", self.tmp))
        self.assertIsNone(borders.model_download_argv("borders_vace", ""))


class Pace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-pace-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(borders, "PACE_FILE",
                              os.path.join(self.tmp, "pace.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_empty(self):
        self.assertIsNone(borders.avg_sec_per_chunk())

    def test_median_and_cap(self):
        for v in (10, 100, 20):
            borders.add_pace_sample(v)
        self.assertEqual(borders.avg_sec_per_chunk(), 20)     # median, not mean
        for v in range(40):
            borders.add_pace_sample(50)
        with open(borders.PACE_FILE) as f:
            self.assertEqual(len(json.load(f)["spc"]), 30)    # last-30 window


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Client(unittest.TestCase):
    def setUp(self):
        self.c = borders.ComfyClient("http://127.0.0.1:8189")

    def test_submit(self):
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp({"prompt_id": "pid-1"})) as u:
            self.assertEqual(self.c.submit({"1": {}}), "pid-1")
        req = u.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/prompt"))
        body = json.loads(req.data.decode())
        self.assertIn("prompt", body)
        self.assertIn("client_id", body)

    def test_submit_rejected(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {},
                                     io.BytesIO(b'{"error": "invalid prompt"}'))
        with mock.patch.object(borders.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                self.c.submit({})

    def test_wait_completes(self):
        hist = {"pid": {"status": {"completed": True, "status_str": "success"},
                        "outputs": {}}}
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp(hist)):
            self.assertEqual(self.c.wait("pid")["status"]["completed"], True)

    def test_wait_surfaces_node_error(self):
        hist = {"pid": {"status": {
            "status_str": "error", "completed": False,
            "messages": [["execution_error",
                          {"exception_message": "MPS backend out of memory"}]]}}}
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp(hist)):
            with self.assertRaisesRegex(RuntimeError, "MPS backend out of memory"):
                self.c.wait("pid")

    def test_wait_surfaces_backend_death(self):
        backend = mock.Mock()
        backend.alive.return_value = False
        backend.tail = ["line1", "RuntimeError: MPS OOM"]
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp({})):
            with self.assertRaisesRegex(RuntimeError, "MPS OOM"):
                self.c.wait("pid", backend=backend)

    def test_wait_abort(self):
        ev = mock.Mock()
        ev.is_set.return_value = True
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp({})):
            with self.assertRaisesRegex(RuntimeError, "aborted"):
                self.c.wait("pid", abort=ev)

    def test_wait_timeout(self):
        with mock.patch.object(borders.urllib.request, "urlopen",
                               return_value=_Resp({})):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.c.wait("pid", timeout_s=0)

    def test_output_file(self):
        hist = {"outputs": {"18": {"gifs": [
            {"filename": "gen_000.mp4", "subfolder": "", "type": "output"}]}}}
        self.assertEqual(borders.ComfyClient.output_file(hist, "/out"),
                         "/out/gen_000.mp4")
        self.assertIsNone(borders.ComfyClient.output_file({"outputs": {}}, "/out"))


if __name__ == "__main__":
    unittest.main()


class AspectBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-aspect-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(borders, "ASPECT_FILE",
                              os.path.join(self.tmp, "aspects.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_parse_sar(self):
        self.assertEqual(borders.parse_sar("8:9"), (8, 9))
        self.assertEqual(borders.parse_sar("1:1"), (1, 1))
        for junk in ("", "0:1", "N/A", None, "x"):
            self.assertEqual(borders.parse_sar(junk), (1, 1))

    def test_aspect_label(self):
        self.assertEqual(borders.aspect_label(640, 480), "4:3")
        self.assertEqual(borders.aspect_label(720, 480, "8:9"), "4:3")
        self.assertEqual(borders.aspect_label(1920, 1080), "16:9")
        self.assertEqual(borders.aspect_label(1280, 536), "other")   # ~2.39:1
        self.assertIsNone(borders.aspect_label(0, 480))
        self.assertIsNone(borders.aspect_label(None, None))

    def test_book_roundtrip(self):
        self.assertIsNone(borders.show_aspect("Show"))
        borders.record_show_aspect("Show", "16:9")
        self.assertEqual(borders.show_aspect("Show"), "16:9")
        borders.record_show_aspect("Show", "garbage")            # junk never lands
        self.assertEqual(borders.show_aspect("Show"), "16:9")
        borders.record_show_aspect("", "4:3")
        self.assertEqual(borders.all_show_aspects(), {"Show": "16:9"})

    def test_4x3_is_sticky_for_mixed_aspect_shows(self):
        """It's Always Sunny: 4:3 through S05, 16:9 from S06 (live-verified). One label
        per show + last-probe-wins made the row vanish mid-show the moment a wide episode
        was probed. A show that ever probed 4:3 HAS 4:3 content — the label stays, and
        the per-episode gate keeps wide episodes skipping themselves."""
        borders.record_show_aspect("Sunny", "4:3")               # an S01 episode
        borders.record_show_aspect("Sunny", "16:9")              # an S06 episode
        self.assertEqual(borders.show_aspect("Sunny"), "4:3")    # row stays offered
        # ...and the upgrade direction still works: 16:9 first, 4:3 later.
        borders.record_show_aspect("Other", "16:9")
        borders.record_show_aspect("Other", "4:3")
        self.assertEqual(borders.show_aspect("Other"), "4:3")


class ExtendGate(unittest.TestCase):
    """The per-episode gate: TV-only, per-show flag, HDR exclusion, 4:3 acceptance —
    fails OPEN (not needed) so extend can never park an episode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "ep.mp4")
        with open(self.src, "w") as f:
            f.write("x")
        p = mock.patch.object(borders, "ASPECT_FILE",
                              os.path.join(self.tmp, "aspects.json"))
        p.start()
        self.addCleanup(p.stop)

    def _p(self, **kw):
        import types
        d = dict(series="Show", source=self.src, source_cfr="",
                 movie=False, youtube=False, combine=False)
        d.update(kw)
        return types.SimpleNamespace(**d)

    def _gate(self, p, *, flag=True, probe=None):
        import settings
        import plan
        probe = probe if probe is not None else {
            "width": 640, "height": 480, "sar": "", "is_hdr": False}
        with mock.patch.object(settings, "get_show_extend_borders",
                               return_value=flag), \
             mock.patch.object(plan, "probe_input", return_value=probe):
            return borders.extend_gate(p)

    def test_tv_episodes_only(self):
        for kw in ({"movie": True}, {"youtube": True}, {"combine": True}):
            g = self._gate(self._p(**kw))
            self.assertFalse(g["needed"])
            self.assertIn("TV episodes only", g["reason"])

    def test_flag_off(self):
        g = self._gate(self._p(), flag=False)
        self.assertFalse(g["needed"])
        self.assertIn("off for this show", g["reason"])

    def test_no_local_source(self):
        os.remove(self.src)
        self.assertFalse(self._gate(self._p())["needed"])

    def test_hdr_excluded(self):
        g = self._gate(self._p(), probe={"width": 640, "height": 480, "sar": "",
                                         "is_hdr": True})
        self.assertFalse(g["needed"])
        self.assertIn("SDR-only", g["reason"])

    def test_wide_episode_in_an_enabled_show_skips(self):
        g = self._gate(self._p(), probe={"width": 1920, "height": 1080, "sar": "",
                                         "is_hdr": False})
        self.assertFalse(g["needed"])
        self.assertEqual(borders.show_aspect("Show"), "16:9")    # probe still fed the book

    def test_4x3_accepted_with_geometry(self):
        g = self._gate(self._p())
        self.assertTrue(g["needed"])
        self.assertEqual(g["geom"]["final_w"], 852)
        self.assertEqual(borders.show_aspect("Show"), "4:3")

    def test_anamorphic_accepted_via_sar(self):
        g = self._gate(self._p(), probe={"width": 720, "height": 480, "sar": "8:9",
                                         "is_hdr": False})
        self.assertTrue(g["needed"])
        self.assertEqual(g["geom"]["disp_w"], 640)


class SnappedChunks(unittest.TestCase):
    """CONTINUITY tier 2: chunk boundaries snap to scene cuts (a wing reset at an edit is
    invisible) and chunks within one scene share a seed."""

    def test_no_cuts_degrades_to_plain_plan(self):
        for total in (81, 162, 170, 200, 1234):
            self.assertEqual(borders.plan_chunks_snapped(total, []),
                             borders.plan_chunks(total))
            self.assertEqual(borders.plan_chunks_snapped(total, None),
                             borders.plan_chunks(total))

    def test_cut_inside_the_window_ends_the_chunk(self):
        # cut at 60: chunk 1 ends there; the next starts exactly at the cut.
        plan = borders.plan_chunks_snapped(200, [60])
        self.assertEqual(plan[0], (0, 60))
        self.assertEqual(plan[1][0], 60)

    def test_last_cut_in_range_wins(self):
        plan = borders.plan_chunks_snapped(300, [30, 60, 75])
        self.assertEqual(plan[0], (0, 75))               # latest cut inside the window

    def test_cut_too_close_to_start_is_ignored(self):
        plan = borders.plan_chunks_snapped(200, [5])     # < MIN_TAIL_FRAMES from start
        self.assertEqual(plan[0], (0, 81))

    def test_cut_beyond_the_window_cannot_stretch_a_chunk(self):
        plan = borders.plan_chunks_snapped(300, [100])   # 81 is the model window
        self.assertEqual(plan[0], (0, 81))
        self.assertEqual(plan[1], (81, 19))              # then snaps to the cut

    def test_coverage_is_gapless_with_cuts(self):
        for total, cuts in ((200, [60]), (300, [30, 60, 75]), (500, [100, 101, 350]),
                            (1234, [81, 400, 1200])):
            plan = borders.plan_chunks_snapped(total, cuts)
            self.assertEqual(plan[0][0], 0)
            self.assertEqual(sum(n for _, n in plan), total)
            for (s1, n1), (s2, _) in zip(plan, plan[1:]):
                self.assertEqual(s1 + n1, s2)

    def test_scene_of(self):
        cuts = [100, 400]
        self.assertEqual(borders.scene_of(0, cuts), 0)
        self.assertEqual(borders.scene_of(99, cuts), 0)
        self.assertEqual(borders.scene_of(100, cuts), 1)   # a cut STARTS its scene
        self.assertEqual(borders.scene_of(399, cuts), 1)
        self.assertEqual(borders.scene_of(400, cuts), 2)
        self.assertEqual(borders.scene_of(50, []), 0)


class SetBook(unittest.TestCase):
    """CONTINUITY tier 3: the per-show set-reference book — recognize a set by dHash,
    remember its widened canvas frame, feed it back as WanVaceToVideo's reference."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="borders-setbook-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(borders, "SET_BOOK_ROOT", os.path.join(self.tmp, "book"))
        p.start()
        self.addCleanup(p.stop)

    def _png(self, name, reverse=False):
        import numpy as np
        import cv2
        path = os.path.join(self.tmp, name)
        row = np.linspace(0, 255, 64, dtype="uint8")
        if reverse:
            row = row[::-1]                       # dHash measures horizontal gradients
        cv2.imwrite(path, np.tile(row, (48, 1)))
        return path

    def test_dhash_and_hamming(self):
        a = borders.dhash_file(self._png("a.png"))
        b = borders.dhash_file(self._png("b.png"))            # identical content
        c = borders.dhash_file(self._png("c.png", reverse=True))
        self.assertIsNotNone(a)
        self.assertEqual(borders.hamming(a, b), 0)
        self.assertGreater(borders.hamming(a, c), borders.SET_MATCH_MAX_DIST)
        self.assertIsNone(borders.dhash_file(os.path.join(self.tmp, "missing.png")))

    def test_register_match_roundtrip(self):
        canvas = self._png("canvas.png")
        self.assertEqual(borders.load_set_book("Sunny"), [])
        e = borders.register_set("Sunny", 0xABCD, canvas)
        self.assertIsNotNone(e)
        self.assertTrue(os.path.exists(e["path"]))
        book = borders.load_set_book("Sunny")
        self.assertEqual(len(book), 1)
        self.assertEqual(borders.match_set(book, 0xABCD)["id"], e["id"])
        self.assertEqual(borders.match_set(book, 0xABCD ^ 0x3), book[0])   # 2 bits off
        self.assertIsNone(borders.match_set(book, 0xABCD ^ ((1 << 20) - 1)))  # 20 bits
        self.assertIsNone(borders.match_set(book, None))    # unhashable probe never matches
        self.assertEqual(borders.set_count("Sunny"), 1)
        self.assertEqual(borders.set_count("Other Show"), 0)   # books are per show

    def test_missing_reference_png_drops_the_entry(self):
        e = borders.register_set("S", 1, self._png("c1.png"))
        os.remove(e["path"])                       # the reference IS the value
        self.assertEqual(borders.load_set_book("S"), [])

    def test_cap_and_reset(self):
        canvas = self._png("c.png")
        with mock.patch.object(borders, "MAX_SETS_PER_SHOW", 2):
            self.assertIsNotNone(borders.register_set("S", 1, canvas))
            self.assertIsNotNone(borders.register_set("S", 2, canvas))
            self.assertIsNone(borders.register_set("S", 3, canvas))   # cap
        self.assertEqual(borders.reset_set_book("S"), (2, True))
        self.assertEqual(borders.set_count("S"), 0)
        self.assertEqual(borders.reset_set_book("S"), (0, True))   # idempotent

    def test_slug_never_escapes_and_never_collides(self):
        # ".." as a show name once resolved to ~/.topaz-pipeline itself — reset would
        # have rmtree'd the whole config dir (review-caught). And punctuation twins must
        # not share a book, or resetting one cross-wipes the other.
        for evil in ("..", ".", "...", "", "___"):
            d = borders.set_book_dir(evil)
            self.assertTrue(d.startswith(borders.SET_BOOK_ROOT + os.sep), evil)
            self.assertNotIn("..", os.path.relpath(d, borders.SET_BOOK_ROOT), evil)
        self.assertNotEqual(borders.set_book_dir("Show: Part 1"),
                            borders.set_book_dir("Show Part 1"))
        self.assertEqual(borders.set_book_dir("Sunny"), borders.set_book_dir("Sunny"))

    def test_extract_frame_cmd(self):
        cmd = borders.extract_frame_cmd("/c.mp4", "/f.png", 40)
        self.assertIn("select=eq(n\\,40)", " ".join(cmd))
        self.assertEqual(cmd[-1], "/f.png")

    def test_graph_reference_wiring(self):
        g = borders.build_outpaint_graph(
            input_name="c.mp4", canvas_w=880, canvas_h=480, pad_left=120,
            pad_right=120, length=81, fps=24.0, seed=1, filename_prefix="o",
            reference_name="setref_0001.png")
        self.assertEqual(g["19"]["class_type"], "LoadImage")
        self.assertEqual(g["19"]["inputs"]["image"], "setref_0001.png")
        self.assertEqual(g["14"]["inputs"]["reference_image"], ["19", 0])
        g2 = borders.build_outpaint_graph(
            input_name="c.mp4", canvas_w=880, canvas_h=480, pad_left=120,
            pad_right=120, length=81, fps=24.0, seed=1, filename_prefix="o")
        self.assertNotIn("19", g2)                            # absent, not disabled
        self.assertNotIn("reference_image", g2["14"]["inputs"])
