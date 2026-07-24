<?php
/**
 * index.php — Plex server status page.
 * Reads data.json produced by the collector (runs as root via a systemd timer).
 * Does nothing privileged itself: no shell, no SMART, no disk access.
 */
declare(strict_types=1);

/**
 * PHP falls back to UTC when date.timezone isn't set in php.ini, which makes
 * every timestamp here disagree with the server's own clock. Follow the system
 * zone instead, so the clock shown means what the shell would say.
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

const DATA_FILE = __DIR__ . '/data.json';
// ---- live activity -------------------------------------------------------
// Serving this from the page rather than the collector's snapshot makes the
// panel current instead of up to five minutes stale. It is safe to do on every
// poll: `ps` reads /proc, and the library lookup is a Plex API call — neither
// goes anywhere near a media disk.

/** sh() equivalent for the page: the collector defines its own. */
function sh(string $cmd): string {
    if (!function_exists('shell_exec')) return '';
    return (string) @shell_exec($cmd . ' 2>/dev/null');
}

/**
 * What is Plex actually doing right now?
 *
 * Works it out from the transcoder/scanner command lines, so a "Plex Transcoder"
 * burning a core is identifiable as preview generation for a specific file
 * rather than an anonymous CPU hog. Reads only /proc via ps — touches no disks.
 */
function classify_job(string $args): array {
    $media = null;
    if (preg_match('/\s-i\s+(.+?)(?=\s+-[a-zA-Z])/', $args, $m)) $media = trim($m[1]);

    if (str_contains($args, '/bif/') || str_contains($args, 'Indexes/tmp') || str_contains($args, 'img-%06d')) {
        return ['bif', 'Preview thumbnails', 'generating scrubbing previews for a recently added video', $media];
    }
    if (str_contains($args, 'thumb-%05d') || (str_contains($args, '-f image2') && str_contains($args, '-ss '))) {
        return ['chapter', 'Chapter thumbnails', 'generating chapter images for a recently added video', $media];
    }
    if (stripos($args, 'ebur128') !== false || stripos($args, 'loudness') !== false) {
        return ['loudness', 'Loudness analysis', 'analysing audio levels of a recently added track', $media];
    }
    if (str_contains($args, 'transcode/session')) {
        return ['playback', 'Playback transcode', 'someone is watching — this is a live stream', $media];
    }
    if (str_contains($args, '-f null') || str_contains($args, 'showinfo')) {
        return ['analysis', 'Media analysis', 'reading codecs and duration of a recently added file', $media];
    }
    return ['other', 'Transcoder', 'background transcoder task', $media];
}

/** Map library folders to library names so a job can say which library it belongs to. */
function library_roots(?string $token, $ctx = null): array {
    if (!$token) return [];
    if ($ctx === null) $ctx = stream_context_create(['http' => ['timeout' => 5]]);
    $raw = @file_get_contents(PLEX_URL . '/library/sections?X-Plex-Token=' . urlencode($token), false, $ctx);
    if ($raw === false) return [];
    $roots = [];
    if (preg_match_all('#<Directory\b.*?</Directory>#s', $raw, $blocks)) {
        foreach ($blocks[0] as $b) {
            if (!preg_match('/\btitle="([^"]*)"/', $b, $t)) continue;
            if (preg_match_all('/<Location\b[^>]*\bpath="([^"]*)"/', $b, $locs)) {
                foreach ($locs[1] as $p) $roots[$p] = $t[1];
            }
        }
    }
    return $roots;
}

function collect_activity(array $roots): array {
    $raw = sh("ps -eo etimes,pcpu,args --sort=-etimes");
    $jobs = [];
    foreach (explode("\n", $raw) as $line) {
        if (!str_contains($line, 'Plex Transcoder') && !str_contains($line, 'Plex Media Scanner')) continue;
        if (str_contains($line, 'grep')) continue;
        $parts = preg_split('/\s+/', trim($line), 3);
        if (count($parts) < 3) continue;
        [$etimes, $pcpu, $args] = $parts;

        if (str_contains($args, 'Plex Media Scanner')) {
            $kind = 'scan'; $label = 'Library scan';
            $why = 'looking for newly added files'; $media = null;
        } else {
            [$kind, $label, $why, $media] = classify_job($args);
        }

        $lib = null;
        if ($media !== null) {
            foreach ($roots as $path => $name) {
                if (str_starts_with($media, $path)) { $lib = $name; break; }
            }
        }
        $jobs[] = [
            'kind' => $kind, 'label' => $label, 'why' => $why,
            'file' => $media !== null ? basename($media) : null,
            'dir'  => $media !== null ? dirname($media) : null,
            'library' => $lib,
            'runtime_s' => (int) $etimes,
            'cpu' => (float) $pcpu,
        ];
    }
    return $jobs;
}

// ---- Plex API helpers ----------------------------------------------------
// Inlined deliberately: keeping this in a shared include would mean a third
// file that both scripts must have in place, and a missing one takes the page
// down with a fatal error. The trade-off is that the copy below and the one in
// the other script must be kept in step if the session format ever changes.

if (!defined('PLEX_URL')) define('PLEX_URL', 'http://127.0.0.1:32400');

/**
 * Where the page can find the Plex token.
 *
 * The collector reads the real one from Preferences.xml, which only root can
 * read. To let the (unprivileged) page query Plex directly, the collector can
 * mirror the token into this file with tight permissions — see
 * SESSION_TOKEN_FILE in the collector. Leave it absent and the page falls back
 * to the sessions.json snapshot instead.
 */
if (!defined('PLEX_TOKEN_FILE')) define('PLEX_TOKEN_FILE', '/etc/plex-status/token');

/** Token from Preferences.xml (root) or the mirrored file (web user). */
function plex_token(?string $prefs = null): ?string {
    if ($prefs !== null && is_readable($prefs)) {
        $x = (string) @file_get_contents($prefs);
        if (preg_match('/PlexOnlineToken="([^"]+)"/', $x, $m)) return $m[1];
    }
    if (is_readable(PLEX_TOKEN_FILE)) {
        $t = trim((string) @file_get_contents(PLEX_TOKEN_FILE));
        if ($t !== '') return $t;
    }
    return null;
}

/** GET a Plex endpoint and decode the JSON reply. */
function plex_json(string $path, ?string $token, float $timeout = 5.0): ?array {
    if (!$token) return null;
    $ctx = stream_context_create(['http' => [
        'timeout' => $timeout,
        'header'  => "Accept: application/json\r\n",
    ]]);
    $sep = str_contains($path, '?') ? '&' : '?';
    $raw = @file_get_contents(PLEX_URL . $path . $sep . 'X-Plex-Token=' . urlencode($token), false, $ctx);
    if ($raw === false) return null;
    $j = json_decode($raw, true);
    return is_array($j) ? $j : null;
}

/** Mount points of real filesystems, straight from /proc — costs nothing. */
function mount_list(): array {
    $out = [];
    foreach (explode("\n", (string) @file_get_contents('/proc/self/mounts')) as $line) {
        $f = preg_split('/\s+/', trim($line));
        if (count($f) < 3) continue;
        if (!in_array($f[2], ['ext4', 'xfs', 'btrfs', 'ext3', 'zfs'], true)) continue;
        $out[] = str_replace('\\040', ' ', $f[1]);
    }
    usort($out, fn($x, $y) => strlen($y) - strlen($x));   // longest prefix wins
    return $out;
}

/**
 * Everything worth showing about what is playing right now.
 *
 * Asks for JSON rather than Plex's default XML — the session structure is
 * deeply nested and JSON survives that far better than regex over XML would.
 */
function collect_sessions(?string $token, array $mounts = []): array {
    $j = plex_json('/status/sessions', $token);
    $items = $j['MediaContainer']['Metadata'] ?? [];
    if (!$items) return [];

    $out = [];
    foreach (array_values($items) as $m) {
        $media  = $m['Media'][0] ?? [];
        $part   = $media['Part'][0] ?? [];
        $player = $m['Player'] ?? [];
        $sess   = $m['Session'] ?? [];
        $user   = $m['User'] ?? [];

        // Stream decisions: 1 = video, 2 = audio, 3 = subtitles.
        // Plex only puts `decision` on the individual streams while a transcode
        // session exists; on a straight direct play it lives on the Part, and
        // with neither present nothing is being converted at all.
        $partDec = $part['decision'] ?? ($media['decision'] ?? null);
        $fallbackDec = $partDec ?? (isset($m['TranscodeSession']) ? null : 'directplay');
        $streams = ['1' => null, '2' => null, '3' => null];
        foreach (($part['Stream'] ?? []) as $s) {
            $t = (string) ($s['streamType'] ?? '');
            // array_key_exists, not isset: the slots start out null and isset()
            // reports null as "not set", which would discard every stream
            if (!array_key_exists($t, $streams) || $streams[$t] !== null) continue;
            if ($t !== '1' && empty($s['selected'])) continue;   // only the chosen track
            $streams[$t] = [
                'title'    => $s['displayTitle'] ?? ($s['extendedDisplayTitle'] ?? null),
                'decision' => $s['decision'] ?? $fallbackDec,
            ];
        }

        // which volume is this file on? tells you why a given disk is awake
        $file = $part['file'] ?? null;
        $vol = null;
        if ($file) {
            foreach ($mounts as $mp) {
                if ($mp !== '/' && str_starts_with($file, rtrim($mp, '/') . '/')) { $vol = $mp; break; }
            }
            if ($vol === null && str_starts_with($file, '/')) $vol = '/';
        }

        $out[] = [
            'type'        => $m['type'] ?? null,
            'title'       => $m['title'] ?? null,
            'show'        => $m['grandparentTitle'] ?? ($m['parentTitle'] ?? null),
            'season'      => isset($m['parentIndex']) ? (int) $m['parentIndex'] : null,
            'episode'     => isset($m['index']) ? (int) $m['index'] : null,
            'year'        => isset($m['year']) ? (int) $m['year'] : null,
            'duration_ms' => isset($m['duration']) ? (int) $m['duration'] : null,
            'offset_ms'   => isset($m['viewOffset']) ? (int) $m['viewOffset'] : 0,
            'state'       => $player['state'] ?? null,
            'user'        => $user['title'] ?? null,
            'product'     => $player['product'] ?? null,
            'player'      => $player['title'] ?? null,
            'address'     => $player['address'] ?? null,
            'local'       => !empty($player['local']),
            'bandwidth'   => isset($sess['bandwidth']) ? (int) $sess['bandwidth'] : null,
            'video'       => $streams['1']['title'] ?? null,
            'video_dec'   => $streams['1']['decision'] ?? null,
            'audio'       => $streams['2']['title'] ?? null,
            'audio_dec'   => $streams['2']['decision'] ?? null,
            'subs'        => $streams['3']['title'] ?? null,
            'subs_dec'    => $streams['3']['decision'] ?? null,
            'volume'      => $vol,
            'art'         => $m['grandparentThumb'] ?? ($m['parentThumb'] ?? ($m['thumb'] ?? null)),
        ];
    }
    return $out;
}


/**
 * Live now-playing, straight from Plex.
 *
 * Works whenever the collector has mirrored the token (SESSION_TOKEN_FILE);
 * otherwise it serves the snapshot the collector last wrote, so the panel keeps
 * working either way — just less often. Either path talks only to the Plex API
 * and the system SSD, never to a media disk.
 */
