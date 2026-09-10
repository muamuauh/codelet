# One-click launcher for the codelet web GUI (Windows / PowerShell).
#   Right-click -> "Run with PowerShell", or:  ./run-web.ps1 [-Port 9000] [-NoOpen]
#   Force an interpreter with:                 ./run-web.ps1 -Python C:\path\to\python.exe
# Starts the server and opens your browser automatically.
param(
    [int]$Port = 8000,
    [string]$WebHost = "127.0.0.1",
    [string]$Python = "",
    [switch]$NoOpen
)

# Run from the repo so .env (LLM config) and .codelet/ (settings, skills) load.
Push-Location $PSScriptRoot
try {
    # Probe for uvicorn/fastapi, NOT just `import codelet`: the repo root is on
    # sys.path when cwd is the project, so ANY python "imports codelet" here --
    # a false green light that then dies on the missing web extras.
    function Test-Interpreter([string]$exe) {
        if (-not $exe) { return $false }
        & $exe -c "import uvicorn, fastapi, codelet" 2>$null
        return ($LASTEXITCODE -eq 0)
    }

    $candidates = @()
    if ($Python) { $candidates += $Python }
    if ($env:CONDA_PREFIX) { $candidates += (Join-Path $env:CONDA_PREFIX "python.exe") }
    foreach ($root in @("$env:USERPROFILE\.conda\envs", "$env:LOCALAPPDATA\conda\conda\envs")) {
        $candidates += (Join-Path $root "codelet\python.exe")
    }
    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($onPath) { $candidates += $onPath }

    $py = $null
    $tried = @()
    foreach ($c in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        # A bare command name won't survive Test-Path; resolve it first.
        $exe = if (Test-Path -LiteralPath $c) { $c } else { (Get-Command $c -ErrorAction SilentlyContinue).Source }
        if (-not $exe) { continue }
        $tried += $exe
        if (Test-Interpreter $exe) { $py = $exe; break }
    }

    if (-not $py) {
        Write-Host "No Python with the codelet web extras was found." -ForegroundColor Yellow
        if ($tried) {
            Write-Host "Tried:"
            $tried | ForEach-Object { Write-Host "    $_" }
        }
        Write-Host ""
        Write-Host "Fix it with either:"
        Write-Host "    conda activate codelet;  pip install -e `".[web]`""
        Write-Host "    .\run-web.ps1 -Python C:\path\to\env\python.exe"
        exit 1
    }

    Write-Host "codelet: using $py" -ForegroundColor DarkGray
    $webArgs = @("-m", "codelet.web", "--host", $WebHost, "--port", $Port)
    if ($NoOpen) { $webArgs += "--no-open" }
    & $py @webArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
