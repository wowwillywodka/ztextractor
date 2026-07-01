"""ctypes-обёртка эмулятора YM2612 (Nuked-OPN2) для рендера GEMS-музыки в PCM.

Нативный код в `ztextractor/opn2/` (ym3438.c/.h = nukeykt/Nuked-OPN2 LGPL 2.1, wrap.c = наша
обёртка). Библиотека собирается build.sh; при отсутствии собираем на лету (нужен cc).

Выход: PCM 16-бит стерео на «нативной» частоте чипа = master_clock/144 ≈ 53267 Гц (NTSC),
один сэмпл = сумма mol/mor за 24 внутренних такта (каналы мультиплексируются по времени).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

# Частота нативного выхода YM2612 (NTSC): 7670454 / 144.
NATIVE_RATE = 53267
_YM2612_CLOCK = 7670454

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opn2")
_LIB_NAMES = ("libopn2.dylib", "libopn2.so")


def _lib_path() -> str:
    for nm in _LIB_NAMES:
        p = os.path.join(_DIR, nm)
        if os.path.exists(p):
            return p
    return ""


def _sources_present() -> bool:
    """Скачаны ли исходники Nuked-OPN2? (ym3438.c/.h не входят в репозиторий — см. opn2/README.md).
    Are the Nuked-OPN2 sources fetched? (ym3438.c/.h are not bundled — see opn2/README.md)."""
    return (os.path.exists(os.path.join(_DIR, "ym3438.c"))
            and os.path.exists(os.path.join(_DIR, "ym3438.h")))


def _ensure_built() -> str:
    p = _lib_path()
    if p:
        return p
    # FM-музыка НЕОБЯЗАТЕЛЬНА: без исходников Nuked-OPN2 просто остаёмся недоступны — никакой
    # сборки/подпроцесса. Всё остальное (графика, текст, PCM-сэмплы, карты) работает без них.
    # FM music is OPTIONAL: without the Nuked-OPN2 sources we just stay unavailable — no build,
    # no subprocess. Everything else (graphics, text, PCM samples, maps) works without them.
    if not _sources_present():
        raise RuntimeError(
            "Nuked-OPN2 sources are not present — FM music is disabled. "
            "See ztextractor/opn2/README.md to enable sound.")
    script = os.path.join(_DIR, "build.sh")
    if os.path.exists(script):
        subprocess.run(["sh", script], cwd=_DIR, check=False,
                       capture_output=True, timeout=120)
    p = _lib_path()
    if not p:
        raise RuntimeError(
            "Failed to build the YM2612 emulator (Nuked-OPN2). Check your C compiler "
            f"and run it manually: sh {script}")
    return p


_lib = None
_load_failed = False


def _load():
    global _lib, _load_failed
    if _lib is not None:
        return _lib
    if _load_failed:                       # не пытаемся повторно (без повторных сборок)
        raise RuntimeError("YM2612 emulator unavailable.")
    try:
        lib = ctypes.CDLL(_ensure_built())
        lib.opn2_size.restype = ctypes.c_int
        lib.opn2_reset.argtypes = [ctypes.c_char_p]
        lib.opn2_settype.argtypes = [ctypes.c_int]
        lib.opn2_wr.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.opn2_gen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int]
        _lib = lib
        return _lib
    except Exception:
        _load_failed = True                # кешируем провал — больше не пробуем/не собираем
        raise


_avail = None


def available() -> bool:
    """Можно ли использовать FM-синтез (библиотека есть/собирается). Результат кешируется —
    без Nuked-OPN2 повторных попыток сборки не будет.
    Whether FM synthesis is usable (library present/buildable). Cached — without Nuked-OPN2
    there are no repeated build attempts."""
    global _avail
    if _avail is None:
        try:
            _load()
            _avail = True
        except Exception:
            _avail = False
    return _avail


class Ym2612:
    """Один экземпляр чипа YM2612. write(part,addr,data) — задать регистр;
    generate(n) — отрендерить n стерео-сэмплов (bytes int16 LE interleaved)."""

    def __init__(self, ym2612_mode: bool = True):
        lib = _load()
        self._lib = lib
        self._chip = ctypes.create_string_buffer(lib.opn2_size())
        lib.opn2_settype(1 if ym2612_mode else 2)   # 1=YM2612(MD1), 2=YM3438 readmode
        lib.opn2_reset(self._chip)

    def write(self, part: int, addr: int, data: int) -> None:
        """Записать регистр. part 0 = каналы 1-3 (порт 0/1), part 1 = каналы 4-6 (порт 2/3)."""
        self._lib.opn2_wr(self._chip, 2 if part else 0, addr & 0xFF, data & 0xFF)

    def generate(self, n: int) -> bytes:
        """n стерео-сэмплов @NATIVE_RATE → bytes (int16 LE, L,R interleaved)."""
        buf = (ctypes.c_int16 * (n * 2))()
        self._lib.opn2_gen(self._chip, buf, n)
        return bytes(buf)


def note_to_fnum_block(midi_note: float):
    """MIDI-нота (полутоны, A4=69=440Гц) → (block 0-7, fnum 0-2047) для YM2612.

    f = fnum * clock / (144 * 2^(20-block)). Подбираем block так, чтобы fnum попал в 0x400..0x7FF.
    """
    freq = 440.0 * 2.0 ** ((midi_note - 69) / 12.0)
    block = 4
    # YM2612: freq = fnum * clock / (144 * 2^(21-block)) → fnum = freq*144*2^(21-block)/clock
    while block < 7:
        fnum = freq * 144.0 * (2 ** (21 - block)) / _YM2612_CLOCK
        if fnum < 0x800:
            break
        block += 1
    while block > 0:
        fnum = freq * 144.0 * (2 ** (21 - block)) / _YM2612_CLOCK
        if fnum >= 0x400:
            break
        block -= 1
    fnum = int(round(freq * 144.0 * (2 ** (21 - block)) / _YM2612_CLOCK))
    fnum = max(0, min(0x7FF, fnum))
    return block, fnum


if __name__ == "__main__":   # быстрый тест-тон
    import wave, struct
    chip = Ym2612()
    for op in (0, 4, 8, 12):
        chip.write(0, 0x30 + op, 0x01); chip.write(0, 0x40 + op, 0x00)
        chip.write(0, 0x50 + op, 0x1F); chip.write(0, 0x80 + op, 0x0F)
    chip.write(0, 0xB0, 0x07); chip.write(0, 0xB4, 0xC0)
    blk, fnum = note_to_fnum_block(69)
    chip.write(0, 0xA4, (blk << 3) | (fnum >> 8)); chip.write(0, 0xA0, fnum & 0xFF)
    chip.write(0, 0x28, 0xF0)
    pcm = chip.generate(NATIVE_RATE // 2)
    w = wave.open("/tmp/opn2_selftest.wav", "wb")
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(NATIVE_RATE)
    w.writeframes(pcm); w.close()
    print("self-test -> /tmp/opn2_selftest.wav  (A4=440 Hz)")
