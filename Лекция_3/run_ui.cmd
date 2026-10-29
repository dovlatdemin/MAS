@echo off
cd /d "%~dp0"
set "PYTHONUTF8=1"
powershell.exe -NoProfile -Command "try { $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 2; if ($response.Content -eq 'ok') { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
    start "" "http://127.0.0.1:8501"
    exit /b 0
)
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "for ($attempt = 0; $attempt -lt 60; $attempt++) { try { $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 1; if ($response.Content -eq 'ok') { Start-Process 'http://127.0.0.1:8501'; exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }"
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
if errorlevel 1 pause
