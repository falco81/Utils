@echo off
chcp 65001 >nul
echo Import EN audio stop do MKV souborů
echo =====================================
echo.

for %%f in (*.mkv) do (
    if exist "%%~nf.ac3" (
        echo Zpracovávám: %%f  +  %%~nf.ac3
        ffmpeg -i "%%f" -i "%%~nf.ac3" ^
            -map 1:a ^
            -map 0 ^
            -c copy ^
            -disposition:a none ^
            -disposition:a:0 default ^
            -metadata:s:a:0 language=eng ^
            -metadata:s:a:0 title=English ^
            "%%~nf_merged.mkv"
        echo Hotovo: %%~nf_merged.mkv
        echo.
    ) else (
        echo PŘESKAKUJI - nenalezen: %%~nf.ac3
        echo.
    )
)

echo =====================================
echo Hotovo!
pause