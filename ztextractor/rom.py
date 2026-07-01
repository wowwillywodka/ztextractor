"""Загрузка ROM-образов Sega Mega Drive / Genesis.

Поддерживает «сырые» дампы (.bin/.md/.gen) и чересстрочный формат SMD (.smd),
который автоматически распознаётся и деинтерливится в линейный вид.
"""

from __future__ import annotations

from dataclasses import dataclass

SMD_BLOCK = 0x4000  # SMD хранит ROM блоками по 16 КБ


@dataclass
class Rom:
    """Загруженный ROM в линейном (деинтерливленном) виде."""

    data: bytes
    path: str
    is_smd: bool
    title: str
    version: str

    @property
    def size(self) -> int:
        return len(self.data)


def detect_version(data: bytes) -> str:
    """Различить Zero Tolerance и два прототипа Beyond ZT.

    Прототипы унаследовали заголовок ZT, поэтому различаем по серийнику и размеру:
    релиз ZT = '-01', прототипы = '-00'; прототипы — по размеру (2 МБ / 3 МБ).
    """
    serial = data[0x180:0x18E].decode("ascii", "replace") if len(data) >= 0x18E else ""
    size = len(data)
    if size >= 0x300000:
        return "Beyond Zero Tolerance (прототип 1995-07-14)"
    if "-01" in serial:
        return "Zero Tolerance (релиз)"
    if "-00" in serial:
        # Zero Tolerance Underground (офиц. мод v1.5, ZEROTOL_1_5): на движке релиза,
        # но всё переразмечено. Маркер: своя таблица имён уровней @0x3174 = "SUBWAY ...".
        if b"SUBWAY" in data[0x3170:0x3190]:
            return "Zero Tolerance Underground (мод v1.5)"
        # Немецкий релиз (люди-враги заменены на инопланетян) = структура релиза,
        # но серийник -00. Маркер: таблица имён уровней @0x30B2 (у прото там мусор).
        if b"DOCKING BAY" in data[0x30B0:0x30C4]:
            return "Zero Tolerance (немецкая)"
        return "Beyond Zero Tolerance (прототип 1995-06-23)"
    return "Неизвестный ROM Mega Drive"


def looks_like_smd(raw: bytes) -> bool:
    """Эвристика распознавания SMD: 512-байтный заголовок + магия 0xAA 0xBB."""
    if len(raw) < 512:
        return False
    if (len(raw) - 512) % SMD_BLOCK != 0:
        return False
    return raw[8] == 0xAA and raw[9] == 0xBB


def deinterleave_smd(raw: bytes) -> bytes:
    """Развернуть SMD: в каждом 16 КБ блоке первая половина — нечётные байты,
    вторая — чётные."""
    body = raw[512:]
    out = bytearray(len(body))
    for base in range(0, len(body), SMD_BLOCK):
        block = body[base : base + SMD_BLOCK]
        half = len(block) // 2
        odd = block[:half]
        even = block[half:]
        for i in range(half):
            out[base + 2 * i] = even[i]
            out[base + 2 * i + 1] = odd[i]
    return bytes(out)


def _read_title(data: bytes) -> str:
    """Международное название игры из заголовка MD (0x150, 48 байт)."""
    if len(data) < 0x180:
        return ""
    raw = data[0x150:0x180]
    text = raw.decode("ascii", "replace")
    return " ".join(text.split()).strip()


def load_rom(path: str, mode: str = "auto") -> Rom:
    """Загрузить ROM. mode: 'auto' | 'raw' | 'smd'."""
    with open(path, "rb") as f:
        raw = f.read()

    if mode == "auto":
        is_smd = looks_like_smd(raw)
    else:
        is_smd = mode == "smd"

    data = deinterleave_smd(raw) if is_smd else raw
    return Rom(
        data=data, path=path, is_smd=is_smd,
        title=_read_title(data), version=detect_version(data),
    )
