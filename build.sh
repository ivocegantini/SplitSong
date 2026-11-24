#!/usr/bin/env bash
# Compila o SplitSong para Linux com o PyInstaller (executável único).
#   - usa o Python do .venv (crie com ./setup.sh)
#   - gera dist/SplitSong e copia para bin/SplitSong
#   - testa o executável gerado (--help)
#
# Uso: ./build.sh [--limpar] [--sem-copia]
#   --limpar      apaga build/ e dist/ antes de compilar
#   --sem-copia   não copia o executável para bin/
#
# O PyInstaller não compila para outro sistema: o .exe precisa ser gerado
# no Windows (build.bat ou build.ps1).

set -euo pipefail
cd "$(dirname "$0")"

NAME="SplitSong"
ENTRY="split_songs.py"
VENV_DIR=".venv"
CLEAN=0
COPY=1

for arg in "$@"; do
    case "$arg" in
        --limpar|--clean) CLEAN=1 ;;
        --sem-copia|--no-copy) COPY=0 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Opção desconhecida: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\n== %s\n' "$*"; }
ok()   { printf '   [ok] %s\n' "$*"; }
warn() { printf '   [aviso] %s\n' "$*"; }
fail() { printf '\nErro: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux*) ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows detectado; executando o build.ps1"
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File build.ps1 "$@"
        ;;
    *) fail "sistema não suportado: $(uname -s) (use Linux ou Windows)" ;;
esac

info "Verificando o ambiente"
PY="$VENV_DIR/bin/python"
[ -x "$PY" ] || fail "$VENV_DIR não encontrado; rode ./setup.sh primeiro"
"$PY" -m PyInstaller --version >/dev/null 2>&1 \
    || fail "PyInstaller não instalado no $VENV_DIR; rode ./setup.sh"
ok "$("$PY" --version)"
ok "PyInstaller $("$PY" -m PyInstaller --version)"

if [ "$CLEAN" -eq 1 ]; then
    info "Limpando build/ e dist/"
    rm -rf build dist
    ok "limpo"
fi

info "Compilando $NAME"
"$PY" -m PyInstaller --noconfirm --onefile --console \
    --name "$NAME" \
    --distpath dist --workpath build --specpath build \
    "$ENTRY"

EXE="dist/$NAME"
[ -x "$EXE" ] || fail "o PyInstaller não gerou $EXE"
ok "$EXE ($(du -h "$EXE" | cut -f1))"

info "Testando o executável"
"$EXE" --help >/dev/null || fail "$EXE --help falhou"
ok "$EXE --help executa"

if [ "$COPY" -eq 1 ]; then
    mkdir -p bin
    cp -f "$EXE" "bin/$NAME"
    chmod +x "bin/$NAME"
    if [ -f bin/.env ]; then chmod 600 bin/.env; fi  # chaves das APIs: só o dono lê
    ok "copiado para bin/$NAME"
fi

info "Pronto"
cat <<EOF
   Executável: $([ "$COPY" -eq 1 ] && echo "bin/$NAME" || echo "$EXE")
   As pastas Input, Temp e Output e o .env ficam ao lado do executável.
   Ele precisa do ffmpeg/ffprobe no PATH (e do yt-dlp para o --download).
EOF
