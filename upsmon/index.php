<?php
/**
 * index.php — UPS dashboard.
 *
 * Renders from the daemon's snapshot and refreshes itself over the small JSON
 * endpoints below. It does nothing privileged: no shell, no upsd credentials,
 * no direct access to the UPS.
 */
declare(strict_types=1);

require_once __DIR__ . '/upsmon-api.php';

/**
 * PHP falls back to UTC when date.timezone isn't set, which makes every
 * timestamp here disagree with the server's own clock. Follow the system zone.
 */
function use_system_timezone(): void
{
    $set = (string) ini_get('date.timezone');
    if ($set !== '' && strcasecmp($set, 'UTC') !== 0) {
        return;
    }
    $tz = '';
    if (is_readable('/etc/timezone')) {
        $tz = trim((string) @file_get_contents('/etc/timezone'));
    }
    if ($tz === '' && is_link('/etc/localtime')) {
        $target = (string) @readlink('/etc/localtime');
        if (preg_match('#zoneinfo/(.+)$#', $target, $m)) {
            $tz = $m[1];
        }
    }
    if ($tz !== '' && in_array($tz, timezone_identifiers_list(), true)) {
        date_default_timezone_set($tz);
    }
}
use_system_timezone();

// ---- JSON endpoints the page polls ---------------------------------------
if (isset($_GET['api'])) {
    $what = (string) $_GET['api'];

    if ($what === 'status') {
        upsmon_relay('/api/status');
        exit;
    }
    if ($what === 'health') {
        upsmon_relay('/api/health');
        exit;
    }
    if ($what === 'history') {
        $range  = preg_replace('/[^0-9smhdwy.]/', '', (string) ($_GET['range'] ?? '24h'));
        $points = (int) ($_GET['points'] ?? 600);
        $points = max(50, min(3000, $points));
        upsmon_relay('/api/history?range=' . rawurlencode($range) . '&points=' . $points, 'GET', null, 30.0);
        exit;
    }
    if ($what === 'events') {
        $limit = max(1, min(500, (int) ($_GET['limit'] ?? 100)));
        upsmon_relay('/api/events?limit=' . $limit);
        exit;
    }
    if ($what === 'tests') {
        $limit = max(1, min(500, (int) ($_GET['limit'] ?? 100)));
        upsmon_relay('/api/tests?limit=' . $limit);
        exit;
    }
    if ($what === 'outages') {
        upsmon_relay('/api/outages?limit=50');
        exit;
    }
    if ($what === 'capabilities') {
        upsmon_relay('/api/capabilities');
        exit;
    }
    // --- the two that change something ------------------------------------
    if ($what === 'command' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $in  = json_decode((string) file_get_contents('php://input'), true);
        $cmd = is_array($in) ? (string) ($in['command'] ?? '') : '';
        $pin = is_array($in) ? (string) ($in['pin'] ?? '') : '';
        if ($cmd === '' || !preg_match('/^[a-z0-9.\-]+$/', $cmd)) {
            http_response_code(400);
            header('Content-Type: application/json');
            echo json_encode(['ok' => false, 'error' => 'bad command name']);
            exit;
        }
        // The PIN is checked by the daemon, not here: this page holds no
        // secrets, and putting the comparison in the daemon keeps one place
        // that counts failed attempts and applies the lockout.
        // A test can run for a while; the daemon answers as soon as the UPS
        // accepts the command, but give it room on a busy machine.
        upsmon_relay('/api/command', 'POST', ['command' => $cmd, 'pin' => $pin], 30.0);
        exit;
    }
    if ($what === 'reset' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $in    = json_decode((string) file_get_contents('php://input'), true);
        $scope = is_array($in) ? (string) ($in['scope'] ?? '') : '';
        if (!in_array($scope, ['all', 'history', 'events', 'outages', 'tests'], true)) {
            http_response_code(400);
            header('Content-Type: application/json');
            echo json_encode(['ok' => false, 'error' => 'unknown scope']);
            exit;
        }
        $body = ['scope' => $scope];
        if (is_array($in) && isset($in['pin'])) {
            $body['pin'] = (string) $in['pin'];
        }
        upsmon_relay('/api/reset', 'POST', $body, 30.0);
        exit;
    }
    if ($what === 'set' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $in    = json_decode((string) file_get_contents('php://input'), true);
        $var   = is_array($in) ? (string) ($in['var'] ?? '') : '';
        $value = is_array($in) ? (string) ($in['value'] ?? '') : '';
        $pin   = is_array($in) ? (string) ($in['pin'] ?? '') : '';
        if ($var === '' || !preg_match('/^[a-z0-9.\-]+$/', $var)) {
            http_response_code(400);
            header('Content-Type: application/json');
            echo json_encode(['ok' => false, 'error' => 'bad variable name']);
            exit;
        }
        // Writing waits for the driver's poll cycle before it can confirm.
        @set_time_limit(90);
        upsmon_relay('/api/set', 'POST',
                     ['var' => $var, 'value' => $value, 'pin' => $pin], 60.0);
        exit;
    }

    http_response_code(404);
    header('Content-Type: application/json');
    echo json_encode(['ok' => false, 'error' => 'unknown endpoint']);
    exit;
}

// ---- first paint ----------------------------------------------------------
$snapshot = upsmon_get('/api/status', 6.0);
$health   = upsmon_get('/api/health', 6.0);

function h($value): string
{
    return htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8');
}

function runtime_text($seconds): string
{
    if ($seconds === null || $seconds === '') {
        return '—';
    }
    $total = (int) $seconds;
    $hours = intdiv($total, 3600);
    $mins  = intdiv($total % 3600, 60);
    $secs  = $total % 60;
    if ($hours) {
        return sprintf('%dh %02dm', $hours, $mins);
    }
    if ($mins) {
        return sprintf('%dm %02ds', $mins, $secs);
    }
    return $secs . 's';
}

function uptime_text(int $seconds): string
{
    $days  = intdiv($seconds, 86400);
    $hours = intdiv($seconds % 86400, 3600);
    $mins  = intdiv($seconds % 3600, 60);
    if ($days) {
        return "{$days}d {$hours}h";
    }
    if ($hours) {
        return "{$hours}h {$mins}m";
    }
    return "{$mins}m";
}

