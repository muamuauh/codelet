# One-click launcher for the codelet web GUI (Windows / PowerShell).
#   Right-click -> "Run with PowerShell", or:  ./run-web.ps1  [-Port 9000]
# Starts the server and opens your browser automatically.
param([int]$Port = 8000, [string]$WebHost = "127.0.0.1")

$py = "python"
& $py -c "import codelet" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "codelet isn't importable by '$py'." -ForegroundColor Yellow
    Write-Host "Activate the env first (e.g. 'conda activate miniClaudeCode'), or install it:"
    Write-Host "    pip install -e `".[web]`""
    exit 1
}
& $py -m codelet.web --host $WebHost --port $Port
