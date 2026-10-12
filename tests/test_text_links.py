import unittest
from book_translator.confidence import Assessment, RegionAssessment, review_flags
from book_translator.core import PageState
from book_translator.text_links import initial_spans, move_spans


def region(text, top):
    return RegionAssessment(text, text, .4, 'Uncertain', (top, 10, top + 20, 200))


class TextLinkTests(unittest.TestCase):
    def test_repeated_quotes_follow_boxes_even_if_region_list_is_reversed(self):
        report = Assessment('Repeat then Repeat', .9, [region('Repeat', 200), region('Repeat', 20)])
        self.assertEqual(initial_spans(report), [[12, 18], [0, 6]])

    def test_unique_surrounding_passage_resolves_partial_duplicate_flags(self):
        report = Assessment('Yes. Context. Yes.', .9, [region('Context.', 100), region('Yes.', 200)])
        self.assertEqual(initial_spans(report)[1], [14, 18])

    def test_ambiguous_or_missing_quote_does_not_default_to_first_match(self):
        report = Assessment('Yes Yes', .9, [region('Yes', 200), region('absent', 300)])
        self.assertEqual(initial_spans(report), [None, None])

    def test_links_are_independent_of_threshold_or_dismissals(self):
        report = Assessment('Repeat Repeat', .9, [region('Repeat', 20), region('Repeat', 200)])
        report.regions[0].confidence = .95
        page = PageState(source=report.text, extraction_assessment=report)
        self.assertEqual(review_flags(page, .8)[0].text_span, [7, 13])
        page.dismissed_reviews.append('source:0')
        self.assertEqual(review_flags(page, .99)[0].text_span, [7, 13])

    def test_insertions_shift_links_and_deleted_duplicate_never_retargets(self):
        spans = [[0, 6], [7, 13]]
        self.assertEqual(move_spans('Repeat Repeat', 'Intro: Repeat Repeat', spans), [[7, 13], [14, 20]])
        self.assertEqual(move_spans('Repeat Repeat', 'Repeat', spans), [None, None])

    def test_edited_quote_keeps_its_range_after_later_edits(self):
        report = Assessment('First second', .9, [region('First', 20), region('second', 200)])
        page = PageState(source=report.text, extraction_assessment=report)
        review_flags(page, .8)
        page.source = 'Changed second'
        self.assertEqual(review_flags(page, .8)[0].text_span, [0, 7])
        page.review_links["source"]["spans"][0] = None  # Link discarded by an older app.
        self.assertEqual(review_flags(page, .8)[0].text_span, [0, 7])
        page.source = 'First second'
        self.assertEqual(review_flags(page, .8)[0].text_span, [0, 5])

    def test_persisted_links_restore_after_edits(self):
        import json
        from book_translator.text_links import validate_links
        report = Assessment('Repeat Repeat', .9, [region('Repeat', 20), region('Repeat', 200)])
        page = PageState(source=report.text, extraction_assessment=report)
        review_flags(page, .8)
        page.source = 'Intro Repeat Repeat'
        review_flags(page, .8)
        restored = PageState(source=page.source, extraction_assessment=report,
                             review_links=validate_links(json.loads(json.dumps(page.review_links))))
        self.assertEqual(review_flags(restored, .8)[1].text_span, [13, 19])

    def test_whitespace_normalization_preserves_real_offsets(self):
        report = Assessment('A\n  word!', .9, [region('A word', 20)])
        self.assertEqual(initial_spans(report), [[0, 8]])