$vars      = $snapshot['vars'] ?? [];
$level     = $snapshot['level'] ?? 'unknown';
$model     = $vars['device.model'] ?? ($vars['ups.model'] ?? 'UPS');
$maker     = $vars['device.mfr'] ?? ($vars['ups.mfr'] ?? '');
$online    = (bool) ($snapshot['online'] ?? false);
$levelText = ['ok' => 'All good', 'warn' => 'Needs attention',
              'crit' => 'Problem', 'unknown' => 'No data'][$level] ?? $level;
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title><?= h($model) ?> — UPS status</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml" id="favicon">
<link rel="alternate icon" href="favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="theme-color" content="#161b22">
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c222b; --line:#262c37;
    --tx:#e6e9ef; --tx-dim:#8b94a3; --tx-mut:#5b6472;
    --ok:#3fb950; --warn:#d29922; --crit:#f85149; --info:#58a6ff;
    --batt:#a371f7; --load:#f0883e; --sleep:#6e7681;
    --ok-bg:rgba(63,185,80,.12); --warn-bg:rgba(210,153,34,.12);
    --crit-bg:rgba(248,81,73,.13); --info-bg:rgba(88,166,255,.12);
    --r:14px;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--tx);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
    padding:26px max(16px,env(safe-area-inset-left)) 56px}
  .wrap{max-width:1240px;margin:0 auto}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
  header h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
  header .host{color:var(--tx-dim);font-weight:400}
  .spacer{flex:1}
  .updated{color:var(--tx-mut);font-size:13px;text-align:right;line-height:1.35}

  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .tabs button{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
    color:var(--tx-dim);font:inherit;font-weight:600;font-size:14px;padding:9px 15px;
    cursor:pointer;border-radius:8px 8px 0 0;transition:color .15s,border-color .15s}
  .tabs button:hover{color:var(--tx)}
  .tabs button[aria-selected="true"]{color:var(--info);border-bottom-color:var(--info)}
  .panel[hidden]{display:none}

  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;
        font-weight:600;font-size:14px;border:1px solid transparent}
  .pill .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .pill.ok{background:var(--ok-bg);color:var(--ok);border-color:rgba(63,185,80,.3)}
  .pill.warn{background:var(--warn-bg);color:var(--warn);border-color:rgba(210,153,34,.3)}
  .pill.crit{background:var(--crit-bg);color:var(--crit);border-color:rgba(248,81,73,.3)}
  .pill.unknown{background:var(--panel2);color:var(--tx-dim)}
  .pill.ok .dot{background:var(--ok)} .pill.warn .dot{background:var(--warn)}
  .pill.crit .dot{background:var(--crit);animation:pulse 1.4s infinite}
  .pill.unknown .dot{background:var(--tx-mut)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

  .grid{display:grid;gap:14px}
  .top{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
  .charts{grid-template-columns:repeat(auto-fit,minmax(480px,1fr))}
  .full{grid-column:1/-1}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:17px 19px}
  .card h2{margin:0 0 13px;font-size:12px;font-weight:700;letter-spacing:.7px;
           text-transform:uppercase;color:var(--tx-mut)}

  .metric .label{font-size:12px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
                 color:var(--tx-mut);margin-bottom:9px}
  .metric .value{font-size:30px;font-weight:680;line-height:1.05;letter-spacing:-.5px}
  .metric .value .unit{font-size:16px;font-weight:600;color:var(--tx-dim);margin-left:3px}
  .metric .sub{color:var(--tx-dim);font-size:13px;margin-top:6px}
  .metric.ok .value{color:var(--ok)} .metric.warn .value{color:var(--warn)}
  .metric.crit .value{color:var(--crit)}

  .bar{height:6px;border-radius:99px;background:var(--panel2);margin-top:11px;overflow:hidden}
  .bar span{display:block;height:100%;border-radius:99px;transition:width .4s ease}

  .kv{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;
      font-size:14px;gap:12px;border-bottom:1px solid rgba(38,44,55,.5)}
  .kv:last-child{border-bottom:0}
  .kv .k{color:var(--tx-dim)} .kv .v{font-weight:600;text-align:right}

  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th{text-align:left;font-size:11px;letter-spacing:.7px;text-transform:uppercase;
     color:var(--tx-mut);font-weight:700;padding:6px 10px 6px 0;border-bottom:1px solid var(--line)}
  td{padding:7px 10px 7px 0;border-bottom:1px solid rgba(38,44,55,.55);vertical-align:top}
  tr:last-child td{border-bottom:0}
  td.num{text-align:right;font-family:ui-monospace,Menlo,Consolas,monospace}

  .tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;font-weight:700;
       letter-spacing:.4px;text-transform:uppercase}
  .tag.ok{background:var(--ok-bg);color:var(--ok)}
  .tag.warn{background:var(--warn-bg);color:var(--warn)}
  .tag.crit{background:var(--crit-bg);color:var(--crit)}
  .tag.info{background:var(--info-bg);color:var(--info)}
  .tag.rw{background:rgba(163,113,247,.14);color:var(--batt)}

  .btn{appearance:none;border:1px solid var(--line);background:var(--panel2);color:var(--tx);
       font:inherit;font-weight:600;font-size:13.5px;padding:9px 15px;border-radius:10px;
       cursor:pointer;transition:border-color .15s,background .15s,transform .05s}
  .btn:hover:not(:disabled){border-color:var(--info);color:var(--info)}
  .btn:active:not(:disabled){transform:translateY(1px)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.danger{border-color:rgba(248,81,73,.35);color:var(--crit)}
  .btn.danger:hover:not(:disabled){background:var(--crit-bg);border-color:var(--crit)}
  .btn.busy{opacity:.6;cursor:progress}
  .btnrow{display:flex;gap:9px;flex-wrap:wrap}
  .btn.cmd{display:inline-flex;align-items:center;gap:9px;padding:10px 16px}
  .btn.cmd svg{flex:0 0 auto;opacity:.85}
  .btn.cmd:hover:not(:disabled) svg{opacity:1}

  .hint{position:fixed;z-index:200;max-width:min(340px,88vw);
    background:linear-gradient(160deg,#1b2230 0%,#141922 100%);
    border:1px solid #2b3444;border-radius:12px;padding:11px 14px;
    font-size:12.5px;line-height:1.5;color:var(--tx-dim);
    box-shadow:0 14px 40px rgba(0,0,0,.55);
    opacity:0;transform:translateY(4px);transition:opacity .14s,transform .14s;
    pointer-events:none}
  .hint.show{opacity:1;transform:none}
  .hint b{color:var(--tx);font-weight:600;font-family:ui-monospace,Menlo,Consolas,monospace}
  .hint em{color:var(--crit);font-style:normal}
  .btnhelp{color:var(--tx-mut);font-size:12.5px;margin:6px 0 16px}

  input[type=text],select{background:var(--bg);border:1px solid var(--line);color:var(--tx);
       font:inherit;font-size:13.5px;padding:7px 10px;border-radius:8px;min-width:120px}
  input[type=text]:focus,select:focus{outline:none;border-color:var(--info)}

  .ranges{display:flex;gap:5px;flex-wrap:wrap}
  .ranges button{appearance:none;background:var(--panel2);border:1px solid var(--line);
       color:var(--tx-dim);font:inherit;font-size:12.5px;font-weight:600;padding:5px 12px;
       border-radius:8px;cursor:pointer}
  .ranges button[aria-pressed="true"]{background:var(--info-bg);color:var(--info);
       border-color:rgba(88,166,255,.35)}

  .cwrap svg{display:block;width:100%;height:auto;user-select:none}
  .legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12.5px;color:var(--tx-dim)}
  .legend i{display:inline-block;width:11px;height:3px;border-radius:2px;margin-right:6px;
            vertical-align:middle}

  .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:50;
     background:var(--panel);border:1px solid var(--line);border-radius:12px;
     padding:12px 18px;font-size:14px;box-shadow:0 8px 30px rgba(0,0,0,.5);
     max-width:min(560px,92vw);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(-4px)}
  .toast.ok{border-color:rgba(63,185,80,.4)} .toast.bad{border-color:rgba(248,81,73,.45)}

  .offline{background:var(--crit-bg);border:1px solid rgba(248,81,73,.35);color:var(--crit);
     padding:13px 17px;border-radius:var(--r);margin-bottom:16px;font-weight:600}
  .muted{color:var(--tx-mut)}
  .refresh{appearance:none;background:none;border:0;padding:0;cursor:pointer;
    font:inherit;font-size:12px;color:var(--tx-mut);display:inline-flex;
    align-items:center;gap:6px}
  .refresh:hover{color:var(--tx-dim)}
  .refresh::before{content:"";width:7px;height:7px;border-radius:50%;
    background:var(--ok);opacity:.85}
  .refresh.paused::before{background:var(--tx-mut);opacity:.6}
  .refresh.busy::before{animation:blink .9s infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
  .note{color:var(--tx-dim);font-size:13px;margin:10px 0 0}

  /* ---- PIN dialog ---- */
  .backdrop{position:fixed;inset:0;z-index:100;display:flex;align-items:center;
    justify-content:center;background:rgba(2,5,10,.72);backdrop-filter:blur(3px);
    padding:20px;animation:fade .16s ease}
  .backdrop[hidden]{display:none}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .pinbox{width:min(380px,94vw);border-radius:20px;padding:28px 26px 22px;text-align:center;
    background:linear-gradient(160deg,#1b2230 0%,#141922 60%,#11151d 100%);
    border:1px solid #2b3444;
    box-shadow:0 24px 70px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.03) inset;
    animation:rise .2s cubic-bezier(.2,.9,.3,1)}
  @keyframes rise{from{opacity:0;transform:translateY(14px) scale(.97)}to{opacity:1;transform:none}}
  .pinicon{width:52px;height:52px;margin:0 auto 14px;border-radius:50%;
    display:flex;align-items:center;justify-content:center;color:var(--info);
    background:radial-gradient(circle at 50% 35%,rgba(88,166,255,.28),rgba(88,166,255,.07));
    border:1px solid rgba(88,166,255,.34)}
  .pinbox h3{margin:0 0 5px;font-size:18px;font-weight:650;letter-spacing:.2px}
  .pinwhat{margin:0 0 20px;color:var(--tx-dim);font-size:13.5px;line-height:1.45;
    word-break:break-word}
  .pinwhat b{color:var(--tx);font-weight:600}
  .pindigits{display:flex;gap:11px;justify-content:center;margin-bottom:6px}
  .pindigits input{width:54px;height:64px;text-align:center;font-size:26px;font-weight:650;
    color:var(--tx);background:#0d1117;border:1.5px solid #2b3444;border-radius:13px;
    caret-color:var(--info);transition:border-color .14s,box-shadow .14s,transform .08s}
  .pindigits input:focus{outline:none;border-color:var(--info);
    box-shadow:0 0 0 3.5px rgba(88,166,255,.16)}
  .pindigits input.filled{border-color:#3d4a5f;background:#111823}
  .pindigits.bad input{border-color:var(--crit);box-shadow:0 0 0 3.5px rgba(248,81,73,.13)}
  .pindigits.bad{animation:shake .32s}
  @keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-8px)}
    40%{transform:translateX(7px)}60%{transform:translateX(-4px)}80%{transform:translateX(3px)}}
  .pinerr{min-height:19px;margin:8px 0 16px;font-size:13px;color:var(--crit)}
  .pinrow{display:flex;gap:10px;justify-content:center}
  .pinrow .btn{min-width:108px;justify-content:center}
  .pinicon.danger{color:var(--crit);
    background:radial-gradient(circle at 50% 35%,rgba(248,81,73,.26),rgba(248,81,73,.07));
    border-color:rgba(248,81,73,.34)}
  .btn.primary.danger{background:var(--crit-bg);border-color:rgba(248,81,73,.45);
    color:var(--crit)}
  .btn.primary.danger:hover:not(:disabled){background:rgba(248,81,73,.2);
    border-color:var(--crit)}
  .btn.primary{background:var(--info-bg);border-color:rgba(88,166,255,.4);color:var(--info)}
  .btn.primary:hover:not(:disabled){background:rgba(88,166,255,.2);border-color:var(--info)}

  /* ---- interactive charts ---- */
  .cwrap{position:relative}
  .cwrap svg{cursor:grab;touch-action:none}
  .cwrap svg.dragging{cursor:grabbing}
  .chint{position:absolute;top:6px;right:8px;font-size:11.5px;color:var(--tx-mut);
    background:rgba(13,17,23,.82);padding:3px 9px;border-radius:7px;pointer-events:none;
    opacity:0;transition:opacity .18s}
  .cwrap:hover .chint{opacity:1}
  .creset{position:absolute;top:4px;left:8px;font-size:11.5px;padding:3px 10px;
    border-radius:7px;border:1px solid var(--line);background:var(--panel2);
    color:var(--tx-dim);cursor:pointer}
  .creset:hover{color:var(--info);border-color:var(--info)}
  .creset[hidden]{display:none}
  .ctip{position:absolute;pointer-events:none;background:rgba(13,17,23,.96);
    border:1px solid var(--line);border-radius:9px;padding:7px 11px;font-size:12.5px;
    white-space:nowrap;opacity:0;transition:opacity .12s;z-index:5;
    box-shadow:0 6px 22px rgba(0,0,0,.45)}
  .ctip.show{opacity:1}
  .ctip .t{color:var(--tx-mut);font-size:11.5px;margin-bottom:3px}
  .ctip .v{font-weight:650}

  /* ---- settings form ---- */
  select.field,input.field{background:#0d1117;border:1px solid var(--line);color:var(--tx);
    font:inherit;font-size:13.5px;padding:8px 10px;border-radius:9px;min-width:132px}
  select.field:focus,input.field:focus{outline:none;border-color:var(--info)}
  .fieldrow{display:flex;align-items:center;gap:8px}
  .fieldunit{color:var(--tx-mut);font-size:12.5px}
  .fieldhelp{color:var(--tx-mut);font-size:12px;margin-top:4px}
  .changed{border-color:var(--warn) !important}

  @media (max-width:900px){
    .charts{grid-template-columns:1fr}
    body{padding-top:18px}
    .metric .value{font-size:26px}
  }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1><?= h($model) ?> <span class="host"><?= h($maker) ?></span></h1>
  <span id="verdict" class="pill <?= h($level) ?>"><span class="dot"></span><?= h($levelText) ?></span>
  <span class="spacer"></span>
  <div class="updated">
    <div id="stamp">—</div>
    <div><button id="autorefresh" class="refresh" type="button"
                 title="click to pause automatic refreshing">auto 5s</button></div>
    <div class="muted">upsmon <?= h($snapshot['version'] ?? '?') ?><?php
      if ($health && isset($health['uptime'])) echo ' · up ' . h(uptime_text((int) $health['uptime']));
    ?></div>
  </div>
</header>

<?php if (!$snapshot): ?>
<div class="offline">
  The monitoring daemon is not responding. Check <span class="mono">systemctl status upsmon</span>.
</div>
<?php elseif (!$online): ?>
<div class="offline">
  The daemon is running but cannot reach the UPS: <?= h($snapshot['error'] ?? 'unknown error') ?>
</div>
<?php endif; ?>

<div class="tabs" role="tablist">
  <button role="tab" aria-selected="true"  data-tab="overview">Overview</button>
  <button role="tab" aria-selected="false" data-tab="history">History</button>
  <button role="tab" aria-selected="false" data-tab="tests">Tests</button>
  <button role="tab" aria-selected="false" data-tab="events">Events</button>
  <button role="tab" aria-selected="false" data-tab="control">Control</button>
  <button role="tab" aria-selected="false" data-tab="all">All variables</button>
</div>

<!-- ==================== OVERVIEW ==================== -->
<section class="panel" id="tab-overview">

  <div class="grid top" id="metrics">
    <div class="card metric" id="m-status">
      <div class="label">Status</div>
      <div class="value" id="v-status">—</div>
      <div class="sub" id="s-status"></div>
    </div>
    <div class="card metric" id="m-charge">
      <div class="label">Battery</div>
      <div class="value"><span id="v-charge">—</span><span class="unit">%</span></div>
      <div class="bar"><span id="b-charge" style="width:0"></span></div>
      <div class="sub" id="s-charge"></div>
    </div>
    <div class="card metric" id="m-runtime">
      <div class="label">Runtime left</div>
      <div class="value" id="v-runtime">—</div>
      <div class="sub" id="s-runtime"></div>
    </div>
    <div class="card metric" id="m-load">
      <div class="label">Load</div>
      <div class="value"><span id="v-load">—</span><span class="unit">%</span></div>
      <div class="bar"><span id="b-load" style="width:0"></span></div>
      <div class="sub" id="s-load"></div>
    </div>
    <div class="card metric" id="m-input">
      <div class="label">Input</div>
      <div class="value"><span id="v-input">—</span><span class="unit">V</span></div>
      <div class="sub" id="s-input"></div>
    </div>
    <div class="card metric" id="m-battv">
      <div class="label">Battery voltage</div>
      <div class="value"><span id="v-battv">—</span><span class="unit">V</span></div>
      <div class="sub" id="s-battv"></div>
    </div>
  </div>

  <div class="grid charts" style="margin-top:14px">
    <div class="card full">
      <h2>Last 24 hours</h2>
      <div class="cwrap" id="chart-quick"></div>
    </div>
  </div>

  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:14px">
    <div class="card">
      <h2>Device</h2>
      <div id="device-facts"></div>
    </div>
    <div class="card">
      <h2>Battery</h2>
      <div id="battery-facts"></div>
    </div>
    <div class="card">
      <h2>Recent events</h2>
      <div id="recent-events" class="muted">loading…</div>
    </div>
  </div>
</section>

<!-- ==================== HISTORY ==================== -->
<section class="panel" id="tab-history" hidden>
  <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px">
    <div class="ranges" id="ranges">
      <button data-range="6h">6 hours</button>
      <button data-range="24h" aria-pressed="true">24 hours</button>
      <button data-range="7d">7 days</button>
      <button data-range="30d">30 days</button>
      <button data-range="90d">90 days</button>
      <button data-range="1y">1 year</button>
    </div>
    <span class="spacer"></span>
    <span class="muted" id="range-info"></span>
  </div>

  <div class="grid charts">
    <div class="card"><h2>Battery charge</h2><div class="cwrap" id="chart-charge"></div></div>
    <div class="card"><h2>Runtime left</h2><div class="cwrap" id="chart-runtime"></div></div>
    <div class="card"><h2>Load</h2><div class="cwrap" id="chart-load"></div></div>
    <div class="card"><h2>Mains voltage</h2><div class="cwrap" id="chart-input"></div></div>
    <div class="card"><h2>Battery voltage</h2><div class="cwrap" id="chart-battv"></div></div>
    <div class="card"><h2>Power drawn</h2><div class="cwrap" id="chart-power"></div></div>
  </div>

  <div class="card full" style="margin-top:14px">
    <h2>Power failures</h2>
    <div id="outages" class="muted">loading…</div>
  </div>
</section>

<!-- ==================== TESTS ==================== -->
<section class="panel" id="tab-tests" hidden>

  <div class="grid" style="grid-template-columns:minmax(280px,1fr) minmax(320px,2fr);gap:14px">
    <div class="card" id="test-latest">
      <h2>Last self test</h2>
      <div class="muted">loading…</div>
    </div>
    <div class="card">
      <h2>Run a test</h2>
      <p class="btnhelp">The UPS switches the load to battery for a few seconds
         and reports a verdict. A quick test is harmless at any charge; a deep
         one drains the battery properly, so leave it until the battery is full.</p>
      <div class="btnrow" id="test-buttons"><span class="muted">loading…</span></div>
      <div id="test-running" hidden style="margin-top:14px">
        <span class="tag warn">running</span>
        <span class="muted" id="test-running-text">the daemon is following it;
          the verdict appears here when the UPS reports it</span>
      </div>
    </div>
  </div>

  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:14px">
    <div class="card" id="test-summary"><h2>Record</h2><div class="muted">loading…</div></div>
    <div class="card full">
      <h2>Battery voltage under load, test by test</h2>
      <p class="btnhelp">How far the battery sagged while carrying the load. This
         says more about its health than pass or fail does — a tired battery
         still passes a short test, but sags further each time.</p>
      <div class="cwrap" id="chart-tests"></div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>History</h2>
    <div id="tests-table" class="muted">loading…</div>
  </div>
