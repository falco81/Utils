<?php
/**
 * index.php — Plex server status page.
 * Reads data.json produced by the collector (runs as root via a systemd timer).
 * Does nothing privileged itself.
 */
declare(strict_types=1);

const DATA_FILE = __DIR__ . '/data.json';

$data = is_readable(DATA_FILE) ? json_decode((string) file_get_contents(DATA_FILE), true) : null;

function h($s): string { return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8'); }

function human_uptime(int $s): string {
    $d = intdiv($s, 86400); $s %= 86400;
    $hh = intdiv($s, 3600);  $s %= 3600;
    $mm = intdiv($s, 60);
    $out = [];
    if ($d)  $out[] = "{$d}d";
    if ($hh) $out[] = "{$hh}h";
    $out[] = "{$mm}m";
    return implode(' ', $out);
}

function bytes(int $b): string {
    $u = ['B', 'KB', 'MB', 'GB', 'TB']; $i = 0; $v = (float) $b;
    while ($v >= 1024 && $i < count($u) - 1) { $v /= 1024; $i++; }
    return round($v, 1) . ' ' . $u[$i];
}

function temp_class(?int $t, array $thr): string {
    if ($t === null) return 'muted';
    if ($t >= ($thr['temp_crit'] ?? 55)) return 'crit';
    if ($t >= ($thr['temp_warn'] ?? 45)) return 'warn';
    return 'ok';
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
                      'Normal activity, not a problem. The system disk shows this most of the time because Plex writes its database, logs and metadata continuously.'],
        'read'    => ['The disk was being read from when this was sampled.',
                      'Normal activity — playback, a library scan or thumbnail generation.'],
        'idle'    => ['Spinning and ready, with no I/O during the sample.',
                      'The healthy resting state for a disk that is in use.'],
        'standby' => ['Spun down to save power.',
                      'SMART values shown here come from the last time the disk was awake, so it is not woken just to draw this page.'],
        'sleep'   => ['In a deeper sleep state than standby.',
                      'SMART values shown here are cached from when the disk was last awake.'],
        'unknown' => ['Power state could not be read from this device.',
                      'Some USB bridges do not report it. Harmless.'],
    ][$cls] ?? ['', ''];
}

$thr = $data['thresholds'] ?? ['temp_warn' => 45, 'temp_crit' => 55];
$cpuThr = ['temp_warn' => 70, 'temp_crit' => 85]; // CPUs run hotter than disks
$overall = $data['overall'] ?? 'unknown';
$overallLabel = ['ok' => 'All healthy', 'warn' => 'Warning', 'crit' => 'Problem', 'unknown' => 'Unknown'][$overall] ?? 'Unknown';
$age = $data ? time() - ($data['generated'] ?? time()) : null;

