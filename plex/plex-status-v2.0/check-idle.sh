#!/bin/bash
# check-idle.sh — sleduje, jestli na datové disky někdo sahá.
#
# Čte POUZE /sys/block/*/stat (paměť), takže sám disky nikdy neprobudí.
# Spusť na serveru a nech běžet aspoň 20 minut, ideálně když se nepřehrává:
#
#     bash check-idle.sh            # sleduje 20 minut
#     bash check-idle.sh 40         # sleduje 40 minut
#
# Když se čítače nehýbou, disky se mohou uspat a démon je nedrží vzhůru.

MINUTES="${1:-20}"
INTERVAL=10
CYCLES=$(( MINUTES * 60 / INTERVAL ))

# datové disky = ty, které démon sleduje (mimo NVMe a kořen)
mapfile -t DISKS < <(curl -s --max-time 5 http://127.0.0.1:9847/data.json 2>/dev/null \
  | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit()
for x in d.get('disks',[]):
    if x.get('tran')!='nvme' and x.get('mount') not in (None,'/'):
        print(x['dev'], x.get('mount',''))
" 2>/dev/null)

if [ ${#DISKS[@]} -eq 0 ]; then
    echo "Nepodařilo se získat seznam disků z démona — běží plex-status.service?"
    exit 1
fi

echo "=================================================="
echo " Sleduji ${#DISKS[@]} disků po dobu $MINUTES minut"
echo " (čtu jen /sys/block/*/stat — disky se tím nebudí)"
echo "=================================================="
echo

declare -A PREV LASTMOVE
now=$(date +%s)
for entry in "${DISKS[@]}"; do
    dev=${entry%% *}
    PREV[$dev]=$(awk '{print $1+$5}' "/sys/block/$dev/stat" 2>/dev/null)
    LASTMOVE[$dev]=$now
done

printf "%-8s %-10s %s\n" "DISK" "MOUNT" "aktivita"
echo "--------------------------------------------------"

for ((i=1; i<=CYCLES; i++)); do
    sleep $INTERVAL
    now=$(date +%s)
    line=""
    for entry in "${DISKS[@]}"; do
        dev=${entry%% *}
        cur=$(awk '{print $1+$5}' "/sys/block/$dev/stat" 2>/dev/null)
        if [ "$cur" != "${PREV[$dev]}" ]; then
            LASTMOVE[$dev]=$now
            line="$line $dev:I/O"
            PREV[$dev]=$cur
        fi
    done
    # každou minutu vypiš přehled
    if [ $(( i % 6 )) -eq 0 ]; then
        printf "\n[%s] po %d min:\n" "$(date +%H:%M:%S)" $(( i * INTERVAL / 60 ))
        for entry in "${DISKS[@]}"; do
            dev=${entry%% *}; mp=${entry#* }
            idle=$(( now - LASTMOVE[$dev] ))
            if [ "$idle" -ge 900 ]; then
                state="klid ${idle}s — disk se měl uspat"
            else
                state="klid ${idle}s"
            fi
            printf "  %-8s %-10s %s\n" "$dev" "$mp" "$state"
        done
    fi
    [ -n "$line" ] && printf "  %s aktivita:%s\n" "$(date +%H:%M:%S)" "$line"
done

echo
echo "=================================================="
echo " Závěr"
echo "=================================================="
now=$(date +%s)
allquiet=1
for entry in "${DISKS[@]}"; do
    dev=${entry%% *}; mp=${entry#* }
    idle=$(( now - LASTMOVE[$dev] ))
    if [ "$idle" -ge 900 ]; then
        printf "  %-8s %-10s v klidu %d s → smí spát\n" "$dev" "$mp" "$idle"
    else
        printf "  %-8s %-10s poslední I/O před %d s → NĚCO na něj sahá\n" "$dev" "$mp" "$idle"
        allquiet=0
    fi
done
echo
if [ "$allquiet" -eq 1 ]; then
    echo "  Na disky nikdo nesahal — démon je nedrží vzhůru."
else
    echo "  Něco disky budí. Kdo to je, ukáže:"
    echo "     systemctl stop plex-status.service   # a pusť tento skript znovu"
    echo "     (když se I/O zastaví, je to démon; když ne, je to Plex nebo jiná služba)"
fi