</section>

<!-- ==================== EVENTS ==================== -->
<section class="panel" id="tab-events" hidden>
  <div class="card">
    <h2>Event log</h2>
    <p class="btnhelp">Status changes, self tests, settings edited on the UPS, and
       every command this dashboard has run.</p>
    <div id="events-table" class="muted">loading…</div>
  </div>
</section>

<!-- ==================== CONTROL ==================== -->
<section class="panel" id="tab-control" hidden>
  <div class="card">
    <h2>Commands</h2>
    <p class="btnhelp">These are the instant commands this UPS reports. What is
       offered differs by model — a cheaper unit may support none at all.</p>
    <div id="commands" class="muted">loading…</div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Recorded data</h2>
    <p class="btnhelp">Clearing history affects only what this machine has
       stored — the UPS itself is untouched, and recording carries straight on.
       There is no undo.</p>
    <div id="reset-counts" class="muted" style="margin-bottom:14px">loading…</div>
    <div class="btnrow" id="reset-buttons"></div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Settings</h2>
    <p class="btnhelp">Writable variables, with the values the UPS will accept.
       Changes go into the UPS itself; whether they survive a power cycle depends
       on the model.</p>
    <div id="writable" class="muted">loading…</div>
  </div>
</section>

<!-- ==================== ALL VARIABLES ==================== -->
<section class="panel" id="tab-all" hidden>
  <div class="card">
    <h2>Everything the UPS reports</h2>
    <p class="btnhelp">Straight from the NUT driver, grouped by subject.
       <span class="tag rw">rw</span> marks the ones that can be changed.</p>
    <div id="all-vars" class="muted">loading…</div>
  </div>
</section>

</div><!-- /wrap -->

<!-- PIN dialog -->
<div class="backdrop" id="askback" hidden>
  <div class="pinbox" role="dialog" aria-modal="true" aria-labelledby="asktitle">
    <div class="pinicon danger" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3.8 2.6 19.2a1.4 1.4 0 0 0 1.2 2.1h16.4a1.4 1.4 0 0 0 1.2-2.1z"/>
        <path d="M12 9.5v4.4"/><path d="M12 17.2h.01"/>
      </svg>
    </div>
    <h3 id="asktitle">Are you sure?</h3>
    <p class="pinwhat" id="askwhat"></p>
    <div class="pinrow">
      <button class="btn" id="askcancel">Cancel</button>
      <button class="btn primary danger" id="askok">Confirm</button>
    </div>
  </div>
</div>

<div class="backdrop" id="pinback" hidden>
  <div class="pinbox" role="dialog" aria-modal="true" aria-labelledby="pintitle">
    <div class="pinicon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <rect x="4" y="10.5" width="16" height="10" rx="2.5"/>
        <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>
      </svg>
    </div>
    <h3 id="pintitle">Enter PIN</h3>
    <p class="pinwhat" id="pinwhat"></p>
    <div class="pindigits" id="pindigits">
      <input type="password" inputmode="numeric" maxlength="1" autocomplete="off" aria-label="digit 1">
      <input type="password" inputmode="numeric" maxlength="1" autocomplete="off" aria-label="digit 2">
      <input type="password" inputmode="numeric" maxlength="1" autocomplete="off" aria-label="digit 3">
      <input type="password" inputmode="numeric" maxlength="1" autocomplete="off" aria-label="digit 4">
    </div>
    <p class="pinerr" id="pinerr"></p>
    <div class="pinrow">
      <button class="btn" id="pincancel">Cancel</button>
      <button class="btn primary" id="pinok" disabled>Confirm</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
"use strict";
// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
var STATE = { snapshot: null, range: "24h", history: null, tab: "overview" };
var LAST = { quick: 0, history: 0, events: 0, capabilities: 0, tests: 0 };

function $(id) { return document.getElementById(id); }
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function num(v) {
  if (v === null || v === undefined || v === "") return null;
  var n = parseFloat(v);
  return isNaN(n) ? null : n;
}
function fmt(v, digits) {
  var n = num(v);
  return n === null ? "—" : n.toFixed(digits === undefined ? 0 : digits);
}
function runtimeText(seconds) {
  var n = num(seconds);
  if (n === null) return "—";
  var t = Math.max(0, Math.round(n)), h = Math.floor(t / 3600),
      m = Math.floor((t % 3600) / 60), s = t % 60;
  if (h) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m) return m + "m " + String(s).padStart(2, "0") + "s";
  return s + "s";
}
function ago(ts) {
  var d = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (d < 60) return d + "s ago";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
function stamp(ts) {
  var d = new Date(ts * 1000);
  return d.toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit",
                                hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function toast(message, ok) {
  var t = $("toast");
  t.textContent = message;
  t.className = "toast show " + (ok ? "ok" : "bad");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(function () { t.className = "toast"; }, ok ? 5000 : 9000);
}
function api(what, options) {
  return fetch("?api=" + what, options).then(function (r) {
    return r.json().catch(function () { return { ok: false, error: "bad reply" }; });
  });
}

// ---------------------------------------------------------------------------
// Charts — inline SVG, no libraries
// ---------------------------------------------------------------------------
// Every chart keeps its own view window over the full dataset it was given.
// Dragging moves that window, the wheel zooms it around the pointer, and a
// double click puts it back. Nothing is re-fetched while panning: the data is
// already in the browser, so it stays responsive on a phone.
var CHART_STATE = {};   // container id -> {rows, spec, from, to}

function drawChart(container, spec) {
  var id = container.id || (container.id = 'c' + Math.random().toString(36).slice(2));
  var rows = spec.rows || [];
  if (!rows.length) {
    container.innerHTML = '<p class="muted">No data for this period yet.</p>';
    delete CHART_STATE[id];
    return;
  }
  var state = CHART_STATE[id];
  var keepView = state && state.signature === chartSignature(rows) && spec.keepView !== false;
  CHART_STATE[id] = {
    rows: rows, spec: spec, signature: chartSignature(rows),
    from: keepView ? state.from : rows[0].ts,
    to:   keepView ? state.to   : rows[rows.length - 1].ts
  };
  renderChart(id);
}

function chartSignature(rows) {
  return rows.length + ':' + rows[0].ts + ':' + rows[rows.length - 1].ts;
}

function renderChart(id) {
  var state = CHART_STATE[id];
  if (!state) return;
  var container = $(id), spec = state.spec, all = state.rows;
  var W = 700, H = 240, padL = 48, padR = 12, padT = 12, padB = 26;

  var t0 = state.from, t1 = state.to;
  if (t1 - t0 < 60) t1 = t0 + 60;

  // One point either side of the window so lines reach the edges instead of
  // stopping short of them.
  var first = 0, last = all.length - 1, i;
  for (i = 0; i < all.length; i++) { if (all[i].ts >= t0) { first = Math.max(0, i - 1); break; } }
  for (i = all.length - 1; i >= 0; i--) { if (all[i].ts <= t1) { last = Math.min(all.length - 1, i + 1); break; } }
  var rows = all.slice(first, last + 1);
  if (!rows.length) rows = all;

  var lo = null, hi = null;
  spec.series.forEach(function (s) {
    rows.forEach(function (r) {
      var v = num(r[s.key]);
      if (v === null) return;
      lo = (lo === null || v < lo) ? v : lo;
      hi = (hi === null || v > hi) ? v : hi;
    });
  });
  if (lo === null) {
    container.innerHTML = '<p class="muted">' +
      (spec.empty || "Nothing recorded for this metric.") + '</p>';
    return;
  }
  if (spec.min !== undefined && spec.min !== null) lo = Math.min(lo, spec.min);
  if (spec.max !== undefined && spec.max !== null) hi = Math.max(hi, spec.max);
  var pad = spec.pad || 1;
  if (hi - lo < pad) { var mid = (hi + lo) / 2; lo = mid - pad / 2; hi = mid + pad / 2; }
  var span = hi - lo;
  lo -= span * 0.08; hi += span * 0.08;

  var plotW = W - padL - padR, plotH = H - padT - padB;
  function px(ts) { return padL + (ts - t0) / (t1 - t0) * plotW; }
  function py(v)  { return padT + plotH - (v - lo) / (hi - lo) * plotH; }

  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">';
  svg += '<defs><clipPath id="clip-' + id + '"><rect x="' + padL + '" y="' + padT
       + '" width="' + plotW + '" height="' + plotH + '"/></clipPath></defs>';

  // periods spent on battery
  svg += '<g clip-path="url(#clip-' + id + ')">';
  var runStart = null;
  rows.forEach(function (r, n) {
    var onBattery = (r.status || '').indexOf('OB') >= 0;
    if (onBattery && runStart === null) runStart = r.ts;
    if ((!onBattery || n === rows.length - 1) && runStart !== null) {
      var x1 = px(runStart), x2 = px(r.ts);
      if (x2 - x1 < 1.5) x2 = x1 + 1.5;
      svg += '<rect x="' + x1.toFixed(1) + '" y="' + padT + '" width="' + (x2 - x1).toFixed(1)
           + '" height="' + plotH + '" fill="rgba(248,81,73,.16)"/>';
      runStart = null;
    }
  });
  svg += '</g>';

  for (var g = 0; g <= 4; g++) {
    var value = lo + (hi - lo) * (g / 4), y = py(value);
    svg += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y.toFixed(1)
         + '" y2="' + y.toFixed(1) + '" stroke="#262c37"/>';
    svg += '<text x="' + (padL - 7) + '" y="' + (y + 3.5).toFixed(1)
         + '" text-anchor="end" font-size="10.5" fill="#5b6472">'
         + value.toFixed(spec.digits === undefined ? 0 : spec.digits) + '</text>';
  }
  [0, 0.5, 1].forEach(function (f) {
    var ts = t0 + (t1 - t0) * f, x = px(ts), d = new Date(ts * 1000);
    var range = t1 - t0;
    var label = range > 172800
      ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
      : range > 3600
        ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    svg += '<text x="' + x.toFixed(1) + '" y="' + (H - 7) + '" font-size="10.5" fill="#5b6472"'
         + ' text-anchor="' + (f === 0 ? 'start' : f === 1 ? 'end' : 'middle') + '">'
         + label + '</text>';
  });

  svg += '<g clip-path="url(#clip-' + id + ')">';
  spec.series.forEach(function (s) {
    var segment = [];
    function flush() {
      if (segment.length > 1) {
        svg += '<polyline points="' + segment.join(' ') + '" fill="none" stroke="' + s.color
             + '" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"'
             + ' vector-effect="non-scaling-stroke"/>';
      } else if (segment.length === 1) {
        var p = segment[0].split(',');
        svg += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="2.2" fill="' + s.color + '"/>';
      }
      segment = [];
    }
    rows.forEach(function (r) {
      var v = num(r[s.key]);
      if (v === null) { flush(); return; }
      segment.push(px(r.ts).toFixed(1) + ',' + py(v).toFixed(1));
    });
    flush();
  });
  svg += '</g>';
  svg += '<line class="cross" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB)
       + '" stroke="#3d4a5f" stroke-dasharray="3 3" opacity="0"/>';
  svg += '</svg>';

  var legend = '<div class="legend">';
  spec.series.forEach(function (s) {
    legend += '<span><i style="background:' + s.color + '"></i>' + s.label + '</span>';
  });
  if (spec.unit) legend += '<span class="muted">' + spec.unit + '</span>';
  legend += '</div>';

  var full = (t0 <= all[0].ts && t1 >= all[all.length - 1].ts);
  container.innerHTML = svg
    + '<button class="creset"' + (full ? ' hidden' : '') + '>reset view</button>'
    + '<span class="chint">drag to pan · wheel to zoom · double-click to reset</span>'
    + '<div class="ctip"></div>'
    + legend;

  wireChart(id, { t0: t0, t1: t1, padL: padL, padR: padR, padT: padT, padB: padB,
                  W: W, H: H, rows: rows, spec: spec });
}

function wireChart(id, view) {
  var container = $(id);
  var svg = container.querySelector('svg');
  var tip = container.querySelector('.ctip');
  var cross = container.querySelector('.cross');
  var reset = container.querySelector('.creset');
  if (!svg) return;
  var state = CHART_STATE[id];

  function tsAt(clientX) {
    var box = svg.getBoundingClientRect();
    var frac = (clientX - box.left) / box.width;          // 0..1 across the svg
    var plotFrom = view.padL / view.W, plotTo = (view.W - view.padR) / view.W;
    var inPlot = (frac - plotFrom) / (plotTo - plotFrom);
    return view.t0 + inPlot * (view.t1 - view.t0);
  }

  // ---- hover readout ----
  svg.addEventListener('mousemove', function (e) {
    if (svg.classList.contains('dragging')) return;
    var ts = tsAt(e.clientX);
    var nearest = null, best = Infinity;
    view.rows.forEach(function (r) {
      var d = Math.abs(r.ts - ts);
      if (d < best) { best = d; nearest = r; }
    });
    if (!nearest) return;
    var box = svg.getBoundingClientRect();
    var x = (e.clientX - box.left);
    cross.setAttribute('x1', (x / box.width * view.W).toFixed(1));
    cross.setAttribute('x2', (x / box.width * view.W).toFixed(1));
    cross.setAttribute('opacity', '1');
    var html = '<div class="t">' + stamp(nearest.ts) + '</div>';
    view.spec.series.forEach(function (s) {
      var v = num(nearest[s.key]);
      html += '<div class="v" style="color:' + s.color + '">' + s.label + ': '
           + (v === null ? '—'
              : s.key === 'runtime' ? runtimeText(v)
              : v.toFixed(view.spec.digits === undefined ? 0 : view.spec.digits))
           + '</div>';
    });
    if (nearest.status) html += '<div class="t">' + nearest.status + '</div>';
    tip.innerHTML = html;
    tip.classList.add('show');
    var wrapBox = container.getBoundingClientRect();
    var left = e.clientX - wrapBox.left + 14;
    if (left + tip.offsetWidth > wrapBox.width) left = e.clientX - wrapBox.left - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + 'px';
    tip.style.top = Math.max(0, e.clientY - wrapBox.top - tip.offsetHeight - 12) + 'px';
  });
  svg.addEventListener('mouseleave', function () {
    tip.classList.remove('show');
    cross.setAttribute('opacity', '0');
  });

  // ---- drag to pan ----
  var dragFrom = null;
  svg.addEventListener('pointerdown', function (e) {
    dragFrom = { x: e.clientX, t0: state.from, t1: state.to };
    svg.classList.add('dragging');
    svg.setPointerCapture(e.pointerId);
    tip.classList.remove('show');
  });
  svg.addEventListener('pointermove', function (e) {
    if (!dragFrom) return;
    var box = svg.getBoundingClientRect();
    var plotWidth = box.width * (view.W - view.padL - view.padR) / view.W;
    var perPixel = (dragFrom.t1 - dragFrom.t0) / plotWidth;
    var shift = -(e.clientX - dragFrom.x) * perPixel;
    applyView(id, dragFrom.t0 + shift, dragFrom.t1 + shift);
  });
  function endDrag(e) {
    if (!dragFrom) return;
    dragFrom = null;
    svg.classList.remove('dragging');
    try { svg.releasePointerCapture(e.pointerId); } catch (err) {}
    renderChart(id);
  }
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);

  // ---- wheel to zoom, anchored on the pointer ----
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    var anchor = tsAt(e.clientX);
    var factor = e.deltaY > 0 ? 1.25 : 0.8;
    var from = anchor - (anchor - state.from) * factor;
    var to   = anchor + (state.to - anchor) * factor;
    applyView(id, from, to);
    renderChart(id);
  }, { passive: false });

  svg.addEventListener('dblclick', function () { resetView(id); });
  if (reset) reset.onclick = function () { resetView(id); };

  // ---- pinch to zoom on a touch screen ----
  var pinch = null;
  svg.addEventListener('touchstart', function (e) {
    if (e.touches.length === 2) {
      pinch = { distance: touchGap(e), t0: state.from, t1: state.to };
    }
  }, { passive: true });
  svg.addEventListener('touchmove', function (e) {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();
    var factor = pinch.distance / Math.max(1, touchGap(e));
    var middle = (pinch.t0 + pinch.t1) / 2;
    var half = (pinch.t1 - pinch.t0) / 2 * factor;
    applyView(id, middle - half, middle + half);
    renderChart(id);
  }, { passive: false });
  svg.addEventListener('touchend', function () { pinch = null; });
}

