#!/bin/bash
# spindown-info.sh — zjistí, po jaké době se disky reálně uspí.
#
#   bash spindown-info.sh            # jen vypíše nastavení (nic nebudí)
#   bash spindown-info.sh --measure  # ZMĚŘÍ skutečnou dobu (disky probudí)
#
# Bez --measure skript disky nebudí: čte jen konfiguraci a /sys.

MEASURE=0
[ "$1" = "--measure" ] && MEASURE=1

DISKS=()
while read -r dev mp; do
    [ -n "$dev" ] && DISKS+=("$dev $mp")
done < <(curl -s --max-time 5 http://127.0.0.1:9847/data.json 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for x in d.get('disks',[]):
    if x.get('tran')!='nvme' and x.get('mount') not in (None,'/'):
        print(x['dev'], x.get('mount',''))
" 2>/dev/null)

if [ ${#DISKS[@]} -eq 0 ]; then
    echo "Nezískal jsem disky z démona — běží plex-status.service?"; exit 1
fi

echo "=================================================="
echo " 1) Co si myslí plexmon"
echo "=================================================="
CFG=900
[ -f /etc/plex-status/config.json ] && CFG=$(python3 -c "
import json
try: print(json.load(open('/etc/plex-status/config.json')).get('spindown_after_s',900))
except Exception: print(900)")
echo "  spindown_after_s = ${CFG}s ($((CFG/60)) min)"
echo "  → po této době bez I/O hlásí web 'Standby'"
echo "  Pozor: je to POUZE předpoklad, démon se disku nikdy neptá."
echo

echo "=================================================="
echo " 2) Co říká systém a disky"
echo "=================================================="
echo "-- ATA standby timer (přes USB most často nefunguje) --"
for entry in "${DISKS[@]}"; do
    dev=${entry%% *}
    out=$(smartctl -g standby "/dev/$dev" 2>&1 | grep -iE 'standby|unavailable|not supported' | head -2)
    printf "  %-6s %s\n" "$dev" "${out:-bez odpovědi}"
done
echo
echo "-- APM úroveň (nižší než 128 = disk se smí parkovat) --"
for entry in "${DISKS[@]}"; do
    dev=${entry%% *}
    out=$(smartctl -g apm "/dev/$dev" 2>&1 | grep -iE 'APM|unavailable|not supported' | head -2)
    printf "  %-6s %s\n" "$dev" "${out:-bez odpovědi}"
done
echo
echo "-- kdo jiný by mohl spindown řídit --"
for svc in hd-idle hdparm udisks2; do
    st=$(systemctl is-enabled "$svc" 2>/dev/null)
    [ -n "$st" ] && [ "$st" != "not-found" ] && echo "  služba $svc: $st"
done
[ -f /etc/hdparm.conf ] && grep -qE '^\s*spindown_time' /etc/hdparm.conf 2>/dev/null \
    && echo "  /etc/hdparm.conf:" && grep -E '^\s*(/dev|spindown_time)' /etc/hdparm.conf | sed 's/^/    /'
echo "  (nic z výše uvedeného = spindown řídí sám USB most nebo firmware disku,"
echo "   a ten se zvenčí zjistit nedá — pak zbývá jen změřit, viz níže)"
echo

if [ "$MEASURE" -eq 0 ]; then
    echo "=================================================="
    echo " 3) Změření skutečné doby"
    echo "=================================================="
    echo "  Skutečnou hodnotu zjistíš jen tak, že necháš disky v klidu"
    echo "  a pak sáhneš — probuzený disk odpovídá do milisekund,"
    echo "  spící se musí roztočit (několik sekund)."
    echo
    echo "     bash spindown-info.sh --measure"
    echo
    echo "  Měření disky probudí a trvá až 25 minut. Pusť ho, když se"
    echo "  nic nepřehrává."
    exit 0
fi

echo "=================================================="
echo " 3) Měření (disky se probudí)"
echo "=================================================="
echo "  Postup: počkám na klid, pak v rostoucích odstupech změřím,"
echo "  jak dlouho trvá první čtení. Skok z ~ms na sekundy = disk spal."
echo

probe_ms() {   # timed O_DIRECT read; vrátí ms
    local dev=$1
    python3 - "$dev" <<'PY'
import os, sys, mmap, time
dev = "/dev/" + sys.argv[1]
try:
    fd = os.open(dev, os.O_RDONLY | os.O_DIRECT)
except OSError as e:
    print("-1"); sys.exit()
try:
    buf = mmap.mmap(-1, 4096)
    t0 = time.time()
    os.preadv(fd, [buf], 0)
    print(int((time.time() - t0) * 1000))
    buf.close()
finally:
    os.close(fd)
PY
}

for entry in "${DISKS[@]}"; do
    dev=${entry%% *}
    ms=$(probe_ms "$dev")
    printf "  %-6s referenční čtení (disk vzhůru): %s ms\n" "$dev" "$ms"
done
echo
echo "  Nyní čekám a zkouším po 5, 10, 15, 20 a 25 minutách klidu."
echo "  (každá zkouška disk probudí, takže hodinky běží od ní znovu)"
echo

for WAIT in 5 10 15 20 25; do
    echo "  … čekám $WAIT min bez dotyku"
    sleep $(( WAIT * 60 ))
    for entry in "${DISKS[@]}"; do
        dev=${entry%% *}
        ms=$(probe_ms "$dev")
        if [ "$ms" -gt 1500 ] 2>/dev/null; then
            printf "    %-6s po %2d min: %5s ms → SPAL (skutečný spindown <= %d min)\n" "$dev" "$WAIT" "$ms" "$WAIT"
        else
            printf "    %-6s po %2d min: %5s ms → byl vzhůru\n" "$dev" "$WAIT" "$ms"
        fi
    done
    echo
done

echo "=================================================="
echo " Co s výsledkem"
echo "=================================================="
echo "  Nejnižší čas, u kterého disk SPAL, je horní odhad skutečné doby."
echo "  Tu hodnotu (v sekundách) nastav démonovi, ať web hlásí pravdu:"
echo
echo "     mkdir -p /etc/plex-status"
echo "     echo '{\"spindown_after_s\": 600}' > /etc/plex-status/config.json"
echo "     systemctl restart plex-status.service"
echo
echo "  Démon si navíc sám všimne, když je hodnota moc velká: hlídá"
echo "  Start_Stop_Count a hlásí to v 'plexmon --check' i v logu."
