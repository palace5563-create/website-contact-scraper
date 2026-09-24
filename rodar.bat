@echo off
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% -m pip install --quiet requests
%PY% ml_finder.py -f url.txt --csv resultado.csv
echo.
pause
