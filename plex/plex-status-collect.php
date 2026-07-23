<?php
/**
 * plex-status-collect.php
 * Runs AS ROOT via a systemd timer (never through the web server!).
 * Gathers SMART / temperatures / disk info + power state + live I/O activity
 * + system and Plex status, then atomically writes data.json.
 *
 * Power-aware: a disk in STANDBY/SLEEPING is NOT woken up for SMART.
 * Its last-known SMART values are reused from a small cache instead.
 *
 * The Plex token is NEVER written into the JSON.
 */

// ---- configuration -------------------------------------------------------
const OUT_FILE   = '/var/www/html/smart/data.json';
const CACHE_FILE = '/var/lib/plex-status/smart-cache.json'; // not web-served
const SMARTCTL   = '/usr/sbin/smartctl';
const HDPARM     = '/usr/sbin/hdparm';
const PLEX_URL   = 'http://127.0.0.1:32400';
const PLEX_PREFS = '/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml';

// Plex has TWO versions: the Media Server (from /identity, e.g. 1.43.x) and the
// bundled Web App (what Settings shows, e.g. 4.160.0). We try to auto-detect the
// Web App version; if that fails, set it explicitly here (leave '' to auto-detect).
const PLEX_WEB_VERSION = '';

// Temperature thresholds (°C).
// These Seagate drives raise their own alarm at 60 °C: SMART attribute 190
// (Airflow_Temperature_Cel) carries THRESH 040, and Seagate normalises that
// attribute as 100 − temperature. Keeping CRIT under 60 means the dashboard
// turns red *before* the drive itself starts complaining, not after.
// WARN sits above the 43–45 °C these disks reach under load, so it flags a real
// cooling problem without crying wolf during normal work.
const TEMP_WARN  = 50;   // °C — worth a look
const TEMP_CRIT  = 58;   // °C — act now; just under the drives' own 60 °C alarm
const IO_SAMPLE  = 1.0;  // seconds to sample disk I/O activity

// If false: a disk in standby/sleeping is NOT woken for SMART (last-known cache is
// shown instead) — this preserves spindown. Set true to always read SMART, which
// gives complete data at the cost of spinning idle disks back up on every poll.
const WAKE_STANDBY = false;

// POWER DETECTION
// Many USB-SATA bridges (JMicron JMS567 among them) do not pass the ATA
// "CHECK POWER MODE" command through, so `hdparm -C` answers "standby" for
// every disk no matter what it is really doing. When that happens the state is
// inferred from the kernel's I/O counters instead, which are read from memory
// and never touch — or wake — the disk.
//
// A disk that has served no I/O for this long is assumed to have spun down.
// Set it to roughly your enclosure's / drive's own spindown timer.
const SPINDOWN_AFTER_S = 900;   // 15 minutes

// SMART is only read when the disk is KNOWN to be awake, so a normal run never
// spins anything up. Set this true to also refresh SMART for disks that served
// I/O since the previous run — they are almost certainly still spinning, but
// "almost": with it false, only --wake can ever cause a spin-up.
const SMART_WHEN_RECENT_IO = false;

// ---- CLI flags (for manual debugging) ------------------------------------
// Usage: php plex-status-collect.php [--debug] [--dry-run]
//   --debug/-v    verbose per-disk diagnostics printed to the console
//   --dry-run/-n  do everything but DON'T write data.json / cache; print JSON
//                 (implies --debug)
$DEBUG = false;
$DRYRUN = false;
$WAKE = WAKE_STANDBY;   // runtime; --wake overrides the config default for one run
if (PHP_SAPI === 'cli') {
    foreach (array_slice($argv ?? [], 1) as $a) {
        switch ($a) {
            case '-v': case '--debug': case '--verbose': $DEBUG = true; break;
            case '-n': case '--dry-run': $DRYRUN = true; $DEBUG = true; break;
            case '-w': case '--wake': $WAKE = true; break;
            case '-h': case '--help':
                fwrite(STDOUT,
                    "Usage: php " . basename(__FILE__) . " [--debug] [--dry-run] [--wake]\n" .
                    "  --debug,   -v   verbose per-disk diagnostics to the console\n" .
                    "  --dry-run, -n   don't write data.json / cache, print JSON (implies --debug)\n" .
                    "  --wake,    -w   read SMART even from standby/sleeping disks (spins them up)\n");
                exit(0);
            default:
                fwrite(STDERR, "unknown option: $a  (try --help)\n");
                exit(2);
        }
    }
}
function dbg(string $msg): void {
    global $DEBUG;
    if ($DEBUG) fwrite(STDERR, $msg . "\n");
}

