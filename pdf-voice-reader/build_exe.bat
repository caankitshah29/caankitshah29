@echo off
REM ---------------------------------------------------------------
REM  Builds a single portable file:  dist\PDFVoiceReader.exe
REM  Needs Python 3.10+ from python.org (tick "Add python to PATH").
REM ---------------------------------------------------------------
cd /d "%~dp0"
python -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || goto :error
python make_icon.py || goto :error
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name PDFVoiceReader ^
  --icon app.ico ^
  --add-data "app.ico;." ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  --collect-data edge_tts ^
  pdf_voice_reader.py || goto :error
echo.
echo Done!  Your app is:  %~dp0dist\PDFVoiceReader.exe
pause
exit /b 0
:error
echo.
echo Build failed - see the messages above.
pause
exit /b 1
