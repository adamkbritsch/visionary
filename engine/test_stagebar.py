import unittest

import stagebar


class Fields(unittest.TestCase):
    def test_a_stage_with_a_number(self):
        self.assertEqual(stagebar.fields(["a", "b"], 1, 41.234, notches=[0.5, 1], seg_done=1,
                                         sub="attempt 2"),
                         {"stages": ["a", "b"], "stage_idx": 1, "stage_pct": 41.2,
                          "stage_notches": [0.5, 1.0], "stage_seg_done": 1, "stage_sub": "attempt 2",
                          "stage_about": ["", ""]})

    def test_no_number_means_no_stage_pct(self):
        self.assertEqual(stagebar.fields(("a",), 0),
                         {"stages": ["a"], "stage_idx": 0, "stage_about": [""]})

    def test_the_number_is_clamped(self):
        self.assertEqual(stagebar.fields(["a"], 0, 140)["stage_pct"], 100.0)
        self.assertEqual(stagebar.fields(["a"], 0, -3)["stage_pct"], 0.0)

    def test_every_field_is_listed(self):
        d = stagebar.fields(["a"], 0, 1, notches=[1], seg_done=0, sub="x")
        self.assertEqual(set(d), set(stagebar.FIELDS))


class Steps(unittest.TestCase):
    def test_letters_follow_the_announced_plan(self):
        s = stagebar.Steps()
        self.assertFalse(s.active)
        s.plan(["peaks", "copy", "mux"])
        self.assertEqual(s.at("peaks")["stage_idx"], 0)
        self.assertEqual(s.at("copy", 12)["stage_idx"], 1)
        self.assertTrue(s.active)

    def test_a_new_plan_replaces_only_what_is_still_to_come(self):
        s = stagebar.Steps()
        s.plan(["peaks", "extract", "copy", "mux"])
        s.at("peaks")
        s.plan(["copy", "mux"])                     # the callee knows better: no extraction
        self.assertEqual(s.labels, ["peaks", "copy", "mux"])
        self.assertEqual(s.at("copy")["stage_idx"], 1)

    def test_an_unplanned_step_goes_after_the_current_one(self):
        s = stagebar.Steps(["peaks", "mux"])
        s.at("peaks")
        d = s.at("planning")
        self.assertEqual(d["stages"], ["peaks", "planning", "mux"])
        self.assertEqual(d["stage_idx"], 1)
        self.assertEqual(s.at("mux")["stage_idx"], 2)

    def test_the_first_unplanned_step_is_stage_a(self):
        s = stagebar.Steps()
        self.assertEqual(s.at("copy")["stage_idx"], 0)

    def test_a_shown_letter_never_moves(self):
        s = stagebar.Steps(["a", "b", "c"])
        s.at("a"); s.at("b")
        s.plan(["x", "c"])
        self.assertEqual(s.labels[:2], ["a", "b"])
        self.assertEqual(s.at("b")["stage_idx"], 1)


if __name__ == "__main__":
    unittest.main()


class EveryStageSaysWhatItIs(unittest.TestCase):
    """The app's tooltip on a stage's section of the bar is ABOUT[label] (user, 2026-10-20):
    every label a step can publish must have one."""

    def test_every_label_has_a_sentence(self):
        import nativelane, nativerender, remux, stages
        from unittest import mock
        labels = set(nativelane.LANE_STAGES)
        for sdr, keep in ((False, False), (True, False), (False, True)):
            labels |= set(nativerender.render_stages(mock.Mock(sdr=sdr, keep_video=keep)))
        labels |= set(stages.DOWNLOAD_STAGES) | set(stages.COMBINE_STAGES)
        labels |= {stages.PEAKS_STAGE, stages.PLAN_STAGE, stages.CAPPED_STAGE}
        for extract in (True, False):
            for skip, conv in ((False, False), (True, False), (True, True)):
                labels |= set(remux.inject_stages(extract, skip_inject=skip, convert_es=conv))
        labels |= set(remux.SHIP_STAGES) | set(remux.MUX_STAGES)
        labels |= set(remux.splice_stages(True)) | set(remux.splice_stages(False))
        missing = sorted(l for l in labels if not stagebar.ABOUT.get(l))
        self.assertEqual(missing, [])

    def test_the_sentences_ride_along(self):
        d = stagebar.fields(["Measuring every shot", "unknown"], 0)
        self.assertEqual(d["stage_about"], [stagebar.ABOUT["Measuring every shot"], ""])
