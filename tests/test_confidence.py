import json
import unittest

from book_translator.confidence import (
    Assessment,
    RegionAssessment,
    check_text_anchors,
    review_flags,
)
from book_translator.core import PageState, read_project, save_project
import test_core


class ConfidenceTests(unittest.TestCase):
    def test_unmatched_quote_cannot_draw_a_misleading_box(self):
        report = Assessment(
            "Translated text",
            0.9,
            [
                RegionAssessment(
                    "Invented quote", "Translated text", 0.6, "Uncertain", (10, 20, 30, 60)
                ),
                RegionAssessment(
                    "Original text", "Translated text", 0.7, "Uncertain", (50, 20, 80, 60)
                ),
            ],
        )
        check_text_anchors(report, "Original\ntext")
        self.assertEqual(report.regions[0].box, ())
        self.assertIn("did not match", report.regions[0].reason)
        self.assertEqual(report.regions[1].box, (50, 20, 80, 60))

    def test_valid_result_and_strict_bounds(self):
        payload = {
            "text": "Source",
            "confidence": 0.9,
            "regions": [
                {
                    "source_text": "Source",
                    "output_text": "Source",
                    "confidence": 0.4,
                    "reason": "Unclear",
                    "box": [10, 20, 100, 900],
                },
            ],
        }
        report = Assessment.from_dict(payload)
        self.assertEqual(report.regions[0].box, (10, 20, 100, 900))
        for bad in (True, -0.1, 1.1, float("nan"), float("inf"), "0.9"):
            with self.assertRaises(ValueError):
                Assessment.from_dict({**payload, "confidence": bad})
        for box in ([20, 10, 10, 100], [0, 0, 1001, 100], [1, 2, 3], [0, True, 10, 100]):
            invalid = json.loads(json.dumps(payload))
            invalid["regions"][0]["box"] = box
            with self.assertRaises(ValueError):
                Assessment.from_dict(invalid)

    def test_threshold_and_staleness(self):
        page = PageState(source="Source", translation="Translation", translated_from="Source")
        page.extraction_assessment = Assessment(
            "Source",
            0.9,
            [
                RegionAssessment("Source", "Source", 0.65, "Blurred", (10, 10, 100, 500)),
                RegionAssessment("Source", "Source", 0.85, "", (100, 10, 200, 500)),
            ],
        )
        page.translation_assessment = Assessment(
            "Translation",
            0.9,
            [
                RegionAssessment("Source", "Translation", 0.7, "Ambiguity", (10, 10, 100, 500)),
            ],
        )
        self.assertEqual(len(review_flags(page, 0.8)), 2)
        self.assertEqual(len(review_flags(page, 0.7)), 1)
        page.translation = "Manually revised"
        self.assertEqual(len(review_flags(page, 0.8)), 2)
        self.assertTrue(review_flags(page, 0.8)[1].outdated)
        page.source = "Manually revised source"
        self.assertEqual(len(review_flags(page, 0.8)), 2)
        self.assertTrue(all(flag.outdated for flag in review_flags(page, 0.8)))

    def test_low_overall_score_without_region_has_explicit_page_flag(self):
        page = PageState(source="Source", extraction_assessment=Assessment("Source", 0.5, []))
        flags = review_flags(page, 0.8)
        self.assertTrue(flags[0].page_level)
        self.assertEqual(flags[0].box, (0, 0, 1000, 1000))
        self.assertIn("whole-page", flags[0].reason)


class ConfidencePersistenceTests(unittest.TestCase):
    setUp = test_core.DocumentTests.setUp
    tearDown = test_core.DocumentTests.tearDown

    def test_assessments_and_threshold_round_trip(self):
        page = self.book.pages[0]
        page.source = "Source"
        page.extraction_assessment = Assessment(
            "Source",
            0.91,
            [
                RegionAssessment("Source", "Source", 0.6, "Faded letters", (100, 100, 200, 600)),
            ],
        )
        path = self.root / "v2.btproject"
        save_project(path, self.book, 0, "Vietnamese", "gemini-2.5-flash", 75)
        project = read_project(path)
        self.assertEqual(project.pages[0], page)
        self.assertEqual(project.confidence_threshold, 75)
        self.assertEqual(len(review_flags(project.pages[0], 0.75)), 1)

    def test_v1_project_still_opens(self):
        path = self.root / "v1.btproject"
        self.book.pages[0].source = "Preserved old project"
        save_project(path, self.book, 0, "Vietnamese", "gemini-2.5-flash")
        data = json.loads(path.read_text())
        data["version"] = 1
        del data["confidence_threshold"]
        for page in data["pages"]:
            page.pop("extraction_assessment")
            page.pop("translation_assessment")
        path.write_text(json.dumps(data))
        result = read_project(path)
        self.assertEqual(result.pages[0].source, "Preserved old project")
        self.assertIsNone(result.pages[0].extraction_assessment)
        self.assertEqual(result.confidence_threshold, 80)
