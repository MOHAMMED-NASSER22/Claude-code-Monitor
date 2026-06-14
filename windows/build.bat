@echo off
REM Build ClaudeMonitor.exe
REM Run this from the windows\ directory.
REM Requirements: pip install PyQt6 pyinstaller

pip install --quiet PyQt6 pyinstaller

echo.
echo Building ClaudeMonitor.exe ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name ClaudeMonitor ^
    --clean ^
    claude_monitor_overlay.py

echo.
if exist dist\ClaudeMonitor.exe (
    echo Done!  dist\ClaudeMonitor.exe is ready.
    echo.
    echo Launch:  dist\ClaudeMonitor.exe
    echo First run: use the built-in sign-in flow ^(no mint_token.sh required^).
) else (
    echo Build failed - check the output above.
    exit /b 1
)
