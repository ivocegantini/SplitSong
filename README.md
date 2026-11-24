# SplitSong

Ferramenta de linha de comando que quebra um MP3 longo (álbum completo, coletânea, show ao vivo) em um MP3 por música, identifica cada faixa pelo áudio e monta uma seleção embaralhada para pen drive ou som do carro.

- **Detecta** as músicas pelos silêncios entre elas, ou corta em tempos informados à mão.
- **Exporta** cada faixa sem recodificar (`ffmpeg -c copy`), já com o número da faixa nas tags.
- **Identifica** artista e título com o [AcoustID](https://acoustid.org)/MusicBrainz e o [ACRCloud](https://www.acrcloud.com), e renomeia os arquivos.
- **Baixa** o áudio de vídeos e playlists do YouTube com o `yt-dlp`.
- **Copia** a biblioteca em ordem aleatória e numerada, sem músicas repetidas e evitando a mesma banda em sequência.

## Requisitos

- Python 3.10+ (o `split_songs.py` só usa a biblioteca padrão)
- `ffmpeg` e `ffprobe` no PATH
- `yt-dlp`, só para o `--download`
- Chave do AcoustID e/ou projeto no ACRCloud, só para o `--rename`

## Instalação

Os scripts de setup instalam as dependências do sistema, criam o `.venv` com o [requirements.txt](requirements.txt) (PyInstaller e yt-dlp) e as pastas `Input`, `Temp` e `Output`.

```bash
./setup.sh                 # Linux (apt/dnf/pacman/zypper); --sem-sistema só cria o .venv
```

```powershell
.\setup.ps1                # Windows (winget: Python, FFmpeg, yt-dlp, Deno); -SemSistema só cria o .venv
setup.bat                  # o mesmo, com dois cliques
```

O setup copia o [.env.exemple](.env.exemple) para `.env`; preencha as chaves (veja [Configuração](#configuração)).

## Fluxo de trabalho

Tudo o que o programa lê ou gera fica na **pasta de dados**: o `.env`, o `.acrcloud_uso.json` e as três pastas de trabalho abaixo. Por padrão ela é a pasta do script (ou do executável); com `--base-dir PASTA` ela passa a ser `PASTA`. Assim dá para rodar o fonte da raiz usando os dados do `bin/`, os mesmos do executável `bin/SplitSong`:

```bash
python split_songs.py --base-dir bin --detect     # lê bin/.env e bin/Input, grava em bin/Temp
python split_songs.py --base-dir bin --split --rename  # exporta e copia para bin/Output
```

O caminho pode ser relativo à pasta atual ou absoluto, e a pasta precisa existir. As opções `--input`, `--temp` e `--output` (e `input-dir` etc. no `.env`) continuam podendo trocar cada pasta de trabalho separadamente.

| Pasta | Conteúdo |
|---|---|
| `Input` | MP3 originais e downloads do YouTube |
| `Temp` | uma subpasta por MP3 com o `musicas.txt` e as faixas exportadas |
| `Output` | faixas finais, uma subpasta por MP3; é a origem do `--copy` |

O caminho comum é:

```bash
python split_songs.py --download URL                # 1. baixa para Input
python split_songs.py --detect                      # 2. detecta as músicas → Temp/<nome>/musicas.txt
#    (opcional) edite o musicas.txt para ajustar cortes, juntar faixas ou dar nomes
python split_songs.py --split --rename              # 3. exporta, identifica e copia para Output/<nome>
python split_songs.py --copy /media/pendrive/Musicas  # 4. monta a seleção embaralhada
```

As etapas podem ser combinadas num só comando, por exemplo `--download URL --detect --split --rename`. Com `--download`, as etapas seguintes processam só os arquivos baixados.

## Uso

```bash
python split_songs.py                    # detecta as faixas de todos os .mp3 da pasta Input
python split_songs.py arquivo.mp3        # detecta só os arquivos indicados
python split_songs.py --split            # exporta as faixas conforme o musicas.txt
python split_songs.py --detect --split   # detecta e já exporta
python split_songs.py --rename           # identifica e renomeia as faixas já exportadas
python split_songs.py --rename --repeat 3  # analisa 3 trechos por serviço (padrão: 4)
python split_songs.py --download URL     # baixa o áudio (vídeo ou playlist) como MP3
python split_songs.py --download URL --cookies-from-browser firefox
                                         # usa os cookies do navegador quando o YouTube
                                         # pede para confirmar que não é um robô
python split_songs.py --manual           # corta cada MP3 nos tempos do .txt de mesmo nome
python split_songs.py --manual show.mp3 --times cortes.txt
python split_songs.py --copy PASTA       # copia a pasta Output embaralhada para PASTA
python split_songs.py --input ~/Albuns --temp /tmp/split --output ~/Musicas
python split_songs.py --version          # mostra a versão do programa
python split_songs.py --help             # lista todas as opções
```

Sem nenhuma ação, o programa roda o `--detect`.

### Detecção por silêncio (`--detect`)

Procura trechos abaixo de `--noise` dB que durem pelo menos `--min-silence` segundos e corta neles, descartando cortes que gerariam músicas menores que `--min-song`. Cada faixa mantém `--margin` segundos de silêncio nas bordas para não perder intros e fade-outs.

O resultado vai para `Temp/<nome>/musicas.txt`: na primeira linha o nome do MP3 de origem e depois uma linha por faixa.

```text
Show ao vivo.mp3
# Edite e rode o --split: ...
01  00:00:00.00 - 00:03:10.25  (00:03:10)  The Who - My Generation [acoustid]
02  00:03:11.50 - 00:06:42.00  (00:03:30)  Queen - Radio Ga Ga [acrcloud, conferir]
03  00:06:43.00 - 00:10:55.40  (00:04:12)  (não identificada)
```

O `--split` lê esse arquivo sem refazer a detecção, então você pode corrigi-lo à mão antes de exportar: mudar os tempos de início e fim (aceitam `hh:mm:ss.cc`, `mm:ss` ou `;` no lugar de `:`), apagar uma linha para juntar faixas (aumentando o fim da anterior) ou trocar `(não identificada)` por `Artista - Título`. O número da faixa e a duração entre parênteses são opcionais e recalculados; linhas vazias ou começando com `#` são ignoradas. Uma linha fora do formato faz o `--split` pular aquele MP3 e mostrar o número da linha. Sem arquivos na linha de comando, o `--split` procura o MP3 de origem (o nome da primeira linha) só na pasta `Input`; um MP3 em outra pasta precisa ser passado de novo, ex.: `python split_songs.py --split outra/pasta/show.mp3`.

Se a detecção errar, ajuste os parâmetros:

| Sintoma | Ajuste |
|---|---|
| músicas cortadas no meio (passagens baixas) | `--noise` mais baixo (ex.: `-45`) ou `--min-silence` maior |
| várias músicas numa faixa só | `--noise` mais alto (ex.: `-30`) ou `--min-silence` menor |
| vinhetas curtas viram faixas | `--min-song` maior |

### Cortes manuais (`--manual`)

Para álbuns sem pausa entre as músicas ou shows ao vivo. Crie um `.txt` com o mesmo nome do MP3 (ex.: `Input/show.txt`) com um tempo de corte por linha:

```
# horas;minutos;segundos  (também aceita ":" e só minutos;segundos)
0;03;45
0;07;12.5
1;02;10
```

Cada tempo fecha a faixa que começou no corte anterior; o que sobra depois do último vira a faixa final. Linhas vazias e o texto depois de `#` são ignorados. O `--manual` já grava o `musicas.txt` e exporta as faixas. Use `--times ARQUIVO` para indicar outro arquivo de tempos (só com um MP3).

### Identificação (`--rename`)

Para cada faixa ainda sem nome (`NN - Faixa NN.mp3`), envia `--repeat` trechos do MP3 original ao AcoustID e ao ACRCloud, e o nome que aparece mais vezes vence. A faixa é renomeada para `NN - Artista - Título.mp3`, com as tags ID3 preenchidas, e o `musicas.txt` é atualizado com o nome e a fonte (`[acoustid]` ou `[acrcloud]`). Nomes com pontuação baixa ou empatados ficam marcados com `conferir`.

Basta um dos dois serviços configurado. O uso do ACRCloud é limitado por `acrcloud-daily-limit` (contado em `.acrcloud_uso.json`) e espaçado por `acrcloud-interval`.

### Download (`--download`)

Baixa o áudio de um ou mais links do YouTube (vídeos ou playlists) como MP3 na pasta `Input`. Se o YouTube bloquear o acesso como robô, passe `--cookies-from-browser firefox` (ou `chrome`, `edge`) ou configure `cookies-from-browser` no `.env`.

### Seleção embaralhada (`--copy`)

Copia todos os MP3 de `Output` (e subpastas) para a pasta indicada:

- remove repetidas (mesmo artista e título, ignorando maiúsculas, versões como "Remastered"/"Live" e o "The" inicial);
- embaralha evitando a mesma banda em sequência;
- numera os arquivos (`001 - <pasta> - <faixa>.mp3`) e os copia um a um, na ordem da lista, para aparelhos que tocam pela ordem de gravação;
- salva a ordem em `lista.txt`.

Em pen drives FAT, prefira uma subpasta: a raiz só aceita 512 entradas.

## Opções

| Opção | Descrição |
|---|---|
| `files` | MP3 a processar (padrão: todos da pasta `Input`); podem estar em qualquer pasta, e o `--split`/`--rename` do mesmo comando os usam como origem. Padrões como `*.mp3` são expandidos pelo shell, então não use aspas neles (`pasta/*.mp3`, não `'pasta/*.mp3'`); no cmd/PowerShell do Windows, que não expandem padrões, liste os arquivos. Um padrão que chega sem expandir gera um aviso |
| `--detect` | detecta os silêncios e grava o `musicas.txt` |
| `--split` | exporta um MP3 por faixa conforme o `musicas.txt` |
| `--rename` | identifica e renomeia as faixas exportadas |
| `--manual` | corta nos tempos de um arquivo texto |
| `--times ARQUIVO` | com `--manual`, arquivo de tempos a usar |
| `--download URL…` | baixa o áudio de vídeos/playlists do YouTube |
| `--cookies-from-browser NAVEGADOR` | cookies do navegador para o `yt-dlp` |
| `--copy PASTA` | copia a pasta `Output` embaralhada e numerada |
| `--noise DB` | nível abaixo do qual é silêncio (padrão: `-35`) |
| `--min-silence S` | duração mínima do silêncio (padrão: `0.5`) |
| `--min-song S` | duração mínima de uma música (padrão: `90`) |
| `--margin S` | silêncio mantido em cada borda da faixa (padrão: `1.5`) |
| `--repeat N` | trechos analisados por serviço no `--rename` (padrão: `4`) |
| `--acoustid-key CHAVE` | chave do AcoustID (padrão: `.env`; prefira o `.env`, pois na linha de comando a chave fica no histórico do shell) |
| `--base-dir PASTA` | pasta de dados: `.env`, `.acrcloud_uso.json`, `Input`, `Temp` e `Output` (padrão: pasta do script) |
| `--input`, `--temp`, `--output PASTA` | troca as pastas de trabalho |
| `-V`, `--version` | mostra a versão e sai |

## Configuração

O `.env` fica na pasta de dados: a pasta do script ou do executável, ou a do `--base-dir`. Cada chave também pode vir de uma variável de ambiente em maiúsculas com `_` (ex.: `acoustid-key` → `ACOUSTID_KEY`), que tem prioridade sobre o `.env`.

| Chave | Descrição |
|---|---|
| `acoustid-key` | chave do AcoustID ([criar](https://acoustid.org/new-application)) |
| `acrcloud-host` | host do projeto ACRCloud (ex.: `identify-us-west-2.acrcloud.com`) |
| `acrcloud-access-key` | access key do projeto ACRCloud |
| `acrcloud-access-secret` | access secret do projeto ACRCloud |
| `acrcloud-interval` | segundos entre envios ao ACRCloud (padrão: `1.5`) |
| `acrcloud-daily-limit` | máximo de envios ao ACRCloud por dia (padrão: `100`) |
| `cookies-from-browser` | navegador padrão do `--cookies-from-browser` |
| `input-dir`, `temp-dir`, `output-dir` | pastas de trabalho (padrão: `Input`, `Temp`, `Output`); caminhos relativos partem da pasta de dados |

As credenciais do ACRCloud ficam em [console.acrcloud.com](https://console.acrcloud.com), em *Audio & Video Recognition*, no projeto.

## Segurança e privacidade

- **Chaves:** o `.env` guarda as chaves das APIs. Ele não vai para o git (`.gitignore`), e no Linux o `setup.sh` deixa o arquivo legível só pelo dono (`chmod 600 .env`). O programa avisa se ele estiver aberto para outros usuários.
- **Envio de áudio:** o `--rename` envia trechos curtos das músicas ao [AcoustID](https://acoustid.org) (só a impressão digital) e ao [ACRCloud](https://www.acrcloud.com) (o próprio áudio, 12 s por trecho).
- **Cookies:** o `--cookies-from-browser` dá ao `yt-dlp` acesso a **todos** os cookies do navegador escolhido, não só aos do YouTube. Se puder, use um perfil de navegador separado, logado só no YouTube.
- **Nomes vindos da internet:** artista e título vêm de bases públicas editáveis por qualquer pessoa (MusicBrainz). O programa remove deles os caracteres de controle e de direção do texto antes de mostrá-los, gravá-los no `musicas.txt` ou usá-los em nomes de arquivo.
- **Arquivos apagados:** ao regravar `Output/<nome>`, só são apagados os MP3 que o próprio programa copiou para lá (lista em `.splitsong.json`); arquivos seus nessa pasta ficam intactos.
- **Programas externos:** `ffmpeg`, `ffprobe`, `yt-dlp` e `deno` são procurados só nas pastas do `PATH` (nunca na pasta atual), para que um executável deixado junto com os MP3 não seja executado no lugar do verdadeiro.

## Executável

Gera um executável único com o PyInstaller, copiado para `bin/`. O executável procura as pastas e o `.env` ao seu lado (ou no `--base-dir`).

```bash
./build.sh                 # Linux → bin/SplitSong   (--limpar, --sem-copia)
```

```powershell
.\build.ps1                # Windows → bin\SplitSong.exe   (-Limpar, -SemCopia; ou build.bat)
```

O PyInstaller não faz compilação cruzada: gere o executável de cada sistema nele mesmo. O executável usa os mesmos comandos, trocando `python split_songs.py` por `SplitSong`.

### Pacote .deb (Debian/Ubuntu)

Cada release traz também um `.deb`, que instala o executável e o `ffmpeg` (e recomenda o `yt-dlp`):

```bash
sudo apt install ./SplitSong-v1.0.0-linux-amd64.deb
splitsong --detect         # o mesmo que SplitSong / python split_songs.py
```

O comando `splitsong` usa `~/SplitSong` como pasta de dados (ou a do `SPLITSONG_HOME`): na primeira execução cria ali as pastas `Input`, `Temp` e `Output` e o `.env` (copiado do exemplo, legível só pelo dono). Um `--base-dir` na linha de comando continua valendo. Para gerar o pacote localmente, depois do `./build.sh`: `./build_deb.sh` → `release/SplitSong-v<versão>-linux-amd64.deb`.

## Testes

Os testes ficam em [tests/](tests/) e usam só a biblioteca padrão (`unittest`):

```bash
./test.sh                  # Linux   (--exigir-ffmpeg, -q, -k PADRAO)
```

```powershell
.\test.ps1                 # Windows (-ExigirFfmpeg, -Resumido, -k PADRAO; ou dois cliques no test.bat)
```

Os scripts usam o Python do `.venv` (ou o do sistema, se não houver `.venv`). Também dá para rodar direto: `python -m unittest discover -s tests -v`.

Os testes de integração geram um MP3 sintético e rodam o `--detect`, `--split` e `--manual` com o ffmpeg de verdade; sem o ffmpeg/ffprobe no PATH eles são pulados (com `SPLITSONG_REQUIRE_FFMPEG=1` passam a falhar). O workflow [tests.yml](.github/workflows/tests.yml) roda tudo no Linux e no Windows a cada push e pull request, e o release só é compilado se os testes passarem.

## Release

A versão fica só no `__version__` do [split_songs.py](split_songs.py). Para lançar uma versão:

1. Mude o `__version__` (ex.: `"1.1.0"`; `"1.1.0-rc1"` sai como pré-release).
2. Faça commit e push na `main`.

O workflow [release.yml](.github/workflows/release.yml) lê a constante e, se a tag `v<versão>` ainda não existir, roda os testes, compila o executável de Linux e de Windows no GitHub Actions, confere o `--version` de cada um, gera o `.deb` e testa a instalação dele, cria a tag e publica o release com o executável de Linux (`.tar.gz`), o `.deb`, o `.exe`, o `SHA256SUMS.txt` e as notas geradas a partir dos commits. Para conferir um download: `sha256sum -c --ignore-missing SHA256SUMS.txt` (no Windows: `Get-FileHash SplitSong-*.exe`). Pushes que não mudam a versão não geram release. Para disparar à mão: *Actions → Release → Run workflow*, ou `gh workflow run release.yml`.
