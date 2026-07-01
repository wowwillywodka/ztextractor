# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Willy Wodka
# Portions derived from realmonster/GEMS (https://github.com/realmonster/GEMS), LGPL-3.0-or-later.

"""GEMS-плеер: рендер песни (секвенции) в PCM через эмулятор YM2612 (Nuked-OPN2).

Связывает gems_music (декод событий + FM-патчи) и opn2_chip (чип). Тайминг по драйверу
GEMS (выверено по realmonster/GEMS gems_to_midi.cpp):
  • tempo (событие 0x68 = байт+40) = ТИКОВ В СЕКУНДУ → сэмплов/тик = NATIVE_RATE / tempo;
  • per-канал «липкие» delay/duration (многобайт, база 0x40); событие `note` = key-on сейчас +
    key-off через `duration` тиков, затем время += `delay`; прочие события тоже += delay (delayadd);
  • patch → загрузка 39-байтного FM-патча в регистры голоса; loop 0x64 / loopend 0x65.

v1: только FM-голоса (6). DAC-ударные (нота на DAC-патче) и PSG-шум — TODO (помечено).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import gems_music as gm
from . import opn2_chip

# YM2612: смещение регистра для ЛОГИЧ. оператора 1..4 внутри канала. Аппаратный порядок
# слотов = 1,3,2,4 (смещения 0,4,8,12 = op1,op3,op2,op4), поэтому логич. op2→8, op3→4.
_OP_OFF = (0, 8, 4, 12)
# Тайминг GEMS: 1 тик = 2.5/tempo сек (gems_to_midi: мкс/четверть=60e6/tempo, division=24).
_TICK_SCALE = 2.5
# Несущие операторы по алгоритму FM (бит i = логич.op i+1 — несущая, её TL = громкость канала).
# ALG 0-3: только op4; 4: op2,op4; 5,6: op2,op3,op4; 7: все. Громкость канала += TL несущих.
_ALG_CARRIERS = (0x8, 0x8, 0x8, 0x8, 0xA, 0xE, 0xE, 0xF)


def _voice_addr(voice: int):
    """FM-голос 0..5 → (part 0/1, ch_offset 0..2, key-маска канала для рег.0x28)."""
    part = 0 if voice < 3 else 1
    cofs = voice % 3
    keych = cofs if part == 0 else (cofs + 4)
    return part, cofs, keych


class _Chan:
    """Состояние одного канала песни (субсеквенции)."""
    __slots__ = ("idx", "pos", "wait", "delay", "duration", "wasdelay", "wasduration",
                 "patch", "patch_type", "voice", "note", "note_off", "done",
                 "loop_stack", "looped", "volume")

    def __init__(self, idx: int, pos: int):
        self.idx = idx
        self.pos = pos
        self.wait = 0            # тиков до обработки следующего события
        self.delay = 0
        self.duration = 0
        self.wasdelay = False
        self.wasduration = False
        self.patch = -1
        self.patch_type = None   # 0 FM, 1 DAC, 2 PSG, 3 NOISE
        self.voice = None        # текущий FM-голос (динамически)
        self.note = None         # текущая играющая нота
        self.note_off = None     # тик key-off
        self.done = False
        self.loop_stack = []     # [(return_pos, remaining)]
        self.looped = 0          # сколько раз отыграл свой луп (для останова)
        self.volume = 0          # аттенюация TL несущих (0 = громче всего)


class GemsPlayer:
    def __init__(self, data: bytes, banks: Dict[str, int], ym2612_mode: bool = True):
        self.d = data
        self.banks = banks
        self.seq = banks["sequences"]
        self.chip = opn2_chip.Ym2612(ym2612_mode=ym2612_mode)   # True=YM2612(MD1), False=YM3438
        self._patch_cache: Dict[int, Optional[Dict]] = {}
        self._patch_ov: Dict[int, int] = {}        # канал → принудит. патч (Jukebox)
        self._trans_ov: Dict[int, int] = {}        # канал → доп. транспонирование (Jukebox)

    # ── инструменты ──
    def _patch_addr(self, idx: int) -> Optional[int]:
        pb = self.banks["patches"]
        first = self.d[pb] | (self.d[pb + 1] << 8)
        if not (0 <= idx < first // 2):
            return None
        off = self.d[pb + idx * 2] | (self.d[pb + idx * 2 + 1] << 8)
        return pb + off

    def _patch_type(self, idx: int) -> Optional[int]:
        a = self._patch_addr(idx)
        return None if a is None else self.d[a]

    def _fm_regs(self, idx: int) -> Optional[Dict]:
        if idx not in self._patch_cache:
            a = self._patch_addr(idx)
            self._patch_cache[idx] = (gm.fm_patch_to_ym2612(bytes(self.d[a:a + 39]))
                                      if a is not None else None)
        return self._patch_cache[idx]

    def _load_fm_patch(self, voice: int, idx: int):
        regs = self._fm_regs(idx)
        if regs is None:
            return
        part, cofs, _ = _voice_addr(voice)
        for op in range(4):
            base = _OP_OFF[op] + cofs
            r = regs["op"][op]
            for reg in (0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90):
                self.chip.write(part, reg + base, r[reg])
        self.chip.write(part, 0xB0 + cofs, regs["B0"])
        self.chip.write(part, 0xB4 + cofs, regs["B4"])
        self.chip.write(0, 0x22, regs["lfo"])           # LFO глобальный

    # ── динамическая раздача 6 FM-голосов (8 каналов > 6 голосов на железе) ──
    def _alloc_voice(self, ch: _Chan, tick: int, state: Dict, chans) -> Optional[int]:
        voices = state["voices"]
        if ch.voice is not None and voices[ch.voice]["owner"] is ch:   # уже держит голос
            voices[ch.voice]["last"] = tick
            return ch.voice
        for v in range(6):                              # свободный голос
            if voices[v]["owner"] is None:
                voices[v].update(owner=ch, last=tick)
                ch.voice = v
                return v
        # все заняты → вытеснить голос, чья нота РАНЬШЕ всего отпустится (минимум слышимого
        # обрыва), при равенстве — давний (LRU). Лучше чистого LRU при 8 каналах на 6 голосов.
        v = min(range(6), key=lambda x: (voices[x]["off"], voices[x]["last"]))
        victim = voices[v]["owner"]
        if victim is not None and victim is not ch:
            _, _, vkey = _voice_addr(v)
            self.chip.write(0, 0x28, vkey)              # key-off жертвы
            victim.voice = None
            victim.note = None
            victim.note_off = None
        voices[v].update(owner=ch, last=tick)
        ch.voice = v
        return v

    def _note_on(self, ch: _Chan, note: int, transpose: int, tick: int, state: Dict, chans):
        v = self._alloc_voice(ch, tick, state, chans)
        if v is None:
            return
        part, cofs, keych = _voice_addr(v)
        # Jukebox: принудит. патч и доп. транспонирование по каналу (иначе — из песни)
        eff_patch = self._patch_ov.get(ch.idx, ch.patch)
        eff_trans = transpose + self._trans_ov.get(ch.idx, 0)
        regs = self._fm_regs(eff_patch)
        if state["voices"][v]["patch"] != eff_patch:    # сменить инструмент голоса при нужде
            self._load_fm_patch(v, eff_patch)
            state["voices"][v]["patch"] = eff_patch
            state["voices"][v]["vol"] = None
        # ГРОМКОСТЬ канала = аттенюация TL несущих (модуляторы не трогаем). Пишем при смене.
        if regs is not None and state["voices"][v].get("vol") != ch.volume:
            carriers = _ALG_CARRIERS[regs["alg"]]
            for op in range(4):
                if carriers & (1 << op):
                    tl = min(0x7F, regs["op"][op][0x40] + ch.volume)
                    self.chip.write(part, 0x40 + _OP_OFF[op] + cofs, tl)
            state["voices"][v]["vol"] = ch.volume
        if ch.note is not None:                         # key-off перед key-on = ретриггер (без слипания нот)
            self.chip.write(0, 0x28, keych)
        block, fnum = opn2_chip.note_to_fnum_block(note + eff_trans)
        self.chip.write(part, 0xA4 + cofs, (block << 3) | (fnum >> 8))
        self.chip.write(part, 0xA0 + cofs, fnum & 0xFF)
        keymask = (regs["key"] if regs else 0xF) & 0xF
        self.chip.write(0, 0x28, (keymask << 4) | keych)
        ch.note = note

    def _note_off(self, ch: _Chan, state: Dict):
        if ch.voice is None or ch.note is None:
            return
        _, _, keych = _voice_addr(ch.voice)
        self.chip.write(0, 0x28, keych)                 # маска операторов 0 = key-off
        state["voices"][ch.voice]["owner"] = None       # освободить голос
        ch.voice = None
        ch.note = None

    # ── обработка событий канала до паузы (wait>0) ──
    def _advance(self, ch: _Chan, tick: int, state: Dict, transpose: int, chans):
        d = self.d
        seq = self.seq
        guard = 0
        while not ch.done and ch.wait == 0 and guard < 10000:
            guard += 1
            c = d[seq + ch.pos]
            if c >= 0xC0:                               # delay (многобайт)
                ch.delay = (ch.delay * 0x40 + (c - 0xC0)) if ch.wasdelay else (c - 0xC0)
                ch.wasdelay = True
                ch.wasduration = False
                ch.pos += 1
                continue
            if c >= 0x80:                               # duration (многобайт)
                ch.duration = (ch.duration * 0x40 + (c - 0x80)) if ch.wasduration else (c - 0x80)
                ch.wasduration = True
                ch.wasdelay = False
                ch.pos += 1
                continue
            if c < 0x60:                                # НОТА
                ch.pos += 1
                if ch.patch_type == 0:                  # FM (DAC/PSG-ноты — TODO)
                    self._note_on(ch, c, transpose, tick, state, chans)
                    ch.note_off = tick + max(1, ch.duration)
                    if ch.voice is not None:            # когда этот голос освободится (для вытеснения)
                        state["voices"][ch.voice]["off"] = ch.note_off
                self._delayadd(ch)
                continue
            if c == 0x60:                               # eos
                ch.done = True
                return
            if c == 0x65:                               # loopend
                ch.pos += 1
                if ch.loop_stack:
                    ret, rem = ch.loop_stack[-1]
                    if rem == 0x7F:                     # 0x7F = БЕСКОНЕЧНЫЙ луп
                        ch.pos = ret
                        ch.looped += 1                  # отметить полный прогон (для останова)
                    elif rem > 1:
                        ch.loop_stack[-1] = (ret, rem - 1)
                        ch.pos = ret
                        ch.looped += 1
                    else:
                        ch.loop_stack.pop()
                ch.wasdelay = ch.wasduration = False
                continue
            # команды 0x61..0x72
            ln = gm._SEQ_FIXED_LEN.get(c, 1)
            arg = d[seq + ch.pos + 1] if ln >= 2 else 0
            if c == 0x61:                               # patch (голос грузится при note-on)
                ch.patch = arg
                ch.patch_type = self._patch_type(arg)
                ch.pos += ln
                self._delayadd(ch)
                continue
            if c == 0x64:                               # loop N
                ch.pos += ln
                ch.loop_stack.append((ch.pos, arg))     # arg=0x7F → бесконечно
                ch.wasdelay = ch.wasduration = False
                continue
            if c == 0x68:                               # tempo
                state["tempo"] = arg + 40
                ch.pos += ln
                self._delayadd(ch)
                continue
            if c == 0x6D:                               # sfx-timebase
                state["tempo"] = 150
                ch.pos += 1
                self._delayadd(ch)
                continue
            if c == 0x6F:                               # goto (word)
                k = d[seq + ch.pos + 1] | (d[seq + ch.pos + 2] << 8)
                ch.pos = k
                ch.wasdelay = ch.wasduration = False
                continue
            if c == 0x72:                               # extra: sub 5 = volume канала
                sub = d[seq + ch.pos + 1]
                val = d[seq + ch.pos + 2]
                if sub == 5:
                    ch.volume = val
                ch.pos += ln
                self._delayadd(ch)
                continue
            # прочие (modulation/priority/play/samplerate/pitch/mute/...) — пропуск + delayadd
            ch.pos += ln
            self._delayadd(ch)

    def _delayadd(self, ch: _Chan):
        ch.wait = ch.delay
        ch.wasdelay = False
        ch.wasduration = False

    # ── рендер песни ──
    def render_song(self, index: int, max_seconds: float = 150.0,
                    transpose: int = 12, gain: float = 6.0, loops: int = 1,
                    speed: float = 1.0, patch_overrides: Optional[Dict[int, int]] = None,
                    transpose_overrides: Optional[Dict[int, int]] = None) -> bytes:
        """Песня index → PCM (int16 LE стерео @NATIVE_RATE).

        Играет до ОДНОГО полного прохода: останов когда КАЖДЫЙ канал либо дошёл до eos,
        либо отыграл свой луп `loops` раз (зацикленные песни не обрезаются на полуслове).
        max_seconds — страховочный потолок. gain — усиление (один FM-голос тихий)."""
        self._patch_ov = patch_overrides or {}
        self._trans_ov = transpose_overrides or {}
        song = gm.parse_song(self.d, self.seq, index)
        if not song:
            return b""
        chans = [_Chan(i, c["ptr"]) for i, c in enumerate(song["channels"])]
        state = {"tempo": 120,
                 "voices": [{"owner": None, "patch": None, "last": -1, "off": 0}
                            for _ in range(6)]}
        rate = opn2_chip.NATIVE_RATE
        max_ticks = int(max_seconds * 300)              # страховочный потолок тиков
        out = bytearray()
        acc = 0.0
        tick = 0
        max_samples = int(max_seconds * rate)
        produced = 0
        while tick < max_ticks and produced < max_samples:
            for ch in chans:
                if not ch.done:
                    self._advance(ch, tick, state, transpose, chans)
            for ch in chans:                            # key-off по расписанию
                if ch.note_off is not None and tick >= ch.note_off:
                    self._note_off(ch, state)
                    ch.note_off = None
            # ОСТАНОВ: все каналы закончили (eos) или отыграли полный луп
            if all(ch.done or ch.looped >= loops for ch in chans):
                for ch in chans:                        # снять все ноты
                    self._note_off(ch, state)
                out += self.chip.generate(int(0.4 * rate))     # хвост огибающих
                break
            acc += rate * _TICK_SCALE / (max(1, state["tempo"]) * max(0.1, speed))
            nsamp = int(acc)
            acc -= nsamp
            if nsamp:
                out += self.chip.generate(nsamp)
                produced += nsamp
            for ch in chans:
                if ch.wait > 0:
                    ch.wait -= 1
            tick += 1
        # НОРМАЛИЗАЦИЯ по пику к ровному уровню (один FM-голос тихий + аттенюация громкости
        # делают уровни неровными между песнями). gain — доп.множитель/потолок усиления.
        return _normalize_s16(bytes(out), target=28000, max_gain=gain * 8.0)


def render_patch_note(data: bytes, banks: Dict[str, int], patch_idx: int,
                      note: int = 60, hold: float = 0.35, tail: float = 1.4,
                      ym2612_mode: bool = True, dst_rate: int = 44100) -> bytes:
    """Сыграть ОДИН патч одной нотой (как игра триггерит FM-SFX) → PCM int16 LE стерео.

    Для прослушивания инструментов/SFX: 30 FM-патчей ZT не звучат в музыке = это SFX
    (выбор меню, лифт, двери, граната, писк робота, выстрел врага, выбор оружия…).
    """
    player = GemsPlayer(data, banks, ym2612_mode=ym2612_mode)
    regs = player._fm_regs(patch_idx)
    if regs is None:
        return b""
    chip = player.chip
    player._load_fm_patch(0, patch_idx)
    part, cofs, keych = _voice_addr(0)
    block, fnum = opn2_chip.note_to_fnum_block(note)
    chip.write(part, 0xA4 + cofs, (block << 3) | (fnum >> 8))
    chip.write(part, 0xA0 + cofs, fnum & 0xFF)
    keymask = (regs["key"] or 0xF) & 0xF
    chip.write(0, 0x28, (keymask << 4) | keych)          # key-on
    rate = opn2_chip.NATIVE_RATE
    out = bytearray(chip.generate(int(hold * rate)))     # удержание ноты
    chip.write(0, 0x28, keych)                           # key-off
    out += chip.generate(int(tail * rate))               # хвост огибающей
    pcm = _normalize_s16(bytes(out), 28000)
    if dst_rate != rate and pcm:
        pcm = _resample_s16_stereo(pcm, rate, dst_rate)
    return pcm


def music_patch_usage(data: bytes, banks: Dict[str, int]):
    """(used:set, all:[(idx,type)…]) — патчи, реально звучащие в музыке, и весь банк.
    Неиспользованные FM-патчи = кандидаты в SFX."""
    from . import gems_music as gm
    pb = banks["patches"]
    ntot = (data[pb] | (data[pb + 1] << 8)) // 2
    all_p = []
    for i, addr in gm.patch_table(data, pb):
        all_p.append((i, data[addr]))
    used = set()
    for s in range(gm.song_count(data, banks["sequences"])):
        song = gm.parse_song(data, banks["sequences"], s)
        if not song:
            continue
        for ch in song["channels"]:
            for ev in ch["events"]:
                if ev.kind == "patch":
                    used.add(ev.value)
    return used, all_p


def render_song_wav(data: bytes, banks: Dict[str, int], index: int,
                    max_seconds: float = 150.0, transpose: int = 12,
                    gain: float = 6.0, dst_rate: int = 44100,
                    ym2612_mode: bool = True, speed: float = 1.0,
                    patch_overrides=None, transpose_overrides=None) -> bytes:
    """Песня → WAV-байты (стерео). Ресемпл NATIVE_RATE→dst_rate (линейный)."""
    import io
    import wave
    pcm = GemsPlayer(data, banks, ym2612_mode=ym2612_mode).render_song(
        index, max_seconds, transpose, gain, speed=speed,
        patch_overrides=patch_overrides, transpose_overrides=transpose_overrides)
    if dst_rate != opn2_chip.NATIVE_RATE and pcm:
        pcm = _resample_s16_stereo(pcm, opn2_chip.NATIVE_RATE, dst_rate)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(dst_rate)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()


def _normalize_s16(pcm: bytes, target: int = 28000, max_gain: float = 48.0) -> bytes:
    """Нормализовать int16-PCM по пику к target (с потолком усиления max_gain)."""
    import array
    a = array.array("h")
    a.frombytes(pcm)
    if not len(a):
        return pcm
    peak = max((abs(x) for x in a), default=0)
    if peak == 0:
        return pcm
    g = min(max_gain, target / peak)
    if abs(g - 1.0) < 0.02:
        return pcm
    for i, v in enumerate(a):
        v = int(v * g)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a.tobytes()


def _resample_s16_stereo(pcm: bytes, src: int, dst: int) -> bytes:
    import array
    a = array.array("h")
    a.frombytes(pcm)
    n = len(a) // 2
    n_out = max(1, round(n * dst / src))
    out = array.array("h", bytes(4 * n_out))
    step = src / dst
    pos = 0.0
    for i in range(n_out):
        idx = int(pos)
        if idx >= n - 1:
            out[2 * i] = a[2 * (n - 1)]
            out[2 * i + 1] = a[2 * (n - 1) + 1]
        else:
            f = pos - idx
            out[2 * i] = int(a[2 * idx] * (1 - f) + a[2 * (idx + 1)] * f)
            out[2 * i + 1] = int(a[2 * idx + 1] * (1 - f) + a[2 * (idx + 1) + 1] * f)
        pos += step
    return out.tobytes()
