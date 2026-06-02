@echo off
chcp 65001 >nul
echo Extrakce EN audio stop ze všech MKV
echo =====================================
echo.

for %%f in (*.mkv) do (
    echo Zpracovávám: %%f
    ffmpeg -i "%%f" -map 0:a:m:language:eng -c:a copy "%%~nf_EN.ac3"
    echo Hotovo: %%~nf_EN.ac3
    echo.
)

echo =====================================
echo Hotovo!
pause