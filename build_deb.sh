#!/usr/bin/env bash
# Empacota o executável do SplitSong (dist/SplitSong) como .deb.
#   - rode antes o ./build.sh (ou ./build.sh --sem-copia)
#   - gera release/SplitSong-v<versão>-linux-<arquitetura>.deb
#
# Uso: ./build_deb.sh [PASTA_DE_SAIDA]   (padrão: release)
#
# O pacote instala:
#   /usr/lib/splitsong/SplitSong   o executável
#   /usr/bin/splitsong             atalho que usa ~/SplitSong como pasta de dados
#                                  (ou a do SPLITSONG_HOME); um --base-dir na
#                                  linha de comando continua valendo
#   /usr/share/splitsong/.env.exemple, /usr/share/doc/splitsong/README.md
#
# Variáveis opcionais: DEB_MAINTAINER ("Nome <email>") e DEB_HOMEPAGE.

set -euo pipefail
cd "$(dirname "$0")"

OUT_DIR="${1:-release}"
EXE="dist/SplitSong"
PKG="splitsong"

fail() { printf '\nErro: %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null || fail "dpkg-deb não encontrado (sistema Debian/Ubuntu)"
[ -x "$EXE" ] || fail "$EXE não encontrado; rode ./build.sh primeiro"

# A versão vem do próprio executável ("SplitSong 1.2.0").
VERSION=$("$EXE" --version | awk '{print $2}')
[ -n "$VERSION" ] || fail "não foi possível ler a versão de $EXE"
# 1.2.0-rc1 → 1.2.0~rc1: no dpkg o "~" ordena antes, então o 1.2.0 final
# é visto como mais novo (e o "-" seria lido como revisão do Debian).
DEB_VERSION=$(printf '%s' "$VERSION" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+)[-.]?([a-z].*)$/\1~\2/')
ARCH=$(dpkg --print-architecture)
MAINTAINER="${DEB_MAINTAINER:-SplitSong <splitsong@localhost>}"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/$PKG"

install -Dm755 "$EXE" "$ROOT/usr/lib/$PKG/SplitSong"
install -Dm644 .env.exemple "$ROOT/usr/share/$PKG/.env.exemple"
install -Dm644 README.md "$ROOT/usr/share/doc/$PKG/README.md"

install -Dm755 /dev/stdin "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
# Atalho do SplitSong instalado pelo .deb. O executável guarda o .env e as
# pastas Input, Temp e Output na pasta de dados, que aqui é ~/SplitSong
# (ou a do SPLITSONG_HOME). Um --base-dir passado na linha de comando vem
# depois deste e por isso tem prioridade.
set -e
DATA="${SPLITSONG_HOME:-$HOME/SplitSong}"
mkdir -p "$DATA/Input" "$DATA/Temp" "$DATA/Output"
if [ ! -e "$DATA/.env" ]; then
    # .env com as chaves das APIs: só o dono lê.
    (umask 077 && cp /usr/share/splitsong/.env.exemple "$DATA/.env")
    echo "SplitSong: criado $DATA/.env; preencha as chaves das APIs para o --rename" >&2
fi
exec /usr/lib/splitsong/SplitSong --base-dir "$DATA" "$@"
EOF

mkdir -p "$ROOT/DEBIAN"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $DEB_VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Depends: libc6 (>= 2.35), zlib1g, ffmpeg
Recommends: yt-dlp
Section: sound
Priority: optional
${DEB_HOMEPAGE:+Homepage: $DEB_HOMEPAGE
}Description: separa um MP3 longo em uma faixa por música
 Detecta as músicas de um álbum, coletânea ou show ao vivo pelos silêncios
 (ou por tempos informados), exporta um MP3 por faixa sem recodificar,
 identifica artista e título com o AcoustID e o ACRCloud, baixa áudio do
 YouTube com o yt-dlp e monta uma seleção embaralhada para pen drive.
 .
 Use o comando splitsong; os dados ficam em ~/SplitSong.
EOF

(cd "$ROOT" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)

mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/SplitSong-v$VERSION-linux-$ARCH.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "Gerado: $DEB"
dpkg-deb --info "$DEB" | sed -n '/Package:/,/Description:/p'
