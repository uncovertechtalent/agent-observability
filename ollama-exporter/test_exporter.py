"""Offline test of the resident check. Run inside the image: docker run --rm <image> python test_exporter.py"""
import unittest

from exporter import resident_loaded

PS = [{"name": "abl-big:latest", "model": "abl-big:latest", "size": 10, "size_vram": 10},
      {"name": "other:7b", "model": "other:7b", "size": 5, "size_vram": 5}]


class Resident(unittest.TestCase):
    def test_loaded(self):
        self.assertEqual(resident_loaded(PS, "abl-big:latest"), 1)

    def test_name_without_tag_means_latest(self):
        # the 2026-11-22 trap: a resident id without ":latest" never matched
        self.assertEqual(resident_loaded(PS, "abl-big"), 1)

    def test_not_loaded(self):
        self.assertEqual(resident_loaded(PS, "abl-small:latest"), 0)
        self.assertEqual(resident_loaded([], "abl-big:latest"), 0)

    def test_other_tag_is_not_the_resident(self):
        self.assertEqual(resident_loaded(PS, "other:latest"), 0)


if __name__ == "__main__":
    unittest.main()
