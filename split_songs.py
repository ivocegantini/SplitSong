"""Detecta as músicas de um MP3 longo separadas por silêncio e registra cada faixa.

Requer apenas Python 3 e o ffmpeg/ffprobe no PATH.

Uso:
    python split_songs.py                 # detecta as faixas de todos os .mp3 da pasta Input
    python split_songs.py arquivo.mp3     # detecta as faixas de arquivos específicos
                                          # (gera o musicas.txt; o mesmo que --detect)
    python split_songs.py --split         # exporta um .mp3 por faixa conforme o musicas.txt
                                          # (edite o txt antes para ajustar cortes e nomes)
    python split_songs.py --detect --split  # detecta e já exporta
    python split_songs.py --rename        # identifica e renomeia as faixas já exportadas
                                          # (não refaz o split)
    python split_songs.py --rename --repeat 3  # analisa 3 trechos por serviço (padrão: 4)
    python split_songs.py --download URL  # baixa o áudio do YouTube como MP3 na pasta Input
                                          # (se for uma playlist, baixa todos os vídeos)
    python split_songs.py --download URL --detect --split  # baixa, detecta e exporta
                                                            # só os arquivos baixados
    python split_songs.py --download URL --cookies-from-browser firefox
                                          # usa os cookies do navegador (quando o YouTube
                                          # pede para confirmar que não é um robô)
    python split_songs.py --manual       # corta cada MP3 da pasta Input nos tempos do .txt
                                         # de mesmo nome (álbum sem pausas, show ao vivo)
    python split_songs.py --manual show.mp3 --times cortes.txt  # usa outro arquivo de tempos
    python split_songs.py --manual --rename  # corta e já identifica as faixas
    python split_songs.py --base-dir bin --split  # usa bin/.env, bin/Input, bin/Temp e bin/Output
    python split_songs.py --input ~/Albuns --temp /tmp/split --output ~/Musicas
                                         # usa outras pastas de entrada, intermediária e saída
    python split_songs.py --copy PASTA   # copia os MP3 da pasta Output (e subpastas) para
                                         # PASTA em ordem aleatória e numerada
    python split_songs.py --version      # mostra a versão do programa

O --download precisa do yt-dlp (winget install yt-dlp.yt-dlp ou pip install yt-dlp).

Configuração (.env na pasta do script/executável ou do --base-dir, ou variáveis de ambiente equivalentes,
ex.: acoustid-key no .env ou ACOUSTID_KEY no ambiente):
    acoustid-key            chave do AcoustID (https://acoustid.org/new-application)
    acrcloud-host           host do projeto ACRCloud (ex.: identify-eu-west-1.acrcloud.com)
    acrcloud-access-key     access key do projeto ACRCloud
    acrcloud-access-secret  access secret do projeto ACRCloud
    acrcloud-interval       segundos entre envios ao ACRCloud (padrão: 1.5)
    acrcloud-daily-limit    máximo de envios ao ACRCloud por dia (padrão: 100)
    cookies-from-browser    navegador de onde o yt-dlp lê os cookies do YouTube
                            (ex.: firefox, chrome, edge; padrão do --cookies-from-browser)
    input-dir               pasta dos MP3 originais (padrão: Input; o mesmo que --input)
    temp-dir                pasta do fluxo intermediário (padrão: Temp; o mesmo que --temp)
    output-dir              pasta das faixas finais (padrão: Output; o mesmo que --output);
                            o --split/--manual e o --rename copiam as faixas para
                            Output/<nome> (com --rename, só depois de renomear)
                            Caminhos relativos no .env partem da pasta do .env.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, namedtuple
from datetime import date
from pathlib import Path

__version__ = "1.0.0"

# Pasta de dados: tudo o que o programa lê ou gera (.env, Input, Temp, Output,
# .acrcloud_uso.json). Por padrão é a pasta do script (ou do executável, no
# PyInstaller); o --base-dir troca por outra (ex.: --base-dir bin).
BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
# Pastas padrão; podem ser trocadas por --input/--output/--temp ou pelo .env
# (input-dir, output-dir, temp-dir).
INPUT_DIR = BASE_DIR / "Input"    # MP3 originais (álbuns, shows, downloads)
TEMP_DIR = BASE_DIR / "Temp"      # fluxo intermediário: musicas.txt e faixas exportadas
OUTPUT_DIR = BASE_DIR / "Output"  # faixas finais, origem do --copy
ENV_FILE = BASE_DIR / ".env"
ACR_USAGE_FILE = BASE_DIR / ".acrcloud_uso.json"

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording"
USER_AGENT = f"SplitSong/{__version__} (script pessoal)"
MIN_SCORE = 0.5            # abaixo disso o nome é marcado para conferir
LOW_SCORE = 0.3            # abaixo disso a resposta é descartada
ACOUSTID_STEP = 5.0        # deslocamento (s) entre os trechos enviados ao AcoustID
ACR_SAMPLE_SECONDS = 12.0  # duração do trecho enviado ao ACRCloud

SILENCE_RE = re.compile(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)")
UNNAMED_RE = re.compile(r"(\d+) - Faixa \d+\.mp3$")
VERSION_RE = re.compile(r"\s+-\s+.*\b(remaster\w*|live|version|mix|edit|mono|stereo)\b.*$",
                        re.IGNORECASE)

# Nome escolhido para uma faixa; `review` indica que vale conferir à mão.
Name = namedtuple("Name", "artist title source review", defaults=("", False))
# Resposta de um serviço de identificação para um trecho.
Match = namedtuple("Match", "artist title score source")


class AcrLimitError(Exception):
    """O limite de envios ao ACRCloud foi atingido."""


def set_base_dir(path):
    """Troca a pasta de dados (--base-dir): o .env, o .acrcloud_uso.json e as
    pastas padrão Input, Temp e Output passam a ficar em `path`."""
    global BASE_DIR, INPUT_DIR, TEMP_DIR, OUTPUT_DIR, ENV_FILE, ACR_USAGE_FILE
    BASE_DIR = Path(path).expanduser().resolve()
    INPUT_DIR = BASE_DIR / "Input"
    TEMP_DIR = BASE_DIR / "Temp"
    OUTPUT_DIR = BASE_DIR / "Output"
    ENV_FILE = BASE_DIR / ".env"
    ACR_USAGE_FILE = BASE_DIR / ".acrcloud_uso.json"


def load_env(path=None):
    """Lê o .env (CHAVE=valor) normalizando as chaves para minúsculas com hífen."""
    path = path or ENV_FILE
    config = {}
    if path.exists():
        warn_if_exposed(path)
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip().lower().replace("_", "-")] = value.strip().strip("\"'")
    return config


def warn_if_exposed(path):
    """Avisa quando o .env (que guarda as chaves das APIs) pode ser lido por
    outros usuários da máquina (Linux/macOS)."""
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        print(f"aviso: {path} pode ser lido por outros usuários; "
              f"proteja as chaves com: chmod 600 \"{path}\"", file=sys.stderr)


def config_dir(config, name, default):
    """Pasta configurada no .env/ambiente; caminhos relativos partem de BASE_DIR."""
    value = setting(config, name)
    return (BASE_DIR / Path(value).expanduser()).resolve() if value else default


def setting(config, name, default=None):
    """A variável de ambiente (ex.: ACOUSTID_KEY) tem prioridade sobre o .env."""
    return os.environ.get(name.upper().replace("-", "_")) or config.get(name) or default


def which_trusted(name):
    """Como o shutil.which, mas só procura nas pastas absolutas do PATH.

    No Windows o shutil.which e o CreateProcess procuram primeiro na pasta
    atual (e o CreateProcess também na pasta do executável). Um ffmpeg.exe
    malicioso deixado ali (ex.: junto com MP3 baixados) seria executado.
    Aqui as entradas relativas do PATH ('.', '') são ignoradas e, no Windows,
    só arquivos .exe são aceitos (.bat/.cmd têm regras próprias de escape)."""
    exe = name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        candidate = Path(folder) / exe
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_program(name, package=None):
    """Procura o executável no PATH ou na pasta do winget (o winget só
    atualiza o PATH de shells abertos depois da instalação). Retorna o
    caminho absoluto ou None."""
    found = which_trusted(name)
    if found or os.name != "nt" or not package:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if not os.path.isabs(local):
        return None
    winget = Path(local) / "Microsoft/WinGet"
    installed = [*winget.glob(f"Links/{name}.exe"), *winget.glob(f"Packages/{package}_*/{name}.exe")]
    return str(installed[0]) if installed else None


_TOOLS = {}


def tool(name):
    """Caminho absoluto do ffmpeg/ffprobe (procurado uma vez); encerra se faltar."""
    if name not in _TOOLS:
        path = find_program(name, "Gyan.FFmpeg")
        if not path:
            sys.exit(f"{name} não encontrado no PATH (instale o ffmpeg; veja o setup)")
        _TOOLS[name] = path
    return _TOOLS[name]


# Caracteres de controle e de direção do texto (ex.: U+202E, que inverte a
# exibição e permite disfarçar "mp3.exe" como "exe.3pm"). Vêm de dados
# externos (MusicBrainz é editado por qualquer pessoa) e não podem chegar ao
# terminal nem aos nomes de arquivo.
_BIDI = set("‎‏‪‫‬‭‮⁦⁧⁨⁩")


def clean_text(text):
    """Remove caracteres de controle (ANSI, quebras de linha, NUL) e de
    direção do texto de um valor vindo de fora."""
    text = "".join(" " if c in "\t\r\n" else c for c in str(text or ""))
    text = "".join(c for c in text
                   if unicodedata.category(c) != "Cc" and c not in _BIDI)
    return re.sub(r"\s+", " ", text).strip()


def find_ytdlp():
    """Retorna o comando do yt-dlp (executável ou módulo Python) ou None."""
    exe = find_program("yt-dlp", "yt-dlp.yt-dlp")
    if exe:
        return [exe]
    if getattr(sys, "frozen", False):
        return None  # no executável não há interpretador para rodar `-m yt_dlp`
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def download_mp3(url, ytdlp, browser=None):
    """Baixa o melhor áudio do vídeo (ou de todos os vídeos, se o link for de
    uma playlist) e converte para MP3 na pasta Input. Retorna a lista dos
    arquivos gerados; vídeos que falharem são pulados."""
    print(f"\n== baixando {url}")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_program("ffmpeg", "Gyan.FFmpeg")
    deno = find_program("deno", "DenoLand.Deno")
    with tempfile.TemporaryDirectory() as tmp:
        # O yt-dlp grava o caminho final de cada arquivo (após a conversão)
        # neste arquivo, uma linha por vídeo.
        path_file = Path(tmp) / "caminho.txt"
        proc = subprocess.run([
            *ytdlp, "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
            "--yes-playlist", "--windows-filenames",
            *(["--ffmpeg-location", ffmpeg] if ffmpeg else []),
            *(["--js-runtimes", f"deno:{deno}"] if deno else []),
            *(["--cookies-from-browser", browser] if browser else []),
            "--print-to-file", "after_move:filepath", str(path_file),
            "-o", str(INPUT_DIR / "%(title)s.%(ext)s"),
            "--", url,  # "--": a URL nunca é lida como opção do yt-dlp (ex.: --exec)
        ])
        lines = (path_file.read_text(encoding="utf-8").splitlines()
                 if path_file.exists() else [])
    if not lines:
        print("   falha no download")
        return []
    if proc.returncode:
        print("   alguns vídeos falharam e foram pulados")
    paths = []
    for line in dict.fromkeys(lines):
        path = Path(line)
        if not path.exists():
            continue
        clean = path.with_name(clean_filename(path.stem) + path.suffix)
        if clean != path:
            path = path.replace(unique_path(clean))  # não sobrescreve um MP3 que já existe
        print(f"   salvo em {path} ({fmt_time(get_duration(path))})")
        paths.append(path)
    return paths


def clean_filename(name):
    """Remove emojis e símbolos gráficos do nome (ex.: '🔥', '♫') e espaços repetidos."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(c for c in name
                   if unicodedata.category(c) not in ("So", "Cf", "Cs", "Cc") and c != "\ufe0f")
    return re.sub(r"\s+", " ", name).strip(" .-_|") or "audio"


