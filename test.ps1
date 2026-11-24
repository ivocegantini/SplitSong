<#
.SYNOPSIS
    Roda os testes do SplitSong (pasta tests\) no Windows.

.DESCRIPTION
    - usa o Python do .venv se existir (crie com setup.bat / setup.ps1); senão o do sistema
    - os testes de integração precisam do ffmpeg/ffprobe no PATH (sem eles são pulados)

    Uso:  .\test.ps1 [-ExigirFfmpeg] [-Resumido] [opções do unittest]   (ou dê dois cliques no test.bat)
    -ExigirFfmpeg   falha (em vez de pular) se o ffmpeg/ffprobe não estiverem no PATH
    -Resumido       saída resumida (o padrão é -v, um teste por linha)
    -k PADRAO       roda só os testes cujo nome contém PADRAO (ex.: .\test.ps1 -k Publish)

    No Linux use o ./test.sh.
#>
param(
    [switch]$ExigirFfmpeg,
    [switch]$Resumido,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Extra = @()
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Fail($msg) { Write-Host "`nErro: $msg" -ForegroundColor Red; exit 1 }

$VenvPy = Join-Path '.venv' 'Scripts\python.exe'
if (Test-Path $VenvPy) {
    $Py = @($VenvPy)
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Py = @('py', '-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = @('python')
} else {
    Fail 'Python 3 não encontrado (rode o setup.bat)'
}
$PyExe = $Py[0]
$PyArgs = @($Py | Select-Object -Skip 1)

if ($ExigirFfmpeg) { $env:SPLITSONG_REQUIRE_FFMPEG = '1' }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'  # não deixa __pycache__ em tests\

Write-Host "== $(& $PyExe @PyArgs --version)" -ForegroundColor Cyan
$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
$ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if ($ffmpeg -and $ffprobe) {
    Write-Host "== ffmpeg: $($ffmpeg.Source)" -ForegroundColor Cyan
} else {
    $what = if ($env:SPLITSONG_REQUIRE_FFMPEG -eq '1') { 'vão falhar' } else { 'serão pulados' }
    Write-Host "== ffmpeg/ffprobe não encontrados: os testes de integração $what" -ForegroundColor Yellow
}
Write-Host ''

$Verbosity = if ($Resumido) { @() } else { @('-v') }
& $PyExe @PyArgs -m unittest discover -s tests @Verbosity @Extra
exit $LASTEXITCODE
