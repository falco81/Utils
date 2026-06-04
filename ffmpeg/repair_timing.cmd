ffmpeg -i "Our.Beloved.Summer.1-6.mkv" -c:v libx264 -profile:v high -level 4.1 -x264opts bluray-compat=1:vbv-maxrate=40000:vbv-bufsize=30000 -crf 18 -c:a copy -c:s copy -map 0 -map_chapters 0 -map_metadata 0 -map_metadata:s:a 0:s:a -map_metadata:s:s 0:s:s "Our.Beloved.Summer.1-6.out.mkv"
"C:\Program Files\MKVToolNix\mkvmerge.exe" -o "Our.Beloved.Summer.1-6.final.mkv" --no-audio --no-subtitles --no-chapters "Our.Beloved.Summer.1-6.out.mkv" --no-video "Our.Beloved.Summer.1-6.mkv"


ffmpeg -i "Our.Beloved.Summer.7-12.mkv" -c:v libx264 -profile:v high -level 4.1 -x264opts bluray-compat=1:vbv-maxrate=40000:vbv-bufsize=30000 -crf 18 -c:a copy -c:s copy -map 0 -map_chapters 0 -map_metadata 0 -map_metadata:s:a 0:s:a -map_metadata:s:s 0:s:s "Our.Beloved.Summer.7-12.out.mkv"
"C:\Program Files\MKVToolNix\mkvmerge.exe" -o "Our.Beloved.Summer.7-12.final.mkv" --no-audio --no-subtitles --no-chapters "Our.Beloved.Summer.7-12.out.mkv" --no-video "Our.Beloved.Summer.7-12.mkv"


ffmpeg -i "Our.Beloved.Summer.13-16.mkv" -c:v libx264 -profile:v high -level 4.1 -x264opts bluray-compat=1:vbv-maxrate=40000:vbv-bufsize=30000 -crf 18 -c:a copy -c:s copy -map 0 -map_chapters 0 -map_metadata 0 -map_metadata:s:a 0:s:a -map_metadata:s:s 0:s:s "Our.Beloved.Summer.13-16.out.mkv"
"C:\Program Files\MKVToolNix\mkvmerge.exe" -o "Our.Beloved.Summer.13-16.final.mkv" --no-audio --no-subtitles --no-chapters "Our.Beloved.Summer.13-16.out.mkv" --no-video "Our.Beloved.Summer.13-16.mkv"