def unique_path(path):
    """`path` se ainda n\u00e3o existe; sen\u00e3o "nome (2).ext", "nome (3).ext"..."""
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def fmt_time(seconds):
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_duration(path):
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_silences(path, noise_db, min_silence):
    """Retorna uma lista de (inicio, fim, duracao) de cada trecho silencioso."""
    proc = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    silences = []
    for match in SILENCE_RE.finditer(proc.stderr):
        end, dur = float(match.group(1)), float(match.group(2))
        silences.append((end - dur, end, dur))
    return silences


def choose_cuts(silences, total, min_song):
    """Escolhe os pontos de corte: silêncios mais longos primeiro, garantindo
    que nenhuma música fique menor que `min_song` segundos. Isso descarta as
    pausas curtas que acontecem dentro de uma mesma música."""
    cuts = []
    for start, end, _ in sorted(silences, key=lambda s: s[2], reverse=True):
        mid = (start + end) / 2
        if mid < min_song or total - mid < min_song:
            continue
        if all(abs(mid - c[0]) >= min_song for c in cuts):
            cuts.append((mid, start, end))
    return sorted(cuts)


def build_tracks(cuts, total, margin):
    """Converte os cortes em faixas (inicio, fim), removendo o silêncio das bordas.
    Mantém `margin` segundos do silêncio em cada borda (sem passar do meio dele)
    para não cortar intros e fade-outs suaves."""
    tracks = []
    start = 0.0
    for mid, sil_start, sil_end in cuts:
        tracks.append((start, min(mid, sil_start + margin)))
        start = max(mid, sil_end - margin)
    tracks.append((start, total))
    return tracks


