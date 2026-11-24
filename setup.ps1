<#
.SYNOPSIS
    Prepara o ambiente para rodar e compilar o SplitSong no Windows.

.DESCRIPTION
    - instala pelo winget: Python 3.14, FFmpeg (Gyan), yt-dlp e Deno
    - cria o .venv e instala nele o requirements.txt (PyInstaller, yt-dlp)
    - cria as pastas Input, Temp e Output
    - verifica se está tudo funcionando

    Uso:  .\setup.ps1 [-SemSistema]      (ou dê dois cliques no setup.bat)
    -SemSistema   não instala programas pelo winget (só cria o .venv)

    No Linux, use ./setup.sh.
#>
param([switch]$SemSistema)

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$VenvDir = '.venv'
$MinPy = [version]'3.10'
$Packages = [ordered]@{
    'Python.Python.3.14' = 'Python 3.14'
    'Gyan.FFmpeg'        = 'FFmpeg'
    'yt-dlp.yt-dlp'      = 'yt-dlp'
    'DenoLand.Deno'      = 'Deno (usado pelo yt-dlp no YouTube)'
}

function Info($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   [aviso] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "`nErro: $msg" -ForegroundColor Red; exit 1 }
function Has($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Update-SessionPath {
    # O winget só atualiza o PATH de terminais abertos depois da instalação.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $links = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    $env:Path = "$machine;$user;$links"
}

function Find-Python {
    # Retorna o comando ('py -3', 'python'...) de um Python >= $MinPy, ou $null.
    foreach ($cand in 'py -3', 'python', 'python3') {
        $parts = $cand -split ' '
        $cmd = Get-Command $parts[0] -ErrorAction SilentlyContinue
        # O "python.exe" de WindowsApps é só um atalho para a Microsoft Store.
        if (-not $cmd -or $cmd.Source -like '*\WindowsApps\*') { continue }
        $args_ = @($parts | Select-Object -Skip 1)
        $ver = & $parts[0] @args_ -c "import sys, venv; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -and ([version]$ver -ge $MinPy)) { return $cand }
    }
    return $null
}

# --------------------------------------------------------------------------- #
# Ambiente
# --------------------------------------------------------------------------- #
Info 'Verificando o ambiente'
if ($IsLinux) {
    Write-Host '   Linux detectado; executando o setup.sh'
    & bash ./setup.sh @(if ($SemSistema) { '--sem-sistema' })
    exit $LASTEXITCODE
}
if ($IsMacOS) { Fail 'sistema não suportado: macOS (use Windows ou Linux)' }
$os = [Environment]::OSVersion.Version
Ok "Windows $($os.Major).$($os.Minor) build $($os.Build), PowerShell $($PSVersionTable.PSVersion)"

# --------------------------------------------------------------------------- #
# Programas (winget)
# --------------------------------------------------------------------------- #
if (-not $SemSistema) {
    Info 'Programas (winget)'
    if (-not (Has 'winget')) {
        Fail 'winget não encontrado. Instale o "Instalador de Aplicativo" (App Installer) pela Microsoft Store e rode o setup de novo.'
    }
    foreach ($id in $Packages.Keys) {
        if ($id -like 'Python.*' -and (Find-Python)) {
            Ok "Python já instalado ($(Find-Python))"
            continue
        }
        winget list --id $id --exact --accept-source-agreements *> $null
        if ($LASTEXITCODE -eq 0) {
            Ok "$($Packages[$id]) já instalado"
            continue
        }
        Write-Host "   instalando $($Packages[$id])..."
        winget install --id $id --exact --silent --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { Ok "$($Packages[$id]) instalado" }
        else { Warn "falha ao instalar $id (código $LASTEXITCODE)" }
    }
    Update-SessionPath
}

$Python = Find-Python
if (-not $Python) { Fail "Python >= $MinPy não encontrado (winget install Python.Python.3.14)" }
$pyParts = $Python -split ' '
$pyArgs = @($pyParts | Select-Object -Skip 1)
Ok "$(& $pyParts[0] @pyArgs --version) ($Python)"

# --------------------------------------------------------------------------- #
# Ambiente virtual
# --------------------------------------------------------------------------- #
Info "Ambiente virtual ($VenvDir)"
$Py = Join-Path $VenvDir 'Scripts\python.exe'
$venvOk = $false
if (Test-Path $Py) {
    & $Py -c 'import sys' 2>$null
    $venvOk = ($LASTEXITCODE -eq 0)
}
if ($venvOk) {
    Ok 'já existe, reaproveitando'
} else {
    if (Test-Path $VenvDir) { Remove-Item -Recurse -Force $VenvDir }
    & $pyParts[0] @pyArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail 'não foi possível criar o .venv' }
    Ok 'criado'
}

& $Py -m pip install --upgrade pip
& $Py -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail 'falha ao instalar o requirements.txt' }
Ok 'dependências instaladas'

# --------------------------------------------------------------------------- #
# Pastas de trabalho
# --------------------------------------------------------------------------- #
foreach ($dir in 'Input', 'Temp', 'Output') {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# --------------------------------------------------------------------------- #
# Verificação
# --------------------------------------------------------------------------- #
Info 'Verificação'
$problems = 0
if (Has 'ffmpeg') {
    Ok "ffmpeg $((& ffmpeg -hide_banner -version | Select-Object -First 1).Split(' ')[2])"
    if ((& ffmpeg -hide_banner -muxers 2>$null) -match '\bchromaprint\b') {
        Ok 'ffmpeg com chromaprint (necessário para o AcoustID)'
    } else {
        Warn 'este ffmpeg não tem chromaprint: o --rename só vai funcionar com o ACRCloud'
    }
} else { Warn 'ffmpeg não encontrado (winget install Gyan.FFmpeg)'; $problems++ }
if (Has 'ffprobe') { Ok 'ffprobe' } else { Warn 'ffprobe não encontrado'; $problems++ }
Ok "PyInstaller $(& $Py -m PyInstaller --version)"
if (Has 'yt-dlp') { Ok "yt-dlp $(& yt-dlp --version) (winget, usado pelo .exe)" }
else { Warn 'yt-dlp do winget não encontrado: o --download do .exe não vai funcionar' }
if (Has 'deno') { Ok 'deno' } else { Warn 'deno não encontrado (opcional, winget install DenoLand.Deno)' }
& $Py split_songs.py --help *> $null
if ($LASTEXITCODE -eq 0) { Ok 'split_songs.py executa' } else { Warn 'split_songs.py não executou'; $problems++ }
if (-not (Test-Path '.env') -and (Test-Path '.env.exemple')) {
    Copy-Item '.env.exemple' '.env'
    Warn '.env criado a partir do .env.exemple: preencha as chaves do AcoustID/ACRCloud'
}

Info 'Pronto'
Write-Host "   Ativar o ambiente:  .\$VenvDir\Scripts\Activate.ps1"
Write-Host '   Rodar pelo fonte:   python split_songs.py --help'
Write-Host '   Compilar:           python -m PyInstaller --onefile --name SplitSong split_songs.py'
Write-Host '                       (gera dist\SplitSong.exe)'
Write-Host '   Abra um novo terminal para que o PATH com os programas instalados valha em todo lugar.'
exit $problems