// ---- helpers -------------------------------------------------------------

/**
 * PHP falls back to UTC when date.timezone isn't set in php.ini, which makes
 * the timestamps printed here disagree with the server's own clock. Follow the
 * system zone instead.
 */
function use_system_timezone(): void {
    if (ini_get('date.timezone') && strcasecmp((string) ini_get('date.timezone'), 'UTC') !== 0) return;
    $tz = '';
    if (is_readable('/etc/timezone')) $tz = trim((string) @file_get_contents('/etc/timezone'));
    if ($tz === '' && is_link('/etc/localtime')) {
        $target = (string) @readlink('/etc/localtime');
        if (preg_match('#zoneinfo/(.+)$#', $target, $m)) $tz = $m[1];
    }
    if ($tz !== '' && in_array($tz, timezone_identifiers_list(), true)) date_default_timezone_set($tz);
}
use_system_timezone();

function sh(string $cmd): string {
    return (string) @shell_exec($cmd . ' 2>/dev/null');
}

/**
 * Read a SMART attribute's RAW_VALUE from `smartctl -a` output.
 * The attribute table columns are fixed:
 *   ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE...
 * so RAW_VALUE is column index 9. We take its leading integer — this handles
 * values with suffixes like "46 (Min/Max 29/56)" or "46115h+00m+00.000s"
 * (the old "last numeric token" approach wrongly grabbed the THRESH 0 or a
 * trailing 0 in those cases).
 */
function smart_attr(string $out, string $needle): ?int {
    foreach (explode("\n", $out) as $line) {
        if (preg_match('/^\s*\d+\s+\S/', $line) && stripos($line, $needle) !== false) {
            $cols = preg_split('/\s+/', trim($line));
            if (isset($cols[9]) && preg_match('/\d+/', $cols[9], $m)) return (int) $m[0];
        }
    }
    return null;
}

function parse_temp(string $out): ?int {
    // 0 °C means "no data" on many USB/SCSI bridges — reject it and anything implausible
    if (preg_match('/Temperature:\s*(\d+)\s*Celsius/i', $out, $m)) { $t = (int) $m[1]; if ($t > 0 && $t < 120) return $t; }
    if (preg_match('/Current Drive Temperature:\s*(\d+)/i', $out, $m)) { $t = (int) $m[1]; if ($t > 0 && $t < 120) return $t; }
    foreach (['Temperature_Celsius', 'Airflow_Temperature_Cel', 'Temperature_Internal'] as $a) {
        $v = smart_attr($out, $a);
        if ($v !== null && $v > 0 && $v < 120) return $v;
    }
    return null;
}

function parse_health(string $out): ?string {
    if (preg_match('/SMART overall-health self-assessment test result:\s*(\S+)/i', $out, $m)) return strtoupper($m[1]);
    if (preg_match('/SMART Health Status:\s*(\S+)/i', $out, $m)) return strtoupper($m[1]) === 'OK' ? 'PASSED' : strtoupper($m[1]);
    return null;
}

function parse_poh(string $out): ?int {
    $v = smart_attr($out, 'Power_On_Hours');
    if ($v !== null) return $v;
    if (preg_match('/Power On Hours:\s*([\d,]+)/i', $out, $m)) return (int) str_replace(',', '', $m[1]);
    if (preg_match('/number of hours powered up\s*=\s*([\d.]+)/i', $out, $m)) return (int) $m[1];
    return null;
}

/**
 * Read SMART trying several -d types and MERGE the best value of each field.
 * Some USB bridges answer one -d type with only a health status and another with
 * only a temperature; taking the best of each field gives the most complete result
 * (and avoids a bogus "temp=0" reading beating a real health status).
 * Returns: [temp, health, smart_ok, poh, realloc, pending, uncorrect, type].
 */
