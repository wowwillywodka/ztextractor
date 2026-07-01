# Nuked-OPN2 (YM2612) — fetch separately / качается отдельно

FM-music playback uses the **Nuked-OPN2** YM2612 emulator. Its sources are **not** bundled here
(LGPL-2.1). To enable FM music, download the two files into **this folder**:

```
ym3438.c
ym3438.h
```
from <https://github.com/nukeykt/Nuked-OPN2>. Then `build.sh` compiles `wrap.c` + `ym3438.c` into
`libopn2.dylib` (macOS) / `libopn2.so` (Linux) — the tool also builds it automatically on first use if a
C compiler is available. Everything except FM-music playback works without these files.

`wrap.c` and `build.sh` are part of this project (a thin ctypes wrapper). `ym3438.*` and the compiled
`libopn2.*` are git-ignored.

---

Воспроизведение FM-музыки использует эмулятор YM2612 **Nuked-OPN2**. Его исходники **не** входят в
репозиторий (LGPL-2.1). Чтобы включить FM-музыку, скачай два файла в **эту папку**:

```
ym3438.c
ym3438.h
```
с <https://github.com/nukeykt/Nuked-OPN2>. Затем `build.sh` соберёт `wrap.c` + `ym3438.c` в
`libopn2.dylib` (macOS) / `libopn2.so` (Linux) — инструмент также собирает её автоматически при первом
использовании, если есть компилятор C. Всё, кроме воспроизведения FM-музыки, работает без этих файлов.

`wrap.c` и `build.sh` — часть этого проекта (тонкая обёртка ctypes). `ym3438.*` и собранная
`libopn2.*` — в `.gitignore`.
