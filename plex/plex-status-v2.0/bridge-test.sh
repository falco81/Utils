#!/bin/bash
# bridge-test.sh — ověří hypotézu: probudí přístup na jeden disk
#                  i ostatní disky na stejném USB mostu?
#
#   bash bridge-test.sh            # jen ukáže topologii (nic nebudí)
#   bash bridge-test.sh --test     # provede experiment (disky probudí)
#
# Experiment trvá ~50 minut, protože mezi měřeními musí disky znovu usnout.
# Pusť ho, když se nic nepřehrává.

TEST=0
[ "$1" = "--test" ] && TEST=1
IDLE_WAIT=${IDLE_WAIT:-1500}      # jak dlouho čekat na usnutí (s), default 25 min

# ---------- topologie ----------
echo "=================================================="
echo " Topologie: který disk visí na kterém USB mostu"
echo "=================================================="
declare -A BRIDGE
mapfile -t ROWS < <(
for d in /sys/block/sd*; do
    n=$(basename "$d")
    [ -e "$d" ] || continue          # glob se nerozvinul = žádné sd disky
    path=$(readlink -f "$d")
    # /sys/devices/.../usbX/A-B/A-B:1.0/host.../block/sdN  -> most je "A-B"
    br=$(echo "$path" | grep -oE '/usb[0-9]+/[0-9]+-[0-9.]+/' | head -1 | tr -d '/' )
    [ -z "$br" ] && br="(ne-USB)"
    mp=$(lsblk -no MOUNTPOINT "/dev/$n" 2>/dev/null | grep -v '^$' | head -1)
    echo "$n|$br|${mp:-—}"
done
)
if [ ${#ROWS[@]} -eq 0 ]; then
    echo "  Žádné sd* disky — na tomto stroji není co testovat."
    exit 1
fi
for row in "${ROWS[@]}"; do
    IFS='|' read -r dev br mp <<< "$row"
    BRIDGE[$dev]=$br
    printf "  %-6s most=%-14s mount=%s\n" "$dev" "$br" "$mp"
done
echo
# seskup podle mostu
declare -A GROUP
for row in "${ROWS[@]}"; do
    IFS='|' read -r dev br mp <<< "$row"
    GROUP[$br]="${GROUP[$br]} $dev"
done
echo "  Skupiny:"
for br in "${!GROUP[@]}"; do
    printf "    %-14s :%s\n" "$br" "${GROUP[$br]}"
done
echo

if [ "$TEST" -eq 0 ]; then
    echo "  Pro ověření hypotézy spusť:  bash bridge-test.sh --test"
    echo "  (trvá ~50 min, disky probudí)"
    exit 0
fi

# ---------- nástroje ----------
probe_ms() {   # časované O_DIRECT čtení; ms, nebo -1 při chybě
    python3 - "$1" <<'PY'
import os, sys, mmap, time
try:
    fd = os.open("/dev/" + sys.argv[1], os.O_RDONLY | os.O_DIRECT)
except OSError:
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
io_sum() { awk '{print $1+$5}' "/sys/block/$1/stat" 2>/dev/null; }

wait_idle() {   # počká, až se čítače všech disků zastaví na $1 sekund
    local want=$1
    echo "  … čekám $((want/60)) min, až disky usnou (sleduju jen /sys, nebudím je)"
    declare -A last
    for row in "${ROWS[@]}"; do IFS='|' read -r d b m <<< "$row"; last[$d]=$(io_sum "$d"); done
    local quiet=0
    while [ "$quiet" -lt "$want" ]; do
        sleep 30
        local moved=0
        for row in "${ROWS[@]}"; do
            IFS='|' read -r d b m <<< "$row"
            cur=$(io_sum "$d")
            [ "$cur" != "${last[$d]}" ] && { moved=1; last[$d]=$cur; }
        done
        if [ "$moved" -eq 1 ]; then quiet=0; echo "    (někdo sáhl na disk, počítám znovu)"
        else quiet=$((quiet+30)); fi
    done
}

# vyber most se dvěma a více disky
TARGET_BR=""
for br in "${!GROUP[@]}"; do
    cnt=$(echo "${GROUP[$br]}" | wc -w)
    [ "$cnt" -ge 2 ] && [ "$br" != "(ne-USB)" ] && { TARGET_BR=$br; break; }
done
if [ -z "$TARGET_BR" ]; then
    echo "  Nenašel jsem most se dvěma a více disky — hypotézu nelze ověřit."
    exit 1
fi
read -r A B REST <<< "${GROUP[$TARGET_BR]}"
echo "=================================================="
echo " Experiment na mostu $TARGET_BR:  budím $A, měřím $B"
echo "=================================================="
echo

# ---------- KONTROLNÍ MĚŘENÍ: spí vůbec disk B sám o sobě? ----------
echo "KROK 1 — kontrola: usne $B, když se ho nikdo nedotkne?"
wait_idle "$IDLE_WAIT"
MS_B_ALONE=$(probe_ms "$B")
printf "  první čtení %s po klidu: %s ms\n" "$B" "$MS_B_ALONE"
if [ "$MS_B_ALONE" -gt 1500 ] 2>/dev/null; then
    echo "  → $B po klidu SPAL (kontrola v pořádku, test má smysl)"
else
    echo "  → $B po klidu NESPAL. Buď je spindown delší než $((IDLE_WAIT/60)) min,"
    echo "     nebo ho něco drží vzhůru. Zvyš IDLE_WAIT a zkus znovu:"
    echo "        IDLE_WAIT=2400 bash bridge-test.sh --test"
    exit 1
fi
echo

# ---------- OSTRÝ TEST: probudím A, hned měřím B ----------
echo "KROK 2 — ostrý test: nechám znovu usnout, probudím $A, hned změřím $B"
wait_idle "$IDLE_WAIT"
MS_A=$(probe_ms "$A")
MS_B_AFTER=$(probe_ms "$B")
printf "  probuzení %s: %s ms\n" "$A" "$MS_A"
printf "  hned poté %s: %s ms\n" "$B" "$MS_B_AFTER"
echo

echo "=================================================="
echo " Závěr"
echo "=================================================="
if [ "$MS_B_AFTER" -lt 1500 ] 2>/dev/null; then
    echo "  HYPOTÉZA POTVRZENA."
    echo "  $B odpověděl za ${MS_B_AFTER} ms, ačkoli sám po stejném klidu spal"
    echo "  (${MS_B_ALONE} ms). Probuzení $A tedy roztočilo i $B —"
    echo "  most $TARGET_BR budí všechny své disky najednou."
    echo
    echo "  Důsledky:"
    echo "   • stav 'Standby' u sourozenců na mostu může být nepravdivý"
    echo "   • přehrávání z jednoho disku roztočí celou skupinu"
    echo "   • čtení SMART ze sourozenců je pak zadarmo (stejně se točí)"
else
    echo "  HYPOTÉZA VYVRÁCENA."
    echo "  $B odpověděl za ${MS_B_AFTER} ms, tedy spal i poté, co se probudil $A."
    echo "  Most $TARGET_BR budí disky nezávisle."
fi
echo
echo "  Pošli celý tento výstup zpět."
