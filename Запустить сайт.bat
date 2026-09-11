@echo off
cd /d "%~dp0"
timeout /t 0.1 /nobreak >nul
start http://127.0.0.1:5000
python app.py
pause
  :: Если что я без понятия как писать что-то... Ярый Болат пж сделай тут все нормально
