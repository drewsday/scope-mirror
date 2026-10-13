@echo off
rem Launch the live oscilloscope mirror. Extra arguments are passed through
rem (e.g. run_scope_mirror.bat --selftest 30)
python "%~dp0scope_mirror.py" %*
if errorlevel 1 pause
