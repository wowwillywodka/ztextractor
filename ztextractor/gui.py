"""GUI экстрактора данных Mega Drive: просмотр и экспорт графики, текста и др. (PySide6)."""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import QBuffer, QIODevice, QRect, QRectF, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QImage, QPainter, QPen, QPixmap, qRgba
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import gems
from . import palette as pal
from . import sprites as spritemod
from . import spriteres
from . import tiles
from . import zones as zonemod
from .rom import Rom, load_rom
from .i18n import get_lang, set_lang, tr, version_label


def _de(version: str, addr: int) -> int:
    """Адрес для немецкого релиза ZT (инопланетяне в ID-картах).

    German = US-релиз со сдвигом данных −0x3C для банков/спрайтов (адреса ≥ 0x10000);
    палитры/имена/таблицы (< 0x10000) на месте. Враги идентичны US."""
    if version.startswith("Zero Tolerance (нем") and addr >= 0x10000:
        return addr - 0x3C
    return addr


def _de_struct(version: str, obj):
    """Рекурсивно применить сдвиг немецкой версии ко всем int в структуре (списке/
    кортеже/словаре). Сдвигаются только адреса ≥0x10000 (банки/спрайты); счётчики,
    палитры, мелкие базы остаются. No-op для US и прототипов."""
    if not version.startswith("Zero Tolerance (нем"):
        return obj
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return _de(version, obj)
    if isinstance(obj, tuple):
        return tuple(_de_struct(version, x) for x in obj)
    if isinstance(obj, list):
        return [_de_struct(version, x) for x in obj]
    if isinstance(obj, dict):
        return {k: _de_struct(version, v) for k, v in obj.items()}
    return obj


def _hex_spin(maximum: int, value: int = 0) -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, maximum)
    s.setValue(value)
    s.setDisplayIntegerBase(16)
    s.setPrefix("0x")
    s.setGroupSeparatorShown(False)
    s.setMinimumWidth(96)   # чтобы hex-значение (напр. 0x100E64) не обрезалось
    return s


def build_qimage(
    buf: bytes,
    width: int,
    height: int,
    colors: List[pal.RGB],
    transparent_index: Optional[int],
) -> QImage:
    """Собрать индексный QImage с 16-цветной палитрой.

    transparent_index — индекс, который станет прозрачным (None — без прозрачности).
    """
    img = QImage(buf, width, height, width, QImage.Format.Format_Indexed8)
    table = []
    for i, (r, g, b) in enumerate(colors):
        a = 0 if i == transparent_index else 255
        table.append(qRgba(r, g, b, a))
    img.setColorTable(table)
    return img.copy()  # копия владеет своими данными независимо от buf


class TileView(QGraphicsView):
    """Сцена с листом тайлов; перетаскиванием выделяется область (рамкой)."""

    selectionChanged = Signal()
    hoverMoved = Signal(float, float)  # координаты сцены под курсором
    doubleClicked = Signal(float, float)  # двойной клик: координаты сцены

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self._scene.addItem(self._item)
        # постоянная подсветка выделенного сегмента (видна после отпускания мыши)
        self._sel_item = QGraphicsRectItem()
        pen = QPen(QColor(0, 255, 255))
        pen.setCosmetic(True)
        pen.setWidth(2)
        self._sel_item.setPen(pen)
        self._sel_item.setBrush(QColor(0, 255, 255, 48))
        self._sel_item.setZValue(10)
        self._scene.addItem(self._sel_item)
        self._sel_item.hide()
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMouseTracking(True)
        self.selection: Optional[QRectF] = None
        self._press_vp = None            # точка нажатия (viewport) для детекта клика
        self.rubberBandChanged.connect(self._on_band)

    def mouseMoveEvent(self, event) -> None:
        sp = self.mapToScene(event.position().toPoint())
        self.hoverMoved.emit(sp.x(), sp.y())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_vp = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        # клик без протягивания (сдвиг < 3 px) — выделить элемент под курсором
        if event.button() == Qt.MouseButton.LeftButton and self._press_vp is not None:
            moved = (event.position().toPoint() - self._press_vp).manhattanLength()
            if moved <= 3:
                sp = self.mapToScene(event.position().toPoint())
                self.selection = QRectF(sp.x(), sp.y(), 1, 1)
                self.selectionChanged.emit()
            self._press_vp = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            self.doubleClicked.emit(sp.x(), sp.y())
        super().mouseDoubleClickEvent(event)

    def set_pixmap(self, pm: QPixmap) -> None:
        self._item.setPixmap(pm)
        self._scene.setSceneRect(QRectF(pm.rect()))

    def set_zoom(self, zoom: int) -> None:
        self.resetTransform()
        self.scale(zoom, zoom)

    def _on_band(self, rect, from_pt, to_pt) -> None:
        if rect.isNull():
            return  # завершение выделения — сохраняем последнее
        self.selection = QRectF(from_pt, to_pt).normalized()
        self.selectionChanged.emit()

    def clear_selection(self) -> None:
        self.selection = None
        self._sel_item.hide()
        self.selectionChanged.emit()

    def set_selection_rect(self, rect) -> None:
        """Показать постоянную подсветку выделения (rect в px сцены) или скрыть."""
        if rect is None:
            self._sel_item.hide()
        else:
            self._sel_item.setRect(rect)
            self._sel_item.show()


# --- Общая сетка ячеек + панель выделения/экспорта (для всех вкладок) -----------

EXPORT_SCALES = [2, 4, 6, 8]


class GridSpec:
    """Равномерная сетка ячеек на листе (в пикселях сцены). Ячейка (c,r) занимает
    прямоугольник [x0 + c·step_x, y0 + r·step_y, cell_w, cell_h]. step ≥ cell (зазор).
    Используется для привязки выделения, нарезки по рядам/столбцам и поячеечного экспорта."""

    def __init__(self, cell_w, cell_h, cols, rows,
                 step_x=None, step_y=None, x0=0, y0=0):
        self.cw, self.ch = int(cell_w), int(cell_h)
        self.cols, self.rows = int(cols), int(rows)
        self.sx = int(step_x) if step_x else self.cw
        self.sy = int(step_y) if step_y else self.ch
        self.x0, self.y0 = int(x0), int(y0)

    def cells_in(self, sel):
        """(c0,r0,c1,r1) ячеек, пересечённых выделением sel (QRectF), либо None."""
        if sel is None or sel.width() < 1 or sel.height() < 1:
            return None
        c0 = max(0, int((sel.left() - self.x0) // self.sx))
        r0 = max(0, int((sel.top() - self.y0) // self.sy))
        c1 = min(self.cols - 1, int((sel.right() - 0.001 - self.x0) // self.sx))
        r1 = min(self.rows - 1, int((sel.bottom() - 0.001 - self.y0) // self.sy))
        if c1 < c0 or r1 < r0:
            return None
        return (c0, r0, c1, r1)

    def cell_rect(self, c, r) -> QRect:
        return QRect(self.x0 + c * self.sx, self.y0 + r * self.sy, self.cw, self.ch)

    def bbox(self, c0, r0, c1, r1) -> QRect:
        x = self.x0 + c0 * self.sx
        y = self.y0 + r0 * self.sy
        return QRect(x, y, (c1 - c0) * self.sx + self.cw, (r1 - r0) * self.sy + self.ch)


def _scale_image(img: QImage, scale: int, smooth: bool) -> QImage:
    if scale and scale != 1:
        mode = (Qt.TransformationMode.SmoothTransformation if smooth
                else Qt.TransformationMode.FastTransformation)
        img = img.scaled(img.width() * scale, img.height() * scale,
                         Qt.AspectRatioMode.IgnoreAspectRatio, mode)
    return img.convertToFormat(QImage.Format.Format_ARGB32)


def _cell_label(owner, c, r) -> str:
    fn = getattr(owner, "grid_cell_label", None)
    if fn:
        try:
            return fn(c, r)
        except Exception:
            pass
    return f"r{r}_c{c}"


class ExportDialog(QDialog):
    """Модальное окно экспорта: предпросмотр экспортируемой области, выбор масштаба
    (×1…×32) и интерполяции, сохранение одним PNG или отдельными PNG по ячейкам.

    Снимок (img, grid, cells, base, label_fn) фиксируется при открытии."""

    def __init__(self, parent, img: QImage, grid: GridSpec, cells, base: str, label_fn):
        super().__init__(parent)
        self.setWindowTitle(tr("Экспорт PNG", "Export PNG"))
        self.setModal(True)
        self._img = img
        self._grid = grid
        self._cells = cells              # (c0,r0,c1,r1) или None (весь лист)
        self._base = base
        self._label_fn = label_fn

        v = QVBoxLayout(self)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(360, 240)
        self.preview.setStyleSheet("background:#111; border:1px solid #333;")
        v.addWidget(self.preview, 1)

        form = QFormLayout()
        self.scale = QComboBox()
        for s in EXPORT_SCALES:
            self.scale.addItem(f"×{s}", s)
        self.scale.currentIndexChanged.connect(self._update_preview)
        form.addRow(tr("Масштаб:", "Scale:"), self.scale)
        self.interp = QComboBox()
        self.interp.addItem(tr("Без сглаживания (пиксель-арт)", "No smoothing (pixel art)"), False)
        self.interp.addItem(tr("Сглаживание (билинейное)", "Smoothing (bilinear)"), True)
        self.interp.currentIndexChanged.connect(self._update_preview)
        form.addRow(tr("Интерполяция:", "Interpolation:"), self.interp)
        v.addLayout(form)

        self.res_lbl = QLabel("—")
        self.res_lbl.setWordWrap(True)
        self.res_lbl.setStyleSheet("font-family: monospace; font-size: 11px; color:#9f9;")
        v.addWidget(self.res_lbl)

        btns = QHBoxLayout()
        self.one_btn = QPushButton(tr("Сохранить один PNG…", "Save one PNG…"))
        self.one_btn.clicked.connect(lambda: self._save(multi=False))
        self.multi_btn = QPushButton(tr("Сохранить отдельные PNG…", "Save separate PNGs…"))
        self.multi_btn.setToolTip(tr("Каждая ячейка выделения — в свой файл (в папку)",
                                     "Each selected cell to its own file (in a folder)"))
        self.multi_btn.clicked.connect(lambda: self._save(multi=True))
        close_btn = QPushButton(tr("Закрыть", "Close"))
        close_btn.clicked.connect(self.reject)
        btns.addWidget(self.one_btn); btns.addWidget(self.multi_btn)
        btns.addStretch(1); btns.addWidget(close_btn)
        v.addLayout(btns)

        # одна ячейка → «отдельные» бессмысленны; всё равно разрешаем, но подсветим режим
        self._update_preview()

    def _region(self):
        if self._cells:
            return self._img.copy(self._grid.bbox(*self._cells))
        return self._img

    def _ncells(self):
        if self._cells:
            c0, r0, c1, r1 = self._cells
            return c1 - c0 + 1, r1 - r0 + 1
        return self._grid.cols, self._grid.rows

    def _update_preview(self):
        scale = self.scale.currentData() or 1
        smooth = bool(self.interp.currentData())
        mode = (Qt.TransformationMode.SmoothTransformation if smooth
                else Qt.TransformationMode.FastTransformation)
        region = self._region()
        disp = QPixmap.fromImage(region).scaled(
            self.preview.width() - 4, self.preview.height() - 4,
            Qt.AspectRatioMode.KeepAspectRatio, mode)
        self.preview.setPixmap(disp)
        ncols, nrows = self._ncells()
        ow, oh = region.width() * scale, region.height() * scale
        cw, ch = self._grid.cw * scale, self._grid.ch * scale
        sel_txt = (tr("весь лист", "whole sheet") if not self._cells
                   else tr(f"{ncols}×{nrows} яч.", f"{ncols}×{nrows} cells"))
        self.res_lbl.setText(tr(
            f"Выделено: {sel_txt}    Один PNG: {ow}×{oh}px    Отдельные: {cw}×{ch}px × {ncols * nrows} шт",
            f"Selected: {sel_txt}    One PNG: {ow}×{oh}px    Separate: {cw}×{ch}px × {ncols * nrows} pcs"))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_preview()

    def _save(self, multi: bool):
        scale = self.scale.currentData()
        smooth = self.interp.currentData()
        if not multi:
            path, _ = QFileDialog.getSaveFileName(
                self, tr("Сохранить PNG", "Save PNG"), f"{self._base}.png", "PNG (*.png)")
            if not path:
                return
            if _scale_image(self._region(), scale, smooth).save(path, "PNG"):
                self.accept()
            else:
                QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))
            return

        cells = self._cells or (0, 0, self._grid.cols - 1, self._grid.rows - 1)
        c0, r0, c1, r1 = cells
        out_dir = QFileDialog.getExistingDirectory(self, tr("Папка для PNG", "Folder for PNGs"))
        if not out_dir:
            return
        saved = 0
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                cell = self._img.copy(self._grid.cell_rect(c, r))
                if _is_blank(cell):
                    continue
                name = f"{self._base}_{self._label_fn(c, r)}.png"
                if _scale_image(cell, scale, smooth).save(os.path.join(out_dir, name), "PNG"):
                    saved += 1
        QMessageBox.information(self, tr("Экспорт", "Export"), tr(f"Сохранено файлов: {saved}", f"Files saved: {saved}"))
        self.accept()


class GridExportBar(QWidget):
    """Панель вкладки с сеткой ячеек: подсветка выделения по сетке, кнопки «Весь ряд»/
    «Весь столбец»/«Сброс» и кнопка «Экспорт…», открывающая модальное окно ExportDialog.

    Хост обязан реализовать grid_state() → (image, grid, basename) либо None, и иметь
    .view (TileView). Необязательно grid_cell_label(c, r) → str для имён отдельных PNG."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        owner.view.selectionChanged.connect(self._snap)

        box = QGroupBox(tr("Выделение / экспорт", "Selection / export"))
        f = QVBoxLayout(box)

        row = QHBoxLayout()
        self.row_btn = QPushButton(tr("Ряд", "Row"))
        self.row_btn.setToolTip(tr("Расширить выделение на весь ряд", "Extend selection to the whole row"))
        self.row_btn.clicked.connect(lambda: self._expand("row"))
        self.col_btn = QPushButton(tr("Столбец", "Column"))
        self.col_btn.setToolTip(tr("Расширить выделение на весь столбец", "Extend selection to the whole column"))
        self.col_btn.clicked.connect(lambda: self._expand("col"))
        self.clr_btn = QPushButton(tr("Сброс", "Reset"))
        self.clr_btn.setToolTip(tr("Сбросить выделение", "Clear selection"))
        self.clr_btn.clicked.connect(lambda: owner.view.clear_selection())
        for b in (self.row_btn, self.col_btn, self.clr_btn):
            b.setMinimumWidth(0)
            row.addWidget(b)
        f.addLayout(row)

        self.sel_lbl = QLabel(tr("Выделение: весь лист", "Selection: whole sheet"))
        self.sel_lbl.setStyleSheet("font-size: 11px; color:#9f9;")
        f.addWidget(self.sel_lbl)

        self.export_btn = QPushButton(tr("Экспорт…", "Export…"))
        self.export_btn.clicked.connect(self._open_dialog)
        f.addWidget(self.export_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(box)

    # ---- подсветка выделения по сетке --------------------------------------
    def _snap(self):
        st = self.owner.grid_state()
        if not st:
            self.sel_lbl.setText(tr("Выделение: —", "Selection: —"))
            return
        _img, grid, _base = st
        cells = grid.cells_in(self.owner.view.selection) if grid else None
        rect = QRectF(grid.bbox(*cells)) if cells else None
        self.owner.view.set_selection_rect(rect)
        if cells:
            c0, r0, c1, r1 = cells
            self.sel_lbl.setText(tr(f"Выделение: {c1 - c0 + 1}×{r1 - r0 + 1} яч.",
                                    f"Selection: {c1 - c0 + 1}×{r1 - r0 + 1} cells"))
        else:
            self.sel_lbl.setText(tr("Выделение: весь лист", "Selection: whole sheet"))

    def _expand(self, axis: str):
        st = self.owner.grid_state()
        if not st:
            return
        _img, grid, _base = st
        cells = grid.cells_in(self.owner.view.selection)
        if not cells:
            cells = (0, 0, grid.cols - 1, grid.rows - 1)
        c0, r0, c1, r1 = cells
        if axis == "row":
            c0, c1 = 0, grid.cols - 1
        else:
            r0, r1 = 0, grid.rows - 1
        self.owner.view.selection = QRectF(grid.bbox(c0, r0, c1, r1))
        self.owner.view.selectionChanged.emit()

    def _open_dialog(self):
        st = self.owner.grid_state()
        if not st:
            QMessageBox.information(self, tr("Экспорт", "Export"), tr("Нечего экспортировать.", "Nothing to export."))
            return
        img, grid, base = st
        if img is None or img.isNull() or grid is None:
            QMessageBox.information(self, tr("Экспорт", "Export"), tr("Нечего экспортировать.", "Nothing to export."))
            return
        cells = grid.cells_in(self.owner.view.selection)
        label_fn = lambda c, r: _cell_label(self.owner, c, r)
        ExportDialog(self, img, grid, cells, base, label_fn).exec()


def _is_blank(img: QImage) -> bool:
    """True, если изображение полностью прозрачное (пустая ячейка сетки)."""
    a = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = a.width(), a.height()
    step = max(1, (w * h) // 256)   # разрежённая выборка для скорости
    i = 0
    for y in range(h):
        for x in range(w):
            i += 1
            if i % step:
                continue
            if a.pixelColor(x, y).alpha() > 8:
                return False
    return True


def decode_one_tile(data: bytes, off: int) -> bytes:
    """Один тайл 32×32 4bpp column-major -> индексный буфер 32×32."""
    buf = bytearray(1024)
    n = len(data)
    for col in range(16):
        coff = off + col * 32
        x = col * 2
        for row in range(32):
            o = coff + row
            b = data[o] if 0 <= o < n else 0
            buf[row * 32 + x] = b >> 4
            buf[row * 32 + x + 1] = b & 0xF
    return bytes(buf)


def _hflip_tile(tbuf: bytes) -> bytes:
    """Зеркалить 32×32 индексный буфер по горизонтали (для H-флипа тайла)."""
    out = bytearray(1024)
    for ty in range(32):
        row = tbuf[ty * 32:ty * 32 + 32]
        out[ty * 32:ty * 32 + 32] = row[::-1]
    return bytes(out)


def assemble_frame(data, frame, tile_base, colors, transparent_index,
                   fmt=spritemod.FORMATS["bzt"]) -> QImage:
    """Собрать кадр спрайта по его размеру w×h тайлов -> QImage."""
    w = frame["w"]
    h = frame["h"]
    cw = w * 32
    ch = h * 32
    canvas = bytearray(cw * ch)
    for row in range(h):
        for col in range(w):
            tile = spritemod.frame_tile(data, frame, row, col, fmt)
            tbuf = decode_one_tile(data, tile_base + tile * 512)
            if spritemod.frame_attr(data, frame, row, col) & 1:
                tbuf = _hflip_tile(tbuf)        # бит0 атрибута = горизонтальный флип
            for ty in range(32):
                dst = (row * 32 + ty) * cw + col * 32
                canvas[dst:dst + 32] = tbuf[ty * 32:ty * 32 + 32]
    return build_qimage(bytes(canvas), cw, ch, colors, transparent_index)


class FrameViewer(QWidget):
    """Вкладка «Кадры врагов»: собирает кадры спрайта из тайлов и показывает их."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._sprite: Optional[dict] = None
        self._tree: Optional[dict] = None
        self._fmt = spritemod.FORMATS["bzt"]

        self.view = TileView()
        panel = QWidget()
        panel.setFixedWidth(350)
        v = QVBoxLayout(panel)

        self.info = QLabel(tr("Загрузите ROM (вкладка «Вся графика»)", "Load a ROM (the «All graphics» tab)"))
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        form = QFormLayout()
        self.sprite_combo = QComboBox()
        self.sprite_combo.currentIndexChanged.connect(self._sprite_changed)
        form.addRow(tr("Спрайт:", "Sprite:"), self.sprite_combo)

        self.anim_combo = QComboBox()
        self.anim_combo.currentIndexChanged.connect(self._render)
        form.addRow(tr("Анимация:", "Animation:"), self.anim_combo)

        self.base = _hex_spin(0)
        self.base.valueChanged.connect(self._render)
        form.addRow(tr("База графики:", "Graphics base:"), self.base)

        self._nudge_btns = []
        nud = QHBoxLayout()
        for label, delta in [("−512", -512), ("−32", -32), ("−2", -2),
                             ("+2", 2), ("+32", 32), ("+512", 512)]:
            b = QPushButton(label)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, dd=delta: self._nudge(dd))
            nud.addWidget(b)
            self._nudge_btns.append(b)
        form.addRow(tr("Подстройка:", "Nudge:"), nud)

        self.zone_pal = QComboBox()
        self.zone_pal.currentIndexChanged.connect(self._zone_pal_changed)
        form.addRow(tr("Палитра зоны:", "Zone palette:"), self.zone_pal)

        self.pal_offset = _hex_spin(0, 0x097BA0)
        self.pal_offset.valueChanged.connect(self._render)
        form.addRow(tr("или смещение:", "or offset:"), self.pal_offset)

        self.gray = QCheckBox(tr("Оттенки серого", "Grayscale"))
        self.gray.stateChanged.connect(self._render)
        form.addRow(self.gray)

        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        form.addRow(self.transparent)

        self.unique = QCheckBox(tr("Только уникальные кадры", "Unique frames only"))
        self.unique.setToolTip(tr("Скрыть дубликаты-холды (повторяющиеся позы)", "Hide hold-duplicates (repeated poses)"))
        self.unique.stateChanged.connect(self._render)
        form.addRow(self.unique)

        self.per_row = QSpinBox()
        self.per_row.setRange(1, 16)
        self.per_row.setValue(8)
        self.per_row.valueChanged.connect(self._render)
        form.addRow(tr("Кадров в ряд:", "Frames per row:"), self.per_row)

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 8)
        self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        hint = QLabel(tr(
            "Крути «Базу графики» (или кнопки подстройки), пока кадры не сложатся в чёткого врага. "
            "Палитру подбери смещением.",
            "Adjust the «Graphics base» (or the nudge buttons) until the frames form a clear enemy. "
            "Tune the palette via the offset."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(hint)

        self._sheet: Optional[QImage] = None
        self._grid: Optional[GridSpec] = None
        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        layout = QHBoxLayout(self)
        layout.addWidget(panel, 0)
        layout.addWidget(self.view, 1)

    def grid_state(self):
        if self.main.rom is None or self._sheet is None or self._grid is None:
            return None
        nm = "frames"
        if self._sprite is not None:
            nm = f"frames_{self._sprite.get('offset', 0):06X}"
        base = os.path.splitext(os.path.basename(self.main.rom.path))[0] + "_" + nm
        return self._sheet, self._grid, base

    def grid_cell_label(self, c, r):
        return f"r{r}_f{c}"

    def set_rom(self, rom, sprite_list=None) -> None:
        self._fmt = spritemod.sprite_format(rom.version) if rom else \
            spritemod.FORMATS["bzt"]
        # враги из таблицы диспетчера игры (версионно-независимо, формат по версии)
        enemies = spritemod.find_enemies(rom.data, self._fmt) if rom else []
        # имена врагов (для июля) по совпадению a1
        known = spritemod.known_sprites(rom.version) if rom else []
        a1_name = {}
        for name, data_off, gfx in known:
            a1 = spritemod.find_header(rom.data, data_off)
            if a1:
                a1_name[a1] = name
        self._entries = []
        for e in enemies:
            nm = a1_name.get(e["a1"])
            label = f"★ {nm}" if nm else tr(f"Враг {e['a1']:#08x} ({e['frames']} кадр.)",
                                            f"Enemy {e['a1']:#08x} ({e['frames']} frames)")
            self._entries.append({"label": label, "a1": e["a1"],
                                  "offset": e["offset"], "end": e["base"],
                                  "base": e["base"]})
        # прочие кадровые кластеры без заголовка дерева (предметы/анимации) — флэт.
        # Только для BZT (find_sprites рассчитан на формат 0x36); у ZT — лишний шум.
        if self._fmt is spritemod.FORMATS["bzt"]:
            auto = (sprite_list if sprite_list is not None
                    else (spritemod.find_sprites(rom.data) if rom else []))
            for s in auto:
                if s["frames"] < 4:
                    continue
                if any(abs(s["offset"] - e["offset"]) < 0x2000 for e in enemies):
                    continue
                self._entries.append({"label": tr(f"{s['offset']:#08x} (объект/аним.)",
                                                  f"{s['offset']:#08x} (object/anim.)"),
                                      "a1": None, "offset": s["offset"],
                                      "end": s["end"], "base": None})
        self.sprite_combo.blockSignals(True)
        self.sprite_combo.clear()
        self.sprite_combo.addItem(tr("— спрайт —", "— sprite —"), None)
        for e in self._entries:
            self.sprite_combo.addItem(e["label"], e)
        self.sprite_combo.blockSignals(False)
        self._sprite = None

        # палитры зон — те же, что на вкладке «Вся графика»
        zones = zonemod.parse_zones(rom.data, rom.version) if rom else []
        self.zone_pal.blockSignals(True)
        self.zone_pal.clear()
        self.zone_pal.addItem(tr("— палитра зоны —", "— zone palette —"), None)
        for z in zones:
            self.zone_pal.addItem(tr(f"Зона {z['index'] + 1} — грань A", f"Zone {z['index'] + 1} — face A"), z["palA"])
            self.zone_pal.addItem(tr(f"Зона {z['index'] + 1} — грань B", f"Zone {z['index'] + 1} — face B"), z["palB"])
        self.zone_pal.blockSignals(False)

        self.base.setMaximum(max(0, len(rom.data) - 1) if rom else 0)
        self.pal_offset.setMaximum(max(0, len(rom.data) - 2) if rom else 0)
        self.view.set_zoom(self.zoom.value())
        if zones:
            self.zone_pal.setCurrentIndex(1)  # зона 1, грань A
        elif rom and rom.version.startswith("Zero Tolerance"):
            self.pal_offset.blockSignals(True)
            # ZTU: палитра спрайтов 0x21B4 (подтв.), остальной ZT — стены 0x20F2
            self.pal_offset.setValue(0x21B4 if "Underground" in rom.version else 0x20F2)
            self.pal_offset.blockSignals(False)
        if self._entries:
            self.sprite_combo.setCurrentIndex(1)

    def _zone_pal_changed(self) -> None:
        off = self.zone_pal.currentData()
        if off is None:
            return
        self.gray.setChecked(False)
        self.pal_offset.setValue(off)  # вызовет _render

    def _nudge(self, delta: int) -> None:
        self.base.setValue(max(0, min(self.base.maximum(),
                                      self.base.value() + delta)))

    def _sprite_changed(self) -> None:
        e = self.sprite_combo.currentData()
        self._sprite = e
        self._tree = None
        rom = self.main.rom
        if e and rom:
            if e.get("a1"):
                self._tree = spritemod.parse_sprite(rom.data, e["a1"], self._fmt)
                base = self._tree["base"]
            else:
                base = e["base"] or spritemod.guess_tile_base(rom.data, e["end"])
            self.base.blockSignals(True)
            self.base.setValue(base)
            self.base.blockSignals(False)
        # список анимаций
        self.anim_combo.blockSignals(True)
        self.anim_combo.clear()
        if self._tree:
            self.anim_combo.addItem(tr("Все анимации", "All animations"), -1)
            for i, dirs in enumerate(self._tree["anims"]):
                self.anim_combo.addItem(tr(f"Аним {i} ({len(dirs)} напр.)", f"Anim {i} ({len(dirs)} dirs)"), i)
        self.anim_combo.blockSignals(False)
        self.anim_combo.setEnabled(self._tree is not None)
        self._render()

    def _palette(self):
        if self.gray.isChecked() or not self.main.rom:
            return pal.grayscale_palette()
        return pal.read_palette(self.main.rom.data, self.pal_offset.value())

    def _render(self, *args) -> None:
        rom = self.main.rom
        if rom is None or self._sprite is None:
            self.view.set_pixmap(QPixmap())
            return
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        base = self.base.value()
        if self._tree:
            self._render_tree(rom, colors, ti, base)
        else:
            self._render_flat(rom, colors, ti, base)

    def _filter(self, rom, frames):
        """Скрыть целиком-пустые кадры (все индексы тайлов = 0 — заглушки);
        и убрать дубликаты, если включён режим уникальных."""
        out = []
        seen = set()
        uniq = self.unique.isChecked()
        for f in frames:
            empty = all(spritemod.frame_tile(rom.data, f, r, c, self._fmt) == 0
                        for r in range(f["h"]) for c in range(f["w"]))
            if empty:
                continue  # пустой кадр-заглушка (в игре не проигрывается)
            if uniq:
                key = bytes(rom.data[f["off"]:f["off"] + spritemod.FRAME_SIZE])
                if key in seen:
                    continue
                seen.add(key)
            out.append(f)
        return out

    def _render_tree(self, rom, colors, ti, base) -> None:
        """Дерево: ряд = НАПРАВЛЕНИЕ (ракурс), столбец = кадр анимации."""
        anims = self._tree["anims"]   # [аним][направление][кадр]
        sel = self.anim_combo.currentData()
        chosen = ([(sel, anims[sel])] if (sel is not None and sel >= 0)
                  else list(enumerate(anims)))
        rows = []   # каждый ряд — список кадров одного направления
        for _ai, dirs in chosen:
            for frames in dirs:
                ff = self._filter(rom, frames)
                if ff:
                    rows.append(ff)
        all_frames = [f for fr in rows for f in fr]
        if not all_frames:
            self.info.setText(tr("Нет кадров в анимации", "No frames in this animation"))
            self.view.set_pixmap(QPixmap())
            self._sheet = None; self._grid = None
            return
        cw = max(f["w"] for f in all_frames) * 32
        ch = max(f["h"] for f in all_frames) * 32
        ncols = max(len(fr) for fr in rows)
        sheet = QImage(max(1, ncols) * cw, len(rows) * ch,
                       QImage.Format.Format_ARGB32)
        sheet.fill(0xFF202020)
        painter = QPainter(sheet)
        for r, frames in enumerate(rows):
            for c, frame in enumerate(frames):
                img = assemble_frame(rom.data, frame, base, colors, ti, self._fmt)
                painter.drawImage(c * cw, r * ch, img)
        painter.end()
        self._sheet = sheet
        self._grid = GridSpec(cw, ch, max(1, ncols), len(rows))
        self.view.set_pixmap(QPixmap.fromImage(sheet))
        total = sum(len(fr) for dirs in anims for fr in dirs)
        self.info.setText(tr(
            f"Враг a1=0x{self._tree['a1']:06X}: {len(anims)} анимаций, {total} кадров<br>"
            f"База графики: 0x{base:06X} (по формуле)<br>Ряд = направление (ракурс), столбец = кадр анимации",
            f"Enemy a1=0x{self._tree['a1']:06X}: {len(anims)} animations, {total} frames<br>"
            f"Graphics base: 0x{base:06X} (computed)<br>Row = direction (view), column = animation frame"))

    def _render_flat(self, rom, colors, ti, base) -> None:
        frames = self._filter(rom, spritemod.parse_frames(rom.data, self._sprite))
        if not frames:
            self.info.setText(tr("Кадры не распознаны", "Frames not recognized"))
            self.view.set_pixmap(QPixmap())
            self._sheet = None; self._grid = None
            return
        cw = max(f["w"] for f in frames) * 32
        ch = max(f["h"] for f in frames) * 32
        per = self.per_row.value()
        nrows = -(-len(frames) // per)
        sheet = QImage(per * cw, nrows * ch, QImage.Format.Format_ARGB32)
        sheet.fill(0xFF202020)
        painter = QPainter(sheet)
        for i, frame in enumerate(frames):
            img = assemble_frame(rom.data, frame, base, colors, ti, self._fmt)
            painter.drawImage((i % per) * cw, (i // per) * ch, img)
        painter.end()
        self._sheet = sheet
        self._grid = GridSpec(cw, ch, per, nrows)
        self.view.set_pixmap(QPixmap.fromImage(sheet))
        self.info.setText(tr(
            f"Спрайт {self._sprite['offset']:#08x}: {len(frames)} кадров<br>База графики: 0x{base:06X}",
            f"Sprite {self._sprite['offset']:#08x}: {len(frames)} frames<br>Graphics base: 0x{base:06X}"))


# Банк графики предметов/объектов (32×32 column-major, 512 б/тайл): (начало, число тайлов).
# Выверено по дизасму: HUD/объекты рисуются блиттером eda0 указателями вида
# `movea.l #$10E9BE,a1; bra eda0` (код @0x115B4 и далее). Указатели идут с шагом 0x200
# от 0x10E9BE до 0x116DBE = ровно 66 тайлов (совпадает с банком из zmap-tools).
# Содержимое: взрывы/эффекты, оружие, аптечка, канистры, HUD-портреты солдата,
# двери/лампы/огонь/кровь.
# BZT: банк предметов = Items{N}ep.bin (отдельный для каждой зоны; адрес и точный
# размер берутся из дескриптора зоны через zonemod.parse_zones). Здесь остаётся
# только общий банк ZT; прототипы больше не должны молча показывать лишь зону 1.
OBJECT_BANKS = {
    "Zero Tolerance": (0x10E9BE, 66),
}
OBJ_TILE_BYTES = 512
OBJ_TEX = 32
OBJ_STRETCH_CELL = 64   # размер ячейки в режиме «игровые пропорции»

# Пропорции отображения объектов (ширина:высота на экране). Ключ — индекс тайла в
# банке (0x10E9BE + i*0x200). Не указанные = 1:1 (квадрат).
#
# КАРТА ВЫВЕДЕНА ИЗ КОДА (символический разбор процедур отрисовки, см. ниже). Блиттер
# eda0 рисует тайл 32×32 как d4(ширина)×d0(высота). Аспект = d4:d0 на входе в eda0.
# Точки входа берутся из ДВУХ таблиц-диспетчеров «тип объекта → процедура»:
#   • @0x113F8 — ПИКАПЫ (оружие/аптечка/свет), процедуры 0x115CC..0x1166C;
#   • @0x114E0 — ДЕКОРАЦИИ/ЭФФЕКТЫ (растения/лампы/бомбы/лучи), 0x11868..0x119BE;
#   • 0x115A0 — эффект-блеск (тайл 0).
# Хелперы пропорций: 0x11A0E→1:2, 0x11A24→1:4, 0x11A3A→1:2, 0x11A52→1:1.
#
# КАРТА КОД-ТОЧНАЯ. Исчерпывающий байт-скан нашёл ВСЕ 36 вызовов eda0 в ROM (полный
# набор billboard-отрисовок банка 0x10E9BE). Аспект каждого тайла = d4:d0 на входе eda0.
#
# ПИКАПЫ (оружие #12–14,17 и аптечка #18) рисуются КВАДРАТОМ 1:1 (статич. дисп-таблица
# @0x113F8 → процедуры 0x115CC..0x1166C → хелпер 0x11A52, вызов eda0 @0x11A64), поэтому
# здесь они НЕ указаны (= 1:1 по умолчанию). Тайл #18 во всём ROM рисуется ТОЛЬКО так,
# квадратом — он НИГДЕ не растягивается. Широкая аптечка-кейс на полу (≈2.5:1 по скрину)
# — это ДРУГОЙ ассет (вероятно аппаратный VDP-спрайт пикапа), он НЕ входит в этот банк.
# Цепочка floor-предмета: think($e) → очередь команд 0x1C03A (длины 0x1C3F4) → flush
# 0x1C52C → дисп по типу команды (таблица 0x1C54C) → jmp $a(a0) → под-тип $3f (таблица
# 0x168DA) → eda0. Все billboard-рендеры дают 1:2/1:1, редко 2:1–4:1, НИКОГДА 2.5:1.
#
# #55 — конфликт: рисуется двумя процедурами (1:4 верт. @0x118BC и 4:3 гориз. @0x118DA);
# оставлен 1:4. #19,#50,#58 — РУЧНЫЕ (нет вызова eda0 в банке: рисует система стен/zmap).
ZT_OBJECT_ASPECT = {
    # --- из кода (подтверждено вызовами eda0) ---
    0: (5, 8),                     # эффект-блеск, 0x115A0 (5:8)
    3: (1, 2),                     # 0x115D6 → 11A3A
    15: (2, 1),                    # 0x11622 — ровно 2:1 (мешки/пилот)
    16: (1, 2), 20: (1, 2),        # 0x13F74 / 0x116A6 — вертикальные 1:2
    46: (1, 2), 47: (1, 2),        # растения, 0x11868/0x11872 → 11A0E
    48: (1, 2), 49: (1, 2),        # 0x118B8 / 0x11986
    51: (1, 4), 52: (1, 4), 54: (1, 4),   # лучи/лампы, 11A24
    53: (1, 2), 55: (1, 4),        # 0x11916; #55 конфликт (см. выше)
    56: (2, 1), 57: (4, 1),        # гориз. свет/снаряды, 0x11938/0x1195A
    60: (1, 2),                    # 0x115CC → 11A3A
    # --- ручные (нет вызова eda0 в банке объектов) ---
    19: (1, 2),                    # вертикальный прибор
    50: (2, 1), 58: (2, 1),        # гориз. эффект/мина (спец. depth-math)
}

# Пер-билд оверрайды/дополнения пропорций. Раскладка объектов по тайлам инвариантна к
# билду (тот же движок — см. CELLTYPE_TO_ITEM_OFFSET), поэтому ZT-таблица выше служит
# БАЗОЙ для всех билдов; здесь — только отличия: лишние тайлы ZTU (банк 145 > 66) и
# правки, если порядок/набор объектов у прото BZT расходится. Выводятся сканом eda0-
# аналогов конкретного билда (см. _obj_aspect). Пусто = пока совпадает с базой ZT.
OBJECT_ASPECT_OVERRIDE: dict = {
    "ZTU": {},     # тайлы 66..144 + расхождения (TODO: скан eda0-аналога ZTU)
    "June": {},    # BZT 1995-06-23 (TODO: сверить objdef 0xA256 / скан билборд-вызовов)
    "July": {},    # BZT 1995-07-14 (TODO: сверить objdef 0xA628)
}


def _obj_aspect_tag(version: str) -> str:
    """Тег билда для таблицы пропорций объектов ('ZT'|'ZTU'|'June'|'July')."""
    if "Underground" in version:
        return "ZTU"
    return _build_tag(version)


_PICKUP_TILES_CACHE = None
# Аспект floor-пикапа = ВЫВЕДЕН ИЗ КОДА floor-рендера (диспетчер $3f @0x16B74 → таблица
# 0x168DA). Единственный floor draw-класс, рисующий из банка объектов 0x10E9BE — $3f=5
# (FUN @0x16BDE): высота d0 = base*(0x10−$40)/16, ширина d4 = d0/2 → аспект 1:2 (вытянут
# вверх), размер пульсирует по кадру $40. Поэтому ВСЕ подбираемые предметы на полу = 1:2.
PICKUP_FLOOR_ASPECT = (2, 1)   # ШИРЕ (горизонтально) — подтверждено фидбеком в игре.
# NB: код floor-класса $3f=5 (0x16BDE) даёт ВНУТРЕННИЙ d4:d0 = 1:2, но на экране 3D-вид
# растягивает по горизонтали (либо d4/d0=высота/ширина транспонированы) → на экране 2:1.


def _pickup_item_tiles():
    """Тайлы ПОДБИРАЕМЫХ предметов на полу (оружие/аптечки/боезапас, celltype 0x19–0x26 =
    №1-14). В 3D-виде их рисует floor draw-класс $3f=5 (0x16BDE) из банка 0x10E9BE с
    пропорцией 1:2 (выше), а не квадратом. Раскладка инвариантна к билду (общий
    CELLTYPE_TO_ITEM_OFFSET)."""
    global _PICKUP_TILES_CACHE
    if _PICKUP_TILES_CACHE is None:
        _PICKUP_TILES_CACHE = frozenset(
            off // 0x200 for ct, off in CELLTYPE_TO_ITEM_OFFSET.items()
            if 0x19 <= ct <= 0x26)
    return _PICKUP_TILES_CACHE


def _obj_aspect(version: str, tile: int):
    """Пропорция (w,h) объекта tile для билда.
    Подбираемые предметы на полу (оружие/аптечки) — 1:2 (floor draw-класс $3f=5).
    Иначе: оверрайд билда → база ZT → 1:1."""
    if tile in _pickup_item_tiles():
        return PICKUP_FLOOR_ASPECT
    ov = OBJECT_ASPECT_OVERRIDE.get(_obj_aspect_tag(version))
    if ov and tile in ov:
        return ov[tile]
    return ZT_OBJECT_ASPECT.get(tile, (1, 1))


def _aspect_cell_size(w: int, h: int, cell: int):
    """Размер тайла в ячейке cell×cell при пропорции w:h (меньшая сторона = cell/2,
    бóльшая растягивается, но не больше cell — так квадраты остаются 32, а широкие
    объекты становятся видимо шире/уже)."""
    half = cell // 2
    if w >= h:
        pw, ph = round(half * w / h), half
        if pw > cell:
            pw, ph = cell, max(1, round(cell * h / w))
    else:
        pw, ph = half, round(half * h / w)
        if ph > cell:
            pw, ph = max(1, round(cell * w / h)), cell
    return pw, ph


def _object_bank(version: str):
    if "Underground" in version:           # ZTU: объект/предмет-банк 0xE0366..0xF2366
        return (0xE0366, 0x91)              # ~145 тайлов (конец блока 0x0F2326 от юзера)
    for key, val in OBJECT_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return None


class ObjectViewer(QWidget):
    """Вкладка «Предметы / объекты»: браузер графики объектов 32×32 (column-major).

    Объекты не имеют структурной таблицы (грузятся по индексу тайла из банка),
    поэтому показываем их сеткой 32×32-тайлов с ZT-палитрой; смещение под курсором
    помогает находить конкретный предмет. Экспорт — выделение или весь лист.
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._hover_off: Optional[int] = None
        self._bank = None   # (start, count) для текущей версии — гасит хвост за банком
        self._zone_banks = []
        self._cell = OBJ_TEX  # размер ячейки сетки (32 или OBJ_STRETCH_CELL)

        self.view = TileView()
        self.view.set_zoom(3)
        self.view.hoverMoved.connect(self._update_hover)
        self.view.selectionChanged.connect(self._update_range)

        panel = QWidget()
        panel.setFixedWidth(350)
        v = QVBoxLayout(panel)

        self.info = QLabel(tr("Загрузите ROM (вкладка «Вся графика»)", "Load a ROM (the «All graphics» tab)"))
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        form = QFormLayout()
        self.start = _hex_spin(0xFFFFFF, 0x10E9BE)
        self.start.valueChanged.connect(self._render)
        form.addRow(tr("Начало банка:", "Bank start:"), self.start)

        self.zone_label = QLabel(tr("Зона:", "Zone:"))
        self.zone_combo = QComboBox()
        self.zone_combo.currentIndexChanged.connect(self._zone_changed)
        form.addRow(self.zone_label, self.zone_combo)
        self.zone_label.setVisible(False)
        self.zone_combo.setVisible(False)

        nud = QHBoxLayout()
        for label, delta in [("−512", -512), ("−32", -32), ("−2", -2),
                             ("+2", 2), ("+32", 32), ("+512", 512)]:
            b = QPushButton(label)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nud.addWidget(b)
        form.addRow(tr("Подстройка:", "Nudge:"), nud)

        self.cols = QSpinBox()
        self.cols.setRange(1, 32)
        self.cols.setValue(8)
        self.cols.valueChanged.connect(self._render)
        form.addRow(tr("Объектов в ряд:", "Objects per row:"), self.cols)

        self.rows = QSpinBox()
        self.rows.setRange(1, 256)
        self.rows.setValue(9)    # 66 тайлов банка при 8/ряд = 9 рядов
        self.rows.valueChanged.connect(self._render)
        form.addRow(tr("Рядов:", "Rows:"), self.rows)

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 16)
        self.zoom.setValue(3)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        self.stretched = QCheckBox(tr("Как в игре (пропорции)", "Game proportions"))
        self.stretched.setToolTip(tr(
            "Показывать объекты в пропорциях, как их рисует игра: одни объекты в 3D-виде "
            "вытянуты по вертикали (лампы/лучи/растения 1:2..1:4), другие шире по "
            "горизонтали (свет/снаряды 2:1..4:1), пикапы — квадрат 1:1. Выкл. — исходные "
            "тайлы 32×32 (источник «приплюснут», т.к. движок растягивает его при рендере).",
            "Show objects in the proportions the game draws them: some are stretched vertically in the "
            "3D view (lamps/beams/plants 1:2..1:4), others wider horizontally (light/projectiles 2:1..4:1), "
            "pickups are square 1:1. Off — raw 32×32 tiles (the source looks squashed because the engine "
            "stretches it at render time)."))
        self.stretched.stateChanged.connect(self._render)
        v.addWidget(self.stretched)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()        # наполняется по версии в set_rom (itemData=офсет)
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, 0x20F2)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.range_lbl = QLabel(tr("Смещения: —", "Offsets: —"))
        self.range_lbl.setWordWrap(True)
        self.range_lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        v.addWidget(self.range_lbl)

        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def grid_state(self):
        if self.main.rom is None:
            return None
        img = self._compose()
        if img is None:
            return None
        cell = getattr(self, "_cell", OBJ_TEX)
        grid = GridSpec(cell, cell, self.cols.value(), self.rows.value())
        base = (os.path.splitext(os.path.basename(self.main.rom.path))[0]
                + f"_obj_{self.start.value():06X}")
        return img, grid, base

    def grid_cell_label(self, c, r):
        return f"tile{r * self.cols.value() + c:03d}"

    # ---------- данные ----------

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        bank = _object_bank(rom.version)
        self.start.setMaximum(max(0, rom.size - 1))
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        zones = zonemod.parse_zones(rom.data, rom.version)
        self._zone_banks = [
            {"index": z["index"], "bank": z["items"], "palette": z["palA"]}
            for z in zones if z.get("items")
        ]
        self.zone_combo.blockSignals(True)
        self.zone_combo.clear()
        for z in self._zone_banks:
            start, count = z["bank"]["offset"], z["bank"]["count"]
            self.zone_combo.addItem(
                tr(f"Зона {z['index'] + 1} — 0x{start:06X}, {count} тайл.",
                   f"Zone {z['index'] + 1} — 0x{start:06X}, {count} tiles"), z["index"])
        self.zone_combo.setCurrentIndex(0 if self._zone_banks else -1)
        self.zone_combo.blockSignals(False)
        has_zone_banks = bool(self._zone_banks)
        self.zone_label.setVisible(has_zone_banks)
        self.zone_combo.setVisible(has_zone_banks)
        if has_zone_banks:
            bank = self._zone_banks[0]["bank"]
        # палитра по умолчанию по версии (для ручного офсета): ZT 0x20F2, BZT — иные
        objpal = {"Beyond Zero Tolerance (прототип 1995-06-23)": 0xB9CBE,
                  "Beyond Zero Tolerance (прототип 1995-07-14)": 0x97BA0}
        defoff = None
        for k, off in objpal.items():
            if rom.version.startswith(k):
                defoff = off
                self.pal_offset.blockSignals(True)
                self.pal_offset.setValue(off)
                self.pal_offset.blockSignals(False)
                break
        # --- наполнить выбор палитр по версии (как в «Иконках»/«Фонах»): линии зон L0-L3,
        #     спец-палитры версии, ручной офсет, серый. itemData = смещение или спец-строка.
        bzt = not rom.version.startswith("Zero Tolerance")
        self.pal_mode.blockSignals(True)
        self.pal_mode.clear()
        if bzt:
            for z in zones:
                pa = z["palA"]
                for ln in range(4):
                    self.pal_mode.addItem(
                        tr(f"Зона{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})",
                           f"Zone{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})"), pa + ln * 0x20)
            if defoff is not None:
                self.pal_mode.addItem(tr(f"Объекты (0x{defoff:04X})", f"Objects (0x{defoff:04X})"), defoff)
        elif "Underground" in rom.version:
            self.pal_mode.addItem(tr("HUD/оружие (0x2194)", "HUD/weapon (0x2194)"), 0x2194)
            self.pal_mode.addItem(tr("Спрайты ZTU (0x21B4)", "ZTU sprites (0x21B4)"), 0x21B4)
        else:
            self.pal_mode.addItem(tr("HUD/оружие (0x20D2)", "HUD/weapon (0x20D2)"), 0x20D2)
            self.pal_mode.addItem(tr("Стены (0x20F2)", "Walls (0x20F2)"), 0x20F2)
        self.pal_mode.addItem(tr("Из ROM (смещение)", "From ROM (offset)"), "rom")
        self.pal_mode.addItem(tr("Оттенки серого", "Grayscale"), "gray")
        # дефолт: пункт с офсетом версии (BZT), ZTU 0x21B4 или «Стены 0x20F2» (ZT), иначе первый
        zt_def = 0x21B4 if "Underground" in rom.version else 0x20F2
        di = self.pal_mode.findData(defoff if defoff is not None else zt_def)
        self.pal_mode.setCurrentIndex(di if di >= 0 else 0)
        self.pal_mode.blockSignals(False)
        self._set_bank(bank, self._zone_banks[0] if has_zone_banks else None)
        self.view.set_zoom(self.zoom.value())
        self._render()

    def _set_bank(self, bank, zone=None) -> None:
        """Показать банк; у прототипов ``zone`` привязывает его к зоне/палитре."""
        if isinstance(bank, dict):
            bank = (bank["offset"], bank["count"])
        self._bank = bank
        if bank is None:
            self.info.setText(tr(
                f"<b>{version_label(self.main.rom.version)}</b><br>Адрес банка объектов для этой версии пока не задан — "
                "укажите «Начало банка» вручную.",
                f"<b>{version_label(self.main.rom.version)}</b><br>The object-bank address is not set for this version yet — "
                "enter «Bank start» manually."))
            return
        start, count = bank
        cols = self.cols.value()
        self.start.blockSignals(True)
        self.rows.blockSignals(True)
        self.start.setValue(start)
        self.rows.setValue(-(-count // cols))   # ceil — ровно весь банк
        self.rows.blockSignals(False)
        self.start.blockSignals(False)
        scope_ru = (f"Банк предметов/декораций зоны {zone['index'] + 1}"
                    if zone else "Банк объектов")
        scope_en = (f"Zone {zone['index'] + 1} item/decor bank"
                    if zone else "Object bank")
        self.info.setText(tr(
            f"<b>{version_label(self.main.rom.version)}</b><br>{scope_ru}: 0x{start:06X}, {count} тайлов 32×32<br>"
            "Эффекты, оружие, аптечка, канистры, HUD-портреты, двери/лампы/огонь.<br>"
            "Наведите курсор — смещение тайла; рамкой — выделение для экспорта.",
            f"<b>{version_label(self.main.rom.version)}</b><br>{scope_en}: 0x{start:06X}, {count} tiles 32×32<br>"
            "Effects, weapons, medkit, canisters, HUD portraits, doors/lamps/fire.<br>"
            "Hover for the tile offset; drag a box to select for export."))

    def _zone_changed(self) -> None:
        """Переключить BZT на банк предметов/декораций и палитру той же зоны."""
        index = self.zone_combo.currentData()
        zone = next((z for z in self._zone_banks if z["index"] == index), None)
        if zone is None:
            return
        self._set_bank(zone["bank"], zone)
        palette_index = self.pal_mode.findData(zone["palette"])
        if palette_index >= 0:
            self.pal_mode.blockSignals(True)
            self.pal_mode.setCurrentIndex(palette_index)
            self.pal_mode.blockSignals(False)
        self._render()

    def _palette(self) -> List[pal.RGB]:
        rom = self.main.rom
        if rom is None:
            return pal.grayscale_palette()
        data = self.pal_mode.currentData()
        if data == "gray":
            return pal.grayscale_palette()
        if data == "rom":                             # ручное смещение
            return pal.read_palette(rom.data, self.pal_offset.value())
        if isinstance(data, int):                     # выбранная палитра (зона/HUD)
            return pal.read_palette(rom.data, data)
        return pal.grayscale_palette()

    def _tile_count(self) -> Optional[int]:
        """Число тайлов банка, если просмотр стоит ровно на его начале (иначе None)."""
        if self._bank is not None and self.start.value() == self._bank[0]:
            return self._bank[1]
        return None

    def _compose(self) -> Optional[QImage]:
        """Собрать лист объектов сеткой ячеек self._cell (с учётом пропорций)."""
        rom = self.main.rom
        if rom is None:
            return None
        cols, rows = self.cols.value(), self.rows.value()
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        start = self.start.value()
        stretch = self.stretched.isChecked()
        self._cell = OBJ_STRETCH_CELL if stretch else OBJ_TEX
        cell = self._cell
        limit = self._tile_count()   # гасит хвост за банком

        sheet = QImage(cols * cell, rows * cell, QImage.Format.Format_ARGB32)
        sheet.fill(0)
        painter = QPainter(sheet)
        for t in range(cols * rows):
            if limit is not None and t >= limit:
                continue                      # за пределами банка — пусто (не мусор)
            tbuf = decode_one_tile(rom.data, start + t * OBJ_TILE_BYTES)
            img = build_qimage(tbuf, OBJ_TEX, OBJ_TEX, colors, ti)
            if stretch:
                w, h = _obj_aspect(rom.version, t) if limit is not None else (1, 1)
                pw, ph = _aspect_cell_size(w, h, cell)
                img = img.scaled(pw, ph, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.FastTransformation)
            cx, cy = (t % cols) * cell, (t // cols) * cell
            painter.drawImage(cx + (cell - img.width()) // 2,
                              cy + (cell - img.height()) // 2, img)
        painter.end()
        return sheet

    def _render(self, *_) -> None:
        sheet = self._compose()
        if sheet is None:
            self.view.set_pixmap(QPixmap())
            return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(sheet))
        self._update_range()

    # ---------- смещения под курсором / выделение ----------

    def _tile_at(self, sx: float, sy: float) -> Optional[int]:
        cols = self.cols.value()
        rows = self.rows.value()
        tx, ty = int(sx) // self._cell, int(sy) // self._cell
        if not (0 <= tx < cols and 0 <= ty < rows):
            return None
        return ty * cols + tx

    def _update_hover(self, sx: float, sy: float) -> None:
        t = self._tile_at(sx, sy)
        self._hover_off = None if t is None else self.start.value() + t * OBJ_TILE_BYTES
        self._update_range()

    def _sel_tiles(self):
        """(t0, t1) индексы тайлов выделения (включительно), либо None."""
        sel = self.view.selection
        if sel is None or sel.width() < 1 or sel.height() < 1:
            return None
        cols, rows = self.cols.value(), self.rows.value()
        c = self._cell
        x0 = max(0, int(sel.left()) // c)
        y0 = max(0, int(sel.top()) // c)
        x1 = min(cols - 1, int(sel.right() - 0.001) // c)
        y1 = min(rows - 1, int(sel.bottom() - 0.001) // c)
        if x1 < x0 or y1 < y0:
            return None
        return (x0, y0, x1, y1)

    def _update_range(self, *_) -> None:
        rom = self.main.rom
        st = self._sel_tiles()
        rect = None
        if st:
            x0, y0, x1, y1 = st
            c = self._cell
            rect = QRectF(x0 * c, y0 * c, (x1 - x0 + 1) * c, (y1 - y0 + 1) * c)
        self.view.set_selection_rect(rect)
        if rom is None:
            self.range_lbl.setText(tr("Смещения: —", "Offsets: —"))
            return
        base = self.start.value()
        lines = [tr(f"Банк: 0x{base:06X}", f"Bank: 0x{base:06X}")]
        if self._hover_off is not None:
            tn = (self._hover_off - base) // OBJ_TILE_BYTES
            lines.append(tr(f"Курсор: тайл #{tn} (0x{self._hover_off:06X})",
                            f"Cursor: tile #{tn} (0x{self._hover_off:06X})"))
        if st:
            cols = self.cols.value()
            x0, y0, x1, y1 = st
            t0, t1 = y0 * cols + x0, y1 * cols + x1
            s = base + t0 * OBJ_TILE_BYTES
            e = base + (t1 + 1) * OBJ_TILE_BYTES
            lines.append(tr(f"Выделение: #{t0}–#{t1}  0x{s:06X}–0x{e:06X} ({t1 - t0 + 1} тайл.)",
                            f"Selection: #{t0}–#{t1}  0x{s:06X}–0x{e:06X} ({t1 - t0 + 1} tiles)"))
        self.range_lbl.setText("   ".join(lines))

    def _nudge(self, delta: int) -> None:
        self.start.setValue(max(0, self.start.value() + delta))

    # ---------- экспорт ----------

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        c = self._cell
        st = self._sel_tiles()
        if st:
            x0, y0, x1, y1 = st
            img = img.copy(x0 * c, y0 * c, (x1 - x0 + 1) * c, (y1 - y0 + 1) * c)
        base = os.path.splitext(os.path.basename(rom.path))[0]
        suggested = f"{base}_obj_{self.start.value():06X}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), suggested, "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


# --- HUD-иконки оружия/предметов -----------------------------------------
# Таблица иконок @0x10AC8 (14 лонгов → 0x15846E, шаг 0x200); каждая иконка = 16
# нативных 8×8 4bpp тайлов, грузится в HUD-плоскость VRAM 0x9600 (загрузчик 0x10A7A,
# = BZT sub_106CC: копия 16 тайлов с зазором 0x18 под цифры боезапаса, sub_1ACBA).
# РАСКЛАДКА: 4×4 COLUMN-MAJOR (tile(col,row)=col*4+row) — как все 32×32-графики ZT/BZT.
# Сверено с BZT-дизасмом: данные @0x15846E байт-в-байт совпадают с Graphics/GUI/Icons/*.bin.
ICON_BANK = 0x15846E
ICON_W = 4               # тайлов в ряд (иконка 4×4 = 32×32 px)
ICON_TILES = 16          # тайлов на иконку
ICON_STEP = 0x200        # шаг между иконками (512б)
ICON_COUNT = 14
ICON_TILE_BYTES = 32
# ПАЛИТРА ИКОНОК = 0x20D2 — линия 3 эпизодной палитры (Pal_1epG), та же, что у оружия в
# руках (HELD_PAL). Иконки в кокпит-HUD делят HUD-линию: жёлтый текст названия, синий
# лазер, оранжевый дробовик, красная ракета/мина — сверено с эталон-скриншотом инвентаря.
# (BZT-прототип грузил отдельную off_16AE82=icon_pal с СИНИМ текстом — в релизе заменено.)
ICON_PAL = 0x20D2
# Запасная BZT icon_pal (синий текст прототипа) — на случай ручного сравнения.
ICON_PAL_BYTES = bytes.fromhex(
    "000004000a4208440222004004440ea80aaa022208880a880622000004400eee")
ICON_NAMES = [
    "Bio scanner", "Mine", "Bullet proof vest", "Fire extinguisher", "Fire proof suit",
    "Flashlight", "Hand grenade", "Handgun", "Night vision", "Laser aimed gun",
    "Rocket launcher", "Shotgun", "Flame thrower", "Pulse laser",
]
# Банк иконок по версии: (начало, число, смещение палитры). ZT — палитра HUD 0x20D2;
# BZT — отдельная icon_pal (128б, СИНИЙ текст прото) прямо перед банком (зазор 0x80).
# Адреса BZT по содержимому Graphics/GUI/Icons/*.bin (дизасм off_16AE82/bio_scan).
ICON_BANKS = {
    "Zero Tolerance": (0x15846E, 14, 0x20D2),
    "Beyond Zero Tolerance (прототип 1995-06-23)": (0x16AF02, 14, 0x16AE82),
    "Beyond Zero Tolerance (прототип 1995-07-14)": (0x162548, 14, 0x1624C8),
}


def _icon_bank(version: str):
    if "Underground" in version:           # ZTU: иконки 0x171A16 (таблица @0x10F40,
        return (0x171A16, 14, 0x2194)      # 14 шт шаг 0x200), палитра HUD 0x2194 — проверено
    for key, val in ICON_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return ICON_BANKS["Zero Tolerance"]


class IconViewer(QWidget):
    """Вкладка «Иконки»: HUD-иконки оружия/предметов. Каждая иконка = 16 тайлов
    8×8 4bpp в раскладке 4×4 COLUMN-MAJOR (как все 32×32-графики ZT/BZT), палитра
    icon_pal. Рисуются сеткой ячеек; наведение показывает имя и смещение, экспорт —
    наведённая иконка или весь лист."""

    GAP = 6                                            # зазор между ячейками, px

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._hover_icon: Optional[int] = None
        self._cell = ICON_W * 8                         # 32×32 px ячейка иконки

        self.view = TileView()
        self.view.set_zoom(5)
        self.view.hoverMoved.connect(self._update_hover)

        panel = QWidget()
        panel.setFixedWidth(350)
        v = QVBoxLayout(panel)

        self.info = QLabel(tr(
            "HUD-иконки оружия/предметов: 16 тайлов 8×8, раскладка 4×4 COLUMN-MAJOR, "
            "палитра HUD/оружия 0x20D2 (жёлтый текст).<br>Таблица 14 иконок @0x10AC8 → "
            "0x15846E, шаг 0x200.<br>В середине — зазор 0x18 под цифры боезапаса.",
            "HUD weapon/item icons: 16 tiles 8×8, 4×4 COLUMN-MAJOR layout, HUD/weapon palette 0x20D2 "
            "(yellow text).<br>Table of 14 icons @0x10AC8 → 0x15846E, stride 0x200.<br>"
            "A 0x18 gap in the middle is for the ammo digits."))
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        form = QFormLayout()
        self.start = _hex_spin(0xFFFFFF, ICON_BANK)
        self.start.valueChanged.connect(self._render)
        form.addRow(tr("Начало:", "Start:"), self.start)

        nud = QHBoxLayout()
        for label, delta in [("−512", -ICON_STEP), ("−32", -32), ("−2", -2),
                             ("+2", 2), ("+32", 32), ("+512", ICON_STEP)]:
            b = QPushButton(label)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nud.addWidget(b)
        form.addRow(tr("Подстройка:", "Nudge:"), nud)

        self.count = QSpinBox()
        self.count.setRange(1, 64)
        self.count.setValue(ICON_COUNT)
        self.count.valueChanged.connect(self._render)
        form.addRow(tr("Иконок:", "Icons:"), self.count)

        self.per_row = QSpinBox()
        self.per_row.setRange(1, 32)
        self.per_row.setValue(5)
        self.per_row.valueChanged.connect(self._render)
        form.addRow(tr("Иконок в ряд:", "Icons per row:"), self.per_row)

        self.colmajor = QCheckBox("Column-major 4×4")
        self.colmajor.setChecked(True)
        self.colmajor.setToolTip(tr(
            "Тайлы иконки читаются по столбцам (как VDP-спрайты ZT). Выкл. — row-major.",
            "Icon tiles read column-by-column (like ZT VDP sprites). Off — row-major."))
        self.colmajor.stateChanged.connect(self._render)
        form.addRow(self.colmajor)

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 24)
        self.zoom.setValue(5)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()        # наполняется по версии в set_rom (itemData=офсет)
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, ICON_PAL)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.range_lbl = QLabel(tr("Иконка: —", "Icon: —"))
        self.range_lbl.setWordWrap(True)
        self.range_lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        v.addWidget(self.range_lbl)

        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def grid_state(self):
        if self.main.rom is None:
            return None
        img = self._compose()
        if img is None:
            return None
        cols = self.per_row.value()
        rows = -(-self.count.value() // cols)
        step = self._cell + self.GAP
        grid = GridSpec(self._cell, self._cell, cols, rows, step, step)
        base = os.path.splitext(os.path.basename(self.main.rom.path))[0] + "_icon"
        return img, grid, base

    def grid_cell_label(self, c, r):
        i = r * self.per_row.value() + c
        nm = ICON_NAMES[i] if i < len(ICON_NAMES) else str(i)
        return f"{i:02d}_{nm}".replace(" ", "_")

    # ---------- данные ----------

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        self.start.setMaximum(max(0, rom.size - 1))
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        # банк/число/палитра иконок по версии (ZT vs BZT June/July)
        bank, count, paloff = _icon_bank(rom.version)
        self._icon_pal_off = paloff
        bzt = not rom.version.startswith("Zero Tolerance")
        self.start.blockSignals(True); self.count.blockSignals(True)
        self.start.setValue(bank); self.count.setValue(count)
        self.start.blockSignals(False); self.count.blockSignals(False)
        # --- наполнить выбор палитр по версии (itemData = смещение в ROM, или спец-строка)
        self.pal_mode.blockSignals(True)
        self.pal_mode.clear()
        if bzt:
            # линии палитр всех зон эпизода (L0-L3) + выделенная icon_pal
            zlist = zonemod.parse_zones(rom.data, rom.version) or []
            for z in zlist:
                pa = z["palA"]
                for ln in range(4):
                    self.pal_mode.addItem(
                        tr(f"Зона{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})",
                           f"Zone{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})"), pa + ln * 0x20)
            self.pal_mode.addItem(f"icon_pal (0x{paloff:04X})", paloff)
        elif "Underground" in rom.version:                  # ZTU: палитра иконок 0x2194
            self.pal_mode.addItem(tr(f"Иконки ZTU (0x{paloff:04X})", f"ZTU icons (0x{paloff:04X})"), paloff)
            self.pal_mode.addItem(tr("Стены/спрайты (0x21B4)", "Walls/sprites (0x21B4)"), 0x21B4)
        else:
            self.pal_mode.addItem(tr("HUD/оружие (0x20D2)", "HUD/weapon (0x20D2)"), 0x20D2)
            self.pal_mode.addItem(tr("Стены (0x20F2)", "Walls (0x20F2)"), 0x20F2)
        self.pal_mode.addItem(tr("Из ROM (смещение)", "From ROM (offset)"), "rom")
        self.pal_mode.addItem(tr("Оттенки серого", "Grayscale"), "gray")
        # дефолт: для BZT — «Зона1 L1» (жёлтый текст, естественные цвета), иначе пункт 0
        self.pal_mode.setCurrentIndex(1 if bzt and self.pal_mode.count() > 2 else 0)
        self.pal_mode.blockSignals(False)
        self.view.set_zoom(self.zoom.value())
        self._render()

    def _palette(self) -> List[pal.RGB]:
        rom = self.main.rom
        if rom is None:
            return pal.grayscale_palette()
        data = self.pal_mode.currentData()
        if data == "gray":
            return pal.grayscale_palette()
        if data == "rom":                             # ручное смещение
            return pal.read_palette(rom.data, self.pal_offset.value())
        if isinstance(data, int):                     # выбранная палитра (зона/icon_pal/HUD)
            return pal.read_palette(rom.data, data)
        return pal.read_palette(rom.data, getattr(self, "_icon_pal_off", ICON_PAL))

    def _cell_origin(self, i: int):
        """Левый-верхний угол ячейки иконки i (в пикселях листа)."""
        cols = self.per_row.value()
        step = self._cell + self.GAP
        return (i % cols) * step, (i // cols) * step

    def _icon_image(self, i: int, colors, ti) -> QImage:
        """Иконка i как 4×4 column-major (или row-major) блок 32×32."""
        off = self.start.value() + i * ICON_STEP
        if self.colmajor.isChecked():
            return _colmajor_block(self.main.rom.data, off, 0, ICON_TILES, ICON_W, colors, ti)
        buf, w, h = tiles.decode_sheet(self.main.rom.data, off, ICON_W, ICON_TILES // ICON_W)
        return build_qimage(buf, w, h, colors, ti)

    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        if rom is None:
            return None
        n = self.count.value()
        cols = self.per_row.value()
        rows = -(-n // cols)
        step = self._cell + self.GAP
        sheet = QImage(cols * step - self.GAP, rows * step - self.GAP,
                       QImage.Format.Format_ARGB32)
        sheet.fill(0)
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        painter = QPainter(sheet)
        for i in range(n):
            gx, gy = self._cell_origin(i)
            painter.drawImage(gx, gy, self._icon_image(i, colors, ti))
        painter.end()
        return sheet

    def _render(self, *_) -> None:
        sheet = self._compose()
        if sheet is None:
            self.view.set_pixmap(QPixmap())
            return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(sheet))
        self._update_range()

    # ---------- курсор / экспорт ----------

    def _icon_at(self, sx: float, sy: float) -> Optional[int]:
        step = self._cell + self.GAP
        cols = self.per_row.value()
        cx, cy = int(sx) % step, int(sy) % step
        if cx >= self._cell or cy >= self._cell:       # попал в зазор
            return None
        i = (int(sy) // step) * cols + (int(sx) // step)
        return i if 0 <= i < self.count.value() else None

    def _update_hover(self, sx: float, sy: float) -> None:
        self._hover_icon = self._icon_at(sx, sy)
        self._update_range()

    def _update_range(self, *_) -> None:
        if self.main.rom is None:
            self.range_lbl.setText(tr("Иконка: —", "Icon: —"))
            return
        i = self._hover_icon
        if i is None:
            self.range_lbl.setText(tr(f"Иконок: {self.count.value()} от 0x{self.start.value():06X}",
                                      f"Icons: {self.count.value()} from 0x{self.start.value():06X}"))
            return
        off = self.start.value() + i * ICON_STEP
        name = ICON_NAMES[i] if i < len(ICON_NAMES) else "?"
        self.range_lbl.setText(f"#{i} {name}\n0x{off:06X}–0x{off + ICON_STEP:06X}")

    def _nudge(self, delta: int) -> None:
        self.start.setValue(max(0, self.start.value() + delta))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        i = self._hover_icon
        if i is not None:                              # одна наведённая иконка
            img = self._icon_image(i, self._palette(),
                                   0 if self.transparent.isChecked() else None)
            tag = f"{ICON_NAMES[i] if i < len(ICON_NAMES) else i}".replace(" ", "_")
            suggested = f"icon_{i:02d}_{tag}.png"
        else:                                          # весь лист
            img = self._compose()
            suggested = f"icons_{self.start.value():06X}.png"
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{suggested}", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


# --- Оружие в руках (FPS-вид) --------------------------------------------
# Held-оружие = VDP-СПРАЙТЫ, тайлы 8×8 в порядке COLUMN-MAJOR (tile(col,row)=col*H+row).
# ZT-релиз: таблица @0x11C98 (ID оружия→графика), 9 блоков по 0x2A0=672б=21 тайл подряд
# от 0x16C2B8. Грузятся DMA в VRAM (sub_744). Палитра 0x20F2 (стены) даёт телесные руки +
# сине-серый металл; зелёные палитры = ночное видение. BZT — таблица off_1182E (адреса иные).
# BZT: 8 стволов по 672б подряд от Hands/-графики (lazer/fire_ext/rifle/gren/pistol/
# gunrock/lazer2/fire_drag), адреса по содержимому Hands/*.bin. June 0x16DE7A, July 0x15DB78.
# Перед стволами лежат руки/удары (hands/hand_punch/hand3/leg_kick) иных размеров.
HELD_BANKS = {
    "Zero Tolerance": (0x16C2B8, 9),   # (начало графики, число уникальных стволов)
    # June: 8 стволов от lazer_h (далее font_num/font_let, не оружие).
    "Beyond Zero Tolerance (прототип 1995-06-23)": (0x16DE7A, 8),
    # July: 10 стволов! Набор шире June — idx 4 (0x15E5F8) и idx 9 (0x15F318) = НОВЫЕ
    # July-оружия (одно = SNOMAN/морозомёт, есть в иконках July). Дальше idx 10+ — мусор.
    "Beyond Zero Tolerance (прототип 1995-07-14)": (0x15DB78, 10),
}
HELD_TILES = 21          # тайлов на ствол (672б/32)
HELD_COLW = 3            # ширина «тела» по умолчанию (спрайт 3×4)
HELD_BODY_TILES = 12     # тайлов в «теле»; остаток = кадр выстрела/вспышка (3×N)
HELD_FLASH_W = 3         # ширина блока вспышки
# Палитра оружия ZT = 0x20D2 (спрайт-линия палитры эпизода-1, прямо перед стенами 0x20F2):
# синие перчатки + серебро ствола + жёлто-оранж-красная вспышка. Найдено сверкой с BZT-дизасмом
# (Pal_1epG линия3) — точное совпадение в ROM. Зелёные палитры = ночное видение (не сюда).
HELD_PAL = 0x20D2
# Палитра оружия по версии = спрайт-линия (line 3 = palA+0x60) активной палитры зоны.
# BZT: June зона0 0xB9CBE+0x60=0xB9D1E, July зона0 0x97BA0+0x60=0x97C00 (серый металл +
# телесные руки + жёлто-красная вспышка + красное пламя). Зависит от зоны/эпизода —
# дефолт зоны 0; иные эпизоды выбираются через «Из ROM (смещение)».
HELD_PALS = {
    "Zero Tolerance": 0x20D2,
    "Beyond Zero Tolerance (прототип 1995-06-23)": 0xB9D1E,
    "Beyond Zero Tolerance (прототип 1995-07-14)": 0x97C00,
}


def _held_pal(version: str) -> int:
    if "Underground" in version:           # ZTU HUD/оружие = sprites 0x21B4 − 0x20
        return 0x2194
    for key, val in HELD_PALS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return HELD_PAL


# Ширина тела отдельных стволов (индекс→ширина): у ГРАНАТОМЁТА/РАКЕТНИЦЫ (gunrock) тело
# 4×3, а не 3×4 — иначе «вразброс». Индекс зависит от версии (порядок стволов разный):
# ZT #6, June #5, July #6 (проверено сверкой рендеров 3×4 vs 4×3 — в 4×3 видна ракетница).
HELD_BODY_OVERRIDES = {
    "Zero Tolerance": {6: 4},
    "Beyond Zero Tolerance (прототип 1995-06-23)": {5: 4},
    "Beyond Zero Tolerance (прототип 1995-07-14)": {6: 4},
}


def _held_overrides(version: str) -> dict:
    for key, val in HELD_BODY_OVERRIDES.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return {}


# Удар ногой (melee) — графика ВНЕ таблицы оружия. ZT: 0x16C038 (0-15 сапог 4×4, 16-19
# лежащая нога 4×1). BZT: leg_kick_h (Hands/Leg_kick.bin, 640б=20 тайлов) — June 0x16D75A,
# July 0x15D458 (адреса по содержимому). Палитра — спрайт-линия зоны (та же, что оружие).
MELEE_KICK_GFX = 0x16C038
MELEE_KICKS = {
    "Zero Tolerance": 0x16C038,
    "Beyond Zero Tolerance (прототип 1995-06-23)": 0x16D75A,
    "Beyond Zero Tolerance (прототип 1995-07-14)": 0x15D458,
}


def _held_kick(version: str) -> int:
    for key, val in MELEE_KICKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return MELEE_KICK_GFX

# ТОЧНАЯ палитра оружия (синтетическая, собрана из эталон-скриншотов: стреляющий пистолет).
# Единой ROM-палитры «синие перчатки + серебро ствола + жёлтая вспышка» нет (спрайт-палитра
# игры составная/динамическая), поэтому роли индексов выведены гистограммой тела/вспышки:
# ствол=1,2,8-10 (серебро/серый); вспышка=3-7 (тёмно-красн→оранж→жёлтый); перчатка=11,12,15
# (синяя); цвета сэмплированы со скриншотов (перчатка (36,72,144), ствол (216,216,216),
# вспышка (252,252,108)). Ночное видение красит сцену зелёным — это другой режим.
HELD_TRUE_PAL = [
    (0, 0, 0), (216, 216, 216), (144, 150, 160), (144, 0, 0),
    (224, 80, 0), (252, 150, 40), (252, 216, 96), (252, 252, 112),
    (96, 100, 116), (60, 64, 84), (184, 188, 200), (36, 72, 144),
    (0, 32, 104), (110, 76, 40), (250, 250, 250), (104, 144, 216),
]


def _colmajor_block(data, off, start_tile, ntiles, W, colors, ti):
    """QImage: ntiles тайлов 8×8 в COLUMN-MAJOR порядке (как читает VDP-спрайт),
    блок шириной W. tile(col,row) = start_tile + col*H + row."""
    H = -(-ntiles // W)
    img = QImage(W * 8, H * 8, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    for col in range(W):
        for row in range(H):
            k = col * H + row
            if k >= ntiles:
                continue
            buf, _, _ = tiles.decode_sheet(data, off + (start_tile + k) * 32, 1, 1)
            painter.drawImage(col * 8, row * 8, build_qimage(buf, 8, 8, colors, ti))
    painter.end()
    return img


def _held_bank(version: str):
    if "Underground" in version:           # ZTU: held-оружие 0x17936C (таблица @0x1264A,
        return (0x17936C, 9)               # 9 стволов по 0x2A0; шаг подтверждён)
    for key, val in HELD_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return None


class WeaponViewer(QWidget):
    """Вкладка «Оружие в руках»: FPS-стволы. Held-графика = VDP-спрайты, тайлы
    COLUMN-MAJOR — поэтому рисуем каждый ствол блоком W×H по столбцам. Палитра 0x20F2
    (истинные цвета: телесные руки, металл); зелёные палитры = ночное видение."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._hover_w: Optional[int] = None

        self.view = TileView()
        self.view.set_zoom(4)
        self.view.hoverMoved.connect(self._update_hover)

        panel = QWidget()
        panel.setFixedWidth(350)
        v = QVBoxLayout(panel)

        self.info = QLabel(
            tr("Оружие в руках (FPS-вид): VDP-спрайты, тайлы COLUMN-MAJOR.<br>"
            "ZT-релиз: таблица @0x11C98, 9 стволов по 21 тайлу от 0x16C2B8.<br>"
            "Палитра 0x20F2 = истинные цвета (зелёные = ночное видение).", "Held weapons (FPS view): VDP sprites, COLUMN-MAJOR tiles.<br>ZT release: table @0x11C98, 9 guns of 21 tiles each from 0x16C2B8.<br>Palette 0x20F2 = true colors (green = night vision)."))
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        form = QFormLayout()
        self.start = _hex_spin(0xFFFFFF, HELD_BANKS["Zero Tolerance"][0])
        self.start.valueChanged.connect(self._render)
        form.addRow(tr("Начало графики:", "Graphics start:"), self.start)

        nud = QHBoxLayout()
        for label, delta in [("−672", -0x2A0), ("−32", -32), ("−2", -2),
                             ("+2", 2), ("+32", 32), ("+672", 0x2A0)]:
            b = QPushButton(label)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nud.addWidget(b)
        form.addRow(tr("Подстройка:", "Nudge:"), nud)

        self.tiles_per = QSpinBox()
        self.tiles_per.setRange(1, 64)
        self.tiles_per.setValue(HELD_TILES)
        self.tiles_per.valueChanged.connect(self._render)
        form.addRow(tr("Тайлов на ствол:", "Tiles per gun:"), self.tiles_per)

        self.colw = QSpinBox()
        self.colw.setRange(1, 16)
        self.colw.setValue(HELD_COLW)
        self.colw.valueChanged.connect(self._render)
        form.addRow(tr("Ширина (тайлов):", "Width (tiles):"), self.colw)

        self.body_tiles = QSpinBox()
        self.body_tiles.setRange(1, 64)
        self.body_tiles.setValue(HELD_BODY_TILES)
        self.body_tiles.valueChanged.connect(self._render)
        form.addRow(tr("Тайлов в теле:", "Body tiles:"), self.body_tiles)

        self.count = QSpinBox()
        self.count.setRange(1, 64)
        self.count.setValue(HELD_BANKS["Zero Tolerance"][1])
        self.count.valueChanged.connect(self._render)
        form.addRow(tr("Стволов:", "Guns:"), self.count)

        self.per_row = QSpinBox()
        self.per_row.setRange(1, 32)
        self.per_row.setValue(9)
        self.per_row.valueChanged.connect(self._render)
        form.addRow(tr("Стволов в ряд:", "Guns per row:"), self.per_row)

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 16)
        self.zoom.setValue(4)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        self.assembled = QCheckBox(tr("Сборка (тело + вспышка)", "Assembled (body + muzzle flash)"))
        self.assembled.setChecked(True)
        self.assembled.setToolTip(
            tr("Собирать ствол как игра: тело (первые «тайлов в теле», блок 3×4) + "
            "кадр выстрела/вспышки (остаток). Выкл. — сырой column-major.", "Assemble the gun like the game: body (the first «body tiles», a 3×4 block) + the shot/muzzle-flash frame (the remainder). Off — raw column-major."))
        self.assembled.stateChanged.connect(self._render)
        v.addWidget(self.assembled)

        self.show_kick = QCheckBox(tr("Удар ногой (3 кадра)", "Kick (3 frames)"))
        self.show_kick.setToolTip(
            tr("Добавить кадры анимации удара ногой (графика 0x16C038, вне таблицы оружия): "
            "лежащая нога (тайлы 16-19) → поднятый сапог (0-15, 4×4) → лежащая нога. "
            "Работает в режиме «Сборка».", "Add kick animation frames (graphics 0x16C038, outside the weapon table): leg down (tiles 16-19) → raised boot (0-15, 4×4) → leg down. Works in «Assembly» mode."))
        self.show_kick.stateChanged.connect(self._render)
        v.addWidget(self.show_kick)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()
        self.pal_mode.addItems(
            [tr("Точная (0x20D2)", "Exact (0x20D2)"), tr("Из ROM (смещение)", "From ROM (offset)"), tr("Стены (0x20F2)", "Walls (0x20F2)"), tr("Оттенки серого", "Grayscale")])
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, HELD_PAL)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.range_lbl = QLabel(tr("Смещения: —", "Offsets: —"))
        self.range_lbl.setWordWrap(True)
        self.range_lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        v.addWidget(self.range_lbl)

        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)
        self._cellw = 0
        self._cellh = 0

    def grid_state(self):
        if self.main.rom is None:
            return None
        img = self._compose()
        if img is None or not self._cellw or not self._cellh:
            return None
        cols = self.per_row.value()
        kick = self.assembled.isChecked() and self.show_kick.isChecked()
        total = self.count.value() + (3 if kick else 0)
        rows = -(-total // cols)
        grid = GridSpec(self._cellw, self._cellh, cols, rows)
        base = (os.path.splitext(os.path.basename(self.main.rom.path))[0]
                + f"_weapon_{self.start.value():06X}")
        return img, grid, base

    def grid_cell_label(self, c, r):
        return f"w{r * self.per_row.value() + c:02d}"

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        self.start.setMaximum(max(0, rom.size - 1))
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        bank = _held_bank(rom.version)
        if bank is not None:
            start, count = bank
            self.start.blockSignals(True)
            self.count.blockSignals(True)
            self.start.setValue(start)
            self.count.setValue(count)
            self.start.blockSignals(False)
            self.count.blockSignals(False)
            hp = _held_pal(rom.version)
            self.pal_offset.blockSignals(True)
            self.pal_offset.setValue(hp)
            self.pal_offset.blockSignals(False)
            self.pal_mode.setItemText(0, tr(f"Точная (0x{hp:04X})", f"Exact (0x{hp:04X})"))
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Оружие в руках: {count} стволов от "
                f"0x{start:06X}, по 21 тайлу (column-major).<br>"
                f"Палитра 0x{hp:04X} = спрайт-линия зоны (серый металл + руки + вспышка).", f"<b>{version_label(rom.version)}</b><br>Held weapons: {count} guns from "
                f"0x{start:06X}, 21 tiles each (column-major).<br>"
                f"Palette 0x{hp:04X} = zone sprite line (gray metal + hands + muzzle flash)."))
        else:
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Таблица held-оружия для этой версии не задана "
                "— укажите «Начало графики» вручную.", f"<b>{version_label(rom.version)}</b><br>Held-weapon table for this version is not defined "
                "— set the «Graphics base» manually."))
        self.view.set_zoom(self.zoom.value())
        self._render()

    def _palette(self) -> List[pal.RGB]:
        rom = self.main.rom
        idx = self.pal_mode.currentIndex()
        if rom is None:
            return pal.grayscale_palette()
        if idx == 0:                                  # Точная (палитра оружия эпизода)
            return pal.read_palette(rom.data, _held_pal(rom.version))
        if idx == 1:                                  # из ROM по смещению
            return pal.read_palette(rom.data, self.pal_offset.value())
        if idx == 2:                                  # палитра стен
            return pal.read_palette(rom.data, 0x20F2)
        return pal.grayscale_palette()

    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data
        start = self.start.value()
        tpw = self.tiles_per.value()
        n = self.count.value()
        W = self.colw.value()
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        assemble = self.assembled.isChecked()
        bt = min(self.body_tiles.value(), tpw)
        ovr = _held_overrides(rom.version)        # ширина тела по индексу (пер-версия)
        kick_gfx = _held_kick(rom.version)        # графика удара ногой (пер-версия)

        kick = assemble and self.show_kick.isChecked()   # удар ногой — доп. ячейки

        # размер ячейки одного ствола (учёт стволов с телом 4×3 и удара ногой 4-шир)
        flash_n = max(0, tpw - bt)
        if assemble:
            kw = 4 if kick else 0
            max_bw = max([W, kw] + [ovr.get(w, W) for w in range(n)])
            cellw = max(max_bw, HELD_FLASH_W) * 8 + 6
            flash_h = (-(-flash_n // HELD_FLASH_W) * 8) if flash_n else 0
            cellh = 4 * 8 + (flash_h + 4 if flash_n else 0) + 14   # тело ≤4 ряда
        else:
            cellh = -(-tpw // W) * 8 + 6
            cellw = W * 8 + 6
        self._cellw, self._cellh = cellw, cellh

        cols = self.per_row.value()
        nk = 3 if kick else 0                  # 3 кадра удара (низ→сапог→низ)
        rows = -(-(n + nk) // cols)
        sheet = QImage(cols * cellw, rows * cellh, QImage.Format.Format_ARGB32)
        sheet.fill(0)
        painter = QPainter(sheet)
        for w in range(n):
            woff = start + w * tpw * 32          # стволы лежат подряд по tpw тайлов
            gx = (w % cols) * cellw
            gy = (w // cols) * cellh
            if assemble:
                bw = ovr.get(w, W)   # 3 обычно, 4 у ZT-гранатомёта
                body = _colmajor_block(d, woff, 0, bt, bw, colors, ti)
                painter.drawImage(gx, gy, body)
                if flash_n > 0:
                    flash = _colmajor_block(d, woff, bt, flash_n, HELD_FLASH_W, colors, ti)
                    painter.drawImage(gx, gy + 4 * 8 + 2, flash)
            else:
                painter.drawImage(gx, gy, _colmajor_block(d, woff, 0, tpw, W, colors, ti))
        if kick:
            # 3 кадра удара ногой как доп. ячейки (по аналогии со стволами)
            for i, knd in enumerate(("low", "boot", "low")):
                c = n + i
                gx = (c % cols) * cellw
                gy = (c // cols) * cellh
                if knd == "boot":                # поднятый сапог: тайлы 0-15, 4×4
                    painter.drawImage(gx, gy,
                                      _colmajor_block(d, kick_gfx, 0, 16, 4, colors, ti))
                else:                            # лежащая нога: тайлы 16-19, гориз. 4×1, у «пола»
                    low = _colmajor_block(d, kick_gfx, 16, 4, 4, colors, ti)
                    painter.drawImage(gx, gy + 4 * 8 - 8, low)
        painter.end()
        return sheet

    def _render(self, *_) -> None:
        sheet = self._compose()
        if sheet is None:
            self.view.set_pixmap(QPixmap())
            return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(sheet))
        self._update_range()

    def _update_hover(self, sx: float, sy: float) -> None:
        if self._cellw and self._cellh:
            cols = self.per_row.value()
            wx, wy = int(sx) // self._cellw, int(sy) // self._cellh
            w = wy * cols + wx
            self._hover_w = w if 0 <= w < self.count.value() else None
        self._update_range()

    def _update_range(self, *_) -> None:
        rom = self.main.rom
        if rom is None:
            self.range_lbl.setText(tr("Смещения: —", "Offsets: —"))
            return
        tpw = self.tiles_per.value()
        lines = [tr(f"Начало: 0x{self.start.value():06X}  ({self.count.value()} стволов × {tpw} т.)", f"Start: 0x{self.start.value():06X}  ({self.count.value()} guns × {tpw} t.)")]
        if self._hover_w is not None:
            off = self.start.value() + self._hover_w * tpw * 32
            lines.append(tr(f"Ствол #{self._hover_w}: 0x{off:06X}", f"Gun #{self._hover_w}: 0x{off:06X}"))
        self.range_lbl.setText("   ".join(lines))

    def _nudge(self, delta: int) -> None:
        self.start.setValue(max(0, self.start.value() + delta))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        suggested = f"{base}_weapons_{self.start.value():06X}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), suggested, "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


# --- Фоны (панорамы неба/города/космоса) ---------------------------------
# Фон 3D-вида = ПАНОРАМА из стандартных 8×8 4bpp тайлов, лежащих В ПОРЯДКЕ ПАНОРАМЫ
# (линейный рендер при ВЕРНОЙ ширине = собранный фон). ZT: код @0x2A1C-0x2B6E грузит
# палитру уровня (64 цв/4 линии с 0x20F2 или 0x2072 по флагам) + тайлы фона в VRAM 0x2820
# (=тайл 0x141) — банк по флагу -$58e6: ГОРОД 0x14F046 (518т, ZTSky2) / КОСМОС 0x154406
# (127т, world-pano). Фон = ПАЛИТРА-ЛИНИЯ 2 набора (тайлкарта-слова 0x41xx). Город ширина
# 128 (=1024px), палитра 0x2132 (0x20F2+0x40). Космос — линия 2 набора 0x21F2 = 0x2232.
# BZT: Plan_A панорама (June ep1 0xBD6FE…), палитра — линия зоны (подбирается).
# Стены/двери — система ZMAP (ztedit.ini, «Episode map data format»). ZMAP-блок эпизода:
# [4 «ZMAP»][4096 texture-defs: 256 МЕТАтекстур × 8 тайл-номеров 2б BE; метатекстура=128×64 из
# 8 текстур 32×32, порядок 1357/2468 (сетка 4×2: тайл i → col=i//2,row=i%2)][2048 texture-orders:
# по 8 байт на cell ID = метатекстуры на гранях][256 cell-types (1=стена,6=гор.дверь,7=верт.дверь,
# углы,лестницы)][16 env][16384 карта]. ZT: один общий банк 0x12EF26 для всех эп., различие палитрой.
# BZT: свой банк на эпизод (тайлы метатекстуры лежат подряд = «хранятся полностью»). Текстура стены
# собирается из 8 кусков 32×32 + палитра стен (Pal_NepG линия 4). Запись: (имя, zmap, банк, палитра).
WALL_BANKS = {
    "Zero Tolerance": [
        (tr("Эпизод 1", "Episode 1"), 0x15A106, 0x12EF26, 0x20F2),
        (tr("Эпизод 2", "Episode 2"), 0x160420, 0x12EF26, 0x20F2),
        (tr("Эпизод 3", "Episode 3"), 0x166028, 0x12EF26, 0x20F2),
    ],
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        (tr("Эпизод 1", "Episode 1"), 0xAAE2C, 0xC883E, 0xB9CBE),
        (tr("Эпизод 2", "Episode 2"), 0xAF4F2, 0xF7C8A, 0xB9DBE),
        (tr("Эпизод 3", "Episode 3"), 0xB44BA, 0x12D582, 0xB9FBE),
        (tr("Эпизод 4 (неисп.)", "Episode 4 (unused)"), 0x148582, 0x12D582, 0xB9FBE),  # экстра-ZMAP, банк E3 (своего нет)
        (tr("Эпизод 5 (неисп.)", "Episode 5 (unused)"), 0x14E18A, 0x12D582, 0xB9FBE),
    ],
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Эпизод 1", "Episode 1"), 0x83D38, 0xA5FF8, 0x97BA0),
        (tr("Эпизод 2", "Episode 2"), 0x8848E, 0xD433C, 0x97CA0),
        (tr("Эпизод 3", "Episode 3"), 0x8D528, 0x100E64, 0x97DA0),
        (tr("Эпизод 4 (инопланетный)", "Episode 4 (alien)"), 0x91B40, 0x12DCF8, 0x97EA0),
    ],
}


# ZTU (Underground): один общий ZMAP @0x1736AE, палитра 0x21B4 (подтв. юзером).
# Банк тайлов стен — per-level из RAM `-0x718c,A6`; 3D-рендер @0xCFB8 `movea.l (-0x718c,A6),A1;
# adda.l tile<<9,A1` (tile-графика = банк + tile*512, как US @0xCD12). Level-load (@0xE62/0xE78)
# ставит -0x718c = 0x10A6CE (субвей) или 0x12A6CE (индустрия/реактор). Найдено по дизасму,
# ячейки/анимация строятся верно (свип/«начало блока» 0x10A526 был смещён на 0x1A8).
WALL_BANKS_ZTU = [
    (tr("Субвей (0x10A6CE)", "Subway (0x10A6CE)"), 0x1736AE, 0x10A6CE, 0x21B4),
    (tr("Индустрия (0x12A6CE)", "Industrial (0x12A6CE)"), 0x1736AE, 0x12A6CE, 0x21B4),
    (tr("Тёмная/реактор (0x1494CE)", "Dark/reactor (0x1494CE)"), 0x1736AE, 0x1494CE, 0x21B4),
]


def _wall_bank(version: str):
    if "Underground" in version:
        return WALL_BANKS_ZTU
    for key, val in WALL_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


# Определения ячеек карты (из ztedit.ini [CellDefs]/[BCellDefs]): cell_id → (тип, имя). Тип: 0=пусто,
# 1=стена, 2-5=углы, 6=гор.дверь, 7=верт.дверь, 9=старт, 10=враг, 11=?, 12=оружие, 13=предмет,
# 14=лестн.стены, 15=РАЗРУШАЕМАЯ стена, 16=декор/труп, 8=окружение/спец. BZT — переопределения поверх ZT.
CELL_DEFS_ZT = {0: (0, 'Empty cell'), 1: (1, 'Wall'), 2: (2, 'Upper-right corner'), 3: (3, 'Lower-right corner'), 4: (4, 'Lower-left corner'), 5: (5, 'Upper-left corner'), 6: (6, 'Horizontal door'), 7: (7, 'Vertical door'), 8: (8, 'Environment: Bright'), 9: (8, 'Environment: Dim'), 10: (8, 'Environment: Haze'), 11: (8, 'Environment: No ceiling'), 12: (14, 'Stair walls - up, bottom'), 13: (14, 'Stair walls - interst., up'), 14: (14, 'Stair walls - transition, bottom'), 15: (14, 'Stair walls - interst., down'), 16: (14, 'Stair walls - down, bottom'), 17: (14, 'Stair central walls - bottom'), 18: (8, 'Stairs - up, bottom'), 19: (8, 'Stairs entrance - bottom'), 20: (8, 'Stairs - down, bottom'), 21: (8, 'Stairs interstorey - up'), 22: (8, 'Stairs interstorey - down'), 23: (8, 'Stairs transition - bottom'), 24: (8, 'Flame'), 25: (13, 'Bio Scanner'), 26: (12, 'Mine'), 27: (13, 'Bulletproof vest'), 28: (13, 'Fire extinguisher'), 29: (13, 'Fireproof suit'), 30: (13, 'Flashlight'), 31: (12, 'Hand grenade'), 32: (12, 'Handgun'), 33: (13, 'Night vision'), 34: (12, 'Laser aimed gun'), 35: (12, 'Rocket launcher'), 36: (12, 'Shotgun'), 37: (13, 'Medipack'), 38: (8, 'Camera'), 39: (8, 'Invisible snipers (1)'), 40: (8, 'Invisible snipers (2)'), 41: (10, 'Former Human Sergeant'), 42: (10, 'Former Human'), 43: (10, 'Imp'), 44: (16, 'Sergeant corpse'), 45: (16, 'Fake horizontal door'), 46: (16, 'Fake Vertical door'), 47: (8, 'Enemy blocker'), 48: (14, 'Elevator walls'), 49: (8, 'Elevator area - left'), 50: (8, 'Elevator - up, left'), 51: (8, 'Elevator - down, left'), 52: (8, 'Elevator - up/down, left'), 53: (16, 'Burnt remains'), 54: (12, 'Pulse laser'), 55: (16, 'Tree 1'), 56: (14, 'Stair walls - up, top'), 57: (14, 'Stair walls - transition, top'), 58: (14, 'Stair walls - down, top'), 59: (14, 'Stair central walls - top'), 60: (8, 'Stairs - up, top'), 61: (8, 'Stairs entrance - top'), 62: (8, 'Stairs - down, top'), 63: (8, 'Stairs transition - top'), 64: (14, 'Stair walls (12)'), 65: (14, 'Stair walls (13)'), 66: (14, 'Stair walls (14)'), 67: (14, 'Stair walls (15)'), 68: (8, 'Stairs 10'), 69: (8, 'Stairs 11'), 70: (8, 'Stairs 12'), 71: (8, 'Stairs 13'), 72: (14, 'Stair walls (16)'), 73: (14, 'Stair walls (17)'), 74: (14, 'Stair walls (18)'), 75: (14, 'Stair walls (19)'), 76: (8, 'Stairs 14'), 77: (8, 'Stairs 15'), 78: (8, 'Stairs 16'), 79: (8, 'Stairs 17'), 80: (8, 'Elevator area - right'), 81: (8, 'Elevator - up, right'), 82: (8, 'Elevator - down, right'), 83: (8, 'Elevator - up/down, right'), 84: (8, 'Elevator area - bottom'), 85: (8, 'Elevator - up, bottom'), 86: (8, 'Elevator - down, bottom'), 87: (8, 'Elevator - up/down, bottom'), 88: (8, 'Elevator area - top'), 89: (8, 'Elevator - up, top'), 90: (8, 'Elevator - down, top'), 91: (8, 'Elevator - up/down, top'), 92: (16, 'Tree 1'), 93: (16, 'Floor lamp'), 94: (16, 'Table'), 95: (16, 'Bar chair'), 96: (16, 'Metal pillar'), 97: (16, 'Concrete pillar'), 98: (16, 'Flashing lamp'), 99: (16, 'Lamp'), 100: (16, 'Halfsphere lamp'), 101: (10, 'Hydaca'), 102: (10, 'Revenant'), 103: (10, 'Boss 1'), 104: (10, 'Pink dog'), 105: (10, 'Former Human SF'), 106: (10, 'Boss 3'), 107: (10, 'Boss 2'), 108: (16, 'Former Human corpse'), 109: (16, 'Imp corpse'), 110: (16, 'Hydaca corpse'), 111: (16, 'Revenant corpse'), 112: (16, 'Boss 1 corpse'), 113: (16, 'Pink dog corpse'), 114: (16, 'Former Human SF corpse'), 115: (16, 'Boss 3 corpse'), 116: (16, 'Boss 2 corpse'), 117: (16, 'Lamp 2'), 118: (16, 'Metal pillar 2'), 119: (9, 'Player start'), 120: (16, 'Floor fan'), 121: (15, 'Shootable wall 1'), 122: (11, 'Unknown'), 123: (15, 'Shootable wall 2'), 124: (11, 'Unknown'), 125: (11, 'Unknown'), 126: (11, 'Unknown'), 127: (15, 'Shootable wall 3'), 128: (1, 'Wall'), 129: (8, 'Episode end'), 130: (12, 'Flamethrower'), 131: (16, 'Fake horizontal door'), 132: (16, 'Fake Vertical door')}
CELL_DEFS_BZT = {8: (10, 'Invisible alien'), 9: (10, 'Red alien 1'), 32: (12, 'Buligun'), 35: (12, 'Gunrock'), 36: (12, 'Rifle'), 39: (10, 'Red alien 2'), 40: (12, 'Glitchy weapon'), 41: (10, 'Red monster'), 42: (10, 'Pink monster'), 101: (10, 'Green alien'), 102: (10, 'Gray alien'), 121: (15, 'Shootable wall'), 130: (12, 'Fire dragon'), 133: (13, 'Medipack')}
# типы ячеек со СТЕНОВОЙ графикой (показываем в режиме «Ячейки»): стена/углы/двери/лестницы/разруш.
_WALL_CELL_TYPES = {1, 2, 3, 4, 5, 6, 7, 14, 15}


def _cell_def(version: str, cid: int):
    """(тип, имя) ячейки: BZT-переопределение поверх ZT-базы."""
    if not version.startswith("Zero Tolerance") and cid in CELL_DEFS_BZT:
        return CELL_DEFS_BZT[cid]
    return CELL_DEFS_ZT.get(cid, (1, f'Cell {cid}'))


# Смещение таблицы анимаций стен ОТ сигнатуры ZMAP (определено по дизасму).
# Структура ZMAP: [4 sig][4096 texdef][2048 texorder][256 celltypes][16 env]…
#   BZT (июнь/июль): далее ТАБЛИЦА АНИМАЦИЙ, затем карта 16384 → анимтаблица @ sig+0x1914.
#   ZT (релиз): далее КАРТА 16384, затем таблица анимаций → анимтаблица @ sig+0x5914.
# Аниматор (BZT sub_2274 @0xAC.. / ZT-аналог): по таймеру пишет пары (texdef_off, новый_тайл)
# в RAM-копию texdef ($FF6686). Запись таблицы: [stride w][counter b][period b][frame_off w]
#   [кадры: пары (texdef_off w, tile w) до отрицательного off][смещение след. кадра w]…
# Конец таблицы: stride==0. texdef_off → метатекстура=off//16, тайл_в_мете=(off%16)//2.
ANIM_TABLE_OFFSET = {"Zero Tolerance": 0x5914}   # остальные версии → 0x1914
_ANIM_DEFAULT_OFFSET = 0x1914


def _anim_table_offset(version: str) -> int:
    for key, off in ANIM_TABLE_OFFSET.items():
        if version.startswith(key):
            return off
    return _ANIM_DEFAULT_OFFSET


def _u16(d, o):
    return (d[o] << 8) | d[o + 1]


# --- Карты уровней (вкладка «Карты») ---------------------------------------
# Стиль отображения — как в редакторе BZTEdit: сетка ячеек, каждая раскрашена
# иконкой по ТИПУ ячейки (_cell_def), плюс hex-код ячейки. Иконки 16×16 (mapres/).
# Тип ячейки → индекс иконки (порядок BMP в MAP_ICONS):
MAP_TYPE_TO_ICON = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7,
                    8: 13, 9: 8, 10: 9, 11: 1, 12: 10, 13: 11, 14: 12, 15: 14, 16: 15}
# Объекты окружения (спрайты-биллборды: лампы/растения/столы/столбы/вентиляторы/деревья/
# фейк-двери) — тип 16 в CELL_DEFS_ZT. Раньше падали в иконку 1 (стена); теперь иконка 15.
DECOR_CELLTYPES = frozenset(k for k, (t, _n) in CELL_DEFS_ZT.items() if t == 16)
# BZT (June/July): cell→иконка ПРЯМО из редактора BZTEdit (BZTEdit.ini [CellIcons]) —
# авторитетно для прототипов (юзер: «для построения карт в bzt используй bztedit»).
# ZT (релиз) рендерится иначе — ремап через таблицу celltypes (как ztedit-mega).
BZTEDIT_CELL_ICONS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 9, 9: 9, 10: 13, 11: 13, 12: 12, 13: 12, 14: 12, 15: 12, 16: 12, 17: 12, 18: 13, 19: 13, 20: 13, 21: 13, 22: 13, 23: 13, 24: 13, 25: 11, 26: 10, 27: 11, 28: 11, 29: 11, 30: 11, 31: 10, 32: 10, 33: 11, 34: 10, 35: 10, 36: 10, 37: 11, 38: 13, 39: 9, 40: 13, 41: 9, 42: 9, 43: 9, 44: 1, 45: 1, 46: 1, 47: 13, 48: 12, 49: 13, 50: 13, 51: 13, 52: 13, 53: 1, 54: 10, 55: 1, 56: 12, 57: 12, 58: 12, 59: 12, 60: 13, 61: 13, 62: 13, 63: 13, 64: 12, 65: 12, 66: 12, 67: 12, 68: 13, 69: 13, 70: 13, 71: 13, 72: 12, 73: 12, 74: 12, 75: 12, 76: 13, 77: 13, 78: 13, 79: 13, 80: 13, 81: 13, 82: 13, 83: 13, 84: 13, 85: 13, 86: 13, 87: 13, 88: 13, 89: 13, 90: 13, 91: 13, 92: 1, 93: 1, 94: 1, 95: 1, 96: 1, 97: 1, 98: 1, 99: 1, 100: 1, 101: 9, 102: 9, 103: 9, 104: 9, 105: 9, 106: 9, 107: 9, 108: 1, 109: 1, 110: 1, 111: 1, 112: 1, 113: 1, 114: 1, 115: 1, 116: 1, 117: 1, 118: 1, 119: 8, 120: 10, 121: 14, 122: 1, 123: 14, 124: 1, 125: 1, 126: 1, 127: 14, 128: 1, 129: 13, 130: 10, 131: 1, 132: 1, 133: 11, 134: 11}
MAP_ICONS = ["empty", "wall", "up_rgt", "dwn_rgt", "dwn_left", "up_left", "hor_door",
             "ver_door", "pl_start", "enemy", "weap", "item", "spc_wall", "spc_cell",
             "sht_wall", "decor"]
MAP_ICON_LABELS = [tr("Пусто / спрайт", "Empty / sprite"), tr("Стена", "Wall"), tr("Угол ↗", "Corner ↗"), tr("Угол ↘", "Corner ↘"), tr("Угол ↙", "Corner ↙"), tr("Угол ↖", "Corner ↖"),
                   tr("Гориз. дверь", "Horiz. door"), tr("Верт. дверь", "Vert. door"), tr("Старт игрока", "Player start"), tr("Враг", "Enemy"), tr("Оружие", "Weapon"),
                   tr("Предмет", "Item"), tr("Спец. стена", "Special wall"), tr("Спец. ячейка", "Special cell"), tr("Разрушаемая стена", "Destructible wall"),
                   tr("Объект окружения (лампа/растение/мебель)", "Environment object (lamp/plant/furniture)")]
_MAP_ICON_CACHE = None


def _load_map_icons():
    global _MAP_ICON_CACHE
    if _MAP_ICON_CACHE is None:
        d = os.path.join(os.path.dirname(__file__), "mapres")
        _MAP_ICON_CACHE = [QImage(os.path.join(d, f"{n}.bmp")) for n in MAP_ICONS]
    return _MAP_ICON_CACHE


# --- CELLTYPE (поведение ячейки) — из дизасма June (sub_B94A4/sub_DDB6/sub_954E) ---
# Двойная косвенность игры: cell ID → celltypes[cell] (256б @ sig+0x1804, СВОЯ на
# эпизод) → CELLTYPE (код поведения) → объект. Редакторы (BZTEdit/ztedit) зашили
# статичный cell→смысл — он НЕВЕРЕН (в Ep1 cell0x1A=угол, cell0x24=враг, cell0x20=
# старт игрока). Поэтому для BZT берём ИГРОВОЙ celltype, а не догадку редактора.
# Таблица предметов (индекс = celltype-0x18), 0-14:
MAP_ITEM_NAMES = [tr("Карта", "Map"), tr("Био-сканер", "Bio-scanner"), tr("Мина", "Mine"), tr("Бронежилет", "Body armor"), tr("Огнетушитель", "Fire extinguisher"),
                  tr("Огнезащ. костюм", "Fire suit"), tr("Фонарик", "Flashlight"), tr("Граната", "Grenade"), tr("Пистолет", "Handgun"), tr("Ночное зрение", "Night vision"),
                  tr("Лазер. прицел", "Laser sight"), tr("Ракетница", "Rocket launcher"), tr("Дробовик", "Shotgun"), tr("Огнемёт", "Flamethrower"), tr("Пульс-лазер", "Pulse laser")]
MAP_WEAPON_ITEM_IDX = {2, 7, 8, 10, 11, 12, 13, 14}   # из 15-таблицы — это ОРУЖИЕ
MAP_ENEMY_KIND = {0x29: "Red Robot", 0x2A: "Purple Robot", 0x2B: "Red Robot",
                  0x65: "Red Man", 0x66: "Man", 0x67: "Red Robot", 0x68: "Red Robot",
                  0x69: "Red Robot", 0x6A: "Red Robot", 0x6B: "Red Robot"}

# Спрайт врага: celltype → тип врага → адрес графики (objdef +0x14), по дизасму.
# Цепочка игры: celltype → spawn-kind (unk_96BE) → objdef → +0x14 = указатель графики.
# Адреса 5 типов × 3 билда (из дизасма Ghidra/IDA). Графика = 3-уровневое дерево
# анимаций (spritemod.parse_sprite); кадр — assemble_frame.
CELLTYPE_TO_ENEMY = {0x29: "RedRobo", 0x2A: "Purple", 0x2B: "RedRobo", 0x65: "RedMan",
                     0x66: "Man", 0x67: "RedRobo", 0x68: "RedRobo", 0x69: "RedRobo",
                     0x6A: "RedRobo", 0x6B: "RedRobo"}
ENEMY_NAMES = {"RedRobo": "Red Robot", "Purple": "Purple Robot", "Man": "Soldier",
               "RedMan": "Red Man", "GrenMan": "Grenade Man"}
ENEMY_GFX = {
    "June": {"RedRobo": 0x16F9BA, "Purple": 0x1A86E0, "Man": 0x1C73A2,
             "RedMan": 0x1E473C, "GrenMan": 0x201AD6},
    "July": {"RedRobo": 0x164820, "Purple": 0x1848E2, "Man": 0x1A35A4,
             "RedMan": 0x1C4576, "GrenMan": 0x1E1910},
    "ZT":   {"RedRobo": 0x1B7B38, "Purple": 0x16EC58, "Man": 0x1A7828,
             "RedMan": 0x1ACC72, "GrenMan": 0x18D078},
}


def _build_tag(version: str) -> str:
    if "1995-06-23" in version:
        return "June"
    if "1995-07-14" in version:
        return "July"
    return "ZT"


# Предметы/оружие/декор: celltype → байт-offset в items_imgs (из дизасма, диспетч
# dword_11014). Графика = тайл(ы) 32×32 column-major @ items_imgs+offset (offset/0x200
# = индекс тайла). Карта одинакова у всех билдов (тот же движок); меняется только base.
CELLTYPE_TO_ITEM_OFFSET = {
    0x18: 0x0, 0x19: 0x600, 0x1A: 0x1000, 0x1B: 0x1400, 0x1C: 0x1600, 0x1D: 0x1600,
    0x1E: 0x800, 0x1F: 0x1A00, 0x20: 0x1C00, 0x21: 0xE00, 0x22: 0xA00, 0x23: 0xC00,
    0x24: 0x1800, 0x25: 0x2400, 0x26: 0x2600, 0x28: 0xC00, 0x36: 0x2200,
    # декор/объекты (выверено по ZT-фидбеку, адрес = 0x10E9BE + offset):
    0x37: 0x5C00, 0x5C: 0x5E00, 0x5D: 0x6000, 0x5E: 0x6E00, 0x5F: 0x6E00, 0x60: 0x6600,
    0x61: 0x6C00, 0x62: 0x6200, 0x63: 0x6A00, 0x64: 0x7000, 0x75: 0x7000, 0x76: 0x6800,
}

# ZT-релиз: целл-тип врага → (имя, графика a1, вариант2|None). Роутинг отдельный от BZT:
# у ZT свой бестиарий (Former Human/боссы/собака/инопланетянин), а не роботы BZT.
# Адреса выверены игроком + дизасмом (слот $42 ZT).
ZT_CELLTYPE_ENEMY = {
    0x29: (tr("Бывший человек (сержант)", "Former Human (sergeant)"), 0x1B7B38, None),
    0x2A: (tr("Бывший человек (Former Human)", "Former Human"), 0x16EC58, 0x1C258A),
    0x2B: ("Imp", 0x1A7828, None),
    0x65: ("Hydaca", 0x1ACC72, None),
    0x66: (tr("Revenant (робот)", "Revenant (robot)"), 0x18D078, None),
    0x67: (tr("Босс 1-го эпизода", "Episode 1 boss"), 0x183B94, None),
    0x68: (tr("Розовый пёс", "Pink dog"), 0x1784F2, None),
    0x69: (tr("Инопланетянин (Former Human SF)", "Alien (Former Human SF)"), 0x17BA80, None),
    0x6A: (tr("Босс 3-го эпизода", "Episode 3 boss"), 0x193F00, None),
    0x6B: (tr("Босс 2-го эпизода", "Episode 2 boss"), 0x19D50C, None),
}

# НЕМЕЦКАЯ ZT: оба ЧЕЛОВЕКА-врага ПОЛНОСТЬЮ заменены на актёра «Alien» (0x17BA80).
# Подтверждено сравнением ROM: в главной objdef-таблице @0xAB20 (нем. @0xAAD6, шаг 0x26)
# слот 0 (Former Human Sergeant 0x1B7B38) и слот 1 (Former Human 0x16EC58) перенаправлены
# на актёра Alien ЦЕЛИКОМ — спрайт + AI-указатели (регион 0x9Cxx) + стат-поле (+0x10:
# 0x2C→0x20). Imp и все монстры/боссы не тронуты; спрайт-вариация Former Human (0x1C258A)
# в нем. ROM вообще не упоминается. Карты/celltypes идентичны US — меняется только актёр.
ZT_DE_ENEMY_OVERRIDE = {
    0x29: (tr("Инопланетянин (заменил сержанта)", "Alien (replaced the sergeant)"), 0x17BA80, None),
    0x2A: (tr("Инопланетянин (заменил Former Human)", "Alien (replaced Former Human)"), 0x17BA80, None),
}
ZT_DE_CELL_NAME = {
    0x29: tr("Alien (заменил сержанта)", "Alien (replaced the sergeant)"),
    0x2A: tr("Alien (заменил Former Human)", "Alien (replaced Former Human)"),
}


def _zt_cell_name(ver, ct, default='?'):
    """Имя клетки ZT по celltype; в немецкой версии люди-враги показаны как Alien."""
    if ver.startswith("Zero Tolerance (нем") and ct in ZT_DE_CELL_NAME:
        return ZT_DE_CELL_NAME[ct]
    return CELL_DEFS_ZT.get(ct, (1, default))[1]


# ZTU (Underground): свой бестиарий — objdef-таблица спрайтов врагов @0xADAA
# (шаг 0x26, спрайт-ptr на офсете 0; как US @0xAB20). Слот→celltype = порядок US.
# Все 9 спрайтов проверены _valid_header (7-16 анимаций). Палитра спрайтов/стен = 0x21B4
# (US-аналог 0x20F2; level-load грузит 0x2134 затем 0x21B4 = US 0x2072→0x20F2). Имена по
# US-ролям (в моде графика своя, роль может отличаться). Вариации-2 нет.
# Палитра 0x21B4 ПОДТВЕРЖДЕНА юзером. Спрайты: objdef-таблица @0xAD84 (objdef0,
# стрид 0x26, спрайт-ptr на офсете +0x14; спавн `move.l (0x14,A1),(0x42,A0)` @0xA3AC).
# celltype→слот = порядок US (0x29,0x2A,0x2B,0x65-0x6B). Слот 0x29 = 0x17BD0C (солдат-
# сержант, спецпроверка `cmpi.l #0x17bd0c` в спавне), подтверждён визуально и юзером
# (C7=0x29=сержант). Графика мода своя — имена по US-ролям как ориентир.
ZTU_SPRITE_PALETTE = 0x21B4
ZTU_CELLTYPE_ENEMY = {
    0x29: (tr("Сержант", "Sergeant"), 0x1C4BEC, None),            # зелёный солдат (юзер: 0x17BD0C = FH, не серж.)
    0x2A: ("Former Human", 0x17BD0C, None),       # красный солдат
    0x2B: (tr("Враг (слот Imp)", "Enemy (Imp slot)"), 0x1B48DC, None),
    0x65: (tr("Враг (слот Hydaca)", "Enemy (Hydaca slot)"), 0x1B9D26, None),
    0x66: (tr("Враг (слот Revenant)", "Enemy (Revenant slot)"), 0x19A12C, None),
    0x67: (tr("Враг (слот Boss 1)", "Enemy (Boss 1 slot)"), 0x190C48, None),
    0x68: (tr("Враг (слот Pink Dog)", "Enemy (Pink Dog slot)"), 0x1855A6, None),
    0x69: (tr("Враг (слот Alien)", "Enemy (Alien slot)"), 0x188B34, None),
    0x6A: (tr("Враг (слот Boss 3)", "Enemy (Boss 3 slot)"), 0x1A0FB4, None),
    0x6B: (tr("Враг (слот Boss 2)", "Enemy (Boss 2 slot)"), 0x1AA5C0, None),
}

# ZT: celltype → объект в формате СПРАЙТА врага (билборд, не объект-банк-тайл).
# Напр. сгоревшие остатки = кадр горения спрайта 0x1cb96a.
ZT_CELLTYPE_OBJSPRITE = {
    0x35: (tr("Сгоревшие остатки (кадр горения)", "Burnt remains (burning frame)"), 0x1CB96A),
}

# Разрушаемые/секретные/стреляемые стены: при выстреле текстура свапается на damage-вариант.
DESTRUCTIBLE_CELLTYPES = {0x06, 0x07, 0x83, 0x84, 0x7B, 0x7D, 0x7F}
# Каноническая метатекстура двери (celltype 0x06/0x07). ВСЕ финальные уровни (ZT эп.1-3,
# June эп.4-5) ставят texorder двери = [18,18,18,18]; ранние прото-уровни (June эп.1-3, July)
# оставили её [0,0,0,0] (незаполнено) — там подставляем 18. Сверено: ZT #18 = sci-fi
# раздвижная дверь, July ep2 #18 = клёпаная металлич., June #18 = двустворчатая панель.
DOOR_METATEX = 18
# items_imgs base по билду/эпизоду (June/July — поле заголовка эпизода; ZT — хардкод).
ITEMS_IMGS_BASE = {
    "June": {1: 0xC043E, 2: 0xEF88A, 3: 0x125182},
    "July": {1: 0x9D7F8, 2: 0xCBB3C, 3: 0xF8664, 4: 0x1254F8},
    "ZT":   {1: 0x10E9BE, 2: 0x10E9BE, 3: 0x10E9BE},
}
# ZTU (Underground): база объектов/предметов = 0xE0366 (юзер дал блок 0x0E0326, точный
# tile-0 = +0x40, подтверждено симметрией креста-аптечки; = 0xDEB66+0xC*0x200, где 0xDEB66 —
# база билборд-рендера объектов @0xf3d8). celltype→offset тот же (CELLTYPE_TO_ITEM_OFFSET,
# общий движок); предмет = база + offset. Проверено: все пикапы/декор центрированы.
ZTU_ITEMS_BASE = 0xE0366


# Выверено в РЕАЛЬНОЙ ИГРЕ (юзер, June; July — общая семантика прототипа).
# В прототипах BZT смысл celltype ОТЛИЧАЕТСЯ от ZT — этот словарь переопределяет
# ZT-догадки _celltype_icon/_celltype_name. celltype → (иконка, имя).
BZT_CELLTYPE = {
    0x06: (6,  tr("Дверь горизонтальная", "Horizontal door")),
    0x07: (7,  tr("Дверь вертикальная", "Vertical door")),
    0x08: (9,  tr("Враг-макет (спрайт по эпизоду: невид./синий)", "Enemy layout (sprite by episode: invis./blue)")),
    0x09: (9,  tr("Враг-макет (спрайт по эпизоду: красный/собака)", "Enemy layout (sprite by episode: red/dog)")),
    0x0A: (13, tr("Триггер градиента/освещения", "Gradient/lighting trigger")),
    0x25: (11, tr("Аптечка", "Medkit")),
    0x27: (9,  tr("Враг: белый инопланетянин (кидает пламя)", "Enemy: white alien (throws flame)")),
    0x28: (10, tr("Оружие: Ракетница", "Weapon: Rocket launcher")),
    0x2B: (9,  tr("Босс эпизода 1", "Episode 1 boss")),
    0x30: (12, tr("Стена лифта", "Elevator wall")),
    0x32: (12, tr("Лифт", "Elevator")),
    0x36: (10, tr("Оружие: Лазерная винтовка", "Weapon: Laser rifle")),
    0x50: (12, tr("Площадка перед лифтом", "Elevator platform")),
    0x51: (12, tr("Лифт", "Elevator")),
    0x54: (12, tr("Площадка перед лифтом", "Elevator platform")),
    0x55: (12, tr("Лифт", "Elevator")),
    0x59: (12, tr("Лифт", "Elevator")),
    0x65: (9,  tr("Враг: зелёный макет", "Enemy: green layout")),
    0x66: (9,  tr("Враг: бурый макет", "Enemy: brown layout")),
    0x67: (9,  tr("Враг: невидимый (граната; спрайт red robot, пал. зоны 2)", "Enemy: invisible (grenade; red robot sprite, zone 2 palette)")),
    0x77: (8,  tr("Старт игрока", "Player start")),
    0x79: (14, tr("Разрушаемая стена (исчезает)", "Destructible wall (vanishes)")),
    0x81: (13, tr("Телепорт на след. эпизод", "Teleport to next episode")),
    0x82: (10, tr("Оружие: Огнемёт", "Weapon: Flamethrower")),
    0x8C: (13, tr("Триггер: зелёная лампа HUD (?)", "Trigger: green HUD lamp (?)")),
}


def _celltype_icon(t: int) -> int:
    """CELLTYPE (код поведения игры) → индекс иконки (0-15). Game-true для BZT."""
    if t in BZT_CELLTYPE:
        return BZT_CELLTYPE[t][0]
    if t == 0:
        return 0
    if t in (1, 0x77):
        return 1
    if 2 <= t <= 5:
        return t
    if t in (6, 7, 0x83, 0x84, 0x7B, 0x7D, 0x7F):
        return 14                                   # разруш./секрет. стена
    if t == 0x32:
        return 11                                   # медипак
    if 0x19 <= t <= 0x26 or t == 0x36:              # предмет/оружие
        return 10 if (t - 0x18) in MAP_WEAPON_ITEM_IDX else 11
    if 0x29 <= t <= 0x2C or 0x65 <= t <= 0x6B:
        return 9                                    # враг
    if t == 0x79:
        return 8                                    # старт игрока
    if t == 0x30 or 0x31 <= t <= 0x34 or 0x50 <= t <= 0x5B:
        return 12                                   # лестница/лифт/дверь
    if 8 <= t <= 0x0B or t in (0x18, 0x27, 0x28, 0x2F, 0x81, 0x82, 0x85):
        return 13                                   # окруж./триггер/выход
    if t in DECOR_CELLTYPES:
        return 15                                   # объект окружения (лампа/растение/мебель)
    return 1                                        # неизв. → стена


def _celltype_name(t: int) -> str:
    """CELLTYPE → человекочитаемое имя объекта (для инфо о ячейке)."""
    if t in BZT_CELLTYPE:
        return BZT_CELLTYPE[t][1]
    if t == 0:
        return tr("Пусто", "Empty")
    if t in (1, 0x77):
        return tr("Стена", "Wall")
    if 2 <= t <= 5:
        return tr("Угол", "Corner")
    if t in (6, 7):
        return tr("Разрушаемая стена (отстрел)", "Destructible wall (shoot out)")
    if t in (0x83, 0x84):
        return tr("Разрушаемая стена (бомба)", "Destructible wall (bomb)")
    if t in (0x7B, 0x7D, 0x7F):
        return tr("Секретная стена", "Secret wall")
    if 8 <= t <= 0x0B:
        return tr("Окружение / свет", "Environment / light")
    if t == 0x32:
        return tr("Медипак", "Medipack")
    if 0x19 <= t <= 0x26:
        idx = t - 0x18
        nm = MAP_ITEM_NAMES[idx] if idx < len(MAP_ITEM_NAMES) else f"#{idx}"
        kind = tr("Оружие", "Weapon") if idx in MAP_WEAPON_ITEM_IDX else tr("Предмет", "Item")
        return f"{kind}: {nm}"
    if t == 0x36:
        return tr("Оружие", "Weapon")
    if t in (0x27, 0x28):
        return tr("Пламя / анимация", "Flame / animation")
    if 0x29 <= t <= 0x2C or 0x65 <= t <= 0x6B:
        return tr(f"Враг: {MAP_ENEMY_KIND.get(t, 'неизв.')}", f"Enemy: {MAP_ENEMY_KIND.get(t, 'unknown')}")
    if t == 0x2F:
        return tr("Блокер врагов", "Enemy blocker")
    if t == 0x30 or 0x31 <= t <= 0x34 or 0x50 <= t <= 0x5B:
        return tr("Лестница / лифт / дверь", "Stairs / elevator / door")
    if t == 0x79:
        return tr("Старт игрока", "Player start")
    if t == 0x81:
        return tr("Цель выполнена", "Objective complete")
    if t in (0x82, 0x85):
        return tr("Выход эпизода", "Episode exit")
    return tr(f"celltype 0x{t:02X} (неизв.)", f"celltype 0x{t:02X} (unknown)")


def _map_celltypes(rom, version: str, episode: int):
    """256-байтовая таблица celltypes для билда+эпизода (ремап cell→поведение)."""
    off = _episode_celltypes_off(version, episode)
    if off is None:
        return None
    return rom.data[off:off + 256]


def _spawner_wall_cids(rom, version: str, episode: int):
    """Множество cell-ID BZT ниша-стен (спавнер-стены): 0xE4-0xFF с метатекстурой-нишей
    249-255 (= враг в нише; выстрел спавнит соседнего невидимого врага). ZT/ZTU → пусто."""
    if version.startswith("Zero Tolerance"):
        return set()
    banks = _wall_bank(version)
    if not banks or episode - 1 >= len(banks):
        return set()
    sig = banks[episode - 1][1]
    d = rom.data
    if d[sig:sig + 4] != b"ZMAP":
        return set()
    texorder = sig + 4 + 4096
    out = set()
    for c in range(0xE4, 0x100):
        o = texorder + c * 8
        if any(249 <= ((d[o + k * 2] << 8) | d[o + k * 2 + 1]) <= 255 for k in range(4)):
            out.add(c)
    return out


def _cell_icon(version: str, cid: int, celltypes=None) -> int:
    """Индекс иконки (0-14) для ячейки cid.
    ZT — текущая цепочка (CELL_DEFS_ZT, совпадает с ztedit); BZT — ИГРОВОЙ celltype."""
    if celltypes is None:
        return BZTEDIT_CELL_ICONS.get(cid, 1)        # fallback без таблицы
    if version.startswith("Zero Tolerance"):
        return MAP_TYPE_TO_ICON.get(CELL_DEFS_ZT.get(celltypes[cid], (1, ''))[0], 1)
    return _celltype_icon(celltypes[cid])            # BZT: game-true


# June (1995-06-23): явная таблица уровней (offset, X, Y) — из ztedit-mega ztedit.ini.
# Уровни упакованы подряд (next = prev + X*Y). BE1=8, BE2=6, BE3=10 уровней.
# level0 = anim_end (СРАЗУ после анимтаблицы, БЕЗ пропуска нулей — пустые верхние
# строки уровня НЕ паддинг!), размеры из заголовка @sig-0x86. Точнее редактора:
# L1=42×43 (ред. 42×42 срезал верх. строку), L7=28×35 (ред. 28×36).
BZT_JUNE_LEVELS = [
    (1, 1, 0xAC7E4, 42, 43), (1, 2, 0xACEF2, 36, 47), (1, 3, 0xAD58E, 46, 37),
    (1, 4, 0xADC34, 34, 30), (1, 5, 0xAE030, 30, 28), (1, 6, 0xAE378, 30, 32),
    (1, 7, 0xAE738, 28, 35), (1, 8, 0xAEB0C, 56, 32),
    (2, 1, 0xB0E08, 56, 53), (2, 2, 0xB19A0, 38, 44), (2, 3, 0xB2028, 40, 40),
    (2, 4, 0xB2668, 38, 42), (2, 5, 0xB2CA4, 80, 58), (2, 6, 0xB3EC4, 28, 28),
    (3, 1, 0xB5E42, 58, 62), (3, 2, 0xB6C4E, 38, 32), (3, 3, 0xB710E, 34, 27),
    (3, 4, 0xB74A4, 30, 34), (3, 5, 0xB78A0, 34, 32), (3, 6, 0xB7CE0, 36, 37),
    (3, 7, 0xB8214, 42, 36), (3, 8, 0xB87FC, 38, 28), (3, 9, 0xB8C24, 22, 30),
    (3, 10, 0xB8EB8, 28, 32),
]
# ZT релиз: 3 эпизода, ZMAP-сигнатуры (из ztedit-mega). Карта = sig+0x1914,
# уровни 32×32 (фиксированный размер — подтверждено), 16 слотов на эпизод.
ZT_EPISODE_SIGS = [0x15A106, 0x160420, 0x166028]
# ZT Underground (мод v1.5): ОДИН эпизод (подтверждено), один общий ZMAP @0x1736AE
# (texdef/texorder/celltypes/карта — US-структура). Карта = sig+0x1904, 16 слотов 32×32
# (имена 0-15 = SUBWAY 1-13 + REACTOR×3; ~10 реальных, дальше паддинг). Имена @0x3174.
ZTU_ZMAP_SIG = 0x1736AE


def _zt_sigs(version: str):
    """ZMAP-сигнатуры эпизодов: у Underground одна, у релиза/немецкой — три."""
    if "Underground" in version:
        return [ZTU_ZMAP_SIG]
    return ZT_EPISODE_SIGS


# Смещения таблиц celltypes (256 байт) по эпизодам, для game-true рендера карт.
# June: = BE{N}Offset из ztedit-ini (это и есть celltypes). July: sig+0x1804.
JUNE_EP_CELLTYPES = {1: 0xAC630, 2: 0xB0CF6, 3: 0xB5CBE}
JULY_EP_SIGS = [0x83D38, 0x8848E, 0x8D528, 0x91B40]


def _episode_celltypes_off(version: str, episode: int):
    """ROM-смещение таблицы celltypes (256 байт) для билда+эпизода, либо None."""
    if version.startswith("Zero Tolerance"):
        sigs = _zt_sigs(version)
        if 1 <= episode <= len(sigs):
            return _de(version, sigs[episode - 1]) + 0x1804
    elif "1995-06-23" in version:
        return JUNE_EP_CELLTYPES.get(episode)
    elif "1995-07-14" in version:
        if 1 <= episode <= len(JULY_EP_SIGS):
            return JULY_EP_SIGS[episode - 1] + 0x1804
    return None


# Имена уровней ZT (16 байт/имя, ASCII с пробелами): DOCKING BAY 1/2, BRIDGE 1,
# ENGINEERING 1-4, … Глобальный индекс = (эпизод-1)*16 + (уровень-1).
ZT_LEVEL_NAMES_OFFSET = 0x30B2


def _zt_level_name(rom, ep: int, ln: int):
    if rom is None or not rom.version.startswith("Zero Tolerance"):
        return None
    base = 0x3174 if "Underground" in rom.version else ZT_LEVEL_NAMES_OFFSET
    idx = (ep - 1) * 16 + (ln - 1)
    o = base + idx * 16
    raw = bytes(b if 32 <= b < 127 else 32 for b in rom.data[o:o + 16])
    name = raw.decode("ascii", "replace").strip()
    return name or None
# July (1995-07-14): таблица уровней ВСКРЫТА из дизасма! Размеры лежат в заголовке
# перед каждым ZMAP (Ep_Maps @ sig-0x88): [счётчик:word][размеры w,h по 4 байта].
# level0 = после анимтаблицы (sig+0x1914 → конец → пропуск нулей). Уровни упакованы.
# E1=8 (=June E1), E2=6 (=June E2), E3=9 (July-УНИКАЛЬНЫЙ!), E4=10 (=June E3). 33 ур.
BZT_JULY_LEVELS = [
    (1, 1, 0x856F0, 42, 43), (1, 2, 0x85DFE, 36, 47), (1, 3, 0x8649A, 46, 37),
    (1, 4, 0x86B40, 34, 30), (1, 5, 0x86F3C, 30, 28), (1, 6, 0x87284, 30, 32),
    (1, 7, 0x87644, 28, 35), (1, 8, 0x87A18, 56, 32),
    (2, 1, 0x89DE6, 56, 53), (2, 2, 0x8A97E, 38, 44), (2, 3, 0x8B006, 40, 40),
    (2, 4, 0x8B646, 38, 42), (2, 5, 0x8BC82, 80, 58), (2, 6, 0x8CEA2, 28, 28),
    (3, 1, 0x8F054, 32, 42), (3, 2, 0x8F594, 36, 32), (3, 3, 0x8FA14, 46, 29),
    (3, 4, 0x8FF4A, 22, 20), (3, 5, 0x90102, 40, 28), (3, 6, 0x90562, 26, 31),
    (3, 7, 0x90888, 52, 42), (3, 8, 0x91110, 26, 27), (3, 9, 0x913CE, 34, 30),
    (4, 1, 0x93AB4, 58, 62), (4, 2, 0x948C0, 38, 32), (4, 3, 0x94D80, 34, 27),
    (4, 4, 0x95116, 30, 34), (4, 5, 0x95512, 34, 32), (4, 6, 0x95952, 36, 37),
    (4, 7, 0x95E86, 42, 36), (4, 8, 0x9646E, 38, 28), (4, 9, 0x96896, 22, 30),
    (4, 10, 0x96B2A, 28, 32),
]


def _map_levels(version: str):
    """Список уровней (эпизод, номер, offset, X, Y) для версии ROM."""
    if version.startswith("Zero Tolerance"):
        # Карта ZT начинается СРАЗУ после celltypes: sig+0x1904 (env-таблицы между
        # celltypes и картой НЕТ). Сверено с editor-скринами: DOCKING BAY 1 (верх не
        # обрезан), BRIDGE 1, ENGINEERING 1. Уровни 32×32; реальный уровень меньше,
        # остаток — пустые ячейки (padding). (sig+0x1914 давал сборку «пополам».)
        out = []
        for ei, sig in enumerate(_zt_sigs(version), 1):
            base = _de(version, sig) + 0x1904
            for lv in range(16):
                out.append((ei, lv + 1, base + lv * 1024, 32, 32))
        return out
    if "1995-06-23" in version:
        return BZT_JUNE_LEVELS
    if "1995-07-14" in version:
        return BZT_JULY_LEVELS
    return []


def parse_wall_anims(data: bytes, zmap: int, version: str):
    """Разобрать таблицу анимаций стен эпизода (по дизасму, не эвристика).

    zmap — адрес сигнатуры «ZMAP». Возвращает список анимаций; каждая:
        {"meta": idx метатекстуры, "period": кадров между сменами, "frames": [subs, …]}
    где subs — список (texdef_off, new_tile) для этого кадра (подстановки тайлов в метатекстуру).
    Первый кадр обычно пустой (базовое состояние). Кадры применяются ПОВЕРХ базовой метатекстуры.
    """
    if data[zmap:zmap + 4] != b"ZMAP":
        return []
    p = zmap + _anim_table_offset(version)
    n = len(data)
    anims = []
    while p + 6 <= n:
        stride = _u16(data, p)
        if stride < 6 or stride > 0x800:        # stride==0 — терминатор таблицы
            break
        if p + stride > n:
            break
        period = data[p + 3]
        foff = _u16(data, p + 4)
        frames = []
        cur = foff
        seen = set()
        ok = True
        while 0 < cur < stride and cur not in seen and len(frames) < 32:
            seen.add(cur)
            a2 = p + cur
            subs = []
            while a2 + 2 <= p + stride:
                off = _u16(data, a2)
                if off & 0x8000:                # отрицательный → конец кадра
                    a2 += 2
                    break
                if off >= 4096 or a2 + 4 > p + stride:
                    ok = False
                    break
                subs.append((off, _u16(data, a2 + 2)))
                a2 += 4
            if not ok:
                break
            frames.append(subs)
            if a2 + 2 > p + stride:
                break
            cur = _u16(data, a2)
        if not ok or len(frames) < 2:
            break
        # метатекстура анимации = по первому ненулевому смещению среди всех кадров
        metas = sorted(set(o // 16 for fr in frames for o, _ in fr))
        meta = metas[0] if metas else 0
        anims.append({"meta": meta, "period": period, "frames": frames, "addr": p})
        p += stride
        if len(anims) > 80:
            break
    return anims


def _build_metatexture(data: bytes, texdef: int, idx: int, bank: int, colors, subs=None):
    """Собрать метатекстуру 128×64 (8 текстур 32×32, порядок 1357/2468 = сетка 4×2).

    subs — необязательные подстановки тайлов кадра анимации: список (texdef_off, new_tile).
    texdef_off относится к этой метатекстуре, если off//16 == idx; тайл_в_мете = (off%16)//2.
    Подстановка заменяет номер тайла для соответствующей из 8 позиций.
    """
    from . import tiles as _t
    # 8 номеров тайлов метатекстуры (с применёнными подстановками кадра)
    base = texdef + idx * 16
    ids = []
    for i in range(8):
        o = base + i * 2
        ids.append((data[o] << 8) | data[o + 1] if o + 1 < len(data) else 0)
    if subs:
        for off, new_tile in subs:
            if off // 16 == idx:
                pos = (off % 16) // 2
                if 0 <= pos < 8:
                    ids[pos] = new_tile
    out = bytearray(128 * 64)
    for i in range(8):
        tid = ids[i]
        col = i // 2; row = i % 2
        toff = bank + tid * 512
        if toff + 512 > len(data):
            continue
        tb, _, _ = _t.decode_zt_sheet(data, toff, 1, 1)
        for y in range(32):
            d = (row * 32 + y) * 128 + col * 32
            out[d:d + 32] = tb[y * 32:y * 32 + 32]
    return out


# Внутриигровой текст — ASCII-блоки (Text/*.bin): названия уровней, биографии отряда, брифинги
# эпизодов, миссии, Game Over, кредиты. Строки фикс-ширины, разделены нулями. Адреса по
# content-match Text/*.bin. Запись: (имя, адрес, размер). Релиз переработал Char_bio/Brif_Ep1/GO.
TEXT_BANKS = {
    "Zero Tolerance": [
        (tr("Названия уровней", "Level names"),   0x576C3, 1175),
        (tr("Короткие назв. уровней", "Short level names"), 0x30B0, 708),
        (tr("Биографии отряда", "Squad bios"),   0xC6B26, 1650),   # релиз: Ishii «Soba», Haile «Psycho», JJWolf…
        (tr("Сообщения о предметах", "Item messages"), 0x1E324, 551),  # MINE/HANDGUN/SHOTGUN/… COLLECTED
        (tr("Входы в уровни", "Level entrances"),     0x1E818, 1554),    # ENTERING … LEVEL
        (tr("Миссия 1", "Mission 1"),           0xCB824, 445),
        (tr("Брифинг эп.2", "Ep.2 briefing"),       0xCBB2F, 408),
        (tr("Миссия 2", "Mission 2"),           0xCBCC7, 297),
        ("Game Over 2",        0xCBDF0, 223),
        (tr("Брифинг эп.3", "Ep.3 briefing"),       0xCBECF, 260),
        (tr("Кредиты / концовка", "Credits / ending"), 0xCBFD3, 4330),
    ],
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        (tr("Названия уровней", "Level names"),   0x5D263, 1175),
        (tr("Биографии отряда", "Squad bios"),   0x6503C, 1590),
        (tr("Сообщения о предметах", "Item messages"), 0x1B08A, 551),
        (tr("Входы в уровни", "Level entrances"),     0x1B57E, 1554),
        (tr("Статистика уровня", "Level stats"),  0x37D4, 887),       # Number of kills / Hit ratio / SNIPER …
        (tr("Брифинг эп.1", "Ep.1 briefing"),       0x698B2, 519),
        (tr("Миссия 1", "Mission 1"),           0x69AB9, 445),
        ("Game Over 1",        0x69C76, 223),
        (tr("Брифинг эп.2", "Ep.2 briefing"),       0x69D55, 408),
        (tr("Миссия 2", "Mission 2"),           0x69EED, 297),
        ("Game Over 2",        0x6A016, 223),
        (tr("Брифинг эп.3", "Ep.3 briefing"),       0x6A0F5, 260),
        (tr("Кредиты / концовка", "Credits / ending"), 0x6A1F9, 4330),
    ],
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Названия уровней", "Level names"),   0x28C45, 1175),
        (tr("Биографии отряда", "Squad bios"),   0x7B5E4, 1590),
        (tr("Сообщения о предметах", "Item messages"), 0x20D13, 510),
        (tr("Входы в уровни", "Level entrances"),     0x21172, 1140),
        (tr("Статистика уровня", "Level stats"),  0x3922, 892),
        (tr("Брифинг эп.1", "Ep.1 briefing"),       0x7FE4E, 519),
        (tr("Миссия 1", "Mission 1"),           0x80055, 445),
        ("Game Over 1",        0x80212, 223),
        (tr("Брифинг эп.2", "Ep.2 briefing"),       0x802F1, 408),
        (tr("Миссия 2", "Mission 2"),           0x80489, 297),
        ("Game Over 2",        0x805B2, 223),
        (tr("Брифинг эп.3", "Ep.3 briefing"),       0x80691, 260),
        (tr("Кредиты / концовка", "Credits / ending"), 0x80795, 4330),
    ],
}


# ZTU (Underground) — текст релокирован; адреса выверены сканом + содержимым (release-адреса дали бы
# мусор, т.к. ZTU startswith «Zero Tolerance»). Остальной текст добавит авто-скан TextViewer.
TEXT_BANKS_ZTU = [
    (tr("Заголовок ROM", "ROM header"),          0x0044F, 0x60),
    (tr("Названия уровней", "Level names"),       0x03172, 707),     # SUBWAY LEVEL 1..13 (+FLOOR/REACTOR задел)
    (tr("Сообщения о предметах", "Item messages"),  0x1ECEE, 0x320),   # MINE/HANDGUN/… COLLECTED
    (tr("Входы в уровни", "Level entrances"),         0x1F1E0, 1554),     # ENTERING SUBWAY LEVEL …
    (tr("Выбор уровня (SECURED)", "Level select (SECURED)"), 0x266D2, 1229),     # *NOT SECURED**SECURED* + список
    (tr("Пароль", "Password"),                 0x27502, 180),      # ENTER PASSWORD A B C …
    (tr("Биографии отряда", "Squad bios"),       0x974B0, 1700),     # Ishii «Soba», Haile «Psycho», Wolf, …, Gjoerup
    (tr("Брифинг миссии", "Mission briefing"),         0x9BD60, 900),      # HOME WORLD DATE / CRISIS BRIEFING / «Get ready…»
]


def _text_bank(version: str):
    if "Underground" in version:
        return TEXT_BANKS_ZTU
    for key, val in TEXT_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


def _scan_text_regions(data: bytes, min_len: int = 20):
    """Найти ВСЕ ASCII-текст-регионы в ROM (для полноты). Фильтр отсеивает графику/звук/шрифты:
    высокая доля букв+пробелов, реальные слова с гласными, нет восходящих рамп (ABCDEF…).
    Возвращает список (start, length)."""
    import re
    from collections import Counter
    VOW = set(b"AEIOUaeiou")
    n = len(data); i = 0; out = []
    while i < n:
        if 0x20 <= data[i] < 0x7F:
            j = i; gap = 0
            while j < n:
                b = data[j]
                if 0x20 <= b < 0x7F:
                    gap = 0; j += 1
                elif b in (0, 0x0A, 0x0D):
                    gap += 1; j += 1
                else:
                    break
                if gap > 24:
                    j -= gap; break
            seg = data[i:j]
            printable = [c for c in seg if 0x20 <= c < 0x7F]
            if len(printable) >= min_len:
                s = "".join(chr(c) if 0x20 <= c < 0x7F else " " for c in seg)
                cnt = Counter(printable)
                top = cnt.most_common(1)[0][1] / len(printable)
                letters = sum(1 for c in printable if 65 <= c <= 90 or 97 <= c <= 122)
                spaces = printable.count(32)
                words = re.findall(r"[A-Za-z]{2,15}", s)
                good = [w for w in words if len(w) >= 3 and any(ord(ch) in VOW for ch in w)]
                longrun = re.search(r"[A-Za-z]{17,}", s)
                # восходящая рампа (ABCDEF…) = шрифт-данные
                ramp = any(all(ord(s[k + m]) == ord(s[k]) + m for m in range(6))
                           for k in range(len(s) - 6) if s[k].isalpha())
                if (top < 0.40 and (letters + spaces) / len(printable) > 0.60
                        and len(good) >= 4 and not longrun and not ramp and len(cnt) >= 10):
                    out.append((i, j - i))
            i = j + 1
        else:
            i += 1
    merged = []
    for s, l in out:
        if merged and s - (merged[-1][0] + merged[-1][1]) < 32:
            merged[-1] = (merged[-1][0], s + l - merged[-1][0])
        else:
            merged.append((s, l))
    return [(s, l) for s, l in merged if l >= min_len]


def _decode_game_text(data: bytes, start: int, length: int) -> str:
    """Декодировать ASCII-блок внутриигрового текста. Печатаемые 0x20-0x7E — текст; нули, управляющие
    байты (иконки/спрайт-рефы) и >=0x7F → разделитель строк. Пустые строки схлопываются."""
    raw = data[start:start + length]
    lines = []
    cur = []
    for b in raw:
        if 0x20 <= b < 0x7F:
            cur.append(chr(b))
        else:                                   # null / control / high → разделитель
            if cur:
                lines.append("".join(cur).rstrip()); cur = []
    if cur:
        lines.append("".join(cur).rstrip())
    out = []
    for ln in lines:
        if len(ln) <= 1:               # одиночные символы = иконка/спрайт-реф между сообщениями
            continue
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out).strip()


# Шрифты — наборы глифов 8×8 4bpp. Letters(40: A-Z 0-9 .-:?), Numbers(10 цифр боезапаса),
# Font2(36: 0-9 A-Z) — простые 8×8. Font_grph(150) — БОЛЬШОЙ декоративный 8×16: каждый глиф =
# 2 тайла по вертикали, тайлы НЕпоследовательны — собираются по таблице (символ→верх|низ тайл,
# рендерер sub_55354). July Font_grph СЖАТ byte-pair @0x14D038. Запись:
# Запись: (имя, адрес, ntiles, столбцов, compressed, table_addr|None, пал_жёлтая|None, пал_белая|None).
# table_addr → режим сборки 8×16. Большой шрифт на заставке БЫВАЕТ И ЖЁЛТЫМ (линия 3 0x6B6A0/...)
# И БЕЛЫМ (кредиты 0x54222/...) — даём обе палитры в выбор.
FONT_BANKS = {
    "Zero Tolerance": [
        ("Letters (A-Z 0-9)", 0x16E758, 40, 20, False, None),
        (tr("Numbers (цифры)", "Numbers (digits)"),   0x16E618, 10, 10, False, None),
        ("Font2",             0x12EAA6, 36, 18, False, None),
        (tr("Font_grph (большой)", "Font_grph (large)"), 0x125116, 150, 16, False, 0x1DD3A, 0xCD47A, 0x1CDAE),
    ],
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        ("Letters (A-Z 0-9)", 0x16F4BA, 40, 20, False, None, None, None),
        (tr("Numbers (цифры)", "Numbers (digits)"),   0x16F37A, 10, 10, False, None, None, None),
        ("Font2",             0x164DE2, 36, 18, False, None, None, None),
        (tr("Font_grph (большой)", "Font_grph (large)"), 0x1544DA, 150, 16, False, 0x551E8, 0x6B6A0, 0x54222),
    ],
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        ("Letters (A-Z 0-9)", 0x164320, 40, 20, False, None, None, None),
        (tr("Numbers (цифры)", "Numbers (digits)"),   0x1641E0, 10, 10, False, None, None, None),
        ("Font2",             0x15CCD8, 36, 18, False, None, None, None),
        (tr("Font_grph (большой, сжат)", "Font_grph (large, compressed)"), 0x14D038, 150, 16, True, 0x2377C, 0x81C3C, 0x227D2),
    ],
}


# ZTU (Underground) — все 4 шрифта релокированы. Letters/Numbers сдвинуты +0xD0B4 (зазор 0x140
# сохранён), Font2/Font_grph −0x24858. Адреса из кода загрузки в VRAM (те же VRAM-слоты 0x75a0/
# 0x4860, что у релиза → структура и таблица 8×16 идентичны, рендер-логика общая). Большой шрифт:
# tile-data 0x1008BE, char→cell таблица 0x1E702. Жёлтую/белую палитры заставки в ZTU не выверял
# → None (FontViewer падает в серый/палитры зон). ZTU startswith «Zero Tolerance» → обрабатываем
# до общего цикла, иначе схватит адреса релиза.
FONT_BANKS_ZTU = [
    ("Letters (A-Z 0-9)",   0x17B80C, 40, 20, False, None, None, None),
    (tr("Numbers (цифры)", "Numbers (digits)"),     0x17B6CC, 10, 10, False, None, None, None),
    ("Font2",               0x10A24E, 36, 18, False, None, None, None),
    (tr("Font_grph (большой)", "Font_grph (large)"), 0x1008BE, 150, 16, False, 0x1E702, None, None),
]


def _font_bank(version: str):
    if "Underground" in version:
        return FONT_BANKS_ZTU
    for key, val in FONT_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


# «Прочие экраны»: HUD/кокпит, меню паузы, титул (Main), Accolade. Полноэкранные 40×28 =
# графика + тайлкарта + палитра. line_add = смещение палитра-линии из d3 блиттера (HUD рисуется
# линией 3: d3=0xE301 → юзер: June HUD = 0xB9C9E = Pal_1epG L3; задаём pal=база эп.палитры + line_add 3).
# compressed = графика byte-pair (July). Запись: (имя, Gr, TM, Pal, W, H, line_add, compressed).
# July HUD/Pause/Title тайлкарты = июньские байт-в-байт (графика сжата). ZT-релиз HUD/Pause/Main
# переработаны (ищутся отдельно); Accolade общий (Acc_Gr @ZT 0x53DA0).
OTHER_SCREEN_BANKS = {
    # ZT-релиз: экраны в формате 0x1F72 (gfx=base+0x8C8, tm=base+8). Палитра эпизода 0x2072
    # (64 цв), line_add = палитра-линия из d3 блиттера (jsr $1f72): HUD d3=0xE000→L3, перспектива
    # d3=0x6000→L3, копирайт d3=0x8000→L0. (Код 0x29F6/0x2A4E: HUD грузит 0x2072 L3=0x20D2.)
    "Zero Tolerance": [
        (tr("Копирайт (Zero Tolerance)", "Copyright (Zero Tolerance)"), 0x117686, 0x116DC6, 0x2072, 40, 28, 0, False),
        (tr("HUD / кокпит", "HUD / cockpit"),             0x156CAE, 0x1563EE, 0x2072, 40, 28, 3, False),
        (tr("Перспектива (оружие)", "Perspective (weapon)"),     0x12DD06, 0x12D446, 0x2072, 40, 28, 3, False),
    ],
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        (tr("Титул (BEYOND ZT)", "Title (BEYOND ZT)"), 0x56068, 0x557A8, 0x5CBE8, 40, 28, 0, False),
        (tr("Меню паузы", "Pause menu"),        0x167F62, 0x16A5C2, 0x16AE82, 40, 28, 0, False),
        (tr("HUD / кокпит", "HUD / cockpit"),      0x165562, 0x1673C2, 0xB9C3E, 40, 28, 3, False),
    ],
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Титул (BEYOND ZT)", "Title (BEYOND ZT)"), 0x245D4, 0x23D14, 0x28664, 40, 28, 0, True),
        (tr("Меню паузы", "Pause menu"),        0x160D7C, 0x161C08, 0x1624C8, 40, 28, 0, True),
        (tr("HUD / кокпит", "HUD / cockpit"),      0x15F5B8, 0x16015C, 0x97B20, 40, 28, 3, True),
    ],
}


def _other_screen_bank(version: str):
    if "Underground" in version:           # ZTU: 0x1F72-экраны (gfx=base+0x8C8, tm=base+8),
        return [                            # палитра 0x2194 (подтв. на кокпите)
            (tr("Копирайт (Technopop)", "Copyright (Technopop)"), 0xF2E2E, 0xF256E, 0x2194, 40, 28, 0, False),
            (tr("Кокпит (перспектива)", "Cockpit (perspective)"), 0x1094AE, 0x108BEE, 0x2194, 40, 28, 0, False),
            (tr("Кокпит (HUD)", "Cockpit (HUD)"),         0x170256, 0x16F996, 0x2194, 40, 28, 0, False),
        ]
    for key, val in OTHER_SCREEN_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


# Межэпизодные экраны (брифинги/кат-сцены: начало/конец/гибель эпизода, концовка).
# Формат (по дизасму BZT_Disasm0.12, sub_68FF6 + Briefs/): полноэкранный 40×28 тайлов =
# 320×224 px = графика _Gr (8×8 4bpp) + тайлкарта _TM (40×28 слов, MD nametable с h/v-флипом
# и БИТАМИ ЛИНИИ палитры 13-14) + палитра _Pal (64 цвета = 4 линии). Лежат КОНСЕКУТИВНО
# Gr→TM→Pal→Gr… (Pal = TM+0x8C0). June: точные адреса из дизасма; ZT-релиз: своя цепочка
# (Ep1Start=солдат-ТВ, Ep1End=канистра — отличаются от June). July СЖАТ (byte-pair) — отдельно.
# Запись: (имя, Gr, TM, Pal). W=40 H=28.
SCREEN_BANKS = {
    "Zero Tolerance": [
        (tr("Эп.1 начало (солдат)", "Ep.1 start (soldier)"), 0xCD49A, 0xD54BA, 0xD5D7A),
        (tr("Эп.1 конец (канистра)", "Ep.1 end (canister)"), 0xD5DFA, 0xDD63A, 0xDDEFA),
        (tr("Эп.2 начало (крыша)", "Ep.2 start (rooftop)"),  0xDDF7A, 0xE679A, 0xE705A),
        (tr("Эп.3 начало (лестница)", "Ep.3 start (stairs)"), 0xE70DA, 0xEF2FA, 0xEFBBA),
        (tr("Эп.2 гибель (взрыв)", "Ep.2 death (explosion)"),  0xEFC3A, 0xF84DA, 0xF8D9A),
        (tr("Эп.3 гибель (EXIT)", "Ep.3 death (EXIT)"),   0xF8E1A, 0x1016DA, 0x101F9A),
        (tr("Концовка (пришелец)", "Ending (alien)"),  0x10201A, 0x10A7DA, 0x10B09A),
    ],
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        (tr("Эп.1 начало (остров)", "Ep.1 start (island)"), 0x6B6C0, 0x72B00, 0x733C0),
        (tr("Эп.1 конец (робот)", "Ep.1 end (robot)"),   0x73440, 0x7B240, 0x7BB00),
        (tr("Эп.2 начало (крыша)", "Ep.2 start (rooftop)"),  0x7BB80, 0x843A0, 0x84C60),
        (tr("Эп.3 начало (лестница)", "Ep.3 start (stairs)"), 0x84CE0, 0x8CF00, 0x8D7C0),
        (tr("Эп.2 гибель (взрыв)", "Ep.2 death (explosion)"),  0x8D840, 0x960E0, 0x969A0),
        (tr("Эп.3 гибель (EXIT)", "Ep.3 death (EXIT)"),   0x96A20, 0x9F2E0, 0x9FBA0),
        (tr("Концовка (пришелец)", "Ending (alien)"),  0x9FC20, 0xA83E0, 0xA8CA0),
    ],
    # July: экраны СЖАТЫ byte-pair (распаковщик 0x98B98). Запись: (имя, Gr, TM, Pal, True)
    # — 5-й элемент = флаг сжатия Gr; тайлкарта/палитра несжаты, индекс base 0.
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Титул (BEYOND ZT)", "Title (BEYOND ZT)"), 0x245D4, 0x23D14, 0x28664, True),
    ],
}


def _screen_bank(version: str):
    if "Underground" in version:           # ZTU: сюжетные экраны — ТОЧНЫЕ адреса из кода
        # рисования @0x9B49A (lea gfx; #ntiles; lea tm; lea pal). ScreenViewer применяет
        # биты линии палитры из тайлкарты (base=line*16) — у некоторых экранов 2-3 линии
        # («двойной jsr 0x1FBD6» = разные палитры на разные части), потому без линий рябило.
        return [
            (tr("Интро (солдат)", "Intro (soldier)"),        0x9E2D6, 0xA62F6, 0xA6BB6),
            (tr("Сюжет 2 (станция)", "Story 2 (station)"),     0xA6C36, 0xAE476, 0xAED36),
            (tr("Крыша (ночной город)", "Rooftop (night city)"),  0xAEDB6, 0xB75D6, 0xB7E96),
            (tr("Коридор (лестница)", "Corridor (stairs)"),    0xB7F16, 0xC0136, 0xC09F6),
            (tr("Взрыв (город)", "Explosion (city)"),         0xC0A76, 0xC9316, 0xC9BD6),
            (tr("Концовка (пришелец)", "Ending (alien)"),   0xD2E56, 0xDB616, 0xDBED6),
        ]
    for key, val in SCREEN_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


# ID-карты персонажей (удостоверения отряда) — карта 16×12 тайлов = 128×96 px.
# ДВА способа хранения (разобрано по дизасму BZT_Disasm0.12, sub_2892 + таблицы
# sub_2B66/loc_2B7A; адреса incbin Card_*_G/_TM, Pal_1epG/Pal_1epM):
#   • РЕЛИЗ ZT — самодостаточные объекты формата блиттера 0x1F72 (таблица @0x2CC8,
#     5 указателей): [+0 ntiles][+2 gfxoff][+4 W][+6 H][+8 тайлкарта W×H][графика 8×8].
#     Палитра HUD 0x20D2 (полноцвет). Персонажи: Ishii, Haile, … (релизный отряд).
#   • ПРОТОТИПЫ June/July — РАЗДЕЛЬНО графика (_G) + тайлкарта (_TM, 16×12=384б идёт
#     сразу за _G). ДВА набора: IN_GAME (палитра Pal_1epG, зелёный кокпит-вид) и
#     IN_MENU (Pal_1epM, полноцвет). Персонажи: Blinder, Rogue, Gecko, BlackJack, Duke.
# Запись split-карты: (метка, _G, _TM, палитра).

# Таблица 0x1F72-карт (релиз): (адрес_таблицы, число, палитра)
ID_CARD_TABLE = {
    "Zero Tolerance": (0x2CC8, 5, 0x20D2),
}
# ZTU (Underground): та же схема, таблица релокирована @0x2D8A (релиз 0x2CC8; копия @0x96CEE),
# 5 карт-объектов 0x1F72 16×12 @0x101B7E/0x103166/0x1047EE/0x105ED6/0x1075BE. Палитра — HUD ZTU
# 0x2194 (релиз 0x20D2). ZTU startswith «Zero Tolerance» → обрабатываем до общего цикла.
ID_CARD_TABLE_ZTU = (0x2D8A, 5, 0x2194)
_CARD_CHARS = ["Blinder", "Rogue", "Gecko", "BlackJack", "Duke"]
# Раздельные _G/_TM карты прототипов: (метка, _G, _TM, палитра). 16×12 тайлов.
# ПАЛИТРА карт = СПРАЙТ-линия эпизода (Pal_1epM + 0x60 = линия 3), та же, что у оружия
# в руках (HELD_PAL): June 0xB9D1E, July 0x97C00. Портреты рисуются спрайт-палитрой
# (подтверждено: верный оранжевый бланк + телесный тон). Оба набора (в игре/меню) — её же.
ID_CARD_LIST = {
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        # IN_GAME графика
        (tr("Blinder (в игре)", "Blinder (in game)"),   0x15579A, 0x156C7A, 0xB9D1E),
        (tr("Rogue (в игре)", "Rogue (in game)"),     0x156DFA, 0x1582FA, 0xB9D1E),
        (tr("Gecko (в игре)", "Gecko (in game)"),     0x15847A, 0x15997A, 0xB9D1E),
        (tr("BlackJack (в игре)", "BlackJack (in game)"), 0x159AFA, 0x15AFFA, 0xB9D1E),
        (tr("Duke (в игре)", "Duke (in game)"),      0x15B17A, 0x15C65A, 0xB9D1E),
        # IN_MENU графика
        (tr("Blinder (меню)", "Blinder (menu)"),     0x15C7DA, 0x15DC9A, 0xB9D1E),
        (tr("Rogue (меню)", "Rogue (menu)"),       0x15DE1A, 0x15F2DA, 0xB9D1E),
        (tr("Gecko (меню)", "Gecko (menu)"),       0x15F45A, 0x16093A, 0xB9D1E),
        (tr("BlackJack (меню)", "BlackJack (menu)"),   0x160ABA, 0x161F9A, 0xB9D1E),
        (tr("Duke (меню)", "Duke (menu)"),        0x16211A, 0x1635DA, 0xB9D1E),
    ],
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Blinder (в игре)", "Blinder (in game)"),   0x14D690, 0x14EB70, 0x97C00),
        (tr("Rogue (в игре)", "Rogue (in game)"),     0x14ECF0, 0x1501F0, 0x97C00),
        (tr("Gecko (в игре)", "Gecko (in game)"),     0x150370, 0x151870, 0x97C00),
        (tr("BlackJack (в игре)", "BlackJack (in game)"), 0x1519F0, 0x152EF0, 0x97C00),
        (tr("Duke (в игре)", "Duke (in game)"),      0x153070, 0x154550, 0x97C00),
        (tr("Blinder (меню)", "Blinder (menu)"),     0x1546D0, 0x155B90, 0x97C00),
        (tr("Rogue (меню)", "Rogue (menu)"),       0x155D10, 0x1571D0, 0x97C00),
        (tr("Gecko (меню)", "Gecko (menu)"),       0x157350, 0x158830, 0x97C00),
        (tr("BlackJack (меню)", "BlackJack (menu)"),   0x1589B0, 0x159E90, 0x97C00),
        (tr("Duke (меню)", "Duke (menu)"),        0x15A010, 0x15B4D0, 0x97C00),
    ],
}


def _id_card_table(version: str):
    if "Underground" in version:
        return ID_CARD_TABLE_ZTU
    for key, val in ID_CARD_TABLE.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return None


def _id_card_list(version: str):
    for key, val in ID_CARD_LIST.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return None


# СБОРКА ПО ТАЙЛКАРТЕ (не линейно!): фон = тайлы (gfx) + тайлкарта (nt, MD nametable-слова
# idx&0x7FF, hflip b11, vflip b12) шириной W × высотой H; тайл банка = (idx&0x7FF) − base.
# ZT: тайлкарта ЛЕЖИТ В ROM сразу за тайлами фона. Город nt 0x153106 (128×18, base 0x141,
# пал 0x2132); космос nt 0x1553E6 (128×18, base 0x141, пал 0x2232). Сверено с ZTSky2.png/
# world-pano-space.png — точное совпадение. BZT: Plan_A(тайлы)+TMap(тайлкарта) из таблицы
# эпизода, idx напрямую (base 0). Запись: (имя, gfx, nt, base, W, H, палитра).
BACKGROUND_BANKS = {
    "Zero Tolerance": [
        (tr("Город (ZTSky2, ep2)", "City (ZTSky2, ep2)"), 0x14F046, 0x153106, 0x141, 128, 16, 0x2132),
        (tr("Космос (world-pano, ep1)", "Space (world-pano, ep1)"), 0x154406, 0x1553E6, 0x141, 128, 16, 0x2232),
    ],
    # BZT: Plan_A(тайлы→VRAM 0x2820) + TMap(тайлкарта, idx 1-233 ОТНОСИТЕЛЬНО, base 0),
    # 128×16. Адреса tile-map по содержимому Graphics/Tile_Map/*.bin (= дизасм).
    "Beyond Zero Tolerance (прототип 1995-06-23)": [
        (tr("Панорама ep1 (лес)", "Panorama ep1 (forest)"), 0xBD6FE, 0xBF43E, 0, 128, 16, 0xB9CFE),
        (tr("Панорама ep2", "Panorama ep2"), 0xEB36A, 0xEE88A, 0, 128, 16, 0xB9DFE),
        (tr("Панорама ep3", "Panorama ep3"), 0x121E22, 0x124182, 0, 128, 16, 0xB9FFE),
    ],
    # July: Plan_A СЖАТ (byte-pair, распаковщик 0x98B98). Дескриптор-зоны @0x97A40
    # (массив шаг 0x38): Plan_A(+0x2C), TMap(+0x30), палитра = поле f1(+0x04)+0x40.
    # gfx = адрес СЖАТОГО Plan_A; _compose распаковывает его (флаг 8-м элементом).
    # 4 эпизода (June было 3 — ep3 «адский город» July-уникальный).
    "Beyond Zero Tolerance (прототип 1995-07-14)": [
        (tr("Панорама ep1 (закат/горы)", "Panorama ep1 (sunset/mountains)"), 0x9B8E0, 0x9C7F8, 0, 128, 16, 0x97BE0, True),
        (tr("Панорама ep2 (саванна/башни)", "Panorama ep2 (savanna/towers)"), 0xC8B24, 0xCAB3C, 0, 128, 16, 0x97CE0, True),
        (tr("Панорама ep3 (джунгли)", "Panorama ep3 (jungle)"), 0xF5E68, 0xF7664, 0, 128, 16, 0x97DE0, True),
        (tr("Панорама ep4 (адский город)", "Panorama ep4 (hell city)"), 0x122990, 0x1244F8, 0, 128, 16, 0x97EE0, True),
    ],
}


def _bg_banks(version: str):
    if "Underground" in version:           # ZTU: фоны переразмечены, адреса не найдены
        return []
    for key, val in BACKGROUND_BANKS.items():
        if version.startswith(key):
            return _de_struct(version, val)
    return []


class BackgroundViewer(QWidget):
    """Вкладка «Фоны»: панорамы неба/города/космоса (плоскость A/B). Стандартные
    8×8 4bpp тайлы; ширина листа регулируется (фон-тайлы лежат ~в порядке панорамы).
    Пресеты банков по версии, выбор палитры (линии зон), зум, экспорт."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self.view = TileView()
        self.view.set_zoom(2)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Фоны 3D-вида: 8×8-тайлы, ширина под панораму.", "3D-view backgrounds: 8×8 tiles, width sized for a panorama."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.preset = QComboBox()                  # пресеты банков по версии
        self.preset.currentIndexChanged.connect(self._preset_changed)
        form.addRow(tr("Фон:", "Background:"), self.preset)
        self.gfx = _hex_spin(0xFFFFFF, 0x14F046)   # тайлы фона
        self.gfx.valueChanged.connect(self._render)
        form.addRow(tr("Тайлы (gfx):", "Tiles (gfx):"), self.gfx)
        self.nt = _hex_spin(0xFFFFFF, 0x153106)    # тайлкарта
        self.nt.valueChanged.connect(self._render)
        form.addRow(tr("Тайлкарта:", "Tilemap:"), self.nt)
        self.base = QSpinBox(); self.base.setRange(0, 0x7FF); self.base.setValue(0x141)
        self.base.valueChanged.connect(self._render)
        form.addRow(tr("База тайла (idx−):", "Tile base (idx−):"), self.base)
        self.width = QSpinBox(); self.width.setRange(1, 256); self.width.setValue(128)
        self.width.valueChanged.connect(self._render)
        form.addRow(tr("Ширина (тайлов):", "Width (tiles):"), self.width)
        self.height = QSpinBox(); self.height.setRange(1, 128); self.height.setValue(16)
        self.height.valueChanged.connect(self._render)
        form.addRow(tr("Высота (тайлов):", "Height (tiles):"), self.height)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 12); self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()                # наполняется по версии (itemData=офсет)
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, 0x20F2)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.export_btn = QPushButton(tr("Экспорт PNG…", "Export PNG…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        for sp in (self.gfx, self.nt):
            sp.setMaximum(max(0, rom.size - 1))
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        # пресеты банков по версии (gfx, nt, base, W, H, pal)
        self.preset.blockSignals(True); self.preset.clear()
        for rec in _bg_banks(rom.version):
            self.preset.addItem(rec[0], rec[1:])
        self.preset.blockSignals(False)
        # палитры: линии зон + стены/HUD + ручной + серый
        self.pal_mode.blockSignals(True); self.pal_mode.clear()
        for z in (zonemod.parse_zones(rom.data, rom.version) or []):
            pa = z["palA"]
            for ln in range(4):
                self.pal_mode.addItem(
                    tr(f"Зона{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})", f"Zone{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})"), pa + ln * 0x20)
        if rom.version.startswith("Zero Tolerance"):
            self.pal_mode.addItem(tr("Город L2 (0x2132)", "City L2 (0x2132)"), 0x2132)
            self.pal_mode.addItem(tr("Космос L2 (0x2232)", "Space L2 (0x2232)"), 0x2232)
        self.pal_mode.addItem(tr("Из ROM (смещение)", "From ROM (offset)"), "rom")
        self.pal_mode.addItem(tr("Оттенки серого", "Grayscale"), "gray")
        self.pal_mode.setCurrentIndex(0)
        self.pal_mode.blockSignals(False)
        self.view.set_zoom(self.zoom.value())
        if self.preset.count():
            self.preset.setCurrentIndex(0)
            self._preset_changed()
        else:
            self._render()

    def _preset_changed(self, *_) -> None:
        data = self.preset.currentData()
        if not data:
            return
        gfx, nt, base, w, h, paloff = data[:6]
        # 8-й элемент = флаг сжатия Plan_A (byte-pair) для прототипа July
        self._compressed = bool(len(data) > 6 and data[6])
        for sp, val in ((self.gfx, gfx), (self.nt, nt), (self.base, base),
                        (self.width, w), (self.height, h)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        # выбрать палитру пресета в чузере, если есть; иначе «из ROM» + офсет
        idx = self.pal_mode.findData(paloff)
        if idx >= 0:
            self.pal_mode.blockSignals(True); self.pal_mode.setCurrentIndex(idx)
            self.pal_mode.blockSignals(False)
        else:
            self.pal_offset.blockSignals(True); self.pal_offset.setValue(paloff)
            self.pal_offset.blockSignals(False)
            ri = self.pal_mode.findData("rom")
            if ri >= 0:
                self.pal_mode.blockSignals(True); self.pal_mode.setCurrentIndex(ri)
                self.pal_mode.blockSignals(False)
        self._render()

    def _palette(self):
        rom = self.main.rom
        if rom is None:
            return pal.grayscale_palette()
        data = self.pal_mode.currentData()
        if data == "gray":
            return pal.grayscale_palette()
        if data == "rom":
            return pal.read_palette(rom.data, self.pal_offset.value())
        if isinstance(data, int):
            return pal.read_palette(rom.data, data)
        return pal.grayscale_palette()

    def _compose(self) -> Optional[QImage]:
        """Собрать фон ПО ТАЙЛКАРТЕ: nt[ty*W+tx] → тайл (idx−base) из gfx, с h/v-флипом.

        Для July (флаг сжатия) Plan_A СЖАТ byte-pair — сначала распаковываем его в
        буфер тайлов (кэш по адресу gfx), затем индексируем тайлкартой как обычно."""
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data
        gfx, nt, base = self.gfx.value(), self.nt.value(), self.base.value()
        W, H = self.width.value(), self.height.value()
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        out = bytearray(W * 8 * H * 8)
        n = len(d)
        # сжатый Plan_A (July): распаковать в буфер тайлов (кэш), индексировать от 0
        src, src_base = d, gfx
        if getattr(self, "_compressed", False):
            if getattr(self, "_decoded_at", None) != gfx:
                self._decoded = tiles.decode_bg_bytepair(d, gfx)
                self._decoded_at = gfx
            src, src_base = self._decoded, 0
        sn = len(src)
        for ty in range(H):
            for tx in range(W):
                o = nt + (ty * W + tx) * 2
                if o + 1 >= n:
                    continue
                e = (d[o] << 8) | d[o + 1]
                idx = (e & 0x7FF) - base
                if idx < 0 or src_base + idx * 32 + 32 > sn:
                    continue
                tb = bytearray(tiles.decode_sheet(src, src_base + idx * 32, 1, 1)[0])
                if e & 0x800:                      # hflip
                    for r in range(8):
                        tb[r * 8:r * 8 + 8] = tb[r * 8:r * 8 + 8][::-1]
                if e & 0x1000:                     # vflip
                    tb = bytearray(b"".join(bytes(tb[r * 8:r * 8 + 8])
                                            for r in range(7, -1, -1)))
                for r in range(8):
                    row = (ty * 8 + r) * (W * 8) + tx * 8
                    out[row:row + 8] = tb[r * 8:r * 8 + 8]
        return build_qimage(bytes(out), W * 8, H * 8, colors, ti)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = self.preset.currentText().split(" (")[0].replace(" ", "_") or "bg"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


class IdCardViewer(QWidget):
    """Вкладка «ID-карты»: удостоверения отряда U-RON (релиз ZT).

    Каждая карта — самодостаточный объект формата блиттера 0x1F72: [+0 ntiles]
    [+2 gfxoff][+4 W][+6 H][+8 тайлкарта W×H слов (MD nametable: idx&0x7FF,
    h/v-флип)][графика 8×8 4bpp]. Таблица @0x2CC8 = 5 указателей. Палитра по
    умолчанию — HUD 0x20D2 (полноцветная). 5 карт = весь отряд (Ishii, Haile, …).
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._cards: List[int] = []        # адреса карт из таблицы
        self.view = TileView()
        self.view.set_zoom(2)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("ID-карты персонажей U-RON (только релиз ZT).", "U-RON character ID cards (ZT release only)."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.card = QComboBox()                    # выбор карты (itemData=(gfx,tm,W,H,pal))
        self.card.currentIndexChanged.connect(self._render)
        form.addRow(tr("Карта:", "Card:"), self.card)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 12); self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()                # наполняется в set_rom
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, 0x20D2)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.export_btn = QPushButton(tr("Экспорт PNG…", "Export PNG…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        # построить унифицированный список карт: (метка, gfx, tm, W, H, палитра)
        self._cards = []
        defpal = 0x20D2
        lst = _id_card_list(rom.version)            # прототипы: раздельные _G/_TM
        tbl = _id_card_table(rom.version)           # релиз: объекты 0x1F72 по таблице
        d = rom.data
        if lst is not None:
            for label, gfx, tm, paloff in lst:
                self._cards.append((label, gfx, tm, 16, 12, paloff))
            defpal = lst[0][3]
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Карты отряда (Blinder/Rogue/Gecko/BlackJack/"
                f"Duke): {len(lst)} — наборы «в игре» (зелёный) и «меню» (цвет). 128×96.", f"<b>{version_label(rom.version)}</b><br>Squad cards (Blinder/Rogue/Gecko/BlackJack/"
                f"Duke): {len(lst)} — «in-game» (green) and «menu» (color) sets. 128×96."))
        elif tbl is not None:
            tbase, cnt, paloff = tbl
            defpal = paloff
            for i in range(cnt):
                o = tbase + i * 4
                if o + 4 > len(d):
                    break
                addr = (d[o] << 24) | (d[o + 1] << 16) | (d[o + 2] << 8) | d[o + 3]
                if not (0 < addr < len(d) - 8):
                    continue
                gfxoff = (d[addr + 2] << 8) | d[addr + 3]
                cw = (d[addr + 4] << 8) | d[addr + 5]
                ch = (d[addr + 6] << 8) | d[addr + 7]
                if not (1 <= cw <= 64 and 1 <= ch <= 64 and gfxoff == 8 + cw * ch * 2):
                    continue
                self._cards.append((tr(f"Карта {i + 1}", f"Card {i + 1}"), addr + gfxoff, addr + 8, cw, ch, paloff))
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Отряд U-RON: {len(self._cards)} ID-карт "
                f"(128×96), таблица 0x{tbase:06X}, формат 0x1F72.", f"<b>{version_label(rom.version)}</b><br>U-RON squad: {len(self._cards)} ID cards "
                f"(128×96), table 0x{tbase:06X}, format 0x1F72."))
        else:
            self.info.setText(tr(f"<b>{version_label(rom.version)}</b><br>ID-карты не найдены.", f"<b>{version_label(rom.version)}</b><br>ID cards not found."))
        # палитра: дефолтная карты + HUD/зелёная + ручной + серый
        self.pal_mode.blockSignals(True); self.pal_mode.clear()
        self.pal_mode.addItem(tr(f"Карты (0x{defpal:04X})", f"Maps (0x{defpal:04X})"), defpal)
        self.pal_mode.addItem("HUD 0x20D2", 0x20D2)
        self.pal_mode.addItem(tr("HUD зелёная 0x2072", "HUD green 0x2072"), 0x2072)
        self.pal_mode.addItem(tr("Из ROM (смещение)", "From ROM (offset)"), "rom")
        self.pal_mode.addItem(tr("Оттенки серого", "Grayscale"), "gray")
        self.pal_mode.setCurrentIndex(0)
        self.pal_offset.setValue(defpal)
        self.pal_mode.blockSignals(False)
        # заполнить список карт
        self.card.blockSignals(True); self.card.clear()
        for label, gfx, tm, cw, ch, paloff in self._cards:
            self.card.addItem(label, (gfx, tm, cw, ch, paloff))
        self.card.blockSignals(False)
        self.view.set_zoom(self.zoom.value())
        self._render()

    def _palette(self, paloff=None):
        rom = self.main.rom
        if rom is None:
            return pal.grayscale_palette()
        data = self.pal_mode.currentData()
        # «Карты» = палитра выбранной карты (зависит от набора в игре/меню)
        if self.pal_mode.currentIndex() == 0 and paloff is not None:
            return pal.read_palette(rom.data, paloff)
        if data == "gray":
            return pal.grayscale_palette()
        if data == "rom":
            return pal.read_palette(rom.data, self.pal_offset.value())
        if isinstance(data, int):
            return pal.read_palette(rom.data, data)
        return pal.grayscale_palette()

    def _compose(self) -> Optional[QImage]:
        """Собрать карту: тайлкарта (с h/v-флипом) индексирует графику (base 0)."""
        rom = self.main.rom
        if rom is None:
            return None
        cur = self.card.currentData()
        if cur is None:
            return None
        gfx, nt, W, H, paloff = cur
        d = rom.data; n = len(d)
        if W <= 0 or H <= 0 or W > 64 or H > 64:
            return None
        colors = self._palette(paloff)
        ti = 0 if self.transparent.isChecked() else None
        out = bytearray(W * 8 * H * 8)
        for ty in range(H):
            for tx in range(W):
                o = nt + (ty * W + tx) * 2
                if o + 1 >= n:
                    continue
                e = (d[o] << 8) | d[o + 1]
                idx = e & 0x7FF
                if gfx + idx * 32 + 32 > n:
                    continue
                tb = bytearray(tiles.decode_sheet(d, gfx + idx * 32, 1, 1)[0])
                if e & 0x800:                      # hflip
                    for r in range(8):
                        tb[r * 8:r * 8 + 8] = tb[r * 8:r * 8 + 8][::-1]
                if e & 0x1000:                     # vflip
                    tb = bytearray(b"".join(bytes(tb[r * 8:r * 8 + 8])
                                            for r in range(7, -1, -1)))
                for r in range(8):
                    row = (ty * 8 + r) * (W * 8) + tx * 8
                    out[row:row + 8] = tb[r * 8:r * 8 + 8]
        return build_qimage(bytes(out), W * 8, H * 8, colors, ti)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.card.currentText().split(" (")[0] or "idcard").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


class ScreenViewer(QWidget):
    """Вкладка «Экраны»: межэпизодные брифинги/кат-сцены (начало/конец/гибель эпизода,
    концовка). Полноэкранный 40×28 = графика (_Gr) + тайлкарта (_TM) + палитра 64 цв
    (_Pal, 4 линии). Тайлкарта несёт h/v-флип и биты линии палитры (13-14).
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self.view = TileView()
        self.view.set_zoom(2)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Межэпизодные экраны (брифинги/кат-сцены).", "Inter-episode screens (briefings/cutscenes)."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.screen = QComboBox()              # выбор экрана (itemData=(gr,tm,pal,W,H))
        self.screen.currentIndexChanged.connect(self._screen_changed)
        form.addRow(tr("Экран:", "Screen:"), self.screen)
        self.gr = _hex_spin(0xFFFFFF, 0x6B6C0)
        self.gr.valueChanged.connect(self._render)
        form.addRow(tr("Графика (Gr):", "Graphics (Gr):"), self.gr)
        self.tm = _hex_spin(0xFFFFFF, 0x72B00)
        self.tm.valueChanged.connect(self._render)
        form.addRow(tr("Тайлкарта (TM):", "Tilemap (TM):"), self.tm)
        self.paloff = _hex_spin(0xFFFFFF, 0x733C0)
        self.paloff.valueChanged.connect(self._render)
        form.addRow(tr("Палитра (Pal):", "Palette (Pal):"), self.paloff)
        self.width = QSpinBox(); self.width.setRange(1, 64); self.width.setValue(40)
        self.width.valueChanged.connect(self._render)
        form.addRow(tr("Ширина (тайлов):", "Width (tiles):"), self.width)
        self.height = QSpinBox(); self.height.setRange(1, 64); self.height.setValue(28)
        self.height.valueChanged.connect(self._render)
        form.addRow(tr("Высота (тайлов):", "Height (tiles):"), self.height)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 8); self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.stateChanged.connect(self._render)
        form.addRow(self.transparent)
        v.addLayout(form)

        self.export_btn = QPushButton(tr("Экспорт PNG…", "Export PNG…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        for sp in (self.gr, self.tm, self.paloff):
            sp.setMaximum(max(0, rom.size - 1))
        banks = _screen_bank(rom.version)
        self.screen.blockSignals(True); self.screen.clear()
        for rec in banks:
            name, gr, tm, pal = rec[0], rec[1], rec[2], rec[3]
            comp = bool(len(rec) > 4 and rec[4])   # 5-й элемент = флаг сжатия Gr (July)
            self.screen.addItem(name, (gr, tm, pal, 40, 28, comp))
        self.screen.blockSignals(False)
        if banks:
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>{len(banks)} межэпизодных экранов (40×28): "
                "начало/конец/гибель эпизода, концовка. Графика+тайлкарта+палитра 64 цв.", f"<b>{version_label(rom.version)}</b><br>{len(banks)} inter-episode screens (40×28): "
                "episode start/end/death, ending. Graphics+tilemap+64-color palette."))
        else:
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Экраны для этой версии пока не заданы "
                "(July сжат) — можно указать Gr/TM/Pal вручную.", f"<b>{version_label(rom.version)}</b><br>Screens for this version are not defined yet "
                "(July is compressed) — you can set Gr/TM/Pal manually."))
        self.view.set_zoom(self.zoom.value())
        if self.screen.count():
            self.screen.setCurrentIndex(0)
            self._screen_changed()
        else:
            self._render()

    def _screen_changed(self, *_) -> None:
        data = self.screen.currentData()
        if not data:
            return
        gr, tm, pal, W, H, comp = data
        self._compressed = comp
        for sp, val in ((self.gr, gr), (self.tm, tm), (self.paloff, pal),
                        (self.width, W), (self.height, H)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        self._render()

    def _compose(self) -> Optional[QImage]:
        """Собрать экран по тайлкарте 40×28 с 64-цветной палитрой (4 линии) и h/v-флипом."""
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data; n = len(d)
        gfx, tmap, pal = self.gr.value(), self.tm.value(), self.paloff.value()
        W, H = self.width.value(), self.height.value()
        if pal + 128 > n:
            return None
        colors = [pal_module_rgb((d[pal + i * 2] << 8) | d[pal + i * 2 + 1]) for i in range(64)]
        ti = 0 if self.transparent.isChecked() else None
        out = bytearray(W * 8 * H * 8)          # индексы 0..63 (линия*16 + значение)
        # July: графика СЖАТА byte-pair — распаковать в буфер (кэш), индексировать от 0
        src, src_base = d, gfx
        if getattr(self, "_compressed", False):
            if getattr(self, "_decoded_at", None) != gfx:
                self._decoded = tiles.decode_bg_bytepair(d, gfx, max_bytes=0x20000)
                self._decoded_at = gfx
            src, src_base = self._decoded, 0
        sn = len(src)
        for ty in range(H):
            for tx in range(W):
                o = tmap + (ty * W + tx) * 2
                if o + 1 >= n:
                    continue
                e = (d[o] << 8) | d[o + 1]
                idx = e & 0x7FF
                line = (e >> 13) & 3
                if src_base + idx * 32 + 32 > sn:
                    continue
                tb = bytearray(tiles.decode_sheet(src, src_base + idx * 32, 1, 1)[0])
                if e & 0x800:
                    for r in range(8):
                        tb[r * 8:r * 8 + 8] = tb[r * 8:r * 8 + 8][::-1]
                if e & 0x1000:
                    tb = bytearray(b"".join(bytes(tb[r * 8:r * 8 + 8])
                                            for r in range(7, -1, -1)))
                base = line * 16
                for r in range(8):
                    row = (ty * 8 + r) * (W * 8) + tx * 8
                    for c in range(8):
                        out[row + c] = base + tb[r * 8 + c]
        return build_qimage(bytes(out), W * 8, H * 8, colors, ti)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.screen.currentText().split(" (")[0] or "screen").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


def pal_module_rgb(word: int):
    """MD-цвет (9-бит BGR, 0000 BBB0 GGG0 RRR0) → (r,g,b) 0..255."""
    return ((word & 0xE) * 0x12, ((word >> 4) & 0xE) * 0x12, ((word >> 8) & 0xE) * 0x12)


class OtherScreenViewer(QWidget):
    """Вкладка «Прочие экраны»: HUD/кокпит, меню паузы, титул, Accolade.

    Полноэкранный 40×28 = графика + тайлкарта + палитра 64 цв. line_add — смещение
    палитра-линии из d3 блиттера (HUD = линия 3). compressed — графика byte-pair (July).
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._compressed = False
        self._line_add = 0
        self.view = TileView()
        self.view.set_zoom(2)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("HUD/кокпит, меню паузы, титул.", "HUD/cockpit, pause menu, title."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.screen = QComboBox()
        self.screen.currentIndexChanged.connect(self._screen_changed)
        form.addRow(tr("Экран:", "Screen:"), self.screen)
        self.gr = _hex_spin(0xFFFFFF, 0x165562); self.gr.valueChanged.connect(self._render)
        form.addRow(tr("Графика (Gr):", "Graphics (Gr):"), self.gr)
        self.tm = _hex_spin(0xFFFFFF, 0x1673C2); self.tm.valueChanged.connect(self._render)
        form.addRow(tr("Тайлкарта (TM):", "Tilemap (TM):"), self.tm)
        self.paloff = _hex_spin(0xFFFFFF, 0xB9C3E); self.paloff.valueChanged.connect(self._render)
        form.addRow(tr("Палитра (Pal):", "Palette (Pal):"), self.paloff)
        self.line_add = QSpinBox(); self.line_add.setRange(0, 3); self.line_add.setValue(0)
        self.line_add.valueChanged.connect(self._on_lineadd)
        form.addRow(tr("Сдвиг линии (d3):", "Line shift (d3):"), self.line_add)
        self.width = QSpinBox(); self.width.setRange(1, 64); self.width.setValue(40)
        self.width.valueChanged.connect(self._render)
        form.addRow(tr("Ширина (тайлов):", "Width (tiles):"), self.width)
        self.height = QSpinBox(); self.height.setRange(1, 64); self.height.setValue(28)
        self.height.valueChanged.connect(self._render)
        form.addRow(tr("Высота (тайлов):", "Height (tiles):"), self.height)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 8); self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        self.export_btn = QPushButton(tr("Экспорт PNG…", "Export PNG…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def _on_lineadd(self, val) -> None:
        self._line_add = val
        self._render()

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        for sp in (self.gr, self.tm, self.paloff):
            sp.setMaximum(max(0, rom.size - 1))
        banks = _other_screen_bank(rom.version)
        self.screen.blockSignals(True); self.screen.clear()
        for rec in banks:
            name, gr, tm, pal, W, H, la, comp = rec
            self.screen.addItem(name, (gr, tm, pal, W, H, la, comp))
        self.screen.blockSignals(False)
        if banks:
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>{len(banks)} экранов: HUD/кокпит, меню паузы, титул. "
                "HUD рисуется линией 3 палитры эпизода.", f"<b>{version_label(rom.version)}</b><br>{len(banks)} screens: HUD/cockpit, pause menu, title. "
                "HUD is drawn with line 3 of the episode palette."))
        else:
            self.info.setText(
                tr(f"<b>{version_label(rom.version)}</b><br>Прочие экраны для этой версии пока не заданы "
                "— можно указать Gr/TM/Pal вручную.", f"<b>{version_label(rom.version)}</b><br>Other screens for this version are not defined yet "
                "— you can set Gr/TM/Pal manually."))
        self.view.set_zoom(self.zoom.value())
        if self.screen.count():
            self.screen.setCurrentIndex(0); self._screen_changed()
        else:
            self._render()

    def _screen_changed(self, *_) -> None:
        data = self.screen.currentData()
        if not data:
            return
        gr, tm, pal, W, H, la, comp = data
        self._compressed = comp
        self._line_add = la
        for sp, val in ((self.gr, gr), (self.tm, tm), (self.paloff, pal),
                        (self.width, W), (self.height, H), (self.line_add, la)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        self._render()

    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data; n = len(d)
        gfx, tmap, pal = self.gr.value(), self.tm.value(), self.paloff.value()
        W, H = self.width.value(), self.height.value()
        la = self._line_add
        if pal + 128 > n:
            return None
        colors = [pal_module_rgb((d[pal + i * 2] << 8) | d[pal + i * 2 + 1]) for i in range(64)]
        src, src_base = d, gfx
        if self._compressed:
            if getattr(self, "_decoded_at", None) != gfx:
                self._decoded = tiles.decode_bg_bytepair(d, gfx, max_bytes=0x20000)
                self._decoded_at = gfx
            src, src_base = self._decoded, 0
        sn = len(src)
        out = bytearray(W * 8 * H * 8)
        for ty in range(H):
            for tx in range(W):
                o = tmap + (ty * W + tx) * 2
                if o + 1 >= n:
                    continue
                e = (d[o] << 8) | d[o + 1]
                idx = e & 0x7FF
                line = ((e >> 13) + la) & 3
                if src_base + idx * 32 + 32 > sn:
                    continue
                tb = bytearray(tiles.decode_sheet(src, src_base + idx * 32, 1, 1)[0])
                if e & 0x800:
                    for r in range(8):
                        tb[r * 8:r * 8 + 8] = tb[r * 8:r * 8 + 8][::-1]
                if e & 0x1000:
                    tb = bytearray(b"".join(bytes(tb[r * 8:r * 8 + 8])
                                            for r in range(7, -1, -1)))
                base = line * 16
                for r in range(8):
                    row = (ty * 8 + r) * (W * 8) + tx * 8
                    for c in range(8):
                        out[row + c] = base + tb[r * 8 + c]
        return build_qimage(bytes(out), W * 8, H * 8, colors, None)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.screen.currentText().split(" (")[0] or "screen").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


class FontViewer(QWidget):
    """Вкладка «Шрифты»: наборы глифов 8×8 (Letters/Numbers/Font2/Font_grph) сеткой тайлов.

    Шрифты — стандартные 8×8 4bpp тайлы (читаются grayscale). July Font_grph сжат byte-pair.
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._compressed = False
        self.view = TileView()
        self.view.set_zoom(4)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Шрифты: глифы 8×8.", "Fonts: 8×8 glyphs."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.font = QComboBox()
        self.font.currentIndexChanged.connect(self._font_changed)
        form.addRow(tr("Шрифт:", "Font:"), self.font)
        self.start = _hex_spin(0xFFFFFF, 0x16F4BA); self.start.valueChanged.connect(self._render)
        form.addRow(tr("Адрес:", "Address:"), self.start)
        self.count = QSpinBox(); self.count.setRange(1, 512); self.count.setValue(40)
        self.count.valueChanged.connect(self._render)
        form.addRow(tr("Глифов:", "Glyphs:"), self.count)
        self.cols = QSpinBox(); self.cols.setRange(1, 64); self.cols.setValue(20)
        self.cols.valueChanged.connect(self._render)
        form.addRow(tr("Столбцов:", "Columns:"), self.cols)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 16); self.zoom.setValue(4)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)
        self.pal_offset = _hex_spin(0xFFFFFF, 0x2072)
        self.pal_offset.valueChanged.connect(self._render)
        pf.addRow(tr("Смещение:", "Offset:"), self.pal_offset)
        self.transparent = QCheckBox(tr("Индекс 0 прозрачный", "Index 0 transparent"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        pf.addRow(self.transparent)
        v.addWidget(pal_box)

        self.export_btn = QPushButton(tr("Экспорт PNG…", "Export PNG…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        self.start.setMaximum(max(0, rom.size - 1))
        self.pal_offset.setMaximum(max(0, rom.size - 2))
        banks = _font_bank(rom.version)
        self.font.blockSignals(True); self.font.clear()
        for rec in banks:
            name, addr, nt, cols, comp = rec[0], rec[1], rec[2], rec[3], rec[4]
            table = rec[5] if len(rec) > 5 else None
            fontpal = rec[6] if len(rec) > 6 else None       # жёлтая (заставка L3)
            fontpal_w = rec[7] if len(rec) > 7 else None     # белая (кредиты)
            self.font.addItem(name, (addr, nt, cols, comp, table, fontpal, fontpal_w))
        self.font.blockSignals(False)
        # палитра: жёлтая/белая шрифта + серый + линии зон + ручной
        self.pal_mode.blockSignals(True); self.pal_mode.clear()
        self.pal_mode.addItem(tr("Шрифт: жёлтая (заставка)", "Font: yellow (intro)"), "font_y")
        self.pal_mode.addItem(tr("Шрифт: белая (кредиты)", "Font: white (credits)"), "font_w")
        self.pal_mode.addItem(tr("Оттенки серого", "Grayscale"), "gray")
        for z in (zonemod.parse_zones(rom.data, rom.version) or []):
            pa = z["palA"]
            for ln in range(4):
                self.pal_mode.addItem(
                    tr(f"Зона{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})", f"Zone{z['index'] + 1} L{ln} (0x{pa + ln * 0x20:04X})"), pa + ln * 0x20)
        if rom.version.startswith("Zero Tolerance"):
            self.pal_mode.addItem("HUD L3 (0x20D2)", 0x20D2)
        self.pal_mode.addItem(tr("Из ROM (смещение)", "From ROM (offset)"), "rom")
        self.pal_mode.setCurrentIndex(0)
        self.pal_mode.blockSignals(False)
        self.info.setText(tr(f"<b>{version_label(rom.version)}</b><br>{len(banks)} шрифтов: глифы 8×8 4bpp.", f"<b>{version_label(rom.version)}</b><br>{len(banks)} fonts: 8×8 4bpp glyphs."))
        self.view.set_zoom(self.zoom.value())
        if self.font.count():
            self.font.setCurrentIndex(0); self._font_changed()
        else:
            self._render()

    def _font_changed(self, *_) -> None:
        data = self.font.currentData()
        if not data:
            return
        addr, nt, cols, comp, table, fontpal, fontpal_w = data
        self._compressed = comp
        self._table = table        # адрес таблицы глифов 8×16 (None = простой 8×8)
        self._fontpal = fontpal     # жёлтая палитра шрифта (заставка)
        self._fontpal_w = fontpal_w  # белая палитра шрифта (кредиты)
        for sp, val in ((self.start, addr), (self.count, nt), (self.cols, cols)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        self._render()

    def _palette(self):
        rom = self.main.rom
        if rom is None:
            return pal.grayscale_palette()
        data = self.pal_mode.currentData()
        if data == "font_y":                            # жёлтая палитра шрифта (заставка)
            fp = getattr(self, "_fontpal", None)
            return pal.read_palette(rom.data, fp) if fp is not None else pal.grayscale_palette()
        if data == "font_w":                            # белая палитра шрифта (кредиты)
            fp = getattr(self, "_fontpal_w", None)
            return pal.read_palette(rom.data, fp) if fp is not None else pal.grayscale_palette()
        if data == "gray":
            return pal.grayscale_palette()
        if data == "rom":
            return pal.read_palette(rom.data, self.pal_offset.value())
        if isinstance(data, int):
            return pal.read_palette(rom.data, data)
        return pal.grayscale_palette()

    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data
        addr = self.start.value(); nt = self.count.value(); cols = self.cols.value()
        src, base = d, addr
        if self._compressed:
            if getattr(self, "_decoded_at", None) != addr:
                self._decoded = tiles.decode_bg_bytepair(d, addr, max_bytes=0x20000)
                self._decoded_at = addr
            src, base = self._decoded, 0
        colors = self._palette()
        ti = 0 if self.transparent.isChecked() else None
        sn = len(src)
        table = getattr(self, "_table", None)
        if table is not None:
            # БОЛЬШОЙ шрифт 8×16: глиф = верх|низ тайл из таблицы (символ→long, рендерер sub_55354).
            # ВАЖНО: тайл-индексы в таблице VRAM-АБСОЛЮТНЫЕ, шрифт грузится в VRAM-тайл 1 (тайл 0 =
            # пустой) → gfx-тайл = idx − 1. Тайлы ≥ ntiles (мусорные символы) отсекаем. ASCII 0x20-0x7E.
            FB = 1                                # базовый сдвиг тайла (idx − 1)
            ngly = 95
            rows = (ngly + cols - 1) // cols
            out = bytearray(cols * 8 * rows * 16)
            for c in range(ngly):
                eo = table + c * 4
                if eo + 4 > len(d):
                    break
                e = (d[eo] << 24) | (d[eo + 1] << 16) | (d[eo + 2] << 8) | d[eo + 3]
                if e == 0:
                    continue
                top = ((e >> 16) & 0x7FF) - FB; bot = (e & 0x7FF) - FB
                tx = c % cols; ty = c // cols
                for half, tile in ((0, top), (1, bot)):
                    if tile < 0 or tile >= nt or base + tile * 32 + 32 > sn:
                        continue
                    tb = tiles.decode_sheet(src, base + tile * 32, 1, 1)[0]
                    for r in range(8):
                        row = (ty * 16 + half * 8 + r) * (cols * 8) + tx * 8
                        out[row:row + 8] = tb[r * 8:r * 8 + 8]
            return build_qimage(bytes(out), cols * 8, rows * 16, colors, ti)
        rows = (nt + cols - 1) // cols
        out = bytearray(cols * 8 * rows * 8)
        for t in range(nt):
            if base + t * 32 + 32 > sn:
                break
            tb = tiles.decode_sheet(src, base + t * 32, 1, 1)[0]
            tx = t % cols; ty = t // cols
            for r in range(8):
                row = (ty * 8 + r) * (cols * 8) + tx * 8
                out[row:row + 8] = tb[r * 8:r * 8 + 8]
        return build_qimage(bytes(out), cols * 8, rows * 8, colors, ti)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.font.currentText().split(" (")[0] or "font").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


class TextViewer(QWidget):
    """Вкладка «Текст»: внутриигровой ASCII-текст (названия уровней, биографии отряда,
    брифинги эпизодов, миссии, Game Over, кредиты). Блоки Text/*.bin, строки разделены нулями.
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        panel = QWidget(); panel.setFixedWidth(320)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Внутриигровой текст.", "In-game text."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.block = QComboBox()
        self.block.currentIndexChanged.connect(self._block_changed)
        form.addRow(tr("Блок:", "Block:"), self.block)
        self.start = _hex_spin(0xFFFFFF, 0x5D263)
        self.start.valueChanged.connect(self._render)
        form.addRow(tr("Адрес:", "Address:"), self.start)
        self.length = QSpinBox(); self.length.setRange(1, 0x8000); self.length.setValue(1175)
        self.length.valueChanged.connect(self._render)
        form.addRow(tr("Длина:", "Length:"), self.length)
        v.addLayout(form)

        self.export_btn = QPushButton(tr("Экспорт TXT…", "Export TXT…"))
        self.export_btn.clicked.connect(self._export)
        v.addWidget(self.export_btn)
        self.export_all_btn = QPushButton(tr("Экспорт всех блоков…", "Export all blocks…"))
        self.export_all_btn.clicked.connect(self._export_all)
        v.addWidget(self.export_all_btn)
        v.addStretch(1)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family: monospace; font-size: 13px;")

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.text, 1)

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        self.start.setMaximum(max(0, rom.size - 1))
        banks = list(_text_bank(rom.version))
        self.block.blockSignals(True); self.block.clear()
        named = []
        for name, addr, length in banks:
            self.block.addItem(name, (addr, length))
            named.append((addr, addr + length))
        # авто-скан: добавить ВЕСЬ остальной найденный текст (не пересекающийся с именованными)
        scanned = _scan_text_regions(rom.data)
        extra = 0
        for s, l in scanned:
            if any(s < ne and s + l > ns for ns, ne in named):
                continue                              # уже покрыт именованным блоком
            label = " ".join(_decode_game_text(rom.data, s, min(l, 48)).split())[:32] or tr("текст", "text")
            self.block.addItem(f"• {label} (0x{s:X})", (s, l))
            extra += 1
        self.block.blockSignals(False)
        self.info.setText(
            tr(f"<b>{version_label(rom.version)}</b><br>{len(banks)} именованных блоков + {extra} найдено сканом "
            "(ASCII-текст: уровни, био, брифинги, пикапы, меню, сообщения).", f"<b>{version_label(rom.version)}</b><br>{len(banks)} named blocks + {extra} found by scan "
            "(ASCII text: levels, bios, briefings, pickups, menus, messages)."))
        if self.block.count():
            self.block.setCurrentIndex(0); self._block_changed()
        else:
            self.text.setPlainText("")

    def _block_changed(self, *_) -> None:
        data = self.block.currentData()
        if not data:
            return
        addr, length = data
        for sp, val in ((self.start, addr), (self.length, length)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        self._render()

    def _render(self, *_) -> None:
        rom = self.main.rom
        if rom is None:
            self.text.setPlainText(""); return
        self.text.setPlainText(
            _decode_game_text(rom.data, self.start.value(), self.length.value()))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.block.currentText() or "text").replace(" ", "_").replace("/", "-")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить TXT", "Save TXT"), f"{base}_{nm}.txt", "Text (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.text.toPlainText())

    def _export_all(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить весь текст", "Save all text"), f"{base}_text.txt", "Text (*.txt)")
        if not path:
            return
        out = []
        for i in range(self.block.count()):           # все блоки из списка (имен. + скан)
            name = self.block.itemText(i); addr, length = self.block.itemData(i)
            out.append(f"===== {name} (0x{addr:X}) =====")
            out.append(_decode_game_text(rom.data, addr, length))
            out.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))


class WallViewer(QWidget):
    """Вкладка «Стены / двери»: метатекстуры стен/дверей эпизода (ZMAP). Каждая метатекстура =
    128×64, собрана из 8 текстур 32×32 (порядок 1357/2468). Двери = метатекстуры cell-типов 6/7.
    """

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self.view = TileView()
        self.view.set_zoom(2)
        self.view.hoverMoved.connect(self._hover)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Стены/двери (метатекстуры 128×64).", "Walls/doors (metatextures 128×64)."))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.ep = QComboBox()
        self.ep.currentIndexChanged.connect(self._ep_changed)
        form.addRow(tr("Эпизод:", "Episode:"), self.ep)
        self.mode = QComboBox()
        self.mode.addItems([tr("Метатекстуры", "Metatextures"), tr("Ячейки (как в игре)", "Cells (in-game)"), tr("Анимация стен", "Wall animation"),
                            tr("Сырые тайлы банка", "Raw bank tiles"), tr("Разрушаемые (целая→разрушенная)", "Destructibles (intact→destroyed)")])
        self.mode.currentIndexChanged.connect(self._render)
        form.addRow(tr("Режим:", "Mode:"), self.mode)
        self.zmap = _hex_spin(0xFFFFFF, 0x15A106); self.zmap.valueChanged.connect(self._render)
        form.addRow("ZMAP:", self.zmap)
        self.bank = _hex_spin(0xFFFFFF, 0x12EF26); self.bank.valueChanged.connect(self._render)
        form.addRow(tr("Банк текстур:", "Texture bank:"), self.bank)
        self.paloff = _hex_spin(0xFFFFFF, 0x20F2); self.paloff.valueChanged.connect(self._render)
        form.addRow(tr("Палитра:", "Palette:"), self.paloff)
        self.count = QSpinBox(); self.count.setRange(1, 256); self.count.setValue(256)
        self.count.valueChanged.connect(self._render)
        form.addRow(tr("Метатекстур:", "Metatextures:"), self.count)
        self.cols = QSpinBox(); self.cols.setRange(1, 16); self.cols.setValue(8)
        self.cols.valueChanged.connect(self._render)
        form.addRow(tr("В ряд:", "Per row:"), self.cols)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 6); self.zoom.setValue(2)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        self.mark_notwall = QCheckBox(tr("Отмечать не-стены красным", "Mark non-walls in red"))
        self.mark_notwall.setChecked(True)
        self.mark_notwall.stateChanged.connect(self._render)
        form.addRow("", self.mark_notwall)
        v.addLayout(form)

        self.hover_lbl = QLabel("—")
        self.hover_lbl.setWordWrap(True)
        self.hover_lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        v.addWidget(self.hover_lbl)
        self._grid: Optional[GridSpec] = None
        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def grid_state(self):
        if self.main.rom is None:
            return None
        img = self._compose()
        if img is None or self._grid is None:
            return None
        modes = {0: "meta", 1: "cells", 2: "anim", 3: "rawtiles", 4: "destruct"}
        tag = modes.get(self.mode.currentIndex(), "wall")
        base = (os.path.splitext(os.path.basename(self.main.rom.path))[0]
                + f"_wall_{tag}_{self.zmap.value():06X}")
        return img, self._grid, base

    def grid_cell_label(self, c, r):
        if self.mode.currentIndex() == 1:
            cells = getattr(self, "_cell_list", [])
            cid = cells[r] if r < len(cells) else r
            return f"cell{cid:02X}_{'NSEW'[c] if c < 4 else c}"
        if self.mode.currentIndex() == 2:
            anims = getattr(self, "_anims", [])
            meta = anims[r]["meta"] if r < len(anims) else r
            return f"anim_m{meta}_f{c}"
        if self.mode.currentIndex() == 3:
            tid = r * max(1, self.cols.value() * 4) + c
            notwall = "" if tid in getattr(self, "_raw_used", set()) else "_notwall"
            return f"tile{tid:02X}{notwall}"
        if self.mode.currentIndex() == 4:
            rows = getattr(self, "_destruct_rows", [])
            if r < len(rows):
                kind, ict, icid, im, dct, dcid, dm = rows[r]
                if c == 0:
                    return f"{kind}_ct{ict:02X}_intact_m{im}"
                if kind == "swap":
                    suf = f"m{dm}" if dm is not None else "empty"
                    return f"swap_ct{ict:02X}_destroyed_ct{dct:02X}_{suf}"
                return f"{kind}_ct{ict:02X}_passage_m{dm}"     # open / vanish
            return f"destruct_{r}_{c}"
        return f"meta{r * self.cols.value() + c:03d}"

    def set_rom(self, rom) -> None:
        if rom is None:
            return
        for sp in (self.zmap, self.bank, self.paloff):
            sp.setMaximum(max(0, rom.size - 1))
        banks = _wall_bank(rom.version)
        self.ep.blockSignals(True); self.ep.clear()
        for name, zmap, bank, pal in banks:
            self.ep.addItem(name, (zmap, bank, pal))
        self.ep.blockSignals(False)
        # двери/cell-типы из ZMAP
        self.info.setText(
            tr(f"<b>{version_label(rom.version)}</b><br>{len(banks)} эпизодов. Метатекстуры стен/дверей 128×64 "
            "(8 текстур 32×32). Двери — cell-типы 6/7.", f"<b>{version_label(rom.version)}</b><br>{len(banks)} episodes. Wall/door metatextures 128×64 "
            "(8 textures 32×32). Doors — cell types 6/7."))
        self.view.set_zoom(self.zoom.value())
        if self.ep.count():
            self.ep.setCurrentIndex(0); self._ep_changed()
        else:
            self._render()

    def _ep_changed(self, *_) -> None:
        data = self.ep.currentData()
        if not data:
            return
        zmap, bank, pal = data
        for sp, val in ((self.zmap, zmap), (self.bank, bank), (self.paloff, pal)):
            sp.blockSignals(True); sp.setValue(val); sp.blockSignals(False)
        self._render()

    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        if rom is None:
            return None
        d = rom.data
        zmap = self.zmap.value()
        if d[zmap:zmap + 4] != b"ZMAP":
            texdef = zmap                       # ручной: считаем что указали texdef
        else:
            texdef = zmap + 4
        bank = self.bank.value(); paloff = self.paloff.value()
        colors = pal.read_palette(d, paloff)
        if self.mode.currentIndex() == 1:
            return self._compose_cells(d, texdef, bank, colors)
        if self.mode.currentIndex() == 2:
            return self._compose_anim(d, texdef, bank, colors)
        if self.mode.currentIndex() == 3:
            return self._compose_raw(d, texdef, bank, colors)
        if self.mode.currentIndex() == 4:
            return self._compose_destruct(d, zmap, texdef, bank, colors)
        n = self.count.value(); cols = self.cols.value()
        rows = (n + cols - 1) // cols
        W = cols * 128; H = rows * 64
        out = bytearray(W * H)
        for t in range(n):
            mt = _build_metatexture(d, texdef, t, bank, colors)
            tx = (t % cols) * 128; ty = (t // cols) * 64
            for y in range(64):
                out[(ty + y) * W + tx:(ty + y) * W + tx + 128] = mt[y * 128:y * 128 + 128]
        self._grid = GridSpec(128, 64, cols, rows)
        return build_qimage(bytes(out), W, H, colors, None)

    def _compose_destruct(self, d, zmap, texdef, bank, colors):
        """Режим «Разрушаемые»: ОБЕ механики разрушения (вскрыты статикой по дизасму).
        (1) СЕКРЕТ-СТЕНЫ (ZT/ZTU, celltype 0x79/7b/7d/7f; хендлер ZT 0xa6bc / ZTU 0xa90f):
            выстрел меняет celltype клетки X→X+1 ПЕРМАНЕНТНО → разрушенная метатекстура.
        (2) РАЗРУШАЕМЫЕ (0x06/0x83 гориз, 0x07/0x84 верт; ZT b130/b168, ZTU b36e/b3a6): выстрел →
            обломки celltype 0x2d/0x2e + таймер → клетка ПУСТЕЕТ (исчезновение в проход). Если
            celltype обломков нет в таблице (ZTU) — клетка сразу пуста. Рисуем: ЦЕЛАЯ | итог."""
        if d[zmap:zmap + 4] != b"ZMAP":
            self.hover_lbl.setText(tr("Режим по ZMAP-сигнатуре — выбери эпизод в дропдауне.", "ZMAP-signature mode — pick an episode in the dropdown."))
            self._grid = GridSpec(128, 64, 1, 1)
            return build_qimage(bytes(128 * 64), 128, 64, colors, None)
        celltypes = d[zmap + 0x1804: zmap + 0x1804 + 0x100]    # cell-ID → celltype (256 б)
        texorder = texdef + 4096
        ct_cid = {}                                            # celltype → первый cell-ID (=remapTable)
        for c in range(256):
            ct_cid.setdefault(celltypes[c], c)

        def dirs_of(cid):
            return self._cell_dirs(d, texorder, cid) if cid is not None else None

        def repr_meta(dirs, is_door=False):
            """Представительная (видимая) метатекстура: дверь с пустым texorder → DOOR_METATEX,
            иначе первая ненулевая грань (боковая стена), иначе #0 (пол/проход)."""
            if dirs is None:
                return None
            if is_door and not any(dirs):
                return DOOR_METATEX
            for v in dirs:
                if v:
                    return v
            return 0

        rows = []                                  # (kind, ict, icid, im, dct, dcid, dm)
        # (1) секрет-стены 0x79/7b/7d/7f. Хендлер пишет grid=remapTable[celltype+1].
        #     • Если разрушенный celltype ЕСТЬ в уровне (ZT/ZTU) → СМЕНА ТЕКСТУРЫ (kind=swap).
        #       Показываем грань, что МЕНЯЕТСЯ (у ZTU боковые S/E, не фронт N).
        #     • Если ОТСУТСТВУЕТ (BZT все уровни, ZTU 0x7F) → remapTable=пусто → клетка → 0 = стена
        #       ИСЧЕЗАЕТ МГНОВЕННО в проход, БЕЗ разрушенной текстуры (kind=open). В BZT за ней
        #       бывают враги — проксимити-спавн. (July доп. ставит флаг render-state + счётчик, НЕ анимацию.)
        for ict, dct in ((0x79, 0x7A), (0x7B, 0x7C), (0x7D, 0x7E), (0x7F, 0x80)):
            icid = ct_cid.get(ict)
            if icid is None:
                continue
            dcid = ct_cid.get(dct)
            idirs = dirs_of(icid); ddirs = dirs_of(dcid)
            if ddirs is not None:
                k = next((j for j in range(4) if idirs[j] != ddirs[j]), 0)
                rows.append(("swap", ict, icid, idirs[k], dct, dcid, ddirs[k]))
            else:
                rows.append(("open", ict, icid, repr_meta(idirs), dct, None, 0))
        # (2) разрушаемые — исчезновение в проход (обломки 0x2d гориз / 0x2e верт → пусто)
        for ict, dct in ((0x06, 0x2D), (0x83, 0x2D), (0x07, 0x2E), (0x84, 0x2E)):
            icid = ct_cid.get(ict)
            if icid is None:
                continue
            is_door = ict in (0x06, 0x07)
            im = repr_meta(dirs_of(icid), is_door)
            dcid = ct_cid.get(dct)                 # débris-клетка (в ZTU отсутствует → пусто)
            ddirs = dirs_of(dcid)
            dm = repr_meta(ddirs) if ddirs is not None else 0   # обломки/пол (проход)
            rows.append(("vanish", ict, icid, im, dct, dcid, dm))
        self._destruct_rows = rows
        if not rows:
            self.hover_lbl.setText(tr("Разрушаемых/секрет-стен (celltype 0x06/07/83/84, 0x79/7b/7d/7f) "
                                   "в этом эпизоде нет.", "No destructible/secret walls (celltype 0x06/07/83/84, 0x79/7b/7d/7f) in this episode."))
            self._grid = GridSpec(128, 64, 1, 1)
            return build_qimage(bytes(128 * 64), 128, 64, colors, None)
        W = 2 * 128; rowH = 64; H = len(rows) * rowH
        out = bytearray(W * H)
        for r, (kind, ict, icid, im, dct, dcid, dm) in enumerate(rows):
            for col, meta in ((0, im), (1, dm)):
                if meta is None:
                    continue
                mt = _build_metatexture(d, texdef, meta, bank, colors)
                tx = col * 128; ty = r * rowH
                for y in range(64):
                    out[(ty + y) * W + tx:(ty + y) * W + tx + 128] = mt[y * 128:y * 128 + 128]
        self._grid = GridSpec(128, 64, 2, len(rows))
        n_swap = sum(1 for x in rows if x[0] == "swap")
        n_open = sum(1 for x in rows if x[0] == "open")
        n_van = sum(1 for x in rows if x[0] == "vanish")
        parts = []
        if n_swap: parts.append(tr(f"{n_swap} со сменой текстуры", f"{n_swap} with texture swap"))
        if n_open: parts.append(tr(f"{n_open} исчезают мгновенно", f"{n_open} open instantly"))
        if n_van: parts.append(tr(f"{n_van} исчезают", f"{n_van} vanish"))
        self.hover_lbl.setText(
            tr("Разрушаемых стен: ", "Destructible walls: ") + " + ".join(parts) + tr(". "
            "ЛЕВО = целая, ПРАВО = итог выстрела (наведи для деталей).", ". LEFT = intact, RIGHT = shot result (hover for details)."))
        return build_qimage(bytes(out), W, H, colors, None)

    def _compose_raw(self, d, texdef, bank, colors):
        """Режим «Сырые тайлы банка»: все 32×32 тайлы банка подряд. Красная рамка = тайл
        НЕ входит ни в одну СТЕНОВУЮ метатекстуру (texdef). ВНИМАНИЕ: это лишь «не стена» —
        тайл может задействоваться системой ОБЪЕКТОВ/декора (отдельный разбор), не «вырезан»."""
        from . import tiles as _t
        used = set()
        for m in range(256):
            for i in range(8):
                o = texdef + m * 16 + i * 2
                used.add((d[o] << 8) | d[o + 1])
        n = self.count.value()
        cols = max(1, self.cols.value() * 4)        # тайлы вчетверо мельче метатекстур
        TS = 32
        avail = (len(d) - bank) // 512
        n = max(0, min(n, avail))
        rows = max(1, (n + cols - 1) // cols)
        W = cols * TS; H = rows * TS
        out = bytearray(W * H)
        for t in range(n):
            tb, _, _ = _t.decode_zt_sheet(d, bank + t * 512, 1, 1)
            tx = (t % cols) * TS; ty = (t // cols) * TS
            for y in range(TS):
                out[(ty + y) * W + tx:(ty + y) * W + tx + TS] = tb[y * TS:y * TS + TS]
        self._grid = GridSpec(TS, TS, cols, rows)
        self._raw_used = used
        img = build_qimage(bytes(out), W, H, colors, None)
        if self.mark_notwall.isChecked():       # галочка: рамка вокруг не-стеновых тайлов
            img = img.convertToFormat(QImage.Format.Format_ARGB32)
            p = QPainter(img); p.setPen(QColor(255, 60, 60))
            for t in range(n):
                if t not in used:
                    p.drawRect((t % cols) * TS, (t // cols) * TS, TS - 1, TS - 1)
            p.end()
        return img

    def _wall_cells(self):
        """Список cell-ID со стеновой графикой (стена/угол/дверь/лестн./разруш.), по типу из ini."""
        rom = self.main.rom
        ver = rom.version
        out = []
        for cid in range(256):
            typ, _name = _cell_def(ver, cid)
            if typ in _WALL_CELL_TYPES:
                out.append(cid)
        return out

    def _cell_dirs(self, d, texorder, cid):
        """4 индекса метатекстур [N,S,E,W] для cell (texorder = 4×16-бит BE)."""
        o = texorder + cid * 8
        return [(d[o + k * 2] << 8) | d[o + k * 2 + 1] for k in range(4)]

    def _compose_cells(self, d, texdef, bank, colors):
        """Режим «Ячейки»: каждая стеновая cell = 4 направленные метатекстуры [N,S,E,W] в ряд."""
        texorder = texdef + 4096
        cells = self._wall_cells()
        self._cell_list = cells
        DIRW = 4 * 128                       # 4 направления × 128
        rowH = 64
        W = DIRW; H = len(cells) * rowH
        out = bytearray(W * H)
        for r, cid in enumerate(cells):
            dirs = self._cell_dirs(d, texorder, cid)
            for di, mt in enumerate(dirs):
                tile = _build_metatexture(d, texdef, mt, bank, colors)
                tx = di * 128; ty = r * rowH
                for y in range(64):
                    out[(ty + y) * W + tx:(ty + y) * W + tx + 128] = tile[y * 128:y * 128 + 128]
        self._grid = GridSpec(128, 64, 4, len(cells))
        return build_qimage(bytes(out), W, H, colors, None)

    def _compose_anim(self, d, texdef, bank, colors):
        """Режим «Анимация»: каждая анимация (из таблицы ZMAP, по дизасму) — ряд кадров.

        Каждый кадр — полная метатекстура 128×64 с подстановками тайлов этого кадра поверх
        базовой. Так видно реальную игровую анимацию (мигающий экран, движущийся глаз и т.п.).
        """
        rom = self.main.rom
        zmap = self.zmap.value()
        anims = parse_wall_anims(d, zmap, rom.version) if d[zmap:zmap + 4] == b"ZMAP" else []
        self._anims = anims
        if not anims:
            self.hover_lbl.setText(tr("Анимаций не найдено (таблица ZMAP пуста или ZMAP задан вручную).", "No animations found (the ZMAP table is empty or ZMAP was set manually)."))
            self._grid = GridSpec(128, 64, 1, 1)
            return build_qimage(bytes(128 * 64), 128, 64, colors, None)
        maxf = max(len(a["frames"]) for a in anims)
        MW, MH = 128, 64
        gap = 4
        W = maxf * (MW + gap)
        H = len(anims) * (MH + gap)
        out = bytearray(W * H)
        for r, a in enumerate(anims):
            meta = a["meta"]
            for c, subs in enumerate(a["frames"]):
                mt = _build_metatexture(d, texdef, meta, bank, colors, subs)
                tx = c * (MW + gap); ty = r * (MH + gap)
                for y in range(MH):
                    out[(ty + y) * W + tx:(ty + y) * W + tx + MW] = mt[y * MW:y * MW + MW]
        self._grid = GridSpec(MW, MH, maxf, len(anims), MW + gap, MH + gap)
        return build_qimage(bytes(out), W, H, colors, None)

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))

    def _hover(self, x: float, y: float) -> None:
        rom = self.main.rom
        if rom is None:
            return
        d = rom.data
        zmap = self.zmap.value()
        texdef = zmap + 4 if d[zmap:zmap + 4] == b"ZMAP" else zmap
        if self.mode.currentIndex() == 1:               # режим «Ячейки»
            cells = getattr(self, "_cell_list", [])
            r = int(y) // 64
            if 0 <= r < len(cells):
                cid = cells[r]; typ, name = _cell_def(rom.version, cid)
                dirs = self._cell_dirs(d, texdef + 4096, cid)
                self.hover_lbl.setText(
                    tr(f"Ячейка #{cid} (0x{cid:02X}) — {name}\nтип {typ}; метатекстуры N/S/E/W: {dirs}", f"Cell #{cid} (0x{cid:02X}) — {name}\ntype {typ}; metatextures N/S/E/W: {dirs}"))
            return
        if self.mode.currentIndex() == 2:               # режим «Анимация»
            anims = getattr(self, "_anims", [])
            r = int(y) // (64 + 4)
            if 0 <= r < len(anims):
                a = anims[r]
                self.hover_lbl.setText(
                    tr(f"Анимация метатекстуры #{a['meta']} (0x{a['meta']:02X})\n"
                    f"кадров: {len(a['frames'])}; период: {a['period']}; таблица @0x{a['addr']:X}", f"Metatexture #{a['meta']} (0x{a['meta']:02X}) animation\n"
                    f"frames: {len(a['frames'])}; period: {a['period']}; table @0x{a['addr']:X}"))
            return
        if self.mode.currentIndex() == 4:               # режим «Разрушаемые»
            rows = getattr(self, "_destruct_rows", [])
            r = int(y) // 64; col = int(x) // 128
            if 0 <= r < len(rows):
                kind, ict, icid, im, dct, dcid, dm = rows[r]
                if col == 0:
                    typ = {"swap": tr("секрет-стена (смена текстуры)", "secret wall (texture swap)"),
                           "open": tr("секрет-стена (исчезает мгновенно в проход)", "secret wall (vanishes instantly into a passage)"),
                           "vanish": tr("разрушаемая (исчезает в проход)", "destructible (vanishes into a passage)")}[kind]
                    self.hover_lbl.setText(tr(f"ЦЕЛАЯ {typ}: celltype 0x{ict:02X} "
                                           f"(cell 0x{icid:02X}), метатекстура #{im}", f"WHOLE {typ}: celltype 0x{ict:02X} "
                                           f"(cell 0x{icid:02X}), metatexture #{im}"))
                elif kind == "swap":
                    dc = f"0x{dcid:02X}" if dcid is not None else "?"
                    self.hover_lbl.setText(tr(f"РАЗРУШЕННАЯ: выстрел → celltype 0x{dct:02X} "
                                           f"(cell {dc}), метатекстура #{dm} — перманентно", f"DESTROYED: shot → celltype 0x{dct:02X} "
                                           f"(cell {dc}), metatexture #{dm} — permanent"))
                elif kind == "open":
                    self.hover_lbl.setText(tr(f"ИСЧЕЗАЕТ МГНОВЕННО: разрушенный celltype 0x{dct:02X} не "
                                           "задан в уровне → клетка → пусто (проход), БЕЗ разрушенной "
                                           "текстуры. BZT: за такой стеной бывают враги (проксимити-спавн).", f"VANISHES INSTANTLY: destroyed celltype 0x{dct:02X} is not "
                                           "defined in the level → cell → empty (passable), WITHOUT a destroyed "
                                           "texture. BZT: enemies can be behind such a wall (proximity spawn)."))
                else:
                    extra = (tr(f"обломки celltype 0x{dct:02X} (cell 0x{dcid:02X})", f"debris celltype 0x{dct:02X} (cell 0x{dcid:02X})")
                             if dcid is not None else tr("клетка пустеет (обломков нет в таблице)", "cell becomes empty (no debris in the table)"))
                    self.hover_lbl.setText(tr(f"ИСЧЕЗАЕТ: выстрел → {extra} → пусто (проход). "
                                           f"Показан пол/проход (метатекстура #{dm})", f"VANISHES: shot → {extra} → empty (passable). "
                                           f"Floor/passage shown (metatexture #{dm})"))
            return
        cols = self.cols.value()
        mx = int(x) // 128; my = int(y) // 64
        idx = my * cols + mx
        if 0 <= idx < self.count.value():
            ids = [(d[texdef + idx * 16 + i * 2] << 8) | d[texdef + idx * 16 + i * 2 + 1]
                   for i in range(8)]
            self.hover_lbl.setText(tr(f"Метатекстура #{idx} (0x{idx:02X})\nтайлы: {ids}", f"Metatexture #{idx} (0x{idx:02X})\ntiles: {ids}"))

    def _export(self) -> None:
        rom = self.main.rom
        if rom is None:
            return
        img = self._compose()
        if img is None:
            return
        base = os.path.splitext(os.path.basename(rom.path))[0]
        nm = (self.ep.currentText() or "walls").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), f"{base}_walls_{nm}.png", "PNG (*.png)")
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(path, "PNG"):
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))


def _cell_meta_dirs(data, texorder, cid):
    """4 индекса метатекстур [N,S,E,W] для ячейки cid (texorder = 256×4×16-бит BE)."""
    o = texorder + cid * 8
    return [(data[o + k * 2] << 8) | data[o + k * 2 + 1] for k in range(4)]


class CellAssetDialog(QDialog):
    """Визуальные ассеты ячейки карты (по коду игры): текстуры стены 4 направления +
    анимация, спрайты дверей/врагов/объектов/предметов. Открывается двойным кликом."""

    DIRS = [tr("Север (N)", "North (N)"), tr("Юг (S)", "South (S)"), tr("Восток (E)", "East (E)"), tr("Запад (W)", "West (W)")]

    def __init__(self, parent, rom, ep, cid, celltypes, spawn_enemy_ct=None):
        super().__init__(parent)
        self.rom = rom
        self.ep = ep
        self.cid = cid
        self.celltypes = celltypes    # таблица cell-ID→celltype эпизода (или None)
        self.spawn_enemy_ct = spawn_enemy_ct  # враг спавнер-стены (celltype соседнего маркера)
        self._anim_labels = []        # (QLabel, [pixmaps], idx) для проигрывания
        ver = rom.version
        ct = celltypes[cid] if celltypes is not None else cid
        self.setWindowTitle(tr(f"Ассеты ячейки 0x{cid:02X} (celltype 0x{ct:02X})", f"Cell 0x{cid:02X} assets (celltype 0x{ct:02X})"))
        self.resize(580, 640)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); v = QVBoxLayout(inner)

        if ver.startswith("Zero Tolerance"):
            name = _zt_cell_name(ver, ct)
            icon = MAP_TYPE_TO_ICON.get(CELL_DEFS_ZT.get(ct, (1, ''))[0], 1)
        else:
            name = _celltype_name(ct)
            icon = _celltype_icon(ct)
        hdr = QLabel(tr(f"<b>Ячейка 0x{cid:02X}</b> · celltype 0x{ct:02X} · {name}", f"<b>Cell 0x{cid:02X}</b> · celltype 0x{ct:02X} · {name}"))
        hdr.setWordWrap(True)
        hdr.setStyleSheet("font-size: 12px;")
        v.addWidget(hdr)

        try:
            self._add_assets(v, ver, ep, cid, ct, icon)
        except Exception as e:
            v.addWidget(QLabel(tr(f"ошибка рендера ассетов: {e}", f"asset render error: {e}")))
        v.addStretch(1)
        scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll)

        # проигрывание анимаций (если есть)
        if self._anim_labels:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(180)

    def _tick(self):
        for i, (lbl, pms) in enumerate(self._anim_labels):
            self._anim_labels[i] = (lbl, pms)
            cur = getattr(lbl, "_ai", 0)
            cur = (cur + 1) % len(pms)
            lbl._ai = cur
            lbl.setPixmap(pms[cur])

    # ---------- диспетч по категории ----------
    def _add_assets(self, v, ver, ep, cid, ct, icon):
        is_zt = ver.startswith("Zero Tolerance")
        if icon == 9:                                  # враг
            self._add_sprite(v, tr("Спрайт врага", "Enemy sprite"), ver, ep, ct, "enemy")
        elif is_zt and ct in ZT_CELLTYPE_OBJSPRITE:    # объект-спрайт (сгор. остатки)
            name, a1 = ZT_CELLTYPE_OBJSPRITE[ct]
            d = self.rom.data
            colors = pal.read_palette(d, self._sprite_palette(ver))
            self._render_enemy_sprite(v, d, name, a1, spritemod.sprite_format(ver), colors)
        elif ct in CELLTYPE_TO_ITEM_OFFSET:            # предмет/оружие/декор (объект-банк)
            self._add_sprite(v, tr("Спрайт объекта", "Object sprite"), ver, ep, ct, "item")
        elif icon in (1, 2, 3, 4, 5, 6, 7, 12, 14):    # стена/угол/дверь/спец/разруш.
            self._add_walls(v, ver, ep, cid, ct)
        else:
            v.addWidget(QLabel(tr("(служебная ячейка — старт/лифт/блокер/снайпер-маркер/"
                               "окружение, визуальных ассетов нет)", "(service cell — start/elevator/blocker/sniper-marker/environment, no visual assets)")))

    # ---------- стены: 4 направления + анимация ----------
    def _add_walls(self, v, ver, ep, cid, ct):
        banks = _wall_bank(ver)
        if not banks or ep - 1 >= len(banks):
            v.addWidget(QLabel(tr("банк стен для эпизода не задан", "wall bank for the episode is not set"))); return
        _nm, sig, bank, paloff = banks[ep - 1]
        d = self.rom.data
        if d[sig:sig + 4] != b"ZMAP":
            v.addWidget(QLabel(tr(f"ZMAP не найден @0x{sig:X}", f"ZMAP not found @0x{sig:X}"))); return
        texdef = sig + 4
        texorder = texdef + 4096
        colors = pal.read_palette(d, paloff)
        dirs = _cell_meta_dirs(d, texorder, cid)
        is_door = ct in (0x06, 0x07)
        if is_door and not any(dirs):
            # Дверь: в ранних прото-уровнях (June ep1-3, July) texorder двери НЕ заполнен
            # ([0,0,0,0] → пустая стена). Движок рисует дверь канонической метатекстурой
            # DOOR_METATEX (18): в ZT и June ep4/5 texorder двери явно = [18,18,18,18].
            dirs = [DOOR_METATEX] * 4
            v.addWidget(QLabel(
                tr(f"<i>Дверь {'горизонтальная' if ct == 0x06 else 'вертикальная'}: texorder "
                f"в этом уровне не заполнен — показана канонич. дверная метатекстура "
                f"#{DOOR_METATEX} (как в финальных уровнях ZT/June ep4-5).</i>", f"<i>{'Horizontal' if ct == 0x06 else 'Vertical'} door: texorder "
                f"in this level is not filled — showing the canonical door metatexture "
                f"#{DOOR_METATEX} (as in the final ZT/June ep4-5 levels).</i>")))
        anims = parse_wall_anims(d, sig, ver)
        anim_by_meta = {a["meta"]: a for a in anims}

        v.addWidget(QLabel(tr("<b>Текстуры стены — 4 направления:</b>", "<b>Wall textures — 4 directions:</b>")))
        self._render_dirs(v, d, texdef, bank, colors, dirs, anim_by_meta)

        # ── BZT СПАВНЕР-СТЕНА (cell-ID 0xE4-0xFF, celltype 0x01): видимая стена с НИШЕЙ, где
        #    изображён враг (метатекстуры 249-255). Выстрел → анимация материализации (враг
        #    проявляется в нише) + спавн врага ПЕРЕД стеной; стена НЕ исчезает. Каждый уровень
        #    использует свой тип ниши (=своего врага) в 4 ориентациях (cell-ID группами по 4).
        if (not ver.startswith("Zero Tolerance") and 0xE4 <= cid <= 0xFF
                and any(249 <= m <= 255 for m in dirs)):
            niche = next(m for m in dirs if 249 <= m <= 255)
            line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color:#a44;"); v.addWidget(line)
            note = QLabel(
                tr(f"💥 <b>Спавнер-стена (BZT):</b> видимая стена с <b>нишей</b> (метатекстура "
                f"#{niche}, выше), в которой изображён ВРАГ. При <b>выстреле</b> в неё враг "
                "<b>материализуется ПЕРЕД стеной</b>; стена НЕ исчезает. Спавн — через невидимый "
                "маркер рядом (пуля сканирует 11×11). cell-ID "
                f"0x{cid:02X} — одна из 4 ориентаций (N/S/E/W) этого типа стены.", f"💥 <b>Spawner wall (BZT):</b> a visible wall with a <b>niche</b> (metatexture "
                f"#{niche}, above) depicting an ENEMY. When <b>shot</b>, the enemy "
                "<b>materializes IN FRONT OF the wall</b>; the wall does NOT vanish. Spawn is via an invisible "
                "marker nearby (the bullet scans 11×11). cell-ID "
                f"0x{cid:02X} — one of the 4 orientations (N/S/E/W) of this wall type."))
            note.setWordWrap(True); v.addWidget(note)
            # АНИМАЦИЯ МАТЕРИАЛИЗАЦИИ — кадры В ДАННЫХ: метатекстуры стадий проявления врага
            # в нише + пустая ниша (#255). Группы: 249-251 / 252-254 (по 3 кадра на врага).
            base = 252 if niche >= 252 else 249
            frame_metas = [base, base + 1, base + 2, 255]
            pms = []
            for fm in frame_metas:
                mt2 = _build_metatexture(d, texdef, fm, bank, colors)
                pms.append(QPixmap.fromImage(build_qimage(mt2, 128, 64, colors, None)).scaled(
                    384, 192, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation))
            v.addWidget(QLabel(tr(f"<b>Анимация материализации</b> (кадры-метатекстуры "
                               f"{frame_metas[:3]} → пусто #255):", f"<b>Materialization animation</b> (metatexture frames "
                               f"{frame_metas[:3]} → empty #255):")))
            play = QLabel(); play._ai = 0; play.setPixmap(pms[0])
            play.setStyleSheet("background:#111;"); v.addWidget(play)
            self._anim_labels.append((play, pms))
            # враг, который материализуется (= соседний маркер) + его анимация
            if self.spawn_enemy_ct is not None:
                enm = _celltype_name(self.spawn_enemy_ct)
                v.addWidget(QLabel(tr(f"<b>🟢 Спавнит:</b> celltype 0x{self.spawn_enemy_ct:02X} — "
                                   f"{enm}. Анимация материализации = спрайт врага (ниже):", f"<b>🟢 Spawns:</b> celltype 0x{self.spawn_enemy_ct:02X} — "
                                   f"{enm}. Materialization animation = enemy sprite (below):")))
                try:
                    self._add_enemy(v, ver, self.spawn_enemy_ct)
                except Exception as e:
                    v.addWidget(QLabel(tr(f"(спрайт врага не отрисован: {e})", f"(enemy sprite not rendered: {e})")))
            else:
                v.addWidget(QLabel(tr("<i>рядом не найден enemy-маркер (враг определяется по "
                                   "ближайшему маркеру в радиусе выстрела 11×11)</i>", "<i>no enemy marker found nearby (the enemy is determined by the nearest marker within the 11×11 shot radius)</i>")))

        # ── Разрушаемая (секрет-)стена ZT/ZTU: выстрел → celltype+1 ПЕРМАНЕНТНО
        # (хендлер 0xa6bc). Заменяющая ячейка = remapTable[celltype+1] = первый
        # cell-ID этого celltype в таблице уровня. В эп.3 разрушенная метатекстура
        # бывает АНИМИРОВАННОЙ — анимация подтянется тем же anim_by_meta.
        DESTR_SECRET = {0x79: 0x7A, 0x7B: 0x7C, 0x7D: 0x7E, 0x7F: 0x80}
        if ct in DESTR_SECRET and self.celltypes is not None:
            dct = DESTR_SECRET[ct]
            dcid = next((c for c in range(256) if self.celltypes[c] == dct), None)
            line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color:#a44;"); v.addWidget(line)
            if dcid is None:
                v.addWidget(QLabel(
                    tr(f"<b>💥 Разрушаемая секрет-стена</b> (celltype 0x{ct:02X} → 0x{dct:02X}): "
                    "разрушенный celltype в таблице уровня не встречается.", f"<b>💥 Destructible secret wall</b> (celltype 0x{ct:02X} → 0x{dct:02X}): "
                    "destroyed celltype does not occur in the level table.")))
            else:
                dname = _zt_cell_name(ver, dct)
                if not dname or dname == "Unknown" or dname == "Wall":
                    dname = tr(f"разрушенный вид «{_zt_cell_name(ver, ct)}»", f"destroyed view «{_zt_cell_name(ver, ct)}»")
                v.addWidget(QLabel(
                    tr(f"<b>💥 При выстреле → разрушенная ячейка</b> (перманентно):<br>"
                    f"celltype <b>0x{ct:02X} → 0x{dct:02X}</b> · заменяющая ячейка "
                    f"<b>0x{dcid:02X}</b> · {dname}", f"<b>💥 On shot → destroyed cell</b> (permanent):<br>"
                    f"celltype <b>0x{ct:02X} → 0x{dct:02X}</b> · replacement cell "
                    f"<b>0x{dcid:02X}</b> · {dname}")))
                ddirs = _cell_meta_dirs(d, texorder, dcid)
                self._render_dirs(v, d, texdef, bank, colors, ddirs, anim_by_meta)
        elif ct in (0x06, 0x07, 0x83, 0x84):
            v.addWidget(QLabel(
                tr("<i>💥 Разрушаемая стена/дверь: при выстреле разрушается в проход "
                "(целл-тип → debris → исчезает; отдельной разрушенной текстуры нет).</i>", "<i>💥 Destructible wall/door: when shot it breaks open into a passage (celltype → debris → disappears; there is no separate destroyed texture).</i>")))

    def _render_dirs(self, v, d, texdef, bank, colors, dirs, anim_by_meta):
        """Рендер 4 направленных метатекстур [N,S,E,W] + их анимаций (если есть)."""
        for label, meta in zip(self.DIRS, dirs):
            row = QHBoxLayout()
            mt = _build_metatexture(d, texdef, meta, bank, colors)
            img = build_qimage(mt, 128, 64, colors, None)
            pic = QLabel()
            pic.setPixmap(QPixmap.fromImage(img).scaled(
                256, 128, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))
            pic.setStyleSheet("background:#111;")
            row.addWidget(pic)
            tag = tr("  🎬 анимируется", "  🎬 animated") if meta in anim_by_meta else ""
            row.addWidget(QLabel(tr(f"{label}\nметатекстура #{meta}{tag}", f"{label}\nmetatexture #{meta}{tag}")), 1)
            v.addLayout(row)
        shown = set()
        for meta in dirs:
            if meta in anim_by_meta and meta not in shown:
                shown.add(meta)
                self._add_anim(v, d, texdef, bank, colors, anim_by_meta[meta])

    def _add_anim(self, v, d, texdef, bank, colors, a):
        frames = a["frames"]
        v.addWidget(QLabel(tr(f"<b>Анимация метатекстуры #{a['meta']} "
                           f"(кадров {len(frames)}, период {a['period']}):</b>", f"<b>Metatexture #{a['meta']} animation "
                           f"(frames {len(frames)}, period {a['period']}):</b>")))
        pms = []
        for subs in frames:
            mt = _build_metatexture(d, texdef, a["meta"], bank, colors, subs)
            img = build_qimage(mt, 128, 64, colors, None)
            pms.append(QPixmap.fromImage(img).scaled(
                192, 96, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))
        play = QLabel()
        play._ai = 0
        play.setPixmap(pms[0])
        play.setStyleSheet("background:#111;")
        v.addWidget(play)
        self._anim_labels.append((play, pms))

    # ---------- спрайты (враги/предметы) ----------
    def _add_sprite(self, v, title, ver, ep, ct, kind):
        if kind == "enemy":
            self._add_enemy(v, ver, ct)
        else:
            self._add_item(v, ver, ep, ct)

    def _sprite_palette(self, ver):
        """Линия палитры спрайтов/объектов/ВРАГОВ эпизода = ЛИНИЯ 0 палитры зоны.
        Билборды врагов и предметов блитятся в тайл-буфер 3D-сцены, который использует
        линию 0 (ту же, что текстуры стен) — НЕ отдельную линию +0x60. Проверено
        визуально на обоих BZT: medkit 0x25 = красный крест на белом ящике, robot 0x29 =
        красный, robot 0x2A = фиолетовый (на +0x60 всё уходило в синь/зелень — баг).
        ZT тоже на линии 0 (0x20F2). ZTU: подтверждённый юзером 0x21B4."""
        if "Underground" in ver:
            return ZTU_SPRITE_PALETTE
        banks = _wall_bank(ver)
        return banks[self.ep - 1][3] if banks and self.ep - 1 < len(banks) else 0x20F2

    def _enemy_resolver(self):
        """Кэш авто-резолвера врагов (ztextractor.spriteres) для текущего ROM."""
        if getattr(self, "_enemyres_rom", None) is not self.rom:
            self._enemyres = spriteres.EnemyResolver(self.rom.data)
            self._enemyres_rom = self.rom
        return self._enemyres

    def _add_enemy(self, v, ver, ct):
        d = self.rom.data
        # ZT: свой бестиарий (имя, графика, вариант2); BZT: тип→ENEMY_GFX.
        if ver.startswith("Zero Tolerance"):
            info = ZT_CELLTYPE_ENEMY.get(ct)
            if "Underground" in ver:
                info = ZTU_CELLTYPE_ENEMY.get(ct)           # ZTU: свои адреса спрайтов
            elif ver.startswith("Zero Tolerance (нем"):
                info = ZT_DE_ENEMY_OVERRIDE.get(ct, info)   # люди→Alien в нем. версии
            if not info:
                v.addWidget(QLabel(tr(f"враг ZT: celltype 0x{ct:02X} не сопоставлен", f"ZT enemy: celltype 0x{ct:02X} not mapped")))
                return
            name, a1, variant = info
            targets = [(name, _de(ver, a1))]
            if variant:
                targets.append((name + tr(" — вариация 2", " — variation 2"), _de(ver, variant)))
        else:
            # BZT — АВТО-резолвер objdef+0x18 (точный спрайт ячейки; см. spriteres):
            # враги И спец-актёры (макеты 0x08/0x09, белый инопланетянин 0x27).
            res = self._enemy_resolver()
            variants = res.enemy_variants(ct)
            if not variants:                                 # запас: class-цепочка
                hit = res.enemy_gfx(ct)
                if hit:
                    variants = [hit[2]]
            declared = getattr(res, "declared", set())
            broken = False
            if not variants and ct in (0x08, 0x09):
                # МАКЕТЫ 0x08/0x09: реальный спрайт назначает upfront-спавн ПО ЭПИЗОДУ =
                # objdef[эпизод−1] (выверено юзером: June ep3 0x09 = 0x1C73A2, красный
                # человекоподобный). Если макет ОБЪЯВЛЕН в objdef, но его gfx вне ROM
                # (June 0x08 → 0x201AD6 за 2МБ), в игре указатель ведёт в мусор → спрайт
                # ЛОМАЕТСЯ/мелькает (НЕ чистая невидимость); показываем спрайт-ориентир эпизода.
                gfx = res.episode_sprite(self.ep - 1)
                if gfx:
                    variants = [gfx]
                    broken = ct in declared
            if not variants:
                v.addWidget(QLabel(tr(f"<b>{_celltype_name(ct)}</b><br>"
                                   "Спрайт в этом эпизоде не определён.", f"<b>{_celltype_name(ct)}</b><br>"
                                   "Sprite not defined in this episode.")))
                return
            if broken:
                v.addWidget(QLabel(
                    tr("<i>⚠ В игре указатель спрайта этого макета ведёт за пределы ROM билда "
                    "→ спрайт ломается/мелькает (на вкладке просмотра врагов он виден "
                    "корректно). Ниже — спрайт-ориентир эпизода.</i>", "<i>⚠ In game the sprite pointer of this layout leads outside the build's ROM → the sprite breaks/flickers (in the enemy viewer tab it shows correctly). Below is the episode's reference sprite.</i>")))
            base_nm = _celltype_name(ct)             # _render_enemy_sprite сам добавит «Враг: »
            if base_nm.startswith(tr("Враг: ", "Enemy: ")):
                base_nm = base_nm[6:]
            elif base_nm.startswith(tr("Враг-", "Enemy-")):
                base_nm = base_nm[5:]
                base_nm = base_nm[:1].upper() + base_nm[1:]
            targets = []
            for i, gfx in enumerate(variants):
                lbl = base_nm + (tr(f" — вариация {i + 1}", f" — variation {i + 1}") if len(variants) > 1 else "")
                targets.append((lbl, gfx))
        fmt = spritemod.sprite_format(ver)
        colors = pal.read_palette(d, self._sprite_palette(ver))
        for name, a1 in targets:
            if not a1 or not spritemod._valid_header(d, a1):
                v.addWidget(QLabel(tr(f"враг {name}: графика @0x{a1:X} не распознана", f"enemy {name}: graphics @0x{a1:X} not recognized")
                                   if a1 else tr(f"враг {name}: адрес не задан", f"enemy {name}: address not set")))
                continue
            self._render_enemy_sprite(v, d, name, a1, fmt, colors)

    def _render_enemy_sprite(self, v, d, name, a1, fmt, colors):
        tree = spritemod.parse_sprite(d, a1, fmt)
        base = tree["base"]
        ti = 0
        v.addWidget(QLabel(tr(f"<b>Враг: {name}</b> "
                           f"(графика 0x{a1:X}, анимаций {len(tree['anims'])})", f"<b>Enemy: {name}</b> "
                           f"(graphics 0x{a1:X}, animations {len(tree['anims'])})")))
        v.addWidget(QLabel(tr("Ракурсы (1-я анимация):", "Angles (1st animation):")))
        anim0 = tree["anims"][0] if tree["anims"] else []
        row = QHBoxLayout(); shown = 0
        for dr in anim0:
            if not dr:
                continue
            img = assemble_frame(d, dr[0], base, colors, ti, fmt)
            pic = QLabel()
            pic.setPixmap(QPixmap.fromImage(img).scaled(
                72, 96, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))
            pic.setStyleSheet("background:#111;")
            row.addWidget(pic)
            shown += 1
            if shown >= 8:
                break
        row.addStretch(1)
        v.addLayout(row)
        # плюс анимация ходьбы (кадры 1-й анимации, 1-е направление) как плеер
        if anim0 and anim0[0] and len(anim0[0]) > 1:
            pms = []
            for fr in anim0[0][:8]:
                img = assemble_frame(d, fr, base, colors, ti, fmt)
                pms.append(QPixmap.fromImage(img).scaled(
                    96, 128, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation))
            if len(pms) > 1:
                v.addWidget(QLabel(tr("Анимация:", "Animation:")))
                play = QLabel(); play._ai = 0; play.setPixmap(pms[0])
                play.setStyleSheet("background:#111;")
                v.addWidget(play)
                self._anim_labels.append((play, pms))

    def _add_item(self, v, ver, ep, ct):
        off = CELLTYPE_TO_ITEM_OFFSET.get(ct)
        if "Underground" in ver:
            base = ZTU_ITEMS_BASE            # объект-банк уровня (level-load -0x718c); проверить
        else:
            base = _de(ver, ITEMS_IMGS_BASE.get(_build_tag(ver), {}).get(ep) or 0) or None
        if off is None or base is None:
            v.addWidget(QLabel(tr(f"графика для celltype 0x{ct:02X} не задана", f"graphics for celltype 0x{ct:02X} not set")))
            return
        d = self.rom.data
        colors = pal.read_palette(d, self._sprite_palette(ver))
        nm = (_zt_cell_name(ver, ct) if ver.startswith("Zero Tolerance")
              else _celltype_name(ct))
        v.addWidget(QLabel(tr(f"<b>{nm}</b>  "
                           f"(объект-банк 0x{base:X} +0x{off:X}, тайл {off // 0x200})", f"<b>{nm}</b>  "
                           f"(object bank 0x{base:X} +0x{off:X}, tile {off // 0x200})")))
        from . import tiles as _t
        toff = base + off
        if toff + 512 <= len(d):
            tb, _, _ = _t.decode_zt_sheet(d, toff, 1, 1)
            timg = build_qimage(tb, 32, 32, colors, 0)
            pic = QLabel()
            pic.setPixmap(QPixmap.fromImage(timg).scaled(
                160, 160, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))
            pic.setStyleSheet("background:#111;")
            v.addWidget(pic)
        v.addWidget(QLabel(tr("(основной тайл графики; объект в игре может быть крупнее)", "(main graphics tile; the in-game object may be larger)")))


# ── Освещение уровня (3D-движок): режим темноты на уровень + палитра эпизода ──
# Каждый уровень имеет «режим темноты» (0-4), формирующий туман/освещение поверх
# палитры эпизода. Источники (из дизасма, проверено):
#   June: заголовок эпизода @0xB9B96 шаг 0x38 → поле0=light[] (байт/этаж), поле8=Pal_G(64цв).
#   July: то же @0x97A40.  ZT: env-массив @ZMAP_sig+0x5904 (байт/этаж); палитра 0x2072.
LIGHT_MODES = {
    0: ("Bright", tr("яркое освещение, без тумана", "bright lighting, no fog")),
    1: ("Dim", tr("приглушённый свет", "dim light")),
    2: ("Haze", tr("туман / дымка", "fog / haze")),
    3: ("No-ceiling", tr("без потолка — открытое пространство", "no ceiling — open space")),
    4: ("Black-ceiling", tr("чёрный потолок — темнота", "black ceiling — darkness")),
}


def _level_lighting(rom, ep: int, ln: int):
    """(режим:int, палитра:[80 CRAM-слов]) для уровня, либо None."""
    if rom is None:
        return None
    d = rom.data
    ver = rom.version
    floor = ln - 1

    def u16(a):
        return (d[a] << 8) | d[a + 1]

    def u32(a):
        return (d[a] << 24) | (d[a + 1] << 16) | (d[a + 2] << 8) | d[a + 3]

    try:
        if ver.startswith("Zero Tolerance"):
            sigs = _zt_sigs(ver)
            if not (1 <= ep <= len(sigs)):
                return None
            mode = d[_de(ver, sigs[ep - 1]) + 0x5904 + floor]
        elif "1995-06-23" in ver:
            mode = d[u32(0xB9B96 + (ep - 1) * 0x38) + floor]
        elif "1995-07-14" in ver:
            mode = d[u32(0x97A40 + (ep - 1) * 0x38) + floor]
        else:
            return None
        # ПАЛИТРА 3D-СЦЕНЫ. Дизасм FUN_1c24 @0x1062 выбирает 0x20F2/0x21F2 по эпизоду (D0=(-0x5704>>4)&3),
        # НО эмпирически (матч скринов всех уровней + красные стены basement) реально используется тёплая
        # 0x20F2 везде; 0x21F2 отличается лишь линией2 (циан) и в 3D-виде не подтвердился. Берём 0x20F2.
        # ВАЖНО про цвет уровня: floor/ceiling — общая текстура в 0x20F2 (≈один тёплый тон + модуляция
        # режимом); РЕАЛЬНУЮ per-level разницу дают СТЕНЫ (per-level метатекстуры texdef sig+4, разные
        # индексы→разные цвета линии0 0x20F2: красный basement, зелёный greenhouse) — см. вкладку «Стены».
        if ver.startswith("Zero Tolerance") and "Underground" not in ver:
            pal = 0x20F2
        else:
            banks = _wall_bank(ver)
            if not banks:
                return None
            pal = banks[min(max(ep - 1, 0), len(banks) - 1)][3]
        words = [u16(pal + i * 2) for i in range(64)]
    except IndexError:
        return None
    return mode, words


# СЫРЫЕ рампы тени движка для ВЕРТИКАЛЬНОГО градиента «иллюзии пространства» (пол↔потолок).
# Из ZT-сеттера FUN_1d66 → ROM 0x14ExxX. Структура на режим: пол[80б] + потолок[80б]; каждый
# байт = одна скан-строка, два ниббла = горизонтальный ДИЗЕРИНГ (показываем попиксельно, без
# смешивания). near (ближняя кромка экрана) = индекс 0; далее в глубину к горизонту. Уровень-
# ниббл: 0 ярчайший (нет тумана) > 4 > 3 > 2 > 1 > 8..E тёмные банки > F чёрный. Движковые
# константы, общие для всех ZT-билдов. Профиль совпал с игрой (Dim→горизонт чёрный, Haze дымка,
# NoCeil пол яркий + небо сверху тёмное, Black всё чёрное).
FOG_RAMP_RAW = {
    0: ("333333333333333333233323322332232232223222222222221222122112211211211121111111118888888888898889988998899998999899999999999b999bb99bb99bbbb9bbb9bbbbbbbbbbbbbbbb",
        "3333333333333333233323333223322332223222222222221222122221122112211121111111111188888888898889889889988998999899999999999b999b99b99bb99bb9bbb9bbbbbbbbbbbbbbbbbb"),  # Bright
    1: ("333323333223322222221222211221111111f1111ff11fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff8ff88f88f88888889889989989999999b99bb9bb9bbbbb",
        "33333323322322322222221221121121111111f11ff1ff1fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff8ffff88ff88888889888899889999999b9999bb99bbbbbbb"),  # Dim
    2: ("444434444334433333332333322332222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222b22b22bb2bbbbbbb9bb9bb99b999999",
        "44444434433433433333332332232232222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222bb22bbbb2bbbbbbb99bb9999b9999"),  # Haze
    3: ("00000000000000000000000000000000000000000000000000000000000000000000000000000000ffffffffffffffffffffffffffffffffff8ff88f88f88888889889989989999999b99bb9bb9bbbbb",
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000ffffffffffffffffffffffffffffffff8ffff88ff88888889888899889999999b9999bb99bbbbbbb"),  # NoCeil
    4: ("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff8ff88f88f88888898898899889998999999999d999d99dd99dd9dd9ddd9ddddddddddddd",
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff8ffff88ff88888888889988998998999999999d999d9999dd99dd99ddd9ddddddddddddddd"),  # Black
}
# ТОЧНЫЕ множители яркости по 8 полосам глубины (near→far) — извлечены из РЕАЛЬНЫХ пер-полосных
# remap-страниц CRAM-свопа движка (ZT): сеттер FUN_1d66 грузит per-режим 8 страниц index→index,
# применённых к палитре сцены даёт пер-полосную CRAM. Средняя яркость полос (доля от базы):
#   • Dim/NoCeil/Black — набор @0x3392..0x3A92: затемнение к чёрному.
#   • Haze — набор @0x3B92..0x4292: НЕ темнеет, ОСВЕТЛЯЕТ к дымке (>1) — потому Haze серо-светлый.
# Ниббл рампы 0x14ExxX (0..F) → полоса = round(nib*7/15). FF→полоса7 (макс. эффект).
FOG_BANDS_DIM = (0.903, 0.746, 0.739, 0.633, 0.603, 0.27, 0.102, 0.018)   # Dim/NoCeil/Black
FOG_BANDS_HAZE = (1.018, 1.0, 1.068, 1.154, 1.222, 1.322, 1.374, 1.374)   # Haze (осветление)
FOG_SKY = (8, 8, 28)                                # «нет потолка» (mode 3) — тёмное небо сверху


def _scene_floor_ceiling(words):
    """База пола (тёплый тон, оранж/корич) и потолка (прохладный, сине-серый) из РЕАЛЬНОЙ
    палитры сцены — реальные записи палитры. Возвращает (floor_rgb, floor_idx, ceil_rgb, ceil_idx)."""
    rgbs = [pal_module_rgb(w) for w in words]
    idxs = [i for i in range(len(rgbs)) if 90 < sum(rgbs[i]) < 620] or list(range(len(rgbs))) or [0]
    FLOOR_REF, CEIL_REF = (205, 70, 30), (150, 175, 205)   # красно-коричн пол / светлый сине-серый потолок;
    # референс краснее, чтобы в 0x21F2 (ep1/ep3) попадать в тёплую линию-2 (180,72,0) — basement краснее,
    # а в 0x20F2 (ep2 FLOOR) — в линию-1 (216,108,36) оранж-коричн. Так пол различается per-эпизод.

    def near(ref):
        return min(idxs, key=lambda i: sum((rgbs[i][k] - ref[k]) ** 2 for k in range(3)))
    fi, ci = near(FLOOR_REF), near(CEIL_REF)
    return rgbs[fi], fi, rgbs[ci], ci


def _lighting_gradient_image(words, mode: int, width: int = 96):
    """ВЕРТИКАЛЬНЫЙ градиент-«иллюзия пространства» (потолок сверху → горизонт → пол снизу) из
    РЕАЛЬНЫХ рамп тени FOG_RAMP_RAW поверх реальных цветов пола/потолка сцены. Дизеринг сырой,
    попиксельно (шахматка двух нибблов байта), БЕЗ смешивания. Высота = 160 (потолок 80 + пол 80).
    Возвращает (QImage, meta) где meta = {floor_idx, ceil_idx, words, split} для клик-инспекции."""
    floor_c, fi, ceil_c, ci = _scene_floor_ceiling(words)
    haze = mode == 2
    bands = FOG_BANDS_HAZE if haze else FOG_BANDS_DIM
    # цвет дымки (Haze) — усреднение самых светлых тонов сцены
    rgbs = [pal_module_rgb(w) for w in words]
    light = sorted(rgbs, key=sum, reverse=True)[:6] or [(234, 222, 222)]
    haze_c = tuple(sum(x) // len(light) for x in zip(*light))
    fl_hex, cl_hex = FOG_RAMP_RAW.get(mode, FOG_RAMP_RAW[1])
    fl = bytes.fromhex(fl_hex)
    cl = bytes.fromhex(cl_hex)
    # сверху вниз: потолок near→горизонт (cl 0..79), затем пол горизонт→near (floor реверс)
    lines = [(False, b) for b in cl] + [(True, b) for b in reversed(fl)]
    h = len(lines)
    img = QImage(width, h, QImage.Format_RGB32)
    for y, (is_floor, b) in enumerate(lines):
        hi, lo = b >> 4, b & 0xF
        base = floor_c if is_floor else ceil_c
        for x in range(width):
            nib = hi if (x + y) & 1 else lo            # шахматный дизеринг двух нибблов
            band = min(7, round(nib * 7 / 15))          # ниббл-уровень → полоса глубины
            if nib == 0xF:
                r, g, bl = 0, 0, 0                       # F = чёрный сентинел (Black-потолок/горизонт)
            elif mode == 3 and not is_floor and nib == 0:
                r, g, bl = FOG_SKY                      # «нет потолка» → небо
            elif haze:                                  # дымка: к светлому haze_c по полосе
                t = min(1.0, max(0.0, (bands[band] - 1.0) / 0.4))
                r, g, bl = (round(base[k] + (haze_c[k] - base[k]) * t) for k in range(3))
            else:                                       # затемнение по точному фактору полосы
                f = bands[band]
                r, g, bl = round(base[0] * f), round(base[1] * f), round(base[2] * f)
            img.setPixel(x, y, qRgba(r, g, bl, 255))
    meta = {"floor_idx": fi, "ceil_idx": ci, "words": words, "split": len(cl),
            "floor_rgb": floor_c, "ceil_rgb": ceil_c}
    return img, meta


class ClickableImage(QLabel):
    """QLabel-картинка с кликом: picked(info) = код цвета пикселя, clicked() = факт клика."""

    picked = Signal(str)
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._img = None
        self._meta = None

    def set_image(self, img: QImage, meta=None) -> None:
        self._img = img
        self._meta = meta
        self.setPixmap(QPixmap.fromImage(img))

    def clear_image(self) -> None:
        self._img = None
        self._meta = None
        self.clear()

    def mousePressEvent(self, e) -> None:
        if self._img is None:
            return
        self.clicked.emit()
        x = int(e.position().x()); y = int(e.position().y())
        if not (0 <= x < self._img.width() and 0 <= y < self._img.height()):
            return
        px = self._img.pixel(x, y)
        r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
        info = f"#{r:02X}{g:02X}{b:02X}"
        is_floor = y >= self._img.height() // 2       # потолок 80 + пол 80 → раздел по середине
        m = self._meta
        if m:
            idx = m["floor_idx"] if is_floor else m["ceil_idx"]
            w = m["words"][idx] if idx < len(m["words"]) else 0
            reg = tr("пол", "floor") if is_floor else tr("потолок", "ceiling")
            info += tr(f"  ·  {reg}, база CRAM ${w:04X} (idx {idx})  ·  скан {y}/{self._img.height()}", f"  ·  {reg}, CRAM base ${w:04X} (idx {idx})  ·  scan {y}/{self._img.height()}")
        self.picked.emit(info)


class GradientWindow(QDialog):
    """Отдельное окно с УВЕЛИЧЕННЫМ градиентом пространства для подробного просмотра.
    Клик по любому месту → код цвета внизу."""

    def __init__(self, img: QImage, meta, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Градиент пространства — подробно (клик = код цвета)", "Space gradient — detailed (click = color code)"))
        v = QVBoxLayout(self)
        big = img.scaled(img.width() * 3, img.height() * 3)   # ×3 для детального просмотра
        self.view = ClickableImage()
        self.view.set_image(big, meta)
        self.view.picked.connect(self._show)
        v.addWidget(self.view, 0, Qt.AlignCenter)
        self.code = QLabel(tr("Клик по градиенту — код цвета", "Click the gradient — color code"))
        self.code.setStyleSheet("font-family: monospace; font-size: 13px; color:#ff9;")
        self.code.setWordWrap(True)
        v.addWidget(self.code)

    def _show(self, info: str) -> None:
        self.code.setText(info)


class ColorStrip(QWidget):
    """Сетка кликабельных цветовых ячеек. colors=[(rgb, инфо)]. Клик → picked(инфо)."""

    picked = Signal(str)

    def __init__(self, cell: int = 16, per_row: int = 16) -> None:
        super().__init__()
        self._colors = []
        self._cell = cell
        self._per_row = per_row
        self._sel = -1

    def set_colors(self, colors) -> None:
        self._colors = list(colors)
        self._sel = -1
        n = len(self._colors)
        cs = self._cell
        if n:
            cols = min(self._per_row, n)
            rows = (n + self._per_row - 1) // self._per_row
            self.setFixedSize(cols * cs + 1, rows * cs + 1)
        else:
            self.setFixedSize(1, 1)
        self.update()

    def _idx(self, x, y) -> int:
        c = int(x) // self._cell
        r = int(y) // self._cell
        if c < 0 or c >= self._per_row or r < 0:
            return -1
        i = r * self._per_row + c
        return i if 0 <= i < len(self._colors) else -1

    def mousePressEvent(self, e) -> None:
        i = self._idx(e.position().x(), e.position().y())
        if i >= 0:
            self._sel = i
            self.update()
            self.picked.emit(self._colors[i][1])

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        cs = self._cell
        for i, (rgb, _info) in enumerate(self._colors):
            x = (i % self._per_row) * cs
            y = (i // self._per_row) * cs
            p.fillRect(x, y, cs - 1, cs - 1, QColor(*rgb))
        if 0 <= self._sel < len(self._colors):
            x = (self._sel % self._per_row) * cs
            y = (self._sel // self._per_row) * cs
            p.setPen(QColor(255, 255, 255)); p.drawRect(x, y, cs - 1, cs - 1)
            p.setPen(QColor(0, 0, 0)); p.drawRect(x + 1, y + 1, cs - 3, cs - 3)
        p.end()


class LightingPanel(QGroupBox):
    """Панель освещения уровня: режим темноты + градиент тумана + палитра (кликабельно)."""

    def __init__(self) -> None:
        super().__init__(tr("Освещение / градиент уровня", "Lighting / level gradient"))
        v = QVBoxLayout(self); v.setSpacing(4)
        self.mode_lbl = QLabel("—"); self.mode_lbl.setWordWrap(True)
        self.mode_lbl.setStyleSheet("font-size: 11px;")
        v.addWidget(self.mode_lbl)
        g = QLabel(tr("Градиент пространства (потолок↑ → пол↓) — клик: увеличить:", "Space gradient (ceiling↑ → floor↓) — click: enlarge:"))
        g.setStyleSheet("font-size: 10px; color:#9cf;"); g.setWordWrap(True)
        v.addWidget(g)
        self._words = None
        self._mode = 0
        self.grad = ClickableImage()
        self.grad.setStyleSheet("background:#000;")
        self.grad.setCursor(Qt.PointingHandCursor)
        self.grad.clicked.connect(self._open_grad_window)
        v.addWidget(self.grad, 0, Qt.AlignLeft)
        self.grad_lbl = QLabel(tr("Клик по градиенту — открыть в отдельном окне", "Click the gradient — open in a separate window"))
        self.grad_lbl.setStyleSheet("font-family: monospace; font-size: 10px; color:#bdf;")
        self.grad_lbl.setWordWrap(True)
        v.addWidget(self.grad_lbl)
        pl = QLabel(tr("Палитра уровня (стены/окружение):", "Level palette (walls/environment):"))
        pl.setStyleSheet("font-size: 10px; color:#9cf;")
        v.addWidget(pl)
        self.pal = ColorStrip(cell=14, per_row=16)
        self.pal.picked.connect(self._show)
        v.addWidget(self.pal)
        self.code = QLabel(tr("Кликните цвет — покажу код", "Click a color — I'll show the code"))
        self.code.setStyleSheet("font-family: monospace; font-size: 11px; color:#ff9;")
        self.code.setWordWrap(True)
        v.addWidget(self.code)

    def _show(self, info: str) -> None:
        self.code.setText(info)

    def _open_grad_window(self) -> None:
        if self._words is None:
            return
        img, meta = _lighting_gradient_image(self._words, self._mode, width=160)
        win = GradientWindow(img, meta, self)
        win.show()

    def update_level(self, rom, ep: int, ln: int) -> None:
        res = _level_lighting(rom, ep, ln)
        if not res:
            self.mode_lbl.setText(tr("освещение: —", "lighting: —"))
            self._words = None
            self.grad.clear_image(); self.grad_lbl.setText(""); self.pal.set_colors([])
            return
        mode, words = res
        nm, desc = LIGHT_MODES.get(mode, ("?", ""))
        self.mode_lbl.setText(
            tr(f"<b>Режим темноты:</b> {mode} — <b>{nm}</b><br>"
            f"<span style='color:#9f9'>{desc}</span>", f"<b>Darkness mode:</b> {mode} — <b>{nm}</b><br>"
            f"<span style='color:#9f9'>{desc}</span>"))
        self._words, self._mode = words, mode
        img, meta = _lighting_gradient_image(words, mode, width=48)   # компактный в панели
        self.grad.set_image(img, meta)
        fc, cc = meta["floor_rgb"], meta["ceil_rgb"]
        self.grad_lbl.setText(
            tr(f"пол #{fc[0]:02X}{fc[1]:02X}{fc[2]:02X} · потолок #{cc[0]:02X}{cc[1]:02X}{cc[2]:02X} "
            "· клик → увеличить в окне", f"floor #{fc[0]:02X}{fc[1]:02X}{fc[2]:02X} · ceiling #{cc[0]:02X}{cc[1]:02X}{cc[2]:02X} "
            "· click → enlarge in window"))
        cells = []
        for w in words:
            rgb = pal_module_rgb(w)
            cells.append((rgb, f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}    CRAM ${w:04X}"))
        self.pal.set_colors(cells)


class MapViewer(QWidget):
    """Вкладка «Карты»: уровни с объектами и их кодами (как в редакторе BZTEdit).

    Каждая ячейка раскрашена иконкой по типу (стена/дверь/враг/оружие/предмет/старт/…)
    и подписана hex-кодом. Слева — выбор уровня, легенда, инфо о ячейке под курсором и
    статистика. ZT: 3 эпизода × 16 уровней 32×32. June: 24 уровня переменного размера."""

    CS = 16   # размер ячейки в пикселях (= натуральный размер иконки)

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._grid: Optional[GridSpec] = None
        self._cur = None          # (offset, X, Y) текущего уровня
        self._stats = {}
        self._celltypes = None    # таблица ремапа ZT (или None для статичного BZT)

        self.view = TileView()
        self.view.set_zoom(1)
        self.view.hoverMoved.connect(self._hover)
        self.view.doubleClicked.connect(self._show_cell_assets)

        panel = QWidget(); panel.setFixedWidth(350)
        v = QVBoxLayout(panel)
        self.info = QLabel(tr("Загрузите ROM (вкладка «Вся графика»)", "Load a ROM (the «All graphics» tab)"))
        self.info.setWordWrap(True); v.addWidget(self.info)

        form = QFormLayout()
        self.level = QComboBox()
        self.level.currentIndexChanged.connect(self._render)
        form.addRow(tr("Уровень:", "Level:"), self.level)
        self.zoom = QSpinBox(); self.zoom.setRange(1, 8); self.zoom.setValue(1)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        self.show_hex = QCheckBox(tr("Показывать hex-коды", "Show hex codes"))
        self.show_hex.setChecked(True)
        self.show_hex.stateChanged.connect(self._render)
        form.addRow(self.show_hex)
        v.addLayout(form)

        self.stats_lbl = QLabel("—")
        self.stats_lbl.setStyleSheet("font-size: 11px; color:#9f9;")
        v.addWidget(self.stats_lbl)

        self.cell_lbl = QLabel(tr("Наведите на ячейку", "Hover over a cell"))
        self.cell_lbl.setWordWrap(True)
        self.cell_lbl.setMinimumHeight(40)
        self.cell_lbl.setStyleSheet("font-family: monospace; font-size: 11px;")
        v.addWidget(self.cell_lbl)

        self.obj_text = QPlainTextEdit()
        self.obj_text.setReadOnly(True)
        self.obj_text.setMaximumHeight(150)
        self.obj_text.setStyleSheet("font-family: monospace; font-size: 10px;")
        v.addWidget(QLabel(tr("Объекты уровня:", "Level objects:")))
        v.addWidget(self.obj_text)

        self.light_panel = LightingPanel()
        v.addWidget(self.light_panel)

        v.addWidget(self._build_legend())

        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)
        v.addStretch(1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(self.view, 1)

    def _build_legend(self) -> QWidget:
        box = QGroupBox(tr("Легенда", "Legend"))
        g = QVBoxLayout(box); g.setSpacing(1)
        icons = _load_map_icons()
        for i, lbl in enumerate(MAP_ICON_LABELS):
            row = QHBoxLayout(); row.setSpacing(4)
            sw = QLabel()
            sw.setPixmap(QPixmap.fromImage(icons[i]).scaled(
                14, 14, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation))
            sw.setFixedSize(14, 14)
            row.addWidget(sw)
            t = QLabel(lbl); t.setStyleSheet("font-size: 10px;")
            row.addWidget(t, 1)
            g.addLayout(row)
        # бирюзовый маркер спавнер-стен (BZT ниши, cell-ID 0xE4-0xFF)
        row = QHBoxLayout(); row.setSpacing(4)
        sw = QLabel(); sw.setFixedSize(14, 14)
        sw.setStyleSheet("background:#00CED1; border:1px solid #066;")
        row.addWidget(sw)
        t = QLabel(tr("Спавнер-стена (ниша, BZT)", "Spawner wall (niche, BZT)")); t.setStyleSheet("font-size: 10px;")
        row.addWidget(t, 1); g.addLayout(row)
        return box

    # ---------- данные ----------
    def set_rom(self, rom) -> None:
        self.level.blockSignals(True)
        self.level.clear()
        levels = _map_levels(rom.version) if rom else []
        for ep, ln, off, X, Y in levels:
            nm = _zt_level_name(rom, ep, ln)
            label = tr(f"Эп.{ep}  Ур.{ln}", f"Ep.{ep}  Lvl.{ln}")
            if nm:
                label += f"  «{nm}»"
            label += f"   {X}×{Y}"
            self.level.addItem(label, (ep, ln, off, X, Y))
        self.level.blockSignals(False)
        if rom is None:
            return
        if levels:
            self.info.setText(tr(f"<b>{version_label(rom.version)}</b><br>{len(levels)} уровней. "
                              "Ячейки раскрашены по типу + hex-код (как в BZTEdit).", f"<b>{version_label(rom.version)}</b><br>{len(levels)} levels. "
                              "Cells colored by type + hex code (as in BZTEdit)."))
            self.level.setCurrentIndex(0)
            self._render()
        else:
            self.info.setText(tr(f"<b>{version_label(rom.version)}</b><br>Таблица уровней для этой "
                              "версии пока не вскрыта.", f"<b>{version_label(rom.version)}</b><br>Level table for this "
                              "version is not cracked yet."))
            self.view.set_pixmap(QPixmap())

    # ---------- рендер ----------
    def _compose(self) -> Optional[QImage]:
        rom = self.main.rom
        lvl = self.level.currentData()
        if rom is None or lvl is None:
            return None
        ep, _ln, off, X, Y = lvl
        icons = _load_map_icons()
        CS = self.CS
        img = QImage(X * CS, Y * CS, QImage.Format.Format_ARGB32)
        img.fill(0xFF000000)
        p = QPainter(img)
        show_hex = self.show_hex.isChecked()
        if show_hex:
            f = QFont("monospace"); f.setPixelSize(7); p.setFont(f)
        data = rom.data; ver = rom.version
        celltypes = _map_celltypes(rom, ver, ep)
        self._celltypes = celltypes
        spawner_cids = _spawner_wall_cids(rom, ver, ep)   # BZT ниша-стены (cell-ID 0xE4-0xFF)
        stats = {8: 0, 9: 0, 10: 0, 11: 0}
        light_text = {1, 2, 3, 4, 5, 6, 7, 12, 14}   # иконки со светлым фоном → чёрный текст
        for y in range(Y):
            for x in range(X):
                o = off + y * X + x
                cid = data[o] if o < len(data) else 0
                ic = _cell_icon(ver, cid, celltypes)
                p.drawImage(QRectF(x * CS, y * CS, CS, CS), icons[ic])
                is_spawner = cid in spawner_cids
                if is_spawner:                            # бирюзовый квадрат-маркер
                    p.fillRect(QRectF(x * CS + 1, y * CS + 1, CS - 2, CS - 2),
                               QColor(0, 206, 209))
                if ic in stats:
                    stats[ic] += 1
                if show_hex and cid:
                    p.setPen(Qt.GlobalColor.black if (ic in light_text or is_spawner)
                             else QColor(200, 200, 200))
                    p.drawText(QRectF(x * CS, y * CS, CS, CS),
                               Qt.AlignmentFlag.AlignCenter, f"{cid:02X}")
        p.end()
        self._grid = GridSpec(CS, CS, X, Y)
        self._cur = (off, X, Y)
        self._stats = stats
        return img

    def _render(self, *_) -> None:
        img = self._compose()
        if img is None:
            self.view.set_pixmap(QPixmap()); return
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))
        s = self._stats
        self.stats_lbl.setText(
            tr(f"Враги: {s.get(9,0)}   Оружие: {s.get(10,0)}   "
            f"Предметы: {s.get(11,0)}   Старт: {s.get(8,0)}", f"Enemies: {s.get(9,0)}   Weapons: {s.get(10,0)}   "
            f"Items: {s.get(11,0)}   Start: {s.get(8,0)}"))
        self._fill_objects()
        lvl = self.level.currentData()
        if lvl is not None:
            self.light_panel.update_level(self.main.rom, lvl[0], lvl[1])

    def _fill_objects(self) -> None:
        """Список объектов уровня (по игровым celltype) с позициями (x,y)."""
        if not self._cur or self.main.rom is None:
            self.obj_text.setPlainText(""); return
        off, X, Y = self._cur
        ver = self.main.rom.version
        ct = getattr(self, "_celltypes", None)
        data = self.main.rom.data
        groups = {8: (tr("СТАРТ", "START"), []), 9: (tr("ВРАГИ", "ENEMIES"), []), 10: (tr("ОРУЖИЕ", "WEAPONS"), []),
                  11: (tr("ПРЕДМЕТЫ", "ITEMS"), []), 14: (tr("СЕКРЕТ/РАЗРУШ", "SECRET/DESTR"), [])}
        for y in range(Y):
            for x in range(X):
                cid = data[off + y * X + x]
                ic = _cell_icon(ver, cid, ct)
                if ic in groups:
                    if ct is not None and not ver.startswith("Zero Tolerance"):
                        nm = _celltype_name(ct[cid])
                    elif ct is not None:
                        nm = _zt_cell_name(ver, ct[cid])
                    else:
                        nm = _cell_def(ver, cid)[1]
                    groups[ic][1].append((x, y, nm))
        lines = []
        for ic, (title, items) in groups.items():
            if not items:
                continue
            from collections import Counter
            by = Counter(nm for _, _, nm in items)
            lines.append(f"■ {title} ({len(items)}):")
            for nm, n in by.most_common():
                pos = [f"({x},{y})" for x, y, m in items if m == nm][:8]
                more = "…" if n > 8 else ""
                lines.append(f"   {nm} ×{n}: {' '.join(pos)}{more}")
        self.obj_text.setPlainText("\n".join(lines) if lines else tr("нет объектов", "no objects"))

    def _hover(self, x: float, y: float) -> None:
        if not self._cur or self.main.rom is None:
            return
        off, X, Y = self._cur
        cx, cy = int(x) // self.CS, int(y) // self.CS
        if 0 <= cx < X and 0 <= cy < Y:
            ver = self.main.rom.version
            cid = self.main.rom.data[off + cy * X + cx]
            ct = getattr(self, "_celltypes", None)
            ic = _cell_icon(ver, cid, ct)
            if ct is None:
                name = _cell_def(ver, cid)[1]
            elif ver.startswith("Zero Tolerance"):   # ZT: каноническая ячейка
                name = _zt_cell_name(ver, ct[cid])
            else:                                     # BZT: игровой объект (celltype)
                name = f"{_celltype_name(ct[cid])}  [ct 0x{ct[cid]:02X}]"
            self.cell_lbl.setText(
                tr(f"код 0x{cid:02X} ({cid}) → {MAP_ICON_LABELS[ic]}\n"
                f"{name};  ячейка ({cx}, {cy})", f"code 0x{cid:02X} ({cid}) → {MAP_ICON_LABELS[ic]}\n"
                f"{name};  cell ({cx}, {cy})"))

    def _show_cell_assets(self, x: float, y: float) -> None:
        """Двойной клик по ячейке — окно её визуальных ассетов (текстуры/спрайты)."""
        if not self._cur or self.main.rom is None:
            return
        off, X, Y = self._cur
        cx, cy = int(x) // self.CS, int(y) // self.CS
        if not (0 <= cx < X and 0 <= cy < Y):
            return
        lvl = self.level.currentData()
        ep = lvl[0] if lvl else 1
        cid = self.main.rom.data[off + cy * X + cx]
        ct = getattr(self, "_celltypes", None)
        # BZT спавнер-стена (ниша 0xE4-0xFF): враг кодируется ЦВЕТОМ ниши (группа метатекстуры
        # 249-251 / 252-254). Резолвим по ДОМИНАНТНОМУ соседнему маркеру этой цвет-группы по
        # всему уровню (устойчивее «ближайшего»: маркеры плотные, ближайший часто чужой).
        spawn_ct = None
        if ct is not None and 0xE4 <= cid <= 0xFF \
                and not self.main.rom.version.startswith("Zero Tolerance"):
            spawn_ct = self._niche_enemy(off, X, Y, cid, ct, ep)
        CellAssetDialog(self, self.main.rom, ep, cid, ct, spawn_enemy_ct=spawn_ct).show()

    _ENEMY_MARKERS = {0x29, 0x2A, 0x2B, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B}

    def _niche_enemy(self, off, X, Y, cid, celltypes, ep):
        """Враг ниши-стены ПО ЦВЕТУ ниши (метатекстура-группа), а не просто ближайшему маркеру:
        для цвет-группы кликнутой ниши берём доминантный соседний enemy-маркер по уровню."""
        d = self.main.rom.data
        banks = _wall_bank(self.main.rom.version)
        if not banks or ep - 1 >= len(banks):
            return None
        sig = banks[ep - 1][1]
        if d[sig:sig + 4] != b"ZMAP":
            return None
        texorder = sig + 4 + 4096

        def group(c):                       # 'purple'(249-251)/'red'(252-254)/None
            o = texorder + c * 8
            for k in range(4):
                m = (d[o + k * 2] << 8) | d[o + k * 2 + 1]
                if 249 <= m <= 255:
                    return "purple" if m <= 251 else "red"
            return None

        my = group(cid)
        if my is None:
            return None

        # 1) ДЕТЕРМИНИРОВАННО: цвет ниши = цвет в ИМЕНИ врага-маркера, присутствующего в уровне
        present = {celltypes[b] for b in set(d[off:off + X * Y])
                   if celltypes[b] in self._ENEMY_MARKERS}
        kw = ("red", tr("красн", "red")) if my == "red" else ("purple", tr("фиолет", "purple"))
        for ect in present:
            if any(k in _celltype_name(ect).lower() for k in kw):
                return ect

        # 2) запас: доминантный соседний маркер этой цвет-группы по уровню
        from collections import Counter
        cnt = Counter()
        for y in range(Y):
            for x in range(X):
                c = d[off + y * X + x]
                if 0xE4 <= c <= 0xFF and group(c) == my:
                    best, bestd = None, 99
                    for dy in range(-5, 6):
                        for dx in range(-5, 6):
                            nx, ny = x + dx, y + dy
                            if 0 <= nx < X and 0 <= ny < Y:
                                nct = celltypes[d[off + ny * X + nx]]
                                if nct in self._ENEMY_MARKERS and max(abs(dx), abs(dy)) < bestd:
                                    bestd = max(abs(dx), abs(dy)); best = nct
                    if best is not None:
                        cnt[best] += 1
        return cnt.most_common(1)[0][0] if cnt else None

    # ---------- экспорт ----------
    def grid_state(self):
        if self.main.rom is None:
            return None
        img = self._compose()
        if img is None or self._grid is None:
            return None
        lvl = self.level.currentData()
        ep, ln = (lvl[0], lvl[1]) if lvl else (0, 0)
        base = (os.path.splitext(os.path.basename(self.main.rom.path))[0]
                + f"_map_e{ep}l{ln}")
        return img, self._grid, base

    def grid_cell_label(self, c, r):
        cid = 0
        if self._cur and self.main.rom is not None:
            off, X, Y = self._cur
            if 0 <= c < X and 0 <= r < Y:
                cid = self.main.rom.data[off + r * X + c]
        return f"x{c}_y{r}_cid{cid:02X}"


class _SongRenderThread(QThread):
    """Фоновый рендер песни (FM-синтез медленный — не морозим UI). Эмитит (key, pcm@44100)."""
    rendered = Signal(object, object)

    def __init__(self, data, banks, index, max_seconds, transpose, key,
                 ym2612_mode=True, speed=1.0, patch_overrides=None, transpose_overrides=None):
        super().__init__()
        self._a = (data, banks, index, max_seconds, transpose, ym2612_mode, speed,
                   patch_overrides, transpose_overrides)
        self._key = key

    def run(self):
        from . import gems_player as gp, opn2_chip
        data, banks, index, secs, transpose, ym, speed, pov, tov = self._a
        try:
            pcm = gp.GemsPlayer(data, banks, ym2612_mode=ym).render_song(
                index, max_seconds=secs, transpose=transpose, speed=speed,
                patch_overrides=pov, transpose_overrides=tov)
            if pcm:
                pcm = gp._resample_s16_stereo(pcm, opn2_chip.NATIVE_RATE, 44100)
        except Exception:
            pcm = b""
        self.rendered.emit(self._key, pcm)


class SoundViewer(QWidget):
    """Вкладка «Звук»: список PCM-сэмплов GEMS, воспроизведение и экспорт в WAV.

    Сэмплы — unsigned 8-bit mono. Частота настраивается (GEMS DAC ~5–10.5 кГц).
    ZT ~73 сэмпла, July ~65; в June PCM нет (только FM)."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._samples = []
        self._base = None
        self._sink = None
        self._buf = None

        self.info = QLabel(tr("Загрузите ROM (вкладка «Вся графика»)", "Load a ROM (the «All graphics» tab)"))
        self.info.setWordWrap(True)

        self.list = QListWidget()
        self.list.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.list.currentRowChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda *_: self._play())

        self.ingame = QCheckBox(tr("Частота как в игре", "In-game rate"))
        self.ingame.setChecked(True)
        self.ingame.setToolTip(tr("Частота DAC по флагам сэмпла: 7670454/(144·(flags&0x0F)). "
                               "Снять — задать вручную.", "DAC rate from sample flags: 7670454/(144·(flags&0x0F)). Uncheck to set manually."))
        self.ingame.stateChanged.connect(self._ingame_changed)
        self.rate = QSpinBox()
        self.rate.setRange(2000, 32000)
        self.rate.setSingleStep(100)
        self.rate.setValue(10500)
        self.rate.setSuffix(tr(" Гц", " Hz"))
        self.rate.setEnabled(False)   # по умолчанию — как в игре

        self.play_btn = QPushButton(tr("▶ Воспроизвести", "▶ Play"))
        self.play_btn.clicked.connect(self._play)
        self.stop_btn = QPushButton(tr("⏹ Стоп", "⏹ Stop"))
        self.stop_btn.clicked.connect(self._stop)
        self.exp_btn = QPushButton(tr("Экспорт выбранного…", "Export selected…"))
        self.exp_btn.clicked.connect(self._export_one)
        self.expall_btn = QPushButton(tr("Экспорт всех…", "Export all…"))
        self.expall_btn.clicked.connect(self._export_all)

        form = QFormLayout()
        form.addRow(self.ingame)
        form.addRow(tr("Частота:", "Rate:"), self.rate)
        row = QHBoxLayout()
        row.addWidget(self.play_btn); row.addWidget(self.stop_btn)
        row2 = QHBoxLayout()
        row2.addWidget(self.exp_btn); row2.addWidget(self.expall_btn)

        panel = QWidget(); panel.setFixedWidth(340)
        v = QVBoxLayout(panel)
        v.addWidget(QLabel(tr("<b>PCM-сэмплы (DAC)</b>", "<b>PCM samples (DAC)</b>")))
        v.addWidget(self.info)
        v.addLayout(form)
        v.addLayout(row)
        v.addLayout(row2)
        self.sel_lbl = QLabel("—")
        self.sel_lbl.setStyleSheet("font-family: monospace; font-size: 11px; color:#9cf;")
        v.addWidget(self.sel_lbl)
        v.addWidget(self.list, 1)

        # ── Музыка (FM-секвенции GEMS) — ДОПОЛНЯЕТ браузер сэмплов ──
        self._banks = None
        self._msink = None
        self._mbuf = None
        self._song_cache = {}        # key=(row,transpose,secs) → pcm@44100
        self._rthread = None
        self._pending_key = None
        self.music_info = QLabel("")
        self.music_info.setWordWrap(True)
        self.song_list = QListWidget()
        self.song_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.song_list.itemDoubleClicked.connect(lambda *_: self._play_song())
        self.song_play = QPushButton(tr("▶ Играть", "▶ Play"))
        self.song_play.clicked.connect(self._play_song)
        self.song_stop = QPushButton(tr("⏹ Стоп", "⏹ Stop"))
        self.song_stop.clicked.connect(self._stop_song)
        self.song_exp = QPushButton(tr("Экспорт песни…", "Export song…"))
        self.song_exp.clicked.connect(self._export_song)
        self.song_expall = QPushButton(tr("Экспорт всех…", "Export all…"))
        self.song_expall.clicked.connect(self._export_all_songs)
        self.song_len = QSpinBox()
        self.song_len.setRange(10, 300); self.song_len.setValue(150); self.song_len.setSuffix(tr(" с", " s"))
        self.song_len.setToolTip(tr("Потолок длины. Песня играет один полный проход (луп) "
                                 "и останавливается сама; этот предел — страховка.", "Length cap. The song plays one full pass (loop) and stops by itself; this limit is a safeguard."))
        self.song_transpose = QSpinBox()
        self.song_transpose.setRange(-36, 36); self.song_transpose.setValue(12)
        self.song_chip = QComboBox()
        self.song_chip.addItem("YM2612 (MD1)", True)
        self.song_chip.addItem("YM3438 (MD2/ASIC)", False)
        self.song_speed = QSpinBox()
        self.song_speed.setRange(25, 200); self.song_speed.setValue(100); self.song_speed.setSuffix(" %")
        self.song_speed.setToolTip(tr("Скорость воспроизведения (темп). 100% = как задано "
                                   "в секвенции; уменьшите, если песня играет слишком быстро.", "Playback speed (tempo). 100% = as set in the sequence; lower it if the song plays too fast."))
        mform = QFormLayout()
        mform.addRow(tr("Макс. длина:", "Max length:"), self.song_len)
        mform.addRow(tr("Транспон.:", "Transpose:"), self.song_transpose)
        mform.addRow(tr("Чип:", "Chip:"), self.song_chip)
        mform.addRow(tr("Скорость:", "Speed:"), self.song_speed)
        mrow = QHBoxLayout(); mrow.addWidget(self.song_play); mrow.addWidget(self.song_stop)
        mrow2 = QHBoxLayout(); mrow2.addWidget(self.song_exp); mrow2.addWidget(self.song_expall)
        self.audition_btn = QPushButton(tr("🔊 Инструменты / SFX…", "🔊 Instruments / SFX…"))
        self.audition_btn.setToolTip(tr("Прослушать инструменты GEMS одной нотой. Патчи, "
                                     "не звучащие в музыке, = звуковые эффекты игры.", "Audition GEMS instruments with a single note. Patches that don't play in the music are the game's sound effects."))
        self.audition_btn.clicked.connect(self._open_audition)
        mpanel = QWidget(); mpanel.setFixedWidth(340)
        mv = QVBoxLayout(mpanel)
        mv.addWidget(QLabel(tr("<b>Музыка (FM, YM2612)</b>", "<b>Music (FM, YM2612)</b>")))
        mv.addWidget(self.music_info)
        mv.addLayout(mform)
        mv.addLayout(mrow)
        mv.addLayout(mrow2)
        mv.addWidget(self.audition_btn)
        mv.addWidget(self.song_list, 1)

        lay = QHBoxLayout(self)
        lay.addWidget(panel, 0)
        lay.addWidget(mpanel, 0)
        lay.addStretch(1)

    def set_rom(self, rom) -> None:
        self._stop()
        self._stop_song()
        self.list.clear()
        self.song_list.clear()
        self._samples = []
        self._base = None
        self._banks = None
        self._song_cache.clear()
        self._pending_key = None
        if rom is None:
            return
        self._base, self._samples = gems.extract_samples(rom)
        if not self._samples:
            self.info.setText(tr(f"<b>{version_label(rom.version)}</b><br>PCM-сэмплов GEMS не найдено "
                              "(вероятно, только FM-музыка).", f"<b>{version_label(rom.version)}</b><br>No GEMS PCM samples found "
                              "(probably FM music only)."))
        else:
            self.info.setText(tr(f"Таблица @0x{self._base:X} · <b>{len(self._samples)}</b> "
                              "PCM-сэмплов (unsigned 8-bit). 2× клик — играть.", f"Table @0x{self._base:X} · <b>{len(self._samples)}</b> "
                              "PCM samples (unsigned 8-bit). Double-click — play."))
            for i, s in enumerate(self._samples):
                self.list.addItem(tr(f"#{i:02d}  {s['length']:>6} б  {s['rate']:>5} Гц"
                                  f"  (0x{s['addr']:06X}, flags {s['flags']:02X})", f"#{i:02d}  {s['length']:>6} b  {s['rate']:>5} Hz"
                                  f"  (0x{s['addr']:06X}, flags {s['flags']:02X})"))
            self.list.setCurrentRow(0)
        self._populate_songs(rom)

    def _populate_songs(self, rom) -> None:
        from . import gems_music as gm, opn2_chip
        try:
            self._banks = gm.find_gems_banks(rom.data, self._base)
        except Exception:
            self._banks = None
        if not self._banks:
            self.music_info.setText(tr("Банк секвенций GEMS не найден.", "GEMS sequence bank not found."))
            return
        nsong = gm.song_count(rom.data, self._banks["sequences"])
        if not opn2_chip.available():
            self.music_info.setText(
                tr(f"Найдено песен: <b>{nsong}</b>, но эмулятор YM2612 недоступен "
                "(нужен звук — см. ztextractor/opn2/README.md).", f"Songs found: <b>{nsong}</b>, but the YM2612 emulator is unavailable "
                "(want sound? see ztextractor/opn2/README.md)."))
            self._banks = None   # FM недоступен → все обработчики музыки (проверяют _banks) выходят
            return
        self.music_info.setText(
            tr(f"Секвенции @0x{self._banks['sequences']:X} · <b>{nsong}</b> песен "
            "(FM-синтез через YM2612). 2× клик — играть.", f"Sequences @0x{self._banks['sequences']:X} · <b>{nsong}</b> songs "
            "(FM synthesis via YM2612). Double-click — play."))
        for i in range(nsong):
            song = gm.parse_song(rom.data, self._banks["sequences"], i)
            ch = len(song["channels"]) if song else 0
            self.song_list.addItem(tr(f"Песня #{i:02d}   каналов: {ch}", f"Song #{i:02d}   channels: {ch}"))
        if nsong:
            self.song_list.setCurrentRow(0)

    def _cur_rate(self, sample) -> int:
        return sample["rate"] if self.ingame.isChecked() else self.rate.value()

    def _ingame_changed(self) -> None:
        self.rate.setEnabled(not self.ingame.isChecked())
        self._on_select(self.list.currentRow())

    def _on_select(self, row: int) -> None:
        if 0 <= row < len(self._samples):
            s = self._samples[row]
            r = self._cur_rate(s)
            if self.ingame.isChecked():
                self.rate.blockSignals(True)
                self.rate.setValue(s["rate"])     # показать частоту сэмпла
                self.rate.blockSignals(False)
            sec = s["length"] / max(1, r)
            tag = tr(" (как в игре)", " (as in game)") if self.ingame.isChecked() else ""
            self.sel_lbl.setText(tr(f"Сэмпл #{row}: 0x{s['addr']:06X}, "
                                 f"{s['length']} б ≈ {sec:.2f} с @ {r} Гц{tag}", f"Sample #{row}: 0x{s['addr']:06X}, "
                                 f"{s['length']} b ≈ {sec:.2f} s @ {r} Hz{tag}"))

    def _stop(self) -> None:
        if self._sink is not None:
            self._sink.stop()
            self._sink = None

    def _play(self) -> None:
        row = self.list.currentRow()
        if row < 0 or self.main.rom is None or not self._samples:
            return
        self._stop()
        # Устройства часто не поддерживают <8 кГц → ресемпл на 44100 (питч сохраняется)
        play_rate = 44100
        raw = gems.sample_pcm(self.main.rom.data, self._samples[row])
        raw = gems.resample_u8(raw, self._cur_rate(self._samples[row]), play_rate)
        fmt = QAudioFormat()
        fmt.setSampleRate(play_rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.UInt8)
        self._buf = QBuffer()
        self._buf.setData(raw)
        self._buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt)
        self._sink.start(self._buf)

    def _export_one(self) -> None:
        row = self.list.currentRow()
        if row < 0 or self.main.rom is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить WAV", "Save WAV"), f"sample_{row:02d}.wav", "WAV (*.wav)")
        if path:
            wav = gems.sample_wav_bytes(self.main.rom.data,
                                        self._samples[row],
                                        self._cur_rate(self._samples[row]))
            with open(path, "wb") as f:
                f.write(wav)

    def _export_all(self) -> None:
        if self.main.rom is None or not self._samples:
            return
        d = QFileDialog.getExistingDirectory(self, tr("Папка для экспорта всех WAV", "Folder to export all WAVs"))
        if not d:
            return
        for i, s in enumerate(self._samples):
            wav = gems.sample_wav_bytes(self.main.rom.data, s, self._cur_rate(s))
            with open(os.path.join(d, f"sample_{i:02d}.wav"), "wb") as f:
                f.write(wav)
        QMessageBox.information(self, tr("Экспорт", "Export"),
                               tr(f"Сохранено {len(self._samples)} WAV в:\n{d}", f"Saved {len(self._samples)} WAV in:\n{d}"))

    # ── музыка (FM) ──
    def _render_song_pcm(self, idx: int):
        """PCM (int16 LE стерео @44100) для песни idx, либо None."""
        from . import gems_player as gp, opn2_chip
        pcm = gp.GemsPlayer(self.main.rom.data, self._banks).render_song(
            idx, max_seconds=float(self.song_len.value()),
            transpose=self.song_transpose.value(), gain=6.0)
        if not pcm:
            return None
        return gp._resample_s16_stereo(pcm, opn2_chip.NATIVE_RATE, 44100)

    def _stop_song(self) -> None:
        if self._msink is not None:
            self._msink.stop()
            self._msink = None

    def _play_song(self) -> None:
        row = self.song_list.currentRow()
        if row < 0 or self.main.rom is None or not self._banks:
            return
        self._stop(); self._stop_song()
        ym = self.song_chip.currentData()
        speed = self.song_speed.value() / 100.0
        key = (row, self.song_transpose.value(), float(self.song_len.value()), ym, speed)
        if key in self._song_cache:                 # уже отрендерено — играем мгновенно
            self._start_play(self._song_cache[key])
            return
        if self._rthread is not None and self._rthread.isRunning():
            return                                  # рендер уже идёт
        self._pending_key = key
        self.song_play.setEnabled(False)
        self.song_play.setText(tr("⏳ рендер…", "⏳ rendering…"))
        self._rthread = _SongRenderThread(
            self.main.rom.data, self._banks, row, key[2], key[1], key,
            ym2612_mode=ym, speed=speed)
        self._rthread.rendered.connect(self._on_song_rendered)
        self._rthread.start()

    def _on_song_rendered(self, key, pcm) -> None:
        self.song_play.setEnabled(True)
        self.song_play.setText(tr("▶ Играть", "▶ Play"))
        if pcm:
            self._song_cache[key] = pcm
        if key == self._pending_key and pcm:        # играем только актуальный запрос
            self._start_play(pcm)

    def _start_play(self, pcm: bytes) -> None:
        fmt = QAudioFormat()
        fmt.setSampleRate(44100)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._mbuf = QBuffer()
        self._mbuf.setData(pcm)
        self._mbuf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._msink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt)
        self._msink.start(self._mbuf)

    def _export_song(self) -> None:
        row = self.song_list.currentRow()
        if row < 0 or self.main.rom is None or not self._banks:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить песню в WAV", "Save song to WAV"), f"song_{row:02d}.wav", "WAV (*.wav)")
        if not path:
            return
        from . import gems_player as gp
        wav = gp.render_song_wav(self.main.rom.data, self._banks, row,
                                 max_seconds=float(self.song_len.value()),
                                 transpose=self.song_transpose.value(), gain=6.0,
                                 ym2612_mode=self.song_chip.currentData(),
                                 speed=self.song_speed.value() / 100.0)
        with open(path, "wb") as f:
            f.write(wav)

    def _export_all_songs(self) -> None:
        if self.main.rom is None or not self._banks:
            return
        from . import gems_music as gm, gems_player as gp
        d = QFileDialog.getExistingDirectory(self, tr("Папка для экспорта всех песен", "Folder to export all songs"))
        if not d:
            return
        n = gm.song_count(self.main.rom.data, self._banks["sequences"])
        for i in range(n):
            wav = gp.render_song_wav(self.main.rom.data, self._banks, i,
                                     max_seconds=float(self.song_len.value()),
                                     transpose=self.song_transpose.value(), gain=6.0,
                                     ym2612_mode=self.song_chip.currentData(),
                                     speed=self.song_speed.value() / 100.0)
            with open(os.path.join(d, f"song_{i:02d}.wav"), "wb") as f:
                f.write(wav)
        QMessageBox.information(self, tr("Экспорт", "Export"), tr(f"Сохранено {n} песен (WAV) в:\n{d}", f"Saved {n} songs (WAV) in:\n{d}"))

    def _open_audition(self) -> None:
        if self.main.rom is None or not self._banks:
            return
        PatchAuditionDialog(self, self.main.rom, self._banks).show()


class PatchAuditionDialog(QDialog):
    """Прослушивание инструментов GEMS одной нотой. 30 FM-патчей ZT не звучат в музыке =
    это SFX (выбор меню, лифт, двери, граната, писк робота, выстрел, выбор оружия)."""

    def __init__(self, parent, rom, banks):
        super().__init__(parent)
        self.rom = rom
        self.banks = banks
        self._sink = None
        self._buf = None
        self.setWindowTitle(tr("Инструменты / SFX — прослушивание", "Instruments / SFX — preview"))
        self.resize(440, 600)
        from . import gems_player as gp
        self._used, self._all = gp.music_patch_usage(rom.data, banks)
        TN = {0: "FM", 1: "DAC", 2: "PSG", 3: "NOISE"}

        self.note = QSpinBox(); self.note.setRange(24, 96); self.note.setValue(60)
        self.note.setToolTip(tr("MIDI-нота (60 = до 4-й октавы). Меняй высоту SFX.", "MIDI note (60 = C of the 4th octave). Change the SFX pitch."))
        self.chip = QComboBox(); self.chip.addItem("YM2612", True); self.chip.addItem("YM3438", False)
        self.only_sfx = QCheckBox(tr("Только SFX (вне музыки)", "SFX only (outside music)"))
        self.only_sfx.stateChanged.connect(self._fill)
        self.list = QListWidget()
        self.list.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.list.itemDoubleClicked.connect(lambda *_: self._play())
        play = QPushButton(tr("▶ Играть нотой", "▶ Play as note")); play.clicked.connect(self._play)
        exp = QPushButton(tr("Экспорт WAV…", "Export WAV…")); exp.clicked.connect(self._export)
        self._tn = TN

        top = QFormLayout()
        top.addRow(tr("Нота:", "Note:"), self.note)
        top.addRow(tr("Чип:", "Chip:"), self.chip)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr("Двойной клик — сыграть инструмент одной нотой. "
                           "«♪» = звучит в музыке, «SFX?» = кандидат в звуковой эффект.", "Double-click to play the instrument with a single note. «♪» = sounds in music, «SFX?» = sound-effect candidate.")))
        v.addLayout(top)
        v.addWidget(self.only_sfx)
        v.addWidget(self.list, 1)
        row = QHBoxLayout(); row.addWidget(play); row.addWidget(exp)
        v.addLayout(row)
        self._fill()

    def _fill(self):
        self.list.clear()
        for i, t in self._all:
            if self.only_sfx.isChecked() and (i in self._used or t != 0):
                continue
            tag = tr("♪ в музыке", "♪ in music") if i in self._used else ("SFX?" if t == 0 else "—")
            it = QListWidgetItem(f"#{i:02d}   {self._tn.get(t, t):5}   {tag}")
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.list.addItem(it)

    def _cur_patch(self):
        it = self.list.currentItem()
        return None if it is None else it.data(Qt.ItemDataRole.UserRole)

    def _render(self):
        from . import gems_player as gp
        idx = self._cur_patch()
        if idx is None:
            return None
        return gp.render_patch_note(self.rom.data, self.banks, idx,
                                    note=self.note.value(), ym2612_mode=self.chip.currentData())

    def _play(self):
        if self._sink is not None:
            self._sink.stop(); self._sink = None
        pcm = self._render()
        if not pcm:
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(44100); fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._buf = QBuffer(); self._buf.setData(pcm)
        self._buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt)
        self._sink.start(self._buf)

    def _export(self):
        import io
        import wave
        idx = self._cur_patch()
        if idx is None:
            return
        pcm = self._render()
        if not pcm:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить", "Save"), f"patch_{idx:02d}.wav", "WAV (*.wav)")
        if not path:
            return
        buf = io.BytesIO(); w = wave.open(buf, "wb")
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(pcm); w.close()
        with open(path, "wb") as f:
            f.write(buf.getvalue())


class JukeboxViewer(QWidget):
    """Вкладка «Jukebox»: то же FM-воспроизведение, но с творческим управлением —
    подмена ИНСТРУМЕНТА (патча) и ТРАНСПОНИРОВАНИЕ каждого канала + рандомайзеры.
    Для поиска интересных вариаций треков под последующее переписывание/ремикс."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        self._banks = None
        self._fm_patches = []          # индексы FM-патчей (для выпадашек)
        self._chan_patch = []          # QComboBox по каналу
        self._chan_trans = []          # QSpinBox по каналу
        self._msink = None
        self._mbuf = None
        self._rthread = None
        self._pending_key = None

        self.info = QLabel(tr("Загрузите ROM (вкладка «Вся графика»)", "Load a ROM (the «All graphics» tab)"))
        self.info.setWordWrap(True)
        self.song_list = QListWidget()
        self.song_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.song_list.currentRowChanged.connect(self._on_song)
        self.song_list.itemDoubleClicked.connect(lambda *_: self._play())

        self.g_transpose = QSpinBox(); self.g_transpose.setRange(-36, 36); self.g_transpose.setValue(12)
        self.g_speed = QSpinBox(); self.g_speed.setRange(25, 200); self.g_speed.setValue(100); self.g_speed.setSuffix(" %")
        self.g_chip = QComboBox(); self.g_chip.addItem("YM2612", True); self.g_chip.addItem("YM3438", False)
        self.g_len = QSpinBox(); self.g_len.setRange(10, 300); self.g_len.setValue(90); self.g_len.setSuffix(tr(" с", " s"))
        gform = QFormLayout()
        gform.addRow(tr("Общий транспон.:", "Global transpose:"), self.g_transpose)
        gform.addRow(tr("Скорость:", "Speed:"), self.g_speed)
        gform.addRow(tr("Чип:", "Chip:"), self.g_chip)
        gform.addRow(tr("Макс. длина:", "Max length:"), self.g_len)

        self.btn_play = QPushButton(tr("▶ Играть", "▶ Play"))
        self.btn_play.clicked.connect(self._play)
        btn_stop = QPushButton(tr("⏹ Стоп", "⏹ Stop")); btn_stop.clicked.connect(self._stop)
        btn_exp = QPushButton(tr("Экспорт WAV…", "Export WAV…")); btn_exp.clicked.connect(self._export)
        rp = QPushButton(tr("🎲 патчи", "🎲 patches")); rp.clicked.connect(self._rand_patches)
        rt = QPushButton(tr("🎲 транспон.", "🎲 transpose")); rt.clicked.connect(self._rand_trans)
        rb = QPushButton(tr("🎲🎲 всё", "🎲🎲 all")); rb.clicked.connect(self._rand_both)
        rs = QPushButton(tr("♻ Сброс", "♻ Reset")); rs.clicked.connect(self._reset_overrides)

        # левая панель
        left = QWidget(); left.setFixedWidth(300)
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel(tr("<b>Jukebox (ремикс FM)</b>", "<b>Jukebox (FM remix)</b>")))
        lv.addWidget(self.info)
        lv.addWidget(self.song_list, 1)
        lv.addLayout(gform)
        r1 = QHBoxLayout(); r1.addWidget(rp); r1.addWidget(rt)
        r2 = QHBoxLayout(); r2.addWidget(rb); r2.addWidget(rs)
        r3 = QHBoxLayout(); r3.addWidget(self.btn_play); r3.addWidget(btn_stop)
        lv.addLayout(r1); lv.addLayout(r2); lv.addLayout(r3); lv.addWidget(btn_exp)

        # правая панель — каналы (динамически)
        self.chan_area = QWidget()
        self.chan_layout = QVBoxLayout(self.chan_area)
        self.chan_layout.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.chan_area)
        right = QVBoxLayout()
        right.addWidget(QLabel(tr("<b>Каналы трека</b> — подмени инструмент и/или транспонируй:", "<b>Track channels</b> — swap the instrument and/or transpose:")))
        right.addWidget(scroll, 1)

        lay = QHBoxLayout(self)
        lay.addWidget(left, 0)
        lay.addLayout(right, 1)

    def set_rom(self, rom) -> None:
        self._stop()
        self.song_list.clear()
        self._banks = None
        self._fm_patches = []
        self._clear_channels()
        if rom is None:
            return
        from . import gems, gems_music as gm, opn2_chip
        base, _ = gems.extract_samples(rom)
        try:
            self._banks = gm.find_gems_banks(rom.data, base)
        except Exception:
            self._banks = None
        if not self._banks or not opn2_chip.available():
            self.info.setText(tr("Музыка GEMS недоступна (нет банка или эмулятора YM2612; "
                                 "для звука см. ztextractor/opn2/README.md).",
                                 "GEMS music unavailable (no bank or YM2612 emulator; "
                                 "for sound see ztextractor/opn2/README.md)."))
            self._banks = None   # FM недоступен → обработчики трека/экспорта выходят
            return
        # список FM-патчей
        for i, addr in gm.patch_table(rom.data, self._banks["patches"]):
            if gm.instrument_type(rom.data, addr) == 0:
                self._fm_patches.append(i)
        nsong = gm.song_count(rom.data, self._banks["sequences"])
        self.info.setText(tr(f"<b>{nsong}</b> треков · <b>{len(self._fm_patches)}</b> FM-патчей. "
                          "Выбери трек, крути патчи/транспон., жми 🎲.", f"<b>{nsong}</b> tracks · <b>{len(self._fm_patches)}</b> FM patches. "
                          "Pick a track, tweak patches/transpose, hit 🎲."))
        for s in range(nsong):
            song = gm.parse_song(rom.data, self._banks["sequences"], s)
            ch = len(song["channels"]) if song else 0
            self.song_list.addItem(tr(f"Трек #{s:02d}   каналов: {ch}", f"Track #{s:02d}   channels: {ch}"))
        if nsong:
            self.song_list.setCurrentRow(0)

    # ── каналы выбранного трека ──
    def _clear_channels(self) -> None:
        while self.chan_layout.count():
            it = self.chan_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._chan_patch = []
        self._chan_trans = []

    def _on_song(self, row: int) -> None:
        self._clear_channels()
        if row < 0 or self.main.rom is None or not self._banks:
            self.chan_layout.addStretch(1)
            return
        from . import gems_music as gm
        song = gm.parse_song(self.main.rom.data, self._banks["sequences"], row)
        nch = len(song["channels"]) if song else 0
        for c in range(nch):
            box = QWidget(); hb = QHBoxLayout(box); hb.setContentsMargins(2, 1, 2, 1)
            hb.addWidget(QLabel(tr(f"К{c:02d}", f"C{c:02d}")))
            pc = QComboBox(); pc.addItem(tr("— ориг —", "— orig —"), None)
            for pi in self._fm_patches:
                pc.addItem(tr(f"патч {pi}", f"patch {pi}"), pi)
            ts = QSpinBox(); ts.setRange(-36, 36); ts.setValue(0)
            hb.addWidget(pc, 1); hb.addWidget(QLabel(tr("полутон:", "halftone:"))); hb.addWidget(ts)
            self.chan_layout.addWidget(box)
            self._chan_patch.append(pc)
            self._chan_trans.append(ts)
        self.chan_layout.addStretch(1)

    def _overrides(self):
        pov, tov = {}, {}
        for c in range(len(self._chan_patch)):
            p = self._chan_patch[c].currentData()
            if p is not None:
                pov[c] = p
            t = self._chan_trans[c].value()
            if t:
                tov[c] = t
        return pov, tov

    # ── рандомайзеры ──
    def _rand_patches(self) -> None:
        import random
        for pc in self._chan_patch:
            if self._fm_patches and random.random() < 0.7:
                idx = pc.findData(random.choice(self._fm_patches))
                pc.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                pc.setCurrentIndex(0)

    def _rand_trans(self) -> None:
        import random
        choices = [-24, -12, -12, -7, -5, 0, 0, 0, 5, 7, 12, 12, 24]
        for ts in self._chan_trans:
            ts.setValue(random.choice(choices))

    def _rand_both(self) -> None:
        self._rand_patches()
        self._rand_trans()

    def _reset_overrides(self) -> None:
        for pc in self._chan_patch:
            pc.setCurrentIndex(0)
        for ts in self._chan_trans:
            ts.setValue(0)

    # ── воспроизведение (фоновый рендер) ──
    def _stop(self) -> None:
        if self._msink is not None:
            self._msink.stop()
            self._msink = None

    def _play(self) -> None:
        row = self.song_list.currentRow()
        if row < 0 or self.main.rom is None or not self._banks:
            return
        self._stop()
        pov, tov = self._overrides()
        key = (row, self.g_transpose.value(), float(self.g_len.value()),
               self.g_chip.currentData(), self.g_speed.value() / 100.0,
               tuple(sorted(pov.items())), tuple(sorted(tov.items())))
        if self._rthread is not None and self._rthread.isRunning():
            return
        self._pending_key = key
        self.btn_play.setEnabled(False); self.btn_play.setText(tr("⏳ рендер…", "⏳ rendering…"))
        self._rthread = _SongRenderThread(
            self.main.rom.data, self._banks, row, key[2], key[1], key,
            ym2612_mode=key[3], speed=key[4], patch_overrides=pov, transpose_overrides=tov)
        self._rthread.rendered.connect(self._on_rendered)
        self._rthread.start()

    def _on_rendered(self, key, pcm) -> None:
        self.btn_play.setEnabled(True); self.btn_play.setText(tr("▶ Играть", "▶ Play"))
        if key == self._pending_key and pcm:
            fmt = QAudioFormat()
            fmt.setSampleRate(44100); fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self._mbuf = QBuffer(); self._mbuf.setData(pcm)
            self._mbuf.open(QIODevice.OpenModeFlag.ReadOnly)
            self._msink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt)
            self._msink.start(self._mbuf)

    def _export(self) -> None:
        row = self.song_list.currentRow()
        if row < 0 or self.main.rom is None or not self._banks:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить вариацию в WAV", "Save variation to WAV"), f"jukebox_{row:02d}.wav", "WAV (*.wav)")
        if not path:
            return
        pov, tov = self._overrides()
        from . import gems_player as gp
        wav = gp.render_song_wav(
            self.main.rom.data, self._banks, row, max_seconds=float(self.g_len.value()),
            transpose=self.g_transpose.value(), ym2612_mode=self.g_chip.currentData(),
            speed=self.g_speed.value() / 100.0, patch_overrides=pov, transpose_overrides=tov)
        with open(path, "wb") as f:
            f.write(wav)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(tr("Zero Tolerance — экстрактор данных", "Zero Tolerance — data extractor"))
        self.resize(1100, 760)

        self.rom: Optional[Rom] = None
        self._guard = False  # защита от рекурсии offset <-> scrollbar
        self._zones = []
        self._cur_zone = None
        self._sprites = []

        self.view = TileView()
        self._build_controls()
        self.view.selectionChanged.connect(self._update_range_label)
        self.view.hoverMoved.connect(self._update_hover)
        self._hover_off: Optional[int] = None

        tab_all = QWidget()
        layout = QHBoxLayout(tab_all)
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setWidget(self.controls)
        ctrl_scroll.setFixedWidth(370)   # 350 + место под вертикальный скроллбар
        ctrl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(ctrl_scroll, 0)
        layout.addWidget(self.view, 1)

        self.frame_tab = FrameViewer(self)
        self.object_tab = ObjectViewer(self)
        self.icon_tab = IconViewer(self)
        self.weapon_tab = WeaponViewer(self)
        self.bg_tab = BackgroundViewer(self)
        self.idcard_tab = IdCardViewer(self)
        self.screen_tab = ScreenViewer(self)
        self.other_tab = OtherScreenViewer(self)
        self.font_tab = FontViewer(self)
        self.text_tab = TextViewer(self)
        self.wall_tab = WallViewer(self)
        self.map_tab = MapViewer(self)
        self.sound_tab = SoundViewer(self)
        self.jukebox_tab = JukeboxViewer(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(tab_all, tr("Вся графика", "All graphics"))
        self.tabs.addTab(self.frame_tab, tr("Кадры врагов", "Enemy frames"))
        self.tabs.addTab(self.object_tab, tr("Предметы / объекты", "Items / objects"))
        self.tabs.addTab(self.icon_tab, tr("Иконки", "Icons"))
        self.tabs.addTab(self.weapon_tab, tr("Оружие в руках", "Held weapons"))
        self.tabs.addTab(self.bg_tab, tr("Фоны", "Backgrounds"))
        self.tabs.addTab(self.idcard_tab, tr("ID-карты", "ID cards"))
        self.tabs.addTab(self.screen_tab, tr("Экраны", "Screens"))
        self.tabs.addTab(self.other_tab, tr("Прочие экраны", "Other screens"))
        self.tabs.addTab(self.font_tab, tr("Шрифты", "Fonts"))
        self.tabs.addTab(self.text_tab, tr("Текст", "Text"))
        self.tabs.addTab(self.wall_tab, tr("Стены / двери", "Walls / doors"))
        self.tabs.addTab(self.map_tab, tr("Карты", "Maps"))
        self.tabs.addTab(self.sound_tab, tr("Звук", "Sound"))
        self.tabs.addTab(self.jukebox_tab, "Jukebox")
        self.setCentralWidget(self.tabs)
        self._build_menu()
        self._set_enabled(False)

    # ---------- меню языка / language menu ----------

    def _build_menu(self) -> None:
        bar = self.menuBar()
        bar.setNativeMenuBar(False)  # меню В ОКНЕ на всех ОС (на macOS иначе уходит в системную полосу)
        menu = bar.addMenu(tr("Язык", "Language"))
        grp = QActionGroup(self)
        grp.setExclusive(True)
        for code, label in (("en", "English"), ("ru", tr("Русский", "Russian"))):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(get_lang() == code)
            act.triggered.connect(lambda _checked=False, c=code: self._switch_lang(c))
            grp.addAction(act)
            menu.addAction(act)

    def _switch_lang(self, code: str) -> None:
        if code == get_lang():
            return
        QSettings("ztextractor", "ztextractor").setValue("lang", code)
        # предупреждение всегда двуязычное (английский первым) / always bilingual, English first
        QMessageBox.information(
            self, "Language / Язык",
            "Language changed. Restart the app to apply.\n"
            "Язык изменён. Перезапустите программу, чтобы применить.")

    # ---------- построение панели управления ----------

    def _build_controls(self) -> None:
        self.controls = QWidget()
        self.controls.setFixedWidth(350)
        v = QVBoxLayout(self.controls)

        # видимый переключатель языка (меню «Language» на macOS уходит в системную строку)
        # visible language switcher (on macOS the «Language» menu lives in the top menu bar)
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(tr("Язык:", "Language:")))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("English", "en")
        self.lang_combo.addItem(tr("Русский", "Russian"), "ru")
        self.lang_combo.setCurrentIndex(0 if get_lang() == "en" else 1)
        self.lang_combo.currentIndexChanged.connect(
            lambda i: self._switch_lang(self.lang_combo.itemData(i)))
        lang_row.addWidget(self.lang_combo, 1)
        v.addLayout(lang_row)

        self.open_btn = QPushButton(tr("Открыть ROM…", "Open ROM…"))
        self.open_btn.clicked.connect(self.open_rom)
        v.addWidget(self.open_btn)

        self.info = QLabel(tr("ROM не загружен", "No ROM loaded"))
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([tr("авто", "auto"), "raw (.bin)", "SMD"])
        self.mode_combo.currentIndexChanged.connect(self._reload)
        form.addRow(tr("Формат файла:", "File format:"), self.mode_combo)

        self.gfx_combo = QComboBox()
        # 0 = 32×32 column-major (стены), 1 = 8×8 linear (MD-тайлы),
        # 2 = 8×8 column-major 4×4 (иконки/оружие), 3 = произвольный N×M column-major
        self.gfx_combo.addItems([
            tr("32×32 column-major (стены)", "32×32 column-major (walls)"),
            tr("8×8 linear (MD-тайлы)", "8×8 linear (MD tiles)"),
            tr("8×8 column-major 4×4 (иконки)", "8×8 column-major 4×4 (icons)"),
            tr("N×M column-major (свой блок)", "N×M column-major (custom block)"),
        ])
        self.gfx_combo.currentIndexChanged.connect(self._gfx_changed)
        form.addRow(tr("Формат графики:", "Graphics format:"), self.gfx_combo)

        # размеры блока для формата «N×M column-major» (в тайлах 8×8)
        self.block_w = QSpinBox(); self.block_w.setRange(1, 16); self.block_w.setValue(4)
        self.block_h = QSpinBox(); self.block_h.setRange(1, 16); self.block_h.setValue(4)
        self.block_w.valueChanged.connect(self._layout_changed)
        self.block_h.valueChanged.connect(self._layout_changed)
        brow = QHBoxLayout()
        brow.addWidget(QLabel(tr("Ш:", "W:"))); brow.addWidget(self.block_w)
        brow.addWidget(QLabel(tr("В:", "H:"))); brow.addWidget(self.block_h)
        self.block_row = QWidget(); self.block_row.setLayout(brow)
        form.addRow(tr("Блок (тайлов):", "Block (tiles):"), self.block_row)
        self.block_row.setVisible(False)

        self.zt_bank = QComboBox()
        self.zt_bank.addItem(tr("— банк/область —", "— bank/region —"), None)
        self.zt_bank.currentIndexChanged.connect(self._bank_changed)
        form.addRow(tr("Банк:", "Bank:"), self.zt_bank)

        self.sprite = QComboBox()
        self.sprite.addItem(tr("— спрайт/объект —", "— sprite/object —"), None)
        self.sprite.currentIndexChanged.connect(self._sprite_changed)
        form.addRow(tr("Спрайт:", "Sprite:"), self.sprite)

        self.offset = _hex_spin(0)
        self.offset.valueChanged.connect(self._offset_changed)
        form.addRow(tr("Смещение:", "Offset:"), self.offset)

        self._nudge_btns = []
        nud = QHBoxLayout()
        for label, delta in [("−512", -512), ("−32", -32), ("−2", -2),
                             ("+2", 2), ("+32", 32), ("+512", 512)]:
            b = QPushButton(label)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nud.addWidget(b)
            self._nudge_btns.append(b)
        form.addRow(tr("Подстройка:", "Nudge:"), nud)

        self.per_row = QSpinBox()
        self.per_row.setRange(1, 64)
        self.per_row.setValue(16)
        self.per_row.valueChanged.connect(self._layout_changed)
        form.addRow(tr("Тайлов в ряд:", "Tiles per row:"), self.per_row)

        self.rows = QSpinBox()
        self.rows.setRange(1, 256)
        self.rows.setValue(32)
        self.rows.valueChanged.connect(self._layout_changed)
        form.addRow(tr("Рядов:", "Rows:"), self.rows)

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 16)
        self.zoom.setValue(3)
        self.zoom.valueChanged.connect(lambda z: self.view.set_zoom(z))
        form.addRow(tr("Зум:", "Zoom:"), self.zoom)
        v.addLayout(form)

        # --- палитра ---
        pal_box = QGroupBox(tr("Палитра", "Palette"))
        pf = QFormLayout(pal_box)
        self.pal_mode = QComboBox()
        self.pal_mode.addItems([tr("Оттенки серого", "Grayscale"), tr("Из ROM", "From ROM"), "Zero Tolerance"])
        self.pal_mode.currentIndexChanged.connect(self._render)
        pf.addRow(tr("Источник:", "Source:"), self.pal_mode)

        self.episode = QComboBox()
        self.episode.addItem(tr("— зона —", "— zone —"), None)
        self.episode.currentIndexChanged.connect(self._episode_changed)
        pf.addRow(tr("Зона:", "Zone:"), self.episode)

        self.dark_face = QCheckBox(tr("Тёмная грань (B) — затенение поворота", "Dark face (B) — turn shading"))
        self.dark_face.stateChanged.connect(self._zone_pal_apply)
        pf.addRow(self.dark_face)

        self.pal_offset = _hex_spin(0)
        self.pal_offset.valueChanged.connect(self._pal_offset_changed)
        pf.addRow(tr("Смещение пал.:", "Palette offset:"), self.pal_offset)

        self._pal_nudge_btns = []
        pnud = QHBoxLayout()
        for label, delta in [("−32", -32), ("−2", -2), ("+2", 2), ("+32", 32)]:
            b = QPushButton(label)
            b.setFixedWidth(46)
            b.clicked.connect(lambda _=False, d=delta: self._pal_nudge(d))
            pnud.addWidget(b)
            self._pal_nudge_btns.append(b)
        pf.addRow(tr("Подстройка:", "Nudge:"), pnud)

        self.find_pal_btn = QPushButton(tr("Найти палитры в ROM", "Find palettes in ROM"))
        self.find_pal_btn.clicked.connect(self.find_palettes)
        pf.addRow(self.find_pal_btn)

        self.pal_found = QComboBox()
        self.pal_found.addItem(tr("— кандидаты —", "— candidates —"), None)
        self.pal_found.currentIndexChanged.connect(self._found_pal_changed)
        self.pal_prev = QPushButton("◀")
        self.pal_prev.setFixedWidth(32)
        self.pal_prev.setToolTip(tr("Предыдущая найденная палитра", "Previous found palette"))
        self.pal_prev.clicked.connect(lambda: self._found_pal_step(-1))
        self.pal_next = QPushButton("▶")
        self.pal_next.setFixedWidth(32)
        self.pal_next.setToolTip(tr("Следующая найденная палитра", "Next found palette"))
        self.pal_next.clicked.connect(lambda: self._found_pal_step(1))
        frow = QHBoxLayout()
        frow.addWidget(self.pal_found, 1)
        frow.addWidget(self.pal_prev)
        frow.addWidget(self.pal_next)
        pf.addRow(tr("Найдено:", "Found:"), frow)

        self.transparent = QCheckBox(tr("Прозрачный индекс:", "Transparent index:"))
        self.transparent.setChecked(True)
        self.transparent.stateChanged.connect(self._render)
        self.trans_index = QSpinBox()
        self.trans_index.setRange(0, 15)
        self.trans_index.valueChanged.connect(self._render)
        trow = QHBoxLayout()
        trow.addWidget(self.transparent)
        trow.addWidget(self.trans_index)
        pf.addRow(trow)

        self.shade = QSlider(Qt.Orientation.Horizontal)
        self.shade.setRange(10, 100)
        self.shade.setValue(100)
        self.shade.setToolTip(tr("Имитация затенения по дистанции (100% — ближняя стена)",
                                 "Distance-shading simulation (100% = nearest wall)"))
        self.shade.valueChanged.connect(self._render)
        srow = QHBoxLayout()
        srow.addWidget(self.shade, 1)
        self.shade_lbl = QLabel("100%")
        self.shade_lbl.setFixedWidth(38)
        srow.addWidget(self.shade_lbl)
        pf.addRow(tr("Затенение:", "Shading:"), srow)

        self.swatch = QLabel()
        self.swatch.setFixedHeight(18)
        pf.addRow(tr("Цвета:", "Colors:"), self.swatch)
        v.addWidget(pal_box)

        # --- скроллбар по всему ROM ---
        self.scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.scroll.valueChanged.connect(self._scroll_changed)
        v.addWidget(QLabel(tr("Прокрутка по ROM:", "Scroll through ROM:")))
        v.addWidget(self.scroll)

        v.addStretch(1)

        self.range_lbl = QLabel(tr("Смещения: —", "Offsets: —"))
        self.range_lbl.setWordWrap(True)
        self.range_lbl.setStyleSheet(
            "font-family: monospace; font-size: 11px; "
            "background:#222; color:#9f9; padding:4px;")
        v.addWidget(self.range_lbl)

        self.copy_btn = QPushButton(tr("Скопировать данные выделения", "Copy selection data"))
        self.copy_btn.clicked.connect(self.copy_selection)
        v.addWidget(self.copy_btn)

        self.map_btn = QPushButton(tr("Карта ROM… (обзор PNG)", "ROM map… (PNG overview)"))
        self.map_btn.clicked.connect(self.save_map)
        v.addWidget(self.map_btn)

        self.export_bar = GridExportBar(self)
        v.addWidget(self.export_bar)

        hint = QLabel(tr(
            "Совет: выделите область мышью — экспортируется она; без выделения — весь лист. "
            "«Весь ряд/столбец» расширяет выделение; экспорт — одним PNG или по ячейкам с масштабом.",
            "Tip: select an area with the mouse to export just it; with no selection the whole sheet is "
            "exported. «Whole row/column» extends the selection; export as one PNG or per-cell with scaling."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(hint)

    def grid_state(self):
        if not self.rom:
            return None
        colors = self._current_palette()
        buf, w, h = self._decode()
        img = build_qimage(buf, w, h, colors, self._transparent_index())
        uw, uh = self._unit_px_wh()
        grid = GridSpec(uw, uh, self.per_row.value(), self.rows.value())
        base = (os.path.splitext(os.path.basename(self.rom.path))[0]
                + f"_{self.offset.value():06X}")
        return img, grid, base

    def grid_cell_label(self, c, r):
        t = r * self.per_row.value() + c
        off = self.offset.value() + t * self._unit_bytes()
        return f"u{t:03d}_{off:06X}"

    # ---------- состояние ----------

    def _set_enabled(self, on: bool) -> None:
        for w in (self.mode_combo, self.gfx_combo, self.block_w, self.block_h,
                  self.offset, self.per_row,
                  self.rows, self.zoom, self.pal_mode, self.pal_offset,
                  self.transparent, self.trans_index, self.find_pal_btn,
                  self.pal_found, self.pal_prev, self.pal_next, self.episode,
                  self.dark_face, self.shade, self.scroll, self.export_bar,
                  self.map_btn, self.copy_btn,
                  *self._nudge_btns, *self._pal_nudge_btns):
            w.setEnabled(on)
        self.zt_bank.setEnabled(on and self._is_zt())
        self.sprite.setEnabled(on and bool(self._sprites))

    def _is_zt(self) -> bool:
        """Совместимость: формат 0 = 32×32 column-major (банки стен)."""
        return self.gfx_combo.currentIndex() == 0

    def _block_tiles(self):
        """(ширина, высота) одного блока в 8×8-тайлах для текущего формата."""
        idx = self.gfx_combo.currentIndex()
        if idx == 0:               # 32×32 column-major = 4×4 тайла, но свой декодер
            return 4, 4
        if idx == 1:               # 8×8 linear
            return 1, 1
        if idx == 2:               # 8×8 column-major 4×4 (32×32)
            return 4, 4
        return self.block_w.value(), self.block_h.value()   # произвольный N×M

    def _unit_px_wh(self):
        """(ширина_px, высота_px) одного блока-юнита."""
        bw, bh = self._block_tiles()
        if self.gfx_combo.currentIndex() == 0:
            return tiles.ZT_TEX, tiles.ZT_TEX     # 32×32 strip
        return bw * tiles.TILE_W, bh * tiles.TILE_H

    def _unit_px(self) -> int:
        """Ширина юнита в px (для квадратных форматов = и высота)."""
        return self._unit_px_wh()[0]

    def _unit_bytes(self) -> int:
        idx = self.gfx_combo.currentIndex()
        if idx == 0:
            return tiles.ZT_TEX_BYTES             # 512
        if idx == 1:
            return tiles.TILE_BYTES               # 32
        bw, bh = self._block_tiles()
        return bw * bh * tiles.TILE_BYTES         # N*M*32

    def _decode(self):
        idx = self.gfx_combo.currentIndex()
        data = self.rom.data
        off = self.offset.value()
        per_row = self.per_row.value()
        rows = self.rows.value()
        if idx == 0:
            return tiles.decode_zt_sheet(data, off, per_row, rows)
        if idx == 1:
            return tiles.decode_sheet(data, off, per_row, rows)
        bw, bh = self._block_tiles()
        return tiles.decode_colmajor_sheet(data, off, per_row, rows, bw, bh)

    def _row_bytes(self) -> int:
        return self.per_row.value() * self._unit_bytes()

    def _sheet_bytes(self) -> int:
        return self._row_bytes() * self.rows.value()

    # ---------- загрузка ----------

    def open_rom(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Открыть ROM Mega Drive", "Open Mega Drive ROM"), "",
            tr("ROM Mega Drive (*.bin *.md *.gen *.smd);;Все файлы (*)", "Mega Drive ROM (*.bin *.md *.gen *.smd);;All files (*)"),
        )
        if path:
            self._load(path)

    def _reload(self) -> None:
        if self.rom:
            self._load(self.rom.path)

    def _load(self, path: str) -> None:
        mode = {0: "auto", 1: "raw", 2: "smd"}[self.mode_combo.currentIndex()]
        try:
            self.rom = load_rom(path, mode)
        except OSError as e:
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr(f"Не удалось открыть файл:\n{e}", f"Failed to open file:\n{e}"))
            return

        size = self.rom.size
        self.info.setText(
            tr(f"<b>{os.path.basename(path)}</b><br>"
            f"Размер: {size:,} байт ({size // 1024} КБ)<br>"
            f"Формат: {'SMD (развёрнут)' if self.rom.is_smd else 'raw'}<br>"
            f"Название: {self.rom.title or '—'}<br>"
            f"Версия: <b>{version_label(self.rom.version)}</b>", f"<b>{os.path.basename(path)}</b><br>"
            f"Size: {size:,} bytes ({size // 1024} KB)<br>"
            f"Format: {'SMD (deinterleaved)' if self.rom.is_smd else 'raw'}<br>"
            f"Title: {self.rom.title or '—'}<br>"
            f"Version: <b>{version_label(self.rom.version)}</b>")
        )
        self.pal_found.blockSignals(True)
        self.pal_found.clear()
        self.pal_found.addItem(tr("— кандидаты —", "— candidates —"), None)
        self.pal_found.blockSignals(False)

        self._guard = True
        self.offset.setMaximum(max(0, size - 1))
        self.pal_offset.setMaximum(max(0, size - 2))
        self.scroll.setRange(0, max(0, size - 1))
        self.scroll.setSingleStep(self._row_bytes())
        self.scroll.setPageStep(self._sheet_bytes())
        self._guard = False

        self._set_enabled(True)
        self.view.set_zoom(self.zoom.value())
        self._apply_version_defaults()
        self._render()
        self.frame_tab.set_rom(self.rom, self._sprites)
        self.object_tab.set_rom(self.rom)
        self.icon_tab.set_rom(self.rom)
        self.weapon_tab.set_rom(self.rom)
        self.bg_tab.set_rom(self.rom)
        self.idcard_tab.set_rom(self.rom)
        self.screen_tab.set_rom(self.rom)
        self.other_tab.set_rom(self.rom)
        self.font_tab.set_rom(self.rom)
        self.text_tab.set_rom(self.rom)
        self.wall_tab.set_rom(self.rom)
        self.map_tab.set_rom(self.rom)
        self.sound_tab.set_rom(self.rom)
        self.jukebox_tab.set_rom(self.rom)

    def _populate_banks(self, items) -> None:
        """items: список (name, offset, count|None). count — число текстур банка."""
        self.zt_bank.blockSignals(True)
        self.zt_bank.clear()
        self.zt_bank.addItem(tr("— банк —", "— bank —"), None)
        for name, off, count in items:
            self.zt_bank.addItem(name, (off, count))
        self.zt_bank.blockSignals(False)

    def _apply_version_defaults(self) -> None:
        """Настроить режим/зоны/банки/палитру под распознанную версию игры."""
        ver = self.rom.version
        self._cur_zone = None
        self._guard = True
        self.gfx_combo.setCurrentIndex(0)   # текстуры 32×32 по умолчанию
        self.per_row.setValue(8)
        self.zt_bank.setEnabled(True)

        # зоны (из таблицы дескрипторов прототипа) -> список «Зона N (цвет)»
        self._zones = zonemod.parse_zones(self.rom.data, ver)
        self.episode.blockSignals(True)
        self.episode.clear()
        self.episode.addItem(tr("— зона —", "— zone —"), None)
        for z in self._zones:
            self.episode.addItem(
                tr(f"Зона {z['index'] + 1} ({len(z['banks'])} банков)", f"Zone {z['index'] + 1} ({len(z['banks'])} banks)"), z["index"])
        self.episode.blockSignals(False)
        self.episode.setEnabled(bool(self._zones))
        self.dark_face.setEnabled(bool(self._zones))
        self.dark_face.setChecked(False)

        # спрайты объектов/врагов (только BZT)
        self._sprites = spritemod.find_sprites(self.rom.data)
        self.sprite.blockSignals(True)
        self.sprite.clear()
        self.sprite.addItem(tr("— спрайт/объект —", "— sprite/object —"), None)
        for s in self._sprites:
            kind = tr("враг", "enemy") if s.get("enemy") else tr("объект", "object")
            self.sprite.addItem(
                tr(f"{s['offset']:#08x} — {s['frames']} кадр. ({kind})", f"{s['offset']:#08x} — {s['frames']} frames ({kind})"), s["offset"])
        self.sprite.blockSignals(False)
        self.sprite.setEnabled(bool(self._sprites))

        if ver.startswith("Zero Tolerance"):
            self._populate_banks(list(tiles.ZT_BANKS))
            self.pal_mode.setCurrentIndex(2)  # встроенная палитра ZT
        else:
            self.pal_mode.setCurrentIndex(0)

        self._guard = False

        if self._zones:
            self.episode.setCurrentIndex(1)   # зона 0: палитра + список банков
        if self.zt_bank.count() > 1:
            self.zt_bank.setCurrentIndex(1)   # начальный прыжок на первый банк

    # ---------- синхронизация смещения ----------

    def _offset_changed(self, value: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.scroll.setValue(value)
        self._guard = False
        self._render()

    def _scroll_changed(self, value: int) -> None:
        if self._guard:
            return
        # Привязка к границе юнита с СОХРАНЕНИЕМ фазы текущего смещения: иначе при
        # перетаскивании ползунка/клике по треку смещение становится невыровненным и
        # сетка текстур «уезжает» (банки стен вроде 0x12EF26 не кратны 512).
        unit = self._unit_bytes()
        phase = self.offset.value() % unit
        snapped = phase + ((value - phase) // unit) * unit
        snapped = max(0, min(self.offset.maximum(), snapped))
        self._guard = True
        self.offset.setValue(snapped)
        if self.scroll.value() != snapped:
            self.scroll.setValue(snapped)   # подтянуть ползунок к выровненной позиции
        self._guard = False
        self._render()

    def _layout_changed(self) -> None:
        self.scroll.setSingleStep(self._row_bytes())
        self.scroll.setPageStep(self._sheet_bytes())
        self._render()

    def _gfx_changed(self) -> None:
        idx = self.gfx_combo.currentIndex()
        # банки стен — только для формата 32×32 column-major
        self.zt_bank.setEnabled(idx == 0)
        # строка размеров блока — только для произвольного N×M
        self.block_row.setVisible(idx == 3)
        self._guard = True
        # удобный шаг сетки по умолчанию под формат
        self.per_row.setValue({0: 8, 1: 16, 2: 8, 3: 8}.get(idx, 8))
        self._guard = False
        self._layout_changed()

    def _bank_changed(self) -> None:
        bank = self.zt_bank.currentData()
        if bank is None:
            return
        off, count = bank
        if off >= 0x200000 and self.gfx_combo.currentIndex() != 0:
            # верхний 1 МБ July — спрайты 32×32 column-major; в 8×8 видны лишь полоски-мусор
            self.gfx_combo.setCurrentIndex(0)
        if count:  # подогнать сетку ровно под банк: 16 в ряд, нужное число рядов
            per_row = 16
            self._guard = True
            self.per_row.setValue(per_row)
            self.rows.setValue(max(1, -(-count // per_row)))
            self.scroll.setSingleStep(self._row_bytes())
            self.scroll.setPageStep(self._sheet_bytes())
            self._guard = False
        self.offset.setValue(off)  # _offset_changed выполнит рендер

    def _nudge(self, delta: int) -> None:
        new = max(0, min(self.offset.maximum(), self.offset.value() + delta))
        self.offset.setValue(new)

    def _pal_offset_changed(self) -> None:
        if self.pal_mode.currentIndex() != 1:
            self._guard = True
            self.pal_mode.setCurrentIndex(1)  # переключиться на «Из ROM»
            self._guard = False
        self._render()

    def _pal_nudge(self, delta: int) -> None:
        new = max(0, min(self.pal_offset.maximum(), self.pal_offset.value() + delta))
        self.pal_offset.setValue(new)

    def _found_pal_step(self, step: int) -> None:
        """Перейти к предыдущей/следующей найденной палитре (кнопки ◀ ▶)."""
        n = self.pal_found.count()
        if n <= 1:
            self.statusBar().showMessage(
                tr("Сначала нажмите «Найти палитры в ROM».", "First click «Find palettes in ROM»."), 4000)
            return
        cur = self.pal_found.currentIndex()
        if cur < 1:  # пропускаем плейсхолдер на индексе 0
            nxt = 1 if step > 0 else n - 1
        else:
            nxt = max(1, min(n - 1, cur + step))
        self.pal_found.setCurrentIndex(nxt)  # вызовет перекраску

    def _episode_changed(self) -> None:
        zi = self.episode.currentData()
        if zi is None:
            return
        self._cur_zone = self._zones[zi]
        banks = self._cur_zone["banks"]
        items = [(tr(f"{b['offset']:#08x} — {b['count']} тек.", f"{b['offset']:#08x} — {b['count']} tex."), b["offset"], b["count"])
                 for b in banks]
        # July: дропдаун зон-based и не доходит до верхнего 1 МБ — доклеиваем landmark'и на
        # доп.спрайты 0x200000+ (выбор такого банка авто-ставит формат 32×32, см. _bank_changed).
        if self.rom and "1995-07-14" in self.rom.version:
            items += [(nm, off, cnt) for nm, off, cnt in tiles.BZT_JUL_POINTS if off >= 0x200000]
        self._populate_banks(items)
        # только перекраска (палитра зоны), без прыжка на банк — вид не скроллится
        self._zone_pal_apply()

    def _sprite_changed(self) -> None:
        off = self.sprite.currentData()
        if off is None:
            return
        # спрайт: таблица кадров + графика ниже. Показываем широкий вертикальный
        # срез, 8 текстур в ряд, чтобы видеть и «последовательность», и тайлы.
        self._guard = True
        self.per_row.setValue(8)
        self.rows.setValue(48)
        self.scroll.setSingleStep(self._row_bytes())
        self.scroll.setPageStep(self._sheet_bytes())
        self._guard = False
        self.offset.setValue(off)  # _offset_changed выполнит рендер

    def _zone_pal_apply(self) -> None:
        """Поставить палитру текущей зоны (грань A светлая / B тёмная)."""
        if not self._cur_zone:
            return
        off = self._cur_zone["palB"] if self.dark_face.isChecked() \
            else self._cur_zone["palA"]
        self._guard = True
        self.pal_mode.setCurrentIndex(1)
        self.pal_offset.setValue(off)
        self._guard = False
        self._render()

    def _found_pal_changed(self) -> None:
        off = self.pal_found.currentData()
        if off is None:
            return
        self._guard = True
        self.pal_mode.setCurrentIndex(1)
        self.pal_offset.setValue(off)
        self._guard = False
        self._render()

    def find_palettes(self) -> None:
        if not self.rom:
            return
        cands = pal.find_palettes(self.rom.data)
        self.pal_found.blockSignals(True)
        self.pal_found.clear()
        self.pal_found.addItem(tr(f"— найдено {len(cands)} —", f"— {len(cands)} found —"), None)
        for off in cands:
            self.pal_found.addItem(f"{off:#08x}", off)
        self.pal_found.blockSignals(False)
        self.statusBar().showMessage(
            tr(f"Найдено кандидатов палитр: {len(cands)}. Перебирайте список — "
            f"картинка перекрашивается вживую.", f"Palette candidates found: {len(cands)}. Browse the list — "
            f"the image recolors live."), 8000)

    # ---------- рендер ----------

    def _transparent_index(self) -> Optional[int]:
        return self.trans_index.value() if self.transparent.isChecked() else None

    # ---------- смещения под курсором / выделения ----------

    def _texel_at(self, sx: float, sy: float) -> Optional[int]:
        """ROM-смещение текстуры/тайла под точкой сцены (px), либо None."""
        if not self.rom:
            return None
        uw, uh = self._unit_px_wh()
        per_row = self.per_row.value()
        col = int(sx) // uw
        row = int(sy) // uh
        if col < 0 or col >= per_row or row < 0 or row >= self.rows.value():
            return None
        t = row * per_row + col
        return self.offset.value() + t * self._unit_bytes()

    def _update_hover(self, sx: float, sy: float) -> None:
        self._hover_off = self._texel_at(sx, sy)
        self._update_range_label()

    def _sel_offsets(self):
        """(start, end_exclusive, n_units) для текущего выделения, либо None."""
        sel = self.view.selection
        if sel is None or sel.width() < 1 or sel.height() < 1:
            return None
        uw, uh = self._unit_px_wh()
        ub = self._unit_bytes()
        per_row = self.per_row.value()
        rows = self.rows.value()
        x0 = max(0, int(sel.left()) // uw)
        y0 = max(0, int(sel.top()) // uh)
        x1 = min(per_row - 1, int(sel.right() - 0.001) // uw)
        y1 = min(rows - 1, int(sel.bottom() - 0.001) // uh)
        if x1 < x0 or y1 < y0:
            return None
        t0 = y0 * per_row + x0
        t1 = y1 * per_row + x1
        base = self.offset.value()
        return base + t0 * ub, base + (t1 + 1) * ub, (t1 - t0 + 1)

    def copy_selection(self) -> None:
        """Скопировать в буфер обмена смещение выделенной области."""
        if not self.rom:
            return
        so = self._sel_offsets()
        if not so:
            self.statusBar().showMessage(tr("Сначала выделите область рамкой", "First select an area with the box"), 4000)
            return
        start, end, _n = so
        text = f"0x{start:06X}–0x{end:06X}"
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(tr(f"Скопировано: {text}", f"Copied: {text}"), 5000)

    def _snapped_sel_rect(self):
        """Прямоугольник выделения, привязанный к сетке текстур (px сцены)."""
        sel = self.view.selection
        if sel is None or sel.width() < 1 or sel.height() < 1:
            return None
        uw, uh = self._unit_px_wh()
        per_row = self.per_row.value()
        rows = self.rows.value()
        x0 = max(0, int(sel.left()) // uw)
        y0 = max(0, int(sel.top()) // uh)
        x1 = min(per_row - 1, int(sel.right() - 0.001) // uw)
        y1 = min(rows - 1, int(sel.bottom() - 0.001) // uh)
        if x1 < x0 or y1 < y0:
            return None
        return QRectF(x0 * uw, y0 * uh,
                      (x1 - x0 + 1) * uw, (y1 - y0 + 1) * uh)

    def _update_range_label(self) -> None:
        self.view.set_selection_rect(self._snapped_sel_rect() if self.rom else None)
        if not self.rom:
            self.range_lbl.setText(tr("Смещения: —", "Offsets: —"))
            return
        base = self.offset.value()
        end = base + self._sheet_bytes()
        lines = [tr(f"Вид: 0x{base:06X}–0x{end:06X}", f"View: 0x{base:06X}–0x{end:06X}")]
        if self._hover_off is not None:
            lines.append(tr(f"Курсор: 0x{self._hover_off:06X}", f"Cursor: 0x{self._hover_off:06X}"))
        sel = self._sel_offsets()
        if sel:
            s, e, nunits = sel
            unit_name = tr("тек.", "cur.") if self.gfx_combo.currentIndex() in (0, 1) else tr("блок.", "block.")
            lines.append(tr(f"Выделение: 0x{s:06X}–0x{e:06X} ({nunits} {unit_name})", f"Selection: 0x{s:06X}–0x{e:06X} ({nunits} {unit_name})"))
        self.range_lbl.setText("   ".join(lines))

    def _current_palette(self) -> List[pal.RGB]:
        idx = self.pal_mode.currentIndex()
        if self.rom and idx == 1:
            colors = pal.read_palette(self.rom.data, self.pal_offset.value())
        elif idx == 2:
            colors = pal.zt_palette()
        else:
            colors = pal.grayscale_palette()
        f = self.shade.value()
        self.shade_lbl.setText(f"{f}%")
        if f != 100:  # имитация затенения по дистанции
            colors = [(r * f // 100, g * f // 100, b * f // 100)
                      for (r, g, b) in colors]
        return colors

    def _update_swatch(self, colors: List[pal.RGB]) -> None:
        cell = 16
        img = QImage(cell * 16, cell, QImage.Format.Format_RGB32)
        for i, (r, g, b) in enumerate(colors):
            for x in range(cell):
                for y in range(cell):
                    img.setPixel(i * cell + x, y, (r << 16) | (g << 8) | b)
        self.swatch.setPixmap(QPixmap.fromImage(img).scaledToHeight(18))

    def _render(self) -> None:
        if not self.rom:
            return
        colors = self._current_palette()
        buf, w, h = self._decode()
        img = build_qimage(buf, w, h, colors, self._transparent_index())
        self.view.clear_selection()
        self.view.set_pixmap(QPixmap.fromImage(img))
        self._update_swatch(colors)
        self._update_range_label()

    # ---------- экспорт ----------

    def export_png(self) -> None:
        if not self.rom:
            return
        colors = self._current_palette()
        buf, w, h = self._decode()
        img = build_qimage(buf, w, h, colors, self._transparent_index())

        sel = self.view.selection
        if sel is not None and sel.width() >= 1 and sel.height() >= 1:
            # привязка к сетке юнита (по ширине/высоте блока) и обрезка по границам листа
            uw, uh = self._unit_px_wh()
            x0 = max(0, int(sel.left()) // uw * uw)
            y0 = max(0, int(sel.top()) // uh * uh)
            x1 = min(w, -(-int(sel.right()) // uw) * uw)
            y1 = min(h, -(-int(sel.bottom()) // uh) * uh)
            if x1 > x0 and y1 > y0:
                img = img.copy(x0, y0, x1 - x0, y1 - y0)

        base = os.path.splitext(os.path.basename(self.rom.path))[0]
        suggested = f"{base}_{self.offset.value():06X}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить PNG", "Save PNG"), suggested, "PNG (*.png)"
        )
        if not path:
            return
        out = img.convertToFormat(QImage.Format.Format_ARGB32)
        if out.save(path, "PNG"):
            self.statusBar().showMessage(tr(f"Сохранено: {path}", f"Saved: {path}"), 5000)
        else:
            QMessageBox.critical(self, tr("Ошибка", "Error"), tr("Не удалось сохранить PNG.", "Failed to save PNG."))

    def save_map(self) -> None:
        """Сохранить обзорные PNG всего ROM полосами (для поиска графики)."""
        if not self.rom:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, tr("Папка для карты ROM", "Folder for the ROM map"),
            os.path.dirname(self.rom.path) or ".")
        if not out_dir:
            return
        decode = tiles.decode_zt_sheet if self._is_zt() else tiles.decode_sheet
        unit_b = self._unit_bytes()
        band_bytes = 0x80000
        gray = pal.grayscale_palette()
        base = os.path.splitext(os.path.basename(self.rom.path))[0]
        saved = 0
        for i in range(0, self.rom.size, band_bytes):
            units = band_bytes // unit_b
            rows = max(1, -(-units // 32))
            buf, w, h = decode(self.rom.data, i, 32, rows)
            img = build_qimage(buf, w, h, gray, None)
            name = os.path.join(out_dir, f"{base}_map_{i:08x}.png")
            if img.convertToFormat(QImage.Format.Format_ARGB32).save(name, "PNG"):
                saved += 1
        self.statusBar().showMessage(
            tr(f"Карта сохранена: {saved} PNG в {out_dir}", f"Map saved: {saved} PNG in {out_dir}"), 8000)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # язык из настроек (по умолчанию английский) / language from settings (English by default)
    set_lang(QSettings("ztextractor", "ztextractor").value("lang", "en"))
    win = MainWindow()
    win.show()
    return app.exec()