def fingerprint(src, start, length):
    """Gera a impressão digital Chromaprint de um trecho do arquivo."""
    proc = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
         "-f", "chromaprint", "-fp_format", "base64", "-"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def extract_sample(src, start, length):
    """Extrai um trecho curto em MP3 mono para enviar ao ACRCloud."""
    proc = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
         "-ac", "1", "-ar", "44100", "-b:a", "96k", "-f", "mp3", "-"],
        capture_output=True, check=True,
    )
    return proc.stdout


def join_artists(credits):
    """Junta os artistas no formato do AcoustID/MusicBrainz (nome + joinphrase)."""
    return "".join(a["name"] + a.get("joinphrase", "") for a in credits)


def musicbrainz_recording(rec_id, cache):
    """Busca (artista, titulo) de uma gravação no MusicBrainz, com cache."""
    if rec_id not in cache:
        req = urllib.request.Request(f"{MUSICBRAINZ_URL}/{rec_id}?inc=artists&fmt=json",
                                     headers={"User-Agent": USER_AGENT})
        time.sleep(1.1)  # o MusicBrainz aceita no máximo 1 requisição por segundo
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except Exception:
            data = {}
        credits = data.get("artist-credit")
        cache[rec_id] = ((join_artists(credits), data["title"])
                         if credits and data.get("title") else None)
    return cache[rec_id]


def acoustid_lookup(src, start, length, duration, api_key, mb_cache):
    """Consulta o AcoustID com a impressão digital de um trecho e retorna o
    melhor Match ou None. `duration` é sempre a duração total da faixa."""
    params = urllib.parse.urlencode({
        "client": api_key,
        "meta": "recordings",
        "duration": int(duration),
        "fingerprint": fingerprint(src, start, length),
    })
    time.sleep(0.4)  # o AcoustID aceita no máximo 3 requisições por segundo
    with urllib.request.urlopen(ACOUSTID_URL, data=params.encode(), timeout=30) as resp:
        data = json.load(resp)
    if data.get("status") != "ok":
        raise RuntimeError(data.get("error", {}).get("message", "erro no AcoustID"))

    best, orphans = None, []
    for result in data.get("results", []):
        score = result.get("score", 0)
        if score < LOW_SCORE:
            continue
        for rec in result.get("recordings", []):
            if not (rec.get("title") and rec.get("artists")):
                # gravação sem metadados no AcoustID: dá para buscar no MusicBrainz
                if rec.get("id"):
                    orphans.append((score, rec["id"]))
                continue
            # maior pontuação primeiro; desempate pela duração mais próxima
            key = (-score, abs(rec.get("duration", 0) - duration))
            if best is None or key < best[0]:
                best = (key, Match(join_artists(rec["artists"]), rec["title"],
                                   score, "acoustid"))
    if best:
        return best[1]
    for score, rec_id in sorted(orphans, reverse=True)[:3]:
        found = musicbrainz_recording(rec_id, mb_cache)
        if found:
            return Match(*found, score, "acoustid")
    return None


class AcrCloud:
    """Cliente do ACRCloud que respeita o intervalo entre envios, o limite
    diário configurado e os avisos de limite da própria API."""

    def __init__(self, host, access_key, access_secret, interval, daily_limit):
        self.host = re.sub(r"^https?://", "", host).strip("/")
        self.access_key = access_key
        self.access_secret = access_secret
        self.interval = interval
        self.daily_limit = daily_limit
        self.last_request = 0.0
        self.disabled = None  # motivo da interrupção, quando houver

    @property
    def available(self):
        return self.disabled is None

    def _usage(self):
        try:
            data = json.loads(ACR_USAGE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data.get("envios", 0) if data.get("data") == date.today().isoformat() else 0

    def _count_request(self):
        ACR_USAGE_FILE.write_text(json.dumps({"data": date.today().isoformat(),
                                              "envios": self._usage() + 1}),
                                  encoding="utf-8")

    def _post(self, sample):
        timestamp = str(int(time.time()))
        to_sign = "\n".join(["POST", "/v1/identify", self.access_key, "audio", "1", timestamp])
        signature = base64.b64encode(hmac.new(self.access_secret.encode(), to_sign.encode(),
                                              hashlib.sha1).digest()).decode()
        fields = {"access_key": self.access_key, "sample_bytes": str(len(sample)),
                  "timestamp": timestamp, "signature": signature,
                  "data_type": "audio", "signature_version": "1"}
        boundary = uuid.uuid4().hex
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                        f"{v}\r\n".encode() for k, v in fields.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="sample"; '
                 f'filename="sample.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n').encode()
        body += sample + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://{self.host}/v1/identify", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def identify(self, src, start, length, duration):
        """Envia um trecho e retorna o melhor Match ou None."""
        if self.disabled:
            raise AcrLimitError(self.disabled)
        sample = extract_sample(src, start, length)
        for attempt in range(2):
            if self._usage() >= self.daily_limit:
                self.disabled = f"limite diário de {self.daily_limit} envios atingido"
                raise AcrLimitError(self.disabled)
            wait = self.interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self._count_request()
            data = self._post(sample)
            status = data.get("status", {})
            code = status.get("code")
            if code == 3015 and attempt == 0:  # muitas requisições: espera e tenta de novo
                time.sleep(max(2.0, self.interval * 2))
                continue
            break
        if code == 1001:  # nenhuma música reconhecida
            return None
        if code == 3003:  # limite da conta
            self.disabled = "limite de envios da conta ACRCloud atingido"
            raise AcrLimitError(self.disabled)
        if code != 0:
            raise RuntimeError(status.get("msg", f"erro {code} no ACRCloud"))

        best = None
        for music in data.get("metadata", {}).get("music", []):
            if not (music.get("title") and music.get("artists")):
                continue
            score = music.get("score", 0) / 100
            if score < LOW_SCORE:
                continue
            key = (-score, abs(music.get("duration_ms", 0) / 1000 - duration))
            if best is None or key < best[0]:
                artist = ", ".join(a["name"] for a in music["artists"])
                best = (key, Match(artist, music["title"], score, "acrcloud"))
        return best[1] if best else None


