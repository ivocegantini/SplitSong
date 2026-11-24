#!/usr/bin/env bash
# Roda os testes do SplitSong (pasta tests/) no Linux.
#   - usa o Python do .venv se existir (crie com ./setup.sh); senão o python3 do sistema
#   - os testes de integração precisam do ffmpeg/ffprobe no PATH (sem eles são pulados)
#
# Uso: ./test.sh [--exigir-ffmpeg] [opções do unittest]
#   --exigir-ffmpeg   falha (em vez de pular) se o ffmpeg/ffprobe não estiverem no PATH
#   -k PADRAO         roda só os testes cujo nome contém PADRAO (ex.: -k Publish)
#   -q                saída resumida (o padrão é -v, um teste por linha)
#
# No Windows use o test.ps1 (ou test.bat).

set -euo pipefail
cd "$(dirname "$0")"

VERBOSITY="-v"
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --exigir-ffmpeg|--require-ffmpeg) export SPLITSONG_REQUIRE_FFMPEG=1 ;;
        -q|--quiet) VERBOSITY="" ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows detectado; executando o test.ps1"
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File test.ps1 "$@"
        ;;
esac

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "Erro: Python 3 não encontrado (rode ./setup.sh)" >&2
    exit 1
fi

printf '== %s\n' "$("$PY" --version)"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    printf '== ffmpeg: %s\n' "$(command -v ffmpeg)"
else
    printf '== ffmpeg/ffprobe não encontrados: os testes de integração %s\n' \
        "$([ "${SPLITSONG_REQUIRE_FFMPEG:-}" = 1 ] && echo "vão falhar" || echo "serão pulados")"
fi
echo

# Sem bytecode: não deixa __pycache__ em tests/
PYTHONDONTWRITEBYTECODE=1 exec "$PY" -m unittest discover -s tests $VERBOSITY ${ARGS[@]+"${ARGS[@]}"}
