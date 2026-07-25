#!/bin/bash
# Diagnostika plakátů — spusť na serveru jako root:  bash diag-art.sh
echo "=================================================="
echo " plexmon — diagnostika plakátů u Now Playing"
echo "=================================================="

echo
echo "=== 1) co démon vrací pro now_playing ==="
curl -s http://127.0.0.1:9847/sessions | python3 -c "
import json,sys
d=json.load(sys.stdin)
ss=d.get('sessions',[])
if not ss:
    print('  ZADNA session — pust neco v Plexu a spust znovu'); sys.exit()
for s in ss:
    print('  title :', s.get('show') or s.get('title'))
    print('  art   :', repr(s.get('art')))
    print('  thumb :', repr(s.get('thumb')))
"

echo
echo "=== 2) art soubory ve webrootu ==="
ls -la /var/www/html/smart/art-*.jpg 2>/dev/null || echo "  ZADNE art-*.jpg soubory"

echo
echo "=== 3) muze demon (root) zapisovat do webrootu? ==="
touch /var/www/html/smart/.art-write-test 2>&1 && echo "  zapis OK" && rm -f /var/www/html/smart/.art-write-test || echo "  ZAPIS SELHAL"

echo
echo "=== 4) rucni stazeni plakatu pres Plex API ==="
PREFS="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml"
TOKEN=$(grep -oP 'PlexOnlineToken="\K[^"]+' "$PREFS" 2>/dev/null | head -1)
if [ -z "$TOKEN" ]; then echo "  TOKEN NENALEZEN v $PREFS"; exit 1; fi
echo "  token: nalezen (${#TOKEN} znaku)"

KEY=$(curl -s http://127.0.0.1:9847/sessions | python3 -c "import json,sys; d=json.load(sys.stdin); ss=d.get('sessions',[]); print(ss[0].get('art','') if ss else '')")
if [ -z "$KEY" ]; then echo "  session nevraci art klic — nema plakat?"; exit 0; fi
echo "  art klic: $KEY"

echo "  --- stahuji bez Accept hlavicky (jako demon) ---"
SIZE=$(curl -s "http://127.0.0.1:32400${KEY}?X-Plex-Token=${TOKEN}" -o /tmp/art-noaccept.bin -w '%{size_download} %{content_type}' 2>/dev/null)
echo "  stazeno: $SIZE"
file /tmp/art-noaccept.bin 2>/dev/null | sed 's/^/  /'
head -c4 /tmp/art-noaccept.bin | xxd 2>/dev/null | head -1 | sed 's/^/  prvni bajty: /'

echo "  --- stahuji S Accept: application/json (stara chyba) ---"
SIZE2=$(curl -s -H "Accept: application/json" "http://127.0.0.1:32400${KEY}?X-Plex-Token=${TOKEN}" -o /tmp/art-accept.bin -w '%{size_download} %{content_type}' 2>/dev/null)
echo "  stazeno: $SIZE2"
file /tmp/art-accept.bin 2>/dev/null | sed 's/^/  /'

echo
echo "=== 5) ocekavany nazev souboru a jestli existuje ==="
EXPECTED=$(python3 -c "import hashlib; print('art-'+hashlib.md5('$KEY'.encode()).hexdigest()[:16]+'.jpg')")
echo "  ocekavany: $EXPECTED"
if [ -f "/var/www/html/smart/$EXPECTED" ]; then
    ls -la "/var/www/html/smart/$EXPECTED" | sed 's/^/  /'
    file "/var/www/html/smart/$EXPECTED" | sed 's/^/  /'
else
    echo "  -> NEEXISTUJE (demon ho nevytvoril)"
fi

echo
echo "=== 6) test PHP proxy ?art= ==="
ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$KEY'))")
echo "  URL: http://127.0.0.1/smart/index.php?art=$ENC"
curl -s "http://127.0.0.1/smart/index.php?art=$ENC" -o /tmp/art-proxy.bin -w '  HTTP %{http_code}, %{size_download} B, %{content_type}\n' 2>/dev/null
file /tmp/art-proxy.bin 2>/dev/null | sed 's/^/  /'

echo
echo "=== 7) posledni logy demona (art/write) ==="
journalctl -u plex-status.service -n 40 --no-pager 2>/dev/null | grep -iE 'art|poster|write|plex /library' | tail -10 || echo "  (nic)"

echo
echo "=================================================="
echo " Posli cely tento vystup zpet."
echo "=================================================="
