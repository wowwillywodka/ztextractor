# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Willy Wodka
# Portions derived from realmonster/GEMS (https://github.com/realmonster/GEMS), LGPL-3.0-or-later.

"""Декодер GEMS-музыки Mega Drive: банки, секвенции (песни), FM/PSG-инструменты.

Формат выверен по realmonster/GEMS (gems_split.cpp = события секвенций и структура банка
песен; instruments.cpp = FM-патч → регистры YM2612) и подтверждён на ZT (9 песен @0x5B0CC).

Движок GEMS хранит 4 банка в 68K-ROM, указатели на них передаются Z80-драйверу при инициализации
четырьмя `move.l #imm,-(sp)` (порядок в стеке: samples, sequences, envelopes, patches):
  • patches    — FM/PSG-инструменты (FM = 39 байт)
  • envelopes  — огибающие громкости
  • sequences  — ПЕСНИ: [word LE: 2*кол-во][word LE: офсет песни]… → [байт: каналов][word LE
                 ptr канала]×N → поток байт-событий до eos
  • samples    — DAC (12-байтные записи; парсятся в gems.py)

PCM-сэмплы (DAC) уже извлекаются в gems.py — здесь только музыкальная часть.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ── длины байт-команд секвенции (фиксированные; note/delay/0x72 — особые) ──
_SEQ_FIXED_LEN = {
    0x60: 1,  # eos          конец секвенции
    0x61: 2,  # patch N      сменить инструмент
    0x62: 2,  # modulation N
    0x63: 1,  # nop
    0x64: 2,  # loop N       начало цикла (N повторов)
    0x65: 1,  # loopend
    0x66: 2,  # retrigger
    0x67: 2,  # sustain
    0x68: 2,  # tempo N      темп = N+40
    0x69: 2,  # mute
    0x6A: 2,  # priority
    0x6B: 2,  # play N       запустить DAC-сэмпл N (на DAC-канале)
    0x6C: 3,  # pitch (word LE)
    0x6D: 1,  # sfx
    0x6E: 2,  # samplerate N
    0x6F: 3,  # goto (word LE — офсет в банке)
    0x70: 3,  # mailbox a,b
    0x71: 5,  # if a<op>b,addr
    0x72: 3,  # extra: 0 stop|1 pause|2 resume|3 pausel|4 mastervol|5 volume
}
_SEQ_NAMES = {
    0x60: "eos", 0x61: "patch", 0x62: "modulation", 0x63: "nop", 0x64: "loop",
    0x65: "loopend", 0x66: "retrigger", 0x67: "sustain", 0x68: "tempo", 0x69: "mute",
    0x6A: "priority", 0x6B: "play", 0x6C: "pitch", 0x6D: "sfx", 0x6E: "samplerate",
    0x6F: "goto", 0x70: "mailbox", 0x71: "if", 0x72: "extra",
}


def _u16le(d, a):
    return d[a] | (d[a + 1] << 8)


def _u32be(d, a):
    return (d[a] << 24) | (d[a + 1] << 16) | (d[a + 2] << 8) | d[a + 3]


class SeqEvent:
    """Одно событие секвенции: kind (строка) + args (кортеж байт) + raw_len."""
    __slots__ = ("kind", "args", "length", "value")

    def __init__(self, kind, args, length, value=None):
        self.kind = kind
        self.args = args
        self.length = length
        self.value = value          # для note=код, delay/duration=число, patch=N…

    def __repr__(self):
        v = "" if self.value is None else f"={self.value}"
        return f"{self.kind}{v}"


def decode_event(d, a: int) -> SeqEvent:
    """Разобрать одно событие секвенции по абсолютному адресу a в ROM/буфере d."""
    c = d[a]
    if c & 0x80:                                   # delay (0xC0) или duration (0x80)
        length = 0
        val = 0
        while length < 10 and (d[a + length] & 0xC0) == (c & 0xC0):
            val = (val << 6) | (d[a + length] & 0x3F)
            length += 1
        kind = "delay" if (c & 0xC0) == 0xC0 else "duration"
        return SeqEvent(kind, (), length, val)
    if c < 0x60:                                   # нота 0x00..0x5F
        return SeqEvent("note", (c,), 1, c)
    length = _SEQ_FIXED_LEN.get(c, 1)
    args = tuple(d[a + 1:a + length])
    val = args[0] if length == 2 else None
    return SeqEvent(_SEQ_NAMES.get(c, f"op{c:02X}"), args, length, val)


def decode_channel(d, base: int, ch_off: int, max_events: int = 100000) -> List[SeqEvent]:
    """Поток событий канала (от base+ch_off) до eos/goto (или предела)."""
    out = []
    j = ch_off
    for _ in range(max_events):
        ev = decode_event(d, base + j)
        out.append(ev)
        j += ev.length
        if ev.kind in ("eos", "goto") or ev.length == 0:
            break
    return out


def song_count(d, seq_base: int) -> int:
    """Число песен в банке секвенций (= word_LE[0] / 2)."""
    return _u16le(d, seq_base) // 2


def _u24le(d, a):
    return d[a] | (d[a + 1] << 8) | (d[a + 2] << 16)


def detect_ptr3(d, seq_base: int) -> bool:
    """Указатели каналов в банке секвенций — 3-байтовые (GEMS-флаг «3», напр. Comix Zone,
    ZT Underground) или 2-байтовые (ZT/June/July)? Авто: верный формат даёт указатели,
    МОНОТОННО растущие в пределах песни. Считаем «немонотонности» для обеих ширин."""
    n = song_count(d, seq_base)
    if n <= 0:
        return False

    def bad(stride):
        score = 0
        for s in range(min(n, 6)):
            st = _u16le(d, seq_base + s * 2)
            nc = d[seq_base + st]
            if not (1 <= nc <= 16):
                score += 5
                continue
            prev = -1
            for c in range(nc):
                a = seq_base + st + 1 + c * stride
                p = _u24le(d, a) if stride == 3 else _u16le(d, a)
                if p < prev:
                    score += 1            # не монотонно = подозрительно
                prev = p
        return score

    return bad(3) < bad(2)


def parse_song(d, seq_base: int, index: int, ptr3: Optional[bool] = None) -> Optional[Dict]:
    """Песня index → {offset, channels:[{ptr, events:[SeqEvent]}…]}.
    ptr3=None → авто-детект ширины указателя канала (2 или 3 байта)."""
    n = song_count(d, seq_base)
    if not (0 <= index < n):
        return None
    if ptr3 is None:
        ptr3 = detect_ptr3(d, seq_base)
    stride = 3 if ptr3 else 2
    seq_start = _u16le(d, seq_base + index * 2)
    channels = d[seq_base + seq_start]
    if not (1 <= channels <= 16):
        return None
    chans = []
    for c in range(channels):
        a = seq_base + seq_start + 1 + c * stride
        ptr = _u24le(d, a) if ptr3 else _u16le(d, a)
        chans.append({"ptr": ptr, "events": decode_channel(d, seq_base, ptr)})
    return {"index": index, "offset": seq_start, "channels": chans}


# ── FM-инструмент GEMS (39 байт) → регистры YM2612 ─────────────────────────
# Раскладка (instruments.cpp ImportGems): почти прямое отображение в регистры.
# 4 оператора в данных лежат в порядке слотов 1,3,2,4; логич. OP[i] = данные слота
# ((i<<1)|(i>>1))&3. Каждый оператор 6 байт: 30(DT/MUL) 40(TL) 50(RS/AR) 60(AM/DR)
# 70(D2R) 80(SL/RR); 90(SSG)=0 (GEMS не хранит).
def fm_patch_to_ym2612(patch: bytes) -> Optional[Dict]:
    """GEMS FM-патч (39 байт, type=0) → словарь регистров YM2612 (для канала).

    Возвращает {'op':[{30,40,50,60,70,80,90}×4 в ЛОГИЧ. порядке 1-4], 'B0','B4',
    'lfo'(reg22 биты), 'key'(маска вкл.операторов)}. Применять к базе канала.
    """
    if len(patch) < 39 or patch[0] != 0:
        return None
    b1, b3, b4 = patch[1], patch[3], patch[4]
    lfo = ((b1 >> 3) & 1) << 3 | (b1 & 7)          # reg 0x22 = LFO_on<<3 | LFO_val
    regB0 = ((b3 >> 3) & 7) << 3 | (b3 & 7)        # FB<<3 | ALG
    regB4 = (b4 >> 7) << 7 | ((b4 >> 6) & 1) << 6 | ((b4 >> 4) & 3) << 4 | (b4 & 7)
    ops = []
    for i in range(4):
        slot = ((i << 1) | (i >> 1)) & 3
        o = 5 + slot * 6
        d0, d1, d2, d3, d4, d5 = patch[o:o + 6]
        ops.append({
            0x30: ((d0 >> 4) & 7) << 4 | (d0 & 0xF),   # DT<<4 | MUL
            0x40: d1 & 0x7F,                            # TL
            0x50: (d2 >> 6) << 6 | (d2 & 0x1F),         # RS<<6 | AR
            0x60: (d3 >> 7) << 7 | (d3 & 0x1F),         # AM<<7 | DR
            0x70: d4 & 0x1F,                            # D2R (SDR)
            0x80: (d5 >> 4) << 4 | (d5 & 0xF),          # SL<<4 | RR
            0x90: 0,                                    # SSG-EG (GEMS не задаёт)
        })
    key = patch[37] & 0xF                              # маска включённых операторов
    return {"op": ops, "B0": regB0, "B4": regB4, "lfo": lfo, "key": key,
            "alg": b3 & 7, "fb": (b3 >> 3) & 7}


def patch_table(d, patches_base: int, max_n: int = 200) -> List[Tuple[int, int]]:
    """Таблица инструментов: [(индекс, абс.адрес записи)…]. Первый офсет/2 = кол-во."""
    first = _u16le(d, patches_base)
    n = first // 2
    out = []
    for i in range(min(n, max_n)):
        off = _u16le(d, patches_base + i * 2)
        out.append((i, patches_base + off))
    return out


def instrument_type(d, addr: int) -> int:
    """Тип инструмента по первому байту: 0 FM, 1 DAC, 2 PSG, 3 NOISE."""
    return d[addr]


# ── Поиск 4 банков GEMS по инструкциям инициализации ───────────────────────
def find_gems_banks(d, samples_base: Optional[int] = None) -> Optional[Dict[str, int]]:
    """Найти 4 банка GEMS (patches/envelopes/sequences/samples).

    Опирается на блок из 4 подряд `move.l #imm,-(sp)` (опкод 0x2F3C) рядом с инициализацией
    звука; порядок в стеке = [samples, sequences, envelopes, patches]. Если задан samples_base,
    ищем блок, содержащий именно его (надёжнее). Возвращает словарь адресов или None.
    """
    n = len(d)
    best = None
    a = 0x1000
    while a < n - 24:
        if d[a] == 0x2F and d[a + 1] == 0x3C:
            vals = []
            p = a
            for _ in range(4):
                if d[p] == 0x2F and d[p + 1] == 0x3C:
                    vals.append(_u32be(d, p + 2))
                    p += 6
                else:
                    break
            if len(vals) == 4 and all(0x10000 <= v < n for v in vals):
                span = max(vals) - min(vals)
                if span < 0x20000:                    # 4 банка в одном районе ROM
                    samples, sequences, envelopes, patches = vals
                    if samples_base is None or samples == samples_base:
                        cand = {"samples": samples, "sequences": sequences,
                                "envelopes": envelopes, "patches": patches}
                        # валидация: банк песен начинается с разумного кол-ва
                        sc = song_count(d, sequences)
                        if 1 <= sc <= 64:
                            if samples_base is not None and samples == samples_base:
                                return cand
                            if best is None:
                                best = cand
            a = p if p > a else a + 2
        else:
            a += 2
    return best
