"""Спрайты объектов/врагов Beyond Zero Tolerance (найдено реверс-инжинирингом).

Враги и предметы — билборд-спрайты. У каждого есть таблица определений кадров;
каждый кадр — 0x36 байт и заканчивается маркером 01 02 04 04 02 03. Кадр ссылается
на тайлы 32×32 по индексам, поэтому прямых указателей на графику нет (её адрес
вычисляется). Сама графика тайлов лежит рядом с таблицей кадров.

Этот формат встречается ТОЛЬКО в прототипах BZT (у релиза ZT спрайты иначе).
Кластеры маркеров = отдельные спрайты; число кадров ≈ повороты × анимация
(крупные враги ~38–51 кадр, предметы — единицы).
"""

from __future__ import annotations

import re
from typing import Dict, List

from .i18n import tr

# Заголовок кадра (в НАЧАЛЕ кадра, из дизасма 0x1FCFA / 0x1F982):
#   [01][флаг][xoff][yoff][ШИРИНА][ВЫСОТА]  — 6 байт, далее тайлы.
# byte[0]=01 всегда; byte[1] — ФЛАГ (0x02 у обычных врагов, 0x00 у ползуна и др.,
# не константа!); byte[4]/byte[5] — ширина/высота в тайлах (1..8). Раньше требовали
# byte[1]==02 и теряли кадры ползуна (01 00 …). Тайлы: word(frame+0x16 + row*8 + col*2).
FRAME_HEADER_RE = re.compile(rb"\x01...[\x01-\x08][\x01-\x08]", re.DOTALL)


def _is_frame(data: bytes, o: int) -> bool:
    # byte[0], byte[1] — флаги-якоря (0=лево/верх, 1=центр, 2=право/низ),
    # НЕ константа! byte[4]/byte[5] — ширина/высота в тайлах (1..8).
    return (o + 6 <= len(data) and data[o] <= 2 and data[o + 1] <= 2
            and 1 <= data[o + 4] <= 8 and 1 <= data[o + 5] <= 8)
FRAME_SIZE = 0x36
TILE_INDEX_OFFSET = 0x16
ROW_STRIDE = 8
CLUSTER_GAP = 0x100  # разрыв между кадрами больше этого = новый спрайт

# Формат спрайтов зависит от версии (один движок, разные параметры — из дизасма):
#   ZT (релиз): хендлер 0x1BD72, кадр 0x26 б, индекс тайла = БАЙТ @+0x16+row*4+col,
#               указатель врага кладётся в $42(a0).
#   BZT (прототипы): хендлер 0x1FCFA, кадр 0x36 б, индекс = WORD @+0x16+row*8+col*2,
#               указатель в $46(a0).
# База графики и таблица анимаций считаются одинаково: base=(a1+2)+word[a1+2],
# блок анимации word[a1+4+anim*2], кадр = подсписок+2 + dir*frame_size.
FORMATS = {
    "zt":  {"frame_size": 0x26, "tile_off": 0x16, "row_stride": 4,
            "col_stride": 1, "word_tiles": False, "slot": 0x42},
    "bzt": {"frame_size": 0x36, "tile_off": 0x16, "row_stride": 8,
            "col_stride": 2, "word_tiles": True, "slot": 0x46},
}


def sprite_format(version: str) -> Dict:
    return FORMATS["zt"] if version.startswith("Zero Tolerance") else FORMATS["bzt"]


def find_sprites(data: bytes, min_frames: int = 2) -> List[Dict]:
    """Список спрайтов: {offset, end, frames}. offset — начало таблицы кадров."""
    raw = [m.start() for m in FRAME_HEADER_RE.finditer(data)]
    if not raw:
        return []
    # отсеять одиночные ложные «01 02 …»: настоящий кадр в цепочке с шагом 0x36
    hset = set(raw)
    hits = [o for o in raw
            if (o - FRAME_SIZE in hset) or (o + FRAME_SIZE in hset)]
    if not hits:
        return []
    clusters: List[List[int]] = []
    cur = [hits[0]]
    for h in hits[1:]:
        if h - cur[-1] <= CLUSTER_GAP:
            cur.append(h)
        else:
            clusters.append(cur)
            cur = [h]
    clusters.append(cur)

    sprites: List[Dict] = []
    for c in clusters:
        if len(c) < min_frames:
            continue
        # кадр начинается с заголовка; таблица кадров = от первого до последнего+0x36
        start = c[0]
        end = c[-1] + FRAME_SIZE
        sprites.append({"offset": start, "end": end, "frames": len(c)})

    # Классификация: враг — на чью таблицу кадров ссылается код (указатель в
    # [offset, offset+0x20]); предметы/графика не используются объектным кодом.
    for s in sprites:
        cnt = 0
        for k in range(0, 0x20, 2):
            cnt += data.count((s["offset"] + k).to_bytes(4, "big"))
        s["refs"] = cnt
        s["enemy"] = cnt >= 2
    return sprites