if (isset($_GET['live']) || isset($_GET['sessions'])) {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    $tok = plex_token();
    if ($tok) {
        $live = collect_sessions($tok, mount_list());
        // the collector caches posters next to data.json; point at one when it
        // exists so artwork survives even if the art proxy can't be used
        foreach ($live as &$ls) {
            if (empty($ls['art'])) continue;
            $cached = 'art-' . substr(md5($ls['art']), 0, 16) . '.jpg';
            if (is_file(__DIR__ . '/' . $cached)) $ls['thumb'] = $cached;
        }
        unset($ls);
        echo json_encode(['generated' => time(), 'live' => true, 'sessions' => $live,
                          'activity' => collect_activity(library_roots($tok))],
                         JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    } else {
        // Say why rather than silently serving stale data: without a readable
        // token the panel can only be as fresh as the collector's last run.
        $tf = PLEX_TOKEN_FILE;
        if (!file_exists($tf)) {
            $why = "token file $tf does not exist — run the collector once";
        } elseif (!is_readable($tf)) {
            // report the user PHP actually runs as; ext-posix may not be installed
            $me = function_exists('posix_geteuid') ? @posix_getpwuid(posix_geteuid()) : null;
            $who = is_array($me) ? $me['name'] : trim(sh('id -un'));
            $why = "token file $tf is not readable by " . ($who !== '' ? $who : 'the web user');
        } else {
            $why = 'token file is empty';
        }

        $snap = @json_decode((string) @file_get_contents(__DIR__ . '/sessions.json'), true);
        echo json_encode([
            'generated' => time(),
            'live'      => false,
            'why'       => $why,
            'sessions'  => $snap['sessions'] ?? [],
            // this part needs no token — it comes from ps
            'activity'  => collect_activity([]),
        ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    }
    exit;
}

// Poster art, fetched server-side. Keeping it behind this proxy is what stops
// the Plex token from ever reaching the browser.
if (isset($_GET['art'])) {
    $key = (string) $_GET['art'];
    // only Plex's own metadata paths, so this can't be pointed anywhere else
    if (!preg_match('#^/library/[A-Za-z0-9/_.\-]+$#', $key)) { http_response_code(400); exit; }
    $tok = plex_token();
    if (!$tok) { http_response_code(404); exit; }
    $ctx = stream_context_create(['http' => ['timeout' => 5]]);
    $img = @file_get_contents(PLEX_URL . $key . '?X-Plex-Token=' . urlencode($tok), false, $ctx);
    if ($img === false || strlen($img) < 100) { http_response_code(404); exit; }
    header('Content-Type: image/jpeg');
    header('Cache-Control: private, max-age=600');
    echo $img;
    exit;
}
/**
 * Waking the disks, without any privileges or helper services.
 *
 * Reading a file off a spun-down disk is what wakes it, so that is all this
 * does — and it only ever happens when the button is pressed, never on a normal
 * page load. The read must bypass the page cache (O_DIRECT), otherwise the
 * kernel would answer from memory and the disk would stay asleep.
 *
 * The probe file is created on each data volume by the collector during a
 * --wake run, when the disk is spinning anyway.
 */
const WAKE_PROBE = '.wake-probe';
// Waking the disks is unprivileged, but reading SMART afterwards is not. If the
// sudoers rule from INSTALL.md is in place, the page can also kick off a full
// collector pass so temperatures and attributes refresh straight away instead
// of on the next timer. Without the rule everything still works — the SMART
// values just arrive with the next scheduled run.
const COLLECTOR_BIN = '/usr/local/sbin/plex-status-collect.php';
const PHP_BIN       = '/usr/bin/php';

/** Data volumes to wake, taken from what the collector last saw. */
function wake_mounts(): array {
    $d = @json_decode((string) @file_get_contents(DATA_FILE), true);
    $out = [];
    foreach (($d['disks'] ?? []) as $x) {
        $m = $x['mount'] ?? null;
        if ($m === null || $m === '/' || strtolower($x['tran'] ?? '') === 'nvme') continue;
        if (is_file(rtrim($m, '/') . '/' . WAKE_PROBE)) $out[$m] = $x['dev'];
    }
    return $out;
}

if (isset($_GET['wake']) || isset($_GET['wakecheck'])) {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');

    if (!function_exists('shell_exec')) {
        http_response_code(503);
        echo json_encode(['ok' => false, 'error' => 'shell_exec is disabled in PHP']);
        exit;
    }
    $mounts = wake_mounts();
    if (!$mounts) {
        http_response_code(503);
        echo json_encode(['ok' => false,
            'error' => 'no probe files found — run the collector once with --wake to create them']);
        exit;
    }

    if (isset($_GET['wake'])) {
        // Fire the reads and return at once: a sleeping disk takes many seconds
        // to answer and we must not hold the request open that long.
        foreach ($mounts as $m => $dev) {
            $f = escapeshellarg(rtrim($m, '/') . '/' . WAKE_PROBE);
            @shell_exec("nohup dd if=$f of=/dev/null bs=4096 count=1 iflag=direct >/dev/null 2>&1 &");
        }
        // …and, if permitted, a full collector pass so SMART is re-read while
        // the disks are up. Ask sudo whether it is allowed *before* claiming so:
        // otherwise the page would wait two minutes for a refresh that was never
        // going to happen, instead of saying so immediately.
        $refresh = false;
        if (is_file(COLLECTOR_BIN)) {
            $rule = trim((string) @shell_exec(
                'sudo -n -l ' . escapeshellarg(PHP_BIN) . ' ' .
                escapeshellarg(COLLECTOR_BIN) . ' --wake 2>/dev/null'));
            if ($rule !== '') {
                @shell_exec('nohup sudo -n ' . escapeshellarg(PHP_BIN) . ' ' .
                            escapeshellarg(COLLECTOR_BIN) . ' --wake >/dev/null 2>&1 &');
                $refresh = true;
            }
        }
        echo json_encode(['ok' => true, 'at' => time(), 'disks' => array_values($mounts),
                          'refresh' => $refresh]);
        exit;
    }

    // wakecheck: a spinning disk answers in milliseconds, a sleeping one can't
    // answer within a second. Run them in parallel so the whole check is ~1 s.
    $cmd = '';
    foreach ($mounts as $m => $dev) {
        $f = escapeshellarg(rtrim($m, '/') . '/' . WAKE_PROBE);
        // device names are plain [a-z0-9]; sanitise rather than shell-quote so
        // the tag comes back out of echo without quotes around it
        $tag = preg_replace('/[^A-Za-z0-9_-]/', '', (string) $dev);
        $cmd .= "( if timeout 1 dd if=$f of=/dev/null bs=4096 count=1 iflag=direct >/dev/null 2>&1;"
              . " then echo '$tag awake'; else echo '$tag asleep'; fi ) & ";
    }
    $out = (string) @shell_exec($cmd . 'wait');
    $state = [];
    foreach (explode("\n", trim($out)) as $line) {
        $p = preg_split('/\s+/', trim($line));
        if (count($p) === 2) $state[$p[0]] = $p[1] === 'awake';
    }
    $awake = count(array_filter($state));
    echo json_encode(['ok' => true, 'disks' => $state,
                      'awake' => $awake, 'total' => count($mounts)]);
    exit;
}

$data = is_readable(DATA_FILE) ? json_decode((string) file_get_contents(DATA_FILE), true) : null;

function h($s): string { return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8'); }

function human_uptime(int $s): string {
    $d = intdiv($s, 86400); $s %= 86400;
    $hh = intdiv($s, 3600);  $s %= 3600;
    $mm = intdiv($s, 60);
    $out = [];
    if ($d)  $out[] = "{$d}d";
    if ($hh) $out[] = "{$hh}h";
    if (!$d) $out[] = "{$mm}m";
    return implode(' ', $out);
}

function bytes(int $b): string {
    $u = ['B', 'KB', 'MB', 'GB', 'TB']; $i = 0; $v = (float) $b;
    while ($v >= 1024 && $i < count($u) - 1) { $v /= 1024; $i++; }
    return round($v, $i >= 2 ? 1 : 0) . ' ' . $u[$i];
}

function temp_class(?int $t, array $thr): string {
    if ($t === null) return 'muted';
    if ($t >= ($thr['temp_crit'] ?? 58)) return 'crit';
    if ($t >= ($thr['temp_warn'] ?? 50)) return 'warn';
    return 'ok';
}

function lat_class(?float $ms): string {
    if ($ms === null) return '';
    if ($ms < 100) return 'good';
    if ($ms < 500) return 'slow';
    return 'bad';
}

/** Resolve power + I/O into one human state: [label, css-class, is_asleep]. */
function disk_state(array $d): array {
    $power = $d['power'] ?? 'unknown';
    $io    = $d['io'] ?? 'idle';
    if ($power === 'standby')  return ['Standby',  'standby', true];
    if ($power === 'sleeping') return ['Sleeping', 'sleep',   true];
    if ($io === 'write')       return ['Writing',  'write',   false];
    if ($io === 'read')        return ['Reading',  'read',    false];
    if ($power === 'active')   return ['Idle',     'idle',    false];
    return ['Active', 'unknown', false];
}

/** Plain-language explanation of a state — none of these are faults. */
function state_help(string $cls): array {
    return [
        'write'   => ['The disk was being written to when this was sampled.',
                      'Normal activity. The system disk shows this most of the time because Plex writes its database, logs and metadata continuously.'],
        'read'    => ['The disk was being read from when this was sampled.',
                      'Normal activity — playback, a library scan or thumbnail generation.'],
        'idle'    => ['Spinning and ready, with no I/O during the sample.',
                      'The healthy resting state for a disk that is in use.'],
        'standby' => ['Spun down to save power.',
                      'SMART values shown here are from the last time the disk was awake — it is never woken just to draw this page.'],
        'sleep'   => ['In a deeper sleep state than standby.',
                      'SMART values shown here are cached from when the disk was last awake.'],
        'unknown' => ['Power state could not be read from this device.',
                      'Some USB bridges do not report it. Harmless.'],
    ][$cls] ?? ['', ''];
}

const PALETTE = ['#58a6ff', '#3fb950', '#d29922', '#a371f7', '#f85149', '#2dd4bf', '#f0883e'];

// only used if data.json predates the thresholds field; the collector normally supplies them
$thr    = $data['thresholds'] ?? ['temp_warn' => 60, 'temp_crit' => 70];
$cpuThr = ['temp_warn' => 70, 'temp_crit' => 85];   // CPUs run hotter than disks
$overall = $data['overall'] ?? 'unknown';
$overallLabel = ['ok' => 'All healthy', 'warn' => 'Warning', 'crit' => 'Problem', 'unknown' => 'Unknown'][$overall] ?? 'Unknown';
$age = $data ? time() - ($data['generated'] ?? time()) : null;

if ($data && !empty($data['disks'])) {
    usort($data['disks'], function ($a, $b) {
        $am = $a['mount'] ?? null; $bm = $b['mount'] ?? null;
        if ($am === null && $bm === null) return strcmp($a['dev'], $b['dev']);
        if ($am === null) return 1;
        if ($bm === null) return -1;
        return strnatcmp($am, $bm);
    });
}

// Build the chart definitions here so the browser only has to draw them.
// With ?series=full the same code runs against the full-resolution history and
// the result is returned as JSON — that way panning uses every recorded sample
// without duplicating any of this logic in JavaScript.
$wantFull = isset($_GET['series']) && $_GET['series'] === 'full';
if ($wantFull) {
    $fullFile = __DIR__ . '/history-full.json';
    $full = is_readable($fullFile) ? json_decode((string) file_get_contents($fullFile), true) : null;
    if (is_array($full) && !empty($full['t'])) $data['history'] = $full;
}
$hist = $data['history'] ?? [];
$ht   = $hist['t'] ?? [];
$charts = [];
if (count($ht) >= 2) {
    $charts[] = [
        'id' => 'api', 'title' => 'API response time', 'unit' => ' ms', 'min' => 0,
        'note' => 'A steady climb is the early sign the database wants a REINDEX / VACUUM / ANALYZE pass.',
        'series' => [
            ['label' => '/hubs (home screen)', 'color' => '#58a6ff', 'data' => $hist['perf']['hb'] ?? []],
            ['label' => '/library/sections',   'color' => '#3fb950', 'data' => $hist['perf']['ls'] ?? []],
            ['label' => '/identity',           'color' => '#8b94a3', 'data' => $hist['perf']['id'] ?? []],
        ],
    ];

    $tempS = []; $capS = []; $cycS = []; $parkS = []; $wrS = []; $rdS = []; $errS = []; $ci = 0;
    foreach (($hist['disks'] ?? []) as $dh) {
        $col = PALETTE[$ci % count(PALETTE)];
        if (array_filter($dh['temp'], fn($v) => $v !== null)) {
            $tempS[] = ['label' => $dh['label'], 'color' => $col, 'data' => $dh['temp']];
        }
        $ssRate = rate_per_day($ht, $dh['ss'] ?? []);
        if (array_filter($ssRate, fn($v) => $v !== null)) {
            $cycS[] = ['label' => $dh['label'], 'color' => $col, 'data' => $ssRate];
        }
        $lcRate = rate_per_day($ht, $dh['lc'] ?? []);
        if (array_filter($lcRate, fn($v) => $v !== null)) {
            $parkS[] = ['label' => $dh['label'], 'color' => $col, 'data' => $lcRate];
        }
        // LBA counts are sector counts; scale to GB so the number means something
        $ub = $dh['unit_b'] ?? 512;
        $wr = rate_per_day($ht, $dh['lw'] ?? []);
        if (array_filter($wr, fn($v) => $v !== null)) {
            $wrS[] = ['label' => $dh['label'], 'color' => $col,
                      'data' => array_map(fn($v) => $v === null ? null : round($v * $ub / 1e9, 2), $wr)];
        }
        $rd = rate_per_day($ht, $dh['lr'] ?? []);
        if (array_filter($rd, fn($v) => $v !== null)) {
            $rdS[] = ['label' => $dh['label'], 'color' => $col,
                      'data' => array_map(fn($v) => $v === null ? null : round($v * $ub / 1e9, 2), $rd)];
        }
        // Error counters are charted only if something actually happened —
        // otherwise it is a flat zero line that tells you nothing.
        foreach ([['crc', 'CRC'], ['errs', 'sector errors']] as [$fld, $what]) {
            $vals = array_filter($dh[$fld] ?? [], fn($v) => $v !== null);
            if ($vals && max($vals) > 0) {
                $errS[] = ['label' => $dh['label'] . ' ' . $what, 'color' => $col, 'data' => $dh[$fld]];
            }
        }
        // Real units, not percent: 94% of an 8 TB disk and 94% of a 1 TB disk
        // are very different amounts of space, and only TB tells you how much
        // you can still put there.
        $used = $dh['used'] ?? [];
        if (array_filter($used, fn($v) => $v !== null)) {
            $capS[] = [
                'label'   => $dh['label'],
                'color'   => $col,
                'data'    => array_map(fn($v) => $v === null ? null : round($v / 1e12, 3), $used),
                'total_b' => $dh['total_b'] ?? null,
            ];
        }
        $ci++;
    }
    if ($tempS) {
        $charts[] = [
            'id' => 'temp', 'title' => 'Disk temperatures', 'unit' => ' °C',
            'note' => 'Gaps are periods a disk was asleep — it is never woken just to read a temperature.',
            'bands' => [
                ['at' => $thr['temp_warn'], 'color' => '#d29922', 'label' => 'warn ' . $thr['temp_warn'] . '°'],
                ['at' => $thr['temp_crit'], 'color' => '#f85149', 'label' => 'crit ' . $thr['temp_crit'] . '°'],
            ],
            'series' => $tempS,
        ];
    }
    if ($capS) {
        $charts[] = [
            'id' => 'cap', 'title' => 'Capacity used', 'unit' => ' TB', 'min' => 0,
            'series' => $capS,
        ];
    }
    if ($cycS) {
        $charts[] = [
            'id' => 'cyc', 'title' => 'Spin-ups per day', 'unit' => '/d', 'min' => 0,
            'note' => 'How hard the spin-down policy works the motors. Readings only land '
                    . 'while a disk is awake, so the line is sparse by design.',
            'series' => $cycS,
        ];
    }
    if ($wrS || $rdS) {
        $charts[] = [
            'id' => 'thr', 'title' => 'Data written per day', 'unit' => ' GB/d', 'min' => 0,
            'note' => 'How much each volume actually takes in — useful for spotting which '
                    . 'disk carries the write load.',
            'series' => $wrS ?: $rdS,
        ];
    }
    if ($rdS && $wrS) {
        $charts[] = [
            'id' => 'thrd', 'title' => 'Data read per day', 'unit' => ' GB/d', 'min' => 0,
            'series' => $rdS,
        ];
    }
    if ($parkS) {
        $charts[] = [
            'id' => 'park', 'title' => 'Head parks per day', 'unit' => '/d', 'min' => 0,
            'note' => 'Heads park far more often than the motor stops. Rated for 600,000 cycles.',
            'series' => $parkS,
        ];
    }
    if ($errS) {
        $charts[] = [
            'id' => 'err', 'title' => 'Error counters', 'unit' => '', 'min' => 0,
            'note' => 'Only drives with a non-zero counter appear here. Any step up is worth '
                    . 'investigating — CRC errors point at the cable or bridge, sector errors at the disk.',
            'series' => $errS,
        ];
    }
    if (array_filter($hist['sys']['cpu'] ?? [], fn($v) => $v !== null)) {
        $charts[] = [
            'id' => 'cpu', 'title' => 'CPU temperature', 'unit' => ' °C',
            'bands' => [['at' => $cpuThr['temp_warn'], 'color' => '#d29922', 'label' => 'warn ' . $cpuThr['temp_warn'] . '°']],
            'series' => [['label' => 'CPU', 'color' => '#f0883e', 'data' => $hist['sys']['cpu']]],
        ];
    }
    $charts[] = [
        'id' => 'load', 'title' => 'Load average', 'unit' => '', 'min' => 0,
        'note' => 'Above the core count means tasks are queuing for CPU.',
        'bands' => [['at' => (float) ($data['system']['ncpu'] ?: 1), 'color' => '#d29922',
                     'label' => $data['system']['ncpu'] . ' cores']],
        'series' => [['label' => '1 min', 'color' => '#2dd4bf', 'data' => $hist['sys']['load'] ?? []]],
    ];
    if (array_filter($hist['srv']['sess'] ?? [], fn($v) => $v !== null)) {
        $charts[] = [
            'id' => 'sess', 'title' => 'Active streams', 'unit' => '', 'min' => 0,
            'note' => 'Concurrent playbacks. Pair this with CPU temperature and load to see '
                    . 'what transcoding actually costs you.',
            'series' => [['label' => 'streams', 'color' => '#f0883e', 'data' => $hist['srv']['sess']]],
        ];
    }
    if (array_filter($hist['srv']['db'] ?? [], fn($v) => $v !== null)) {
        $dbS = [['label' => 'library.db', 'color' => '#58a6ff',
                 'data' => array_map(fn($v) => $v === null ? null : round($v / 1048576, 1), $hist['srv']['db'])]];
        if (array_filter($hist['srv']['wal'] ?? [], fn($v) => $v !== null)) {
            $dbS[] = ['label' => 'WAL', 'color' => '#d29922',
                      'data' => array_map(fn($v) => $v === null ? null : round($v / 1048576, 1), $hist['srv']['wal'])];
        }
        $charts[] = [
            'id' => 'db', 'title' => 'Database size', 'unit' => ' MB', 'min' => 0,
            'note' => 'A WAL that keeps growing instead of being checkpointed is worth a look.',
            'series' => $dbS,
        ];
    }
    $charts[] = [
        'id' => 'mem', 'title' => 'Memory used', 'unit' => ' %', 'min' => 0, 'max' => 100,
        'series' => [['label' => 'RAM', 'color' => '#a371f7', 'data' => $hist['sys']['mem'] ?? []]],
    ];
}

// JSON endpoint for the lazy full-resolution fetch.
if ($wantFull) {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode([
        't' => $ht,
        'charts' => array_map(fn($c) => [
            'id' => $c['id'], 'unit' => $c['unit'] ?? '', 'min' => $c['min'] ?? null,
            'max' => $c['max'] ?? null, 'bands' => $c['bands'] ?? [],
            'series' => array_map(fn($s) => ['label' => $s['label'], 'color' => $s['color'],
                                             'data' => $s['data']], $c['series']),
        ], $charts),
    ], JSON_UNESCAPED_SLASHES);
    exit;
}

/**
 * Turn a cumulative SMART counter into a per-day rate.
 *
 * Charting the raw counter would just draw a line that only ever goes up; what
 * actually matters is how fast it climbs, because that is what the spin-down
 * policy costs the drive. Readings are sparse (a sleeping disk isn't polled),
 * so each rate is derived from the gap to the previous real reading.
 */
function rate_per_day(array $times, array $counts): array {
    $out = array_fill(0, count($counts), null);
    $prev = null;
    foreach ($counts as $i => $v) {
        if ($v === null || !isset($times[$i])) continue;
        if ($prev !== null) {
            $dt = $times[$i] - $times[$prev];
            $dv = $v - $counts[$prev];
            // negative means the counter reset or the disk was swapped: skip it
            if ($dt >= 600 && $dv >= 0) $out[$i] = round($dv / $dt * 86400, 1);
        }
        $prev = $i;
    }
    return $out;
}

/** Least-squares slope per day; used to project when a disk fills up. */
function trend_per_day(array $times, array $vals): ?float {
    $pts = [];
    foreach ($vals as $i => $v) if ($v !== null && isset($times[$i])) $pts[] = [(float) $times[$i], (float) $v];
    if (count($pts) < 8) return null;
    if ($pts[count($pts) - 1][0] - $pts[0][0] < 6 * 3600) return null;
    $n = count($pts); $sx = $sy = $sxy = $sxx = 0.0;
    foreach ($pts as [$t, $v]) { $sx += $t; $sy += $v; $sxy += $t * $v; $sxx += $t * $t; }
    $den = $n * $sxx - $sx * $sx;
    if (abs($den) < 1e-9) return null;
    return (($n * $sxy - $sx * $sy) / $den) * 86400;
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Plex — server status<?= isset($data['hostname']) ? ' · ' . h($data['hostname']) : '' ?></title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c222b; --line:#262c37;
    --tx:#e6e9ef; --tx-dim:#8b94a3; --tx-mut:#5b6472;
    --ok:#3fb950; --warn:#d29922; --crit:#f85149; --info:#58a6ff; --write:#a371f7; --sleep:#6e7681;
    --ok-bg:rgba(63,185,80,.12); --warn-bg:rgba(210,153,34,.12);
    --crit-bg:rgba(248,81,73,.13); --info-bg:rgba(88,166,255,.12);
    --write-bg:rgba(163,113,247,.14); --sleep-bg:rgba(110,118,129,.14);
    --r:14px;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--tx);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;padding:26px max(16px,env(safe-area-inset-left)) 56px}
  .wrap{max-width:1240px;margin:0 auto}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
  header h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
  header .host{color:var(--tx-dim);font-weight:400}
  .spacer{flex:1}
  .updated{color:var(--tx-mut);font-size:13px;text-align:right;line-height:1.35}

  /* tabs */
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .tabs button{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
    color:var(--tx-dim);font:inherit;font-weight:600;font-size:14px;padding:9px 15px;
    cursor:pointer;border-radius:8px 8px 0 0;transition:color .15s,border-color .15s}
  .tabs button:hover{color:var(--tx)}
  .tabs button[aria-selected="true"]{color:var(--info);border-bottom-color:var(--info)}
  .panel[hidden]{display:none}

  h3.sec{font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
         color:var(--tx-mut);margin:24px 0 12px}

  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;
        font-weight:600;font-size:14px;border:1px solid transparent}
  .pill .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .pill.ok{background:var(--ok-bg);color:var(--ok);border-color:rgba(63,185,80,.3)}
  .pill.warn{background:var(--warn-bg);color:var(--warn);border-color:rgba(210,153,34,.3)}
  .pill.crit{background:var(--crit-bg);color:var(--crit);border-color:rgba(248,81,73,.3)}
  .pill.unknown{background:var(--panel2);color:var(--tx-dim)}
  .pill.ok .dot{background:var(--ok)} .pill.warn .dot{background:var(--warn)}
  .pill.crit .dot{background:var(--crit);animation:pulse 1.4s infinite} .pill.unknown .dot{background:var(--tx-mut)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.45}}

  .grid{display:grid;gap:14px}
  .top{grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}
  .perf{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
  .charts{grid-template-columns:repeat(auto-fit,minmax(520px,1fr))}
  .disks{grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
  .full{grid-column:1/-1}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:17px 19px}
  .card h2{margin:0 0 13px;font-size:12px;font-weight:700;letter-spacing:.7px;
           text-transform:uppercase;color:var(--tx-mut)}

  .kv{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;font-size:14px;gap:12px}
  .kv .k{color:var(--tx-dim)} .kv .v{font-weight:600;text-align:right}
  .kv+.kv{border-top:1px solid rgba(38,44,55,.6)}
  .big{font-size:29px;font-weight:700;line-height:1.1;margin:2px 0}
  .sub{color:var(--tx-mut);font-size:13px}
  .flag{color:var(--ok)} .flag.warn{color:var(--warn)} .flag.crit{color:var(--crit)} .flag.muted{color:var(--tx-mut)}

  /* activity */
  .job{display:flex;gap:11px;padding:11px 0;align-items:flex-start}
  .job+.job{border-top:1px solid rgba(38,44,55,.7)}
  .job .ico{width:9px;height:9px;border-radius:50%;margin-top:7px;flex:0 0 auto;animation:blink 1.6s infinite}
  .job.bif .ico,.job.chapter .ico{background:var(--write)}
  .job.playback .ico{background:var(--warn)}
  .job.scan .ico{background:var(--info)}
  .job.loudness .ico,.job.analysis .ico,.job.other .ico{background:var(--tx-dim)}
  .job .jb{min-width:0;flex:1}
  .job .jt{font-weight:650;font-size:14px}
  .job.playback .jt{color:var(--warn)}
  .job .jw{color:var(--tx-mut);font-size:12.5px}
  .job .jf{font-size:13px;margin-top:4px;word-break:break-word}
  .job .jm{color:var(--tx-mut);font-size:12px;white-space:nowrap;text-align:right}
  .idle-note{color:var(--ok);font-size:14px;font-weight:600}

  /* charts */
  .crangewrap{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .crange{display:flex;gap:6px;flex-wrap:wrap}
  .crange button:disabled{opacity:.38;cursor:not-allowed}
  .pannote{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--info);
    background:var(--info-bg);border:1px solid rgba(88,166,255,.3);border-radius:999px;padding:3px 6px 3px 12px}
  .pannote button{appearance:none;background:var(--info);color:#0d1117;border:0;border-radius:999px;
    padding:2px 10px;font-size:12px;font-weight:700;cursor:pointer}
  /* charts are draggable to scroll through history */
  .cwrap{cursor:grab}
  .cwrap.grabbing{cursor:grabbing}
  .cwrap svg{user-select:none;-webkit-user-select:none}
  .crange button{appearance:none;background:var(--panel);border:1px solid var(--line);color:var(--tx-dim);
    font:inherit;font-size:13px;font-weight:600;padding:6px 13px;border-radius:9px;cursor:pointer}
  .crange button[aria-pressed="true"]{background:var(--info-bg);border-color:rgba(88,166,255,.4);color:var(--info)}
  .cwrap{position:relative}
  .cwrap svg{display:block;width:100%;height:auto;touch-action:pan-y}
  .legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:12.5px;color:var(--tx-dim)}
  .legend .li{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
  .legend i{width:9px;height:9px;border-radius:2px;display:inline-block;flex:0 0 auto}
  .legend b{color:var(--tx);font-weight:600}
  .ctip{position:absolute;pointer-events:none;z-index:20;background:var(--panel2);
    border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:12.5px;
    box-shadow:0 8px 24px rgba(0,0,0,.5);opacity:0;transition:opacity .1s;white-space:nowrap}
  .ctip .ct{color:var(--tx-mut);margin-bottom:5px}
  .ctip .cr{display:flex;align-items:center;gap:7px;line-height:1.5}
  .ctip .cr i{width:8px;height:8px;border-radius:2px;flex:0 0 auto}
  .ctip .cr b{margin-left:auto;padding-left:12px}
  .nodata{color:var(--tx-mut);font-size:13px;padding:26px 0;text-align:center}

  .lat{display:flex;justify-content:space-between;align-items:baseline;padding:6px 0;gap:10px}
  .lat+.lat{border-top:1px solid rgba(38,44,55,.6)}
  .lat .ep{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;color:var(--tx-dim)}
  .lat .ms{font-weight:700;white-space:nowrap}
  .lat .ms.good{color:var(--ok)} .lat .ms.slow{color:var(--warn)} .lat .ms.bad{color:var(--crit)}

  .disk{transition:opacity .3s}
  .disk.asleep{opacity:.66}
  .disk .dhead{display:flex;align-items:center;gap:9px;margin-bottom:4px;flex-wrap:wrap}
  .disk .dname{font-size:17px;font-weight:700}
  .badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;letter-spacing:.4px;text-transform:uppercase}
  .badge.usb{background:var(--info-bg);color:var(--info)}
  .badge.nvme{background:rgba(163,113,247,.14);color:#a371f7}
  .badge.sata{background:var(--panel2);color:var(--tx-dim)}
  .disk .model{color:var(--tx-dim);font-size:12.5px;margin-bottom:13px;word-break:break-word}
  .disk .mnt{color:var(--info);font-weight:600}

  .state{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;
         font-size:13px;font-weight:700;letter-spacing:.3px;border:1px solid transparent}
  .state .sdot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .state.idle{background:var(--ok-bg);color:var(--ok);border-color:rgba(63,185,80,.3)}
  .state.idle .sdot{background:var(--ok)}
  .state.read{background:var(--info-bg);color:var(--info);border-color:rgba(88,166,255,.35)}
  .state.read .sdot{background:var(--info);animation:blink 1.6s infinite}
  .state.write{background:var(--write-bg);color:var(--write);border-color:rgba(163,113,247,.35)}
  .state.write .sdot{background:var(--write);animation:blink 1.2s infinite}
  .state.standby,.state.sleep{background:var(--sleep-bg);color:var(--sleep);border-color:rgba(110,118,129,.3)}
  .state.standby .sdot,.state.sleep .sdot{background:var(--sleep)}
  .state.unknown{background:var(--panel2);color:var(--tx-dim)}
  .state.unknown .sdot{background:var(--tx-mut)}

  .temp{display:flex;align-items:baseline;gap:8px;margin:13px 0;flex-wrap:wrap}
  .temp .t{font-size:33px;font-weight:800;line-height:1}
  .temp .t.ok{color:var(--ok)} .temp .t.warn{color:var(--warn)}
  .temp .t.crit{color:var(--crit)} .temp .t.muted{color:var(--tx-mut)}
  .temp .health{margin-left:auto}

  .bar{height:8px;border-radius:6px;background:var(--panel2);overflow:hidden;margin:6px 0 4px}
  .bar>i{display:block;height:100%;border-radius:6px;background:var(--info)}
  .bar.warn>i{background:var(--warn)} .bar.crit>i{background:var(--crit)}

  .attrs{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;margin-top:13px;
         padding-top:13px;border-top:1px solid var(--line);font-size:13px}
  .attrs .a{display:flex;justify-content:space-between;gap:8px}
  .attrs .a .an{color:var(--tx-mut)} .attrs .a .av{font-weight:600}
  .attrs .a .av.bad{color:var(--crit)}
  .cachenote{font-size:11px;color:var(--tx-mut)}

  /* now playing */
  .now{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
  .idlecard{color:var(--tx-mut);text-align:center;padding:22px}
  .livewarn{margin-left:10px;font-weight:400;text-transform:none;letter-spacing:0;
    color:var(--warn);font-size:11.5px}
  .np{display:flex;gap:14px;padding:14px}
  /* The frame simply takes the shape of the artwork: fixed width, height from
     the image itself. No aspect-ratio to crop against and no max-height to
     letterbox against — whatever Plex hands us is shown whole.
     align-self keeps flexbox from stretching the frame to the card's height,
     which would show its background as bands above and below the image. */
  .np .art{width:132px;flex:0 0 auto;align-self:flex-start;
    border-radius:8px;overflow:hidden;background:var(--panel2)}
  .np .art img{width:100%;height:auto;display:block}
  .np .art span{display:block;text-align:center;padding:56px 0;color:var(--tx-mut);font-size:24px}
  .np .meta{flex:1;min-width:0}
  .np .t1{font-weight:700;font-size:15px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .np .t2{color:var(--tx-dim);font-size:13px;margin-top:2px}
  .np .who{display:flex;align-items:center;gap:7px;margin-top:8px;font-size:12.5px;color:var(--tx-dim);flex-wrap:wrap}
  .np .st{display:inline-flex;align-items:center;gap:5px;font-weight:700;font-size:12px;
    padding:2px 8px;border-radius:999px}
  .np .st.playing{background:var(--ok-bg);color:var(--ok)}
  .np .st.paused{background:var(--sleep-bg);color:var(--sleep)}
  .np .st.buffering{background:var(--warn-bg);color:var(--warn)}
  .np .pbar{height:5px;border-radius:4px;background:var(--panel2);overflow:hidden;margin:9px 0 4px}
  .np .pbar i{display:block;height:100%;background:var(--info);border-radius:4px;transition:width 1s linear}
  .np .times{display:flex;justify-content:space-between;font-size:11.5px;color:var(--tx-mut)}
  .np .rows{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
    display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:12.5px}
  .np .rk{color:var(--tx-mut)}
  .np .rv{color:var(--tx)}
  .np .dec{font-size:11px;font-weight:700;padding:1px 6px;border-radius:5px;margin-left:6px}
  .np .dec.direct{background:var(--ok-bg);color:var(--ok)}
  .np .dec.transcode{background:var(--warn-bg);color:var(--warn)}
  .np .dec.copy{background:var(--info-bg);color:var(--info)}
  .np .vol{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--info)}

  /* wake control */
  .sechead{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .sechead .sec{margin-bottom:12px}
  .wakebox{margin:26px 0 12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  #wakebtn{appearance:none;background:var(--panel);border:1px solid var(--line);color:var(--tx-dim);
    padding:6px 14px;border-radius:9px;font-size:12.5px;font-weight:600;cursor:pointer}
  #wakebtn:hover:not(:disabled){color:var(--tx);border-color:var(--info)}
  #wakebtn:disabled{opacity:.5;cursor:default}
  .wakeprog{display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--tx-dim)}
  .wakeprog .wbar{width:140px;height:6px;border-radius:4px;background:var(--panel2);overflow:hidden}
  .wakeprog .wbar i{display:block;height:100%;width:0;background:var(--info);border-radius:4px;
    transition:width .4s linear}
  .wakeprog.done .wbar i{background:var(--ok)}
  .wakeprog.fail .wbar i{background:var(--crit)}
  .wakeprog.done .wtxt{color:var(--ok)}
  .wakeprog.fail .wtxt{color:var(--crit)}
  .recent{display:flex;gap:14px;flex-wrap:wrap;margin-top:7px;font-size:12.5px;color:var(--tx-mut)}
  .recent b{color:var(--tx);font-weight:700;font-size:13.5px}

  /* collapsible full attribute list */
  .moreattrs{margin-top:11px;border-top:1px solid var(--line);padding-top:10px}
  .moreattrs summary{cursor:pointer;list-style:none;font-size:12.5px;color:var(--tx-dim);
    display:flex;align-items:center;gap:8px;user-select:none;padding:2px 0}
  .moreattrs summary::-webkit-details-marker{display:none}
  .moreattrs summary::before{content:"▸";color:var(--tx-mut);transition:transform .15s;display:inline-block}
  .moreattrs[open] summary::before{transform:rotate(90deg)}
  .moreattrs summary:hover{color:var(--tx)}
  .moreattrs summary span{margin-left:auto;font-size:11px;color:var(--tx-mut);
    background:var(--panel2);border-radius:9px;padding:1px 7px}
  .mgrid{display:grid;grid-template-columns:1fr;gap:5px;margin-top:10px;font-size:13px}
  .mgrid .a{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
  .mgrid .a+.a{border-top:1px solid rgba(38,44,55,.5)}
  .mgrid .an{color:var(--tx-mut)} .mgrid .av{font-weight:600;text-align:right}
  .mgrid .av.bad{color:var(--crit)}

  .tip{position:relative;display:inline-flex}
  .tip .pill,.tip .state{cursor:help}
  .tip .why{position:absolute;top:calc(100% + 10px);left:0;z-index:60;
    min-width:290px;max-width:430px;padding:12px 14px;
    background:var(--panel2);border:1px solid var(--line);border-radius:11px;
    box-shadow:0 12px 34px rgba(0,0,0,.55);
    opacity:0;visibility:hidden;transform:translateY(-4px);
    transition:opacity .15s,transform .15s,visibility .15s;text-align:left;font-weight:400}
  .tip:hover .why,.tip:focus-within .why{opacity:1;visibility:visible;transform:translateY(0)}
  .tip .why::before{content:"";position:absolute;bottom:100%;left:18px;
    border:7px solid transparent;border-bottom-color:var(--line)}
  .why h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--tx-mut);font-weight:700}
  .why ul{margin:0;padding:0;list-style:none}
  .why li{display:flex;gap:8px;padding:4px 0;font-size:13px;line-height:1.45;color:var(--tx)}
  .why li+li{border-top:1px solid rgba(38,44,55,.6)}
  .why li .lv{flex:0 0 auto;width:7px;height:7px;border-radius:50%;margin-top:7px}
  .why li.crit .lv{background:var(--crit)}
  .why li.warn .lv{background:var(--warn)}
  .why .allgood{font-size:13px;color:var(--ok)}
  .tip-sm .why{min-width:250px;max-width:320px;left:auto;right:0;padding:10px 12px}
  .tip-sm .why::before{left:auto;right:16px}
  .tip-sm .why p{margin:0;font-size:12.5px;line-height:1.5;color:var(--tx)}
  .tip-sm .why p+p{margin-top:7px;color:var(--tx-dim)}

  .empty{background:var(--crit-bg);border:1px solid rgba(248,81,73,.3);color:#ffb3ae;
         padding:20px;border-radius:var(--r);text-align:center}
  footer{margin-top:26px;text-align:center;color:var(--tx-mut);font-size:12px}

  @media (max-width:1100px){ .charts{grid-template-columns:1fr} }
  @media (max-width:900px){
    .tip .why{min-width:0;width:min(86vw,400px)}
    header h1{font-size:18px}
  }
  @media (max-width:560px){
    body{padding:18px 12px 40px}
    .disks{grid-template-columns:1fr}
    .updated{text-align:left}
    .tabs button{padding:9px 12px;font-size:13px}
  }
</style>
</head>
<body>
<div class="wrap">

<?php if (!$data): ?>
  <div class="empty">
    <strong>No data yet.</strong><br>
    Run the collector: <span class="mono">systemctl start plex-status.service</span>
    and check that <span class="mono"><?= h(DATA_FILE) ?></span> was created.
  </div>
<?php else:
  $sys  = $data['system'];
  $plex = $data['plex'];
  $reasons = $data['reasons'] ?? [];
  $activity = $data['activity'] ?? [];
?>

  <header>
    <h1>Plex server <span class="host">· <?= h($data['hostname']) ?></span></h1>
    <span class="tip" tabindex="0">
      <span class="pill <?= h($overall) ?>"><span class="dot"></span><?= h($overallLabel) ?><?= $reasons ? ' · ' . count($reasons) : '' ?></span>
      <span class="why">
        <h4><?= $reasons ? 'Why this status' : 'Status' ?></h4>
        <?php if ($reasons): ?>
          <ul>
          <?php foreach ($reasons as $r): ?>
            <li class="<?= h($r['level']) ?>"><span class="lv"></span><span><?= h($r['text']) ?></span></li>
          <?php endforeach; ?>
          </ul>
        <?php elseif ($overall !== 'ok'): ?>
          <div class="sub">No details recorded — run the collector again.</div>
        <?php else: ?>
          <div class="allgood">All checks passed — no warnings.</div>
        <?php endif; ?>
      </span>
    </span>
    <div class="spacer"></div>
    <div class="updated">
      updated <?= $age < 90 ? "{$age}s ago" : human_uptime((int) $age) . ' ago' ?><br>
      <span class="mono"><?= h(date('H:i:s', $data['generated'])) ?></span>
    </div>
  </header>

  <div class="tabs" role="tablist">
    <button role="tab" data-tab="overview" aria-selected="true">Overview</button>
    <button role="tab" data-tab="trends" aria-selected="false">Trends<?= $charts ? '' : ' (no data yet)' ?></button>
  </div>

  <!-- ============ OVERVIEW ============ -->
  <section class="panel" id="tab-overview">

    <div class="grid top">
      <div class="card">
        <h2>Plex</h2>
        <div class="big flag <?= $plex['active'] ? 'ok' : 'crit' ?>"><?= $plex['active'] ? 'Running' : 'Stopped' ?></div>
        <div class="sub">web <?= h($plex['web_version'] ?? '—') ?> · server <?= h($plex['version'] ?? '—') ?></div>
        <?php if (($plex['sessions'] ?? null) !== null): ?>
          <div class="kv" style="margin-top:9px"><span class="k">Active streams</span><span class="v"><?= (int) $plex['sessions'] ?></span></div>
        <?php endif; ?>
      </div>

      <div class="card">
        <h2>Load</h2>
        <div class="big"><?= h($sys['load'][0]) ?></div>
        <div class="sub"><?= h($sys['load'][1]) ?> / <?= h($sys['load'][2]) ?> · <?= (int) $sys['ncpu'] ?> CPU</div>
        <?php if ($sys['cpu_temp'] !== null): ?>
          <div class="kv" style="margin-top:9px"><span class="k">CPU temp</span>
            <span class="v flag <?= temp_class($sys['cpu_temp'], $cpuThr) ?>"><?= (int) $sys['cpu_temp'] ?> °C</span></div>
        <?php endif; ?>
      </div>

      <div class="card">
        <h2>Memory</h2>
        <?php $mp = $sys['mem_total'] ? (int) round($sys['mem_used'] / $sys['mem_total'] * 100) : 0; ?>
        <div class="big"><?= $mp ?><span style="font-size:16px;color:var(--tx-mut)"> %</span></div>
        <div class="sub"><?= bytes((int) $sys['mem_used']) ?> / <?= bytes((int) $sys['mem_total']) ?></div>
        <div class="bar <?= $mp >= 90 ? 'crit' : ($mp >= 75 ? 'warn' : '') ?>" style="margin-top:11px"><i style="width:<?= $mp ?>%"></i></div>
      </div>

      <div class="card">
        <h2>System</h2>
        <div class="big" style="font-size:22px"><?= human_uptime((int) $sys['uptime_s']) ?></div>
        <div class="sub">uptime</div>
        <div style="margin-top:11px">
          <div class="sub" style="margin-bottom:2px">Kernel</div>
          <div class="mono" style="font-size:12px;word-break:break-all"><?= h($sys['kernel']) ?></div>
        </div>
      </div>
    </div>

    <!-- what Plex is doing right now -->
    <h3 class="sec">Activity</h3>
    <div class="card" id="activitycard">
      <?php if (!$activity): ?>
        <div class="idle-note">Idle</div>
        <div class="sub" style="margin-top:4px">No transcoding, analysis or library scan running.</div>
      <?php else:
        $tcpu = 0.0; foreach ($activity as $j) $tcpu += $j['cpu']; ?>
        <?php foreach ($activity as $j): ?>
          <div class="job <?= h($j['kind']) ?>">
            <span class="ico"></span>
            <span class="jb">
              <span class="jt"><?= h($j['label']) ?></span>
              <div class="jw"><?= h($j['why']) ?></div>
              <?php if ($j['file']): ?>
                <div class="jf"><?= h($j['file']) ?>
                  <?php if ($j['library']): ?><span class="sub">· <?= h($j['library']) ?></span><?php endif; ?>
                </div>
                <div class="sub mono" style="font-size:11.5px"><?= h($j['dir']) ?></div>
              <?php endif; ?>
            </span>
            <span class="jm"><?= h(human_uptime((int) $j['runtime_s'])) ?><br><?= round($j['cpu']) ?> % CPU</span>
          </div>
        <?php endforeach; ?>
        <div class="sub" style="margin-top:11px">
          <?= count($activity) ?> process(es) · <?= round($tcpu) ?> % CPU total.
          Background jobs are triggered by your "asap" settings whenever new media is added.
        </div>
      <?php endif; ?>
    </div>

    <?php $np = $data['now_playing'] ?? []; ?>
    <h3 class="sec">Now playing<span id="npcount"><?= $np ? ' · ' . count($np) : '' ?></span>
      <span class="livewarn" id="livewarn" hidden></span></h3>
    <div class="grid now" id="nowplaying">
      <?php if (!$np): ?>
        <div class="card idlecard" id="npidle">Nothing is playing right now.</div>
      <?php endif; ?>
    </div>


    <?php
      $perf   = $data['perf'] ?? [];
      $direct = $perf['direct'] ?? [];
      $pxy    = $perf['proxy'] ?? null;
      $pdb    = $perf['db'] ?? [];
    if ($direct || $pdb): ?>
    <h3 class="sec">Performance</h3>
    <div class="grid perf">
      <div class="card">
        <h2>API response</h2>
        <?php foreach (['/identity' => 'baseline, no DB access', '/library/sections' => 'library list',
                        '/hubs' => 'home screen — the heavy one'] as $ep => $desc):
                $v = $direct[$ep] ?? null; if (!$v) continue; ?>
          <div class="lat">
            <span><span class="ep"><?= h($ep) ?></span><br><span class="sub"><?= h($desc) ?></span></span>
            <span class="ms <?= lat_class((float) $v['median_ms']) ?>"><?= number_format((float) $v['median_ms'], 1) ?> ms</span>
          </div>
        <?php endforeach; ?>
        <div class="sub" style="margin-top:8px">median over one reused connection</div>
      </div>

      <?php if ($pxy): ?>
      <div class="card">
        <h2>Reverse proxy</h2>
        <?php if (!empty($pxy['error'])): ?>
          <div class="big flag crit" style="font-size:20px">unreachable</div>
          <div class="sub"><?= h($pxy['url']) ?></div>
          <div class="sub" style="margin-top:8px">Plex answers locally, so the fault is in
            nginx / TLS / DNS rather than in Plex.</div>
        <?php else:
          $ov = $pxy['overhead_ms'];
          $ovc = $ov === null ? '' : ($ov < 3 ? 'ok' : ($ov < 15 ? 'warn' : 'crit')); ?>
          <div class="big flag <?= $ovc ?>"><?= $ov === null ? '—' : sprintf('%+.1f', $ov) ?><span style="font-size:15px"> ms</span></div>
          <div class="sub">added per request vs. reaching Plex directly</div>
          <div class="sub" style="margin-top:8px">
            <?php if ($ov !== null && $ov < 3): ?>Negligible — the proxy is not a bottleneck,
              so anything slow is Plex-side.
            <?php elseif ($ov !== null && $ov < 15): ?>Noticeable but minor.
            <?php else: ?>High — check upstream keepalive in nginx.<?php endif; ?>
          </div>
        <?php endif; ?>
      </div>
      <?php endif; ?>

      <?php if ($pdb): ?>
      <div class="card">
        <h2>Database</h2>
        <div class="big" style="font-size:24px"><?= bytes((int) ($pdb['bytes'] ?? 0)) ?></div>
        <div class="sub">library.db</div>
        <div class="kv" style="margin-top:9px"><span class="k">WAL</span>
          <span class="v"><?= bytes((int) ($pdb['wal_bytes'] ?? 0)) ?></span></div>
        <?php if (isset($pdb['free_pct'])): ?>
          <div class="kv"><span class="k">Free pages</span>
            <span class="v flag <?= $pdb['free_pct'] >= 25 ? 'warn' : '' ?>"><?= h($pdb['free_pct']) ?> %</span></div>
        <?php endif; ?>
        <?php if (!empty($pdb['backup_count'])): ?>
          <div class="kv"><span class="k">Plex backups</span>
            <span class="v"><?= (int) $pdb['backup_count'] ?> · <?= bytes((int) $pdb['backup_bytes']) ?></span></div>
        <?php endif; ?>
      </div>
      <?php endif; ?>
    </div>
    <?php endif; ?>

    <div class="sechead">
      <h3 class="sec">Disks</h3>
      <div class="wakebox">
        <button type="button" id="wakebtn">Wake all disks</button>
        <div class="wakeprog" id="wakeprog" hidden>
          <div class="wbar"><i></i></div>
          <span class="wtxt"></span>
        </div>
      </div>
    </div>
    <div class="grid disks">
    <?php foreach ($data['disks'] as $d):
        $tran = strtolower($d['tran']);
        $tclass = temp_class($d['temp'], $thr);
        $pct = $d['fs_pct'];
        $barcls = ($pct === null || $d['mount'] !== '/') ? ''
                  : ($pct >= 95 ? 'crit' : ($pct >= 90 ? 'warn' : ''));
        [$stLabel, $stClass, $asleep] = disk_state($d);

        if ($d['smart_ok'] === true)      { $hlabel = 'SMART OK';     $hcls = 'ok'; }
        elseif ($d['smart_ok'] === false) { $hlabel = 'SMART FAILED'; $hcls = 'crit'; }
        else                              { $hlabel = 'SMART N/A';    $hcls = 'muted'; }
    ?>
      <div class="card disk<?= $asleep ? ' asleep' : '' ?>">
        <div class="dhead">
          <span class="dname mono"><?= h($d['dev']) ?></span>
          <span class="badge <?= $tran === 'usb' ? 'usb' : ($tran === 'nvme' ? 'nvme' : 'sata') ?>"><?= h($d['tran']) ?></span>
          <?php
            [$hh1, $hh2] = state_help($stClass);
            $psrc = $d['power_src'] ?? '';
            $inferred = str_starts_with((string) $psrc, 'inferred') || ($d['power_reliable'] ?? true) === false;
            $idle = $d['idle_for_s'] ?? null;
          ?>
          <span class="tip tip-sm" tabindex="0" style="margin-left:auto">
            <span class="state <?= $stClass ?>"><span class="sdot"></span><?= $stLabel ?></span>
            <span class="why">
              <p><?= h($hh1) ?></p>
              <p><?= h($hh2) ?></p>
              <?php if ($inferred): ?>
                <p>This USB bridge does not report power state, so it is worked out from disk
                   activity<?= $idle !== null ? ' (idle for ' . h(human_uptime((int) $idle)) . ')' : '' ?>.</p>
              <?php endif; ?>
            </span>
          </span>
        </div>
        <div class="model">
          <?php if ($d['mount'] !== null): ?><span class="mnt mono"><?= h($d['mount']) ?></span> · <?php endif; ?>
          <?= h($d['size']) ?> · <?= h($d['model']) ?> · <span class="mono"><?= h($d['serial']) ?></span>
        </div>

        <div class="temp">
          <span class="t <?= $tclass ?>"><?= $d['temp'] !== null ? (int) $d['temp'] : '—' ?><?php if ($d['temp'] !== null): ?><span style="font-size:16px;font-weight:600"> °C</span><?php endif; ?></span>
          <?php if (!empty($d['from_cache'])): ?><span class="cachenote">cached<?= ($d['cache_age'] ?? null) !== null ? ' ' . h(human_uptime((int) $d['cache_age'])) . ' ago' : '' ?></span>
          <?php elseif (!empty($d['smart_limited'])): ?><span class="cachenote">bridge exposes health only</span><?php endif; ?>
          <span class="pill <?= $hcls === 'muted' ? 'unknown' : $hcls ?> health"><span class="dot"></span><?= $hlabel ?></span>
        </div>

        <?php if ($d['mount'] !== null): ?>
          <div class="kv" style="padding-top:0"><span class="k">Usage</span><span class="v"><?= $pct !== null ? $pct . ' %' : '—' ?></span></div>
          <div class="bar <?= $barcls ?>"><i style="width:<?= (int) $pct ?>%"></i></div>
          <div class="sub"><?= h($d['fs_used'] ?? '?') ?> / <?= h($d['fs_size'] ?? '?') ?></div>
        <?php else: ?>
          <div class="sub flag muted">not mounted</div>
        <?php endif; ?>

        <div class="attrs">
        <?php if ($tran === 'nvme'): ?>
          <div class="a"><span class="an">Endurance used</span>
            <span class="av"><?= $d['nvme_used'] !== null ? (int) $d['nvme_used'] . ' %' : '—' ?></span></div>
          <div class="a"><span class="an">Spare left</span>
            <span class="av <?= ($d['nvme_spare'] !== null && $d['nvme_spare'] < 20) ? 'bad' : '' ?>"><?= $d['nvme_spare'] !== null ? (int) $d['nvme_spare'] . ' %' : '—' ?></span></div>
          <div class="a"><span class="an">Media errors</span>
            <span class="av <?= (int) $d['nvme_media_err'] > 0 ? 'bad' : '' ?>"><?= $d['nvme_media_err'] ?? '—' ?></span></div>
          <div class="a"><span class="an">Power-on</span>
            <span class="av"><?= $d['poh'] !== null ? number_format((int) $d['poh'], 0, '.', ' ') . ' h' : '—' ?></span></div>
        <?php else: ?>
          <div class="a"><span class="an">Reallocated</span>
            <span class="av <?= (int) $d['realloc'] > 0 ? 'bad' : '' ?>"><?= $d['realloc'] ?? '—' ?></span></div>
          <div class="a"><span class="an">Pending</span>
            <span class="av <?= (int) $d['pending'] > 0 ? 'bad' : '' ?>"><?= $d['pending'] ?? '—' ?></span></div>
          <div class="a"><span class="an">Uncorrectable</span>
            <span class="av <?= (int) $d['uncorrect'] > 0 ? 'bad' : '' ?>"><?= $d['uncorrect'] ?? '—' ?></span></div>
          <div class="a"><span class="an">Power-on</span>
            <span class="av"><?= $d['poh'] !== null ? number_format((int) $d['poh'], 0, '.', ' ') . ' h' : '—' ?></span></div>
          <div class="a"><span class="an">Spin-ups</span>
            <span class="av"><?= $d['start_stop'] !== null ? number_format((int) $d['start_stop'], 0, '.', ' ') : '—' ?></span></div>
          <div class="a"><span class="an">Head parks</span>
            <span class="av"><?= $d['load_cycle'] !== null ? number_format((int) $d['load_cycle'], 0, '.', ' ') : '—' ?></span></div>
        <?php endif; ?>
        </div>

        <?php
          // Everything else lives behind a disclosure so the card stays scannable.
          $lbaW = $d['lba_written'] ?? null; $lbaR = $d['lba_read'] ?? null;
          $nvW  = $d['nvme_written'] ?? null; $nvR = $d['nvme_read'] ?? null;
          $wrTb = $lbaW !== null ? $lbaW * 512 / 1e12 : ($nvW !== null ? $nvW * 512000 / 1e12 : null);
          $rdTb = $lbaR !== null ? $lbaR * 512 / 1e12 : ($nvR !== null ? $nvR * 512000 / 1e12 : null);
          $more = [];
          if ($d['crc_err'] !== null)       $more['Interface CRC errors'] = [(int) $d['crc_err'], (int) $d['crc_err'] > 0];
          if ($d['reported_unc'] !== null)  $more['Reported uncorrectable'] = [(int) $d['reported_unc'], (int) $d['reported_unc'] > 0];
          if ($d['spin_retry'] !== null)    $more['Spin retries'] = [(int) $d['spin_retry'], (int) $d['spin_retry'] > 0];
          if ($d['cmd_timeout'] !== null)   $more['Command timeouts'] = [number_format((int) $d['cmd_timeout'], 0, '.', ' '), false];
          if ($d['offretract'] !== null)    $more['Emergency head retracts'] = [(int) $d['offretract'], false];
          if ($d['power_cycle'] !== null)   $more['Power cycles'] = [(int) $d['power_cycle'], false];
          if ($d['spinup_ms'] !== null && $d['spinup_ms'] > 0) $more['Spin-up time'] = [$d['spinup_ms'] . ' ms', false];
          if ($wrTb !== null)               $more['Data written'] = [number_format($wrTb, 1) . ' TB', false];
          if ($rdTb !== null)               $more['Data read'] = [number_format($rdTb, 1) . ' TB', false];
          if (($d['tmax'] ?? null) !== null) {
              $rangeTxt = ($d['tmin'] ?? null) !== null
                  ? $d['tmin'] . '–' . $d['tmax'] . ' °C'
                  : 'max ' . $d['tmax'] . ' °C';
              $more['Lifetime temp'] = [$rangeTxt, $d['tmax'] >= ($thr['temp_crit'] ?? 58)];
          }
          if ($d['unsafe_shutdown'] !== null) $more['Unsafe shutdowns'] = [(int) $d['unsafe_shutdown'], false];
          if ($d['err_log'] !== null)       $more['Error log entries'] = [(int) $d['err_log'], (int) $d['err_log'] > 0];
          if ($d['margin'] !== null)        $more['Closest to threshold'] = [$d['margin_attr'] . ' (+' . $d['margin'] . ')', $d['margin'] <= 0];
          if (!empty($d['selftest']))       $more['Last self-test'] = [$d['selftest']['type'] . ' — ' . $d['selftest']['status'], !$d['selftest']['passed']];
        ?>
        <?php if ($more): ?>
        <details class="moreattrs">
          <summary>All SMART values<span><?= count($more) ?></span></summary>
          <div class="mgrid">
          <?php foreach ($more as $k => [$v, $bad]): ?>
            <div class="a"><span class="an"><?= h($k) ?></span>
              <span class="av <?= $bad ? 'bad' : '' ?>"><?= h((string) $v) ?></span></div>
          <?php endforeach; ?>
          </div>
        </details>
        <?php endif; ?>

        <?php
          // Absolute counts mean little without a timescale — the rate is what
          // tells you whether the spin-down policy is wearing the drive out.
          $ssc = $d['start_stop'] ?? null; $poh = $d['poh'] ?? null;
          if ($ssc !== null && $poh > 100):
              $perYear = $ssc / ($poh / 8766);
              $lcc = $d['load_cycle'] ?? null;
              $lccYear = $lcc !== null ? $lcc / ($poh / 8766) : null; ?>
          <div class="sub" style="margin-top:9px">
            ≈<?= number_format($perYear, 0, '.', ' ') ?> spin-ups/year<?php
              if ($lccYear !== null): ?> · ≈<?= number_format($lccYear, 0, '.', ' ') ?> head parks/year<?php endif; ?>
          </div>
          <?php
            $s1 = $d['spinups_1h'] ?? null; $s24 = $d['spinups_24h'] ?? null;
            if ($s1 !== null || $s24 !== null): ?>
            <div class="recent">
              <span>Spin-ups <b><?= $s1 !== null ? (int) $s1 : '—' ?></b> last hour</span>
              <span><b><?= $s24 !== null ? (int) $s24 : '—' ?></b> last 24 h</span>
            </div>
          <?php endif; ?>
        <?php endif; ?>

        <?php if (!empty($d['stable_since']) && $d['smart_ok'] === true):
                $stable = time() - (int) $d['stable_since']; ?>
          <div class="sub" style="margin-top:9px">No error-counter change in
            <?= h(human_uptime($stable)) ?> of monitoring.</div>
        <?php endif; ?>
      </div>
    <?php endforeach; ?>
    </div>
  </section>

  <!-- ============ TRENDS ============ -->
  <section class="panel" id="tab-trends" hidden>
    <?php if (!$charts): ?>
      <div class="card"><div class="nodata">
        History is still being collected — charts appear once the collector has run at
        least twice.
      </div></div>
    <?php else: ?>
      <div class="crangewrap">
        <div class="crange" id="ranges">
          <button data-h="6">6 h</button>
          <button data-h="24" aria-pressed="true">24 h</button>
          <button data-h="72">3 d</button>
          <button data-h="168">7 d</button>
          <button data-h="0">All</button>
        </div>
        <span class="sub" id="rangenote"></span>
        <span class="sub" id="resnote"></span>
        <span class="pannote" id="pannote" hidden></span>
      </div>
      <div class="grid charts">
        <?php foreach ($charts as $c): ?>
          <div class="card <?= $c['id'] === 'api' ? 'full' : '' ?>">
            <h2><?= h($c['title']) ?></h2>
            <div class="cwrap" data-chart="<?= h($c['id']) ?>"></div>
            <?php if (!empty($c['note'])): ?>
              <div class="sub" style="margin-top:9px"><?= h($c['note']) ?></div>
            <?php endif; ?>
            <?php if ($c['id'] === 'cap'):
              $proj = [];
              foreach ($c['series'] as $s) {
                  $rate = trend_per_day($ht, $s['data']);       // TB per day
                  $last = null;
                  for ($k = count($s['data']) - 1; $k >= 0; $k--) {
                      if ($s['data'][$k] !== null) { $last = (float) $s['data'][$k]; break; }
                  }
                  if ($last === null) continue;
                  $totalTb = ($s['total_b'] ?? null) ? $s['total_b'] / 1e12 : null;
                  // below ~1 GB/day the slope is indistinguishable from noise
                  if ($rate === null || $rate < 0.001) continue;
                  $gbDay = $rate * 1000;
                  $txt = $s['label'] . ' +' . ($gbDay >= 10 ? round($gbDay) : round($gbDay, 1)) . ' GB/day';
                  if ($totalTb !== null && $last < $totalTb) {
                      $days = ($totalTb - $last) / $rate;
                      if ($days < 3650) {
                          $txt .= ', full in ~' . ($days < 60 ? round($days) . ' d'
                                : ($days < 730 ? round($days / 30) . ' mo' : round($days / 365, 1) . ' y'));
                      }
                  }
                  $proj[] = $txt;
              } ?>
              <div class="sub" style="margin-top:9px">
                <?= $proj ? h(implode(' · ', $proj)) : 'No measurable growth yet — projections appear once a trend forms.' ?>
              </div>
            <?php endif; ?>
          </div>
        <?php endforeach; ?>
      </div>
    <?php endif; ?>
  </section>

  <footer>
    data.json age <?= (int) $age ?>s · collector runs as root via systemd timer ·
    disks are never woken to draw this page · auto-refresh 60s
  </footer>

<?php endif; ?>
</div>

<script>
(function () {
  "use strict";

  // ---- chart data ----
  // Declared FIRST: show() below can trigger a draw straight away when the page
  // is loaded on #trends, and reaching these as `undefined` would throw and take
  // the rest of this script (button handlers included) down with it.
  var HIST = <?= json_encode($ht ? ['t' => $ht] : ['t' => []], JSON_UNESCAPED_SLASHES) ?>;
  var CHARTS = <?= json_encode(array_map(function ($c) {
      return ['id' => $c['id'], 'unit' => $c['unit'] ?? '', 'min' => $c['min'] ?? null,
              'max' => $c['max'] ?? null, 'bands' => $c['bands'] ?? [],
              'series' => array_map(fn($s) => ['label' => $s['label'], 'color' => $s['color'], 'data' => $s['data']], $c['series'])];
  }, $charts), JSON_UNESCAPED_SLASHES) ?>;

  // ---- tabs (state kept in the URL so the auto-refresh doesn't lose it) ----
  var tabs = document.querySelectorAll('.tabs button');
  function show(name) {
    document.querySelectorAll('.panel').forEach(function (p) {
      p.hidden = (p.id !== 'tab-' + name);
    });
    tabs.forEach(function (b) { b.setAttribute('aria-selected', b.dataset.tab === name ? 'true' : 'false'); });
    if (name === 'trends') { loadFull(); drawAll(); }
  }
  tabs.forEach(function (b) {
    b.addEventListener('click', function () {
      location.hash = b.dataset.tab;
      show(b.dataset.tab);
    });
  });
  var initial = (location.hash || '').replace('#', '') || 'overview';
  if (initial !== 'trends' && initial !== 'overview') initial = 'overview';
  show(initial);

  // reload keeps the hash, so you stay on the tab you were reading
  // Reloading mid-drag would yank the view back to "now", so hold off while the
  // user is reading history and retry shortly after.
  (function autoReload() {
    setTimeout(function () {
      if (panOffset > 0) { autoReload(); return; }
      location.reload();
    }, 60000);
  })();

  var rangeH = 24;
  var panOffset = 0;        // seconds shifted back from "now"
  var fullLoaded = false;   // full-resolution history fetched yet?

  /**
   * Pull the full-resolution series once the user actually looks at Trends.
   * data.json only carries a down-sampled copy so the first paint stays quick;
   * this brings in every recorded sample so panning has something to reveal.
   */
  function loadFull() {
    if (fullLoaded) return;
    fullLoaded = true;
    // Guard: an exception raised here would abort the rest of this script and
    // take the range buttons and panning with it. Charts already have the
    // down-sampled data, so failing quietly is the right fallback.
    if (typeof fetch !== 'function') return;
    try {
      fetch('?series=full', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.t || j.t.length < 2) return;
        HIST.t = j.t;
        // match incoming series to the charts already on the page
        (j.charts || []).forEach(function (nc) {
          var cur = CHARTS.filter(function (c) { return c.id === nc.id; })[0];
          if (cur) cur.series = nc.series;
        });
        var n = document.getElementById('resnote');
        if (n) n.textContent = j.t.length + ' samples loaded';
        drawAll();
      })
      .catch(function () { /* keep the embedded low-res data */ });
    } catch (e) { /* no full-resolution data; the embedded series still work */ }
  }

  /** Drag left/right on any chart to move through history. */
  function attachPan(el) {
    var dragging = false, startX = 0, startPan = 0, moved = false;
    function span() {
      var T = HIST.t || [];
      return T.length > 1 ? T[T.length - 1] - T[0] : 0;
    }

    /**
     * Zoom around a fixed point: whatever moment sits under the pointer stays
     * under the pointer, which is what makes wheel and pinch feel predictable.
     * rangeH is a float here, so zooming isn't limited to the button presets.
     */
    function zoomAt(factor, clientX) {
      var T = HIST.t || [];
      if (T.length < 2) return;
      var total = span(), last = T[T.length - 1];
      var cur = rangeH > 0 ? rangeH * 3600 : total;
      var next = Math.min(total, Math.max(360, cur * factor));   // floor: 6 minutes
      var r = el.getBoundingClientRect();
      var f = r.width ? Math.min(1, Math.max(0, (clientX - r.left) / r.width)) : 0.5;
      var to = last - panOffset, from = to - cur;
      var anchor = from + f * cur;                                // time under the pointer
      var from2 = anchor - f * next;
      panOffset = Math.min(Math.max(0, total - next), Math.max(0, last - (from2 + next)));
      rangeH = (next >= total - 1) ? 0 : next / 3600;             // fully zoomed out == "All"
      if (rangeH === 0) panOffset = 0;
      drawAll();
    }

    el.addEventListener('wheel', function (e) {
      if (!e.deltaY) return;
      e.preventDefault();
      zoomAt(e.deltaY > 0 ? 1.25 : 1 / 1.25, e.clientX);
    }, { passive: false });

    // pinch to zoom (iPad and any other multi-touch screen)
    var pinchDist = 0;
    function dist(t) {
      var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }
    el.addEventListener('touchstart', function (e) {
      if (e.touches.length === 2) { pinchDist = dist(e.touches); dragging = false; }
    }, { passive: true });
    el.addEventListener('touchmove', function (e) {
      if (e.touches.length !== 2 || !pinchDist) return;
      e.preventDefault();
      var d2 = dist(e.touches);
      if (Math.abs(d2 - pinchDist) < 4) return;
      var mid = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      zoomAt(pinchDist / d2, mid);
      pinchDist = d2;
    }, { passive: false });
    el.addEventListener('touchend', function (e) {
      if (e.touches.length < 2) pinchDist = 0;
    });

    function down(e) {
      if (e.touches && e.touches.length > 1) return;   // two fingers = pinch, not pan
      if (rangeH === 0) return;               // "All" already shows everything
      dragging = true; moved = false;
      startX = (e.touches ? e.touches[0].clientX : e.clientX);
      startPan = panOffset;
      el.classList.add('grabbing');
    }
    function move(e) {
      if (!dragging) return;
      var x = (e.touches ? e.touches[0].clientX : e.clientX);
      var dx = x - startX;
      if (Math.abs(dx) > 3) moved = true;
      // one chart width == the visible window, so dragging maps 1:1 to time
      var secPerPx = (rangeH * 3600) / Math.max(1, el.clientWidth);
      var next = startPan + dx * secPerPx;
      var maxPan = Math.max(0, span() - rangeH * 3600);
      panOffset = Math.min(maxPan, Math.max(0, next));
      if (e.cancelable && moved) e.preventDefault();
      drawAll();
    }
    function up() {
      dragging = false;
      el.classList.remove('grabbing');
    }
    el.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    el.addEventListener('touchstart', down, { passive: true });
    el.addEventListener('touchmove', move, { passive: false });
    el.addEventListener('touchend', up);
  }

  var rb = document.getElementById('ranges');

  /**
   * A range button is only meaningful once the history is longer than the
   * window it selects — otherwise every button shows the same full series and
   * clicking them looks broken. Lock the ones that cannot crop anything yet and
   * say how much history there actually is.
   */
  function syncRanges() {
    if (!rb) return;
    var T = HIST.t || [];
    var spanH = T.length > 1 ? (T[T.length - 1] - T[0]) / 3600 : 0;

    // A preset is only meaningful once the history outlasts it; otherwise every
    // button shows the same full series and clicking them looks broken.
    var usable = null, matched = false;
    rb.querySelectorAll('button').forEach(function (b) {
      var hrs = parseInt(b.dataset.h, 10);
      var ok = hrs === 0 || spanH > hrs * 1.02;
      b.disabled = !ok;
      b.title = ok ? '' : 'Needs more than ' + b.textContent.trim() + ' of history';
      if (ok && hrs > 0 && usable === null) usable = hrs;
      // wheel/pinch zoom lands between the presets, so highlight one only on an
      // exact match rather than pretending a preset is active
      var on = ok && Math.abs(hrs - rangeH) < 0.01;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) matched = true;
    });

    var note = document.getElementById('rangenote');
    if (note) {
      var txt;
      if (spanH < 1)       txt = Math.max(1, Math.round(spanH * 60)) + ' min of history so far';
      else if (spanH < 48) txt = spanH.toFixed(1) + ' h of history so far';
      else                 txt = Math.round(spanH / 24) + ' d of history';
      if (!matched && rangeH > 0) {
        txt = (rangeH < 1 ? Math.round(rangeH * 60) + ' min' : rangeH.toFixed(1) + ' h') + ' window · ' + txt;
      }
      note.textContent = txt + (usable === null ? ' — longer ranges unlock as it builds up' : '');
    }

    var pn = document.getElementById('pannote');
    if (pn) {
      if (panOffset > 60) {
        var h = panOffset / 3600;
        pn.innerHTML = 'viewing ' + (h < 48 ? h.toFixed(1) + ' h' : Math.round(h / 24) + ' d')
                     + ' back <button type="button" id="pannow">now</button>';
        pn.hidden = false;
        var nb = document.getElementById('pannow');
        if (nb) nb.onclick = function () { panOffset = 0; drawAll(); };
      } else {
        pn.hidden = true;
      }
    }
  }

  if (rb) {
    rb.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b || b.disabled) return;
      rangeH = parseInt(b.dataset.h, 10);
      panOffset = 0;                     // a new window always starts at "now"
      rb.querySelectorAll('button').forEach(function (x) {
        x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
      });
      drawAll();
    });
  }

  function fmtTime(ts, span) {
    var d = new Date(ts * 1000);
    var t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    if (span > 172800) return d.toLocaleDateString([], { day: 'numeric', month: 'numeric' }) + ' ' + t;
    return t;
  }

  function draw(wrap, cfg) {
    var T = HIST.t || [];
    if (T.length < 2) { wrap.innerHTML = '<div class="nodata">Not enough history yet.</div>'; return; }

    // Visible window: rangeH wide, shifted back by panOffset seconds. Panning
    // is what makes a week of five-minute samples explorable instead of just
    // squashed into one screen.
    var last = T[T.length - 1], first = T[0];
    var from = 0, to = last;
    if (rangeH > 0) {
      to = last - panOffset;
      from = to - rangeH * 3600;
      if (from < first) { from = first; to = Math.min(last, first + rangeH * 3600); }
      if (to > last) { to = last; from = to - rangeH * 3600; }
    }
    var idx = [];
    for (var i = 0; i < T.length; i++) if (T[i] >= from && T[i] <= to) idx.push(i);
    if (idx.length < 2) idx = T.map(function (_, i) { return i; });
    var times = idx.map(function (i) { return T[i]; });
    var series = cfg.series.map(function (s) {
      return { label: s.label, color: s.color, data: idx.map(function (i) { return s.data[i]; }) };
    });

    var W = 900, H = 260, padL = 48, padR = 14, padT = 14, padB = 26;
    var all = [];
    series.forEach(function (s) { s.data.forEach(function (v) { if (v !== null && v !== undefined) all.push(+v); }); });
    if (!all.length) { wrap.innerHTML = '<div class="nodata">No data in this range.</div>'; return; }

    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    (cfg.bands || []).forEach(function (b) { hi = Math.max(hi, +b.at); });
    if (hi - lo < 0.5) hi = lo + 1;
    var pad = (hi - lo) * 0.12;
    var min = cfg.min !== null && cfg.min !== undefined ? cfg.min : Math.max(0, lo - pad);
    var max = cfg.max !== null && cfg.max !== undefined ? cfg.max : hi + pad;
    if (max - min < 0.001) max = min + 1;

    var n = times.length;
    var X = function (i) { return padL + (i / (n - 1)) * (W - padL - padR); };
    var Y = function (v) { return padT + (1 - ((v - min) / (max - min))) * (H - padT - padB); };

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet">';
    for (var g = 0; g <= 3; g++) {
      var val = min + (max - min) * g / 3, yy = Y(val).toFixed(1);
      svg += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yy + '" y2="' + yy + '" stroke="#262c37"/>'
           + '<text x="' + (padL - 8) + '" y="' + (+yy + 4) + '" text-anchor="end" font-size="12" fill="#5b6472">'
           + (Math.abs(max - min) < 5 ? val.toFixed(1) : Math.round(val)) + '</text>';
    }
    (cfg.bands || []).forEach(function (b) {
      if (b.at < min || b.at > max) return;
      var yy = Y(+b.at).toFixed(1);
      svg += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yy + '" y2="' + yy
           + '" stroke="' + b.color + '" stroke-dasharray="4 4" opacity=".55"/>'
           + '<text x="' + (W - padR) + '" y="' + (+yy - 5) + '" text-anchor="end" font-size="11" fill="'
           + b.color + '" opacity=".85">' + b.label + '</text>';
    });
    var span = times[n - 1] - times[0];
    [0, Math.floor((n - 1) / 2), n - 1].forEach(function (i) {
      var a = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      svg += '<text x="' + X(i).toFixed(1) + '" y="' + (H - 7) + '" text-anchor="' + a
           + '" font-size="12" fill="#5b6472">' + fmtTime(times[i], span) + '</text>';
    });

    series.forEach(function (s) {
      var seg = [];
      var flush = function () {
        if (seg.length > 1) svg += '<polyline points="' + seg.join(' ') + '" fill="none" stroke="' + s.color
          + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
        else if (seg.length === 1) { var p = seg[0].split(','); svg += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="2.4" fill="' + s.color + '"/>'; }
        seg = [];
      };
      s.data.forEach(function (v, i) {
        if (v === null || v === undefined) { flush(); return; }
        seg.push(X(i).toFixed(1) + ',' + Y(+v).toFixed(1));
      });
      flush();
    });
    svg += '<line class="cross" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB)
         + '" stroke="#8b94a3" stroke-width="1" opacity="0"/>';
    svg += '</svg>';

    var leg = '<div class="legend">';
    series.forEach(function (s) {
      var last = null;
      for (var i = s.data.length - 1; i >= 0; i--) if (s.data[i] !== null && s.data[i] !== undefined) { last = s.data[i]; break; }
      leg += '<span class="li"><i style="background:' + s.color + '"></i>' + s.label
           + (last !== null ? ' <b>' + (Math.round(last * 10) / 10) + cfg.unit + '</b>' : '') + '</span>';
    });
    leg += '</div>';

    wrap.innerHTML = svg + leg + '<div class="ctip"></div>';
    if (!wrap.dataset.pan) { wrap.dataset.pan = '1'; attachPan(wrap); }

    // hover / touch readout
    var el = wrap.querySelector('svg'), tip = wrap.querySelector('.ctip'),
        cross = wrap.querySelector('.cross');
    function move(ev) {
      var r = el.getBoundingClientRect();
      var cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      var rel = (cx / r.width) * W;
      var i = Math.round(((rel - padL) / (W - padL - padR)) * (n - 1));
      if (i < 0) i = 0; if (i > n - 1) i = n - 1;
      cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('opacity', '.5');
      var html = '<div class="ct">' + fmtTime(times[i], span) + '</div>';
      var any = false;
      series.forEach(function (s) {
        var v = s.data[i];
        html += '<div class="cr"><i style="background:' + s.color + '"></i>' + s.label
              + '<b>' + (v === null || v === undefined ? '—' : (Math.round(v * 10) / 10) + cfg.unit) + '</b></div>';
        if (v !== null && v !== undefined) any = true;
      });
      tip.innerHTML = html;
      tip.style.opacity = any ? '1' : '.75';
      var px = (X(i) / W) * r.width;
      tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), r.width - tip.offsetWidth) + 'px';
      tip.style.top = '4px';
    }
    el.addEventListener('mousemove', move);
    el.addEventListener('touchstart', move, { passive: true });
    el.addEventListener('touchmove', move, { passive: true });
    el.addEventListener('mouseleave', function () { tip.style.opacity = '0'; cross.setAttribute('opacity', '0'); });
  }

  // ---- now playing ----------------------------------------------------
  var NP = <?= json_encode($data['now_playing'] ?? [], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;
  var NP_AT = <?= (int) ($data['generated'] ?? time()) ?>;
  var DISKS = <?= json_encode(array_map(fn($d) => ['dev' => $d['dev'], 'tran' => $d['tran']],
                                        $data['disks'] ?? []), JSON_UNESCAPED_SLASHES) ?>;

  function hms(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
  }
  // Plex reports 'burn' when it renders subtitles into the picture — that is a
  // full transcode of the video, so it must not be mistaken for direct play.
  function decClass(d) {
    if (!d) return '';
    d = String(d).toLowerCase();
    if (d.indexOf('transcode') >= 0 || d.indexOf('burn') >= 0) return 'transcode';
    if (d.indexOf('copy') >= 0) return 'copy';
    return 'direct';
  }
  function decLabel(d) {
    if (!d) return '';
    d = String(d).toLowerCase();
    if (d.indexOf('burn') >= 0) return 'Burned in';
    if (d.indexOf('transcode') >= 0) return 'Transcode';
    if (d.indexOf('copy') >= 0) return 'Stream copy';
    return 'Direct Play';
  }
  // Prefer the live proxy, fall back to the collector's cached copy, then to a
  // plain glyph — a broken image icon would just look like a bug.
  function art(s) {
    var cached = s.thumb ? esc(s.thumb) : '';
    var onerr = cached
      ? "this.onerror=null;this.src='" + cached + "'"
      : "this.onerror=null;this.parentNode.innerHTML='<span>&#9835;</span>'";
    if (s.art) {
      return '<img src="?art=' + encodeURIComponent(s.art) + '" alt="" loading="lazy" onerror="'
           + onerr + '">';
    }
    return cached ? '<img src="' + cached + '" alt="">' : '<span>&#9835;</span>';
  }

  function esc(x) {
    return String(x == null ? '' : x).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // What the rendered card actually depends on. Rebuilding the DOM every second
  // would throw the poster away and re-insert it each tick, which is exactly
  // what makes the image flicker — so the markup is only regenerated when one
  // of these changes, and the moving parts are updated in place instead.
  var npSig = null;
  function npSignature() {
    return JSON.stringify(NP.map(function (s) {
      return [s.show, s.title, s.season, s.episode, s.state, s.user, s.product, s.player,
              s.video, s.video_dec, s.audio, s.audio_dec, s.subs, s.subs_dec,
              s.volume, s.bandwidth, s.duration_ms, s.art, s.thumb];
    }));
  }

  /** Advance just the progress bar and elapsed time — no DOM replacement. */
  function tickNP() {
    var box = document.getElementById('nowplaying');
    if (!box || !NP.length) return;
    NP.forEach(function (s, i) {
      var card = box.children[i];
      if (!card) return;
      var off = s.offset_ms || 0;
      if (s.state === 'playing') off += (Date.now() / 1000 - NP_AT) * 1000;
      var dur = s.duration_ms || 0;
      if (dur) off = Math.min(off, dur);
      var bar = card.querySelector('.pbar i');
      if (bar) bar.style.width = (dur ? off / dur * 100 : 0).toFixed(2) + '%';
      var t = card.querySelector('.times span');
      if (t) t.textContent = hms(off);
    });
  }

  function renderNP() {
    var box = document.getElementById('nowplaying');
    if (!box) return;
    var cnt = document.getElementById('npcount');
    if (cnt) cnt.textContent = NP.length ? ' · ' + NP.length : '';
    if (!NP.length) {
      if (npSig !== 'idle') {
        box.innerHTML = '<div class="card idlecard">Nothing is playing right now.</div>';
        npSig = 'idle';
      }
      return;
    }
    var sig = npSignature();
    if (sig === npSig) { tickNP(); return; }   // same content: just move the bar
    npSig = sig;
    box.innerHTML = NP.map(function (s, i) {
      // The collector only samples every so often, so advance the position
      // ourselves while something is actually playing — otherwise the bar would
      // sit still and look broken between refreshes.
      var off = s.offset_ms || 0;
      if (s.state === 'playing') off += (Date.now() / 1000 - NP_AT) * 1000;
      var dur = s.duration_ms || 0;
      if (dur) off = Math.min(off, dur);
      var pct = dur ? (off / dur * 100) : 0;

      var head = s.show || s.title || 'Unknown';
      var sub = '';
      if (s.show && (s.season != null || s.episode != null)) {
        sub = 'S' + (s.season != null ? s.season : '?') +
              ' · E' + (s.episode != null ? s.episode : '?') +
              (s.title ? ' — ' + s.title : '');
      } else if (s.year) { sub = String(s.year); }

      var rows = '';
      function row(k, v, dec) {
        if (!v) return;
        rows += '<div class="rk">' + k + '</div><div class="rv">' + esc(v) +
                (dec ? '<span class="dec ' + decClass(dec) + '">' + decLabel(dec) + '</span>' : '') +
                '</div>';
      }
      row('Video', s.video, s.video_dec);
      row('Audio', s.audio, s.audio_dec);
      row('Subtitles', s.subs, s.subs_dec);
      if (s.volume) rows += '<div class="rk">Reading from</div><div class="rv vol">' + esc(s.volume) + '</div>';

      var st = (s.state || '').toLowerCase();
      return '<div class="card np">' +
        '<div class="art">' + art(s) + '</div>' +
        '<div class="meta">' +
          '<div class="t1">' + esc(head) + '</div>' +
          (sub ? '<div class="t2">' + esc(sub) + '</div>' : '') +
          '<div class="pbar"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
          '<div class="times"><span>' + hms(off) + '</span><span>' + hms(dur) + '</span></div>' +
          '<div class="who">' +
            '<span class="st ' + st + '">' + esc(s.state || '?') + '</span>' +
            (s.user ? '<span>' + esc(s.user) + '</span>' : '') +
            (s.product ? '<span>' + esc(s.product) + (s.player ? ' — ' + esc(s.player) : '') + '</span>' : '') +
            (s.bandwidth ? '<span>' + (s.bandwidth / 1000).toFixed(1) + ' Mbps' +
                           (s.local ? ' local' : ' remote') + '</span>' : '') +
          '</div>' +
          (rows ? '<div class="rows">' + rows + '</div>' : '') +
        '</div></div>';
    }).join('');
  }

  // ---- activity -------------------------------------------------------
  var ACT = <?= json_encode($activity ?? [], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;
  var actSig = null;

  function dur(s) {
    s = Math.max(0, Math.floor(s));
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    var out = [];
    if (d) out.push(d + 'd');
    if (h) out.push(h + 'h');
    if (!d) out.push(m + 'm');
    return out.join(' ');
  }

  function renderACT() {
    var card = document.getElementById('activitycard');
    if (!card) return;
    var sig = JSON.stringify(ACT.map(function (j) {
      return [j.kind, j.label, j.file, j.library, Math.round(j.cpu), Math.round(j.runtime_s / 5)];
    }));
    if (sig === actSig) return;      // nothing changed: leave the DOM alone
    actSig = sig;

    if (!ACT.length) {
      card.innerHTML = '<div class="idle-note">Idle</div>' +
        '<div class="sub" style="margin-top:4px">No transcoding, analysis or library scan running.</div>';
      return;
    }
    var tcpu = 0;
    var html = ACT.map(function (j) {
      tcpu += j.cpu || 0;
      return '<div class="job ' + esc(j.kind) + '">' +
        '<span class="ico"></span>' +
        '<span class="jb">' +
          '<span class="jt">' + esc(j.label) + '</span>' +
          '<div class="jw">' + esc(j.why) + '</div>' +
          (j.file ? '<div class="jf">' + esc(j.file) +
                    (j.library ? '<span class="sub">· ' + esc(j.library) + '</span>' : '') + '</div>' +
                    '<div class="sub mono" style="font-size:11.5px">' + esc(j.dir) + '</div>' : '') +
        '</span>' +
        '<span class="jm">' + dur(j.runtime_s) + '<br>' + Math.round(j.cpu) + ' % CPU</span>' +
      '</div>';
    }).join('');
    card.innerHTML = html +
      '<div class="sub" style="margin-top:11px">' + ACT.length + ' process(es) · ' +
      Math.round(tcpu) + ' % CPU total. Background jobs are triggered by your "asap" ' +
      'settings whenever new media is added.</div>';
  }

  // One request refreshes both live panels, so poll it for a
  // near-live panel without reloading the whole page
  function pollSessions() {
    if (typeof fetch !== 'function') return;
    try {
      fetch('?live=1&_=' + Date.now(), { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (!j) return;
          if (j.sessions) { NP = j.sessions; NP_AT = j.generated || NP_AT; renderNP(); }
          if (j.activity) { ACT = j.activity; renderACT(); }
          var warn = document.getElementById('livewarn');
          if (warn) {
            if (j.live === false && j.why) {
              warn.textContent = 'not live: ' + j.why;
              warn.hidden = false;
            } else {
              warn.hidden = true;
            }
          }
        })
        .catch(function () {});
    } catch (e) {}
  }
  renderNP();
  setInterval(tickNP, 1000);        // moves the bar without touching the poster
  setInterval(pollSessions, 10000);   // both live panels come from one request
  pollSessions();

  // ---- wake all disks -------------------------------------------------
  // No helper service and no privileges: the button asks the page to read a
  // probe file off each data volume, and that read is what spins the disk up.
  // Verification is direct — a woken disk answers a 1 s probe, a sleeping one
  // cannot — so this reports what the disks are actually doing, not just that
  // a request was sent.
  (function () {
    var btn = document.getElementById('wakebtn');
    var box = document.getElementById('wakeprog');
    if (!btn || !box) return;
    var bar = box.querySelector('.wbar i'), txt = box.querySelector('.wtxt');
    var LIMIT = 120;      // spin-up plus a full SMART pass over five disks
    var awakeSeen = false;

    function fail(msg) {
      box.className = 'wakeprog fail';
      bar.style.width = '100%';
      txt.textContent = msg;
      btn.disabled = false;
    }

    btn.addEventListener('click', function () {
      if (typeof fetch !== 'function') { alert('This browser cannot send the request.'); return; }
      btn.disabled = true;
      box.hidden = false;
      box.className = 'wakeprog';
      bar.style.width = '3%';
      txt.textContent = 'spinning up…';
      var t0 = Date.now() / 1000;

      fetch('?wake=1', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j || !j.ok) throw new Error(j && j.error ? j.error : 'request refused');
          txt.textContent = 'spinning up ' + j.disks.length + ' disks…' +
                            (j.refresh ? '' : ' (SMART refresh not permitted)');
          setTimeout(function () { poll(t0, j.disks.length, !!j.refresh); }, 2500);
        })
        .catch(function (e) { fail('failed: ' + e.message); });
    });

    /**
     * Two things have to land: every disk answering a probe (proof they woke),
     * and a data.json newer than the click in which no disk is serving cached
     * SMART (proof the values on screen are fresh). If the collector refresh
     * wasn't permitted, the first alone finishes the job and we say so.
     */
    function poll(t0, total, wantFresh) {
      var elapsed = Date.now() / 1000 - t0;
      if (elapsed > LIMIT) {
        if (awakeSeen) {
          box.className = 'wakeprog done';
          bar.style.width = '100%';
          txt.textContent = 'disks awake — SMART refreshes on the next collector pass';
          btn.disabled = false;
        } else {
          fail('timed out — the disks did not answer');
        }
        return;
      }

      Promise.all([
        fetch('?wakecheck=1&_=' + Date.now(), { cache: 'no-store' }).then(function (r) { return r.json(); }),
        wantFresh ? fetch('data.json?_=' + Date.now(), { cache: 'no-store' })
                      .then(function (r) { return r.ok ? r.json() : null; })
                      .catch(function () { return null; })
                  : Promise.resolve(null),
      ]).then(function (res) {
        var chk = res[0], data = res[1];
        if (!chk || !chk.ok) throw new Error(chk && chk.error ? chk.error : 'check failed');

        var awake = chk.awake >= chk.total;
        if (awake) awakeSeen = true;
        // last_smart_read is a timestamp the collector persists, so it stays
        // true even when the regular five-minute run rewrites data.json from
        // cache moments later and clears every from_cache flag.
        var fresh = !wantFresh || (data && (data.last_smart_read || 0) >= Math.floor(t0));

        var pct = (chk.total ? chk.awake / chk.total : 0) * (wantFresh ? 70 : 100);
        if (awake && wantFresh) pct = fresh ? 100 : 85;
        bar.style.width = Math.max(5, pct).toFixed(0) + '%';

        if (awake && fresh) {
          box.className = 'wakeprog done';
          bar.style.width = '100%';
          var temps = data ? (data.disks || []).filter(function (d) {
              return d.temp != null && (d.tran || '').toLowerCase() !== 'nvme';
            }).map(function (d) { return d.temp; }) : [];
          txt.textContent = 'all ' + chk.total + ' disks awake' +
            (wantFresh ? ', SMART refreshed' : '') +
            (temps.length ? ' (' + Math.min.apply(null, temps) + '–' +
                            Math.max.apply(null, temps) + ' °C)' : '');
          if (wantFresh) setTimeout(function () { location.reload(); }, 1500);
          else setTimeout(function () { btn.disabled = false; }, 3000);
          return;
        }

        txt.textContent = !awake
          ? chk.awake + ' of ' + chk.total + ' awake… ' + Math.max(0, Math.round(LIMIT - elapsed)) + 's'
          : 'reading SMART… ' + Math.max(0, Math.round(LIMIT - elapsed)) + 's';
        setTimeout(function () { poll(t0, total, wantFresh); }, 2000);
      }).catch(function () {
        setTimeout(function () { poll(t0, total, wantFresh); }, 2500);
      });
    }
  })();

  function drawAll() {
    // Drawing into a hidden panel would size every chart against a zero-width
    // container and produce empty SVGs, so wait until Trends is actually shown.
    var panel = document.getElementById('tab-trends');
    if (!panel || panel.hidden) return;
    syncRanges();
    CHARTS.forEach(function (cfg) {
      var wrap = document.querySelector('[data-chart="' + cfg.id + '"]');
      if (wrap) draw(wrap, cfg);
    });
  }

  // Draw once the DOM is ready, and again after a resize so the SVG geometry
  // matches the new width (rotating an iPad, or dragging a window narrower).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', drawAll);
  } else {
    drawAll();
  }
  var rzT;
  window.addEventListener('resize', function () {
    clearTimeout(rzT);
    rzT = setTimeout(drawAll, 150);
  });
})();
</script>
</body>
</html>
