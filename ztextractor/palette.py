"""Палитры Sega Mega Drive.

Цвет MD — 9-битный, упакован в 16-битное слово (big-endian) вида:
    0000 BBB0 GGG0 RRR0
То есть по 3 бита на канал, итого 512 возможных цветов.
"""

from __future__ import annotations

from typing import List, Tuple

from .i18n import tr

RGB = Tuple[int, int, int]

# 3-битный уровень канала (0..7) -> 8-битное значение.
MD_LEVELS: List[int] = [round(i * 255 / 7) for i in range(8)]


def decode_color(word: int) -> RGB:
    """Декодировать 16-битное слово CRAM в (r, g, b) 0..255."""
    b = (word >> 9) & 7
    g = (word >> 5) & 7
    r = (word >> 1) & 7
    return (MD_LEVELS[r], MD_LEVELS[g], MD_LEVELS[b])


def grayscale_palette() -> List[RGB]:
    """16 оттенков серого — палитра по умолчанию, когда настоящая неизвестна."""
    return [(i * 17, i * 17, i * 17) for i in range(16)]


# Палитра текстур Zero Tolerance (из zmap-tools, alex-west).
# Индекс 0 — прозрачный (на экране пурпурный как маркер).
ZT_PALETTE: List[RGB] = [
    (255, 0, 255), (0, 36, 72), (72, 108, 144), (144, 180, 216),
    (216, 216, 252), (36, 36, 0), (72, 72, 0), (36, 108, 252),
    (72, 0, 0), (108, 36, 0), (252, 144, 72), (144, 72, 36),
    (252, 252, 180), (216, 36, 0), (72, 144, 36), (0, 0, 0),
]


def zt_palette() -> List[RGB]:
    return list(ZT_PALETTE)


# Палитры стен по эпизодам для прототипов Beyond ZT — смещения палитр (CRAM)
# в ROM, вычислены сопоставлением с реальными скриншотами из игры.
# Цвета намеренно «дикие»: розовый 1-й акт, жёлтый 2-й, алый 4-й.
EPISODE_PALETTES = {
    "1995-07-14": [
        (tr("Эпизод 1 (розовый)", "Episode 1 (pink)"), 0x097EDE),
        (tr("Эпизод 2 (жёлтый)", "Episode 2 (yellow)"), 0x097CA4),
        (tr("Эпизод 3 (песочный)", "Episode 3 (sand)"), 0x097CA6),
        (tr("Эпизод 4 (алый)", "Episode 4 (scarlet)"), 0x097EEA),
    ],
    "1995-06-23": [
        (tr("Эпизод 1 (розовый)", "Episode 1 (pink)"), 0x084C5E),
        (tr("Эпизод 2 (жёлтый)", "Episode 2 (yellow)"), 0x0B9DC2),
        (tr("Эпизод 4 (алый)", "Episode 4 (scarlet)"), 0x0B9FFC),
    ],
}


def episode_palettes(version: str):
    """Список (имя, смещение) палитр эпизодов для версии (пусто для ZT-релиза)."""
    for key, items in EPISODE_PALETTES.items():
        if key in version:
            return items
    return []


def is_md_color_word(word: int) -> bool:
    """Слово палитры MD имеет вид 0000 BBB0 GGG0 RRR0 — значащие биты обнулены."""
    return (word & 0xF111) == 0


def find_palettes(data: bytes, max_results: int = 400) -> List[int]:
    """Найти в ROM смещения, похожие на 16-цветные палитры MD (CRAM).

    Сканирует по словам (чётные адреса) и возвращает офсеты, где 16 подряд идущих
    слов — корректные цвета MD, достаточно разнообразные (не сплошной мусор/нули).
    """
    n = len(data)
    out: List[int] = []
    i = 0
    end = n - 32
    while i <= end:
        first = (data[i] << 8) | data[i + 1]
        if not is_md_color_word(first):
            i += 2
            continue
        ok = True
        nonzero = 0
        distinct = set()
        for k in range(16):
            o = i + k * 2
            w = (data[o] << 8) | data[o + 1]
            if not is_md_color_word(w):
                ok = False
                break
            if w != 0:
                nonzero += 1
            distinct.add(w)
        if ok and nonzero >= 6 and len(distinct) >= 6:
            out.append(i)
            if len(out) >= max_results:
                break
            i += 32  # перепрыгнуть найденную палитру
        else:
            i += 2

    # палитры BZT яркие/кислотные — сортируем по «цветности», чтобы реальные
    # всплывали в начало списка (удобно листать кнопками ◀ ▶)
    def colorfulness(off: int) -> float:
        cols = read_palette(data, off)
        return sum(max(c) - min(c) for c in cols) / 16

    out.sort(key=colorfulness, reverse=True)
    return out


def read_palette(data: bytes, offset: int, count: int = 16) -> List[RGB]:
    """Прочитать count цветов (16-битные слова) из ROM начиная с offset."""
    colors: List[RGB] = []
    for i in range(count):
        o = offset + i * 2
        if 0 <= o and o + 1 < len(data):
            word = (data[o] << 8) | data[o + 1]
        else:
            word = 0
        colors.append(decode_color(word))
    return colors