# Именованные враги (от пользователя по скриншотам игры). Для каждого:
# (имя, смещение данных/таблицы кадров, база графики тайлов). База графики у
# большинства = данные + 0xE00. Адреса грубые (±) — в интерфейсе уточняются
# подстройкой базы с живым предпросмотром.
KNOWN_SPRITES = {
    "1995-07-14": [
        (tr("Красный робот", "Red robot"), 0x1646B8, 0x1654C2),
        (tr("Фиолетовый робот", "Purple robot"), 0x184716, 0x185524),
        (tr("Босс уровня 1", "Level 1 boss"), 0x1A33CD, 0x1A41D6),
        (tr("Макет врага 1", "Enemy layout 1"), 0x1C446E, 0x1C53F0),
        (tr("Макет врага 2", "Enemy layout 2"), 0x1E16F3, 0x1E24FD),
        (tr("Девушка-инопланетянка", "Alien girl"), 0x1FEC9F, 0x1FFAB0),
        (tr("Макет врага 3", "Enemy layout 3"), 0x2213DE, 0x2221EA),
        (tr("Потолочный инопланетянин (кокон+ползун)", "Ceiling alien (cocoon+crawler)"), 0x23E6D0, 0x23F24C),
        (tr("Босс уровня 2", "Level 2 boss"), 0x2560E1, 0x256EFC),
        (tr("Макет врага 4", "Enemy layout 4"), 0x27D866, 0x27E676),
        (tr("Собака", "Dog"), 0x29ABEB, 0x29B7FB),
    ],
}


def known_sprites(version: str):
    """Именованные враги для версии: список (имя, данные, база графики)."""
    for key, items in KNOWN_SPRITES.items():
        if key in version:
            return items
    return []


def parse_frames(data: bytes, sprite: Dict) -> List[Dict]:
    """Список кадров спрайта: {off, w, h} (размеры из заголовка кадра).

    Тайлы кадра читаются как word(off + 0x16 + row*8 + col*2), сетка w×h.
    """
    lo = sprite["offset"] & ~1   # заголовки кадров на чётных адресах
    hi = min(len(data), sprite["end"])
    frames: List[Dict] = []
    o = lo
    while o < hi - 6:
        if _is_frame(data, o):
            frames.append({"off": o, "w": data[o + 4], "h": data[o + 5]})
            o += FRAME_SIZE
        else:
            o += 2
    return frames


def _w(data: bytes, o: int) -> int:
    return (data[o] << 8) | data[o + 1]


def _valid_header(data: bytes, x: int) -> bool:
    """Похоже ли на заголовок врага: счётчик + растущая таблица анимаций + база."""
    n = len(data)
    if x < 0 or x + 4 > n:
        return False
    count = _w(data, x)
    if not (1 <= count <= 20) or x + 4 + count * 2 > n:
        return False
    boff = _w(data, x + 2)
    if boff < count * 2 + 4:
        return False
    prev = -1
    for i in range(count):
        ao = _w(data, x + 4 + i * 2)
        if ao <= prev or ao >= boff:
            return False
        prev = ao
    base = (x + 2) + boff
    return x < base < x + 0x20000 and base < n


def find_header(data: bytes, start: int, limit: int = 0x400):
    """Найти a1 (заголовок врага) от start. None если не найден."""
    for x in range(start & ~1, min(len(data), start + limit), 2):
        if _valid_header(data, x):
            return x
    return None


def _header_for_sprite(data: bytes, sprite_off: int, back: int = 0x400):
    """Найти заголовок a1 ПЕРЕД кадрами спрайта (его дерево ведёт к этим кадрам)."""
    lo = max(0, (sprite_off - back) & ~1)
    found = None
    for x in range(lo, sprite_off + 2, 2):
        if not _valid_header(data, x):
            continue
        t = parse_sprite(data, x)
        if sum(1 for fr in t["anims"] if fr) < 2:
            continue
        allf = [f for fr in t["anims"] for f in fr]
        if not allf:
            continue
        lo_f = min(f["off"] for f in allf)
        hi_f = max(f["off"] for f in allf)
        if lo_f <= sprite_off + 0x40 <= hi_f + FRAME_SIZE:
            found = x  # держим ближайший к спрайту
    return found


