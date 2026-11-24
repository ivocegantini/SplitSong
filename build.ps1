<#
.SYNOPSIS
    Compila o SplitSong para Windows com o PyInstaller (SplitSong.exe).

.DESCRIPTION
    - usa o Python do .venv (crie com setup.bat / setup.ps1)
    - gera dist\SplitSong.exe e copia para bin\SplitSong.exe
    - testa o executável gerado (--help)

    Uso:  .\build.ps1 [-Limpar] [-SemCopia]      (ou dê dois cliques no build.bat)
    -Limpar     apaga build\ e dist\ antes de compilar
    -SemCopia   não copia o executável para bin\

    O PyInstaller não compila para outro sistema: o executável de Linux
    precisa ser gerado no Linux (./build.sh).
#>
param([switch]$Limpar, [switch]$SemCopia)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Name = 'SplitSong'
$Entry = 'split_songs.py'
$VenvDir = '.venv'

function Info($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   [ok] $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "`nErro: $msg" -ForegroundColor Red; exit 1 }

Info 'Verificando o ambiente'
$Py = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $Py)) { Fail "$VenvDir não encontrado; rode o setup.bat primeiro" }
$pyiVersion = & $Py -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller não instalado no $VenvDir; rode o setup.bat" }
Ok (& $Py --version)
Ok "PyInstaller $pyiVersion"

if ($Limpar) {
    Info 'Limpando build\ e dist\'
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    Ok 'limpo'
}

Info "Compilando $Name.exe"
& $Py -m PyInstaller --noconfirm --onefile --console `
    --name $Name `
    --distpath dist --workpath build --specpath build `
    $Entry
if ($LASTEXITCODE -ne 0) { Fail "o PyInstaller falhou (código $LASTEXITCODE)" }

$Exe = Join-Path 'dist' "$Name.exe"
if (-not (Test-Path $Exe)) { Fail "o PyInstaller não gerou $Exe" }
$sizeMb = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Ok "$Exe ($sizeMb MB)"

Info 'Testando o executável'
& $Exe --help *> $null
if ($LASTEXITCODE -ne 0) { Fail "$Exe --help falhou" }
Ok "$Exe --help executa"

$Final = $Exe
if (-not $SemCopia) {
    New-Item -ItemType Directory -Force -Path bin | Out-Null
    try {
        Copy-Item -Force $Exe (Join-Path 'bin' "$Name.exe")
    } catch {
        Fail "não foi possível copiar para bin\$Name.exe (ele está aberto?)"
    }
    $Final = Join-Path 'bin' "$Name.exe"
    Ok "copiado para $Final"
}

Info 'Pronto'
Write-Host "   Executável: $Final"
Write-Host '   As pastas Input, Temp e Output e o .env ficam ao lado do executável.'
Write-Host '   Ele precisa do ffmpeg/ffprobe no PATH (e do yt-dlp para o --download).'
exit 0
