"""Long lists in the drawer: NodeSeq makes nodes on demand (200,000 channels across 4 providers).

The drawer's own tests run again with every list lazy (LAZY_ROWS of 1), so the lazy path holds
to everything the list path does.
"""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import navigation
from services.navigation import LAZY_ROWS, NavModel, NodeSeq, _item_node, payloads
from tests.test_drawer import DrawerTests
from tests.test_navigation import FakeCatalog


def channels(count, numbered=True):
    return [
        {
            "id": n,
            "name": f"Kanal {n}" if n % 3 else f"Café {n}",
            "number": str(n) if numbered else "",
            "_provider": "A" if n % 2 else "B",
        }
        for n in range(1, count + 1)
    ]


class NodeSeqTests(unittest.TestCase):
    def test_it_reads_like_the_list_of_nodes_without_making_them(self):
        items = channels(50)
        seq = NodeSeq(items)
        self.assertEqual(len(seq), 50)
        self.assertEqual(seq[3], _item_node(items[3]))
        self.assertEqual(seq[-1], _item_node(items[-1]))
        self.assertEqual(seq[2:4], [_item_node(items[2]), _item_node(items[3])])
        self.assertEqual(list(seq), [_item_node(i) for i in items])
        self.assertEqual(sorted(seq._made), [2, 3, 49])  # only the rows asked for stay made
        with self.assertRaises(IndexError):
            seq[50]

    def test_a_row_is_found_by_its_node_even_from_an_older_list(self):
        items = channels(50)
        seq, older = NodeSeq(items), NodeSeq(channels(50))
        node = older[7]  # never made in `seq`
        self.assertEqual(seq.row_of(node), 7)
        self.assertEqual(seq.row_of(seq[9]), 9)
        gone = _item_node({"id": 999, "name": "x"})
        self.assertIsNone(seq.row_of(gone))

    def test_the_same_id_at_two_providers_is_two_rows(self):
        items = [
            {"id": 1, "name": "Same", "_provider": "A"},
            {"id": 1, "name": "Same", "_provider": "B"},
        ]
        seq = NodeSeq(items)
        other = NodeSeq([dict(i) for i in items])
        self.assertEqual([seq.row_of(other[0]), seq.row_of(other[1])], [0, 1])
        self.assertEqual(seq.find_focus(("B", "1")), seq[1])
        self.assertEqual(seq.find_focus("1"), seq[0])
        self.assertIsNone(seq.find_focus(("C", "1")))

    def test_matching_folds_narrows_and_remembers_the_source_rows(self):
        seq = NodeSeq(channels(30))
        seq._folded_titles()  # (folded ahead, as the drawer does a moment after showing)
        cafe = seq.matching("cafe")  # "Café" folds to "cafe"
        self.assertEqual([n.key for n in cafe], [str(n) for n in range(3, 31, 3)])
        self.assertEqual([cafe.source_row(r) for r in range(3)], [2, 5, 8])
        self.assertIs(seq.matching("cafe"), cafe)
        narrower = seq.matching("cafe 12")  # typing on narrows the rows of the filter before
        self.assertEqual([n.key for n in narrower], ["12"])
        self.assertEqual(narrower.source_row(0), 11)
        self.assertEqual(seq.matching("zzz").items, [])
        self.assertIs(seq.matching(""), seq)

    def test_titles_folded_ahead_in_the_background_serve_the_filter(self):
        seq = NodeSeq(channels(30))
        seq.fold_ahead(0)
        seq.fold_ahead(0)  # asking again starts nothing
        self.assertTrue(seq._fold_due)
        seq._fold_in_background()
        thread = seq._folding
        seq._fold_in_background()
        self.assertIs(seq._folding, thread)
        folded = seq._folded_titles()  # waits for the thread if it is still folding
        self.assertEqual(folded[2], "cafe 3")
        self.assertIs(seq._folded_titles(), folded)
        self.assertEqual([n.key for n in seq.matching("cafe 3")], ["3", "30"])

    def test_filtering_before_any_fold_starts_it_in_the_background_not_here(self):
        seq = NodeSeq(channels(30))
        started = []
        with mock.patch.object(NodeSeq, "_fold_in_background", lambda s: started.append(s)):
            self.assertIs(seq.matching("cafe"), seq)  # unfiltered; nothing folded on this thread
        self.assertEqual(started, [seq])
        self.assertIsNone(seq._folded)
        seq._folded_titles()  # the background fold ends
        self.assertEqual(len(seq.matching("cafe")), 10)

    def test_filtering_while_the_titles_are_still_folding_shows_the_list_as_it_is(self):
        seq = NodeSeq(channels(30))
        seq._folding = object()  # a fold is under way
        self.assertIs(seq.matching("cafe"), seq)
        seq._folding = None
        seq._folded_titles()
        self.assertEqual(len(seq.matching("cafe")), 10)

    def test_numbers_and_their_width(self):
        self.assertTrue(NodeSeq(channels(5)).numbered())
        self.assertFalse(NodeSeq(channels(5, numbered=False)).numbered())
        self.assertEqual(NodeSeq(channels(1200)).number_digits(), 4)
        self.assertEqual(NodeSeq(channels(5, numbered=False)).number_digits(), 0)

    def test_payloads_are_the_dicts_of_a_list_or_of_a_long_list(self):
        items = channels(4)
        self.assertEqual(payloads(NodeSeq(items)), items)
        self.assertEqual(payloads([_item_node(i) for i in items]), items)


class LongGroupTests(unittest.TestCase):
    def test_a_long_group_is_lazy_and_a_short_one_is_a_list(self):
        catalog = FakeCatalog()
        nav = NavModel(catalog, SimpleNamespace(favorites=[], watch_history=[]))
        nav.reset_to("live", "10")
        group = nav.path[-1]
        self.assertIsInstance(nav.children(group), list)
        with mock.patch.object(navigation, "LAZY_ROWS", 1):
            nav._memo.clear()
            seq = nav.children(group)
            self.assertIsInstance(seq, NodeSeq)
            self.assertIs(nav.children(group), seq)  # memoized: the same list, not a copy
        self.assertGreater(LAZY_ROWS, 100)


class LazyDrawerTests(DrawerTests):
    """Every drawer test again, with the channel lists made on demand."""

    def setUp(self):
        patch = mock.patch.object(navigation, "LAZY_ROWS", 1)
        patch.start()
        self.addCleanup(patch.stop)
        # the titles are folded in the background a moment after a list shows: here, at once
        fold = mock.patch.object(NodeSeq, "_fold_in_background", lambda seq: seq._folded_titles())
        fold.start()
        self.addCleanup(fold.stop)
        super().setUp()


del DrawerTests  # (only LazyDrawerTests runs here: test_drawer runs the list path)

if __name__ == "__main__":
    unittest.main()
