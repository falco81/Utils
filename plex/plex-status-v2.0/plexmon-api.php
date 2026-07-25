<?php
/**
 * plexmon-api.php — the page's side of the daemon API.
 *
 * index.php no longer collects anything itself. The daemon does all the work and
 * exposes it on http://127.0.0.1:<port>; this file is the thin client that talks
 * to it. Read endpoints are open; the two privileged ones (wake, force SMART)
 * send the token the daemon dropped in a root-owned file the web user can read.
 *
 * None of this can wake a disk — waking happens inside the daemon, and only when
 * the daemon decides to.
 */
declare(strict_types=1);

const PLEXMON_HOST  = '127.0.0.1';
const PLEXMON_PORT  = 9847;
const PLEXMON_TOKEN = '/run/plex-status/api-token';

/** Read the API token the daemon wrote for us, or null if we can't. */
function plexmon_token(): ?string {
    if (!is_readable(PLEXMON_TOKEN)) return null;
    $t = trim((string) @file_get_contents(PLEXMON_TOKEN));
    return $t !== '' ? $t : null;
}

/**
 * Call the daemon. Returns [status, body-string] or [0, ''] if unreachable.
 * $auth adds the token header for privileged POSTs.
 */
function plexmon_call(string $path, string $method = 'GET',
                      bool $auth = false, float $timeout = 8.0): array {
    $ctx = [
        'http' => [
            'method'        => $method,
            'timeout'       => $timeout,
            'ignore_errors' => true,
            'header'        => [],
        ],
    ];
    if ($auth) {
        $tok = plexmon_token();
        if ($tok === null) return [0, ''];
        $ctx['http']['header'][] = 'X-Plexmon-Token: ' . $tok;
    }
    if ($method === 'POST') {
        $ctx['http']['header'][] = 'Content-Length: 0';
    }
    $url = 'http://' . PLEXMON_HOST . ':' . PLEXMON_PORT . $path;
    $body = @file_get_contents($url, false, stream_context_create($ctx));
    if ($body === false) return [0, ''];
    $status = 0;
    foreach (($http_response_header ?? []) as $h) {
        if (preg_match('#^HTTP/\S+\s+(\d+)#', $h, $m)) $status = (int) $m[1];
    }
    return [$status, (string) $body];
}

/** Pass a daemon JSON reply straight through to the browser. */
function plexmon_relay(string $path, string $method = 'GET', bool $auth = false,
                      float $timeout = 8.0): void {
    // Pairing a television waits on a person, so the caller can ask for longer.
    // PHP's own execution limit has to be lifted with it, or the script is cut
    // off while the daemon is still waiting.
    if ($timeout > 25.0) {
        @set_time_limit((int) ceil($timeout) + 30);
        @ini_set('default_socket_timeout', (string) ((int) ceil($timeout) + 10));
    }
    [$status, $body] = plexmon_call($path, $method, $auth, $timeout);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    if ($status === 0) {
        http_response_code(503);
        echo json_encode(['ok' => false,
            'error' => 'the monitoring daemon is not responding — is plex-status.service running?']);
        return;
    }
    http_response_code($status);
    echo $body;
}