function smart_collect(string $devpath, string $tran): array {
    $types = $tran === 'nvme'
        ? ['nvme', 'auto']
        : ['sat', 'sat,12', 'sat,16', 'usbjmicron', 'auto', 'scsi'];
    $r = ['temp' => null, 'health' => null, 'poh' => null, 'realloc' => null, 'pending' => null, 'uncorrect' => null,
          'nvme_used' => null, 'nvme_spare' => null, 'nvme_media_err' => null,
          'real_model' => null, 'real_serial' => null];
    $used = [];
    foreach ($types as $t) {
        $out = sh(SMARTCTL . ' -a -d ' . escapeshellarg($t) . ' ' . escapeshellarg($devpath));
        if ($out === '') { dbg(sprintf("    smartctl -d %-9s : no output", $t)); continue; }
        $vals = [
            'temp'      => parse_temp($out),
            'health'    => parse_health($out),
            'poh'       => parse_poh($out),
            'realloc'   => smart_attr($out, 'Reallocated_Sector_Ct'),
            'pending'   => smart_attr($out, 'Current_Pending_Sector'),
            'uncorrect' => smart_attr($out, 'Offline_Uncorrectable'),
            // NVMe has its own health metrics instead of ATA attributes
            'nvme_used'      => preg_match('/Percentage Used:\s*(\d+)%/i', $out, $m) ? (int) $m[1] : null,
            'nvme_spare'     => preg_match('/Available Spare:\s*(\d+)%/i', $out, $m) ? (int) $m[1] : null,
            'nvme_media_err' => preg_match('/Media and Data Integrity Errors:\s*([\d,]+)/i', $out, $m) ? (int) str_replace(',', '', $m[1]) : null,
            // the DRIVE's own identity — USB bridges report the enclosure's instead
            'real_model'  => preg_match('/^(?:Device Model|Model Number):\s*(.+)$/mi', $out, $m) ? trim($m[1]) : null,
            'real_serial' => preg_match('/^Serial Number:\s*(.+)$/mi', $out, $m) ? trim($m[1]) : null,
        ];
        $gained = [];
        foreach ($vals as $k => $v) {
            if ($v !== null && $r[$k] === null) { $r[$k] = $v; $gained[] = $k; }
        }
        dbg(sprintf("    smartctl -d %-9s : health=%s temp=%s poh=%s%s", $t,
            $vals['health'] ?? '-', $vals['temp'] !== null ? $vals['temp'] : '-',
            $vals['poh'] !== null ? $vals['poh'] : '-', $gained ? '  (+' . implode(',', $gained) . ')' : ''));
        if ($gained) $used[] = $t;
        if ($r['health'] !== null && $r['temp'] !== null && $r['poh'] !== null) break; // got the essentials
    }
    $r['smart_ok'] = $r['health'] === null ? null : ($r['health'] === 'PASSED');
    $r['type'] = $used ? implode('+', $used) : 'n/a';
    dbg("    -> merged from: " . $r['type']);
    return $r;
}

/**
 * Power state via `hdparm -C` — this does NOT spin up a sleeping drive.
 * Returns one of: active | standby | sleeping | unknown.
 * NVMe has no ATA power modes here, so it is reported as active.
 */
/**
 * Raw power state from `hdparm -C`. Does NOT spin a sleeping drive up.
 * Returns: active | standby | sleeping | unknown.
 * NOTE: unreliable behind many USB bridges — see POWER DETECTION at the top.
 */
function power_state_raw(string $devpath, string $tran): string {
    if ($tran === 'nvme') return 'active';
    $out = sh(HDPARM . ' -C ' . escapeshellarg($devpath));
    dbg("    hdparm -C: " . trim(str_replace("\n", ' | ', $out) ?: '(no output)'));
    if (preg_match('/drive state is:\s*(.+)/i', $out, $m)) {
        $s = strtolower(trim($m[1]));
        if (str_contains($s, 'standby')) return 'standby';
        if (str_contains($s, 'sleep'))   return 'sleeping';
        if (str_contains($s, 'active') || str_contains($s, 'idle')) return 'active';
    }
    return 'unknown';
}

/** Short human duration, e.g. 45s / 12m / 3h 20m. */
function fmt_dur(int $s): string {
    if ($s < 60)   return "{$s}s";
    if ($s < 3600) return intdiv($s, 60) . "m";
    return intdiv($s, 3600) . "h " . intdiv($s % 3600, 60) . "m";
}

/** Reads kernel I/O counters for a block device (does NOT wake the disk). */
function diskstat(string $dev): array {
    $f = @file_get_contents("/sys/block/$dev/stat");
    if ($f === false) return ['r' => 0, 'w' => 0];
    $p = preg_split('/\s+/', trim($f));
    return ['r' => (int) ($p[2] ?? 0), 'w' => (int) ($p[6] ?? 0)]; // sectors read / written
}

/**
 * Find a stable identifier for a disk from /dev/disk/by-id.
 *
 * Needed because USB-SATA bridges report the ENCLOSURE's serial via lsblk, not
 * the drive's: with several bays behind one bridge every disk shows the same
 * serial, which would make them collide in the SMART cache. The by-id name is
 * unique per bay/LUN and — unlike smartctl — costs nothing and never spins a
 * sleeping disk up.
 */