function touchGap(e) {
  var dx = e.touches[0].clientX - e.touches[1].clientX;
  var dy = e.touches[0].clientY - e.touches[1].clientY;
  return Math.sqrt(dx * dx + dy * dy);
}

// Keep the window inside the data and never smaller than a minute, so it
// cannot be zoomed into nothing or dragged off into empty space.
function applyView(id, from, to) {
  var state = CHART_STATE[id];
  if (!state) return;
  var rows = state.rows;
  var firstTs = rows[0].ts, lastTs = rows[rows.length - 1].ts;
  var full = (lastTs - firstTs) || 60;
  // A minute is the floor and the whole dataset is the ceiling, so the view can
  // neither collapse to a single instant nor drift off into empty space.
  var width = Math.max(60, Math.min(to - from, full));
  var middle = (from + to) / 2;
  from = middle - width / 2;
  to = from + width;
  if (from < firstTs) { from = firstTs; to = from + width; }
  if (to > lastTs)    { to = lastTs;    from = Math.max(firstTs, to - width); }
  state.from = from;
  state.to = to;
}

function resetView(id) {
  var state = CHART_STATE[id];
  if (!state) return;
  state.from = state.rows[0].ts;
  state.to = state.rows[state.rows.length - 1].ts;
  renderChart(id);
}

// ---------------------------------------------------------------------------
// Rendering the snapshot
// ---------------------------------------------------------------------------
var STATUS_TEXT = {
  OL: "on line", OB: "on battery", LB: "low battery", HB: "high battery",
  RB: "replace battery", CHRG: "charging", DISCHRG: "discharging",
  BYPASS: "bypass", CAL: "calibrating", OFF: "output off", OVER: "overloaded",
  TRIM: "trimming voltage", BOOST: "boosting voltage", FSD: "forced shutdown",
  ALARM: "alarm"
};
var CRIT_FLAGS = ["OB", "LB", "RB", "OVER", "FSD", "ALARM", "OFF"];
var WARN_FLAGS = ["DISCHRG", "BYPASS", "CAL", "TRIM", "BOOST"];

function levelFor(value, warnAt, critAt, invert) {
  var n = num(value);
  if (n === null) return "";
  if (invert) return n >= critAt ? "crit" : n >= warnAt ? "warn" : "ok";
  return n <= critAt ? "crit" : n <= warnAt ? "warn" : "ok";
}
function paintBar(id, percent, level) {
  var colour = level === "crit" ? "var(--crit)" : level === "warn" ? "var(--warn)" : "var(--ok)";
  var bar = $(id);
  bar.style.width = Math.max(0, Math.min(100, percent || 0)) + "%";
  bar.style.background = colour;
}

function renderSnapshot(snap) {
  STATE.snapshot = snap;
  var v = snap.vars || {}, th = snap.thresholds || {};

  var verdict = $("verdict");
  verdict.className = "pill " + (snap.level || "unknown");
  verdict.innerHTML = '<span class="dot"></span>' +
    ({ ok: "All good", warn: "Needs attention", crit: "Problem" }[snap.level] || "No data");

  $("stamp").textContent = snap.generated ? stamp(snap.generated) + " · " + ago(snap.generated) : "—";
  setFavicon(snap.level || "unknown");

  // status
  var flags = (v["ups.status"] || "").split(" ").filter(Boolean);
  var critical = flags.some(function (f) { return CRIT_FLAGS.indexOf(f) >= 0; });
  var warning = flags.some(function (f) { return WARN_FLAGS.indexOf(f) >= 0; });
  $("v-status").textContent = v["ups.status"] || "—";
  $("m-status").className = "card metric " + (critical ? "crit" : warning ? "warn" : "ok");
  $("s-status").textContent = snap.status_text || "";

  // charge
  var charge = num(v["battery.charge"]);
  var chargeLevel = levelFor(charge, th.charge_warn, th.charge_crit, false);
  $("v-charge").textContent = fmt(charge, 0);
  $("m-charge").className = "card metric " + chargeLevel;
  paintBar("b-charge", charge, chargeLevel);
  $("s-charge").textContent = v["battery.charge.low"]
    ? "low battery below " + v["battery.charge.low"] + "%" : "";

  // runtime
  var runtime = num(v["battery.runtime"]);
  var runtimeLevel = flags.indexOf("OB") >= 0
    ? levelFor(runtime, th.runtime_warn_s, th.runtime_crit_s, false) : "";
  $("v-runtime").textContent = runtimeText(runtime);
  $("m-runtime").className = "card metric " + runtimeLevel;
  $("s-runtime").textContent = v["battery.runtime.low"]
    ? "shutdown below " + runtimeText(v["battery.runtime.low"]) : "";

  // load
  var load = num(v["ups.load"]);
  var loadLevel = levelFor(load, th.load_warn, th.load_crit, true);
  $("v-load").textContent = fmt(load, 0);
  $("m-load").className = "card metric " + loadLevel;
  paintBar("b-load", load, loadLevel);
  var watts = num(v["ups.realpower"]);
  var nominal = num(v["ups.realpower.nominal"]);
  if (watts === null && load !== null && nominal !== null) watts = Math.round(nominal * load / 100);
  $("s-load").textContent = watts !== null
    ? watts + " W" + (nominal ? " of " + nominal + " W" : "") : "";

  // input
  $("v-input").textContent = fmt(v["input.voltage"], 0);
  $("s-input").textContent = [
    v["input.frequency"] ? v["input.frequency"] + " Hz" : "",
    v["input.transfer.low"] && v["input.transfer.high"]
      ? "transfers outside " + v["input.transfer.low"] + "–" + v["input.transfer.high"] + " V" : ""
  ].filter(Boolean).join(" · ");

  // battery voltage
  $("v-battv").textContent = fmt(v["battery.voltage"], 1);
  $("s-battv").textContent = v["battery.voltage.nominal"]
    ? "nominal " + v["battery.voltage.nominal"] + " V" : "";

  // facts
  var device = [];
  [["Model", "device.model"], ["Manufacturer", "device.mfr"], ["Serial", "device.serial"],
   ["Firmware", "ups.firmware"], ["Made", "ups.mfr.date"],
   ["Rated power", "ups.realpower.nominal"], ["Rated VA", "ups.power.nominal"],
   ["Driver", "driver.name"], ["Driver data", "driver.version.data"]].forEach(function (pair) {
    if (v[pair[1]] === undefined) return;
    var unit = pair[1] === "ups.realpower.nominal" ? " W"
             : pair[1] === "ups.power.nominal" ? " VA" : "";
    device.push([pair[0], v[pair[1]] + unit]);
  });
  renderKV($("device-facts"), device);

  var battery = [];
  if (snap.battery_age) {
    var age = snap.battery_age;
    var years = age.years, life = age.life_years || 4;
    var text = age.raw + " · " + (years < 1
      ? Math.round(age.days / 30.44) + " months old" : years.toFixed(1) + " years old");
    battery.push(["Date", text, years >= life ? "crit" : years >= life - 0.5 ? "warn" : "ok"]);
  }
  [["Chemistry", "battery.type"], ["Voltage", "battery.voltage"],
   ["Nominal voltage", "battery.voltage.nominal"], ["Low threshold", "battery.charge.low"],
   ["Runtime threshold", "battery.runtime.low"], ["Last self test", "ups.test.result"],
   ["Beeper", "ups.beeper.status"]].forEach(function (pair) {
    if (v[pair[1]] === undefined) return;
    var value = v[pair[1]];
    if (pair[1] === "battery.runtime.low") value = runtimeText(value);
    if (pair[1] === "battery.charge.low") value = value + " %";
    if (pair[1].indexOf("voltage") >= 0) value = value + " V";
    battery.push([pair[0], value]);
  });
  renderKV($("battery-facts"), battery);

  if (snap.issues && snap.issues.length) {
    var box = el("div");
    snap.issues.forEach(function (issue) {
      var row = el("div", "kv");
      row.appendChild(el("span", "k", "Attention"));
      var tag = el("span", "v");
      tag.appendChild(el("span", "tag " + issue.level, issue.text));
      row.appendChild(tag);
      box.appendChild(row);
    });
    $("battery-facts").appendChild(box);
  }
}