def find_enemies(data: bytes, fmt: Dict):
    """Список врагов из таблицы диспетчера игры (game-accurate, любая версия).

    Сканирует `move.l #ptr, $slot(a0)` (21 7C <ptr> 00 slot) — так код кладёт
    указатель врага перед вызовом хендлера. Возвращает {a1, base, frames}.
    """
    slot = fmt["slot"]
    pat = re.compile(rb"\x21\x7c....\x00" + bytes([slot]), re.DOTALL)
    n = len(data)
    seen = set()
    out = []
    for m in pat.finditer(data):
        o = m.start()
        a1 = (data[o + 2] << 24) | (data[o + 3] << 16) | (data[o + 4] << 8) | data[o + 5]
        if not (0x10000 <= a1 < n) or a1 in seen or not _valid_header(data, a1):
            continue
        seen.add(a1)
        t = parse_sprite(data, a1, fmt)
        total = sum(len(fr) for dirs in t["anims"] for fr in dirs)
        if total < 1:
            continue
        out.append({"a1": a1, "base": t["base"], "frames": total, "offset": a1})
    out.sort(key=lambda e: e["a1"])
    return out


def _dir_frames(data: bytes, sub: int, fsize: int) -> List[Dict]:
    """Кадры одного направления: word[sub]=число кадров (0 => 1 кадр @sub+2)."""
    nfr = _w(data, sub)
    nframes = nfr if 0 < nfr <= 64 else (1 if nfr == 0 else 0)
    out = []
    for i in range(nframes):
        fo = sub + 2 + i * fsize
        if _is_frame(data, fo):
            out.append({"off": fo, "w": data[fo + 4], "h": data[fo + 5]})
    return out


def parse_sprite(data: bytes, a1: int, fmt: Dict = FORMATS["bzt"]) -> Dict:
    """Разобрать дерево врага по структуре хендлера (3 уровня).

    Возвращает {a1, base, anims}, где anims — список анимаций; каждая анимация —
    список НАПРАВЛЕНИЙ (ракурсов), каждое направление — список кадров {off, w, h}.
    """
    fsize = fmt["frame_size"]
    count = _w(data, a1)
    base = (a1 + 2) + _w(data, a1 + 2)
    anims: List[List[List[Dict]]] = []
    for n in range(count):
        a = (a1 + 2) + _w(data, a1 + 4 + n * 2)        # блок анимации
        ndirs = _w(data, a)                             # число направлений
        dirs: List[List[Dict]] = []
        if 1 <= ndirs <= 16:
            for dr in range(ndirs):
                sub = a + _w(data, a + 2 + dr * 2)      # под-список направления
                frames = _dir_frames(data, sub, fsize)
                if frames:
                    dirs.append(frames)
        anims.append(dirs)
    return {"a1": a1, "base": base, "anims": anims}


def frame_tile(data: bytes, frame: Dict, row: int, col: int,
               fmt: Dict = FORMATS["bzt"]) -> int:
    """Индекс тайла в кадре по (row, col) с учётом формата версии."""
    o = (frame["off"] + fmt["tile_off"]
         + row * fmt["row_stride"] + col * fmt["col_stride"])
    if fmt["word_tiles"]:
        return (data[o] << 8) | data[o + 1]
    return data[o]


# Массив атрибутов тайлов кадра: байт на frame+6+row*4+col (шаг строки 4 байта),
# одинаков для ZT и BZT (хендлеры 0x1BCAA и 0x1FCFA: a3=frame+6, +1/столбец, +4/строка).
# Ненулевой байт = горизонтальный флип тайла (в блиттере шаг X негируется).
ATTR_OFF = 6
ATTR_ROW_STRIDE = 4


def frame_attr(data: bytes, frame: Dict, row: int, col: int) -> int:
    """Атрибутный байт тайла (бит0 = H-флип). Тот же layout для всех версий."""
    o = frame["off"] + ATTR_OFF + row * ATTR_ROW_STRIDE + col
    return data[o] if 0 <= o < len(data) else 0


_MASK = frozenset((0x77, 0x7F, 0xF7, 0xFF))


def guess_tile_base(data: bytes, after: int, limit: int = 0x2000) -> int:
    """Эвристика: первое смещение после таблицы кадров, похожее на тайлы 32×32.

    Неточная — база уточняется в интерфейсе подстройкой с живым предпросмотром.
    """
    def good(o: int) -> bool:
        seg = data[o:o + 512]
        if len(seg) < 512:
            return False
        nz = sum(1 for b in seg if b) / 512
        mf = sum(1 for b in seg if b in _MASK) / 512
        return 0.25 < nz < 0.97 and mf < 0.35 and len(set(seg)) >= 10

    o = after
    end = min(len(data) - 512, after + limit)
    while o < end:
        # требуем 4 подряд «настоящих» тайла — отсекаем короткий заголовок/маску
        if good(o) and good(o + 512) and good(o + 1024) and good(o + 1536):
            return o
        o += 2
    return after