function disk_by_id(string $dev): ?string {
    $dir = '/dev/disk/by-id';
    if (!is_dir($dir)) return null;
    $best = null;
    foreach (scandir($dir) ?: [] as $entry) {
        if ($entry === '.' || $entry === '..') continue;
        $link = @readlink("$dir/$entry");
        if ($link === false) continue;
        if (basename($link) !== $dev) continue;
        // prefer a wwn-* name (tied to the drive) over a bus-derived one
        if (str_starts_with($entry, 'wwn-')) return $entry;
        if ($best === null) $best = $entry;
    }
    return $best;
}

/**
 * Detect the bundled Plex Web App version (e.g. 4.160.0), which is separate from
 * the Media Server version. Tries the served app first, then bundled files on disk.
 * Returns null if it can't be found (then set PLEX_WEB_VERSION explicitly).
 */
function plex_web_version($ctx): ?string {
    // 1) served web app HTML — assets are named like ...-plex-4.160.0-<hash>.js
    foreach (['/web/index.html', '/web/', '/web/index.htm'] as $p) {
        $html = @file_get_contents(PLEX_URL . $p, false, $ctx);
        if (!$html) continue;
        if (preg_match('/-plex-(\d+\.\d+\.\d+)-[0-9a-f]+\.(?:js|css)/i', $html, $m)) {
            dbg("    web version from $p (asset name): $m[1]");
            return $m[1];
        }
        if (preg_match('/"?version"?\s*[:=]\s*["\']?(4\.\d+\.\d+)/i', $html, $m)) {
            dbg("    web version from $p: $m[1]");
            return $m[1];
        }
    }
    dbg("    web version: not detected (set PLEX_WEB_VERSION to override)");
    return null;
}

// ---- disk enumeration ----------------------------------------------------
dbg("=== plex-status collector " . ($DRYRUN ? "(dry-run)" : "(debug)") . " @ " . date('Y-m-d H:i:s') . " ===");
$lsblk = json_decode(sh(
    'lsblk -bJ -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,TYPE,MOUNTPOINT,FSSIZE,FSUSED,"FSUSE%"'
), true);

$devs = [];
foreach (($lsblk['blockdevices'] ?? []) as $dev) {
    if (($dev['type'] ?? '') === 'disk') $devs[] = $dev;
}
dbg("found " . count($devs) . " disks: " . implode(', ', array_column($devs, 'name')));

// I/O activity: snapshot -> sleep -> snapshot (single window for all disks)
$io1 = [];
foreach ($devs as $dev) $io1[$dev['name']] = diskstat($dev['name']);
usleep((int) (IO_SAMPLE * 1_000_000));
$io2 = [];
foreach ($devs as $dev) $io2[$dev['name']] = diskstat($dev['name']);

// SMART cache (keyed by serial, else dev)
@mkdir(dirname(CACHE_FILE), 0755, true);
$cache = json_decode((string) @file_get_contents(CACHE_FILE), true) ?: [];

