"""Авто-резолвер спрайтов ячеек: celltype → реальная графика врага/объекта.

Вместо ручных карт (CELLTYPE_TO_ENEMY и т.п.) выводит соответствие ПРЯМО из игровых
таблиц ROM — работает на любом билде (June/July/ZT/ZTU/немецкий/ромхаки) без ручной
разметки. Цепочка ВРАГОВ (выверена, воспроизводит известные адреса):

    celltype → [таблица класс-врага][celltype] = класс (1..N)
             → [таблица класс→objdef][класс-1] = адрес objdef (запись шага 0x26)
             → objdef + 0x14 = графика врага (a1 спрайт-дерево)

Таблицы находятся по сигнатуре содержимого (не по адресу), поэтому метод билдо-независим.
"""
from __future__ import annotations

from typing import Optional, Dict, Tuple, List

from . import sprites as _sm          # _valid_header (sprites НЕ импортирует spriteres → без цикла)

OBJDEF_STRIDE = 0x26
ENEMY_GFX_OFF = 0x14          # objdef + 0x14 = указатель графики (a1)
OBJDEF_CT_OFF = 0x18          # objdef + 0x18 (старший байт) = celltype, спавнимый этим objdef
# целл-типы врагов (диапазоны движка) — у всех билдов одинаковы
ENEMY_CELLTYPES = list(range(0x29, 0x2D)) + list(range(0x65, 0x6C))
# целл-типы, которые objdef может ЗАЯВЛЯТЬ в +0x18 (враги + спец-актёры-«макеты»/инопланетянин)
OBJDEF_CT_OK = frozenset(list(range(0x27, 0x2D)) + list(range(0x65, 0x6C)) + [0x08, 0x09])


def _u32(d: bytes, a: int) -> int:
    return (d[a] << 24) | (d[a + 1] << 16) | (d[a + 2] << 8) | d[a + 3]


def find_enemy_class_table(data: bytes) -> Optional[int]:
    """Таблица celltype→класс-врага (256 байт). Сигнатура: [0x29,0x2A,0x2B]=1,2,3;
    [0x65..0x6B]=4..10; [0x00],[0x01]=0. Уникальна во всех билдах."""
    n = len(data)
    for b in range(n - 0x100):
        if (data[b + 0x29] == 1 and data[b + 0x2A] == 2 and data[b + 0x2B] == 3
                and data[b + 0x65] == 4 and data[b + 0x66] == 5 and data[b + 0x6B] == 10
                and data[b] == 0 and data[b + 1] == 0):
            return b
    return None


def find_class_objdef_table(data: bytes, classtab: int) -> Tuple[Optional[int], Optional[int]]:
    """Таблица «класс → указатель objdef» (14 longword'ов) + база objdef-таблицы.

    Ищет рядом с таблицей класса набор из 14 указателей, ведущих в плотный кластер
    записей с шагом 0x26 (objdef-таблица), у которых +0x14 = валидный спрайт-адрес.
    Возвращает (адрес_таблицы_указателей, база_objdef) или (None, None).
    """
    n = len(data)
    for window in (0x400, 0x1200):
        for b in range(classtab, min(classtab + window, n - 56), 2):
            vals = [_u32(data, b + i * 4) for i in range(14)]
            if any(v == 0 or v + ENEMY_GFX_OFF + 4 >= n for v in vals):
                continue
            lo, hi = min(vals), max(vals)
            if hi - lo > 0x400:
                continue
            if any((v - lo) % OBJDEF_STRIDE for v in vals):
                continue
            # ключевая проверка: objdef0+0x14 = графика в верхнем ROM
            if 0x100000 <= _u32(data, lo + ENEMY_GFX_OFF) < n:
                return b, lo
    return None, None


def find_objdef_table_direct(data: bytes) -> Optional[int]:
    """Запасной поиск objdef-таблицы (ZT/ZTU) по содержимому: ≥6 записей подряд шага
    0x26, у каждой +0x14 = спрайт-адрес в верхнем ROM. Класс индексирует её напрямую."""
    n = len(data)
    for b in range(0xA000, min(0x10000, n - 0x100), 2):
        ok = 0
        for i in range(10):
            g = _u32(data, b + i * OBJDEF_STRIDE + ENEMY_GFX_OFF)
            if 0x100000 <= g < n:
                ok += 1
            else:
                break
        if ok >= 6:
            return b
    return None