def vote_key(match):
    """Chave de votação: ignora maiúsculas e sufixos como '(Remastered 2011)'."""
    title = re.sub(r"\s*[(\[].*?[)\]]", "", match.title)
    title = VERSION_RE.sub("", title)
    return (match.artist.casefold().strip(), title.casefold().strip())


def poll(positions, query, errors):
    """Consulta cada trecho e para assim que nenhum outro nome consegue mais
    alcançar o mais votado, economizando envios."""
    matches = []
    for i, pos in enumerate(positions):
        try:
            match = query(pos)
        except AcrLimitError as exc:
            errors.append(exc)
            break
        except Exception as exc:
            errors.append(exc)
            continue
        if match:
            matches.append(match)
        counts = sorted(_group(matches).values(), key=len, reverse=True)
        runner_up = len(counts[1]) if len(counts) > 1 else 0
        if counts and len(counts[0]) > runner_up + len(positions) - i - 1:
            break
    return matches


def _group(matches):
    groups = defaultdict(list)
    for match in matches:
        groups[vote_key(match)].append(match)
    return groups


def elect(matches):
    """Escolhe o nome que mais apareceu (desempate pela maior pontuação).
    Retorna (Name, votos) ou None."""
    if not matches:
        return None
    ranked = sorted(_group(matches).values(),
                    key=lambda g: (len(g), max(m.score for m in g)), reverse=True)
    winner = ranked[0]
    best = max(winner, key=lambda m: m.score)
    tie = len(ranked) > 1 and len(ranked[1]) == len(winner)
    # artista e título vêm de serviços externos: sem caracteres de controle
    return (Name(clean_text(best.artist), clean_text(best.title), best.source,
                 best.score < MIN_SCORE or tie), len(winner))


def acoustid_offsets(pieces):
    """0, -5, +5, -10, +10...: recua primeiro, pois o corte pode ter comido o início."""
    offsets = [0.0]
    step = 1
    while len(offsets) < pieces:
        offsets.append(-ACOUSTID_STEP * step)
        if len(offsets) < pieces:
            offsets.append(ACOUSTID_STEP * step)
        step += 1
    return offsets


def identify_track(src, start, end, pieces, api_key, acr, mb_cache):
    """Identifica a faixa [start, end] de `src`. Tenta o AcoustID em `pieces`
    trechos deslocados em torno do início; se não houver resposta confiável,
    consulta o ACRCloud em `pieces` trechos espalhados pela música. Retorna
    (Name ou None, votos, respostas, erros)."""
    duration = end - start
    matches, errors = [], []

    if api_key:
        positions = list(dict.fromkeys(
            round(min(max(0.0, start + off), max(start, end - 30)), 3)
            for off in acoustid_offsets(pieces)))
        matches += poll(positions, lambda p: acoustid_lookup(
            src, p, min(120.0, end - p), duration, api_key, mb_cache), errors)

    result = elect(matches)
    if (result is None or result[0].review) and acr and acr.available:
        length = min(ACR_SAMPLE_SECONDS, duration)
        positions = [min(max(start, start + duration * (i + 1) / (pieces + 1) - length / 2),
                         end - length)
                     for i in range(pieces)]
        matches += poll(positions, lambda p: acr.identify(src, p, length, duration), errors)
        result = elect(matches)

    name, votes = result or (None, 0)
    return name, votes, len(matches), errors


MAX_NAME = 180  # caracteres; o limite comum dos sistemas de arquivos é 255 bytes


def safe_filename(name):
    """Nome de arquivo sem separadores de pasta, caracteres proibidos no
    Windows, caracteres de controle/direção e com tamanho limitado."""
    name = re.sub(r'[<>:"/\\|?*]', "", clean_text(name))
    return name[:MAX_NAME].strip(" .")


def export_track(src, dest, start, end, number, artist=None, title=None):
    meta = ["-metadata", f"track={number}"]
    if artist:
        meta += ["-metadata", f"artist={artist}", "-metadata", f"title={title}"]
    subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
         "-c", "copy", "-map_metadata", "-1", *meta, str(dest)],
        check=True,
    )


LOG_FILE = "musicas.txt"  # registro das faixas em Temp/<nome>, editável à mão
UNNAMED = "(não identificada)"
LOG_HELP = ("# Edite e rode o --split: mude os tempos (início - fim), apague uma linha para\n"
            "# juntar faixas (aumentando o fim da anterior) ou troque o nome por\n"
            "# \"Artista - Título\". A duração entre parênteses é só informativa.\n")
# "01  00:00:00.00 - 00:03:12.50  (00:03:12)  Artista - Título [acoustid, conferir]"
LOG_LINE_RE = re.compile(r"(?:\d+\s+)?([\d:;.,]+)\s*-\s*([\d:;.,]+)"
                         r"(?:\s+\([\d:]*\))?(?:\s+(.*))?$")
# marcadores no fim do nome: a fonte do nome e se vale conferir
LOG_TAGS_RE = re.compile(r"\s*\[((?:acoustid|acrcloud|conferir)"
                         r"(?:\s*,\s*(?:acoustid|acrcloud|conferir))*)\]$", re.IGNORECASE)