$disks = [];
foreach ($devs as $dev) {
    $path = $dev['path'] ?? ('/dev/' . $dev['name']);
    $name = $dev['name'];
    $tran = $dev['tran'] ?? '';
    // Cache key must be unique per physical drive. lsblk's serial comes from the
    // USB bridge, so several bays behind one bridge share it — use by-id instead.
    $byid = disk_by_id($name);
    $key  = $byid ?: (trim((string) ($dev['serial'] ?? '')) ?: $name);
    dbg("\n[$name] $path  tran=$tran  model=" . trim((string) ($dev['model'] ?? '')) . "  key=$key");

    // Pick the "main" filesystem for this disk.
    // Preference: root "/" > largest mounted filesystem > first partition (for size only).
    // This ignores /boot, /boot/efi and swap on the system NVMe.
    $mount = $dev['mountpoint'] ?? null;
    $fsuse = $dev['fsuse%'] ?? null;
    $fssize = $dev['fssize'] ?? null;
    $fsused = $dev['fsused'] ?? null;
    if ($mount === null && !empty($dev['children'])) {
        // flatten partitions + any LVM/LUKS grandchildren
        $cands = [];
        foreach ($dev['children'] as $ch) {
            $cands[] = $ch;
            foreach (($ch['children'] ?? []) as $gc) $cands[] = $gc;
        }
        $best = null;
        foreach ($cands as $c) {
            if (empty($c['mountpoint'])) continue;
            if ($c['mountpoint'] === '/') { $best = $c; break; }               // root always wins
            if ($best === null || (int) ($c['fssize'] ?? 0) > (int) ($best['fssize'] ?? 0)) {
                $best = $c;                                                     // otherwise largest fs
            }
        }
        if ($best !== null) {
            $mount  = $best['mountpoint'];
            $fsuse  = $best['fsuse%'] ?? null;
            $fssize = $best['fssize'] ?? null;
            $fsused = $best['fsused'] ?? null;
        } else {
            // nothing mounted — show the largest partition's size only
            $fssize = $dev['children'][0]['fssize'] ?? null;
            $fsused = $dev['children'][0]['fsused'] ?? null;
        }
    }

    // ---- power state -----------------------------------------------------
    // Two separate questions, deliberately kept apart:
    //   $power        — what to show (may be inferred when the bridge lies)
    //   $awake_certain— may we touch SMART? Only when the disk is KNOWN awake,
    //                   so a normal run can never spin a disk up.
    dbg("    fs: mount=" . ($mount ?? '(none)') . " size=" . ($fssize ?? '-') . " used=" . ($fsused ?? '-') . " pct=" . ($fsuse ?? '-'));
    $hd = power_state_raw($path, $tran);

    // I/O during this run's short sample window
    $dr = ($io2[$name]['r'] ?? 0) - ($io1[$name]['r'] ?? 0);
    $dw = ($io2[$name]['w'] ?? 0) - ($io1[$name]['w'] ?? 0);
    $io = $dw > 0 ? 'write' : ($dr > 0 ? 'read' : 'idle');
    $live = ($dr > 0 || $dw > 0);

    // I/O since the previous collector run (absolute kernel counters)
    $prev     = $cache[$key] ?? [];
    $io_total = ($io2[$name]['r'] ?? 0) + ($io2[$name]['w'] ?? 0);
    $prev_tot = $prev['io_total'] ?? null;
    $recent_io = ($prev_tot !== null && $io_total !== $prev_tot);
    $io_ts = ($live || $recent_io || $prev_tot === null)
        ? time()                                   // activity now (or first sight)
        : (int) ($prev['io_ts'] ?? time());
    $idle_for = time() - $io_ts;

    // Is hdparm telling the truth? If it claims standby while the disk is
    // demonstrably serving I/O, the bridge is not passing the command through.
    $hd_lies = in_array($hd, ['standby', 'sleeping'], true) && $live;
    $power_reliable = ($tran === 'nvme') || ($hd === 'active') || !$hd_lies;

    if ($tran === 'nvme') {
        $power = 'active';                 $psrc = 'nvme';
    } elseif ($hd === 'active') {
        $power = 'active';                 $psrc = 'hdparm';
    } elseif ($live || $recent_io) {
        $power = 'active';                 $psrc = 'inferred: I/O since last run';
    } elseif (in_array($hd, ['standby', 'sleeping'], true) && $idle_for < SPINDOWN_AFTER_S) {
        $power = 'active';                 $psrc = "inferred: idle only {$idle_for}s";
    } elseif (in_array($hd, ['standby', 'sleeping'], true)) {
        $power = $hd;                      $psrc = 'hdparm + no I/O for ' . fmt_dur($idle_for);
    } else {
        $power = 'unknown';                $psrc = 'not reported';
    }

    $awake_certain = ($tran === 'nvme') || ($hd === 'active') || $live
                     || (SMART_WHEN_RECENT_IO && $recent_io);

    dbg(sprintf("    power=%s (%s)  io=%s (Δr=%d Δw=%d /%.0fs, idle %s)  smart-safe=%s",
        $power, $psrc, $io, $dr, $dw, IO_SAMPLE, fmt_dur($idle_for),
        $awake_certain ? 'yes' : 'no'));
    if ($hd_lies) dbg("    NOTE: hdparm says '$hd' while the disk is serving I/O — "
                      . "this bridge does not report power state; using I/O inference");

    // SMART is read only when the disk is certainly awake, so a normal run
    // never spins one up. --wake (or WAKE_STANDBY) overrides that.
    $from_cache = false; $cache_age = null;
    $asleep = !$awake_certain && !$WAKE;
    if ($asleep && isset($cache[$key])) {
        $c = $cache[$key];
        $temp = $c['temp'] ?? null; $health = $c['health'] ?? null;
        $smart_ok = $c['smart_ok'] ?? null; $poh = $c['poh'] ?? null;
        $realloc = $c['realloc'] ?? null; $pending = $c['pending'] ?? null;
        $uncorr = $c['uncorrect'] ?? null; $stype = ($c['smart_type'] ?? 'cache');
        $nvme_used = $c['nvme_used'] ?? null; $nvme_spare = $c['nvme_spare'] ?? null; $nvme_media_err = $c['nvme_media_err'] ?? null;
        $real_model = $c['real_model'] ?? null; $real_serial = $c['real_serial'] ?? null;
        $from_cache = true; $cache_age = time() - (int) ($c['ts'] ?? time());
        dbg("    SMART skipped (disk asleep) -> using cache from {$cache_age}s ago");
    } elseif ($asleep) {
        // asleep (or unverifiable) and no cache yet — leave blank, don't wake it
        $temp = $health = $smart_ok = $poh = $realloc = $pending = $uncorr = null;
        $nvme_used = $nvme_spare = $nvme_media_err = null;
        $real_model = $real_serial = null;
        $stype = 'n/a';
        dbg("    SMART skipped (not verifiably awake) -> no cache yet, leaving blank");
    } else {
        $woke = !$awake_certain;           // only possible with --wake
        if ($woke) dbg("    waking disk -> reading SMART (spins it up)");
        $sm = smart_collect($path, $tran);
        $temp = $sm['temp']; $health = $sm['health']; $smart_ok = $sm['smart_ok'];
        $poh = $sm['poh']; $realloc = $sm['realloc']; $pending = $sm['pending']; $uncorr = $sm['uncorrect'];
        $nvme_used = $sm['nvme_used']; $nvme_spare = $sm['nvme_spare']; $nvme_media_err = $sm['nvme_media_err'];
        $real_model = $sm['real_model']; $real_serial = $sm['real_serial'];
        $stype = $sm['type'];
        if ($real_serial) dbg("    drive identity: {$real_model} / {$real_serial}");
        // we just spun it up — re-read power so the card doesn't still say "standby"
        if ($woke) {
            $hd2 = power_state_raw($path, $tran);
            $power = ($hd2 === 'active') ? 'active' : 'active';   // it is awake now by definition
            $psrc = 'after forced wake';
            dbg("    power after wake: $power (hdparm says '$hd2')");
        }
        // refresh cache
        $cache[$key] = [
            'temp' => $temp, 'health' => $health, 'smart_ok' => $smart_ok, 'poh' => $poh,
            'realloc' => $realloc, 'pending' => $pending, 'uncorrect' => $uncorr,
            'nvme_used' => $nvme_used, 'nvme_spare' => $nvme_spare, 'nvme_media_err' => $nvme_media_err,
            'real_model' => $real_model, 'real_serial' => $real_serial,
            'smart_type' => $stype, 'ts' => time(),
        ];
    }
    // I/O counters are tracked for every disk, awake or not — they drive the
    // power inference and cost nothing.
    $cache[$key] = ($cache[$key] ?? $prev);
    $cache[$key]['io_total'] = $io_total;
    $cache[$key]['io_ts']    = $io_ts;
    dbg(sprintf("    => temp=%s health=%s poh=%s realloc=%s pending=%s uncorrect=%s%s",
        $temp ?? '-', $health ?? '-', $poh ?? '-', $realloc ?? '-', $pending ?? '-', $uncorr ?? '-',
        $from_cache ? ' [cached]' : ''));

    $disks[] = [
        'dev'        => $name,
        'path'       => $path,
        // prefer what the DRIVE reports; USB bridges advertise generic values
        // like "USB3.0 DISK00" and one shared serial for every bay
        'model'      => $real_model ?: (trim((string) ($dev['model'] ?? '')) ?: '—'),
        'serial'     => $real_serial ?: (trim((string) ($dev['serial'] ?? '')) ?: '—'),
        'enclosure'  => trim((string) ($dev['model'] ?? '')) ?: null,
        'by_id'      => $byid,
        'size'       => $dev['size'] ? round($dev['size'] / 1e12, 2) . ' TB' : '—',
        'tran'       => $tran ?: '—',
        'power'      => $power,          // active | standby | sleeping | unknown
        'power_src'  => $psrc,           // how it was determined
        'power_reliable' => $power_reliable,
        'idle_for_s' => $idle_for,
        'io'         => $io,             // write | read | idle
        'temp'       => $temp,
        'health'     => $health,
        'smart_ok'   => $smart_ok,
        // health returned but no temp/attributes at all -> USB bridge only passes health
        'smart_limited' => $health !== null && $temp === null && $poh === null
                           && $realloc === null && $pending === null && $uncorr === null,
        'poh'        => $poh,
        'realloc'    => $realloc,
        'pending'    => $pending,
        'uncorrect'  => $uncorr,
        'nvme_used'      => $nvme_used,      // % of rated write endurance consumed
        'nvme_spare'     => $nvme_spare,     // % spare capacity remaining
        'nvme_media_err' => $nvme_media_err, // media & data integrity errors
        'smart_type' => $stype,
        'from_cache' => $from_cache,
        'cache_age'  => $cache_age,
        'mount'      => $mount,
        'fs_size'    => $fssize ? round($fssize / 1e12, 2) . ' TB' : null,
        'fs_used'    => $fsused ? round($fsused / 1e12, 2) . ' TB' : null,
        'fs_pct'     => $fsuse !== null ? (int) rtrim((string) $fsuse, '%') : null,
    ];
}