class EnemyResolver:
    """Авто-резолвер врагов для одного ROM. enemy_gfx(celltype) → (класс, objdef, gfx)."""

    def __init__(self, data: bytes):
        self.data = data
        self.classtab = find_enemy_class_table(data)
        self.ptrtab, self.objbase = (None, None)
        if self.classtab is not None:
            self.ptrtab, self.objbase = find_class_objdef_table(data, self.classtab)
        self.direct_objbase = None
        if self.ptrtab is None:
            self.direct_objbase = find_objdef_table_direct(data)
        self._gfx_index = self._build_gfx_index()
        self.direct = self._build_direct_map()         # celltype → [(objdef, gfx), …]

    def _build_direct_map(self) -> Dict[int, List[Tuple[int, int]]]:
        """celltype (из objdef+0x18) → список (адрес_objdef, gfx). Спавнер игры сопоставляет
        objdef к поставленной ячейке ИМЕННО по этому байту, поэтому это ПРЯМОЙ и точный
        источник спрайта — ловит и спец-актёров (0x08/0x09 «макеты», 0x27 белый
        инопланетянин), которых нет в class-таблице 0x96BE (там class=0). Несколько
        записей на один celltype = вариации спрайта (порядок таблицы = приоритет)."""
        out: Dict[int, List[Tuple[int, int]]] = {}
        self.declared = set()                          # celltype, ОБЪЯВЛЕННЫЕ в objdef (даже с OOB-gfx)
        base = self.objbase if self.objbase is not None else self.direct_objbase
        if base is None:
            return out
        d = self.data
        miss = 0
        for i in range(40):
            od = base + i * OBJDEF_STRIDE
            if od + OBJDEF_STRIDE > len(d):
                break
            ct = d[od + OBJDEF_CT_OFF]
            if ct not in OBJDEF_CT_OK:
                miss += 1
                if miss >= 2:                          # 2 подряд «чужих» байта = таблица кончилась
                    break
                continue
            miss = 0
            self.declared.add(ct)
            gfx = _u32(d, od + ENEMY_GFX_OFF)
            if not (0x100000 <= gfx < len(d)) or not _sm._valid_header(d, gfx):
                continue                               # объявлен, но gfx вне ROM = намеренно НЕВИДИМ
            out.setdefault(ct, []).append((od, gfx))
        return out

    def _build_gfx_index(self) -> Dict[int, int]:
        """objdef-индекс → gfx (по порядку записей objdef-таблицы), для имён по индексу."""
        out: Dict[int, int] = {}
        base = self.objbase if self.objbase is not None else self.direct_objbase
        if base is None:
            return out
        for i in range(12):
            od = base + i * OBJDEF_STRIDE
            if od + ENEMY_GFX_OFF + 4 > len(self.data):
                break
            g = _u32(self.data, od + ENEMY_GFX_OFF)
            if 0x100000 <= g < len(self.data):
                out[i] = g
        return out

    def enemy_class(self, celltype: int) -> Optional[int]:
        if self.classtab is None:
            return None
        c = self.data[self.classtab + (celltype & 0xFF)]
        return c or None

    def enemy_gfx(self, celltype: int) -> Optional[Tuple[int, int, int]]:
        """celltype → (класс, адрес_objdef, адрес_графики) или None если спрайта нет.

        Приоритет — ПРЯМОЕ сопоставление objdef+0x18 (точный спрайт ячейки, ловит спец-
        актёров); если у целл-типа нет своего objdef — запасная class-цепочка
        (целл-типы без выделенного objdef, напр. 0x66/0x67/0x6A/0x6B в прототипах)."""
        ct = celltype & 0xFF
        hit = self.direct.get(ct)
        if hit:
            od, gfx = hit[0]
            return self.enemy_class(ct) or 0, od, gfx
        cls = self.enemy_class(ct)
        if cls is None:
            return None
        if self.ptrtab is not None:
            od = _u32(self.data, self.ptrtab + (cls - 1) * 4)
        elif self.direct_objbase is not None:
            od = self.direct_objbase + (cls - 1) * OBJDEF_STRIDE
        else:
            return None
        if od + ENEMY_GFX_OFF + 4 > len(self.data):
            return None
        gfx = _u32(self.data, od + ENEMY_GFX_OFF)
        if not (0x100000 <= gfx < len(self.data)):
            return None
        return cls, od, gfx

    def enemy_variants(self, celltype: int) -> List[int]:
        """Все gfx-адреса для celltype (вариации спрайта). Первый = основной."""
        return [gfx for _od, gfx in self.direct.get(celltype & 0xFF, [])]

    def episode_sprite(self, ep_index: int) -> Optional[int]:
        """gfx из objdef-таблицы по индексу (= эпизод−1). Для June-МАКЕТОВ 0x08/0x09:
        их собственное поле objdef+0x14 указывает ВНЕ 2МБ ROM (не используется), а
        upfront-спавн назначает спрайт ПО ЭПИЗОДУ из objdef[эпизод−1] (выверено юзером:
        June ep3 макет = objdef[2] = 0x1C73A2, красный человекоподобный макет)."""
        return self._gfx_index.get(ep_index)
