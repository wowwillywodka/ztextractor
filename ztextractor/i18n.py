# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Willy Wodka
"""Минимальная локализация GUI: английский / русский (по умолчанию английский).
Minimal GUI localization: English / Russian (English by default).

Использование / Usage:  tr("русский текст", "English text")
Язык переключается из меню «Language» и сохраняется в настройках (см. gui.py);
смена применяется после перезапуска. При импорте язык читается из QSettings, чтобы
строки уровня модуля (таблицы данных) тоже отображались на выбранном языке.
The language is switched from the «Language» menu and saved in settings (see gui.py);
the change applies after a restart. On import the language is read from QSettings so that
module-level strings (data tables) are shown in the chosen language too.
"""
from __future__ import annotations

_LANG = "en"  # по умолчанию английский / English by default


def set_lang(lang: str) -> None:
    """Установить язык: 'en' или 'ru'. / Set language: 'en' or 'ru'."""
    global _LANG
    _LANG = "ru" if str(lang).lower().startswith("ru") else "en"


def get_lang() -> str:
    return _LANG


def tr(ru: str, en: str) -> str:
    """ru — оригинал в коде, en — перевод; возвращает строку текущего языка.
    ru is the in-code original, en the translation; returns the current-language string."""
    return ru if _LANG == "ru" else en


# Строки версий — это ИДЕНТИФИКАТОРЫ (ключи таблиц данных, сравнения rom.version),
# их трогать нельзя. Локализуем только ОТОБРАЖЕНИЕ через version_label().
# Version strings are IDENTIFIERS (data-table keys, rom.version comparisons) and must
# not change; only their DISPLAY is localized via version_label().
_VERSION_EN = {
    "Zero Tolerance (релиз)": "Zero Tolerance (release)",
    "Zero Tolerance (немецкая)": "Zero Tolerance (German)",
    "Zero Tolerance Underground (мод v1.5)": "Zero Tolerance Underground (mod v1.5)",
    "Beyond Zero Tolerance (прототип 1995-06-23)": "Beyond Zero Tolerance (prototype 1995-06-23)",
    "Beyond Zero Tolerance (прототип 1995-07-14)": "Beyond Zero Tolerance (prototype 1995-07-14)",
    "Неизвестный ROM Mega Drive": "Unknown Mega Drive ROM",
}


def version_label(v: str) -> str:
    """Локализованная подпись версии для показа; сам v остаётся ключом-идентификатором.
    Localized display label for a version string; v itself stays the identity key."""
    if _LANG == "ru":
        return v
    return _VERSION_EN.get(v, v)


def _init_from_settings() -> None:
    """Прочитать язык из QSettings при импорте (до создания QApplication это работает
    на чтение). Если Qt недоступен — остаёмся на английском.
    Read the language from QSettings at import time (reading works before QApplication
    exists). If Qt is unavailable we stay on English."""
    try:
        from PySide6.QtCore import QSettings
        set_lang(QSettings("ztextractor", "ztextractor").value("lang", "en"))
    except Exception:
        pass


_init_from_settings()
