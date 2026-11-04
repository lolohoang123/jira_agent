import unittest
from util.common import validate_duration

class DurationTests(unittest.TestCase):
    def test_valid_duration(self):
        self.assertEqual(validate_duration("1h 30m"),"1h 30m")
    def test_invalid_duration(self):
        with self.assertRaises(ValueError): validate_duration("tomorrow")

if __name__=="__main__": unittest.main()