// sort disk cards by mountpoint (natural order); unmounted last
if ($data && !empty($data['disks'])) {
    usort($data['disks'], function ($a, $b) {
        $am = $a['mount'] ?? null; $bm = $b['mount'] ?? null;
        if ($am === null && $bm === null) return strcmp($a['dev'], $b['dev']);
        if ($am === null) return 1;
        if ($bm === null) return -1;
        return strnatcmp($am, $bm);
    });
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Plex — server status<?= isset($data['hostname']) ? ' · ' . h($data['hostname']) : '' ?></title>
<style>
  :root{
    --bg:#0e1116; --panel:#171b22; --panel2:#1d222b; --line:#262c37;
    --tx:#e6e9ef; --tx-dim:#8b94a3; --tx-mut:#5b6472;
    --ok:#3fb950; --warn:#d29922; --crit:#f85149; --info:#58a6ff; --write:#a371f7; --sleep:#6e7681;
    --ok-bg:rgba(63,185,80,.12); --warn-bg:rgba(210,153,34,.12);
    --crit-bg:rgba(248,81,73,.13); --info-bg:rgba(88,166,255,.12);
    --write-bg:rgba(163,113,247,.14); --sleep-bg:rgba(110,118,129,.14);
    --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;padding:28px 20px 60px}
  .wrap{max-width:1100px;margin:0 auto}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

  header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:24px}
  header h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
  header .host{color:var(--tx-dim);font-weight:400}
  .spacer{flex:1}
  .updated{color:var(--tx-mut);font-size:13px;text-align:right}

  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;
        font-weight:600;font-size:14px;border:1px solid transparent}
  .pill .dot{width:9px;height:9px;border-radius:50%}
  .pill.ok{background:var(--ok-bg);color:var(--ok);border-color:rgba(63,185,80,.3)}
  .pill.warn{background:var(--warn-bg);color:var(--warn);border-color:rgba(210,153,34,.3)}
  .pill.crit{background:var(--crit-bg);color:var(--crit);border-color:rgba(248,81,73,.3)}
  .pill.unknown{background:var(--panel2);color:var(--tx-dim)}
  .pill.ok .dot{background:var(--ok)} .pill.warn .dot{background:var(--warn)}
  .pill.crit .dot{background:var(--crit);animation:pulse 1.4s infinite} .pill.unknown .dot{background:var(--tx-mut)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.45}}

  .grid{display:grid;gap:14px}
  .top{grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:14px}
  .disks{grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px}
  .card h2{margin:0 0 14px;font-size:12px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--tx-mut)}

  .kv{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;font-size:14px}
  .kv .k{color:var(--tx-dim)} .kv .v{font-weight:600}
  .kv+.kv{border-top:1px solid rgba(38,44,55,.6)}
  .big{font-size:30px;font-weight:700;line-height:1.1;margin:2px 0 2px}
  .sub{color:var(--tx-mut);font-size:13px}

  /* disk card */
  .disk{position:relative;transition:opacity .3s}
  .disk.asleep{opacity:.62}
  .disk .dhead{display:flex;align-items:center;gap:10px;margin-bottom:4px}
  .disk .dname{font-size:17px;font-weight:700}
  .badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;letter-spacing:.4px;text-transform:uppercase}
  .badge.usb{background:var(--info-bg);color:var(--info)}
  .badge.nvme{background:rgba(163,113,247,.14);color:#a371f7}
  .badge.sata{background:var(--panel2);color:var(--tx-dim)}
  .disk .model{color:var(--tx-dim);font-size:13px;margin-bottom:14px;word-break:break-word}
  .disk .mnt{color:var(--info);font-weight:600}

  /* prominent state badge */
  .state{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;
         font-size:13px;font-weight:700;letter-spacing:.3px;border:1px solid transparent}
  .state .sdot{width:8px;height:8px;border-radius:50%}
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

  .temp{display:flex;align-items:baseline;gap:8px;margin:14px 0}
  .temp .t{font-size:34px;font-weight:800;line-height:1}
  .temp .t.ok{color:var(--ok)} .temp .t.warn{color:var(--warn)}
  .temp .t.crit{color:var(--crit)} .temp .t.muted{color:var(--tx-mut)}
  .temp .health{margin-left:auto}

  .bar{height:8px;border-radius:6px;background:var(--panel2);overflow:hidden;margin:6px 0 4px}
  .bar > i{display:block;height:100%;border-radius:6px;background:var(--info)}
  .bar.warn > i{background:var(--warn)} .bar.crit > i{background:var(--crit)}

  .attrs{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;margin-top:14px;
         padding-top:14px;border-top:1px solid var(--line);font-size:13px}
  .attrs .a{display:flex;justify-content:space-between}
  .attrs .a .an{color:var(--tx-mut)} .attrs .a .av{font-weight:600}
  .attrs .a .av.bad{color:var(--crit)}

  .flag{color:var(--ok)} .flag.warn{color:var(--warn)} .flag.crit{color:var(--crit)} .flag.muted{color:var(--tx-mut)}
  .cachenote{font-size:11px;color:var(--tx-mut);margin-left:6px}

  /* status badge tooltip */
  .tip{position:relative;display:inline-flex}
  .tip .pill{cursor:help}
  .tip .why{
    position:absolute;top:calc(100% + 10px);left:0;z-index:50;
    min-width:300px;max-width:480px;padding:12px 14px;
    background:var(--panel2);border:1px solid var(--line);border-radius:10px;
    box-shadow:0 10px 30px rgba(0,0,0,.5);
    opacity:0;visibility:hidden;transform:translateY(-4px);
    transition:opacity .15s,transform .15s,visibility .15s;
    text-align:left;font-weight:400;
  }
  .tip:hover .why,.tip:focus-within .why{opacity:1;visibility:visible;transform:translateY(0)}
  .tip .why::before{
    content:"";position:absolute;bottom:100%;left:18px;
    border:7px solid transparent;border-bottom-color:var(--line);
  }
  .why h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--tx-mut);font-weight:700}
  .why ul{margin:0;padding:0;list-style:none}
  .why li{display:flex;gap:8px;padding:4px 0;font-size:13px;line-height:1.45;color:var(--tx)}
  .why li+li{border-top:1px solid rgba(38,44,55,.6)}
  .why li .lv{flex:0 0 auto;width:7px;height:7px;border-radius:50%;margin-top:7px}
  .why li.crit .lv{background:var(--crit)}
  .why li.warn .lv{background:var(--warn)}
  .why .allgood{font-size:13px;color:var(--ok)}
  @media (max-width:640px){ .tip .why{min-width:0;width:min(88vw,420px)} }

  /* compact tooltip for badges inside a disk card (right-aligned) */
  .tip-sm .why{min-width:250px;max-width:330px;left:auto;right:0;padding:10px 12px}
  .tip-sm .why::before{left:auto;right:16px}
  .tip-sm .why p{margin:0;font-size:12.5px;line-height:1.5;color:var(--tx)}
  .tip-sm .why p+p{margin-top:7px;color:var(--tx-dim)}
  .tip-sm .state,.tip-sm .pill{cursor:help}

  .empty{background:var(--crit-bg);border:1px solid rgba(248,81,73,.3);color:#ffb3ae;
         padding:20px;border-radius:var(--radius);text-align:center}
  footer{margin-top:26px;text-align:center;color:var(--tx-mut);font-size:12px}
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
  $sys = $data['system']; $plex = $data['plex'];