function renderKV(host, pairs) {
  host.innerHTML = "";
  if (!pairs.length) { host.appendChild(el("p", "muted", "nothing reported")); return; }
  pairs.forEach(function (pair) {
    var row = el("div", "kv");
    row.appendChild(el("span", "k", pair[0]));
    var value = el("span", "v", pair[1]);
    if (pair[2]) { value.textContent = ""; value.appendChild(el("span", "tag " + pair[2], pair[1])); }
    row.appendChild(value);
    host.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------
var CHARTS = [
  { id: "chart-charge",  key: "charge",    label: "Charge",  colour: "var(--ok)",   unit: "%", min: 0, max: 100, digits: 0 },
  { id: "chart-runtime", key: "runtime",   label: "Runtime", colour: "var(--info)", unit: "seconds", min: 0, digits: 0 },
  { id: "chart-load",    key: "load",      label: "Load",    colour: "var(--load)", unit: "%", min: 0, digits: 0 },
  { id: "chart-input",   key: "input_v",   label: "Mains",   colour: "var(--info)", unit: "volts", digits: 0 },
  { id: "chart-battv",   key: "battery_v", label: "Battery", colour: "var(--batt)", unit: "volts", digits: 1 },
  { id: "chart-power",   key: "realpower", label: "Power",   colour: "var(--warn)", unit: "watts", min: 0, digits: 0, derived: "power" }
];

function loadHistory(range) {
  STATE.range = range;
  $("range-info").textContent = "loading…";
  return api("history&range=" + encodeURIComponent(range) + "&points=700").then(function (data) {
    var rows = (data && data.samples) || [];
    STATE.history = rows;
    if (!rows.length) {
      $("range-info").textContent = "nothing recorded yet for this period";
    } else {
      var days = (rows[rows.length - 1].ts - rows[0].ts) / 86400;
      $("range-info").textContent = rows.length + " points over " +
        (days >= 1 ? days.toFixed(1) + " days" : ((rows[rows.length - 1].ts - rows[0].ts) / 3600).toFixed(1) + " hours");
    }
    CHARTS.forEach(function (chart) {
      var host = $(chart.id);
      if (!host) return;
      var spec = {
        rows: rows, min: chart.min, max: chart.max, digits: chart.digits,
        unit: chart.unit,
        series: [{ key: chart.key, label: chart.label, color: chart.colour }]
      };
      if (chart.derived === "power") applyPowerFallback(spec, rows);
      drawChart(host, spec);
    });
    return rows;
  });
}

// Many models publish only their nameplate rating, never the watts actually
// being drawn. Rather than leaving the chart blank, work it out from the load
// percentage the same way the Load tile does — and label it as an estimate,
// because that is what it is.
function applyPowerFallback(spec, rows) {
  var measured = rows.some(function (r) { return num(r.realpower) !== null; });
  if (measured) return;

  var vars = (STATE.snapshot && STATE.snapshot.vars) || {};
  var nominal = num(vars["ups.realpower.nominal"]);
  var apparent = num(vars["ups.power.nominal"]);
  // A VA rating is not a watt rating; 0.6 is the usual power factor these
  // consumer units are built to.
  if (nominal === null && apparent !== null) nominal = apparent * 0.6;

  if (nominal === null) {
    spec.empty = "This UPS reports neither the power drawn nor a power rating, "
      + "so there is nothing to chart or to work it out from.";
    return;
  }
  if (!rows.some(function (r) { return num(r.load) !== null; })) {
    spec.empty = "This UPS does not report the power drawn, and without a load "
      + "reading it cannot be worked out either.";
    return;
  }

  spec.rows = rows.map(function (r) {
    var load = num(r.load);
    var copy = { ts: r.ts, status: r.status };
    copy.power_est = load === null ? null : Math.round(nominal * load / 100);
    return copy;
  });
  spec.series = [{ key: "power_est", label: "Power (estimated)", color: "var(--warn)" }];
  spec.unit = "watts, worked out from load against a " + Math.round(nominal)
            + " W rating — this UPS does not measure it";
}

function loadQuickChart() {
  return api("history&range=24h&points=400").then(function (data) {
    var rows = (data && data.samples) || [];
    drawChart($("chart-quick"), {
      rows: rows, min: 0, max: 100, digits: 0, unit: "percent · red bands are power failures",
      series: [
        { key: "charge", label: "Battery charge", color: "var(--ok)" },
        { key: "load",   label: "Load",           color: "var(--load)" }
      ]
    });
  });
}

function loadOutages() {
  return api("outages").then(function (data) {
    var rows = (data && data.outages) || [];
    var host = $("outages");
    host.innerHTML = "";
    if (!rows.length) {
      host.appendChild(el("p", "muted", "No power failure has been recorded. "
        + "A self test does not count — the mains never actually went away."));
      return;
    }
    var table = el("table");
    table.innerHTML = "<thead><tr><th>Started</th><th>Lasted</th><th>Charge</th>"
      + "<th>Lowest</th><th>Battery volts</th></tr></thead>";
    var body = el("tbody");
    rows.forEach(function (o) {
      var tr = el("tr");
      tr.appendChild(el("td", null, stamp(o.started)));
      tr.appendChild(el("td", null, o.ended ? runtimeText(o.ended - o.started)
                                            : "still running"));
      tr.appendChild(el("td", "num", o.charge_start !== null
        ? fmt(o.charge_start, 0) + "% → " + (o.charge_end !== null ? fmt(o.charge_end, 0) + "%" : "?")
        : "—"));
      tr.appendChild(el("td", "num", o.min_charge !== null ? fmt(o.min_charge, 0) + "%" : "—"));
      tr.appendChild(el("td", "num", o.min_battery_v !== null ? fmt(o.min_battery_v, 1) + " V" : "—"));
      body.appendChild(tr);
    });
    table.appendChild(body);
    host.appendChild(table);
  });
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
function eventTable(rows, limit) {
  var table = el("table");
  table.innerHTML = "<thead><tr><th>When</th><th>Kind</th><th>What happened</th></tr></thead>";
  var body = el("tbody");
  rows.slice(0, limit || rows.length).forEach(function (e) {
    var tr = el("tr");
    var when = el("td");
    when.appendChild(el("div", null, stamp(e.ts)));
    when.appendChild(el("div", "muted", ago(e.ts)));
    tr.appendChild(when);
    var kind = el("td");
    kind.appendChild(el("span", "tag " + (e.level === "crit" ? "crit"
      : e.level === "warn" ? "warn" : "info"), e.kind));
    tr.appendChild(kind);
    tr.appendChild(el("td", null, e.detail));
    body.appendChild(tr);
  });
  table.appendChild(body);
  return table;
}

function loadEvents() {
  return api("events&limit=200").then(function (data) {
    var rows = (data && data.events) || [];
    var full = $("events-table"), recent = $("recent-events");
    full.innerHTML = "";
    recent.innerHTML = "";
    if (!rows.length) {
      full.appendChild(el("p", "muted", "Nothing logged yet."));
      recent.appendChild(el("p", "muted", "Nothing logged yet."));
      return;
    }
    full.appendChild(eventTable(rows));
    var list = el("div");
    rows.slice(0, 6).forEach(function (e) {
      var row = el("div", "kv");
      var left = el("span", "k");
      left.appendChild(el("span", "tag " + (e.level === "crit" ? "crit"
        : e.level === "warn" ? "warn" : "info"), e.kind));
      left.appendChild(document.createTextNode(" " + e.detail));
      row.appendChild(left);
      row.appendChild(el("span", "v muted", ago(e.ts)));
      list.appendChild(row);
    });
    recent.appendChild(list);
  });
}

// ---------------------------------------------------------------------------
// PIN dialog
// ---------------------------------------------------------------------------
// Asked for before anything is written or run, when the daemon says one is
// configured. It is not a password — four digits stops a misclick and a
// passer-by, and the daemon's lockout is what stops anything more determined.
var PIN = { resolve: null, digits: null };

function pinInputs() {
  return Array.prototype.slice.call($('pindigits').querySelectorAll('input'));
}

function askPin(what) {
  // Resolves with the four digits, or null if the person backed out.
  return new Promise(function (resolve) {
    if (!(STATE.snapshot && STATE.snapshot.pin_required)) { resolve(''); return; }
    PIN.resolve = resolve;
    $('pinwhat').innerHTML = what;
    $('pinerr').textContent = '';
    $('pindigits').classList.remove('bad');
    pinInputs().forEach(function (input) { input.value = ''; input.classList.remove('filled'); });
    $('pinok').disabled = true;
    $('pinback').hidden = false;
    setTimeout(function () { pinInputs()[0].focus(); }, 40);
  });
}

function closePin(value) {
  $('pinback').hidden = true;
  var resolve = PIN.resolve;
  PIN.resolve = null;
  if (resolve) resolve(value);
}

function pinValue() {
  return pinInputs().map(function (input) { return input.value; }).join('');
}

function pinShake(message) {
  $('pinerr').textContent = message;
  var box = $('pindigits');
  box.classList.add('bad');
  setTimeout(function () { box.classList.remove('bad'); }, 400);
  pinInputs().forEach(function (input) { input.value = ''; input.classList.remove('filled'); });
  $('pinok').disabled = true;
  pinInputs()[0].focus();
}

(function wirePin() {
  var inputs = pinInputs();
  inputs.forEach(function (input, index) {
    input.addEventListener('input', function () {
      // Keep digits only; typing over a filled box replaces it.
      input.value = input.value.replace(/\D/g, '').slice(-1);
      input.classList.toggle('filled', input.value !== '');
      if (input.value && index < inputs.length - 1) inputs[index + 1].focus();
      $('pinok').disabled = pinValue().length !== 4;
      if (pinValue().length === 4) $('pinok').focus();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Backspace' && !input.value && index > 0) {
        inputs[index - 1].focus();
        inputs[index - 1].value = '';
        inputs[index - 1].classList.remove('filled');
        e.preventDefault();
      } else if (e.key === 'ArrowLeft' && index > 0) {
        inputs[index - 1].focus();
      } else if (e.key === 'ArrowRight' && index < inputs.length - 1) {
        inputs[index + 1].focus();
      } else if (e.key === 'Enter' && pinValue().length === 4) {
        closePin(pinValue());
      } else if (e.key === 'Escape') {
        closePin(null);
      }
    });
    // Pasting the whole PIN into any box fills them all.
    input.addEventListener('paste', function (e) {
      var text = (e.clipboardData || window.clipboardData).getData('text') || '';
      var digits = text.replace(/\D/g, '').slice(0, 4);
      if (!digits) return;
      e.preventDefault();
      inputs.forEach(function (box, n) {
        box.value = digits[n] || '';
        box.classList.toggle('filled', !!digits[n]);
      });
      $('pinok').disabled = digits.length !== 4;
      inputs[Math.min(digits.length, 3)].focus();
    });
  });

  $('pinok').onclick = function () {
    if (pinValue().length === 4) closePin(pinValue());
  };
  $('pincancel').onclick = function () { closePin(null); };
  $('pinback').addEventListener('mousedown', function (e) {
    if (e.target === $('pinback')) closePin(null);   // click outside to dismiss
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('pinback').hidden) closePin(null);
  });
})();

// Run something that needs a PIN, re-asking while the daemon says it was wrong.
function withPin(what, attempt) {
  return askPin(what).then(function (pin) {
    if (pin === null) return { cancelled: true };
    return attempt(pin).then(function (reply) {
      if (reply && reply.pin_error && !reply.locked) {
        return new Promise(function (resolve) {
          $('pinback').hidden = false;
          pinShake(reply.error || 'wrong PIN');
          PIN.resolve = function (again) {
            if (again === null) { resolve({ cancelled: true }); return; }
            resolve(attempt(again).then(function (second) {
              if (second && second.pin_error) {
                $('pinback').hidden = true;
                toast(second.error || 'wrong PIN', false);
              }
              return second;
            }));
          };
        });
      }
      return reply;
    });
  });
}


// ---------------------------------------------------------------------------
// Self tests
// ---------------------------------------------------------------------------
function testVerdict(row) {
  if (row.finished === null || row.finished === undefined) return "running";
  if (row.passed === 1) return "ok";
  if (row.passed === 0) return "crit";
  return "warn";
}

function loadTests() {
  return api("tests&limit=100").then(function (data) {
    var rows = (data && data.tests) || [];
    renderLatestTest(rows[0]);
    renderTestSummary(rows);
    renderTestChart(rows);
    renderTestTable(rows);
    renderTestButtons();
  });
}

function renderLatestTest(row) {
  var running = row && !row.finished;
  var note = $("test-running");
  if (note) note.hidden = !running;
  var host = $("test-latest");
  host.innerHTML = "";
  host.appendChild(el("h2", null, "Last self test"));
  if (!row) {
    host.appendChild(el("p", "muted",
      "No self test has been recorded yet. Run one and it will appear here, "
      + "along with everything the daemon saw while it ran."));
    return;
  }

  var verdict = testVerdict(row);
  var pill = el("span", "pill " + (verdict === "running" ? "warn" : verdict));
  pill.appendChild(el("span", "dot"));
  pill.appendChild(document.createTextNode(
    verdict === "running" ? "running now" : (row.result || "no verdict")));
  var head = el("div");
  head.style.cssText = "margin-bottom:14px";
  head.appendChild(pill);
  host.appendChild(head);

  var facts = [];
  facts.push(["When", stamp(row.started) + " · " + ago(row.started)]);
  if (row.duration_s) facts.push(["Took", runtimeText(row.duration_s)]);
  if (row.on_battery_s) facts.push(["On battery", runtimeText(row.on_battery_s)]);
  if (row.voltage_min !== null && row.voltage_min !== undefined) {
    facts.push(["Lowest voltage", fmt(row.voltage_min, 1) + " V"]);
  }
  if (row.charge_start !== null && row.charge_start !== undefined) {
    facts.push(["Charge", fmt(row.charge_start, 0) + "% → "
                + (row.charge_end !== null ? fmt(row.charge_end, 0) + "%" : "?")]);
  }
  facts.push(["Started by", row.source === "dashboard" ? "this dashboard"
                                                       : "the UPS itself"]);
  renderKV(host.appendChild(el("div")), facts);

  // A short test may finish between two driver polls, so the voltage never
  // moves. Saying so is more useful than showing a blank.
  if (row.finished && (row.voltage_min === null || row.voltage_min === undefined)) {
    host.appendChild(el("p", "note",
      "The voltage did not move during this test — the driver refreshes it only "
      + "every 30 seconds or so, and the test was shorter than that."));
  }
}

function renderTestSummary(rows) {
  var host = $("test-summary");
  host.innerHTML = "";
  host.appendChild(el("h2", null, "Record"));
  var done = rows.filter(function (r) { return r.finished; });
  if (!done.length) {
    host.appendChild(el("p", "muted", "nothing completed yet"));
    return;
  }
  var passed = done.filter(function (r) { return r.passed === 1; }).length;
  var failed = done.filter(function (r) { return r.passed === 0; }).length;
  var voltages = done.filter(function (r) { return num(r.voltage_min) !== null; });

  var facts = [["Tests recorded", String(done.length)],
               ["Passed", String(passed), passed === done.length ? "ok" : null],
               ["Failed", String(failed), failed ? "crit" : null]];
  if (voltages.length) {
    var newest = num(voltages[0].voltage_min);
    var oldest = num(voltages[voltages.length - 1].voltage_min);
    facts.push(["Lowest seen",
                fmt(Math.min.apply(null, voltages.map(function (r) {
                  return num(r.voltage_min); })), 1) + " V"]);
    if (voltages.length > 1) {
      var drift = newest - oldest;
      facts.push(["Trend", (drift >= 0 ? "+" : "") + drift.toFixed(1)
                  + " V since the first test",
                  drift <= -0.5 ? "warn" : null]);
    }
  }
  var last = done[0];
  facts.push(["Last run", ago(last.started)]);
  renderKV(host.appendChild(el("div")), facts);

  var age = (Date.now() / 1000 - done[0].started) / 86400;
  if (age > 60) {
    host.appendChild(el("p", "note",
      "The last test was " + Math.round(age) + " days ago. Once every month or "
      + "two is enough to catch a battery going bad before a real outage does."));
  }
}

function renderTestChart(rows) {
  var done = rows.filter(function (r) {
    return r.finished && num(r.voltage_min) !== null;
  }).slice().reverse();
  if (done.length < 2) {
    $("chart-tests").innerHTML = '<p class="muted">Two completed tests with a '
      + 'voltage reading are needed before a trend means anything. Short tests '
      + 'often finish before the driver refreshes the voltage at all.</p>';
    return;
  }
  drawChart($("chart-tests"), {
    rows: done.map(function (r) {
      return { ts: r.started, voltage_min: num(r.voltage_min) };
    }),
    digits: 1, unit: "volts at the lowest point of each test",
    series: [{ key: "voltage_min", label: "Lowest voltage", color: "var(--batt)" }]
  });
}

function renderTestTable(rows) {
  var host = $("tests-table");
  host.innerHTML = "";
  if (!rows.length) {
    host.appendChild(el("p", "muted", "Nothing yet."));
    return;
  }
  var table = el("table");
  table.innerHTML = "<thead><tr><th>When</th><th>Verdict</th><th>Started by</th>"
    + "<th>Took</th><th>On battery</th><th>Lowest V</th><th>Charge</th></tr></thead>";
  var body = el("tbody");
  rows.forEach(function (r) {
    var tr = el("tr");
    var when = el("td");
    when.appendChild(el("div", null, stamp(r.started)));
    when.appendChild(el("div", "muted", ago(r.started)));
    tr.appendChild(when);

    var verdict = el("td");
    var kind = testVerdict(r);
    verdict.appendChild(el("span", "tag " + (kind === "running" ? "warn" : kind),
                           kind === "running" ? "running" : (r.result || "—")));
    tr.appendChild(verdict);

    tr.appendChild(el("td", "muted",
      r.source === "dashboard" ? "dashboard" : "the UPS"));
    tr.appendChild(el("td", "num", r.duration_s ? runtimeText(r.duration_s) : "—"));
    tr.appendChild(el("td", "num", r.on_battery_s ? runtimeText(r.on_battery_s) : "—"));
    tr.appendChild(el("td", "num", num(r.voltage_min) !== null
      ? fmt(r.voltage_min, 1) + " V" : "—"));
    tr.appendChild(el("td", "num", num(r.charge_start) !== null
      ? fmt(r.charge_start, 0) + "% → " + (num(r.charge_end) !== null
        ? fmt(r.charge_end, 0) + "%" : "?") : "—"));
    body.appendChild(tr);
  });
  table.appendChild(body);
  host.appendChild(table);
}

function renderTestButtons() {
  var host = $("test-buttons");
  var commands = ((STATE.snapshot && STATE.snapshot.commands) || [])
    .filter(function (c) { return /^test\.|^calibrate\./.test(c.name); });
  host.innerHTML = "";
  if (!commands.length) {
    host.appendChild(el("span", "muted",
      "This UPS reports no test commands, so there is nothing to start from here."));
    return;
  }
  commands.forEach(function (c) {
    host.appendChild(commandButton(c, function (button) {
      runCommand(c, button);
      $("test-running").hidden = false;
      setTimeout(loadTests, 3000);
    }));
  });
}

// ---------------------------------------------------------------------------
// Control
// ---------------------------------------------------------------------------
// Commands are grouped so the useful everyday ones come first and the ones that
// would cut power sit apart, behind a confirmation.
var COMMAND_GROUPS = [
  { title: "Self tests", match: /^test\.|^calibrate\./ },
  { title: "Beeper",     match: /^beeper\./ },
  { title: "Other",      match: /^(shutdown\.stop|reset\.|bypass\.stop|driver\.reload)/ },
  { title: "Power switching", match: /.*/, dangerous: true }
];

function loadCapabilities() {
  loadResetCounts();
  return api("capabilities").then(function (data) {
    renderCommands(data || {});
    renderWritable(data || {});
  });
}

function renderCommands(caps) {
  var host = $("commands");
  host.innerHTML = "";
  var commands = caps.commands || [];
  if (!commands.length) {
    host.appendChild(el("p", "muted", "This UPS reports no instant commands."));
    return;
  }
  var used = {};
  COMMAND_GROUPS.forEach(function (group) {
    var members = commands.filter(function (c) {
      if (used[c.name]) return false;
      if (group.dangerous) return c.dangerous;
      if (c.dangerous) return false;
      return group.match.test(c.name);
    });
    if (!members.length) return;
    members.forEach(function (c) { used[c.name] = true; });

    host.appendChild(el("h3", null, group.title)).style.cssText =
      "font-size:12px;letter-spacing:.8px;text-transform:uppercase;color:var(--tx-mut);margin:18px 0 9px";
    if (group.dangerous) {
      host.appendChild(el("p", "btnhelp",
        caps.allow_dangerous
          ? "These cut power to everything plugged into the UPS, including whatever is reading this page. Each one asks first."
          : "These are disabled. Set allow_dangerous_commands in /etc/upsmon/config.json to enable them."));
    }
    var row = el("div", "btnrow");
    members.forEach(function (c) {
      var button = commandButton(c, function (b) { runCommand(c, b); });
      if (c.dangerous && !caps.allow_dangerous) button.disabled = true;
      row.appendChild(button);
    });
    host.appendChild(row);
  });
}

function runCommand(command, button) {
  var gate = command.dangerous
    ? askConfirm("<b>" + command.name + "</b><br>" + command.help
                 + "<br><br>This cuts power to everything plugged into the UPS, "
                 + "which may include the machine showing this page.",
                 { title: "Switch the power off?", confirmLabel: "Do it" })
    : Promise.resolve(true);
  gate.then(function (agreed) { if (agreed) startCommand(command, button); });
}

function startCommand(command, button) {
  var what = 'Run <b>' + command.name + '</b><br>' + command.help;
  button.disabled = true;
  button.classList.add("busy");

  withPin(what, function (pin) {
    return api("command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: command.name, pin: pin })
    });
  }).then(function (reply) {
    if (!reply || reply.cancelled) return;
    toast(reply.message || reply.error || "no reply", !!reply.ok);
    if (reply.ok && /^test\.|^calibrate\./.test(command.name)) {
      toast((reply.message || "started") + " — the daemon is following it and "
            + "will record the verdict under Events.", true);
    }
    refresh();
    setTimeout(loadEvents, 2000);
  }).catch(function (e) {
    toast("request failed: " + e, false);
  }).then(function () {
    button.disabled = false;
    button.classList.remove("busy");
  });
}





// ---------------------------------------------------------------------------
// Favicon
// ---------------------------------------------------------------------------
// The tab is often the only part of this page anyone can see. Colouring the
// icon by the verdict means a problem is visible without switching to it.
var FAVICON_COLOURS = {
  ok:      ["#3fb950", "#2ea043"],
  warn:    ["#d29922", "#b8860b"],
  crit:    ["#f85149", "#da3633"],
  unknown: ["#6e7681", "#57606a"]
};
var faviconLevel = null;

function faviconSvg(level) {
  var colours = FAVICON_COLOURS[level] || FAVICON_COLOURS.unknown;
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    + '<defs><linearGradient id="c" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0" stop-color="' + colours[0] + '"/>'
    + '<stop offset="1" stop-color="' + colours[1] + '"/></linearGradient>'
    + '<mask id="b"><rect width="64" height="64" fill="#fff"/>'
    + '<path d="M34.5 20 24 35h7l-2.5 11L39 31h-7z" fill="#000"/></mask></defs>'
    + '<rect width="64" height="64" rx="14" fill="#161b22"/>'
    + '<rect x="26" y="8" width="12" height="5" rx="2" fill="#8b94a3"/>'
    + '<rect x="15" y="13" width="34" height="43" rx="7" fill="none" '
    + 'stroke="#8b94a3" stroke-width="3.5"/>'
    + '<rect x="19.5" y="17.5" width="25" height="34" rx="4" '
    + 'fill="url(#c)" mask="url(#b)"/></svg>';
}

function setFavicon(level) {
  if (level === faviconLevel) return;        // repainting costs a repaint
  var link = $("favicon");
  if (!link) return;
  faviconLevel = level;
  link.setAttribute("href",
    "data:image/svg+xml," + encodeURIComponent(faviconSvg(level)));
}

// ---------------------------------------------------------------------------
// Command buttons
// ---------------------------------------------------------------------------
// NUT command names are precise but unreadable on a button. Each gets a plain
// label and an icon; the name and the explanation move into the hint that
// appears if the pointer rests on it.
var ICONS = {
  test: '<path d="M9 3h6M10 3v5.2L4.8 18A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.7-3L14 8.2V3"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
  bellOff: '<path d="M18 8a6 6 0 0 0-9.3-5"/><path d="M6.3 6.3A6 6 0 0 0 6 8c0 7-3 8-3 8h13"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/><path d="M2 2l20 20"/>',
  mute: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/><path d="M15 4l6 6"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>',
  reboot: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
  cancel: '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
  gauge: '<path d="M12 21a9 9 0 1 1 9-9"/><path d="M12 12l5-3"/>',
  bypass: '<path d="M4 7h4l8 10h4"/><path d="M4 17h4l3-3.8"/><path d="M17 4l3 3-3 3"/><path d="M17 14l3 3-3 3"/>',
  reset: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
  panel: '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 20h10"/><path d="M7 9h5"/>',
  chart: '<path d="M3 3v17a1 1 0 0 0 1 1h17"/><path d="M7 15l4-5 3.5 3L20 7"/>',
  log:   '<path d="M5 3h14a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  plug:  '<path d="M9 3v6M15 3v6"/><path d="M6 9h12v3a6 6 0 0 1-12 0z"/><path d="M12 18v3"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6M14 11v6"/>',
  save:  '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
  check: '<path d="M20 6 9 17l-5-5"/>'
};

function iconMarkup(key, size) {
  return '<svg viewBox="0 0 24 24" width="' + (size || 17) + '" height="'
       + (size || 17) + '" fill="none" stroke="currentColor" stroke-width="1.7" '
       + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
       + ICONS[key] + '</svg>';
}

// name -> [label, icon]. Anything not listed falls back to the name itself.
var COMMAND_UI = {
  'test.battery.start':        ['Battery test',        'test'],
  'test.battery.start.quick':  ['Quick test',          'test'],
  'test.battery.start.deep':   ['Deep test',           'test'],
  'test.battery.stop':         ['Stop the test',       'stop'],
  'test.panel.start':          ['Panel test',          'panel'],
  'test.panel.stop':           ['Stop panel test',     'stop'],
  'test.system.start':         ['System test',         'test'],
  'test.failure.start':        ['Simulate a failure',  'power'],
  'test.failure.stop':         ['Stop simulating',     'stop'],
  'calibrate.start':           ['Calibrate runtime',   'gauge'],
  'calibrate.stop':            ['Stop calibrating',    'stop'],
  'beeper.enable':             ['Alarm on',            'bell'],
  'beeper.disable':            ['Alarm off',           'bellOff'],
  'beeper.mute':               ['Mute until next time','mute'],
  'beeper.toggle':             ['Toggle the alarm',    'bell'],
  'beeper.on':                 ['Alarm on (legacy)',   'bell'],
  'beeper.off':                ['Alarm off (legacy)',  'bellOff'],
  'shutdown.stop':             ['Cancel shutdown',     'cancel'],
  'shutdown.return':           ['Off until mains back','power'],
  'shutdown.stayoff':          ['Off and stay off',    'power'],
  'shutdown.reboot':           ['Reboot the UPS',      'reboot'],
  'shutdown.reboot.graceful':  ['Reboot after a delay','reboot'],
  'load.off':                  ['Outlets off',         'power'],
  'load.on':                   ['Outlets on',          'power'],
  'load.off.delay':            ['Outlets off shortly', 'power'],
  'load.on.delay':             ['Outlets on shortly',  'power'],
  'load.cycle':                ['Power cycle',         'reboot'],
  'bypass.start':              ['Enter bypass',        'bypass'],
  'bypass.stop':               ['Leave bypass',        'bypass'],
  'reset.input.minmax':        ['Reset min/max',       'reset'],
  'reset.watchdog':            ['Reset watchdog',      'reset'],
  'driver.reload':             ['Reload the driver',   'reset']
};

function commandLabel(name) {
  if (COMMAND_UI[name]) return COMMAND_UI[name][0];
  // outlet.2.load.off -> "Load off (outlet 2)"
  var outlet = /^outlet\.(\w+)\.(.+)$/.exec(name);
  var base = outlet ? outlet[2] : name;
  var words = base.replace(/\./g, " ").replace(/_/g, " ");
  var label = words.charAt(0).toUpperCase() + words.slice(1);
  return outlet ? label + " (outlet " + outlet[1] + ")" : label;
}

function commandIcon(name) {
  var key = COMMAND_UI[name] && COMMAND_UI[name][1];
  if (!key) {
    if (/^test\./.test(name)) key = "test";
    else if (/^beeper\./.test(name)) key = "bell";
    else if (/^calibrate\./.test(name)) key = "gauge";
    else if (/stop$|cancel/.test(name)) key = "cancel";
    else if (/^reset\./.test(name)) key = "reset";
    else key = "power";
  }
  return iconMarkup(key);
}

function commandButton(command, onclick) {
  var button = el("button", "btn cmd" + (command.dangerous ? " danger" : ""));
  button.innerHTML = commandIcon(command.name)
    + '<span>' + commandLabel(command.name) + '</span>';
  attachHint(button, '<b>' + command.name + '</b><br>' + command.help
    + (command.dangerous ? '<br><em>Cuts power to everything on the UPS.</em>' : ''));
  button.onclick = function () { onclick(button); };
  return button;
}

// ---------------------------------------------------------------------------
// Hints
// ---------------------------------------------------------------------------
// Deliberately slow. The label answers the everyday question; the exact NUT
// name is only wanted when somebody stops to look for it, and a hint that
// appears the moment the pointer crosses a button is just noise.
var HINT_DELAY_MS = 5000;
var HINT = { timer: null, node: null };

function attachHint(element, html) {
  element.addEventListener("mouseenter", function () {
    clearTimeout(HINT.timer);
    HINT.timer = setTimeout(function () { showHint(element, html); }, HINT_DELAY_MS);
  });
  element.addEventListener("mouseleave", hideHint);
  element.addEventListener("blur", hideHint);
  // Touch has no hover at all, and a long press is the closest equivalent.
  element.addEventListener("touchstart", function () {
    clearTimeout(HINT.timer);
    HINT.timer = setTimeout(function () { showHint(element, html); }, 600);
  }, { passive: true });
  element.addEventListener("touchend", hideHint);
}

function showHint(element, html) {
  hideHint();
  var tip = el("div", "hint");
  tip.innerHTML = html;
  document.body.appendChild(tip);
  var box = element.getBoundingClientRect();
  var width = tip.offsetWidth, height = tip.offsetHeight;
  var left = box.left + box.width / 2 - width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  var top = box.top - height - 10;
  if (top < 8) top = box.bottom + 10;          // no room above; drop below
  tip.style.left = Math.round(left) + "px";
  tip.style.top = Math.round(top) + "px";
  requestAnimationFrame(function () { tip.classList.add("show"); });
  HINT.node = tip;
}

function hideHint() {
  clearTimeout(HINT.timer);
  if (HINT.node && HINT.node.parentNode) {
    HINT.node.parentNode.removeChild(HINT.node);
  }
  HINT.node = null;
}

window.addEventListener("scroll", hideHint, true);

// ---------------------------------------------------------------------------
// Confirmation dialog
// ---------------------------------------------------------------------------
// The browser's own confirm() looks nothing like the rest of the page and, on
// some platforms, offers to suppress future ones — which would silently remove
// the last guard in front of switching the power off.
var ASK = { resolve: null };

function askConfirm(what, options) {
  options = options || {};
  return new Promise(function (resolve) {
    var box = $("askback");
    if (!box) { resolve(window.confirm(what.replace(/<[^>]+>/g, ""))); return; }
    $("asktitle").textContent = options.title || "Are you sure?";
    $("askwhat").innerHTML = what;
    $("askok").textContent = options.confirmLabel || "Confirm";
    $("askok").classList.toggle("danger", options.danger !== false);
    box.hidden = false;
    ASK.resolve = function (answer) {
      box.hidden = true;
      ASK.resolve = null;
      resolve(answer);
    };
    setTimeout(function () { $("askok").focus(); }, 40);
  });
}

function wireConfirm() {
  var ok = $("askok"), cancel = $("askcancel"), back = $("askback");
  if (!ok || !cancel || !back) return;
  ok.onclick = function () { if (ASK.resolve) ASK.resolve(true); };
  cancel.onclick = function () { if (ASK.resolve) ASK.resolve(false); };
  back.addEventListener("click", function (event) {
    // Clicking the darkened area behind the card means "no".
    if (event.target === back && ASK.resolve) ASK.resolve(false);
  });
  document.addEventListener("keydown", function (event) {
    if (!ASK.resolve || back.hidden) return;
    if (event.key === "Escape") { event.preventDefault(); ASK.resolve(false); }
    if (event.key === "Enter") { event.preventDefault(); ASK.resolve(true); }
  });
}
wireConfirm();

// ---------------------------------------------------------------------------
// Clearing recorded data
// ---------------------------------------------------------------------------
// Each scope names the tables it empties, so the button can say exactly how
// many rows are about to go rather than asking for blind confirmation.
var RESET_SCOPES = [
  { scope: "history", label: "Charts",   icon: "chart",
    tables: ["samples", "samples_hourly", "snapshots"],
    help: "every recorded sample; the charts start again from nothing" },
  { scope: "events",  label: "Events",   icon: "log", tables: ["events"],
    help: "the event log" },
  { scope: "tests",   label: "Tests",    icon: "test", tables: ["tests"],
    help: "the self-test history and its voltage trend" },
  { scope: "outages", label: "Outages",  icon: "plug", tables: ["outages"],
    help: "the record of power failures" },
  { scope: "all",     label: "Everything", icon: "trash", danger: true,
    tables: ["samples", "samples_hourly", "snapshots", "events", "outages", "tests"],
    help: "all of the above, leaving an empty database" }
];

function loadResetCounts() {
  return api("health").then(function (health) {
    var counts = (health && health.database) || {};
    renderResetCard(counts);
  }).catch(function () {
    renderResetCard({});
  });
}

function renderResetCard(counts) {
  var host = $("reset-counts");
  host.innerHTML = "";
  var rows = [];
  RESET_SCOPES.forEach(function (entry) {
    if (entry.scope === "all") return;
    var total = entry.tables.reduce(function (sum, table) {
      return sum + (counts[table] || 0);
    }, 0);
    entry.count = total;
    rows.push([entry.label, total.toLocaleString() + " row"
               + (total === 1 ? "" : "s")]);
  });
  var all = RESET_SCOPES[RESET_SCOPES.length - 1];
  all.count = rows.reduce(function (sum, row, i) {
    return sum + RESET_SCOPES[i].count;
  }, 0);
  if (counts.db_bytes) {
    rows.push(["Database size", (counts.db_bytes / 1048576).toFixed(2) + " MB"]);
  }
  renderKV(host, rows);

  var buttons = $("reset-buttons");
  buttons.innerHTML = "";
  RESET_SCOPES.forEach(function (entry) {
    var button = el("button", "btn cmd" + (entry.danger ? " danger" : ""));
    button.innerHTML = iconMarkup(entry.icon)
      + '<span>Clear ' + entry.label.toLowerCase() + '</span>';
    button.disabled = !entry.count;
    attachHint(button, '<b>' + entry.label + '</b><br>'
      + (entry.count ? entry.count.toLocaleString() + ' row'
                       + (entry.count === 1 ? '' : 's') + ' — ' : 'nothing to delete — ')
      + entry.help
      + (entry.danger ? '<br><em>Leaves the database empty.</em>' : ''));
    button.onclick = function () { resetData(entry, button); };
    buttons.appendChild(button);
  });
}

function resetData(entry, button) {
  var total = entry.count || 0;
  button.disabled = true;
  button.classList.add("busy");

  // No separate confirmation step: the PIN dialog is the confirmation and it
  // carries the same detail. Two dialogs in a row train people to dismiss the
  // first without reading it.
  var summary = entry.label.toLowerCase() + " — " + total.toLocaleString()
              + " row" + (total === 1 ? "" : "s") + ": " + entry.help;
  var what = "Clear <b>" + entry.label.toLowerCase() + "</b> — "
           + total.toLocaleString() + " row" + (total === 1 ? "" : "s");

  // With no PIN set there would be nothing between a stray click and an empty
  // database, so in that case ask the plain question instead.
  // Always ask, then ask for the PIN if one is set. Deleting history is
  // irreversible, so it gets the same warning whether or not a PIN follows.
  var gate = askConfirm("Clear <b>" + entry.label.toLowerCase() + "</b> — "
                        + total.toLocaleString() + " row" + (total === 1 ? "" : "s")
                        + "<br>" + entry.help
                        + "<br><br>This cannot be undone.",
                        { title: "Clear recorded data", confirmLabel: "Clear" });

  gate.then(function (agreed) {
    if (!agreed) {
      button.disabled = false;
      button.classList.remove("busy");
      return { cancelled: true };
    }
    return withPin(what, function (pin) {
    return api("reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: entry.scope, pin: pin })
    });
    });
  }).then(function (reply) {
    if (!reply || reply.cancelled) return;
    toast(reply.message || reply.error || "no reply", !!reply.ok);
    if (reply.ok) {
      // Everything on screen was drawn from what was just deleted.
      STATE.history = null;
      LAST.history = LAST.events = LAST.tests = 0;
      loadResetCounts();
      loadEvents();
      if (STATE.tab === "tests") loadTests();
      loadQuickChart();
    }
  }).catch(function (e) {
    toast("request failed: " + e, false);
  }).then(function () {
    button.classList.remove("busy");
    button.disabled = false;
    loadResetCounts();
  });
}

