# One-shot Windows setup for catalyst-bot. Run from the unzipped folder in PowerShell:
#   Set-ExecutionPolicy -Scope Process Bypass -Force; .\deploy\windows-setup.ps1
# It installs the Python environment, creates .env (asking for each key), runs the checks and offers to start the bot.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
Write-Host "== catalyst-bot setup in $(Get-Location)" -ForegroundColor Cyan

# 1. Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "Python not found. Install Python 3.11+ from https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH', then re-run." -ForegroundColor Red; exit 1 }
$ver = (& python --version) -replace "Python ", ""
if ([version]$ver -lt [version]"3.11") { Write-Host "Python $ver is too old; need 3.11+." -ForegroundColor Red; exit 1 }
Write-Host "Python $ver OK"

# 2. Virtual environment + dependencies
if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install --quiet -e .
Write-Host "dependencies installed"

# 3. .env
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
function Set-EnvValue($name, $prompt, $secret) {
  $current = (Select-String -Path .env -Pattern "^$name=(.*)$" | ForEach-Object { $_.Matches[0].Groups[1].Value })
  if ($current) { Write-Host "$name already set"; return }
  if ($secret) { $v = Read-Host -AsSecureString $prompt; $v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($v)) }
  else { $v = Read-Host $prompt }
  if ($v) { (Get-Content .env) -replace "^$name=.*$", "$name=$v" | Set-Content .env }
}
Set-EnvValue "ANTHROPIC_API_KEY" "Anthropic API key (console.anthropic.com -> API keys)" $true
Set-EnvValue "TELEGRAM_BOT_TOKEN" "Telegram bot token from @BotFather (Enter to skip)" $true
Set-EnvValue "TELEGRAM_CHAT_ID" "Telegram chat id (message @userinfobot; Enter to skip)" $false
$ack = Read-Host "Type I_UNDERSTAND_THE_RISK to enable LIVE trading (anything else keeps the interlock closed)"
if ($ack -eq "I_UNDERSTAND_THE_RISK") { (Get-Content .env) -replace "^LIVE_TRADING_ACK=.*$", "LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK" | Set-Content .env; Write-Host "live interlock OPEN" -ForegroundColor Yellow }
else { Write-Host "live interlock closed: the bot will refuse to start in live mode until .env has LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISK" }

# 4. Checks
& .\.venv\Scripts\Activate.ps1
Write-Host "`n== replay (synthetic end-to-end)" -ForegroundColor Cyan
earnings-bot replay
Write-Host "`n== doctor (needs TWS running with API enabled on port 7496)" -ForegroundColor Cyan
earnings-bot doctor
Write-Host "`n== this week's calendar (live data)" -ForegroundColor Cyan
earnings-bot preview --start 2026-11-09 --days 5

# 5. Autostart shortcut
$startup = [Environment]::GetFolderPath("Startup")
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut((Join-Path $startup "catalyst-bot.lnk"))
$sc.TargetPath = (Resolve-Path .\deploy\windows-start.bat).Path
$sc.WorkingDirectory = (Get-Location).Path
$sc.Save()
Write-Host "autostart shortcut created in $startup"

$go = Read-Host "`nStart the bot now? (y/n)"
if ($go -eq "y") { earnings-bot run } else { Write-Host "Start later with: .\deploy\windows-start.bat" }
