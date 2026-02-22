from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QTextCursor, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


class HtmlItemDelegate(QStyledItemDelegate):
    elidedPostfix = "..."
    doc = QTextDocument()
    doc.setDocumentMargin(1)

    def __init__(self):
        super().__init__()

    def paint(self, painter, inOption, index):
        options = QStyleOptionViewItem(inOption)
        self.initStyleOption(options, index)
        if not options.text:
            return super().paint(painter, inOption, index)
        style = options.widget.style() if options.widget else QApplication.style()

        textOption = QTextOption()
        textOption.setWrapMode(
            QTextOption.WordWrap
            if options.features & QStyleOptionViewItem.WrapText
            else QTextOption.ManualWrap
        )
        textOption.setTextDirection(options.direction)

        self.doc.setDefaultTextOption(textOption)
        self.doc.setHtml(options.text)
        self.doc.setDefaultFont(options.font)
        self.doc.setTextWidth(options.rect.width())
        self.doc.adjustSize()

        if self.doc.size().width() > options.rect.width():
            cursor = QTextCursor(self.doc)
            cursor.movePosition(QTextCursor.End)
            metric = QFontMetrics(options.font)
            postfixWidth = metric.horizontalAdvance(self.elidedPostfix)
            while self.doc.size().width() > options.rect.width() - postfixWidth:
                cursor.deletePreviousChar()
                self.doc.adjustSize()
            cursor.insertText(self.elidedPostfix)

        options.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, options, painter, inOption.widget)

        textRect = style.subElementRect(QStyle.SE_ItemViewItemText, options)
        documentSize = QSize(self.doc.size().width(), self.doc.size().height())
        layoutRect = QRect(
            QStyle.alignedRect(
                Qt.LayoutDirectionAuto, options.displayAlignment, documentSize, textRect
            )
        )

        painter.save()
        painter.translate(layoutRect.topLeft())
        self.doc.drawContents(painter, textRect.translated(-textRect.topLeft()))
        painter.restore()

    def sizeHint(self, inOption, index):
        options = QStyleOptionViewItem(inOption)
        self.initStyleOption(options, index)
        if not options.text:
            return super().sizeHint(inOption, index)
        self.doc.setHtml(options.text)
        self.doc.setTextWidth(options.rect.width())
        return QSize(self.doc.idealWidth(), self.doc.size().height())


class ChannelItemDelegate(QStyledItemDelegate):
    def __init__(self):
        super().__init__()
        self.default_font = QFont()
        self.default_font.setPointSize(12)

    def paint(self, painter, inOption, index):
        col = index.column()
        if col == 2:
            progress = index.data(Qt.UserRole)
            if progress is not None:
                options = QStyleOptionViewItem(inOption)
                self.initStyleOption(options, index)
                style = (
                    options.widget.style() if options.widget else QApplication.style()
                )
                style.drawPrimitive(
                    QStyle.PE_PanelItemViewItem, options, painter, options.widget
                )
                painter.save()
                painter.setRenderHint(painter.RenderHint.Antialiasing)
                padding = 4
                rect = QRectF(options.rect.adjusted(padding, padding, -padding, -padding))
                radius = 3.0
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(128, 128, 128, 60))
                painter.drawRoundedRect(rect, radius, radius)
                if progress > 0:
                    progress_width = rect.width() * progress / 100.0
                    progress_rect = QRectF(
                        rect.x(), rect.y(), progress_width, rect.height()
                    )
                    painter.setBrush(QColor(0, 191, 165))
                    painter.drawRoundedRect(progress_rect, radius, radius)
                painter.restore()
            else:
                super().paint(painter, inOption, index)
        elif col == 3:
            epg_text = index.data(Qt.UserRole)
            if epg_text:
                options = QStyleOptionViewItem(inOption)
                self.initStyleOption(options, index)
                style = (
                    options.widget.style() if options.widget else QApplication.style()
                )
                options.text = epg_text
                style.drawControl(
                    QStyle.CE_ItemViewItem, options, painter, inOption.widget
                )
            else:
                super().paint(painter, inOption, index)
        else:
            super().paint(painter, inOption, index)

    def sizeHint(self, option, index):
        col = index.column()
        if col == 2:
            return QSize(100, 24)
        elif col == 3:
            options = QStyleOptionViewItem(option)
            self.initStyleOption(options, index)
            style = options.widget.style() if options.widget else QApplication.style()
            text = index.data(Qt.UserRole)
            font = options.font or style.font(QStyle.CE_ItemViewItem, options, index)
            metrics = QFontMetrics(font)
            return QSize(metrics.boundingRect(text).width(), metrics.height())
        return super().sizeHint(option, index)