def fmt_time_precise(seconds):
    """Como o fmt_time, mas com centésimos (00:03:12.50), para o registro."""
    cs = round(max(0.0, seconds) * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    return f"{h:02d}:{m:02d}:{rem // 100:02d}.{rem % 100:02d}"


def log_label(name):
    """Nome da faixa como aparece no registro."""
    if not name:
        return UNNAMED
    tags = [t for t in (name.source, "conferir" if name.review else "") if t]
    return f"{name.artist} - {name.title}" + (f" [{', '.join(tags)}]" if tags else "")


def parse_label(text):
    """Desfaz o log_label: retorna Name ou None (faixa sem nome)."""
    text = clean_text(text)
    tags = []
    match = LOG_TAGS_RE.search(text)
    if match:
        tags = [t.strip().lower() for t in match.group(1).split(",")]
        text = text[:match.start()].strip()
    if not text or text == UNNAMED:
        return None
    artist, sep, title = text.partition(" - ")
    if not (sep and artist.strip() and title.strip()):
        raise ValueError(f"nome {text!r} fora do formato \"Artista - Título\"")
    source = next((t for t in tags if t != "conferir"), "")
    return Name(artist.strip(), title.strip(), source, "conferir" in tags)


def write_log(out_dir, src_name, tracks, names):
    """Grava o musicas.txt e retorna as linhas das faixas. A primeira linha do
    arquivo é o nome do MP3 de origem; depois vem uma linha por faixa."""
    lines = [f"{i:02d}  {fmt_time_precise(start)} - {fmt_time_precise(end)}  "
             f"({fmt_time(end - start)})  {log_label(name)}"
             for i, ((start, end), name) in enumerate(zip(tracks, names), 1)]
    with open(out_dir / LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"{src_name}\n{LOG_HELP}")
        f.writelines(line + "\n" for line in lines)
    return lines


def process(path, args):
    print(f"\n== {path.name}")
    total = get_duration(path)
    silences = detect_silences(path, args.noise, args.min_silence)
    cuts = choose_cuts(silences, total, args.min_song)
    tracks = build_tracks(cuts, total, args.margin)
    print(f"   duração {fmt_time(total)} | {len(silences)} silêncios | {len(tracks)} músicas")

    out_dir = TEMP_DIR / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    for line in write_log(out_dir, path.name, tracks, [None] * len(tracks)):
        print("   " + line)
    print(f"   Registro salvo em {out_dir}")


def read_log(out_dir):
    """Lê o musicas.txt e retorna (nome do MP3 de origem, faixas, nomes), ou
    None se ele não existir. Linhas vazias e começando com '#' são ignoradas;
    uma linha fora do formato vira ValueError com o número da linha."""
    log = out_dir / LOG_FILE
    if not log.exists():
        return None
    lines = log.read_text(encoding="utf-8-sig").splitlines()
    src_name = source_name(lines[0] if lines else "") or f"{out_dir.name}.mp3"
    tracks, names = [], []
    for number, line in enumerate(lines[1:], 2):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            match = LOG_LINE_RE.fullmatch(line)
            if not match:
                raise ValueError("use: NN  início - fim  (duração)  Artista - Título")
            start, end = parse_time(match.group(1)), parse_time(match.group(2))
            if end <= start:
                raise ValueError(f"o fim ({fmt_time(end)}) não é maior que o início "
                                 f"({fmt_time(start)})")
            name = parse_label(match.group(3) or "")
        except ValueError as exc:
            raise ValueError(f"{LOG_FILE}, linha {number}: {str(exc) or 'tempo inválido'}") from None
        tracks.append((start, end))
        names.append(name)
    return src_name, tracks, names


def source_name(text):
    """Nome do MP3 de origem lido do musicas.txt, reduzido ao nome do arquivo.
    O arquivo pode ter sido editado ou vir de outra pessoa: um caminho
    absoluto ou com '..' faria o programa ler um arquivo fora da pasta Input.
    Retorna "" se o nome não for válido."""
    name = clean_text(text)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # tira pastas (de qualquer sistema)
    if ":" in name and os.name == "nt":
        name = name.rsplit(":", 1)[-1]  # "C:arquivo.mp3"
    return "" if name in ("", ".", "..") else name


def input_file(name):
    """INPUT_DIR / name, ou None se o nome sair de INPUT_DIR."""
    path = INPUT_DIR / name
    return path if name and path.parent == INPUT_DIR and name not in (".", "..") else None


def source_file(src_name, given=None):
    """MP3 de origem de um registro. `given` é o arquivo passado na linha de
    comando (confiável, pode estar em qualquer pasta). Sem ele, só se aceita o
    nome gravado no musicas.txt dentro da pasta Input, pois o txt pode ter sido
    editado. Retorna o caminho (que pode não existir) ou None."""
    return given or input_file(src_name)


def missing_source(src):
    print(f"   arquivo de origem não encontrado: {src or '(nome inválido)'}\n"
          f"   coloque o MP3 em {INPUT_DIR} ou passe o arquivo na linha de comando")


def split_tracks(out_dir, src=None):
    """Exporta um MP3 por faixa conforme o musicas.txt, sem refazer a detecção.
    Assim, ajustes feitos à mão no txt (cortes, junções, nomes) são respeitados.
    `src` é o MP3 de origem passado na linha de comando (padrão: o nome gravado
    no txt, procurado na pasta Input). Retorna True se exportou as faixas."""
    print(f"\n== {out_dir.name}")
    try:
        log = read_log(out_dir)
    except ValueError as exc:
        print(f"   erro no {exc}, pulando")
        return False
    if not log:
        print(f"   sem {LOG_FILE}, pulando (rode o --detect antes)")
        return False
    src_name, tracks, names = log
    src = source_file(src_name, src)
    if not src or not src.exists():
        missing_source(src)
        return False

    write_log(out_dir, src_name, tracks, names)  # regrava o txt renumerado e alinhado
    export_tracks(src, out_dir, tracks, names)
    return True


TRACK_RE = re.compile(r"\d+ - .+\.mp3$")  # nome das faixas que o programa exporta
MANIFEST = ".splitsong.json"              # lista das faixas copiadas para Output/<nome>


def publish_tracks(out_dir):
    """Copia os MP3 de Temp/<nome> para Output/<nome>, trocando os MP3 que o
    próprio programa copiou numa execução anterior pelos atuais.

    Só são apagados os arquivos listados no .splitsong.json da pasta. Uma pasta
    sem essa lista (de uma versão anterior) só é limpa se tiver apenas faixas
    no formato "NN - ....mp3"; caso contrário nada é apagado, pois a pasta
    pode ser de outra coleção (ex.: --output apontando para a biblioteca de
    músicas)."""
    tracks = sorted(out_dir.glob("*.mp3"))
    if not tracks:
        return
    dest = OUTPUT_DIR / out_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    manifest = dest / MANIFEST
    try:
        old_names = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = [p.name for p in dest.glob("*.mp3")]
        old_names = existing if all(TRACK_RE.match(n) for n in existing) else []
        if len(old_names) < len(existing):
            print(f"   {dest} já tem MP3 que não são deste programa; nada foi apagado")
    for name in old_names:
        old = dest / Path(str(name)).name
        if old.suffix.lower() == ".mp3" and old.is_file():
            old.unlink()
    for mp3 in tracks:
        shutil.copy2(mp3, dest / mp3.name)
    manifest.write_text(json.dumps([mp3.name for mp3 in tracks], ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"   {len(tracks)} arquivos copiados para {dest}")


def export_tracks(src, out_dir, tracks, names):
    """Apaga as faixas antigas de `out_dir` (só os MP3 no formato "NN - ....mp3"
    que o programa gera) e exporta um MP3 por faixa. Faixas sem nome viram
    "NN - Faixa NN.mp3", que o --rename sabe identificar depois."""
    for old in out_dir.glob("*.mp3"):
        if TRACK_RE.match(old.name):
            old.unlink()
    for i, ((start, end), name) in enumerate(zip(tracks, names), 1):
        base = f"{i:02d} - {name.artist} - {name.title}" if name else f"{i:02d} - Faixa {i:02d}"
        export_track(src, out_dir / f"{safe_filename(base)}.mp3", start, end, i,
                     *(name[:2] if name else (None, None)))
    print(f"   {len(tracks)} arquivos exportados")


def parse_time(text):
    """Converte "horas;minutos;segundos" (ou "minutos;segundos" ou "segundos";
    também aceita ":" no lugar de ";") em segundos. Os segundos podem ter
    fração (ex.: 0;03;45.5)."""
    parts = re.split(r"\s*[;:]\s*", text.strip())
    if not 1 <= len(parts) <= 3:
        raise ValueError
    values = [float(p.replace(",", ".")) for p in parts]
    if any(v < 0 for v in values) or any(v >= 60 for v in values[1:]):
        raise ValueError
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


def read_times(path):
    """Lê o arquivo de tempos de corte: um tempo por linha no formato
    horas;minutos;segundos. Linhas vazias e o que vem depois de '#' são
    ignorados. Retorna [(numero_da_linha, segundos)]; erros viram ValueError."""
    times = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            times.append((number, parse_time(line)))
        except ValueError:
            raise ValueError(f"linha {number}: tempo inválido {line!r} "
                             "(use horas;minutos;segundos, ex.: 0;03;45)") from None
    return times


def manual_split(path, times_file):
    """Corta o MP3 nos tempos do arquivo texto, para álbuns sem pausa entre as
    músicas ou shows ao vivo. Cada tempo fecha uma faixa que começa no corte
    anterior (a primeira começa no 0): o 1º corte gera a Faixa 01, o 2º a
    Faixa 02 e assim por diante. O que sobra depois do último corte vira mais
    uma faixa. Grava o musicas.txt e exporta os MP3 em Temp/<nome>.
    Retorna a pasta em Temp, ou None se não exportou."""
    print(f"\n== {path.name} (cortes de {times_file.name})")
    if not path.exists():
        print(f"   arquivo não encontrado: {path}")
        return
    if not times_file.exists():
        print(f"   arquivo de tempos não encontrado: {times_file}, pulando")
        return
    total = get_duration(path)
    tracks, start = [], 0.0
    try:
        for number, cut in read_times(times_file):
            if cut <= start:
                raise ValueError(f"linha {number}: {fmt_time(cut)} não é maior que o "
                                 f"corte anterior ({fmt_time(start)})")
            if cut >= total:
                raise ValueError(f"linha {number}: {fmt_time(cut)} passa da duração "
                                 f"do arquivo ({fmt_time(total)})")
            tracks.append((start, cut))
            start = cut
        if not tracks:
            raise ValueError("nenhum tempo de corte no arquivo")
    except ValueError as exc:
        print(f"   erro em {times_file.name}, {exc}")
        return
    if total - start > 1.0:  # resto depois do último corte (ignora sobras < 1 s)
        tracks.append((start, total))
    print(f"   duração {fmt_time(total)} | {len(tracks)} faixas")

    out_dir = TEMP_DIR / path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    names = [None] * len(tracks)
    for line in write_log(out_dir, path.name, tracks, names):
        print("   " + line)
    export_tracks(path, out_dir, tracks, names)
    print(f"   Registro salvo em {out_dir}")
    return out_dir


def rename_tracks(out_dir, api_key, acr, pieces, mb_cache, src=None):
    """Identifica as faixas já exportadas que ainda não têm nome e as renomeia,
    sem refazer a detecção. O registro (musicas.txt) é atualizado com os nomes.
    Os trechos são tirados do MP3 original (quando existe), o que permite
    recuar antes do corte caso ele tenha comido o início da música. `src` é o
    MP3 original passado na linha de comando, como no split_tracks."""
    print(f"\n== {out_dir.name}")
    try:
        log = read_log(out_dir)
    except ValueError as exc:
        print(f"   erro no {exc}, pulando")
        return False
    if not log:
        print(f"   sem {LOG_FILE}, pulando (rode o --detect e o --split antes)")
        return False
    src_name, tracks, names = log
    original = source_file(src_name, src) or INPUT_DIR / "(nome inválido)"
    if not original.exists():
        print(f"   MP3 original não encontrado, usando as faixas exportadas: {original}")

    renamed = 0
    for mp3 in sorted(out_dir.glob("*.mp3")):
        match = UNNAMED_RE.match(mp3.name)
        if not match:
            continue  # já tem nome
        number = int(match.group(1))
        duration = get_duration(mp3)
        if original.exists() and number <= len(tracks):
            src, (start, end) = original, tracks[number - 1]
        else:
            src, start, end = mp3, 0.0, duration

        name, votes, answers, errors = identify_track(src, start, end, pieces,
                                                      api_key, acr, mb_cache)
        if not name:
            reason = (f"erro ao identificar ({clean_text(errors[-1])})" if errors and not answers
                      else "não identificada")
            print(f"   faixa {number:02d}: {reason}")
            continue

        dest = out_dir / f"{safe_filename(f'{number:02d} - {name.artist} - {name.title}')}.mp3"
        export_track(mp3, dest, 0.0, duration, number, name.artist, name.title)
        mp3.unlink()
        if number <= len(names):
            names[number - 1] = name
        renamed += 1
        print(f"   faixa {number:02d}: {name.artist} - {name.title} ("
              f"{name.source}, {votes} de {answers} resposta(s))"
              f"{' [conferir]' if name.review else ''}")

    write_log(out_dir, src_name, tracks, names)
    print(f"   {renamed} arquivos renomeados")
    return True


def artist_key(path):
    """Artista de um MP3 no formato "NN - Artista - Título.mp3", normalizado
    (minúsculas, sem o "The" inicial). Arquivos fora desse formato usam o nome
    inteiro, ficando cada um no seu próprio grupo."""
    stem = re.sub(r"^\d+\s*-\s*", "", Path(path).stem)
    artist = stem.partition(" - ")[0].replace("’", "'").casefold().strip()
    return re.sub(r"^the\s+", "", artist)


def copy_shuffled(dest):
    """Lista os MP3 da pasta Output (recursivamente), prefixa cada nome com o da
    pasta onde ele está, embaralha a lista evitando faixas da mesma banda em
    sequência, numera e copia os arquivos para `dest` um a um, na ordem da
    lista (aparelhos que tocam pela ordem de gravação seguem a mesma sequência).
    A lista é salva em dest/lista.txt."""
    dest = dest.resolve()
    files = [p for p in OUTPUT_DIR.rglob("*.mp3") if dest not in p.resolve().parents]
    if not files:
        sys.exit(f"Nenhum MP3 encontrado em {OUTPUT_DIR}")
    random.shuffle(files)

    # Remove músicas repetidas (mesmo artista e título, ignorando maiúsculas,
    # versões e o "The" inicial). Faixas sem nome ("Faixa NN") não entram na
    # comparação.
    items, seen = [], set()
    for p in files:
        if not UNNAMED_RE.match(p.name):
            artist, _, title = re.sub(r"^\d+\s*-\s*", "", p.stem).partition(" - ")
            key = vote_key(Match(artist.replace("’", "'"), title.replace("’", "'"), 0, ""))
            key = (re.sub(r"^the\s+", "", key[0]), key[1])
            if key in seen:
                continue
            seen.add(key)
        items.append((p, f"{p.parent.name} - {p.name}" if p.parent != OUTPUT_DIR else p.name))
    if len(items) < len(files):
        print(f"\n== {len(files) - len(items)} músicas repetidas ignoradas")

    # Embaralha evitando a mesma banda em sequência: agrupa por artista e, a
    # cada passo, tira uma faixa do maior grupo cujo artista é diferente do
    # último escolhido. O artista vem do nome do próprio arquivo (it[0]), não do
    # nome de destino (it[1]), que pode ter o nome da pasta na frente.
    groups = {}
    for it in items:
        groups.setdefault(artist_key(it[0]), []).append(it)

    # grupos maiores primeiro; cada grupo embaralhado internamente
    ordered_groups = sorted(groups.values(), key=len, reverse=True)
    for g in ordered_groups:
        random.shuffle(g)

    result = []
    last_key = None

    pool = ordered_groups

    while pool:
        # sempre tenta o grupo com mais faixas restantes
        pool.sort(key=len, reverse=True)

        # primeiro grupo cujo artista é diferente do último
        chosen = None
        for idx, g in enumerate(pool):
            key = artist_key(g[0][0])
            if key != last_key:
                chosen = idx
                break

        if chosen is None:
            # só sobrou o mesmo artista: não tem como evitar a repetição
            chosen = 0

        g = pool[chosen]
        item = g.pop(0)
        result.append(item)
        last_key = artist_key(item[0])

        # descarta os grupos que esvaziaram
        pool = [g for g in pool if g]

    items = result

    # numera com zeros à esquerda para que a ordem alfabética seja a da lista
    width = max(2, len(str(len(items))))
    dest.mkdir(parents=True, exist_ok=True)
    print(f"\n== copiando {len(items)} arquivos para {dest}")
    lines = []
    for i, (src, name) in enumerate(items, 1):
        target = f"{i:0{width}d} - {name}"
        try:
            shutil.copyfile(src, dest / target)
        except OSError as exc:
            sys.exit(f"\nFalha ao copiar {target}: {exc}\n"
                     f"{i - 1} arquivos foram copiados. Se o destino é a raiz de um "
                     "pen drive FAT, ela só aceita 512 entradas (cada nome longo ocupa "
                     "várias); use uma subpasta (ex.: D:\\Musicas) ou formate o pen "
                     "drive em FAT32.")
        lines.append(target)
        print(f"   {target}")
    (dest / "lista.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   {len(items)} arquivos copiados; lista salva em {dest / 'lista.txt'}")


WILDCARD_CHARS = "*?["


def check_files(files):
    """Encerra com uma mensagem clara se algum MP3 da linha de comando não
    existir. Um nome com caracteres de pesquisa (*, ?, [) que não existe quase
    sempre é um padrão que o shell não expandiu: entre aspas ('pasta/*.mp3')
    ou no cmd/PowerShell do Windows, que não expandem padrões."""
    errors = []
    for path in files:
        if path.expanduser().is_file():
            continue
        text = str(path)
        if any(c in text for c in WILDCARD_CHARS):
            errors.append(
                f"{text!r} tem caracteres de pesquisa de arquivos (* ? [ ]), mas o programa "
                "não expande padrões: ele recebeu o texto literal.\n"
                "   - no Linux/macOS, tire as aspas para o shell expandir: pasta/*.mp3\n"
                "   - no Windows (cmd/PowerShell), liste os arquivos um a um\n"
                "   - para processar todos os MP3 da pasta Input, não passe arquivos")
        elif path.expanduser().is_dir():
            errors.append(f"{text!r} é uma pasta; para processar os MP3 de uma pasta, "
                          "use --input PASTA (ou --base-dir)")
        else:
            errors.append(f"arquivo não encontrado: {text!r}")
    if errors:
        sys.exit("erro: " + "\nerro: ".join(errors))


def build_acrcloud(config):
    host = setting(config, "acrcloud-host")
    key = setting(config, "acrcloud-access-key")
    secret = setting(config, "acrcloud-access-secret")
    if not (host and key and secret):
        return None
    return AcrCloud(host, key, secret,
                    float(setting(config, "acrcloud-interval", 1.5)),
                    int(setting(config, "acrcloud-daily-limit", 100)))


def main():
    global INPUT_DIR, TEMP_DIR, OUTPUT_DIR
    # O --base-dir é lido antes dos outros: ele define de onde vem o .env, que
    # dá os valores padrão das demais opções.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--base-dir", type=Path)
    base_dir = pre.parse_known_args()[0].base_dir
    if base_dir:
        if not base_dir.expanduser().is_dir():
            sys.exit(f"--base-dir: pasta não encontrada: {base_dir}")
        set_base_dir(base_dir)
    config = load_env()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-dir", type=Path, metavar="PASTA",
                        help="pasta de dados: o .env, o .acrcloud_uso.json e as pastas padrão "
                             "Input, Temp e Output ficam nela (padrão: a pasta do "
                             "script/executável; ex.: --base-dir bin)")
    parser.add_argument("-V", "--version", action="version",
                        version=f"SplitSong {__version__}",
                        help="mostra a versão do programa e sai")
    parser.add_argument("--input", type=Path, metavar="PASTA",
                        default=config_dir(config, "input-dir", INPUT_DIR),
                        help=f"pasta dos MP3 originais e dos downloads (padrão: input-dir do .env ou {INPUT_DIR.name})")
    parser.add_argument("--temp", type=Path, metavar="PASTA",
                        default=config_dir(config, "temp-dir", TEMP_DIR),
                        help=f"pasta do fluxo intermediário: musicas.txt e faixas exportadas/renomeadas (padrão: temp-dir do .env ou {TEMP_DIR.name})")
    parser.add_argument("--output", type=Path, metavar="PASTA",
                        default=config_dir(config, "output-dir", OUTPUT_DIR),
                        help=f"pasta das faixas finais, de onde o --copy lê (padrão: output-dir do .env ou {OUTPUT_DIR.name})")
    parser.add_argument("files", nargs="*", type=Path,
                        help="arquivos MP3 (padrão: todos da pasta Input)")
    parser.add_argument("--noise", type=float, default=-35,
                        help="nível em dB abaixo do qual é silêncio (padrão: -35)")
    parser.add_argument("--min-silence", type=float, default=0.5,
                        help="duração mínima do silêncio em segundos (padrão: 0.5)")
    parser.add_argument("--min-song", type=float, default=90,
                        help="duração mínima de uma música em segundos (padrão: 90)")
    parser.add_argument("--margin", type=float, default=1.5,
                        help="segundos de silêncio mantidos em cada borda da faixa, para não cortar intros e fade-outs (padrão: 1.5)")
    parser.add_argument("--download", nargs="+", metavar="URL",
                        help="baixa o áudio dos links do YouTube (vídeos ou playlists) como MP3 na pasta Input; com --detect/--split/--rename, processa só os baixados")
    parser.add_argument("--cookies-from-browser", metavar="NAVEGADOR",
                        default=setting(config, "cookies-from-browser"),
                        help="com --download, usa os cookies do navegador (ex.: firefox, chrome, edge) para o YouTube não bloquear como robô (padrão: cookies-from-browser do .env)")
    parser.add_argument("--detect", action="store_true",
                        help="detecta os silêncios e grava o musicas.txt na pasta Temp (padrão quando nenhuma outra ação é passada)")
    parser.add_argument("--split", action="store_true",
                        help="exporta um MP3 por faixa conforme o musicas.txt já existente (não refaz a detecção) e copia as faixas para a pasta Output (com --rename, só depois de renomear)")
    parser.add_argument("--rename", action="store_true",
                        help="identifica as faixas já exportadas na pasta Temp, renomeia os arquivos (não refaz a detecção nem o split) e copia as faixas para a pasta Output")
    parser.add_argument("--repeat", type=int, default=4, metavar="N",
                        help="com --rename, quantidade de trechos analisados por serviço; o nome que aparecer mais vezes vence (padrão: 4)")
    parser.add_argument("--manual", action="store_true",
                        help="corta nos tempos de um arquivo texto em vez de detectar os silêncios (álbuns sem pausa, shows ao vivo); um tempo por linha no formato horas;minutos;segundos; o arquivo padrão é o .txt com o mesmo nome do MP3 (ex.: Input/show.txt)")
    parser.add_argument("--times", type=Path, metavar="ARQUIVO",
                        help="com --manual, arquivo de tempos a usar no lugar do .txt com o nome do MP3 (só com um MP3)")
    parser.add_argument("--copy", type=Path, metavar="PASTA",
                        help="copia os MP3 da pasta Output (e subpastas) para PASTA em ordem aleatória, numerados e com o nome da pasta de origem no nome")
    parser.add_argument("--acoustid-key",
                        help="chave da API AcoustID (padrão: acoustid-key do .env ou variável ACOUSTID_KEY; "
                             "prefira essas duas: na linha de comando a chave fica no histórico do shell e "
                             "visível na lista de processos)")

    args = parser.parse_args()
    if args.acoustid_key:
        print("aviso: --acoustid-key deixa a chave no histórico do shell e na lista de "
              "processos; prefira o .env ou a variável ACOUSTID_KEY", file=sys.stderr)
    else:
        args.acoustid_key = setting(config, "acoustid-key")
    # Caminhos absolutos: o ffmpeg trata nomes como "concat:..." ou "http:..."
    # como protocolos, não como arquivos.
    check_files(args.files)
    args.files = [p.expanduser().resolve() for p in args.files]

    INPUT_DIR = args.input.expanduser().resolve()
    TEMP_DIR = args.temp.expanduser().resolve()
    OUTPUT_DIR = args.output.expanduser().resolve()

    acr = build_acrcloud(config)
    if args.rename and not args.acoustid_key and not acr:
        sys.exit("--rename precisa da chave do AcoustID ou da configuração do ACRCloud (veja o .env)")

    if args.times and not args.manual:
        sys.exit("--times só funciona junto com --manual")

    if args.repeat < 1:
        sys.exit("--repeat precisa ser pelo menos 1")

    if args.download:
        for url in args.download:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                sys.exit(f"--download aceita só links http(s): {url!r}")
        ytdlp = find_ytdlp()
        if not ytdlp:
            sys.exit("--download precisa do yt-dlp (winget install yt-dlp.yt-dlp ou pip install yt-dlp)")

        downloaded = [p for url in args.download
                      for p in download_mp3(url, ytdlp, args.cookies_from_browser)]
        if not downloaded:
            sys.exit("Nenhum arquivo foi baixado")
        if not (args.detect or args.split or args.rename or args.manual):
            return
        args.files = args.files or downloaded

    if args.copy and not (args.detect or args.split or args.rename or args.manual
                          or args.files):
        copy_shuffled(args.copy)
        return

    if args.manual:
        files = args.files or sorted(INPUT_DIR.glob("*.mp3"))
        if not files:
            sys.exit(f"Nenhum MP3 encontrado em {INPUT_DIR}")
        if args.times and len(files) > 1:
            sys.exit("--times só pode ser usado com um único MP3 (informe qual)")
        for path in files:
            out_dir = manual_split(path, args.times or path.with_suffix(".txt"))
            if out_dir and not args.rename:  # com --rename, copia só depois de renomear
                publish_tracks(out_dir)
    elif args.detect or not (args.split or args.rename):
        files = args.files or sorted(INPUT_DIR.glob("*.mp3"))
        if not files:
            sys.exit(f"Nenhum MP3 encontrado em {INPUT_DIR}")
        for path in files:
            process(path, args)

    # Com arquivos na linha de comando, o split/rename usam esses arquivos como
    # origem (podem estar fora da pasta Input); sem eles, todas as pastas de Temp.
    sources = {TEMP_DIR / path.stem: path for path in args.files}
    dirs = (list(sources) if args.files
            else sorted(d for d in TEMP_DIR.iterdir() if d.is_dir()) if TEMP_DIR.is_dir() else [])
    if args.split:
        for out_dir in dirs:
            if split_tracks(out_dir, sources.get(out_dir)) and not args.rename:  # copia depois
                publish_tracks(out_dir)
    if args.rename:
        if not acr:
            print("ACRCloud não configurado no .env; usando só o AcoustID")
        mb_cache = {}
        for out_dir in dirs:
            if rename_tracks(out_dir, args.acoustid_key, acr, args.repeat, mb_cache,
                             sources.get(out_dir)):
                publish_tracks(out_dir)
        if acr and acr.disabled:
            print(f"\nACRCloud interrompido: {acr.disabled}")
    if args.copy:
        copy_shuffled(args.copy)


if __name__ == "__main__":
    main()
