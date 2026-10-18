import unittest

from bosshunter.collection.capabilities import platform_supports


class PlatformCapabilitiesTests(unittest.TestCase):


    def test_unknown_platform_supports_nothing(self):
        self.assertFalse(platform_supports("unknown", "collect"))
        self.assertFalse(platform_supports("nonexistent", "score"))

    def test_unknown_capability_returns_false(self):
        self.assertFalse(platform_supports("boss", "nonexistent"))


if __name__ == "__main__":
    unittest.main()