?>

  <header>
    <h1>Plex server <span class="host">· <?= h($data['hostname']) ?></span></h1>
    <?php $reasons = $data['reasons'] ?? []; ?>
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
          <div class="sub">No details available — run the collector again to record
            the reasons behind this status.</div>
        <?php else: ?>
          <div class="allgood">All checks passed — no warnings.</div>
        <?php endif; ?>
      </span>
    </span>
    <div class="spacer"></div>
    <div class="updated">
      updated <?= $age < 90 ? "{$age}s ago" : human_uptime((int)$age) . ' ago' ?><br>
      <span class="mono"><?= h(date('H:i:s', $data['generated'])) ?></span>
    </div>
  </header>

  <div class="grid top">
    <div class="card">
      <h2>Plex</h2>
      <div class="big flag <?= $plex['active'] ? 'ok' : 'crit' ?>"><?= $plex['active'] ? 'Running' : 'Stopped' ?></div>
      <div class="sub">web <?= h($plex['web_version'] ?? '—') ?> · server <?= h($plex['version'] ?? '—') ?></div>
      <?php if ($plex['sessions'] !== null): ?>
        <div class="kv" style="margin-top:10px"><span class="k">Active streams</span><span class="v"><?= (int) $plex['sessions'] ?></span></div>
      <?php endif; ?>
    </div>
    <div class="card">
      <h2>Load</h2>
      <div class="big"><?= h($sys['load'][0]) ?></div>
      <div class="sub"><?= h($sys['load'][1]) ?> / <?= h($sys['load'][2]) ?> · <?= (int) $sys['ncpu'] ?> CPU</div>
      <?php if ($sys['cpu_temp'] !== null): ?>
        <div class="kv" style="margin-top:10px"><span class="k">CPU temp</span>
          <span class="v flag <?= temp_class($sys['cpu_temp'], $cpuThr) ?>"><?= (int) $sys['cpu_temp'] ?> °C</span></div>
      <?php endif; ?>
    </div>
    <div class="card">
      <h2>Memory</h2>
      <?php $mp = $sys['mem_total'] ? (int) round($sys['mem_used'] / $sys['mem_total'] * 100) : 0; ?>
      <div class="big"><?= $mp ?><span style="font-size:16px;color:var(--tx-mut)"> %</span></div>
      <div class="sub"><?= bytes($sys['mem_used']) ?> / <?= bytes($sys['mem_total']) ?></div>
      <div class="bar <?= $mp >= 90 ? 'crit' : ($mp >= 75 ? 'warn' : '') ?>" style="margin-top:12px"><i style="width:<?= $mp ?>%"></i></div>
    </div>
    <div class="card">
      <h2>System</h2>
      <div class="big" style="font-size:22px"><?= human_uptime($sys['uptime_s']) ?></div>
      <div class="sub">uptime</div>
      <div style="margin-top:12px">
        <div class="sub" style="margin-bottom:2px">Kernel</div>
        <div class="mono" style="font-size:12px;word-break:break-all"><?= h($sys['kernel']) ?></div>
      </div>
    </div>
  </div>

  <div class="grid disks">
  <?php foreach ($data['disks'] as $d):
      $tran = strtolower($d['tran']);
      $tclass = temp_class($d['temp'], $thr);
      $pct = $d['fs_pct'];
      // Only the OS disk gets warning colours: media drives are expected to be
      // nearly full, so a red bar there would be misleading.
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
          [$h1, $h2] = state_help($stClass);
          $psrc = $d['power_src'] ?? '';
          $inferred = str_starts_with($psrc, 'inferred') || ($d['power_reliable'] ?? true) === false;
          $idle = $d['idle_for_s'] ?? null;
        ?>
        <span class="tip tip-sm" tabindex="0" style="margin-left:auto">
          <span class="state <?= $stClass ?>"><span class="sdot"></span><?= $stLabel ?></span>
          <span class="why">
            <p><?= h($h1) ?></p>
            <p><?= h($h2) ?></p>
            <?php if ($inferred): ?>
              <p>This USB bridge does not report power state, so it is worked out from
                 disk activity<?= $idle !== null ? ' (idle for ' . h(human_uptime((int) $idle)) . ')' : '' ?>.</p>
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
        <?php if ($d['from_cache']): ?><span class="cachenote">cached<?= $d['cache_age'] !== null ? ' ' . human_uptime((int)$d['cache_age']) . ' ago' : '' ?></span>
        <?php elseif (!empty($d['smart_limited'])): ?><span class="cachenote">bridge exposes health only</span><?php endif; ?>
        <span class="pill <?= $hcls === 'muted' ? 'unknown' : $hcls ?> health"><span class="dot"></span><?= $hlabel ?></span>
      </div>

      <?php if ($d['mount'] !== null): ?>
        <div class="kv" style="padding-top:0"><span class="k">Usage</span><span class="v"><?= $pct !== null ? $pct.' %' : '—' ?></span></div>
        <div class="bar <?= $barcls ?>"><i style="width:<?= (int) $pct ?>%"></i></div>
        <div class="sub"><?= h($d['fs_used'] ?? '?') ?> / <?= h($d['fs_size'] ?? '?') ?></div>
      <?php else: ?>
        <div class="sub flag muted">not mounted</div>
      <?php endif; ?>

      <div class="attrs">
      <?php if ($tran === 'nvme'): ?>
        <div class="a"><span class="an">Endurance used</span>
          <span class="av"><?= $d['nvme_used'] !== null ? (int) $d['nvme_used'].' %' : '—' ?></span></div>
        <div class="a"><span class="an">Spare left</span>
          <span class="av <?= $d['nvme_spare'] !== null && $d['nvme_spare'] < 20 ? 'bad' : '' ?>"><?= $d['nvme_spare'] !== null ? (int) $d['nvme_spare'].' %' : '—' ?></span></div>
        <div class="a"><span class="an">Media errors</span>
          <span class="av <?= (int) $d['nvme_media_err'] > 0 ? 'bad' : '' ?>"><?= $d['nvme_media_err'] ?? '—' ?></span></div>
        <div class="a"><span class="an">Power-on</span>
          <span class="av"><?= $d['poh'] !== null ? number_format((int)$d['poh'], 0, '.', ' ').' h' : '—' ?></span></div>
      <?php else: ?>
        <div class="a"><span class="an">Reallocated</span>
          <span class="av <?= (int) $d['realloc'] > 0 ? 'bad' : '' ?>"><?= $d['realloc'] ?? '—' ?></span></div>
        <div class="a"><span class="an">Pending</span>
          <span class="av <?= (int) $d['pending'] > 0 ? 'bad' : '' ?>"><?= $d['pending'] ?? '—' ?></span></div>
        <div class="a"><span class="an">Uncorrectable</span>
          <span class="av <?= (int) $d['uncorrect'] > 0 ? 'bad' : '' ?>"><?= $d['uncorrect'] ?? '—' ?></span></div>
        <div class="a"><span class="an">Power-on</span>
          <span class="av"><?= $d['poh'] !== null ? number_format((int)$d['poh'], 0, '.', ' ').' h' : '—' ?></span></div>
      <?php endif; ?>
      </div>
    </div>
  <?php endforeach; ?>
  </div>

  <footer>
    data.json age <?= (int) $age ?>s · collector runs as root via systemd timer · page auto-refreshes every 60s
  </footer>

<?php endif; ?>
</div>
</body>
</html>
