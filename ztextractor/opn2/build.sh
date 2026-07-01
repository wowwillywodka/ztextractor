#!/bin/sh
# Сборка Nuked-OPN2 (эмулятор YM2612) в разделяемую библиотеку для ctypes.
# Исходники ym3438.c/.h — nukeykt/Nuked-OPN2 (LGPL 2.1). wrap.c — наша тонкая обёртка.
cd "$(dirname "$0")"
case "$(uname)" in
  Darwin) OUT=libopn2.dylib; FLAGS="-dynamiclib" ;;
  *)      OUT=libopn2.so;    FLAGS="-shared -fPIC" ;;
esac
cc -O2 $FLAGS -o "$OUT" ym3438.c wrap.c && echo "built $OUT"