if (!$DRYRUN) {
    file_put_contents(CACHE_FILE, json_encode($cache), LOCK_EX);
} else {
    dbg("\nDRY-RUN: cache not written");
}

// ---- system --------------------------------------------------------------
$load = sys_getloadavg();
$mem  = [];
foreach (explode("\n", (string) @file_get_contents('/proc/meminfo')) as $l) {
    if (preg_match('/^(MemTotal|MemAvailable):\s+(\d+)/', $l, $m)) $mem[$m[1]] = (int) $m[2];
}
$mem_total = ($mem['MemTotal'] ?? 0) * 1024;
$mem_used  = $mem_total - ($mem['MemAvailable'] ?? 0) * 1024;

$cpu_temp = null;
foreach (glob('/sys/class/thermal/thermal_zone*/type') ?: [] as $tf) {
    $type = trim((string) @file_get_contents($tf));
    if (stripos($type, 'x86_pkg') !== false || stripos($type, 'cpu') !== false || $cpu_temp === null) {
        $raw = (int) @file_get_contents(dirname($tf) . '/temp');
        if ($raw > 0) $cpu_temp = round($raw / 1000);
        if (stripos($type, 'x86_pkg') !== false) break;
    }
}
$uptime_s = (int) explode(' ', (string) @file_get_contents('/proc/uptime'))[0];
dbg("\nsystem: load=" . implode('/', $load) . " mem_used=" . $mem_used . " cpu_temp=" . ($cpu_temp ?? '-') . " uptime=" . $uptime_s . "s");

