"""Дескрипторы зон Beyond Zero Tolerance (найдены реверс-инжинирингом).

В ROM прототипов есть таблица дескрипторов зон. Каждый дескриптор (шаг 0x38):
    +00: указатель на палитру A (светлая грань стен)
    +04: указатель на палитру B (тёмная грань — затенение при повороте)
    +08..+34: указатели на банки текстур этой зоны (стены, спрайты, объекты)

Движок для зоны грузит её палитру в CRAM и рисует стены из её банков. Указатели
дают точные (выровненные) адреса банков — это и есть «как игра выбирает графику».

Адреса таблиц проверены визуально для обоих прототипов.
"""

from __future__ import annotations

import bisect
from typing import Dict, List

from .i18n import tr

ZONE_STRIDE = 0x38
BANKS_PER_ZONE = 12
TEX_BYTES = 512  # размер одной текстуры 32×32 4bpp
# В дескрипторе это 11-й графический указатель (+0x30): Items{N}ep.bin,
# то есть общий для зоны банк предметов, оружия и декораций.
ITEMS_BANK_SLOT = 10

# version-substring -> (offset таблицы, число зон)
ZONE_TABLES = {
    "1995-07-14": (0x097A44, 4),
    "1995-06-23": (0x0B9B9A, 3),
}


def _long(data: bytes, o: int) -> int:
    return (data[o] << 24) | (data[o + 1] << 16) | (data[o + 2] << 8) | data[o + 3]


def _hue_name(colors: List) -> str:
    sat = [c for c in colors if max(c) - min(c) > 40]
    if not sat:
        return tr("серая", "gray")
    r = sum(c[0] for c in sat) / len(sat)
    g = sum(c[1] for c in sat) / len(sat)
    b = sum(c[2] for c in sat) / len(sat)
    if r > g + 20 and b > g + 20:
        return tr("розовая", "pink")
    if r > b + 20 and g > b + 20:
        return tr("жёлтая", "yellow")
    if r > g + 20 and r > b + 20:
        return tr("алая", "scarlet")
    if g > r and g > b:
        return tr("зелёная", "green")
    if b > r and b > g:
        return tr("синяя", "blue")
    return tr("разная", "mixed")


def parse_zones(data: bytes, version: str) -> List[Dict]:
    """Список зон для версии.

    Каждая зона: {index, palA, palB, banks, items}. ``banks`` — список словарей
    {offset, size, count} ТОЛЬКО для настоящих банков текстур (размер кратен 512).
    ``items`` — такой же словарь для выделенного в дескрипторе банка предметов и
    декораций этой зоны, либо ``None`` для неизвестной/повреждённой таблицы.

    Размер банка = расстояние до следующего указателя на графику во всей таблице
    (банки лежат в ROM встык). Мелкие записи (метатекстуры 0xA0, заголовки 0x0C)
    отсеиваются — раньше из-за них банки бились неправильно.
    """
    table = None
    for key, val in ZONE_TABLES.items():
        if key in version:
            table = val
            break
    if not table:
        return []

    base, count = table
    n = len(data)
    MAX_BANK = 0x20000   # 256 текстур — наблюдаемый максимум (главный банк стен)
    MIN_TEX = 4          # меньше — это метатекстуры/мелочь, не банк

    # 1) границы банков задаёт ВЕСЬ список указателей таблицы (не только 12 слотов
    #    дескриптора) — банки лежат в ROM встык, конец = следующий указатель.
    win_hi = base + count * ZONE_STRIDE + 0x40
    # указатели таблицы выровнены по чётности базы (база может быть не кратна 4)
    start = max(0, base - 0x80)
    start += (base - start) % 4
    all_ptrs = set()
    for o in range(start, min(n - 4, win_hi), 4):
        v = _long(data, o)
        if 0x080000 <= v < n:
            all_ptrs.add(v)
    sorted_ptrs = sorted(all_ptrs)

    def bank_count(p: int):
        j = bisect.bisect_right(sorted_ptrs, p)
        gap = (sorted_ptrs[j] - p) if j < len(sorted_ptrs) else MAX_BANK
        gap = min(gap, MAX_BANK)        # ограничить последний/несвязанный банк
        c = gap // TEX_BYTES            # округлить вниз до целых текстур
        return c if c >= MIN_TEX else None

    # 2) разобрать зоны, оставив только настоящие банки текстур
    zones: List[Dict] = []
    for i in range(count):
        z = base + i * ZONE_STRIDE
        if z + 8 > n:
            break
        pal_a = _long(data, z)
        pal_b = _long(data, z + 4)
        banks = []
        seen = set()
        for k in range(BANKS_PER_ZONE):
            b = _long(data, z + 8 + 4 * k)
            if not (0x080000 <= b < n) or b in seen:
                continue
            c = bank_count(b)
            if c:
                seen.add(b)
                banks.append({"offset": b, "size": c * TEX_BYTES, "count": c})
        banks.sort(key=lambda x: -x["count"])  # главный банк стен — сверху
        items_ptr = _long(data, z + 8 + 4 * ITEMS_BANK_SLOT)
        items_bank = next((bank for bank in banks if bank["offset"] == items_ptr), None)
        zones.append({"index": i, "palA": pal_a, "palB": pal_b,
                      "banks": banks, "items": items_bank})
    return zones
