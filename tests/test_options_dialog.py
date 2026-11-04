"""Settings as a window: what Enter does, Save's checks, and closing with changes not saved."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QTabWidget

from services.config_manager import ConfigManager
from services.provider_link import parse_provider_link
from tests import test_tv_window as base
from ui.settings_dialog import NO_LINK_TEXT, OptionsDialog

ASK = "ui.settings_dialog.QMessageBox.question"
CHECK = "ui.settings_dialog.check_for_updates"


class OptionsDialogCase(base.WindowCase):
    def dialog(self, *, shown=True, new_provider=False):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.done, 0)  # (never asks: done() closes as it is)
        if new_provider:
            dialog.start_new_provider()
        if shown:  # offscreen: nothing shows on a screen
            dialog.show()
            QApplication.processEvents()
        return dialog

    @staticmethod
    def defaults(dialog):
        return [b for b in dialog.findChildren(QPushButton) if b.isDefault()]


class EnterTests(OptionsDialogCase):
    def test_save_is_the_default_button_from_the_gear_and_from_add_provider(self):
        # Without one, the first button after the focus was it: Clear Image Cache, Paste link.
        for new_provider in (False, True):
            with self.subTest(new_provider=new_provider):
                dialog = self.dialog(new_provider=new_provider)
                self.assertEqual(self.defaults(dialog), [dialog.save_button])

    def test_enter_in_the_image_cache_field_saves_and_keeps_the_cache(self):
        dialog = self.dialog()
        self.assertEqual(self.defaults(dialog), [dialog.save_button])
        clear = mock.Mock()
        with mock.patch.object(self.window.image_manager, "clear_cache", clear):
            dialog.cache_image_size_input.setText("150")
            dialog.cache_image_size_input.setFocus()
            QTest.keyClick(dialog.cache_image_size_input, Qt.Key.Key_Return)
        clear.assert_not_called()
        self.assertFalse(dialog.isVisible())  # Save closed it
        self.assertEqual(ConfigManager().max_cache_image_size, 150)

    def test_enter_in_the_new_providers_fields_presses_save_not_paste_link(self):
        dialog = self.dialog(new_provider=True)
        pressed = []
        dialog.save_button.clicked.disconnect()  # (only which button Enter presses)
        dialog.save_button.clicked.connect(lambda: pressed.append("save"))
        dialog.paste_link_button.clicked.connect(lambda: pressed.append("paste"))
        for field in (dialog.name_input, dialog.url_input):
            field.setFocus()
            QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(pressed, ["save", "save"])
        self.assertNotEqual(dialog.verify_result.text(), NO_LINK_TEXT)


class SaveTests(OptionsDialogCase):
    def test_an_empty_image_cache_size_saves_the_default(self):
        dialog = self.dialog(shown=False)
        dialog.cache_image_size_input.setText("")
        dialog.save_settings()  # once raised ValueError: Save did nothing
        self.assertEqual(
            ConfigManager().max_cache_image_size,
            ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE,
        )

    def test_the_image_cache_size_takes_digits_only(self):
        dialog = self.dialog()
        field = dialog.cache_image_size_input
        field.clear()
        QTest.keyClicks(field, "1a2-b")
        self.assertEqual(field.text(), "12")
        self.assertEqual(dialog.image_cache_mb(), 12)

    def test_clear_image_cache_updates_the_size_shown(self):
        self.window.image_manager.current_cache_size = 5 * 1024 * 1024
        dialog = self.dialog()
        label = dialog.settings_layout.labelForField(dialog.cache_image_size_input)
        self.assertIs(label, dialog.cache_image_size_label)
        self.assertIn("5.00 MB", label.text())
        with mock.patch(ASK, return_value=QMessageBox.StandardButton.Yes):
            dialog.clear_image_cache_button.click()
        self.assertIn("0.00 MB", label.text())  # the label shown, not a new hidden one
        self.assertEqual(len([w for w in dialog.findChildren(QLabel) if "MB)" in w.text()]), 1)

    def test_ticking_check_for_updates_checks_only_once_saved(self):
        self.config.check_updates = False
        dialog = self.dialog(shown=False)
        with mock.patch(CHECK) as check:
            dialog.check_updates_checkbox.setChecked(True)
            check.assert_not_called()  # no network before Save
            dialog.save_settings()
        check.assert_called_once_with(config_manager=self.config)
        again = self.dialog(shown=False)  # on already: Save checks nothing more
        with mock.patch(CHECK) as check:
            again.save_settings()
        check.assert_not_called()

    def test_clear_image_cache_asks_first(self):  # 2.9.3: it cleared at the click
        self.window.image_manager.current_cache_size = 5 * 1024 * 1024
        dialog = self.dialog()
        with mock.patch(ASK, return_value=QMessageBox.StandardButton.No) as ask:
            dialog.clear_image_cache_button.click()
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[1], "Clear Image Cache")
        self.assertIn("5.00 MB", dialog.cache_image_size_label.text())  # nothing cleared

    def test_save_is_the_one_way_to_keep_a_providers_edits(self):
        # 2.9.3: no "Apply Change" beside Save (it only said the edits were kept).
        dialog = self.dialog()
        dialog.options_tab.setCurrentWidget(dialog.providers_tab)
        self.assertFalse(hasattr(dialog, "apply_button"))
        texts = [b.text() for b in dialog.findChildren(QPushButton)]
        self.assertNotIn("Apply Change", texts)
        dialog.name_input.setText("Renamed")
        dialog.save_settings()
        self.assertEqual(self.provider_manager.providers[0]["name"], "Renamed")

    def test_the_epg_source_names_the_providers_own_guide(self):
        dialog = self.dialog()
        combo = dialog.epg_source_combo
        index = combo.findData("STB")  # the config's value stays "STB"
        self.assertGreaterEqual(index, 0)
        self.assertEqual(combo.itemText(index), "Provider's own guide")

    def test_the_portal_guides_period_shows_only_with_a_portal(self):
        dialog = self.dialog()
        dialog.epg_source_combo.setCurrentIndex(dialog.epg_source_combo.findData("STB"))
        dialog.options_tab.setCurrentWidget(dialog.epg_tab)
        portal = any(p.get("type") == "STB" for p in dialog.providers)
        self.assertEqual(dialog.epg_stb_period_spinner.isVisibleTo(dialog), portal)
        dialog.providers.append({"type": "STB", "name": "Portal", "url": "http://portal.test"})
        dialog.options_tab.setCurrentWidget(dialog.providers_tab)
        dialog.options_tab.setCurrentWidget(dialog.epg_tab)
        self.assertTrue(dialog.epg_stb_period_spinner.isVisibleTo(dialog))

    def test_each_tab_scrolls_so_the_window_fits_a_laptops_screen(self):
        # 2.9.3: it could not be made smaller than its longest tab (710×722 on a Mac).
        dialog = self.dialog()
        from PySide6.QtWidgets import QScrollArea

        tabs = dialog.options_tab
        for index in range(tabs.count()):
            with self.subTest(tab=tabs.tabText(index)):
                self.assertIsInstance(QTabWidget.widget(tabs, index), QScrollArea)
        self.assertLessEqual(dialog.minimumSizeHint().height(), 600)
        dialog.resize(700, 600)
        QApplication.processEvents()
        self.assertLessEqual(dialog.height(), 600)
        self.assertIs(tabs.currentWidget(), tabs.widget(tabs.currentIndex()))
        tabs.setCurrentWidget(dialog.providers_tab)  # the page itself, as before
        self.assertIs(tabs.currentWidget(), dialog.providers_tab)


class ClosingTests(OptionsDialogCase):
    def close_with(self, dialog, answer, how="reject"):
        with mock.patch(ASK, return_value=answer) as ask:
            if how == "escape":
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
            elif how == "close":
                dialog.close()
            else:
                dialog.reject()
        return ask

    def test_closing_without_changes_does_not_ask(self):
        dialog = self.dialog()
        ask = self.close_with(dialog, QMessageBox.StandardButton.Cancel)
        ask.assert_not_called()
        self.assertFalse(dialog.isVisible())

    def test_a_change_undone_is_no_change(self):
        dialog = self.dialog()
        box = dialog.detailed_log_checkbox
        box.setChecked(not box.isChecked())
        box.setChecked(not box.isChecked())
        self.close_with(dialog, QMessageBox.StandardButton.Cancel).assert_not_called()

    def test_cancel_keeps_settings_open_with_the_changes(self):
        dialog = self.dialog()
        dialog.detailed_log_checkbox.setChecked(True)
        for how in ("reject", "escape", "close"):  # ✕ is close(); Esc and reject() alike
            with self.subTest(how=how):
                ask = self.close_with(dialog, QMessageBox.StandardButton.Cancel, how)
                ask.assert_called_once()
                self.assertTrue(dialog.isVisible())
                self.assertTrue(dialog.detailed_log_checkbox.isChecked())

    def test_discard_closes_without_saving(self):
        dialog = self.dialog()
        dialog.detailed_log_checkbox.setChecked(True)
        self.close_with(dialog, QMessageBox.StandardButton.Discard)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(ConfigManager().detailed_log)

    def test_save_saves_and_closes(self):
        dialog = self.dialog()
        dialog.detailed_log_checkbox.setChecked(True)
        self.close_with(dialog, QMessageBox.StandardButton.Save)
        self.assertFalse(dialog.isVisible())
        self.assertTrue(ConfigManager().detailed_log)
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)

    def test_a_provider_edit_asks(self):
        dialog = self.dialog()
        dialog.options_tab.setCurrentWidget(dialog.providers_tab)
        dialog.name_input.setText("Renamed")
        self.close_with(dialog, QMessageBox.StandardButton.Cancel).assert_called_once()

    def test_add_providers_empty_form_alone_does_not_ask_but_a_dropped_link_does(self):
        dialog = self.dialog(new_provider=True)
        self.close_with(dialog, QMessageBox.StandardButton.Cancel).assert_not_called()
        self.assertFalse(dialog.isVisible())
        dialog = self.dialog(new_provider=True)
        dialog.fill_from_link(parse_provider_link("http://portal.example/playlist.m3u"))
        self.close_with(dialog, QMessageBox.StandardButton.Cancel).assert_called_once()
        self.assertTrue(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()


class LayoutTests(OptionsDialogCase):
    """Save is the window's accented main action with Cancel beside it; Remove Provider
    stands apart in red; the first tab is General; the kinds of provider read most common
    first, and a new provider starts in the address field."""

    def test_cancel_sits_left_of_the_accented_save_and_closes_as_esc_does(self):
        dialog = self.dialog()
        self.assertIn("background", dialog.save_button.styleSheet())
        cancel, save = dialog.close_button, dialog.save_button
        self.assertLess(cancel.mapTo(dialog, cancel.rect().topLeft()).x(), save.x())
        middle = cancel.geometry().center().y()
        self.assertAlmostEqual(middle, save.geometry().center().y(), delta=2)  # one row
        cancel.click()
        QApplication.processEvents()
        self.assertFalse(dialog.isVisible())  # nothing changed: closed without asking

    def test_cancel_with_changes_asks_first(self):
        dialog = self.dialog()
        dialog.cache_image_size_input.setText("150")
        with mock.patch(ASK, return_value=QMessageBox.StandardButton.Cancel) as asked:
            dialog.close_button.click()
        asked.assert_called_once()
        self.assertTrue(dialog.isVisible())

    def test_remove_provider_stands_apart_in_red_below_the_form(self):
        dialog = self.dialog()
        dialog.options_tab.setCurrentWidget(dialog.providers_tab)
        QApplication.processEvents()
        remove, add = dialog.remove_provider_button, dialog.add_provider_button
        self.assertIn("color", remove.styleSheet())
        self.assertGreater(remove.y(), dialog.verify_apply_group.y())  # under the form
        self.assertLess(add.y(), dialog.name_input.y())  # Add stays at the top
        self.assertLess(remove.width(), dialog.providers_tab.width() / 2)  # on its own, left

    def test_the_first_tab_is_general(self):
        dialog = self.dialog(shown=False)
        self.assertEqual(dialog.options_tab.tabText(0), "General")

    def test_the_kinds_read_playlist_xtream_stb_stream_then_media_servers(self):
        dialog = self.dialog()
        dialog.options_tab.setCurrentWidget(dialog.providers_tab)
        QApplication.processEvents()
        kinds = [
            dialog.type_M3UPLAYLIST,
            dialog.type_XTREAM,
            dialog.type_STB,
            dialog.type_M3USTREAM,
            dialog.type_JELLYFIN,
            dialog.type_EMBY,
        ]
        places = [(b.mapTo(dialog, b.rect().topLeft()).y(), b.x()) for b in kinds]
        self.assertEqual(places, sorted(places))  # row by row, left to right

    def test_a_new_provider_starts_in_the_address_field(self):
        dialog = self.dialog(new_provider=True)
        dialog.activateWindow()
        QApplication.processEvents()
        self.assertIs(dialog.focusWidget(), dialog.url_input)