// ---- Plex ----------------------------------------------------------------
$ctx = stream_context_create(['http' => ['timeout' => 3]]);
$plex = ['active' => null, 'version' => null, 'web_version' => null, 'sessions' => null];
$plex['active'] = trim(sh('systemctl is-active plexmediaserver')) === 'active';

$identity = @file_get_contents(PLEX_URL . '/identity', false, $ctx);
// NB: skip the XML declaration's version="1.0" — read MediaContainer's version
if ($identity && preg_match('/<MediaContainer\b[^>]*\bversion="([^"]+)"/', $identity, $m)) $plex['version'] = $m[1];

// Web App version: explicit override, else try the served app / bundled files.
$plex['web_version'] = PLEX_WEB_VERSION ?: plex_web_version($ctx);
dbg("plex: active=" . ($plex['active'] ? 'yes' : 'no')
    . " server=" . ($plex['version'] ?? '(identity unreachable)')
    . " web=" . ($plex['web_version'] ?? '(not detected)'));

$token = null; // used only for sessions, NEVER stored
if (is_readable(PLEX_PREFS)) {
    $prefs = (string) file_get_contents(PLEX_PREFS);
    if (preg_match('/PlexOnlineToken="([^"]+)"/', $prefs, $m)) $token = $m[1];
}
dbg("plex: token " . ($token ? 'found in Preferences.xml (not stored)' : 'NOT found — sessions unavailable'));
if ($token) {
    $sess = @file_get_contents(PLEX_URL . '/status/sessions?X-Plex-Token=' . urlencode($token), false, $ctx);
    if ($sess !== false && preg_match('/<MediaContainer[^>]*\bsize="(\d+)"/', $sess, $m)) {
        $plex['sessions'] = (int) $m[1];
    }
    dbg("plex: sessions=" . ($plex['sessions'] ?? '(query failed)'));
}