function renderWritable(caps) {
  var host = $("writable");
  host.innerHTML = "";
  var fields = caps.fields || {};
  var names = Object.keys(fields).sort();
  if (!names.length) {
    host.appendChild(el("p", "muted",
      "This UPS exposes no writable variables. Many cheaper models allow nothing at all."));
    return;
  }
  var table = el("table");
  table.innerHTML = "<thead><tr><th>Setting</th><th>Value</th><th></th></tr></thead>";
  var body = el("tbody");

  names.forEach(function (name) {
    var field = fields[name];
    var tr = el("tr");

    var left = el("td");
    left.appendChild(el("div", "mono", name));
    if (field.description) left.appendChild(el("div", "muted", field.description));
    tr.appendChild(left);

    var cell = el("td");
    var row = el("div", "fieldrow");
    var control = buildControl(name, field);
    row.appendChild(control);
    if (field.unit) row.appendChild(el("span", "fieldunit", field.unit));
    cell.appendChild(row);
    var hint = controlHint(field);
    if (hint) cell.appendChild(el("div", "fieldhelp", hint));
    tr.appendChild(cell);

    var action = el("td");
    var button = el("button", "btn cmd", "");
    button.innerHTML = iconMarkup("save") + '<span>Save</span>';
    attachHint(button, '<b>' + name + '</b><br>'
      + (field.description || 'Write this value into the UPS.')
      + '<br>The UPS is asked, then read back — a few seconds, because the '
      + 'driver refreshes its cache only periodically.');
    button.disabled = true;
    // Only offer to save once something has actually changed, so the button
    // never invites a pointless write (each one costs a PIN and a poll cycle).
    control.addEventListener("input", function () {
      var changed = controlValue(control) !== String(field.value);
      button.disabled = !changed;
      control.classList.toggle("changed", changed);
    });
    control.addEventListener("change", function () {
      var changed = controlValue(control) !== String(field.value);
      button.disabled = !changed;
      control.classList.toggle("changed", changed);
    });
    control.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !button.disabled) button.click();
    });
    button.onclick = function () {
      saveVariable(name, controlValue(control), button, field, control);
    };
    action.appendChild(button);
    tr.appendChild(action);
    body.appendChild(tr);
  });

  table.appendChild(body);
  host.appendChild(table);
  host.appendChild(el("p", "note",
    "Values are checked against what the UPS says it accepts, written, then read "
    + "back — which takes a few seconds, because the driver refreshes its cache "
    + "only every few seconds."));
}

