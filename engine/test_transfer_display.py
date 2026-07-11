"""display_name(): FTP wire names (GB18030 carried in latin-1) decode for DISPLAY only."""
import unittest
import transfer


class DisplayName(unittest.TestCase):
    def test_wire_gb18030_decodes(self):
        # the real observed name: ＂ (fullwidth quote) and ’ over the wire
        wire = 'This £¢wizard£¢ thinks he ISN¡¯T getting the death penalty.'
        self.assertEqual(transfer.display_name(wire),
                         'This ＂wizard＂ thinks he ISN’T getting the death penalty.')

    def test_ascii_passes_through(self):
        s = "The Office  Superfan Episodes S04e15 Night Out (Extended Cut).mp4"
        self.assertEqual(transfer.display_name(s), s)

    def test_real_unicode_passes_through(self):
        s = "Amélie — café ’ already decoded ＂"   # not latin-1 encodable
        self.assertEqual(transfer.display_name(s), s)

    def test_none_and_empty(self):
        self.assertIsNone(transfer.display_name(None))
        self.assertEqual(transfer.display_name(""), "")

    def test_undecodable_bytes_return_original(self):
        s = "bad \x81\x00 tail"           # invalid in both utf-8 and gb18030
        self.assertEqual(transfer.display_name(s), s)


if __name__ == "__main__":
    unittest.main()