// ---- overall status ------------------------------------------------------
// Collect the concrete reasons so the page can explain the badge instead of
// just colouring it.
$reasons = [];
foreach ($disks as $d) {
    $label = $d['mount'] ?: $d['dev'];

    if ($d['smart_ok'] === false) {
        $reasons[] = ['crit', "$label — SMART health check FAILED (drive is reporting itself as failing)"];
    }
    if ((int) $d['realloc'] > 0) {
        $reasons[] = ['crit', "$label — {$d['realloc']} reallocated sector(s): the drive has remapped bad sectors"];
    }
    if ((int) $d['pending'] > 0) {
        $reasons[] = ['crit', "$label — {$d['pending']} pending sector(s) waiting to be remapped"];
    }
    if ((int) $d['uncorrect'] > 0) {
        $reasons[] = ['warn', "$label — {$d['uncorrect']} uncorrectable sector(s)"];
    }
    if ($d['temp'] !== null && $d['temp'] >= TEMP_CRIT) {
        $reasons[] = ['crit', "$label — {$d['temp']} °C, at or above the critical threshold of " . TEMP_CRIT . " °C"];
    } elseif ($d['temp'] !== null && $d['temp'] >= TEMP_WARN) {
        $reasons[] = ['warn', "$label — {$d['temp']} °C, at or above the warning threshold of " . TEMP_WARN . " °C"];
    }
    // Fullness only matters for the OS disk. Media drives are meant to run
    // nearly full, so a 94% data volume is not a fault.
    if ($d['mount'] === '/') {
        if ($d['fs_pct'] !== null && $d['fs_pct'] >= 95) {
            $reasons[] = ['crit', "$label — {$d['fs_pct']}% full ({$d['fs_used']} of {$d['fs_size']}), the system disk is almost out of space"];
        } elseif ($d['fs_pct'] !== null && $d['fs_pct'] >= 90) {
            $reasons[] = ['warn', "$label — {$d['fs_pct']}% full ({$d['fs_used']} of {$d['fs_size']}), the system disk is filling up"];
        }
    }
    if ($d['nvme_spare'] !== null && $d['nvme_spare'] < 20) {
        $reasons[] = ['crit', "$label — only {$d['nvme_spare']}% spare capacity left"];
    }
    if ((int) $d['nvme_media_err'] > 0) {
        $reasons[] = ['warn', "$label — {$d['nvme_media_err']} media/data integrity error(s)"];
    }
    if ($d['mount'] === null) {
        $reasons[] = ['warn', "{$d['dev']} — not mounted"];
    }
}
if (!$plex['active']) {
    $reasons[] = ['crit', 'Plex Media Server is not running'];
}

$overall = 'ok';
foreach ($reasons as $r) {
    if ($r[0] === 'crit') { $overall = 'crit'; break; }
    $overall = 'warn';
}

// ---- write ---------------------------------------------------------------
$payload = [
    'generated' => time(),
    'hostname'  => trim(sh('hostname')),
    'overall'   => $overall,
    'reasons'   => array_map(fn($r) => ['level' => $r[0], 'text' => $r[1]], $reasons),
    'thresholds'=> ['temp_warn' => TEMP_WARN, 'temp_crit' => TEMP_CRIT],
    'system'    => [
        'uptime_s'  => $uptime_s,
        'load'      => array_map(fn($x) => round($x, 2), $load),
        'ncpu'      => (int) trim(sh('nproc')),
        'mem_total' => $mem_total,
        'mem_used'  => $mem_used,
        'cpu_temp'  => $cpu_temp,
        'kernel'    => trim(sh('uname -r')),
    ],
    'plex'  => $plex,
    'disks' => $disks,
];

$json = json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
dbg("\noverall status: $overall" . ($reasons ? " — " . count($reasons) . " reason(s):" : " (no issues)"));
foreach ($reasons as $r) dbg("    [{$r[0]}] {$r[1]}");
if ($DRYRUN) {
    dbg("DRY-RUN: not writing " . OUT_FILE . " — JSON follows on stdout:\n");
    fwrite(STDOUT, $json . "\n");
} else {
    @mkdir(dirname(OUT_FILE), 0755, true);
    $tmp = OUT_FILE . '.tmp';
    file_put_contents($tmp, $json, LOCK_EX);
    chmod($tmp, 0644);
    rename($tmp, OUT_FILE);
    sh('restorecon -F ' . escapeshellarg(OUT_FILE)); // fix SELinux context
    dbg("wrote " . OUT_FILE . " (" . strlen($json) . " bytes)");
}

echo "OK " . count($disks) . " disks, status=$overall" . ($DRYRUN ? " [dry-run]" : "") . "\n";
