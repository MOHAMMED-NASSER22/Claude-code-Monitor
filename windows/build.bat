@echo off
REM Build TokenMaxxing.exe
REM Run this from the windows\ directory.
REM Requirements: pip install PyQt6 pyinstaller

pip install --quiet PyQt6 pyinstaller

echo.
echo Generating app icon (spark.ico) ...
python make_icon.py

echo.
echo Building TokenMaxxing.exe ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name TokenMaxxing ^
    --icon spark.ico ^
    --add-data "spark.ico;." ^
    --clean ^
    claude_monitor_overlay.py

echo.
if exist dist\TokenMaxxing.exe (
    echo Done!  dist\TokenMaxxing.exe is ready.
    echo.
    echo Launch:  dist\TokenMaxxing.exe
    echo First run: use the built-in sign-in flow ^(no mint_token.sh required^).
) else (
    echo Build failed - check the output above.
    exit /b 1
)