// The UPS tells us what each variable will accept; use it rather than making
// everyone type a value and find out from an error message.
function buildControl(name, field) {
  var value = String(field.value);

  if (field.kind === "enum" && field.enum.length) {
    var select = el("select", "field");
    field.enum.forEach(function (option) {
      var node = el("option", null, option + (field.unit ? " " + field.unit : ""));
      node.value = option;
      if (option === value) node.selected = true;
      select.appendChild(node);
    });
    if (field.enum.indexOf(value) < 0) {         // current value outside the list
      var current = el("option", null, value + " (current)");
      current.value = value;
      select.insertBefore(current, select.firstChild);
    }
    select.value = value;
    return select;
  }

  if (field.kind === "suggest" && field.suggest.length) {
    var box = el("select", "field");
    var options = field.suggest.slice();
    if (options.indexOf(value) < 0) options.unshift(value);
    options.forEach(function (option) {
      var node = el("option", null, option);
      node.value = option;
      box.appendChild(node);
    });
    box.value = value;
    return box;
  }

  if (field.kind === "date") {
    var date = el("input", "field");
    date.type = "date";
    date.dataset.format = detectDateFormat(value);
    date.value = isoFromVendorDate(value);
    return date;
  }

  var input = el("input", "field");
  if (field.kind === "range" || field.kind === "number") {
    input.type = "number";
    input.step = /\./.test(value) ? "0.1" : "1";
    if (field.ranges && field.ranges.length) {
      input.min = field.ranges[0][0];
      input.max = field.ranges[field.ranges.length - 1][1];
    }
  } else {
    input.type = "text";
    if (field.maxlength) input.maxLength = field.maxlength;
  }
  input.value = value;
  return input;
}

