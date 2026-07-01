"""Декодирование графики Mega Drive.

Тайл (паттерн) VDP: 8x8 пикселей, 4 бита на пиксель -> 32 байта.
Формат строки: 4 байта = 8 пикселей; в байте старший ниббл — левый пиксель,
младший — правый. Индекс 0 обычно прозрачный.
"""

from __future__ import annotations

from typing import Tuple

from .i18n import tr

TILE_W = 8
TILE_H = 8
TILE_BYTES = 32  # 8 строк * 4 байта

# Таблица: байт -> два индекса пикселей (старший и младший ниббл).
_NIBBLES = [bytes((b >> 4, b & 0xF)) for b in range(256)]


def decode_sheet(
    data: bytes, offset: int, tiles_per_row: int, rows: int
) -> Tuple[bytes, int, int]:
    """Разложить tiles_per_row * rows тайлов в индексный буфер (1 байт/пиксель).

    Возвращает (buffer, width, height). Ширина кратна 8 — это гарантирует
    32-битное выравнивание строк, нужное QImage(Format_Indexed8).
    """
    width = tiles_per_row * TILE_W
    height = rows * TILE_H
    buf = bytearray(width * height)
    total = tiles_per_row * rows
    n = len(data)

    for t in range(total):
        tx = (t % tiles_per_row) * TILE_W
        ty = (t // tiles_per_row) * TILE_H
        base = offset + t * TILE_BYTES
        if base >= n:
            break  # дальше только нули — буфер уже обнулён
        for r in range(TILE_H):
            ro = base + r * 4
            chunk = data[ro : ro + 4]
            if len(chunk) == 4:
                row = (
                    _NIBBLES[chunk[0]]
                    + _NIBBLES[chunk[1]]
                    + _NIBBLES[chunk[2]]
                    + _NIBBLES[chunk[3]]
                )
            elif chunk:
                chunk = chunk + bytes(4 - len(chunk))
                row = (
                    _NIBBLES[chunk[0]]
                    + _NIBBLES[chunk[1]]
                    + _NIBBLES[chunk[2]]
                    + _NIBBLES[chunk[3]]
                )
            else:
                continue  # вышли за конец ROM
            dst = (ty + r) * width + tx
            buf[dst : dst + TILE_W] = row

    return bytes(buf), width, height


# --- Текстуры Zero Tolerance ---------------------------------------------
# Несжатые 32x32, 4 бита/пиксель, хранятся ПО СТОЛБЦАМ: 16 полос по 2 пикселя
# (1 байт/строка). На полосу 32 байта, на текстуру 512 байт.
ZT_TEX = 32
ZT_TEX_BYTES = 512


def decode_zt_sheet(
    data: bytes, offset: int, tex_per_row: int, rows: int
) -> Tuple[bytes, int, int]:
    """Разложить tex_per_row * rows текстур Zero Tolerance 32x32 в индексный буфер."""
    width = tex_per_row * ZT_TEX
    height = rows * ZT_TEX
    buf = bytearray(width * height)
    total = tex_per_row * rows
    n = len(data)

    for t in range(total):
        base = offset + t * ZT_TEX_BYTES
        if base >= n:
            break
        tx0 = (t % tex_per_row) * ZT_TEX
        ty0 = (t // tex_per_row) * ZT_TEX
        for col in range(16):
            coff = base + col * 32
            x = tx0 + col * 2
            for row in range(32):
                o = coff + row
                if o >= n:
                    break
                byte = data[o]
                dst = (ty0 + row) * width + x
                buf[dst] = byte >> 4
                buf[dst + 1] = byte & 0xF

    return bytes(buf), width, height


def decode_colmajor_sheet(
    data: bytes, offset: int, blocks_per_row: int, rows: int,
    block_w: int, block_h: int
) -> Tuple[bytes, int, int]:
    """Разложить блоки из 8×8-тайлов в COLUMN-MAJOR порядке внутри блока.

    Блок = block_w × block_h тайлов (8×8 4bpp, 32б каждый), тайлы внутри блока идут
    ПО СТОЛБЦАМ: tile(col,row) = col*block_h + row (как VDP-спрайт / иконки HUD ZT).
    Раскладывает blocks_per_row*rows таких блоков сеткой. Возвращает (buf, width, height).
    """
    bw_px = block_w * TILE_W
    bh_px = block_h * TILE_H
    tiles_per_block = block_w * block_h
    width = blocks_per_row * bw_px
    height = rows * bh_px
    buf = bytearray(width * height)
    n = len(data)
    total = blocks_per_row * rows
    for b in range(total):
        bx0 = (b % blocks_per_row) * bw_px
        by0 = (b // blocks_per_row) * bh_px
        bbase = offset + b * tiles_per_block * TILE_BYTES
        if bbase >= n:
            break
        for col in range(block_w):
            for row in range(block_h):
                k = col * block_h + row
                tbase = bbase + k * TILE_BYTES
                if tbase >= n:
                    continue
                tile, _, _ = decode_sheet(data, tbase, 1, 1)
                tx = bx0 + col * TILE_W
                ty = by0 + row * TILE_H
                for r in range(TILE_H):
                    dst = (ty + r) * width + tx
                    buf[dst:dst + TILE_W] = tile[r * TILE_W:r * TILE_W + TILE_W]
    return bytes(buf), width, height


def decode_bg_bytepair(data: bytes, offset: int, max_bytes: int = 0x8000) -> bytes:
    """Распаковать фон-тайлы прототипа BZT-July (рекурсивный byte-pair / digram).

    Распаковщик ROM = 0x98B98 (вызов из bg-setup 0x2A68). Дескриптор-зоны @0x97A40
    (массив шаг 0x38) хранит указатель Plan_A (+0x2C); там лежат СЖАТЫЕ тайлы,
    распаковываемые в VRAM. Формат — блоки до 0xFF:
        [cnt] [cnt× (code, left, right)] [выравнивание чёт] [data_count word]
        [data_count× code-байт]
    Каждый code раскрывается рекурсивно: если он в словаре → emit(left); emit(right),
    иначе — литерал. Литералы образуют поток байт тайлов (8×8 4bpp, 32 б/тайл).
    Возвращает распакованный буфер тайлов (для индексации тайлкартой, base 0).
    """
    out = bytearray()
    pos = offset
    n = len(data)
    while pos < n and len(out) < max_bytes:
        cnt = data[pos]; pos += 1
        if cnt == 0xFF:                       # конец потока
            break
        flag = bytearray(256); left = bytearray(256); right = bytearray(256)
        for _ in range(cnt):                  # словарь digram-ов
            if pos + 2 >= n:
                return bytes(out)
            c = data[pos]; flag[c] = 1
            left[c] = data[pos + 1]; right[c] = data[pos + 2]; pos += 3
        if pos & 1:                           # выравнивание на чётный адрес
            pos += 1
        if pos + 1 >= n:
            break
        dcount = (data[pos] << 8) | data[pos + 1]; pos += 2
        for _ in range(dcount):               # поток кодов → рекурсивная развёртка
            if pos >= n or len(out) >= max_bytes:
                return bytes(out)
            stack = [data[pos]]; pos += 1
            # ЛИМИТЫ ВНУТРИ развёртки: на мусорном адресе словарь может содержать
            # цикл (code→самого себя) → бесконечный рост. Обрываем по out/стеку.
            while stack:
                if len(out) >= max_bytes or len(stack) > max_bytes:
                    return bytes(out)
                s = stack.pop()
                if flag[s] == 0:
                    out.append(s)
                else:
                    stack.append(right[s]); stack.append(left[s])
        if pos & 1:
            pos += 1
    return bytes(out)


# Известные банки текстур Zero Tolerance (offset, число текстур) из zmap-tools.
ZT_BANKS = [
    ("Spaceship walls (255)", 0x12EF26, 255),
    ("Bank 0x10E9BE (66)", 0x10E9BE, 66),
    ("Bank 0x1726F2 (47)", 0x1726F2, 47),
    ("Bank 0x178C80 (23)", 0x178C80, 23),
    ("Bank 0x17EB94 (40)", 0x17EB94, 40),
    ("Bank 0x184478 (70)", 0x184478, 70),
    ("Bank 0x18EB00 (42)", 0x18EB00, 42),
    ("Bank 0x19450C (72)", 0x19450C, 72),
    ("Bank 0x19DC28 (78)", 0x19DC28, 78),
    ("Bank 0x1A8872 (34)", 0x1A8872, 34),
    ("Bank 0x1AD938 (81)", 0x1AD938, 81),
    ("Bank 0x1BAF8A (59)", 0x1BAF8A, 59),
    ("Bank 0x1C656A (42)", 0x1C656A, 42),
    ("Bank 0x1CBA34 (3)", 0x1CBA34, 3),
    ("Bank 0x1CC332 (24)", 0x1CC332, 24),
    ("Bank 0x15A52A (256)", 0x15A52A, 256),
]

# Прототипы Beyond ZT: точные адреса банков не опубликованы, формат тот же.
# Это найденные ориентиры — области графики, откуда удобно начинать прокрутку
# и выравнивание (кнопки «Подстройка»).
BZT_JUN_POINTS = [
    (tr("Графика ~0x0A0000", "Graphics ~0x0A0000"), 0x0A0000, None),
    (tr("Графика ~0x100000", "Graphics ~0x100000"), 0x100000, None),
    (tr("Графика ~0x140000", "Graphics ~0x140000"), 0x140000, None),
    (tr("ZMAP уровень 0x148582", "ZMAP level 0x148582"), 0x148582, None),
]
BZT_JUL_POINTS = [
    (tr("Графика ~0x080000", "Graphics ~0x080000"), 0x080000, None),
    (tr("Графика ~0x100000", "Graphics ~0x100000"), 0x100000, None),
    (tr("Графика ~0x180000", "Graphics ~0x180000"), 0x180000, None),
    # ── ВЕРХНИЙ 1 МБ (ТОЛЬКО July — ROM 3 МБ!): ~744 КБ спрайтов персонажей,
    # несжатая 4bpp 32×32 column-major, которых НЕТ в нижних 2 МБ. Палитра спрайтов 0x97C00.
    # Дропдаун раньше доходил лишь до 0x180000 → казалось «не видно выше 0x200000».
    (tr("Спрайты July ~0x200000", "July sprites ~0x200000"), 0x200000, None),
    (tr("Спрайты July ~0x230000", "July sprites ~0x230000"), 0x230000, None),
    (tr("Спрайты July ~0x260000", "July sprites ~0x260000"), 0x260000, None),
    (tr("Спрайты July ~0x280000", "July sprites ~0x280000"), 0x280000, None),
    (tr("Спрайты July ~0x2A0000", "July sprites ~0x2A0000"), 0x2A0000, None),
]


def banks_for_version(version: str):
    """Список (имя, смещение, count|None) банков/ориентиров под версию игры.

    Структура движка BZT унаследована от ZT: банки стен лежат по тем же адресам
    (содержимое — новое, по актам). Поэтому прототипам отдаём банки ZT как точки
    входа на стены плюс найденные области графики.
    """
    if version.startswith("Zero Tolerance"):
        return ZT_BANKS
    if "1995-07-14" in version:
        return ZT_BANKS + BZT_JUL_POINTS
    if "1995-06-23" in version:
        return ZT_BANKS + BZT_JUN_POINTS
    return []
