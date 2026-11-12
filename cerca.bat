@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Serve Python: scaricalo da https://www.python.org/downloads/ e spunta Add python.exe to PATH & pause & exit /b)
python -m pip install -q -r requirements.txt
set "RICERCA="
set "PMIN="
set "PMAX="
set /p RICERCA=Cosa cerchi? 
set /p PMIN=Prezzo minimo (invio per saltare): 
set /p PMAX=Prezzo massimo (invio per saltare): 
python vinted_cerca.py "%RICERCA%" --prezzo-min "%PMIN%" --prezzo-max "%PMAX%"
pause
