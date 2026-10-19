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
from services.navigation import (
    LAZY_ROWS,
    NavModel,
    NodeSeq,
    _item_node,
    payloads,
)
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
        super().setUp()


del DrawerTests  # (only LazyDrawerTests runs here: test_drawer runs the list path)

if __name__ == "__main__":
    unittest.main()
