<?php
/**
 * upsmon-api.php — the page's side of the daemon API.
 *
 * index.php collects nothing itself. The daemon does all the work and exposes
 * it on http://127.0.0.1:<port>; this file is the thin client that talks to it.
 * Read endpoints are open; the two that change something (running a command,
 * writing a variable) send the token the daemon dropped in a file readable only
 * by root and the web server's group.
 *
 * The browser never sees the token, and the web user holds no upsd credentials
 * — the daemon owns those and is the only thing that talks to the UPS.
 */
declare(strict_types=1);

const UPSMON_HOST  = '127.0.0.1';
const UPSMON_PORT  = 9848;
const UPSMON_TOKEN = '/run/upsmon/api-token';

/** Read the API token the daemon wrote for us, or null if we can't. */
function upsmon_token(): ?string
{
    if (!is_readable(UPSMON_TOKEN)) {
        return null;
    }
    $token = trim((string) @file_get_contents(UPSMON_TOKEN));
    return $token !== '' ? $token : null;
}

/**
 * Call the daemon. Returns [status, body-string]; status 0 means unreachable.
 * $payload, when given, is sent as a JSON body and turns this into a POST.
 */
function upsmon_call(string $path, string $method = 'GET', ?array $payload = null,
                     float $timeout = 10.0): array
{
    $headers = ['Accept: application/json'];
    $body    = null;

    if ($payload !== null) {
        $body = json_encode($payload, JSON_UNESCAPED_UNICODE);
        $headers[] = 'Content-Type: application/json';
        $headers[] = 'Content-Length: ' . strlen($body);
    }
    if ($method !== 'GET') {
        $token = upsmon_token();
        if ($token === null) {
            return [0, ''];
        }
        $headers[] = 'X-Upsmon-Token: ' . $token;
        if ($body === null) {
            $headers[] = 'Content-Length: 0';
        }
    }

    $context = stream_context_create(['http' => [
        'method'        => $method,
        'timeout'       => $timeout,
        'ignore_errors' => true,
        'header'        => implode("\r\n", $headers),
        'content'       => $body,
    ]]);

    $url = 'http://' . UPSMON_HOST . ':' . UPSMON_PORT . $path;
    $reply = @file_get_contents($url, false, $context);
    if ($reply === false) {
        return [0, ''];
    }

    $status = 0;
    foreach (($http_response_header ?? []) as $header) {
        if (preg_match('#^HTTP/\S+\s+(\d+)#', $header, $m)) {
            $status = (int) $m[1];
        }
    }
    return [$status, (string) $reply];
}

/** Call the daemon and return the decoded reply, or null. */
function upsmon_get(string $path, float $timeout = 10.0): ?array
{
    [$status, $body] = upsmon_call($path, 'GET', null, $timeout);
    if ($status !== 200) {
        return null;
    }
    $data = json_decode($body, true);
    return is_array($data) ? $data : null;
}

/** Pass a daemon reply straight through to the browser. */
function upsmon_relay(string $path, string $method = 'GET', ?array $payload = null,
                      float $timeout = 10.0): void
{
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');

    if ($method !== 'GET' && upsmon_token() === null) {
        http_response_code(503);
        echo json_encode(['ok' => false, 'error' =>
            'the API token is unreadable — is upsmon.service running, and is '
            . UPSMON_TOKEN . ' readable by the web server?']);
        return;
    }

    [$status, $body] = upsmon_call($path, $method, $payload, $timeout);
    if ($status === 0) {
        http_response_code(503);
        echo json_encode(['ok' => false, 'error' =>
            'the monitoring daemon is not responding — is upsmon.service running?']);
        return;
    }
    http_response_code($status);
    echo $body;
}