function controlValue(control) {
  if (control.type === "date") return vendorDateFromIso(control.value, control.dataset.format);
  return control.value;
}

function controlHint(field) {
  if (field.kind === "enum" && field.enum.length) return "accepts: " + field.enum.join(", ");
  if (field.kind === "range" && field.ranges.length) {
    return "range: " + field.ranges.map(function (r) { return r[0] + " – " + r[1]; }).join(", ");
  }
  if (field.kind === "suggest") return "typical values; this UPS does not publish a list";
  if (field.kind === "date") return "stored as " + field.value;
  if (field.maxlength) return "up to " + field.maxlength + " characters";
  return "";
}

// Vendors disagree about date formats — APC writes 2026/08/29, others use
// ISO or the European order. Keep whatever this UPS already uses.
function detectDateFormat(value) {
  if (/^\d{4}\/\d{2}\/\d{2}$/.test(value)) return "Y/m/d";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Y-m-d";
  if (/^\d{2}\/\d{2}\/\d{2}$/.test(value)) return "m/d/y";
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(value)) return "m/d/Y";
  if (/^\d{2}\.\d{2}\.\d{4}$/.test(value)) return "d.m.Y";
  return "Y-m-d";
}

function isoFromVendorDate(value) {
  var m;
  if ((m = /^(\d{4})[\/-](\d{2})[\/-](\d{2})$/.exec(value))) return m[1] + "-" + m[2] + "-" + m[3];
  if ((m = /^(\d{2})\/(\d{2})\/(\d{2})$/.exec(value))) {
    var year = parseInt(m[3], 10);
    return (year > 70 ? 1900 + year : 2000 + year) + "-" + m[1] + "-" + m[2];
  }
  if ((m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value))) return m[3] + "-" + m[1] + "-" + m[2];
  if ((m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value))) return m[3] + "-" + m[2] + "-" + m[1];
  return "";
}

function vendorDateFromIso(iso, format) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return iso || "";
  var Y = m[1], M = m[2], D = m[3], y = Y.slice(2);
  switch (format) {
    case "Y/m/d": return Y + "/" + M + "/" + D;
    case "m/d/y": return M + "/" + D + "/" + y;
    case "m/d/Y": return M + "/" + D + "/" + Y;
    case "d.m.Y": return D + "." + M + "." + Y;
    default:      return Y + "-" + M + "-" + D;
  }
}

function saveVariable(name, value, button, field, control) {
  var what = 'Change <b>' + name + '</b><br>from ' + field.value + ' to <b>' + value + '</b>';
  button.disabled = true;
  button.classList.add("busy");
  setButtonLabel(button, "Saving…");

  withPin(what, function (pin) {
    return api("set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ var: name, value: value, pin: pin })
    });
  }).then(function (reply) {
    if (reply && reply.cancelled) return;
    toast(reply.message || reply.error || "no reply", !!reply.ok);
    // The daemon returns what the UPS holds now, which may differ from what we
    // asked for if it clamped the value. Show that rather than the request.
    if (reply && reply.ok && reply.value !== undefined && reply.value !== null) {
      // Show what the UPS actually holds now, which is not always what was
      // asked for — some values get clamped to the nearest the model accepts.
      field.value = reply.value;
      if (control) {
        if (control.type === "date") {
          var iso = isoFromVendorDate(reply.value);
          if (iso) control.value = iso;
        } else {
          control.value = reply.value;
        }
        control.classList.remove("changed");
      }
      button.disabled = true;      // nothing left to save until it changes again
    }
    refresh();
    loadCapabilities();
    setTimeout(loadEvents, 1500);
  }).catch(function (e) {
    toast("request failed: " + e, false);
  }).then(function () {
    // Re-enable unconditionally. Cancelling the PIN returns early, so relying
    // on the table being re-rendered would leave this button dead.
    button.disabled = false;
    button.classList.remove("busy");
    setButtonLabel(button, "Save");
  });
}

function setButtonLabel(button, text) {
  // The label lives in a span next to the icon; replacing textContent would
  // take the icon with it.
  var span = button.querySelector("span");
  if (span) span.textContent = text;
  else button.textContent = text;
}

// ---------------------------------------------------------------------------
// All variables
// ---------------------------------------------------------------------------
var GROUP_ORDER = ["device", "ups", "battery", "input", "output", "ambient",
                   "outlet", "driver", "server"];

function renderAllVariables(snap) {
  var host = $("all-vars");
  host.innerHTML = "";
  var vars = snap.vars || {}, descriptions = snap.descriptions || {},
      writable = snap.writable || {};
  var names = Object.keys(vars);
  if (!names.length) { host.appendChild(el("p", "muted", "no data")); return; }

  var groups = {};
  names.forEach(function (name) {
    var prefix = name.split(".")[0];
    var key = GROUP_ORDER.indexOf(prefix) >= 0 ? prefix : "other";
    (groups[key] = groups[key] || []).push(name);
  });
  var ordered = GROUP_ORDER.filter(function (g) { return groups[g]; })
    .concat(Object.keys(groups).filter(function (g) { return GROUP_ORDER.indexOf(g) < 0; }).sort());

  ordered.forEach(function (group) {
    var heading = el("h3", null, group.toUpperCase());
    heading.style.cssText = "font-size:12px;letter-spacing:.8px;color:var(--tx-mut);margin:20px 0 8px";
    host.appendChild(heading);
    var table = el("table");
    var body = el("tbody");
    groups[group].sort().forEach(function (name) {
      var tr = el("tr");
      var left = el("td");
      var title = el("div", "mono");
      title.textContent = name;
      if (writable[name] !== undefined) {
        title.appendChild(document.createTextNode(" "));
        title.appendChild(el("span", "tag rw", "rw"));
      }
      left.appendChild(title);
      if (descriptions[name]) left.appendChild(el("div", "muted", descriptions[name]));
      tr.appendChild(left);
      var value = el("td", "num");
      value.textContent = vars[name];
      if (name === "battery.runtime") {
        value.appendChild(el("div", "muted", runtimeText(vars[name])));
      }
      tr.appendChild(value);
      body.appendChild(tr);
    });
    table.appendChild(body);
    host.appendChild(table);
  });
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function refresh() {
  return api("status").then(function (snap) {
    if (!snap || snap.online === false) {
      var verdict = $("verdict");
      verdict.className = "pill crit";
      verdict.innerHTML = '<span class="dot"></span>' +
        (snap && snap.error ? "UPS unreachable" : "Daemon unreachable");
      setFavicon("crit");
      return;
    }
    renderSnapshot(snap);
    if (STATE.tab === "all") renderAllVariables(snap);
  }).catch(function () {
    var verdict = $("verdict");
    verdict.className = "pill unknown";
    verdict.innerHTML = '<span class="dot"></span>Dashboard offline';
    setFavicon("unknown");
  });
}

function showTab(name) {
  STATE.tab = name;
  ["overview", "history", "tests", "events", "control", "all"].forEach(function (tab) {
    $("tab-" + tab).hidden = tab !== name;
  });
  document.querySelectorAll(".tabs button").forEach(function (button) {
    button.setAttribute("aria-selected", button.dataset.tab === name ? "true" : "false");
  });
  if (name === "history" && !STATE.history) {
    LAST.history = Date.now();
    loadHistory(STATE.range);
    loadOutages();
  }
  if (name === "tests") { LAST.tests = Date.now(); loadTests(); }
  if (name === "events") { LAST.events = Date.now(); loadEvents(); }
  if (name === "control") { LAST.capabilities = Date.now(); loadCapabilities(); }
  if (name === "all" && STATE.snapshot) renderAllVariables(STATE.snapshot);
  try { history.replaceState(null, "", "#" + name); } catch (e) {}
}

document.querySelectorAll(".tabs button").forEach(function (button) {
  button.onclick = function () { showTab(button.dataset.tab); };
});
document.querySelectorAll("#ranges button").forEach(function (button) {
  button.onclick = function () {
    document.querySelectorAll("#ranges button").forEach(function (other) {
      other.setAttribute("aria-pressed", other === button ? "true" : "false");
    });
    LAST.history = Date.now();
    loadHistory(button.dataset.range);
  };
});

// Redraw the SVGs on resize so labels stay legible on a phone turned sideways.
var resizeTimer;
window.addEventListener("resize", function () {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function () {
    if (STATE.history) loadHistory(STATE.range);
  }, 300);
});

// ---------------------------------------------------------------------------
// Automatic refreshing
// ---------------------------------------------------------------------------
// The snapshot is small and cheap, so it comes every 5 s. Everything else is
// refreshed on its own cadence and only while its tab is open — an idle
// dashboard should not be asking the daemon for a year of history.
var REFRESH_MS = 5000;
// A test finishes on the UPS's own schedule, so this tab is polled briskly
// while it is open — that is where someone waits for the verdict.
var CADENCE = { quick: 60000, history: 30000, events: 5000, capabilities: 30000,
                tests: 5000 };

var TIMER = null;
var PAUSED = false;
var BUSY = false;

function markBusy(on) {
  BUSY = on;
  var button = $("autorefresh");
  if (!button) return;
  button.classList.toggle("busy", on && !PAUSED);
}

function tick(force) {
  if (PAUSED && !force) return;
  if (document.hidden && !force) return;   // nothing to see; do not poll
  if (BUSY) return;                        // a slow reply must not stack up

  markBusy(true);
  var now = Date.now();
  var jobs = [refresh()];

  if (STATE.tab === "overview" && now - LAST.quick >= CADENCE.quick) {
    LAST.quick = now;
    jobs.push(loadQuickChart());
  }
  if (STATE.tab === "history" && now - LAST.history >= CADENCE.history) {
    LAST.history = now;
    jobs.push(loadHistory(STATE.range), loadOutages());
  }
  if (STATE.tab === "events" && now - LAST.events >= CADENCE.events) {
    LAST.events = now;
    jobs.push(loadEvents());
  } else if (STATE.tab === "overview" && now - LAST.events >= CADENCE.events) {
    LAST.events = now;
    jobs.push(loadEvents());       // the overview shows the six most recent
  }
  if (STATE.tab === "tests" && now - LAST.tests >= CADENCE.tests) {
    LAST.tests = now;
    jobs.push(loadTests());
  }
  if (STATE.tab === "control" && now - LAST.capabilities >= CADENCE.capabilities) {
    LAST.capabilities = now;
    jobs.push(loadCapabilities());
  }

  Promise.all(jobs.map(function (p) {
    return p && p.catch ? p.catch(function () {}) : p;
  })).then(function () { markBusy(false); });
}

function startRefreshing() {
  if (TIMER) clearInterval(TIMER);
  TIMER = setInterval(tick, REFRESH_MS);
}

function setPaused(paused) {
  PAUSED = paused;
  var button = $("autorefresh");
  if (button) {
    button.classList.toggle("paused", paused);
    button.textContent = paused ? "paused" : "auto " + (REFRESH_MS / 1000) + "s";
    button.title = paused ? "click to resume automatic refreshing"
                          : "click to pause automatic refreshing";
  }
  if (!paused) tick(true);
}

var refreshButton = $("autorefresh");
if (refreshButton) {
  refreshButton.onclick = function () { setPaused(!PAUSED); };
}

// Polling a hidden tab wastes the daemon's time and the laptop's battery.
// Coming back to it should show current data at once, not five seconds later.
document.addEventListener("visibilitychange", function () {
  if (!document.hidden && !PAUSED) tick(true);
});

// First paint. The snapshot is already embedded by PHP, so the page is useful
// before the first request completes.
var initial = <?= json_encode($snapshot ?: null, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE) ?>;
if (initial && initial.online !== false) renderSnapshot(initial);
loadQuickChart();
loadEvents();
LAST.quick = LAST.events = Date.now();
refresh();
startRefreshing();

var wanted = (location.hash || "").replace("#", "");
if (["overview", "history", "tests", "events", "control", "all"].indexOf(wanted) >= 0) showTab(wanted);
</script>
</body>
</html>
