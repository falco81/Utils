@echo off
chcp 65001 >nul
echo Konverze AAC -> AC3 pro všechny MKV v adresáøi
echo ================================================
echo.

for %%f in (*.mkv) do (
    echo Zpracovávám: %%f
    ffmpeg -i "%%f" -map 0 -c:v copy -c:a ac3 -b:a 640k -c:s copy "%%~nf_ac3.mkv"
    echo Hotovo: %%~nf_ac3.mkv
    echo.
)

echo ================================================
echo Všechny soubory dokonèeny!
pause