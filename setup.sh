#!/usr/bin/env bash
# Prepara o ambiente para rodar e compilar o SplitSong no Linux:
#   - instala Python 3 (com venv/pip) e ffmpeg pelo gerenciador de pacotes
#   - cria o .venv e instala nele o requirements.txt (PyInstaller, yt-dlp)
#   - cria as pastas Input, Temp e Output
#   - verifica se está tudo funcionando
#
# Uso: ./setup.sh [--sem-sistema]
#   --sem-sistema   não instala pacotes do sistema (só cria o .venv)
#
# No Windows, use setup.bat (ou setup.ps1).

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"
MIN_PY="3.10"
SKIP_SYSTEM=0

for arg in "$@"; do
    case "$arg" in
        --sem-sistema|--no-system) SKIP_SYSTEM=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Opção desconhecida: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\n== %s\n' "$*"; }
ok()   { printf '   [ok] %s\n' "$*"; }
warn() { printf '   [aviso] %s\n' "$*"; }
fail() { printf '\nErro: %s\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------- #
# Ambiente
# --------------------------------------------------------------------------- #
info "Verificando o ambiente"
case "$(uname -s)" in
    Linux*)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            ok "Linux (WSL) - o executável gerado aqui será para Linux, não .exe"
        else
            ok "Linux"
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "   Windows detectado; executando o setup.ps1"
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File setup.ps1 "$@"
        ;;
    *)
        fail "sistema não suportado: $(uname -s) (use Linux ou Windows)"
        ;;
esac

# --------------------------------------------------------------------------- #
# Pacotes do sistema
# --------------------------------------------------------------------------- #
python_ok() {
    has python3 && python3 - "$MIN_PY" <<'EOF' >/dev/null 2>&1
import sys, venv, ensurepip
need = tuple(map(int, sys.argv[1].split(".")))
sys.exit(0 if sys.version_info[:2] >= need else 1)
EOF
}

if [ "$SKIP_SYSTEM" -eq 0 ]; then
    info "Pacotes do sistema"
    if python_ok && has ffmpeg && has ffprobe; then
        ok "Python 3, venv e ffmpeg já instalados"
    else
        SUDO=""
        if [ "$(id -u)" -ne 0 ]; then
            has sudo || fail "precisa de root ou sudo para instalar os pacotes"
            SUDO="sudo"
        fi
        if has apt-get; then
            $SUDO apt-get update
            $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg
        elif has dnf; then
            # No Fedora o ffmpeg completo vem do RPM Fusion; sem ele, use ffmpeg-free.
            $SUDO dnf install -y python3 python3-pip ffmpeg \
                || $SUDO dnf install -y python3 python3-pip ffmpeg-free
        elif has pacman; then
            $SUDO pacman -S --needed --noconfirm python python-pip ffmpeg
        elif has zypper; then
            $SUDO zypper --non-interactive install python3 python3-pip ffmpeg
        else
            fail "gerenciador de pacotes não reconhecido; instale Python >= $MIN_PY (com venv) e ffmpeg manualmente e rode ./setup.sh --sem-sistema"
        fi
        ok "pacotes instalados"
    fi
fi

python_ok || fail "Python >= $MIN_PY com o módulo venv não encontrado (no Debian/Ubuntu: sudo apt install python3-venv)"
ok "$(python3 --version)"

# --------------------------------------------------------------------------- #
# Ambiente virtual
# --------------------------------------------------------------------------- #
info "Ambiente virtual ($VENV_DIR)"
if [ -x "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
    ok "já existe, reaproveitando"
else
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
    ok "criado"
fi
PY="$VENV_DIR/bin/python"

"$PY" -m pip install --upgrade pip
"$PY" -m pip install --upgrade -r requirements.txt
ok "dependências instaladas"

# --------------------------------------------------------------------------- #
# Pastas de trabalho
# --------------------------------------------------------------------------- #
mkdir -p Input Temp Output

# --------------------------------------------------------------------------- #
# Verificação
# --------------------------------------------------------------------------- #
info "Verificação"
PROBLEMS=0
if has ffmpeg; then
    ok "ffmpeg $(ffmpeg -hide_banner -version | head -1 | awk '{print $3}')"
    if ffmpeg -hide_banner -muxers 2>/dev/null | grep -qw chromaprint; then
        ok "ffmpeg com chromaprint (necessário para o AcoustID)"
    else
        warn "este ffmpeg não tem chromaprint: o --rename só vai funcionar com o ACRCloud"
    fi
else
    warn "ffmpeg não encontrado"; PROBLEMS=1
fi
has ffprobe && ok "ffprobe" || { warn "ffprobe não encontrado"; PROBLEMS=1; }
ok "PyInstaller $("$PY" -m PyInstaller --version)"
ok "yt-dlp $("$PY" -m yt_dlp --version)"
if has deno; then
    ok "deno $(deno --version | head -1 | awk '{print $2}')"
else
    warn "deno não encontrado (opcional; o yt-dlp usa para os desafios JS do YouTube)."
    warn "Para instalar: curl -fsSL https://deno.land/install.sh | sh"
fi
"$PY" split_songs.py --help >/dev/null && ok "split_songs.py executa" \
    || { warn "split_songs.py não executou"; PROBLEMS=1; }
if [ ! -f .env ] && [ -f .env.exemple ]; then
    cp .env.exemple .env
    warn ".env criado a partir do .env.exemple: preencha as chaves do AcoustID/ACRCloud"
fi
# O .env guarda as chaves das APIs: só o dono pode ler.
for f in .env bin/.env; do
    if [ -f "$f" ]; then chmod 600 "$f" && ok "$f protegido (chmod 600)"; fi
done

info "Pronto"
cat <<EOF
   Ativar o ambiente:  source $VENV_DIR/bin/activate
   Rodar pelo fonte:   python split_songs.py --help
   Compilar:           python -m PyInstaller --onefile --name SplitSong split_songs.py
                       (gera dist/SplitSong para Linux; o .exe precisa ser compilado no Windows)
EOF
exit "$PROBLEMS"
