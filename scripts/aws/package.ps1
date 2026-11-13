$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$archivePath = if ($args.Count -gt 0) {
    [System.IO.Path]::GetFullPath($args[0])
} else {
    Join-Path $env:TEMP "anipick-source.tar.gz"
}

$tarArguments = @(
    "-czf", $archivePath,
    "--exclude=.env",
    "--exclude=.git",
    "--exclude=.venv",
    "--exclude=.venv-*",
    "--exclude=__pycache__",
    "--exclude=*.py[cod]",
    "--exclude=data/anime.sqlite3",
    "--exclude=data/character-cache.json",
    "-C", $projectRoot,
    "."
)

& tar @tarArguments
if ($LASTEXITCODE -ne 0) {
    throw "Failed to package AniPick source (tar exit code $LASTEXITCODE)."
}
Write-Output "Deployment archive created: $archivePath"
