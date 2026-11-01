"""A provider's text (a channel's or programme's name) shows as it is, never as HTML: Qt reads a
text that looks like HTML as HTML, and an <img> in it could show a file of this computer."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QToolTip

from ui.overlays.base import TooltipWidget, plain_tooltip
from ui.record_timer_dialog import RecordTimerDialog

HOSTILE = '<h1>News</h1><img src="file:///etc/hosts">'


class _Named(TooltipWidget):
    def tooltip_at(self, pos: QPointF) -> str:
        return HOSTILE


class PlainTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_tooltip_that_looks_like_html_shows_as_it_is(self):
        shown = plain_tooltip(HOSTILE)
        self.assertNotIn("<h1>", shown)
        self.assertNotIn("<img", shown)
        self.assertIn("&lt;h1&gt;News&lt;/h1&gt;", shown)
        self.assertEqual(plain_tooltip("News & Sports (G)"), "News & Sports (G)")  # as before

    def test_a_painted_widgets_tooltip_is_escaped(self):
        widget = _Named()
        self.addCleanup(widget.deleteLater)
        event = QHelpEvent(QEvent.Type.ToolTip, QPoint(1, 1), QPoint(1, 1))
        with mock.patch.object(QToolTip, "showText") as show:
            widget.event(event)
        self.assertEqual(show.call_args.args[1], plain_tooltip(HOSTILE))

    def test_record_at_a_time_names_the_channel_as_it_is(self):
        dialog = RecordTimerDialog(HOSTILE)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.channel.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.channel.text(), HOSTILE)


if __name__ == "__main__":
    unittest.main()
