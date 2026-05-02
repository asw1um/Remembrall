# Move to the project directory
Set-Location -Path "E:\backup\personal\own projects\hrithik"

# Activate the Virtual Environment
& ".\venv\Scripts\Activate.ps1"

do {
    Write-Host "🤖 Starting Bot..." -ForegroundColor Green
    # Running python via the venv
    python main.py
    
    # If the bot exits/crashes, this part runs
    Write-Host "⚠️ Bot stopped or crashed. Restarting in 5 seconds..." -ForegroundColor Red
    Start-Sleep -Seconds 5
} while ($true)