#!/bin/bash
# nvme-check.sh — vypíše VŠECHNA SMART data z NVMe a porovná je s tím,
#                 co si z nich plexmon vytáhl. Nic nebudí, NVMe nemá co roztáčet.
#
#     bash nvme-check.sh              # autodetekce zařízení
#     bash nvme-check.sh /dev/nvme0n1

DEV="${1:-}"
if [ -z "$DEV" ]; then
    DEV=$(lsblk -ndo PATH,TRAN 2>/dev/null | awk '$2=="nvme"{print $1; exit}')
fi
[ -z "$DEV" ] && { echo "Nenašel jsem NVMe zařízení. Zadej ho ručně: bash nvme-check.sh /dev/nvme0n1"; exit 1; }
echo "zařízení: $DEV"
echo

echo "=================================================="
echo " 1) VŠE, co o disku ví smartctl"
echo "=================================================="
smartctl -x "$DEV" 2>&1
echo

if command -v nvme >/dev/null 2>&1; then
    echo "=================================================="
    echo " 2) Syrový NVMe health log (nvme-cli)"
    echo "=================================================="
    nvme smart-log "$DEV" 2>&1
    echo
    echo "-- identifikace řadiče (zkráceně) --"
    nvme id-ctrl "$DEV" 2>&1 | grep -iE '^(mn|sn|fr|tnvmcap|unvmcap|wctemp|cctemp|mtfa)\s' || true
    echo
else
    echo "(nvme-cli není nainstalováno — 'dnf install nvme-cli' dá ještě podrobnější výpis)"
    echo
fi

echo "=================================================="
echo " 3) Co si z toho vzal plexmon"
echo "=================================================="
curl -s --max-time 5 http://127.0.0.1:9847/data.json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  démon neodpovídá — běží plex-status.service?'); sys.exit()
nv = [x for x in d.get('disks', []) if x.get('tran') == 'nvme']
if not nv:
    print('  démon nevidí žádný NVMe disk'); sys.exit()
x = nv[0]
show = [
    ('temp',            'teplota (°C)'),
    ('health',          'zdraví'),
    ('smart_ok',        'SMART OK'),
    ('poh',             'hodin provozu'),
    ('power_cycle',     'zapnutí'),
    ('unsafe_shutdown', 'nekorektní vypnutí'),
    ('err_log',         'záznamů v error logu'),
    ('nvme_used',       'opotřebení (%)'),
    ('nvme_spare',      'zbývající rezerva (%)'),
    ('nvme_media_err',  'media/integrity chyby'),
    ('nvme_written',    'zapsáno (data units)'),
    ('nvme_read',       'přečteno (data units)'),
    ('smart_type',      'režim čtení'),
    ('cache_age',       'stáří údajů (s)'),
    ('smart_stale',     'označeno jako staré'),
]
for k, label in show:
    v = x.get(k)
    mark = '  <-- CHYBÍ' if v is None and k not in ('cache_age',) else ''
    print(f'  {label:24} {str(v):20}{mark}')
attrs = x.get('all_attrs') or {}
print(f'\n  all_attrs ({len(attrs)}):')
for name, a in attrs.items():
    print(f'    {name:34} raw={a.get(\"raw\")}')
"
echo
echo "=================================================="
echo " Jak to číst"
echo "=================================================="
echo "  Projdi sekci 1 (a 2) a zkontroluj, že každý údaj, který tě zajímá,"
echo "  je i v sekci 3. Kde je 'CHYBÍ', tam se parsování netrefilo —"
echo "  pošli výstup a doplním to."
echo
echo "  Pozn.: 'Data Units' × 512 000 = bajty (1 unit = 1000 bloků po 512 B)."
