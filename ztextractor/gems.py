# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Willy Wodka
# Sample-bank format from ValleyBell/GEMSPlay; cf. realmonster/GEMS (LGPL-3.0-or-later).

"""Извлечение PCM-сэмплов звукового движка GEMS из ROM Mega Drive.

Формат банка сэмплов GEMS (из ValleyBell/GEMSPlay): таблица 12-байтных записей
[Flags:1][StartLSB:2 LE][StartMSB:1][Skip:2][DataLen:2 LE][Loop:2][End:2].
Адрес сэмпла = база_таблицы + (StartMSB<<16 | StartLSB); длина = DataLen.
Конец таблицы — пустая запись (Start==0). PCM = unsigned 8-bit mono.
"""

import io
import wave

# Базы таблиц сэмплов по версии (авто-детект как fallback). June — PCM нет.
SAMPLE_TABLE_BASE = {
    "Underground": 0x2EE77,  # ZTU-мод (73 сэмпла) — проверять ДО «Zero Tolerance»!
    "немецкая": 0x5E4E0,     # немецкий релиз = US − 0x3C (проверять ДО «Zero Tolerance»)
    "Zero Tolerance": 0x5E51C,
    "1995-07-14": 0x2E8D4,
}
# Версии, где PCM нет (June-прото) — чтобы не гонять медленный авто-скан.
NO_PCM_VERSIONS = ("1995-06-23",)


YM2612_CLOCK = 7670454  # такт YM2612 (NTSC master / 7)


def sample_ingame_rate(flags):
    """Частота воспроизведения сэмпла GEMS в игре (Гц).
    rate = YM2612_clock / (144 * (flags & 0x0F)) — из GEMSPlay sound.c DACPlay."""
    div = 144 * (flags & 0x0F)
    if div == 0:
        return 10500
    return round(YM2612_CLOCK / div)


def _u16(d, a):
    return d[a] | (d[a + 1] << 8)


def _read_table(d, base, maxn=128):
    """Разобрать таблицу сэмплов с базы base. Возвращает список словарей."""
    samples = []
    prev_end = None
    for i in range(maxn):
        o = base + i * 12
        if o + 12 > len(d):
            break
        start = (d[o + 3] << 16) | d[o + 1] | (d[o + 2] << 8)
        dlen = _u16(d, o + 6)
        flags = d[o]
        if start == 0 and i > 0:
            break
        addr = base + start
        # валидность: разумная длина, в пределах ROM, сэмплы идут подряд
        if not (0x100 <= start < 0x180000 and 0x40 <= dlen <= 0xFFFF
                and addr + dlen <= len(d)):
            break
        if prev_end is not None and addr != prev_end:
            break
        samples.append({"index": i, "addr": addr, "length": dlen, "flags": flags,
                        "rate": sample_ingame_rate(flags)})
        prev_end = addr + dlen
    return samples


def find_sample_table(d):
    """Авто-поиск базы таблицы сэмплов (если версия неизвестна). Медленно."""
    best = None
    for base in range(0x10000, len(d) - 0x1000, 2):
        s = _read_table(d, base)
        if len(s) >= 5 and (best is None or len(s) > len(best[1])):
            best = (base, s)
            if len(s) >= 60:
                break
    return best


def extract_samples(rom):
    """(база_таблицы, [сэмплы]) для ROM, либо (None, []) если PCM нет."""
    d = rom.data
    if any(k in rom.version for k in NO_PCM_VERSIONS):
        return None, []
    for key, base in SAMPLE_TABLE_BASE.items():
        if key in rom.version:
            s = _read_table(d, base)
            if len(s) >= 5:
                return base, s
    r = find_sample_table(d)
    return (r[0], r[1]) if r else (None, [])


def sample_pcm(d, sample):
    """Сырые байты сэмпла (unsigned 8-bit)."""
    return bytes(d[sample["addr"]:sample["addr"] + sample["length"]])


def resample_u8(raw, src_rate, dst_rate):
    """Линейный ресемпл unsigned-8-bit PCM src_rate→dst_rate.
    Нужен для воспроизведения: устройства часто не тянут <8 кГц, играем на 44100."""
    if not raw or src_rate == dst_rate:
        return raw
    n = len(raw)
    n_out = max(1, round(n * dst_rate / src_rate))
    out = bytearray(n_out)
    step = src_rate / dst_rate
    pos = 0.0
    for i in range(n_out):
        idx = int(pos)
        if idx >= n - 1:
            out[i] = raw[n - 1]
        else:
            frac = pos - idx
            out[i] = int(raw[idx] * (1.0 - frac) + raw[idx + 1] * frac) & 0xFF
        pos += step
    return bytes(out)


def sample_wav_bytes(d, sample, rate=10500):
    """WAV-байты сэмпла: 8-bit unsigned mono @rate (как в ROM)."""
    raw = sample_pcm(d, sample)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(1)          # 8-bit WAV = unsigned по спецификации = данные ROM
    w.setframerate(rate)
    w.writeframes(raw)
    w.close()
    return buf.getvalue()
