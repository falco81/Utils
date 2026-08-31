#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upsmon.py - UPS monitoring daemon for a UPS published by a Synology NAS (or any
other host running Network UPS Tools).

One file, Python standard library only, nothing to pip install.

It polls upsd over TCP 3493, keeps a long-term history in SQLite, and exposes
everything on a localhost HTTP API that the PHP dashboard reads. Control
actions (self tests, beeper, writable settings) are proxied through the same
API and guarded by a token, so the web user never holds upsd credentials.

Runs as a systemd service, but every function is also available from the
command line for testing:

    upsmon --status            one-off report, straight from the UPS
    upsmon --oneshot           one collection into the database, then exit
    upsmon --list-rw           what this UPS lets you change
    upsmon --set VAR=VALUE     change a writable variable
    upsmon --exec COMMAND      run an instant command
    upsmon --check             health check against the running daemon
    upsmon --diag              API latency, database size, config in force
    upsmon --history 24h       recent samples as a table
    upsmon                     run the daemon in the foreground
"""

# Bumped by hand with every change to this file.
__version__ = '3.4.4'

import argparse
import getpass
import json
import os
import re
import signal
import socket
import sqlite3
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------
CONFIG_FILE = Path(os.environ.get('UPSMON_CONFIG', '/etc/upsmon/config.json'))
STATE_DIR = Path('/var/lib/upsmon')
DB_FILE = STATE_DIR / 'history.db'
RUN_DIR = Path('/run/upsmon')
TOKEN_FILE = RUN_DIR / 'api-token'


def token_path():
    return Path(CONFIG.get('token_file') or str(TOKEN_FILE))

DEFAULTS = {
    # --- where the UPS lives -----------------------------------------------
    'nut_host': '127.0.0.1',
    'nut_port': 3493,
    'ups_name': '',              # empty = use the first UPS the server reports
    'nut_username': '',          # only needed for control actions
    'nut_password': '',
    'nut_timeout': 5.0,

    # --- our own API --------------------------------------------------------
    'api_host': '127.0.0.1',
    'api_port': 9848,
    # Shared secret for the privileged endpoints. It must be readable by the
    # web server; /run is cleared on every boot, which is why it lives there.
    'token_file': '/run/upsmon/api-token',

    # --- timing (seconds) ---------------------------------------------------
    'poll_interval': 10,         # how often we ask the UPS for its variables
    'sample_interval': 60,       # how often a sample is written to the database
    'sample_interval_battery': 10,   # ...while running on battery
    'aggregate_interval': 3600,  # how often old samples are rolled up
    # A status flag must be seen this many polls running before it counts.
    # usbhid-ups occasionally reports a single bad read - LB and RB appearing
    # for exactly one poll and vanishing again - and without this the event log
    # fills with alarms that never happened. 1 disables the debounce.
    'flag_confirm_polls': 2,

    # --- retention ----------------------------------------------------------
    'retain_full_days': 14,      # full-resolution samples
    'retain_hourly_days': 730,   # hourly averages
    'retain_events_days': 730,

    # --- thresholds used for the health verdict -----------------------------
    'charge_warn': 50,
    'charge_crit': 25,
    'load_warn': 70,
    'load_crit': 90,
    'runtime_warn_s': 300,
    'runtime_crit_s': 120,
    'battery_life_years': 4.0,

    # --- logging ------------------------------------------------------------
    # Empty disables the file and leaves everything to the journal.
    'log_file': '/var/log/upsmon/upsmon.log',
    'log_level': 'info',         # 'debug' adds poll-by-poll detail
    'log_to_journal': True,      # also write to stdout, which systemd captures

    # --- smart plug (Shelly Gen2+) ------------------------------------------
    # Empty host disables everything below. The plug speaks JSON-RPC over plain
    # HTTP on the LAN; no cloud account is involved and enabling it here does
    # not interfere with the Shelly app, Matter or anything else already using
    # the device.
    'plug_host': '',
    'plug_password': '',          # only if one is set in the plug's web UI
    'plug_switch_id': 0,          # which relay; single-socket plugs are always 0
    'plug_poll_interval': 15,
    # Gen3 firmware with authentication on answers 429 when requests arrive
    # too close together, and the two a poll needs would otherwise go out
    # milliseconds apart. Spacing them costs nothing and stops the flapping.
    'plug_min_request_gap': 1.0,
    'plug_sample_interval': 60,
    'plug_timeout': 5.0,

    # --- pushed sensor readings ---------------------------------------------
    # Battery sensors such as the Shelly H&T sleep between measurements, so
    # nothing can poll them. They push instead: point their webhook here.
    # A sensor can also be addressed directly, which works whenever it happens
    # to be awake - always, if it runs on USB power.
    'sensor_host': '',
    'sensor_password': '',

    'sensor_listen': False,
    'sensor_listen_host': '0.0.0.0',
    'sensor_listen_port': 8088,
    'sensor_retain_days': 730,

    # --- safety -------------------------------------------------------------
    # Commands that cut power to the load are refused through the API unless
    # this is turned on. The dashboard hides them entirely when it is off.
    'allow_dangerous_commands': False,

    # A four-digit PIN asked for before anything is written or run. Empty
    # disables it. This is a guard against a misclick or a passer-by, not a
    # secret: four digits is 10 000 combinations, so the lockout below is what
    # actually makes it worth having.
    'pin': '',
    'pin_attempts': 5,
    'pin_lockout_s': 300,
}

CONFIG = dict(DEFAULTS)
CONFIG_NOTES = []
DEBUG = False


CONFIG_FATAL = []


def load_config():
    """File first, then UPSMON_* environment variables, which win."""
    if CONFIG_FILE.is_file():
        try:
            raw = json.loads(CONFIG_FILE.read_text())
            if not isinstance(raw, dict):
                CONFIG_FATAL.append('%s is not a JSON object' % CONFIG_FILE)
            else:
                for key, value in raw.items():
                    if key not in DEFAULTS:
                        CONFIG_NOTES.append('unknown option "%s" ignored' % key)
                        continue
                    CONFIG[key] = _coerce(DEFAULTS[key], value)
        except PermissionError:
            # Falling back to defaults here would point the daemon at
            # 127.0.0.1 and make it look like the UPS had vanished.
            CONFIG_FATAL.append(
                '%s exists but cannot be read as %s. Fix with:  '
                'sudo chown root:%s %s && sudo chmod 640 %s'
                % (CONFIG_FILE, _whoami(), SERVICE_USER, CONFIG_FILE, CONFIG_FILE))
        except ValueError as e:
            CONFIG_FATAL.append('%s is not valid JSON: %s' % (CONFIG_FILE, e))
        except OSError as e:
            CONFIG_FATAL.append('could not read %s: %s' % (CONFIG_FILE, e))

    for key in DEFAULTS:
        env = os.environ.get('UPSMON_' + key.upper())
        if env is not None:
            CONFIG[key] = _coerce(DEFAULTS[key], env)
            CONFIG_NOTES.append('%s taken from the environment' % key)
    return CONFIG


def _whoami():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return 'uid %d' % os.getuid()


def _coerce(current, value):
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    return str(value)


LOG_HANDLE = None
LOG_PATH = None


def open_log(path=None):
    """Open the log file for appending. Safe to call again to reopen it.

    logrotate renames the file and creates a fresh one, then signals us; at
    that point the old handle still points at the renamed file, so everything
    written afterwards would land somewhere nobody looks.
    """
    global LOG_HANDLE, LOG_PATH
    close_log()
    path = path if path is not None else CONFIG.get('log_file')
    if not path:
        return None
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        LOG_HANDLE = open(str(target), 'a', encoding='utf-8')
        LOG_PATH = target
    except OSError as e:
        LOG_HANDLE, LOG_PATH = None, None
        sys.stderr.write('cannot write the log file %s: %s\n' % (path, e))
    return LOG_HANDLE


def ensure_log_current():
    """Reopen if the file we hold is no longer the one at the configured path.

    Renaming an open file does not break the handle, so a rotation whose signal
    never arrived would leave us writing into the rotated copy indefinitely.
    Comparing inodes catches that.
    """
    if not LOG_HANDLE or not LOG_PATH:
        return False
    try:
        on_disk = os.stat(str(LOG_PATH)).st_ino
        held = os.fstat(LOG_HANDLE.fileno()).st_ino
        same = (on_disk == held)
    except OSError:
        same = False                # the path is gone entirely
    if same:
        return False
    open_log()
    log('log file reopened - it was rotated without a signal reaching us')
    return True


def close_log():
    global LOG_HANDLE
    if LOG_HANDLE:
        try:
            LOG_HANDLE.close()
        except OSError:
            pass
    LOG_HANDLE = None


def log(msg, level='info'):
    if level == 'debug' and not (DEBUG or CONFIG.get('log_level') == 'debug'):
        return
    line = '%s  %-5s %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), level, msg)

    if LOG_HANDLE:
        try:
            LOG_HANDLE.write(line + '\n')
            LOG_HANDLE.flush()
        except (OSError, ValueError):
            # The file went away underneath us - a rotation without the signal
            # reaching us, or the filesystem filling up. Try once to get it
            # back; never let logging take the daemon down.
            try:
                open_log()
                if LOG_HANDLE:
                    LOG_HANDLE.write(line + '\n')
                    LOG_HANDLE.flush()
            except Exception:
                pass

    if CONFIG.get('log_to_journal', True) or not LOG_HANDLE:
        stream = sys.stderr if level in ('error', 'warn') else sys.stdout
        stream.write(line + '\n')
        stream.flush()


# ---------------------------------------------------------------------------
# NUT protocol client
# ---------------------------------------------------------------------------
class NUTError(Exception):
    pass


def tokenize(line):
    """Split a NUT protocol line into tokens, honouring "quotes" and escapes."""
    tokens, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n and line[i] != '"':
                if line[i] == '\\' and i + 1 < n:
                    i += 1
                buf.append(line[i])
                i += 1
            i += 1
            tokens.append(''.join(buf))
        else:
            start = i
            while i < n and not line[i].isspace():
                i += 1
            tokens.append(line[start:i])
    return tokens


class NUTClient(object):
    """Minimal client for the NUT (upsd) protocol."""

    def __init__(self, host=None, port=None, timeout=None):
        self.host = host or CONFIG['nut_host']
        self.port = int(port or CONFIG['nut_port'])
        self.timeout = float(timeout or CONFIG['nut_timeout'])
        self.sock = None
        self.f = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as e:
            raise NUTError('cannot reach upsd at %s:%d (%s)' % (self.host, self.port, e))
        self.sock.settimeout(self.timeout)
        self.f = self.sock.makefile('rwb')

    def close(self):
        try:
            if self.f:
                self._send('LOGOUT')
                self.f.close()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.f = self.sock = None

    def _send(self, cmd):
        self.f.write((cmd + '\n').encode('utf-8'))
        self.f.flush()

    def _readline(self):
        try:
            raw = self.f.readline()
        except socket.timeout:
            raise NUTError('timed out waiting for upsd')
        if not raw:
            raise NUTError('upsd closed the connection')
        return raw.decode('utf-8', 'replace').rstrip('\r\n')

    def command(self, cmd):
        self._send(cmd)
        line = self._readline()
        if line.startswith('ERR '):
            raise NUTError(line[4:])
        return line

    def list_query(self, subcmd):
        self._send('LIST ' + subcmd)
        first = self._readline()
        if first.startswith('ERR '):
            raise NUTError(first[4:])
        if not first.startswith('BEGIN LIST'):
            raise NUTError('unexpected reply: ' + first)
        rows = []
        while True:
            line = self._readline()
            if line.startswith('ERR '):
                raise NUTError(line[4:])
            if line.startswith('END LIST'):
                break
            rows.append(tokenize(line))
        return rows

    # -- high level ---------------------------------------------------------
    def login(self, username, password):
        self.command('USERNAME ' + username)
        self.command('PASSWORD ' + password)

    def server_version(self):
        try:
            return self.command('VER')
        except NUTError:
            return '?'

    def list_ups(self):
        out = []
        for r in self.list_query('UPS'):
            if len(r) >= 2:
                out.append((r[1], r[2] if len(r) > 2 else ''))
        return out

    def list_vars(self, ups):
        return {r[2]: r[3] for r in self.list_query('VAR ' + ups) if len(r) >= 4}

    def list_rw(self, ups):
        try:
            return {r[2]: r[3] for r in self.list_query('RW ' + ups) if len(r) >= 4}
        except NUTError:
            return {}

    def list_cmds(self, ups):
        try:
            return [r[2] for r in self.list_query('CMD ' + ups) if len(r) >= 3]
        except NUTError:
            return []

    def list_enum(self, ups, var):
        try:
            return [r[-1] for r in self.list_query('ENUM %s %s' % (ups, var))]
        except NUTError:
            return []

    def list_range(self, ups, var):
        try:
            return [(r[-2], r[-1]) for r in self.list_query('RANGE %s %s' % (ups, var))]
        except NUTError:
            return []

    def get_type(self, ups, var):
        try:
            return ' '.join(tokenize(self.command('GET TYPE %s %s' % (ups, var)))[3:])
        except NUTError:
            return ''

    def set_var(self, ups, var, value):
        return self.command('SET VAR %s %s "%s"' % (ups, var, value.replace('"', '\\"')))

    def instcmd(self, ups, cmd):
        return self.command('INSTCMD %s %s' % (ups, cmd))

    def resolve_ups(self, wanted=''):
        available = self.list_ups()
        if not available:
            raise NUTError('upsd reports no UPS')
        if wanted:
            for name, _ in available:
                if name == wanted:
                    return name
            raise NUTError('UPS "%s" not found; available: %s'
                           % (wanted, ', '.join(n for n, _ in available)))
        return available[0][0]


def connect_ups(authenticate=False, username=None, password=None):
    """Open a client, optionally logging in, and resolve the UPS name."""
    client = NUTClient()
    client.connect()
    user = username if username is not None else CONFIG['nut_username']
    pw = password if password is not None else CONFIG['nut_password']
    if authenticate and user:
        client.login(user, pw or '')
    name = client.resolve_ups(CONFIG['ups_name'])
    return client, name



# ---------------------------------------------------------------------------
# Shelly Gen2+ RPC
# ---------------------------------------------------------------------------
class PlugError(Exception):
    pass


class ShellyRPC(object):
    """Minimal JSON-RPC client for a Shelly Gen2+ device, standard library only.

    The published clients use `requests`, which this daemon deliberately does
    without. That means implementing HTTP digest authentication by hand: Shelly
    answers the first request with 401 and a challenge, and expects SHA-256
    digest with qop=auth. urllib's own digest handler predates SHA-256 on the
    Python that AlmaLinux 9 ships, so it cannot be relied on here.
    """

    # One challenge is reused for every later call, with the nonce counter
    # stepping on each time. Re-authenticating from scratch doubles the number
    # of HTTP requests, and a Gen3 plug answers 429 when that adds up.
    _challenges = {}
    _last_request = {}
    _pace = threading.Lock()

    def __init__(self, host=None, password=None, timeout=None):
        self.host = host or CONFIG['plug_host']
        self.password = password if password is not None else CONFIG['plug_password']
        self.timeout = float(timeout or CONFIG['plug_timeout'])
        self.retry_after = 0.0

    @property
    def url(self):
        return 'http://%s/rpc' % self.host

    def call(self, method, params=None):
        if not self.host:
            raise PlugError('no plug configured (set plug_host)')
        payload = {'id': 1, 'method': method}
        if params:
            payload['params'] = params
        body = json.dumps(payload).encode('utf-8')

        # Send the credentials straight away when we already hold a challenge.
        known = self._challenges.get(self.host)
        auth = self._digest_header(known, 'POST', '/rpc') if known else None
        status, reply, headers = self._post(body, auth)

        if status == 401:
            challenge = headers.get('WWW-Authenticate', '')
            if not self.password:
                raise PlugError('the plug requires a password; set plug_password')
            self._challenges[self.host] = self._parse_challenge(challenge)
            status, reply, headers = self._post(
                body, self._digest_header(self._challenges[self.host], 'POST', '/rpc'))
            if status == 401:
                self._challenges.pop(self.host, None)
                raise PlugError('the plug rejected the password')
        if status == 429:
            retry = headers.get('Retry-After')
            self.retry_after = to_float(retry) or 0
            # Wait it out and try again, up to three times. Each command-line
            # run starts with no memory of when the last request went out, so
            # invocations a second apart trip the limit through no fault of the
            # caller's - and a device that counts refused requests too needs
            # more than one pause to let the window clear.
            attempt = getattr(self, '_retries', 0)
            if attempt < 3:
                self._retries = attempt + 1
                delay = min(max(self.retry_after, 2.0) * (attempt + 1), 15.0)
                log('the plug asked us to wait; retrying in %.0fs' % delay,
                    level='debug')
                time.sleep(delay)
                try:
                    return self.call(method, params)
                finally:
                    self._retries = attempt
            raise PlugError('the plug is rate limiting us (HTTP 429)'
                            + (' - it asks for %s seconds' % retry if retry else ''))
        if status != 200:
            raise PlugError('the plug answered HTTP %s' % status)

        try:
            result = json.loads(reply.decode('utf-8'))
        except ValueError:
            raise PlugError('the plug did not return JSON - is it a Gen2+ device?')
        if 'error' in result:
            error = result['error']
            raise PlugError('RPC error %s: %s'
                            % (error.get('code'), error.get('message')))
        return result.get('result')

    def _wait_turn(self):
        """Keep a minimum gap between requests to the same device."""
        gap = float(CONFIG.get('plug_min_request_gap') or 0)
        if gap <= 0:
            return
        with self._pace:
            previous = self._last_request.get(self.host, 0.0)
            delay = previous + gap - time.time()
            if delay > 0:
                time.sleep(min(delay, gap))
            self._last_request[self.host] = time.time()

    def _post(self, body, auth=None):
        import urllib.request
        import urllib.error
        self._wait_turn()
        request = urllib.request.Request(self.url, data=body, method='POST')
        request.add_header('Content-Type', 'application/json')
        if auth:
            request.add_header('Authorization', auth)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
        except OSError as e:
            raise PlugError('cannot reach the plug at %s (%s)' % (self.host, e))

    @staticmethod
    def _parse_challenge(header):
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        fields['nc'] = 0
        return fields

    def _digest_header(self, fields, method, path):
        """Answer a digest challenge. Shelly uses SHA-256 with qop=auth."""
        import hashlib
        algorithm = (fields.get('algorithm') or 'SHA-256').upper()
        digest = hashlib.sha256 if 'SHA-256' in algorithm else hashlib.md5

        def h(text):
            return digest(text.encode('utf-8')).hexdigest()

        realm = fields.get('realm', '')
        nonce = fields.get('nonce', '')
        fields['nc'] = int(fields.get('nc', 0)) + 1
        nc = '%08x' % fields['nc']
        cnonce = secrets.token_hex(8)
        # The username is always "admin" on these devices; the web UI does not
        # offer any other, and the password field alone is what it configures.
        ha1 = h('admin:%s:%s' % (realm, self.password))
        ha2 = h('%s:%s' % (method, path))
        response = h('%s:%s:%s:%s:auth:%s' % (ha1, nonce, nc, cnonce, ha2))
        return ('Digest username="admin", realm="%s", nonce="%s", uri="%s", '
                'algorithm=%s, qop=auth, nc=%s, cnonce="%s", response="%s"'
                % (realm, nonce, path, algorithm, nc, cnonce, response))

    # -- the calls this daemon makes ---------------------------------------
    def switch_status(self):
        return self.call('Switch.GetStatus', {'id': int(CONFIG['plug_switch_id'])})

    def device_status(self):
        return self.call('Shelly.GetStatus')

    def device_info(self):
        return self.call('Shelly.GetDeviceInfo')

    def set_output(self, on):
        return self.call('Switch.Set', {'id': int(CONFIG['plug_switch_id']),
                                        'on': bool(on)})

    def toggle(self):
        return self.call('Switch.Toggle', {'id': int(CONFIG['plug_switch_id'])})

    def reset_counters(self, types=None):
        """Reset counters. Without a type the plug resets every one it has.

        Firmware differs on what it will accept here, so the batch is tried
        first and each type separately afterwards. Whether it worked is decided
        by reading the counters back, not by the reply.
        """
        params = {'id': int(CONFIG['plug_switch_id'])}
        if types:
            params['type'] = list(types)
        try:
            return self.call('Switch.ResetCounters', params)
        except PlugError:
            # Older firmware rejects a batch outright when it contains one type
            # it does not know, rather than ignoring that one. Retry each on its
            # own and keep whatever the device accepts.
            accepted = []
            for one in (types or PLUG_COUNTER_TYPES):
                try:
                    self.call('Switch.ResetCounters',
                              {'id': int(CONFIG['plug_switch_id']), 'type': [one]})
                    accepted.append(one)
                except PlugError:
                    continue
            if not accepted:
                raise
            return {'reset': accepted}


def describe_plug_field(path):
    """Fall back to something readable for fields not in the table."""
    tail = re.sub(r'^[a-z_0-9]+:\d+\.', '', path)
    if tail in PLUG_DESCRIPTIONS:
        return PLUG_DESCRIPTIONS[tail]
    return tail.replace('.', ' ').replace('_', ' ').capitalize()


def flatten(obj, prefix=''):
    """Turn a nested RPC reply into dotted paths: aenergy.total, sys.uptime."""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(flatten(value, '%s.%s' % (prefix, key) if prefix else str(key)))
    elif isinstance(obj, list):
        if obj and all(isinstance(item, (int, float)) for item in obj):
            flat[prefix] = obj
        else:
            for index, item in enumerate(obj):
                flat.update(flatten(item, '%s.%d' % (prefix, index)))
    else:
        flat[prefix] = obj
    return flat


# What Switch.ResetCounters accepts, in the order the plug's own web UI lists
# them. Sending no type at all resets everything, which is what "all" does.
PLUG_COUNTERS = [
    {'type': 'aenergy',      'label': 'Active energy',
     'help': 'the kilowatt-hours consumed since the last reset'},
    {'type': 'ret_aenergy',  'label': 'Returned energy',
     'help': 'energy fed back to the grid; always zero on a plain socket'},
    {'type': 'on_time',      'label': 'Total runtime',
     'help': 'how long the relay has been switched on, in total'},
    {'type': 'switch_on',    'label': 'Switching cycles',
     'help': 'how many times the relay has been switched on'},
    {'type': 'on_above_thr', 'label': 'Active load runtime',
     'help': 'time spent above the active load threshold set on the plug'},
]
PLUG_COUNTER_TYPES = [c['type'] for c in PLUG_COUNTERS]


PLUG_DESCRIPTIONS = {
    'output': 'Relay output',
    'apower': 'Active power (W)',
    'voltage': 'Voltage (V)',
    'current': 'Current (A)',
    'pf': 'Power factor',
    'freq': 'Line frequency (Hz)',
    'aenergy.total': 'Energy consumed since the counters were reset (Wh)',
    'aenergy.by_minute': 'Energy in each of the last three minutes (mWh)',
    'aenergy.minute_ts': 'Timestamp of the last minute counted',
    'ret_aenergy.total': 'Energy returned to the grid (Wh)',
    'temperature.tC': 'Plug temperature (deg C)',
    'temperature.tF': 'Plug temperature (deg F)',
    'counts.on_time': 'Total runtime — time the relay has been on (s)',
    'counts.switch_on': 'Switching cycles — times the relay has been switched on',
    'counts.on_above_thr': 'Active load runtime — time above the plug threshold (s)',
    'counts.on_time_rst_ts': 'When the runtime counter was last reset',
    'counts.switch_on_rst_ts': 'When the cycle counter was last reset',
    'counts.on_above_thr_rst_ts': 'When the load-runtime counter was last reset',
    'aenergy.total_rst_ts': 'When the energy counter was last reset',
    'sys.uptime': 'Plug uptime (s)',
    'sys.mac': 'MAC address',
    'sys.available_updates.stable.version': 'Firmware update waiting',
    'wifi.rssi': 'Wi-Fi signal (dBm)',
    'wifi.sta_ip': 'Plug IP address',
    'wifi.ssid': 'Wi-Fi network',
    'cloud.connected': 'Shelly Cloud',
    'mqtt.connected': 'MQTT broker',
    'tC': 'Temperature (deg C)',
    'rh': 'Relative humidity (%)',
    'battery.V': 'Sensor battery (V)',
    'battery.percent': 'Sensor battery (%)',
}


# ---------------------------------------------------------------------------
# Interpreting what the UPS reports
# ---------------------------------------------------------------------------
STATUS_TEXT = {
    'OL': 'on line (mains power)',
    'OB': 'on battery',
    'LB': 'low battery',
    'HB': 'high battery',
    'RB': 'replace battery',
    'CHRG': 'battery charging',
    'DISCHRG': 'battery discharging',
    'BYPASS': 'bypass active',
    'CAL': 'calibration in progress',
    'OFF': 'output off',
    'OVER': 'overloaded',
    'TRIM': 'trimming incoming voltage',
    'BOOST': 'boosting incoming voltage',
    'FSD': 'forced shutdown',
    'ALARM': 'alarm active',
}

CRITICAL_FLAGS = {'OB', 'LB', 'RB', 'OVER', 'FSD', 'ALARM', 'OFF'}
WARNING_FLAGS = {'DISCHRG', 'BYPASS', 'CAL', 'TRIM', 'BOOST'}
DISCHARGE_FLAGS = {'OB', 'DISCHRG'}
# The mains has actually failed. A self test discharges the battery too, but
# reports "OL DISCHRG" - still on line - so it must not be logged as an outage.
OUTAGE_FLAGS = {'OB'}
# Charging state flips constantly, especially during a test. It is visible in
# the status column of every chart; it does not deserve an event each way.
NOISY_FLAGS = {'CHRG', 'DISCHRG'}

DESCRIPTIONS = {
    'device.mfr': 'Device manufacturer',
    'device.model': 'Device model',
    'device.serial': 'Device serial number',
    'device.type': 'Device type',
    'battery.charge': 'Battery charge (percent)',
    'battery.charge.low': 'Low battery threshold (percent)',
    'battery.charge.warning': 'Battery warning threshold (percent)',
    'battery.runtime': 'Remaining runtime (seconds)',
    'battery.runtime.low': 'Remaining runtime threshold (seconds)',
    'battery.voltage': 'Battery voltage (V)',
    'battery.voltage.nominal': 'Nominal battery voltage (V)',
    'battery.type': 'Battery chemistry',
    'battery.date': 'Battery date',
    'battery.mfr.date': 'Battery date - on APC units this is the date the battery '
                        'was last declared new, so set it when you replace one',
    'battery.temperature': 'Battery temperature (deg C)',
    'battery.protection': 'Deep discharge protection',
    'input.voltage': 'Input voltage (V)',
    'input.voltage.nominal': 'Nominal input voltage (V)',
    'input.frequency': 'Input frequency (Hz)',
    'input.transfer.high': 'High transfer voltage point (V)',
    'input.transfer.low': 'Low transfer voltage point (V)',
    'input.sensitivity': 'Input sensitivity',
    'output.voltage': 'Output voltage (V)',
    'output.voltage.nominal': 'Nominal output voltage (V)',
    'output.frequency': 'Output frequency (Hz)',
    'output.frequency.nominal': 'Nominal output frequency (Hz)',
    'output.current': 'Output current (A)',
    'ups.status': 'UPS status flags',
    'ups.load': 'UPS load (percent)',
    'ups.mfr': 'UPS manufacturer',
    'ups.mfr.date': 'Date the UPS was manufactured',
    'ups.model': 'UPS model',
    'ups.serial': 'UPS serial number',
    'ups.firmware': 'Firmware version',
    'ups.firmware.aux': 'Auxiliary firmware version',
    'ups.temperature': 'UPS temperature (deg C)',
    'ups.power': 'Apparent power (VA)',
    'ups.power.nominal': 'Nominal apparent power (VA)',
    'ups.realpower': 'Real power (W)',
    'ups.realpower.nominal': 'Nominal real power (W)',
    'ups.beeper.status': 'Beeper status',
    'ups.delay.shutdown': 'Shutdown delay (seconds)',
    'ups.delay.start': 'Start delay (seconds)',
    'ups.test.result': 'Result of the last self test',
    'ups.timer.shutdown': 'Shutdown timer (seconds)',
    'ups.timer.start': 'Start timer (seconds)',
    'ups.timer.reboot': 'Countdown to a pending reboot (s); 0 or -1 means none',
    'ups.start.battery': 'Allow cold start from battery with no mains present',
    'ups.productid': 'USB product ID',
    'ups.vendorid': 'USB vendor ID',
    'outlet.desc': 'Outlet description (label only, held by the driver)',
    'outlet.id': 'Outlet number',
    'outlet.switchable': 'Whether this outlet can be switched separately',
    'driver.name': 'Driver name',
    'driver.version': 'Driver version',
    'driver.version.internal': 'Internal driver version',
    'driver.version.data': 'Driver data mapping version',
    'driver.version.usb': 'USB backend used by the driver',
    'driver.parameter.port': 'Driver port',
    'driver.parameter.pollinterval': 'Poll interval (seconds)',
    'driver.parameter.pollfreq': 'Full poll interval (seconds)',
    'driver.parameter.synchronous': 'Driver synchronous write mode',
}

SUBJECT_WORDS = {
    'device': 'device', 'ups': 'UPS', 'battery': 'battery', 'input': 'input',
    'output': 'output', 'ambient': 'ambient sensor', 'outlet': 'outlet',
    'driver': 'driver', 'server': 'server', 'experimental': 'experimental',
}
MEASURE_WORDS = {
    'voltage': 'voltage (V)', 'current': 'current (A)', 'frequency': 'frequency (Hz)',
    'power': 'apparent power (VA)', 'realpower': 'real power (W)',
    'temperature': 'temperature (deg C)', 'humidity': 'humidity (%)',
    'charge': 'charge (%)', 'load': 'load (%)', 'runtime': 'runtime (s)',
    'capacity': 'capacity', 'energy': 'energy', 'delay': 'delay (s)',
    'timer': 'timer (s)', 'status': 'status', 'date': 'date', 'time': 'time',
    'serial': 'serial number', 'model': 'model', 'mfr': 'manufacturer',
    'firmware': 'firmware version', 'type': 'type', 'id': 'identifier',
    'count': 'count', 'protection': 'protection', 'alarm': 'alarm',
    'test': 'test', 'efficiency': 'efficiency (%)', 'packs': 'battery packs',
    'desc': 'description', 'name': 'name', 'contacts': 'contact state',
    'phases': 'number of phases', 'switchable': 'whether it can be switched',
    'sensitivity': 'sensitivity', 'transfer': 'transfer point',
}
QUALIFIER_WORDS = {
    'nominal': 'nominal', 'low': 'low threshold', 'high': 'high threshold',
    'warning': 'warning threshold', 'critical': 'critical threshold',
    'minimum': 'minimum recorded', 'maximum': 'maximum recorded',
    'min': 'minimum recorded', 'max': 'maximum recorded',
    'start': 'on start', 'shutdown': 'on shutdown', 'reboot': 'on reboot',
    'restart': 'on restart', 'stop': 'on stop', 'return': 'on return',
    'aux': 'auxiliary', 'internal': 'internal', 'external': 'external',
    'total': 'total', 'approx': 'approximate',
}


def describe_variable(name):
    """Best-effort description for a variable with no explicit entry."""
    parts = name.split('.')
    subject = SUBJECT_WORDS.get(parts[0], parts[0])
    index, tail = None, []
    for piece in parts[1:]:
        if piece.isdigit() or (piece.upper().startswith('L')
                               and any(ch.isdigit() for ch in piece)):
            index = piece
            continue
        tail.append(piece)

    measure, qualifiers = None, []
    for piece in tail:
        if piece in MEASURE_WORDS and measure is None:
            measure = MEASURE_WORDS[piece]
        elif piece in QUALIFIER_WORDS:
            qualifiers.append(QUALIFIER_WORDS[piece])
        else:
            qualifiers.append(piece.replace('-', ' '))

    if measure == 'manufacturer' and 'date' in tail:
        measure = 'manufacturing date'
        qualifiers = [q for q in qualifiers if q != 'date']

    if measure is None and not qualifiers:
        return ''
    text = '%s %s' % (subject, measure or ' '.join(qualifiers))
    if measure and qualifiers:
        text += ', ' + ' '.join(qualifiers)
    if index:
        text += ' [%s]' % index
    return text[0].upper() + text[1:]


def variable_description(name):
    return DESCRIPTIONS.get(name) or describe_variable(name)


# Values a variable is known to accept when the UPS itself offers no ENUM.
# Only used as a suggestion: the field still accepts anything, because the next
# UPS may well use different wording.
SUGGESTED_VALUES = {
    'ups.start.battery': ['yes', 'no'],
    'battery.protection': ['yes', 'no'],
    'ups.beeper.status': ['enabled', 'disabled', 'muted'],
    'input.sensitivity': ['low', 'medium', 'high', 'auto'],
    'input.transfer.reason': [],
    'outlet.switchable': ['yes', 'no'],
    'battery.energysave': ['yes', 'no'],
}

# Variables whose value is a date, so the dashboard can offer a date picker
# instead of asking someone to guess the vendor's format.
DATE_VARIABLES = ('battery.date', 'battery.mfr.date', 'ups.mfr.date', 'device.date')


DANGEROUS_COMMANDS = {
    'load.off', 'load.off.delay', 'load.cycle',
    'shutdown.return', 'shutdown.stayoff', 'shutdown.reboot',
    'shutdown.reboot.graceful', 'bypass.start',
    'driver.killpower', 'test.failure.start', 'calibrate.start',
}

COMMAND_HELP = {
    'load.off': 'Switch the outlets OFF immediately - cuts power to everything',
    'load.on': 'Switch the outlets on immediately',
    'load.off.delay': 'Switch the outlets off after ups.delay.shutdown seconds',
    'load.on.delay': 'Switch the outlets on after ups.delay.start seconds',
    'load.cycle': 'Switch the outlets off and back on (power cycle the load)',
    'shutdown.return': 'Switch the load off, switch it back on when mains returns',
    'shutdown.stayoff': 'Switch the load off and keep it off',
    'shutdown.stop': 'Cancel a shutdown that is already counting down',
    'shutdown.reboot': 'Briefly drop the load while the UPS restarts',
    'shutdown.reboot.graceful': 'Same as shutdown.reboot, but after a delay',
    'test.battery.start': 'Start a battery self test of unspecified length',
    'test.battery.start.quick': 'Start a quick battery self test',
    'test.battery.start.deep': 'Start a deep battery test (discharges the battery)',
    'test.battery.stop': 'Abort a running battery test',
    'test.panel.start': 'Start a front panel / indicator test',
    'test.panel.stop': 'Stop the front panel test',
    'test.failure.start': 'Simulate a mains failure - the load runs on battery',
    'test.failure.stop': 'Stop simulating a mains failure',
    'test.system.start': 'Start a general system test',
    'calibrate.start': 'Start runtime calibration - fully discharges the battery',
    'calibrate.stop': 'Abort runtime calibration',
    'beeper.enable': 'Enable the audible alarm',
    'beeper.disable': 'Disable the audible alarm permanently',
    'beeper.mute': 'Silence the alarm until the next event',
    'beeper.toggle': 'Toggle the audible alarm on or off',
    'beeper.on': 'Enable the alarm (deprecated alias for beeper.enable)',
    'beeper.off': 'Disable the alarm (deprecated alias for beeper.disable)',
    'bypass.start': 'Switch to bypass - the load runs unprotected on raw mains',
    'bypass.stop': 'Leave bypass mode and protect the load again',
    'reset.input.minmax': 'Clear the recorded minimum and maximum input voltage',
    'reset.watchdog': 'Reset the watchdog timer so the UPS does not reboot the load',
    'driver.killpower': 'Tell the driver to run its shutdown sequence right now',
    'driver.reload': 'Reload the driver configuration',
}

OUTLET_COMMAND_HELP = {
    'load.off': 'Switch outlet {n} off immediately',
    'load.on': 'Switch outlet {n} on immediately',
    'load.off.delay': 'Switch outlet {n} off after its configured delay',
    'load.on.delay': 'Switch outlet {n} on after its configured delay',
    'load.cycle': 'Power cycle outlet {n}',
    'shutdown.return': 'Switch outlet {n} off, back on when mains returns',
    'shutdown.stayoff': 'Switch outlet {n} off and keep it off',
}
OUTLET_DANGEROUS = {'load.off', 'load.off.delay', 'load.cycle',
                    'shutdown.return', 'shutdown.stayoff'}

# Commands that start something worth following afterwards, and for how long.
FOLLOWABLE_COMMANDS = {
    'test.battery.start': 120,
    'test.battery.start.quick': 120,
    'test.battery.start.deep': 900,
    'test.system.start': 120,
    'test.panel.start': 60,
    'test.failure.start': 300,
    'calibrate.start': 3600,
}


def _split_outlet(cmd):
    parts = cmd.split('.')
    if len(parts) >= 3 and parts[0] == 'outlet':
        return parts[1], '.'.join(parts[2:])
    return None, None


def is_dangerous(cmd):
    if cmd in DANGEROUS_COMMANDS:
        return True
    outlet, action = _split_outlet(cmd)
    return outlet is not None and action in OUTLET_DANGEROUS


def command_help(cmd):
    if cmd in COMMAND_HELP:
        return COMMAND_HELP[cmd]
    outlet, action = _split_outlet(cmd)
    if outlet and action in OUTLET_COMMAND_HELP:
        return OUTLET_COMMAND_HELP[action].replace('{n}', outlet)
    return 'Vendor-specific command - see the manual for this UPS'


def _duration(seconds):
    if seconds < 60:
        return '%d seconds' % seconds
    if seconds % 60 == 0:
        return '%d minute%s' % (seconds // 60, '' if seconds == 60 else 's')
    return '%d:%02d' % (seconds // 60, seconds % 60)


def describe_fields(client, ups, writable):
    """Everything the dashboard needs to render a sensible input for each
    writable variable: the type NUT reports, the values it will accept, and a
    hint about how to present it."""
    fields = {}
    for name, value in writable.items():
        raw_type = client.get_type(ups, name)
        enum = client.list_enum(ups, name)
        ranges = [[low, high] for low, high in client.list_range(ups, name)]

        maximum_length = None
        match = re.search(r'STRING:(\d+)', raw_type or '')
        if match:
            maximum_length = int(match.group(1))

        if enum:
            kind = 'enum'
        elif ranges:
            kind = 'range'
        elif name in DATE_VARIABLES:
            kind = 'date'
        elif SUGGESTED_VALUES.get(name):
            kind = 'suggest'
        elif 'NUMBER' in (raw_type or '') or _looks_numeric(value):
            kind = 'number'
        else:
            kind = 'text'

        fields[name] = {
            'value': value,
            'kind': kind,
            'type': raw_type,
            'enum': enum,
            'ranges': ranges,
            'suggest': SUGGESTED_VALUES.get(name, []),
            'maxlength': maximum_length,
            'description': variable_description(name),
            'unit': _unit_for(name),
        }
    return fields


def _looks_numeric(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _unit_for(name):
    if name.endswith(('.charge', '.load', '.charge.low', '.charge.warning')):
        return '%'
    if '.voltage' in name:
        return 'V'
    if '.runtime' in name or '.delay' in name or '.timer' in name:
        return 's'
    if '.frequency' in name:
        return 'Hz'
    if '.current' in name:
        return 'A'
    if '.transfer' in name:
        return 'V'
    if '.temperature' in name:
        return 'deg C'
    return ''


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def status_flags(variables):
    return set((variables.get('ups.status') or '').split())


def describe_status(value):
    return ', '.join(STATUS_TEXT.get(f, f) for f in (value or '').split())


def parse_date(text):
    """Accept the date formats different UPS vendors use."""
    text = (text or '').strip()
    if not text or text.lower() in ('not set', 'unknown', 'n/a', '00/00/00'):
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%m/%d/%y', '%m/%d/%Y',
                '%d/%m/%Y', '%d.%m.%Y', '%d-%m-%Y', '%b %d %Y', '%Y-%m'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def assess(variables):
    """Overall verdict plus the reasons behind it, for the dashboard banner."""
    flags = status_flags(variables)
    issues = []
    level = 'ok'

    def raise_to(new):
        order = {'ok': 0, 'warn': 1, 'crit': 2}
        return new if order[new] > order[level] else level

    for flag in sorted(flags & CRITICAL_FLAGS):
        issues.append({'level': 'crit', 'text': STATUS_TEXT.get(flag, flag)})
        level = raise_to('crit')
    for flag in sorted(flags & WARNING_FLAGS):
        issues.append({'level': 'warn', 'text': STATUS_TEXT.get(flag, flag)})
        level = raise_to('warn')

    charge = to_float(variables.get('battery.charge'))
    if charge is not None:
        if charge <= CONFIG['charge_crit']:
            issues.append({'level': 'crit', 'text': 'battery at %d%%' % charge})
            level = raise_to('crit')
        elif charge <= CONFIG['charge_warn']:
            issues.append({'level': 'warn', 'text': 'battery at %d%%' % charge})
            level = raise_to('warn')

    load = to_float(variables.get('ups.load'))
    if load is not None:
        if load >= CONFIG['load_crit']:
            issues.append({'level': 'crit', 'text': 'load at %d%%' % load})
            level = raise_to('crit')
        elif load >= CONFIG['load_warn']:
            issues.append({'level': 'warn', 'text': 'load at %d%%' % load})
            level = raise_to('warn')

    runtime = to_float(variables.get('battery.runtime'))
    if runtime is not None and flags & DISCHARGE_FLAGS:
        if runtime <= CONFIG['runtime_crit_s']:
            issues.append({'level': 'crit',
                           'text': 'only %s of runtime left' % human_runtime(runtime)})
            level = raise_to('crit')
        elif runtime <= CONFIG['runtime_warn_s']:
            issues.append({'level': 'warn',
                           'text': 'runtime down to %s' % human_runtime(runtime)})
            level = raise_to('warn')

    for key in ('battery.date', 'battery.mfr.date'):
        when = parse_date(variables.get(key))
        if when:
            years = (datetime.now().date() - when).days / 365.25
            if years >= CONFIG['battery_life_years']:
                issues.append({'level': 'warn',
                               'text': 'battery is %.1f years old' % years})
                level = raise_to('warn')
            break

    return level, issues


def human_runtime(seconds):
    value = to_float(seconds)
    if value is None:
        return None
    total = int(value)
    hours, rest = divmod(max(total, 0), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '%dh %02dm' % (hours, minutes)
    if minutes:
        return '%dm %02ds' % (minutes, secs)
    return '%ds' % secs


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
# Numeric columns cover everything worth charting; the complete variable set is
# kept separately in `snapshots`, written only when it actually changes, so a
# year of history stays small while nothing is lost.
SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER NOT NULL,
    ups         TEXT    NOT NULL,
    status      TEXT,
    charge      REAL,
    runtime     REAL,
    load        REAL,
    input_v     REAL,
    output_v    REAL,
    battery_v   REAL,
    input_hz    REAL,
    temperature REAL,
    realpower   REAL,
    PRIMARY KEY (ts, ups)
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS samples_hourly (
    ts          INTEGER NOT NULL,
    ups         TEXT    NOT NULL,
    samples     INTEGER,
    charge      REAL,
    charge_min  REAL,
    runtime     REAL,
    runtime_min REAL,
    load        REAL,
    load_max    REAL,
    input_v     REAL,
    input_v_min REAL,
    input_v_max REAL,
    output_v    REAL,
    battery_v   REAL,
    battery_v_min REAL,
    input_hz    REAL,
    temperature REAL,
    realpower   REAL,
    on_battery_s INTEGER,
    PRIMARY KEY (ts, ups)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    ups     TEXT,
    kind    TEXT NOT NULL,
    level   TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS snapshots (
    ts      INTEGER PRIMARY KEY,
    ups     TEXT,
    vars    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  INTEGER NOT NULL,
    ended    INTEGER,
    ups      TEXT,
    charge_start REAL,
    charge_end   REAL,
    runtime_start REAL,
    min_charge   REAL,
    min_battery_v REAL
);

CREATE TABLE IF NOT EXISTS tests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started       INTEGER NOT NULL,
    finished      INTEGER,
    ups           TEXT,
    command       TEXT,
    source        TEXT,      -- 'dashboard' when we started it, 'ups' otherwise
    result        TEXT,
    passed        INTEGER,   -- 1, 0, or NULL while it is still running
    duration_s    INTEGER,
    on_battery_s  INTEGER,
    charge_start  REAL,
    charge_end    REAL,
    voltage_start REAL,
    voltage_min   REAL
);
CREATE INDEX IF NOT EXISTS tests_started ON tests (started);

CREATE TABLE IF NOT EXISTS plug_samples (
    ts          INTEGER PRIMARY KEY,
    output      INTEGER,       -- 1 on, 0 off
    power       REAL,          -- W
    voltage     REAL,
    current     REAL,          -- A
    pf          REAL,
    freq        REAL,
    energy      REAL,          -- Wh, cumulative since the counters were reset
    energy_ret  REAL,
    temperature REAL,          -- the plug's own, deg C
    on_time     INTEGER,       -- seconds switched on, cumulative
    switch_on   INTEGER,       -- times switched on, cumulative
    on_above_thr INTEGER,      -- seconds above the plug's load threshold
    rssi        REAL,          -- Wi-Fi signal, dBm
    uptime      INTEGER,
    ram_free    INTEGER
);

CREATE TABLE IF NOT EXISTS plug_hourly (
    ts          INTEGER PRIMARY KEY,
    samples     INTEGER,
    power       REAL,
    power_max   REAL,
    voltage     REAL,
    current     REAL,
    freq        REAL,
    temperature REAL,
    energy      REAL,          -- the reading at the end of the hour
    energy_used REAL,          -- and how much was used during it
    on_seconds  INTEGER
);

CREATE TABLE IF NOT EXISTS sensor_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    source      TEXT NOT NULL,   -- which device pushed it
    temperature REAL,
    humidity    REAL,
    battery_v   REAL,
    battery_pct REAL,
    rssi        REAL,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS sensor_ts ON sensor_samples (ts);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""

SAMPLE_COLUMNS = ['status', 'charge', 'runtime', 'load', 'input_v', 'output_v',
                  'battery_v', 'input_hz', 'temperature', 'realpower']

VAR_FOR_COLUMN = {
    'charge': 'battery.charge',
    'runtime': 'battery.runtime',
    'load': 'ups.load',
    'input_v': 'input.voltage',
    'output_v': 'output.voltage',
    'battery_v': 'battery.voltage',
    'input_hz': 'input.frequency',
    'temperature': 'ups.temperature',
    'realpower': 'ups.realpower',
}


def thin(rows, points):
    """Reduce a series to at most `points` rows, keeping the first and last."""
    if not points or len(rows) <= points:
        return rows
    step = len(rows) / float(points)
    picked, i = [], 0.0
    while int(i) < len(rows):
        picked.append(rows[int(i)])
        i += step
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return picked


class StorageError(Exception):
    pass


class Storage(object):
    """SQLite history. One connection per thread, guarded by a lock.

    Opened read-only for the commands that only report, so running
    `upsmon --history` as root can never leave root-owned files behind for the
    daemon to trip over afterwards.
    """

    def __init__(self, path=None, readonly=False):
        self.path = Path(path or DB_FILE)
        self.readonly = readonly
        self.lock = threading.Lock()
        self._local = threading.local()

        if readonly:
            if not self.path.exists():
                raise StorageError('no database yet at %s - nothing has been '
                                   'recorded. Is upsmon.service running?'
                                   % self.path)
            return

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageError('cannot create %s: %s' % (self.path.parent, e))
        try:
            with self.connection() as conn:
                conn.executescript(SCHEMA)
                # WAL keeps the dashboard's reads from blocking the writer.
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=NORMAL')
            self.migrate()
        except sqlite3.OperationalError as e:
            raise StorageError(self._explain(e))

    # Columns added to tables that already existed in an earlier version.
    # CREATE TABLE IF NOT EXISTS leaves an existing table exactly as it was, so
    # a new column has to be added deliberately or every insert fails.
    MIGRATIONS = {
        'plug_samples': [('on_above_thr', 'INTEGER'), ('rssi', 'REAL'),
                         ('uptime', 'INTEGER'), ('ram_free', 'INTEGER')],
        'plug_hourly': [('on_above_thr', 'INTEGER'), ('rssi', 'REAL')],
        'sensor_samples': [('rssi', 'REAL'), ('raw', 'TEXT')],
        'samples': [('input_hz', 'REAL'), ('temperature', 'REAL'),
                    ('realpower', 'REAL')],
        'tests': [('on_battery_s', 'INTEGER'), ('voltage_min', 'REAL')],
    }

    def migrate(self):
        """Bring a database made by an earlier version up to date."""
        added = []
        with self.lock:
            conn = self.connection()
            for table, columns in self.MIGRATIONS.items():
                try:
                    existing = {row['name'] for row in
                                conn.execute('PRAGMA table_info(%s)' % table)}
                except sqlite3.OperationalError:
                    continue                # the table itself is new; skip
                if not existing:
                    continue
                for name, kind in columns:
                    if name in existing:
                        continue
                    try:
                        conn.execute('ALTER TABLE %s ADD COLUMN %s %s'
                                     % (table, name, kind))
                        added.append('%s.%s' % (table, name))
                    except sqlite3.OperationalError:
                        pass                # already there under a race
            conn.commit()
        if added:
            log('database updated: added %s' % ', '.join(added))
        return added

    def _explain(self, error):
        """SQLite's wording hides the actual problem, which is always ownership."""
        import pwd
        who = pwd.getpwuid(os.getuid()).pw_name
        detail = ['%s (running as %s)' % (error, who)]
        directory = self.path.parent
        if not os.access(directory, os.W_OK):
            detail.append('%s is not writable by %s.' % (directory, who))
        elif self.path.exists() and not os.access(self.path, os.W_OK):
            detail.append('%s exists but is not writable by %s.' % (self.path, who))
        try:
            stat = self.path.stat() if self.path.exists() else directory.stat()
            target = self.path if self.path.exists() else directory
            import grp
            detail.append('%s is owned by %s:%s, mode %o.'
                          % (target, pwd.getpwuid(stat.st_uid).pw_name,
                             grp.getgrgid(stat.st_gid).gr_name, stat.st_mode & 0o777))
        except (OSError, KeyError):
            pass
        detail.append('Fix with:  sudo chown -R upsmon:upsmon %s' % directory)
        return ' '.join(detail)

    def connection(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            if self.readonly:
                conn = sqlite3.connect('file:%s?mode=ro' % self.path,
                                       uri=True, timeout=15.0)
            else:
                conn = sqlite3.connect(str(self.path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # -- writing ------------------------------------------------------------
    def add_sample(self, ups, variables, ts=None):
        ts = int(ts or time.time())
        row = {'ts': ts, 'ups': ups, 'status': variables.get('ups.status')}
        for column, var in VAR_FOR_COLUMN.items():
            row[column] = to_float(variables.get(var))
        columns = ['ts', 'ups'] + SAMPLE_COLUMNS
        sql = ('INSERT OR REPLACE INTO samples (%s) VALUES (%s)'
               % (', '.join(columns), ', '.join('?' * len(columns))))
        with self.lock:
            conn = self.connection()
            conn.execute(sql, [row[c] for c in columns])
            conn.commit()
        return ts

    PLUG_COLUMNS = ['output', 'power', 'voltage', 'current', 'pf', 'freq',
                    'energy', 'energy_ret', 'temperature', 'on_time', 'switch_on',
                    'on_above_thr', 'rssi', 'uptime', 'ram_free']

    def add_plug_sample(self, values, ts=None):
        ts = int(ts or time.time())
        columns = ['ts'] + self.PLUG_COLUMNS
        row = [ts] + [values.get(c) for c in self.PLUG_COLUMNS]
        with self.lock:
            conn = self.connection()
            conn.execute('INSERT OR REPLACE INTO plug_samples (%s) VALUES (%s)'
                         % (', '.join(columns), ', '.join('?' * len(columns))), row)
            conn.commit()
        return ts

    def add_sensor_sample(self, source, values, raw=None, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            conn.execute('INSERT INTO sensor_samples '
                         '(ts, source, temperature, humidity, battery_v, '
                         'battery_pct, rssi, raw) VALUES (?,?,?,?,?,?,?,?)',
                         (ts, source, values.get('temperature'), values.get('humidity'),
                          values.get('battery_v'), values.get('battery_pct'),
                          values.get('rssi'), raw))
            conn.commit()
        return ts

    def counter_restarted_at(self, column):
        """When a counter last went backwards, which is what a reset looks like.

        These totals only ever climb, so a fall between two samples is a reset.
        Returns (timestamp, exact) - exact is False when no fall was found and
        the answer is simply the oldest reading we hold, meaning the counter has
        been running at least that long.
        """
        with self.lock:
            rows = self.connection().execute(
                'SELECT ts, %s AS value FROM plug_samples WHERE %s IS NOT NULL '
                'ORDER BY ts' % (column, column)).fetchall()
        if not rows:
            return None, False
        previous = None
        found = None
        for row in rows:
            if previous is not None and row['value'] < previous:
                found = row['ts']
            previous = row['value']
        return (found, True) if found else (rows[0]['ts'], False)

    def latest_plug_sample(self):
        with self.lock:
            row = self.connection().execute(
                'SELECT * FROM plug_samples ORDER BY ts DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def plug_series(self, since, until=None, points=600):
        until = int(until or time.time())
        cutoff = int(time.time()) - CONFIG['retain_full_days'] * 86400
        rows = []
        with self.lock:
            conn = self.connection()
            if since < cutoff:
                for record in conn.execute(
                        'SELECT ts, power, power_max, voltage, current, freq, '
                        'temperature, energy, energy_used, on_seconds, samples '
                        'FROM plug_hourly WHERE ts >= ? AND ts < ? ORDER BY ts',
                        (int(since), min(cutoff, until))):
                    row = dict(record)
                    row['output'] = 1 if (row.get('on_seconds') or 0) > 0 else 0
                    row['hourly'] = 1
                    rows.append(row)
            sql = ('SELECT ts, output, power, voltage, current, pf, freq, energy, '
                   'energy_ret, temperature, on_time, switch_on, on_above_thr, '
                   'rssi, uptime, ram_free FROM plug_samples '
                   'WHERE ts >= ? AND ts <= ? ORDER BY ts')
            rows.extend(dict(r) for r in conn.execute(
                sql, (max(int(since), cutoff if since < cutoff else int(since)), until)))
        return thin(rows, points)

    def sensor_series(self, since, until=None, points=600, source=None):
        until = int(until or time.time())
        sql = ('SELECT ts, source, temperature, humidity, battery_v, battery_pct, '
               'rssi FROM sensor_samples WHERE ts >= ? AND ts <= ?')
        args = [int(since), until]
        if source:
            sql += ' AND source = ?'
            args.append(source)
        with self.lock:
            rows = [dict(r) for r in self.connection().execute(sql + ' ORDER BY ts', args)]
        return thin(rows, points)

    def sensor_sources(self):
        with self.lock:
            return [dict(r) for r in self.connection().execute(
                'SELECT source, COUNT(*) samples, MAX(ts) last FROM sensor_samples '
                'GROUP BY source ORDER BY last DESC')]

    def add_snapshot(self, ups, variables, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            conn.execute('INSERT OR REPLACE INTO snapshots (ts, ups, vars) VALUES (?,?,?)',
                         (ts, ups, json.dumps(variables, sort_keys=True)))
            conn.commit()

    def add_event(self, kind, detail, level='info', ups=None, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            conn.execute('INSERT INTO events (ts, ups, kind, level, detail) '
                         'VALUES (?,?,?,?,?)', (ts, ups, kind, level, detail))
            conn.commit()
        log('event: %s - %s' % (kind, detail),
            level='warn' if level in ('warn', 'crit') else 'info')
        return ts

    def start_outage(self, ups, variables, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            cur = conn.execute(
                'INSERT INTO outages (started, ups, charge_start, runtime_start, '
                'min_charge, min_battery_v) VALUES (?,?,?,?,?,?)',
                (ts, ups, to_float(variables.get('battery.charge')),
                 to_float(variables.get('battery.runtime')),
                 to_float(variables.get('battery.charge')),
                 to_float(variables.get('battery.voltage'))))
            conn.commit()
            return cur.lastrowid

    def update_outage(self, outage_id, variables):
        charge = to_float(variables.get('battery.charge'))
        voltage = to_float(variables.get('battery.voltage'))
        with self.lock:
            conn = self.connection()
            conn.execute(
                'UPDATE outages SET '
                ' min_charge = CASE WHEN ? IS NOT NULL AND '
                '   (min_charge IS NULL OR ? < min_charge) THEN ? ELSE min_charge END,'
                ' min_battery_v = CASE WHEN ? IS NOT NULL AND '
                '   (min_battery_v IS NULL OR ? < min_battery_v) THEN ? ELSE min_battery_v END '
                'WHERE id = ?',
                (charge, charge, charge, voltage, voltage, voltage, outage_id))
            conn.commit()

    def end_outage(self, outage_id, variables, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            conn.execute('UPDATE outages SET ended = ?, charge_end = ? WHERE id = ?',
                         (ts, to_float(variables.get('battery.charge')), outage_id))
            conn.commit()

    def start_test(self, ups, command, source, variables, ts=None):
        ts = int(ts or time.time())
        with self.lock:
            conn = self.connection()
            cursor = conn.execute(
                'INSERT INTO tests (started, ups, command, source, charge_start, '
                'voltage_start) VALUES (?,?,?,?,?,?)',
                (ts, ups, command, source,
                 to_float(variables.get('battery.charge')),
                 to_float(variables.get('battery.voltage'))))
            conn.commit()
            return cursor.lastrowid

    def finish_test(self, test_id, result, variables, on_battery_s=None,
                    voltage_min=None, ts=None):
        ts = int(ts or time.time())
        passed = None
        if result:
            lowered = result.lower()
            if 'pass' in lowered or lowered.startswith('done and warning'):
                passed = 1 if 'pass' in lowered else 0
            elif 'fail' in lowered or 'error' in lowered or 'bad' in lowered:
                passed = 0
            elif 'done' in lowered:
                passed = 1
        with self.lock:
            conn = self.connection()
            conn.execute(
                'UPDATE tests SET finished = ?, result = ?, passed = ?, '
                'duration_s = ? - started, on_battery_s = ?, charge_end = ?, '
                'voltage_min = ? WHERE id = ?',
                (ts, result, passed, ts, on_battery_s,
                 to_float(variables.get('battery.charge')), voltage_min, test_id))
            conn.commit()

    def record_test(self, ups, command, source, result, variables, ts=None):
        """A test we did not start, noticed only by its result changing."""
        test_id = self.start_test(ups, command, source, variables, ts)
        self.finish_test(test_id, result, variables, ts=ts)
        return test_id

    def tests(self, limit=50):
        with self.lock:
            return [dict(r) for r in self.connection().execute(
                'SELECT * FROM tests ORDER BY started DESC LIMIT ?', (int(limit),))]

    def open_outage(self):
        with self.lock:
            row = self.connection().execute(
                'SELECT * FROM outages WHERE ended IS NULL ORDER BY started DESC '
                'LIMIT 1').fetchone()
        return dict(row) if row else None

    def set_meta(self, key, value):
        with self.lock:
            conn = self.connection()
            conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)',
                         (key, str(value)))
            conn.commit()

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.connection().execute(
                'SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    # -- reading ------------------------------------------------------------
    def series(self, since, until=None, points=600, ups=None):
        """Samples between two timestamps, thinned to at most `points` rows.

        Recent data comes from `samples`, anything older than the full-detail
        window from the hourly roll-up, and the two are concatenated - so a
        year-long chart costs a few hundred rows rather than a million.
        """
        until = int(until or time.time())
        since = int(since)
        cutoff = int(time.time()) - CONFIG['retain_full_days'] * 86400
        rows = []
        with self.lock:
            conn = self.connection()
            if since < cutoff:
                sql = ('SELECT ts, charge, runtime, load, input_v, output_v, '
                       'battery_v, input_hz, temperature, realpower, '
                       'charge_min, runtime_min, load_max, input_v_min, input_v_max, '
                       'on_battery_s, samples FROM samples_hourly '
                       'WHERE ts >= ? AND ts < ?')
                args = [since, min(cutoff, until)]
                if ups:
                    sql += ' AND ups = ?'
                    args.append(ups)
                for record in conn.execute(sql + ' ORDER BY ts', args):
                    row = dict(record)
                    # The hourly table keeps seconds-on-battery rather than the
                    # flags. Give the charts the same 'status' key they shade on
                    # for recent data, so an old outage is still visible.
                    row['status'] = 'OB' if (row.get('on_battery_s') or 0) > 0 else 'OL'
                    row['hourly'] = 1
                    rows.append(row)
            sql = ('SELECT ts, status, charge, runtime, load, input_v, output_v, '
                   'battery_v, input_hz, temperature, realpower FROM samples '
                   'WHERE ts >= ? AND ts <= ?')
            args = [max(since, cutoff if since < cutoff else since), until]
            if ups:
                sql += ' AND ups = ?'
                args.append(ups)
            rows.extend(dict(r) for r in conn.execute(sql + ' ORDER BY ts', args))

        return thin(rows, points)

    def events(self, limit=200, since=None, ups=None):
        sql = 'SELECT id, ts, ups, kind, level, detail FROM events'
        args, where = [], []
        if since:
            where.append('ts >= ?')
            args.append(int(since))
        if ups:
            where.append('ups = ?')
            args.append(ups)
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY ts DESC LIMIT ?'
        args.append(int(limit))
        with self.lock:
            return [dict(r) for r in self.connection().execute(sql, args)]

    def outages(self, limit=50):
        with self.lock:
            return [dict(r) for r in self.connection().execute(
                'SELECT * FROM outages ORDER BY started DESC LIMIT ?', (int(limit),))]

    def latest_snapshot(self):
        with self.lock:
            row = self.connection().execute(
                'SELECT ts, ups, vars FROM snapshots ORDER BY ts DESC LIMIT 1').fetchone()
        if not row:
            return None
        try:
            return {'ts': row['ts'], 'ups': row['ups'], 'vars': json.loads(row['vars'])}
        except ValueError:
            return None

    def stats(self):
        with self.lock:
            conn = self.connection()
            out = {}
            for name in ('samples', 'samples_hourly', 'events', 'snapshots',
                         'outages', 'tests', 'plug_samples', 'plug_hourly',
                         'sensor_samples'):
                out[name] = conn.execute('SELECT COUNT(*) c FROM %s' % name).fetchone()['c']
            row = conn.execute('SELECT MIN(ts) a, MAX(ts) b FROM samples').fetchone()
            out['first_sample'] = row['a']
            out['last_sample'] = row['b']
            row = conn.execute('SELECT MIN(ts) a FROM samples_hourly').fetchone()
            out['first_hourly'] = row['a']
        try:
            out['file_bytes'] = self.path.stat().st_size
            out['wal_bytes'] = 0
            for suffix in ('-wal', '-shm'):
                extra = Path(str(self.path) + suffix)
                if extra.exists():
                    out['wal_bytes'] += extra.stat().st_size
            out['db_bytes'] = out['file_bytes'] + out['wal_bytes']
        except OSError:
            out['db_bytes'] = out['file_bytes'] = out['wal_bytes'] = None
        return out

    # -- housekeeping -------------------------------------------------------
    RESET_SCOPES = {
        'all': ['samples', 'samples_hourly', 'snapshots', 'events', 'outages',
                'tests', 'plug_samples', 'plug_hourly', 'sensor_samples'],
        'history': ['samples', 'samples_hourly', 'snapshots'],
        'plug': ['plug_samples', 'plug_hourly'],
        'sensor': ['sensor_samples'],
        'events': ['events'],
        'outages': ['outages'],
        'tests': ['tests'],
    }

    def counts(self, tables=None):
        tables = tables or self.RESET_SCOPES['all']
        with self.lock:
            conn = self.connection()
            return {t: conn.execute('SELECT COUNT(*) c FROM %s' % t).fetchone()['c']
                    for t in tables}

    def reset(self, scope='all'):
        """Delete recorded data. Returns how many rows went, per table."""
        tables = self.RESET_SCOPES[scope]
        removed = self.counts(tables)
        with self.lock:
            conn = self.connection()
            for table in tables:
                conn.execute('DELETE FROM %s' % table)
            conn.commit()
            # DELETE alone leaves the file its old size; reclaim it, so the
            # point of the reset is visible in --diag rather than only in a
            # row count.
            conn.execute('VACUUM')
        return removed

    def aggregate(self):
        """Roll full-resolution samples older than the detail window into hours."""
        cutoff = int(time.time()) - CONFIG['retain_full_days'] * 86400
        with self.lock:
            conn = self.connection()
            done = conn.execute(
                'SELECT COALESCE(MAX(ts), 0) m FROM samples_hourly').fetchone()['m']
            rows = conn.execute("""
                SELECT (ts / 3600) * 3600 AS hour, ups,
                       COUNT(*) n,
                       AVG(charge) charge, MIN(charge) charge_min,
                       AVG(runtime) runtime, MIN(runtime) runtime_min,
                       AVG(load) load, MAX(load) load_max,
                       AVG(input_v) input_v, MIN(input_v) input_v_min,
                       MAX(input_v) input_v_max,
                       AVG(output_v) output_v,
                       AVG(battery_v) battery_v, MIN(battery_v) battery_v_min,
                       AVG(input_hz) input_hz, AVG(temperature) temperature,
                       AVG(realpower) realpower,
                       SUM(CASE WHEN status LIKE '%OB%' OR status LIKE '%DISCHRG%'
                                THEN 1 ELSE 0 END) on_batt
                FROM samples
                WHERE ts < ? AND ts > ?
                GROUP BY hour, ups
            """, (cutoff, done)).fetchall()

            interval = max(1, CONFIG['sample_interval'])
            for r in rows:
                conn.execute("""
                    INSERT OR REPLACE INTO samples_hourly
                    (ts, ups, samples, charge, charge_min, runtime, runtime_min,
                     load, load_max, input_v, input_v_min, input_v_max, output_v,
                     battery_v, battery_v_min, input_hz, temperature, realpower,
                     on_battery_s)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (r['hour'], r['ups'], r['n'], r['charge'], r['charge_min'],
                      r['runtime'], r['runtime_min'], r['load'], r['load_max'],
                      r['input_v'], r['input_v_min'], r['input_v_max'],
                      r['output_v'], r['battery_v'], r['battery_v_min'],
                      r['input_hz'], r['temperature'], r['realpower'],
                      int(r['on_batt']) * interval))
            # the plug's own history, rolled up the same way
            plug_done = conn.execute(
                'SELECT COALESCE(MAX(ts), 0) m FROM plug_hourly').fetchone()['m']
            plug_rows = conn.execute("""
                SELECT (ts / 3600) * 3600 AS hour, COUNT(*) n,
                       AVG(power) power, MAX(power) power_max,
                       AVG(voltage) voltage, AVG(current) current,
                       AVG(freq) freq, AVG(temperature) temperature,
                       MAX(energy) energy, MAX(energy) - MIN(energy) energy_used,
                       SUM(CASE WHEN output = 1 THEN 1 ELSE 0 END) on_count
                FROM plug_samples WHERE ts < ? AND ts > ? GROUP BY hour
            """, (cutoff, plug_done)).fetchall()
            plug_interval = max(1, CONFIG['plug_sample_interval'])
            for r in plug_rows:
                conn.execute("""
                    INSERT OR REPLACE INTO plug_hourly (ts, samples, power, power_max,
                        voltage, current, freq, temperature, energy, energy_used,
                        on_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (r['hour'], r['n'], r['power'], r['power_max'], r['voltage'],
                      r['current'], r['freq'], r['temperature'], r['energy'],
                      r['energy_used'], int(r['on_count']) * plug_interval))
            conn.execute('DELETE FROM plug_samples WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM plug_hourly WHERE ts < ?',
                         (int(time.time()) - CONFIG['retain_hourly_days'] * 86400,))
            conn.execute('DELETE FROM sensor_samples WHERE ts < ?',
                         (int(time.time()) - CONFIG['sensor_retain_days'] * 86400,))

            conn.execute('DELETE FROM samples WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM samples_hourly WHERE ts < ?',
                         (int(time.time()) - CONFIG['retain_hourly_days'] * 86400,))
            conn.execute('DELETE FROM events WHERE ts < ?',
                         (int(time.time()) - CONFIG['retain_events_days'] * 86400,))
            conn.execute('DELETE FROM snapshots WHERE ts < ? AND ts NOT IN '
                         '(SELECT MAX(ts) FROM snapshots)',
                         (int(time.time()) - CONFIG['retain_hourly_days'] * 86400,))
            conn.commit()
            try:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                pass                # a reader holds it open; next hour will do
            return len(rows)


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------
class Daemon(object):
    @staticmethod
    def _empty_capabilities():
        return {'writable': {}, 'fields': {}, 'commands': [], 'fetched': 0}

    def __init__(self, storage=None):
        open_log()
        self.store = storage or Storage()
        self.lock = threading.Lock()
        self.snapshot = {}
        self.token = self._ensure_token()
        self.started = time.time()
        self.ups_name = CONFIG['ups_name'] or '?'
        self.server_version = '?'
        self.last_ok = None
        self.last_error = None
        self.consecutive_errors = 0
        self.last_sample_at = 0.0
        self.last_aggregate_at = 0.0
        self.last_vars_signature = None
        self.previous_flags = None     # last confirmed set, what events compare against
        self.candidate_flags = None    # seen once, waiting to be seen again
        self.candidate_seen = 0
        self.suppressed_reads = 0    # single-poll flag glitches ignored
        self.previous_test_result = None
        self.outage_id = None
        self.capabilities = self._empty_capabilities()
        self.recent_writes = {}      # var -> when we wrote it, to avoid double logging
        self.active_test_id = None   # set while we are following a test we began
        self.plug = None             # snapshot of the smart plug, if configured
        self.plug_error = None
        self.plug_info = None
        self.last_plug_poll = 0.0
        self.last_plug_sample = 0.0
        self.plug_output = None      # last confirmed relay state, for events
        self.last_device_status = 0.0
        self.device_status_cache = {}
        self.plug_backoff_until = 0.0
        self.plug_failures = 0
        self.last_sensor_poll = 0.0
        self.sensors = {}            # newest reading per pushing device
        self.pin_failures = 0
        self.pin_locked_until = 0.0
        self._control_lock = threading.Lock()

    # -- API token ----------------------------------------------------------
    def _ensure_token(self):
        """A shared secret for the privileged endpoints.

        Kept in /run so it is regenerated on every boot, and readable only by
        root and the web server's group - the same arrangement the Plex monitor
        uses, for the same reason: the browser never sees it, and nothing else
        on the machine can drive the UPS.
        """
        token = None
        try:
            token = TOKEN_FILE.read_text().strip() or None
        except OSError:
            pass
        token = token or secrets.token_urlsafe(32)
        path = token_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token)
            path.chmod(0o640)
            # Ownership is left exactly as it falls: the file belongs to
            # whoever runs the daemon, and the web server reads it by being a
            # member of that group (see INSTALL.md). Chowning it to nginx only
            # worked when started as root, which meant the same daemon produced
            # different ownership depending on who launched it.
            import grp
            import pwd
            info = path.stat()
            log('API token at %s (owner %s, group %s, mode %o)'
                % (path, pwd.getpwuid(info.st_uid).pw_name,
                   grp.getgrgid(info.st_gid).gr_name, info.st_mode & 0o777))
        except OSError as e:
            log('could not write the token file: %s' % e, level='warn')
        return token

    # -- collection ---------------------------------------------------------
    def poll(self):
        """One pass: read everything, publish it, record what deserves keeping."""
        now = time.time()
        try:
            client, name = connect_ups()
        except NUTError as e:
            self._record_failure(str(e))
            return None
        try:
            self.ups_name = name
            self.server_version = client.server_version()
            variables = client.list_vars(name)
            if now - self.capabilities['fetched'] > 300:
                writable = client.list_rw(name)
                self.capabilities = {
                    'writable': writable,
                    'fields': describe_fields(client, name, writable),
                    'commands': sorted(client.list_cmds(name)),
                    'fetched': now,
                }
            else:
                # Refresh only the values, from the reading we already have.
                # Costs nothing and keeps the settings table honest. The types,
                # ranges and enums in 'fields' genuinely never change, so only
                # the value inside each descriptor is updated.
                self.capabilities['writable'] = {
                    key: variables.get(key, old)
                    for key, old in self.capabilities['writable'].items()
                }
                for key, field in self.capabilities.get('fields', {}).items():
                    if key in variables:
                        field['value'] = variables[key]
        except NUTError as e:
            self._record_failure(str(e))
            return None
        finally:
            client.close()

        if self.consecutive_errors:
            self.store.add_event('connection', 'contact with upsd restored', 'info',
                                 self.ups_name)
        self.consecutive_errors = 0
        self.last_error = None
        self.last_ok = now

        self._detect_events(variables, now)
        self._maybe_record(variables, now)
        self._publish(variables, now)
        return variables

    def _record_failure(self, message):
        self.consecutive_errors += 1
        self.last_error = message
        # One event per outage of the connection, not one per failed poll.
        if self.consecutive_errors == 1:
            self.store.add_event('connection', 'lost contact with upsd: %s' % message,
                                 'warn', self.ups_name)
        log('poll failed: %s' % message, level='warn')
        with self.lock:
            if self.snapshot:
                self.snapshot = dict(self.snapshot)
                self.snapshot['online'] = False
                self.snapshot['error'] = message

    def _detect_events(self, variables, now):
        """Turn changes in the UPS's own reporting into log entries."""
        self._detect_status(variables, now)
        self._detect_test_result(variables, now)

    def _confirmed_flags(self, variables):
        """The status, once it has held still long enough to be believed.

        A genuine event lasts minutes; a bad USB read lasts a single poll.
        Returns None while a reading is still unconfirmed, so callers know to
        leave everything as it was.
        """
        raw = status_flags(variables)
        needed = max(1, int(CONFIG['flag_confirm_polls']))

        if raw == self.candidate_flags:
            self.candidate_seen += 1
        else:
            # The reading we were watching is being replaced. If it never held
            # long enough to be believed, and it differed from the state we do
            # believe, it was a bad read - count it and move on.
            if (self.candidate_flags is not None
                    and self.candidate_seen < needed
                    and self.previous_flags is not None
                    and self.candidate_flags != self.previous_flags):
                self.suppressed_reads += 1
                log('ignored a one-poll status reading: %s'
                    % (' '.join(sorted(self.candidate_flags)) or '(none)'),
                    level='debug')
            self.candidate_flags = raw
            self.candidate_seen = 1

        if self.candidate_seen < needed:
            if self.previous_flags is not None and raw != self.previous_flags:
                log('status %s seen once, waiting for confirmation'
                    % (' '.join(sorted(raw)) or '(none)'), level='debug')
            return None
        return raw

    def _detect_status(self, variables, now):
        confirmed = self._confirmed_flags(variables)
        if confirmed is None:
            return

        if self.previous_flags is None:
            self.previous_flags = confirmed      # nothing to compare against yet
            return

        if confirmed == self.previous_flags:
            # A reading that differed but never held: a bad read. Count it so
            # --diag can show how often the driver produces them.
            # An outage in progress still needs its running minimum updated.
            if (confirmed & OUTAGE_FLAGS) and self.outage_id:
                self.store.update_outage(self.outage_id, variables)
            return

        flags, previous = confirmed, self.previous_flags
        self.previous_flags = flags

        for flag in sorted((flags - previous) - NOISY_FLAGS):
            level = ('crit' if flag in CRITICAL_FLAGS
                     else 'warn' if flag in WARNING_FLAGS else 'info')
            self.store.add_event('status', '%s - %s' % (flag, STATUS_TEXT.get(flag, flag)),
                                 level, self.ups_name, now)
        for flag in sorted((previous - flags) - NOISY_FLAGS):
            self.store.add_event('status', 'cleared %s' % flag, 'info',
                                 self.ups_name, now)

        on_battery = bool(flags & OUTAGE_FLAGS)
        was_on_battery = bool(previous & OUTAGE_FLAGS)
        if on_battery and not was_on_battery:
            self.outage_id = self.store.start_outage(self.ups_name, variables, now)
        elif was_on_battery and not on_battery and self.outage_id:
            self.store.end_outage(self.outage_id, variables, now)
            self.outage_id = None

    def _detect_test_result(self, variables, now):
        """Self-test verdicts. Runs on every poll — a test finishes while the
        status is sitting perfectly still, so this must not depend on it."""
        result = variables.get('ups.test.result')
        if not result or result == self.previous_test_result:
            return
        # "In progress" is a stage, not a verdict; logging it as a warning makes
        # a perfectly normal test look like a fault.
        running = any(word in result.lower()
                      for word in ('progress', 'pending', 'scheduled'))
        if self.previous_test_result is not None and not running:
            level = 'info' if 'pass' in result.lower() else 'warn'
            self.store.add_event('test', 'self test result: %s' % result,
                                 level, self.ups_name, now)
            # A verdict arriving while we follow our own test belongs to that
            # test; anything else the UPS ran by itself and deserves its own row.
            if self.active_test_id is None:
                self.store.record_test(self.ups_name, 'ups.self-test', 'ups',
                                       result, variables, now)
        self.previous_test_result = result

    def _maybe_record(self, variables, now):
        on_battery = bool(status_flags(variables) & DISCHARGE_FLAGS)
        interval = (CONFIG['sample_interval_battery'] if on_battery
                    else CONFIG['sample_interval'])
        if now - self.last_sample_at >= interval:
            self.store.add_sample(self.ups_name, variables, now)
            self.last_sample_at = now

        # The full variable set is only worth storing when it changes, which is
        # rare - a new firmware string, a setting someone edited, a battery date.
        signature = json.dumps(variables, sort_keys=True)
        if signature != self.last_vars_signature:
            volatile = {'ups.status', 'battery.charge', 'battery.runtime', 'ups.load',
                        'input.voltage', 'output.voltage', 'battery.voltage',
                        'input.frequency', 'output.frequency', 'ups.realpower',
                        'ups.power', 'ups.temperature', 'battery.temperature',
                        'output.current', 'ups.timer.shutdown', 'ups.timer.start',
                        'ups.timer.reboot', 'ups.test.result', 'battery.charger.status',
                        'ups.efficiency', 'input.current', 'ups.alarm'}
            stable = {k: v for k, v in variables.items() if k not in volatile}
            previous = self.store.latest_snapshot()
            if previous:
                # Merge rather than replace: a variable missing from this poll
                # keeps its last known value instead of looking deleted.
                merged = dict(previous['vars'])
                merged.update(stable)
                stable = merged
            if not previous or previous['vars'] != stable:
                self.store.add_snapshot(self.ups_name, stable, now)
                if previous:
                    for key in sorted(set(stable) | set(previous['vars'])):
                        before = previous['vars'].get(key)
                        after = stable.get(key)
                        if before == after:
                            continue
                        # A key that merely appears or disappears is the driver
                        # refreshing its cache, not somebody changing a setting.
                        # usbhid-ups publishes some values only on its slower
                        # full poll, so they come and go on their own.
                        if before is None or after is None:
                            continue
                        # A change we made ourselves has already been logged by
                        # set_variable; only report changes made elsewhere.
                        if now - self.recent_writes.get(key, 0) < 120:
                            continue
                        self.store.add_event(
                            'setting', '%s changed outside upsmon: %s -> %s'
                            % (key, before if before is not None else '(absent)',
                               after if after is not None else '(removed)'),
                            'info', self.ups_name, now)
            self.last_vars_signature = signature

    def _publish(self, variables, now):
        level, issues = assess(variables)
        writable = self.capabilities['writable']
        commands = self.capabilities['commands']
        battery_age = None
        for key in ('battery.date', 'battery.mfr.date'):
            when = parse_date(variables.get(key))
            if when:
                battery_age = {
                    'source': key,
                    'raw': variables.get(key),
                    'installed': when.isoformat(),
                    'days': (datetime.now().date() - when).days,
                    'years': round((datetime.now().date() - when).days / 365.25, 2),
                    'life_years': CONFIG['battery_life_years'],
                }
                break

        snapshot = {
            'generated': int(now),
            'online': True,
            'version': __version__,
            'ups': self.ups_name,
            'nut_host': CONFIG['nut_host'],
            'server': self.server_version,
            'uptime': int(now - self.started),
            'level': level,
            'issues': issues,
            'status_text': describe_status(variables.get('ups.status')),
            'vars': variables,
            'descriptions': {k: variable_description(k) for k in variables},
            'writable': writable,
            'commands': [{'name': c, 'help': command_help(c),
                          'dangerous': is_dangerous(c),
                          'followable': c in FOLLOWABLE_COMMANDS}
                         for c in commands],
            'battery_age': battery_age,
            'thresholds': {k: CONFIG[k] for k in
                           ('charge_warn', 'charge_crit', 'load_warn', 'load_crit',
                            'runtime_warn_s', 'runtime_crit_s', 'battery_life_years')},
            'allow_dangerous': CONFIG['allow_dangerous_commands'],
            'pin_required': self.pin_required(),
            'outage': self.store.open_outage(),
            'plug': self.plug_snapshot(),
            'sensors': sorted(self.sensors.values(),
                              key=lambda s: s.get('source') or ''),
        }
        with self.lock:
            self.snapshot = snapshot

    # -- PIN ----------------------------------------------------------------
    def check_pin(self, supplied):
        """Returns (ok, message). No PIN configured means everything is allowed.

        Four digits is only 10 000 combinations, so the lockout is what makes
        this worth anything: five wrong tries and nothing is accepted for five
        minutes, which turns a brute force into weeks of work and fills the
        event log while it happens.
        """
        expected = str(CONFIG.get('pin') or '').strip()
        if not expected:
            return True, ''

        now = time.time()
        if now < self.pin_locked_until:
            remaining = int(self.pin_locked_until - now) + 1
            return False, ('too many wrong PINs - locked for another %s'
                           % _duration(remaining))

        if secrets.compare_digest(str(supplied or '').strip(), expected):
            if self.pin_failures:
                self.store.add_event('pin', 'correct PIN after %d failed attempt(s)'
                                     % self.pin_failures, 'info', self.ups_name)
            self.pin_failures = 0
            return True, ''

        self.pin_failures += 1
        allowed = max(1, int(CONFIG.get('pin_attempts') or 5))
        if self.pin_failures >= allowed:
            self.pin_locked_until = now + float(CONFIG.get('pin_lockout_s') or 300)
            self.pin_failures = 0
            self.store.add_event('pin', 'locked out after %d wrong PINs' % allowed,
                                 'warn', self.ups_name)
            return False, ('too many wrong PINs - locked for %s'
                           % _duration(int(CONFIG.get('pin_lockout_s') or 300)))
        self.store.add_event('pin', 'wrong PIN (%d of %d before lockout)'
                             % (self.pin_failures, allowed), 'warn', self.ups_name)
        return False, ('wrong PIN - %d attempt(s) left'
                       % (allowed - self.pin_failures))

    def pin_required(self):
        return bool(str(CONFIG.get('pin') or '').strip())

    def plug_snapshot(self):
        """What the dashboard needs about the plug, or why there is nothing."""
        if not CONFIG['plug_host']:
            return None
        if self.plug is None:
            # A restart empties the in-memory snapshot, and if the first poll
            # is refused the panel would sit blank for a minute with a database
            # full of readings behind it. Show the last one, plainly marked.
            return self.plug_from_history()
        snapshot = dict(self.plug)
        snapshot['error'] = self.plug_error
        # A missed poll should not blank a panel full of perfectly good
        # readings. Keep showing them, marked as stale, and say why.
        snapshot['online'] = True
        snapshot['stale'] = self.plug_error is not None
        snapshot['age'] = int(time.time() - snapshot.get('generated', time.time()))
        if self.plug_backoff_until > time.time():
            snapshot['retry_in'] = int(self.plug_backoff_until - time.time())
        return snapshot

    # -- the smart plug ------------------------------------------------------
    def poll_plug(self, force=False):
        """Read the plug and record it. Failure here never disturbs the UPS."""
        if not CONFIG['plug_host']:
            return None
        now = time.time()
        if not force and now - self.last_plug_poll < CONFIG['plug_poll_interval']:
            return self.plug
        if not force and now < self.plug_backoff_until:
            return self.plug
        self.last_plug_poll = now

        rpc = ShellyRPC()
        try:
            switch = rpc.switch_status()
            # The system, Wi-Fi and cloud sections change slowly and cost a
            # whole extra request each time. Once a minute is plenty, and a
            # Gen3 plug starts answering 429 if asked for everything at every
            # poll.
            if now - self.last_device_status >= 60 or not self.device_status_cache:
                self.device_status_cache = rpc.device_status()
                self.last_device_status = now
            device = self.device_status_cache
            if self.plug_info is None:
                self.plug_info = rpc.device_info()
                self.store.set_meta('plug_info', json.dumps(self.plug_info))
        except PlugError as e:
            self.plug_failures += 1
            # One missed poll is noise; three in a row is worth recording. The
            # log was filling with lost/restored pairs from a plug that was
            # only ever briefly busy.
            if self.plug_failures == 3:
                self.store.add_event('plug', 'lost contact with the plug: %s' % e,
                                     'warn')
            self.plug_error = str(e)
            first_ever = self.plug_info is None and self.plug is None
            if '429' in str(e) and not first_ever:
                # Being told to slow down is not a fault. Wait for as long as
                # the device asked, or a minute, rather than hammering it and
                # staying blocked.
                asked = getattr(rpc, 'retry_after', 0) or 0
                self.plug_backoff_until = now + max(asked or 0, 60)
                log('plug is rate limiting; backing off for %ds'
                    % int(self.plug_backoff_until - now),
                    level='warn' if self.plug_failures == 3 else 'debug')
            elif '429' in str(e):
                # Straight after a restart, try again on the next tick rather
                # than waiting a minute with nothing to show.
                self.plug_backoff_until = now + max(CONFIG['plug_poll_interval'], 5)
                log('plug is rate limiting on the first poll; trying again shortly',
                    level='debug')
            else:
                log('plug poll failed: %s' % e,
                    level='warn' if self.plug_failures <= 3 else 'debug')
            return None

        if self.plug_failures >= 3:
            self.store.add_event('plug', 'contact with the plug restored', 'info')
        self.plug_failures = 0
        self.last_sensor_poll = 0.0
        self.plug_error = None

        flat = flatten(switch)
        flat.update(flatten(device))
        values = {
            'output': 1 if switch.get('output') else 0,
            'power': to_float(switch.get('apower')),
            'voltage': to_float(switch.get('voltage')),
            'current': to_float(switch.get('current')),
            'pf': to_float(switch.get('pf')),
            'freq': to_float(switch.get('freq')),
            'energy': to_float((switch.get('aenergy') or {}).get('total')),
            'energy_ret': to_float((switch.get('ret_aenergy') or {}).get('total')),
            'temperature': to_float((switch.get('temperature') or {}).get('tC')),
            'on_time': to_float((switch.get('counts') or {}).get('on_time')),
            'switch_on': to_float((switch.get('counts') or {}).get('switch_on')),
            'on_above_thr': to_float((switch.get('counts') or {}).get('on_above_thr')),
            'rssi': to_float(flat.get('wifi.rssi')),
            'uptime': to_float(flat.get('sys.uptime')),
            'ram_free': to_float(flat.get('sys.ram_free')),
        }

        # The relay changing state is worth an event; nobody wants to trawl a
        # chart to find out when something switched off.
        if self.plug_output is not None and values['output'] != self.plug_output:
            self.store.add_event('plug', 'socket switched %s'
                                 % ('on' if values['output'] else 'off'),
                                 'info' if values['output'] else 'warn')
        self.plug_output = values['output']

        if now - self.last_plug_sample >= CONFIG['plug_sample_interval']:
            self.store.add_plug_sample(values, now)
            self.last_plug_sample = now

        self.plug = {
            'generated': int(now),
            'online': True,
            'host': CONFIG['plug_host'],
            'values': values,
            'vars': {k: v for k, v in flat.items() if not isinstance(v, (dict, list))},
            'descriptions': {k: PLUG_DESCRIPTIONS.get(k, describe_plug_field(k))
                             for k in flat},
            'info': self.plug_info or {},
            'counters': [dict(c, value=self._counter_value(flat, c['type']),
                              reset_at=self._counter_reset_at(flat, c['type'])[0],
                              reset_exact=self._counter_reset_at(flat, c['type'])[1])
                         for c in PLUG_COUNTERS],
        }
        return self.plug

    COUNTER_RESET_FIELDS = {
        'aenergy': 'aenergy.total_rst_ts',
        'ret_aenergy': 'ret_aenergy.total_rst_ts',
        'on_time': 'counts.on_time_rst_ts',
        'switch_on': 'counts.switch_on_rst_ts',
        'on_above_thr': 'counts.on_above_thr_rst_ts',
    }

    COUNTER_COLUMNS = {'aenergy': 'energy', 'ret_aenergy': 'energy_ret',
                       'on_time': 'on_time', 'switch_on': 'switch_on',
                       'on_above_thr': 'on_above_thr'}

    def _counter_reset_at(self, flat, counter):
        """When this counter started counting, and how sure we are.

        Three sources, in order of authority: the timestamp the plug publishes,
        a reset made from here, and failing both, the recorded history - these
        totals only climb, so a fall in the chart is a reset. If none of that
        applies, the oldest reading still gives a date the counter has been
        running since, which beats telling somebody nothing at all.
        """
        reported = to_float(flat.get(self.COUNTER_RESET_FIELDS.get(counter, '')))
        if reported and reported > 1000000000:
            return int(reported), True
        remembered = self.store.get_meta('plug_reset_' + counter)
        if remembered:
            return int(remembered), True
        column = self.COUNTER_COLUMNS.get(counter)
        if column:
            found, exact = self.store.counter_restarted_at(column)
            if found:
                return int(found), exact
        return None, False

    def plug_from_history(self):
        """Build a stale snapshot out of the newest stored sample."""
        row = self.store.latest_plug_sample()
        if not row:
            return {'online': False, 'host': CONFIG['plug_host'],
                    'error': self.plug_error or 'no reading yet'}

        values = {key: row.get(key) for key in Storage.PLUG_COLUMNS}
        info = {}
        remembered = self.store.get_meta('plug_info')
        if remembered:
            try:
                info = json.loads(remembered)
            except ValueError:
                info = {}
        counters = [dict(c,
                         value={'aenergy': row.get('energy'),
                                'ret_aenergy': row.get('energy_ret'),
                                'on_time': row.get('on_time'),
                                'switch_on': row.get('switch_on'),
                                'on_above_thr': row.get('on_above_thr')}[c['type']],
                         reset_at=self._counter_reset_at({}, c['type'])[0],
                         reset_exact=self._counter_reset_at({}, c['type'])[1])
                    for c in PLUG_COUNTERS]
        return {
            'generated': row['ts'],
            'online': True,
            'stale': True,
            'from_history': True,
            'age': int(time.time() - row['ts']),
            'host': CONFIG['plug_host'],
            'error': self.plug_error or 'waiting for the first reading since restart',
            'values': values,
            'vars': {},
            'descriptions': {},
            'info': info,
            'counters': counters,
        }

    @staticmethod
    def _counter_value(flat, counter):
        """Where each resettable counter lives in the status reply."""
        return to_float(flat.get({
            'aenergy': 'aenergy.total',
            'ret_aenergy': 'ret_aenergy.total',
            'on_time': 'counts.on_time',
            'switch_on': 'counts.switch_on',
            'on_above_thr': 'counts.on_above_thr',
        }[counter]))

    def set_plug_output(self, on):
        try:
            ShellyRPC().set_output(on)
        except PlugError as e:
            return False, str(e)
        # Claim the new state before re-reading, or the poll would see a change
        # it did not cause and log the same switch a second time.
        self.plug_output = 1 if on else 0
        self.poll_plug(force=True)
        self.store.add_event('plug', 'socket switched %s from the dashboard'
                             % ('on' if on else 'off'),
                             'info' if on else 'warn')
        return True, 'socket switched %s' % ('on' if on else 'off')

    def read_plug_counters(self, attempts=2):
        """The current value of each resettable counter, straight from the plug.

        Raises rather than returning nothing: a verification built on an empty
        reading would be worse than no verification at all.
        """
        last = None
        for attempt in range(attempts):
            try:
                flat = flatten(ShellyRPC().switch_status())
                break
            except PlugError as e:
                last = e
                time.sleep(1.5)
        else:
            raise last or PlugError('could not read the counters')
        return {
            'aenergy': to_float(flat.get('aenergy.total')),
            'ret_aenergy': to_float(flat.get('ret_aenergy.total')),
            'on_time': to_float(flat.get('counts.on_time')),
            'switch_on': to_float(flat.get('counts.switch_on')),
            'on_above_thr': to_float(flat.get('counts.on_above_thr')),
        }

    def reset_plug_counters(self, types=None):
        """Reset counters and check they actually moved.

        The plug answers a reset with success whether or not it understood the
        counter name, so taking its word for it means reporting a reset that
        never happened. Read the values back and compare.
        """
        wanted = [t for t in (types or []) if t in PLUG_COUNTER_TYPES]
        try:
            before = self.read_plug_counters()
        except PlugError as e:
            return False, 'could not read the counters first: %s' % e

        try:
            ShellyRPC().reset_counters(wanted or None)
        except PlugError as e:
            return False, str(e)

        time.sleep(1.0)                     # the plug needs a moment to settle
        try:
            after = self.read_plug_counters()
        except PlugError as e:
            # The reset probably worked; we simply cannot say so honestly.
            self.store.add_event('plug', 'reset requested but the result could '
                                 'not be checked: %s' % e, 'warn')
            return True, ('the reset was sent, but the plug did not answer when '
                          'asked to confirm it — check the counters in a moment')
        checked = wanted or PLUG_COUNTER_TYPES
        labels = {c['type']: c['label'] for c in PLUG_COUNTERS}

        cleared, unchanged, already = [], [], []
        for counter in checked:
            was, now = before.get(counter), after.get(counter)
            if was is None and now is None:
                continue                    # this model does not have it
            if now is not None and now == 0:
                (already if was in (0, None) else cleared).append(labels[counter])
            elif was is not None and now is not None and now < was:
                cleared.append(labels[counter])
            else:
                unchanged.append(labels[counter])

        # Record when each counter was cleared before rebuilding the snapshot,
        # so the dashboard shows the new date at once rather than a poll later.
        # The plug's own *_rst_ts fields are used when it publishes them; this
        # is the fallback for models that report 0 or omit them.
        stamped = int(time.time())
        for counter in checked:
            if labels[counter] in cleared:
                self.store.set_meta('plug_reset_' + counter, stamped)
        self.poll_plug(force=True)

        if cleared:
            message = 'reset ' + ', '.join(c.lower() for c in cleared)
            if unchanged:
                message += ('; the plug did not clear '
                            + ', '.join(u.lower() for u in unchanged))
            self.store.add_event('plug', message, 'info')
            return True, message
        if already and not unchanged:
            message = ', '.join(a.lower() for a in already) + ' was already at zero'
            return True, message
        message = ('the plug accepted the request but nothing changed: '
                   + ', '.join(u.lower() for u in unchanged or checked)
                   + '. This firmware may not support resetting those.')
        self.store.add_event('plug', message, 'warn')
        return False, message

    # -- control ------------------------------------------------------------
    def run_command(self, cmd):
        """Run an instant command. Returns (ok, message)."""
        if is_dangerous(cmd) and not CONFIG['allow_dangerous_commands']:
            return False, ('"%s" would cut power to the load. Set '
                           'allow_dangerous_commands in the config to permit it.' % cmd)
        with self._control_lock:
            try:
                client, name = connect_ups(authenticate=True)
            except NUTError as e:
                return False, str(e)
            try:
                client.instcmd(name, cmd)
            except NUTError as e:
                self.store.add_event('command', '%s refused: %s' % (cmd, e),
                                     'warn', name)
                return False, explain_error(str(e), 'instcmds = ALL')
            finally:
                client.close()
        self.store.add_event('command', 'ran %s' % cmd, 'info', self.ups_name)
        if cmd in FOLLOWABLE_COMMANDS:
            threading.Thread(target=self._follow_command, args=(cmd,),
                             daemon=True).start()
        return True, '%s accepted' % cmd

    def _follow_command(self, cmd):
        """Watch a test through to its verdict and record what happened."""
        limit = FOLLOWABLE_COMMANDS.get(cmd, 120)
        started = time.time()
        opening = (self.snapshot.get('vars', {}) or {})
        test_id = self.store.start_test(self.ups_name, cmd, 'dashboard', opening)
        self.active_test_id = test_id
        seen_running = False
        discharge_seconds = 0.0
        last = None
        min_voltage = None
        voltages = set()
        baseline = (self.snapshot.get('vars', {}) or {}).get('ups.test.result')

        while time.time() - started < limit:
            time.sleep(2.0)
            variables = self.poll()
            if not variables:
                continue
            now = time.time()
            flags = status_flags(variables)
            if flags & DISCHARGE_FLAGS:
                discharge_seconds += (now - last) if last else 0.0
            last = now
            voltage = to_float(variables.get('battery.voltage'))
            if voltage is not None:
                voltages.add(voltage)
                min_voltage = voltage if min_voltage is None else min(min_voltage, voltage)

            result = (variables.get('ups.test.result') or '').lower()
            running = ('progress' in result or 'pending' in result
                       or bool(flags & DISCHARGE_FLAGS))
            if running:
                seen_running = True
            elif seen_running:
                break

        elapsed = int(time.time() - started)
        result = (self.snapshot.get('vars', {}) or {}).get('ups.test.result', '')
        parts = ['%s finished after %ds' % (cmd, elapsed)]
        if result and (result != baseline or seen_running):
            parts.append('result: %s' % result)
        elif result:
            parts.append('result unchanged: %s' % result)
        if discharge_seconds >= 1:
            parts.append('on battery for about %ds' % int(round(discharge_seconds)))
        if min_voltage is not None and len(voltages) > 1:
            parts.append('battery dipped to %.1f V' % min_voltage)
        level = 'info'
        if result and 'pass' not in result.lower() and 'done' not in result.lower():
            level = 'warn'
        self.store.add_event('test', '; '.join(parts), level, self.ups_name)

        closing = (self.snapshot.get('vars', {}) or {})
        self.store.finish_test(
            test_id, result, closing,
            on_battery_s=int(round(discharge_seconds)) or None,
            voltage_min=min_voltage if len(voltages) > 1 else None)
        self.active_test_id = None

    def reset_data(self, scope):
        """Erase recorded data. Returns (ok, message, rows removed per table)."""
        try:
            removed = self.store.reset(scope)
        except sqlite3.Error as e:
            return False, 'could not clear the database: %s' % e, {}

        total = sum(removed.values())
        # Logged after the wipe, so the entry survives even when the event log
        # itself was part of what went.
        self.store.add_event('data', 'cleared %s (%d row%s)'
                             % (scope, total, '' if total == 1 else 's'),
                             'warn', self.ups_name)

        # Whatever the daemon was comparing against no longer exists, so start
        # from a clean slate rather than reporting transitions from deleted rows.
        if scope in ('all', 'history'):
            self.last_sample_at = 0.0
            self.last_vars_signature = None
        if scope in ('all', 'outages'):
            self.outage_id = None
        if scope in ('all', 'tests'):
            self.previous_test_result = None
        self.poll()
        return True, ('cleared %s: %d row%s removed'
                      % (scope, total, '' if total == 1 else 's')), removed

    def set_variable(self, var, value):
        """Change a writable variable, then wait for the driver to confirm it."""
        with self._control_lock:
            try:
                client, name = connect_ups(authenticate=True)
            except NUTError as e:
                return False, str(e)
            try:
                writable = client.list_rw(name)
                if var not in writable:
                    return False, ('%s is not writable on this UPS. Writable: %s'
                                   % (var, ', '.join(sorted(writable)) or 'nothing'))
                enum = client.list_enum(name, var)
                if enum and value not in enum:
                    return False, '%s only accepts: %s' % (var, ', '.join(enum))
                ranges = client.list_range(name, var)
                if ranges:
                    number = to_float(value)
                    if number is not None and not any(
                            to_float(lo) <= number <= to_float(hi) for lo, hi in ranges):
                        return False, ('%s must be within %s' % (var, ' or '.join(
                            '%s..%s' % r for r in ranges)))
                elif _unit_for(var) == '%':
                    # Most UPS units publish no range at all, so an obvious
                    # nonsense like 999% would go straight to the hardware and
                    # be silently clamped, or worse, accepted.
                    number = to_float(value)
                    if number is not None and not 0 <= number <= 100:
                        return False, '%s is a percentage - it must be 0 to 100' % var
                before = client.list_vars(name).get(var)
                client.set_var(name, var, value)
            except NUTError as e:
                self.store.add_event('setting', '%s=%s refused: %s' % (var, value, e),
                                     'warn', name)
                return False, explain_error(str(e), 'actions = SET')
            finally:
                client.close()

        self.recent_writes[var] = time.time()
        # usbhid-ups only refreshes its cache every pollinterval seconds, so a
        # read-back straight after the write still returns the old value.
        after = self._wait_for_value(var, value)
        if after is not None:
            self.capabilities['writable'][var] = after
            field = self.capabilities.get('fields', {}).get(var)
            if field is not None:
                field['value'] = after
        # Republish at once; otherwise the dashboard would show the old value
        # until the next scheduled poll came round.
        self.poll()
        if after == value:
            self.store.add_event('setting', '%s: %s -> %s' % (var, before, value),
                                 'info', self.ups_name)
            return True, '%s is now %s' % (var, after)
        return True, ('%s accepted, but the UPS still reports %s - it may have '
                      'adjusted the value, or the driver has not polled it back yet'
                      % (var, after))

    def _wait_for_value(self, var, expected, limit=None):
        variables = self.snapshot.get('vars', {}) or {}
        interval = to_float(variables.get('driver.parameter.pollinterval')) or 5.0
        limit = limit or max(6.0, interval * 3)
        deadline = time.time() + limit
        current = None
        while True:
            try:
                client, name = connect_ups()
            except NUTError:
                return None
            try:
                current = client.list_vars(name).get(var)
            finally:
                client.close()
            if current == expected or time.time() >= deadline:
                return current
            time.sleep(1.0)

    def poll_sensor(self, force=False):
        """Read a sensor that has an address of its own.

        A battery H&T is asleep almost always, so this usually fails and that
        is not an error - the webhook is what carries its readings. On USB
        power it answers every time and gives a proper history without needing
        webhooks at all.
        """
        if not CONFIG['sensor_host']:
            return None
        now = time.time()
        if not force and now - self.last_sensor_poll < CONFIG['plug_poll_interval']:
            return None
        self.last_sensor_poll = now

        try:
            rpc = ShellyRPC(CONFIG['sensor_host'], CONFIG['sensor_password'])
            flat = flatten(rpc.call('Shelly.GetStatus'))
        except PlugError as e:
            log('sensor poll: %s' % e, level='debug')
            return None

        def pick(*names):
            for name in names:
                for key, value in flat.items():
                    if key == name or key.endswith('.' + name):
                        number = to_float(value)
                        if number is not None:
                            return number
            return None

        values = {
            'temperature': pick('tC'),
            'humidity': pick('rh'),
            'battery_v': pick('battery.V', 'V'),
            'battery_pct': pick('battery.percent', 'percent'),
            'rssi': pick('rssi'),
        }
        if all(v is None for v in values.values()):
            return None

        source = CONFIG['sensor_host']
        self.store.add_sensor_sample(source, values, json.dumps(
            {k: v for k, v in flat.items() if not isinstance(v, (dict, list))}), now)
        entry = dict(values)
        entry['ts'] = int(now)
        entry['source'] = source
        entry['polled'] = True
        self.sensors[source] = entry
        return entry

    # -- pushed sensor readings ---------------------------------------------
    def serve_sensors(self):
        """Receive webhook pushes from battery sensors that cannot be polled.

        A Shelly H&T sleeps between measurements and is unreachable almost all
        the time, so it pushes instead. Point its webhook at this port:

          http://<this-host>:8088/?t=${ev.tC}&rh=${ev.rh}
        """
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _accept(self, fields, raw):
                source = (fields.get('id') or fields.get('device')
                          or self.client_address[0])
                values = {
                    'temperature': to_float(fields.get('t') or fields.get('tC')
                                            or fields.get('temperature')),
                    'humidity': to_float(fields.get('rh') or fields.get('humidity')),
                    'battery_v': to_float(fields.get('bv') or fields.get('battery')),
                    'battery_pct': to_float(fields.get('bp') or fields.get('percent')),
                    'rssi': to_float(fields.get('rssi')),
                }
                if all(v is None for v in values.values()):
                    self.send_response(400)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                now = time.time()
                daemon.store.add_sensor_sample(source, values, raw, now)
                previous = daemon.sensors.get(source)
                entry = dict(values)
                entry['ts'] = int(now)
                entry['source'] = source
                daemon.sensors[source] = entry
                if previous is None:
                    daemon.store.add_event('sensor', 'first reading from %s' % source,
                                           'info')
                log('sensor %s: %s' % (source, raw), level='debug')
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                fields = {k: v[0] for k, v in query.items() if v}
                self._accept(fields, json.dumps(fields))

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length).decode('utf-8', 'replace') if length else ''
                fields = {}
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        fields = {k: v for k, v in flatten(parsed).items()}
                except ValueError:
                    fields = {k: v[0] for k, v in parse_qs(raw).items() if v}
                # A Shelly webhook body nests the reading; flatten finds it
                # wherever the firmware decided to put it.
                short = {}
                for key, value in fields.items():
                    short[key.rsplit('.', 1)[-1]] = value
                short.update(fields)
                self._accept(short, raw)

        try:
            server = ThreadingHTTPServer(
                (CONFIG['sensor_listen_host'], int(CONFIG['sensor_listen_port'])),
                Handler)
        except OSError as e:
            log('cannot listen for sensor pushes on %s:%s - %s'
                % (CONFIG['sensor_listen_host'], CONFIG['sensor_listen_port'], e),
                level='error')
            return
        server.daemon_threads = True
        log('listening for sensor pushes on http://%s:%s/'
            % (CONFIG['sensor_listen_host'], CONFIG['sensor_listen_port']))
        server.serve_forever()

    # -- loops --------------------------------------------------------------
    def run(self):
        # logrotate signals us after it has moved the file aside.
        def reopen(signum, frame):
            open_log()
            log('log file reopened after rotation')
        try:
            signal.signal(signal.SIGHUP, reopen)
        except (ValueError, AttributeError, OSError):
            pass                      # not the main thread, or no SIGHUP here

        already = _running_instance()
        if already:
            log('another upsmon is already serving %s:%s (up %ss, UPS "%s")'
                % (CONFIG['api_host'], CONFIG['api_port'],
                   already.get('uptime'), already.get('ups')), level='error')
            log('nothing to do - use "systemctl status upsmon" to see it, or '
                'stop it first if you meant to run this one', level='error')
            raise SystemExit(3)

        for note in CONFIG_NOTES:
            log('config: %s' % note)
        log('upsmon %s starting; UPS at %s:%s, API on http://%s:%s'
            % (__version__, CONFIG['nut_host'], CONFIG['nut_port'],
               CONFIG['api_host'], CONFIG['api_port']))
        self.store.add_event('daemon', 'upsmon %s started' % __version__, 'info')

        api = threading.Thread(target=self.serve, daemon=True)
        api.start()

        if CONFIG['plug_host']:
            log('smart plug at %s, polled every %ss'
                % (CONFIG['plug_host'], CONFIG['plug_poll_interval']))
        if CONFIG['sensor_listen']:
            threading.Thread(target=self.serve_sensors, daemon=True).start()

        self.poll()
        while True:
            start = time.time()
            try:
                ensure_log_current()
                self.poll()
                try:
                    self.poll_plug()
                except Exception as e:      # the plug must never stop the UPS
                    log('plug error: %s' % e, level='error')
                try:
                    self.poll_sensor()
                except Exception as e:
                    log('sensor error: %s' % e, level='error')
                if start - self.last_aggregate_at >= CONFIG['aggregate_interval']:
                    self.last_aggregate_at = start
                    rolled = self.store.aggregate()
                    if rolled:
                        log('rolled %d hours of samples into the summary table' % rolled,
                            level='debug')
            except Exception as e:            # never let one bad poll kill the loop
                log('poll error: %s' % e, level='error')
            time.sleep(max(1.0, CONFIG['poll_interval'] - (time.time() - start)))

    # -- HTTP ---------------------------------------------------------------
    def serve(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'upsmon/' + __version__

            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode('utf-8')
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass          # the browser went away mid-reply; harmless

            def handle_one_request(self):
                try:
                    BaseHTTPRequestHandler.handle_one_request(self)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _authed(self):
                sent = self.headers.get('X-Upsmon-Token', '')
                return secrets.compare_digest(sent, daemon.token)

            def handle_request(self, method):
                """Route one request, turning any failure into a plain answer.

                Without this an unexpected error inside an endpoint escapes
                into the socket server, which prints an entire traceback per
                request. A browser polling every five seconds then buries the
                journal in identical stack traces and the actual cause scrolls
                away.
                """
                try:
                    return method()
                except BrokenPipeError:
                    raise
                except Exception as e:
                    log('%s %s failed: %s: %s'
                        % (self.command, self.path, type(e).__name__, e),
                        level='error')
                    self._send(500, {'ok': False,
                                     'error': '%s: %s' % (type(e).__name__, e)})

            def do_GET(self):
                self.handle_request(self._get)

            def do_POST(self):
                self.handle_request(self._post)

            def _get(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path in ('/status', '/api/status', '/data.json'):
                    with daemon.lock:
                        self._send(200, daemon.snapshot or
                                   {'online': False, 'error': daemon.last_error})
                elif path in ('/history', '/api/history'):
                    span = parse_span(query.get('range', ['24h'])[0])
                    points = min(5000, int(query.get('points', ['600'])[0] or 600))
                    since = time.time() - span
                    self._send(200, {
                        'range': span,
                        'generated': int(time.time()),
                        'samples': daemon.store.series(since, points=points),
                    })
                elif path in ('/events', '/api/events'):
                    limit = min(1000, int(query.get('limit', ['100'])[0] or 100))
                    self._send(200, {'events': daemon.store.events(limit)})
                elif path in ('/plug', '/api/plug'):
                    self._send(200, daemon.plug_snapshot()
                               or {'configured': False,
                                   'error': 'no plug configured'})
                elif path in ('/sensors', '/api/sensors'):
                    self._send(200, {
                        'listening': bool(CONFIG['sensor_listen']),
                        'endpoint': 'http://%s:%s/' % (CONFIG['sensor_listen_host'],
                                                       CONFIG['sensor_listen_port']),
                        'latest': sorted(daemon.sensors.values(),
                                         key=lambda s: s.get('source') or ''),
                        'sources': daemon.store.sensor_sources(),
                    })
                elif path in ('/plug-history', '/api/plug-history'):
                    span = parse_span(query.get('range', ['24h'])[0])
                    points = min(5000, int(query.get('points', ['600'])[0] or 600))
                    self._send(200, {
                        'range': span,
                        'samples': daemon.store.plug_series(time.time() - span,
                                                            points=points),
                    })
                elif path in ('/sensor-history', '/api/sensor-history'):
                    span = parse_span(query.get('range', ['24h'])[0])
                    points = min(5000, int(query.get('points', ['600'])[0] or 600))
                    self._send(200, {
                        'range': span,
                        'sources': daemon.store.sensor_sources(),
                        'samples': daemon.store.sensor_series(
                            time.time() - span, points=points,
                            source=(query.get('source', [''])[0] or None)),
                    })
                elif path in ('/tests', '/api/tests'):
                    limit = min(500, int(query.get('limit', ['50'])[0] or 50))
                    self._send(200, {'tests': daemon.store.tests(limit)})
                elif path in ('/outages', '/api/outages'):
                    self._send(200, {'outages': daemon.store.outages(
                        min(200, int(query.get('limit', ['20'])[0] or 20)))})
                elif path in ('/health', '/api/health'):
                    self._send(200, daemon.health())
                elif path in ('/capabilities', '/api/capabilities'):
                    self._send(200, {
                        'writable': daemon.capabilities['writable'],
                        'fields': daemon.capabilities.get('fields', {}),
                        'commands': [{'name': c, 'help': command_help(c),
                                      'dangerous': is_dangerous(c),
                                      'followable': c in FOLLOWABLE_COMMANDS}
                                     for c in daemon.capabilities['commands']],
                        'allow_dangerous': CONFIG['allow_dangerous_commands'],
                        'pin_required': daemon.pin_required(),
                        'locked_for': max(0, int(daemon.pin_locked_until - time.time())),
                    })
                else:
                    self._send(404, {'ok': False, 'error': 'no such endpoint'})

            def _post(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if not self._authed():
                    self._send(403, {'ok': False, 'error': 'bad or missing token'})
                    return

                length = int(self.headers.get('Content-Length') or 0)
                payload = {}
                if length:
                    try:
                        payload = json.loads(self.rfile.read(length).decode('utf-8'))
                    except ValueError:
                        payload = {}

                if path in ('/command', '/api/command'):
                    cmd = payload.get('command') or query.get('command', [''])[0]
                    if not cmd:
                        self._send(400, {'ok': False, 'error': 'no command given'})
                        return
                    allowed, why = daemon.check_pin(payload.get('pin'))
                    if not allowed:
                        self._send(403, {'ok': False, 'error': why, 'pin_error': True,
                                         'locked': time.time() < daemon.pin_locked_until})
                        return
                    ok, message = daemon.run_command(cmd)
                    self._send(200 if ok else 400, {'ok': ok, 'message': message})
                elif path in ('/set', '/api/set'):
                    var = payload.get('var') or query.get('var', [''])[0]
                    value = payload.get('value', query.get('value', [''])[0])
                    if not var:
                        self._send(400, {'ok': False, 'error': 'no variable given'})
                        return
                    allowed, why = daemon.check_pin(payload.get('pin'))
                    if not allowed:
                        self._send(403, {'ok': False, 'error': why, 'pin_error': True,
                                         'locked': time.time() < daemon.pin_locked_until})
                        return
                    ok, message = daemon.set_variable(var, str(value))
                    # Hand back what the UPS actually holds now, so the page can
                    # correct its own field without waiting for a poll.
                    self._send(200 if ok else 400, {
                        'ok': ok, 'message': message, 'var': var,
                        'value': daemon.capabilities['writable'].get(var),
                    })
                elif path in ('/plug/switch', '/api/plug/switch'):
                    if not CONFIG['plug_host']:
                        self._send(400, {'ok': False, 'error': 'no plug configured'})
                        return
                    wanted = payload.get('on')
                    allowed, why = daemon.check_pin(payload.get('pin'))
                    if not allowed:
                        self._send(403, {'ok': False, 'error': why, 'pin_error': True,
                                         'locked': time.time() < daemon.pin_locked_until})
                        return
                    ok, message = daemon.set_plug_output(bool(wanted))
                    self._send(200 if ok else 400,
                               {'ok': ok, 'message': message,
                                'plug': daemon.plug_snapshot()})
                elif path in ('/plug/reset', '/api/plug/reset'):
                    if not CONFIG['plug_host']:
                        self._send(400, {'ok': False, 'error': 'no plug configured'})
                        return
                    allowed, why = daemon.check_pin(payload.get('pin'))
                    if not allowed:
                        self._send(403, {'ok': False, 'error': why, 'pin_error': True,
                                         'locked': time.time() < daemon.pin_locked_until})
                        return
                    ok, message = daemon.reset_plug_counters(payload.get('types'))
                    self._send(200 if ok else 400,
                               {'ok': ok, 'message': message,
                                'plug': daemon.plug_snapshot()})
                elif path in ('/reset', '/api/reset'):
                    scope = (payload.get('scope')
                             or query.get('scope', ['all'])[0] or 'all')
                    if scope not in Storage.RESET_SCOPES:
                        self._send(400, {'ok': False,
                                         'error': 'unknown scope "%s"' % scope})
                        return
                    allowed, why = daemon.check_pin(payload.get('pin'))
                    if not allowed:
                        self._send(403, {'ok': False, 'error': why,
                                         'pin_error': True,
                                         'locked': time.time() < daemon.pin_locked_until})
                        return
                    ok, message, removed = daemon.reset_data(scope)
                    self._send(200 if ok else 400,
                               {'ok': ok, 'message': message, 'removed': removed})
                elif path in ('/poll', '/api/poll'):
                    daemon.poll()
                    with daemon.lock:
                        self._send(200, {'ok': True, 'snapshot': daemon.snapshot})
                else:
                    self._send(404, {'ok': False, 'error': 'no such endpoint'})

        try:
            server = ThreadingHTTPServer((CONFIG['api_host'], int(CONFIG['api_port'])),
                                         Handler)
        except OSError as e:
            # Losing the API silently would leave a daemon that collects data
            # nobody can read. Say why, and take the whole process down so
            # systemd restarts it rather than leaving it half working.
            log('cannot listen on %s:%s - %s' % (CONFIG['api_host'],
                                                 CONFIG['api_port'], e),
                level='error')
            log('change api_port in %s, or stop whatever holds it' % CONFIG_FILE,
                level='error')
            os._exit(3)
        server.daemon_threads = True
        server.serve_forever()

    def health(self):
        stats = self.store.stats()
        with self.lock:
            snap = self.snapshot
        age = int(time.time() - self.last_ok) if self.last_ok else None
        return {
            'ok': bool(self.last_ok) and self.consecutive_errors == 0,
            'version': __version__,
            'uptime': int(time.time() - self.started),
            'ups': self.ups_name,
            'nut': '%s:%s' % (CONFIG['nut_host'], CONFIG['nut_port']),
            'last_contact_age': age,
            'consecutive_errors': self.consecutive_errors,
            'suppressed_reads': self.suppressed_reads,
            'log_file': str(LOG_PATH) if LOG_PATH else None,
            'plug': ({'host': CONFIG['plug_host'],
                      'online': self.plug_error is None and self.plug is not None,
                      'error': self.plug_error} if CONFIG['plug_host'] else None),
            'sensors': {'listening': bool(CONFIG['sensor_listen']),
                        'devices': len(self.sensors)},
            'last_error': self.last_error,
            'level': snap.get('level'),
            'database': stats,
            'config_notes': CONFIG_NOTES,
        }


def explain_error(message, needed_right):
    if 'USERNAME-REQUIRED' in message or 'PASSWORD-REQUIRED' in message:
        return ('upsd refuses anonymous writes - set nut_username and nut_password '
                'in %s' % CONFIG_FILE)
    if 'ACCESS-DENIED' in message:
        return ('the upsd account lacks "%s" - add it to upsd.users on the NAS'
                % needed_right)
    if 'CMD-NOT-SUPPORTED' in message or 'VAR-NOT-SUPPORTED' in message:
        return 'this UPS does not implement that'
    if 'INVALID-VALUE' in message:
        return 'the UPS rejected that value'
    return message


def _running_instance():
    """Ask the API whether a upsmon is already up. None if the port is free."""
    import urllib.request
    import urllib.error
    url = 'http://%s:%s/api/health' % (CONFIG['api_host'], CONFIG['api_port'])
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return None


def parse_span(text):
    """'24h', '7d', '30m', '1y' or a plain number of seconds."""
    text = str(text).strip().lower()
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([smhdwy]?)$', text)
    if not match:
        return 86400
    value = float(match.group(1))
    unit = match.group(2) or 's'
    return int(value * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400,
                        'w': 604800, 'y': 31536000}[unit])



# ---------------------------------------------------------------------------
# Shelly command line
# ---------------------------------------------------------------------------
# Everything the standalone reader could do, addressed by role rather than by
# IP: "plug" and "sensor" resolve to the hosts and passwords already in the
# configuration, so nothing has to be typed twice or pasted into a shell.
SHELLY_DEVICES = {
    'plug':      ('plug_host', 'plug_password'),
    'sensor':    ('sensor_host', 'sensor_password'),
    'temp':      ('sensor_host', 'sensor_password'),
    'tempmeter': ('sensor_host', 'sensor_password'),
    'ht':        ('sensor_host', 'sensor_password'),
}


def shelly_for(role):
    """Build a client for 'plug' or 'sensor' from the configuration."""
    if role not in SHELLY_DEVICES:
        raise PlugError('unknown device "%s" - use plug or sensor' % role)
    host_key, password_key = SHELLY_DEVICES[role]
    host = CONFIG.get(host_key)
    if not host:
        raise PlugError('no address configured for the %s - set %s in %s'
                        % (role, host_key, CONFIG_FILE))
    return ShellyRPC(host, CONFIG.get(password_key))


SHELLY_COMPONENTS = ('Shelly.GetStatus', 'Shelly.GetConfig', 'Shelly.GetDeviceInfo',
                     'Sys.GetStatus', 'Wifi.GetStatus', 'Cloud.GetStatus',
                     'MQTT.GetStatus', 'BLE.GetConfig', 'Matter.GetStatus',
                     'Switch.GetStatus', 'Temperature.GetStatus',
                     'Humidity.GetStatus', 'DevicePower.GetStatus')


def shelly_everything(rpc):
    """Ask for every component the device might have; skip what it lacks."""
    out = {}
    for method in SHELLY_COMPONENTS:
        params = None
        if method.split('.')[0] in ('Switch', 'Temperature', 'Humidity', 'DevicePower'):
            params = {'id': int(CONFIG['plug_switch_id'])
                      if method.startswith('Switch') else 0}
        try:
            result = rpc.call(method, params)
        except PlugError as e:
            if not is_missing_component(e):
                raise                # a rate limit or a dead network, not absence
            continue                 # not present on this model, which is normal
        if result is not None:
            out[method] = result
    return out


def shelly_print_flat(pal, flat, title=None):
    if title:
        print(pal.group(title))
    width = max([len(k) for k in flat] + [10])
    for key in sorted(flat):
        value = flat[key]
        if isinstance(value, list):
            value = ', '.join(str(v) for v in value)
        text = 'ON' if value is True else 'OFF' if value is False else str(value)
        painter = (pal.ok if value is True else pal.bad if value is False
                   else pal.note if value is None or value == '' else pal.value)
        print('  %s : %s' % (pal.key(key.ljust(width)), painter(text)))
        note = PLUG_DESCRIPTIONS.get(re.sub(r'^[a-z_0-9]+:\d+\.', '', key))
        if note:
            print('  %s   %s' % (' ' * width, pal.note(note)))


# ---- mDNS discovery -------------------------------------------------------
def shelly_discover(pal, seconds=4.0):
    """Find Shelly devices by asking _shelly._tcp.local over multicast DNS.

    The published tool uses the zeroconf package for this. One query and a few
    seconds of listening is all it takes, so it is done here with a socket
    rather than adding a dependency to a daemon that has none.
    """
    import struct

    def encode_name(name):
        out = b''
        for label in name.split('.'):
            if label:
                out += bytes([len(label)]) + label.encode('ascii')
        return out + b'\x00'

    def read_name(data, offset, depth=0):
        parts = []
        while offset < len(data) and depth < 20:
            length = data[offset]
            if length == 0:
                offset += 1
                break
            if length & 0xC0 == 0xC0:          # a compression pointer
                pointer = struct.unpack('!H', data[offset:offset + 2])[0] & 0x3FFF
                parts.append(read_name(data, pointer, depth + 1)[0])
                offset += 2
                break
            parts.append(data[offset + 1:offset + 1 + length].decode('ascii', 'replace'))
            offset += 1 + length
        return '.'.join(p for p in parts if p), offset

    query = (struct.pack('!HHHHHH', 0, 0, 1, 0, 0, 0)
             + encode_name('_shelly._tcp.local') + struct.pack('!HH', 12, 1))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    try:
        sock.bind(('', 0))
        sock.sendto(query, ('224.0.0.251', 5353))
    except OSError as e:
        print(pal.bad('cannot send the multicast query: %s' % e))
        sock.close()
        return {}

    found = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            counts = struct.unpack('!HHHHHH', data[:12])
            offset = 12
            for _ in range(counts[2]):          # skip the questions
                _, offset = read_name(data, offset)
                offset += 4
            names = []
            for _ in range(counts[3] + counts[4] + counts[5]):
                name, offset = read_name(data, offset)
                rtype, _cls, _ttl, length = struct.unpack('!HHIH', data[offset:offset + 10])
                offset += 10
                if rtype == 12:                 # PTR
                    target, _ = read_name(data, offset)
                    names.append(target)
                offset += length
        except (struct.error, IndexError):
            names = []
        entry = found.setdefault(addr[0], {'ip': addr[0], 'names': set()})
        for name in names:
            if 'shelly' in name.lower():
                entry['names'].add(name.split('.')[0])
    sock.close()
    return {ip: {'ip': ip, 'names': sorted(v['names'])}
            for ip, v in found.items() if v['names']}


# ---- WebSocket, for live pushes -------------------------------------------
def _ws_send(sock, payload):
    """Send one masked text frame, as a client must."""
    import struct
    data = payload.encode('utf-8')
    header = bytes([0x81])
    mask = secrets.token_bytes(4)
    if len(data) < 126:
        header += bytes([0x80 | len(data)])
    elif len(data) < 65536:
        header += bytes([0x80 | 126]) + struct.pack('!H', len(data))
    else:
        header += bytes([0x80 | 127]) + struct.pack('!Q', len(data))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(header + mask + masked)


def _ws_frames(sock):
    """Yield the payload of each text frame arriving on an open socket."""
    import struct
    buffer = b''

    def need(count):
        nonlocal buffer
        while len(buffer) < count:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError('the connection closed')
            buffer += chunk
        taken, buffer = buffer[:count], buffer[count:]
        return taken

    while True:
        first, second = need(2)
        opcode = first & 0x0F
        masked = second & 0x80
        length = second & 0x7F
        if length == 126:
            length = struct.unpack('!H', need(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', need(8))[0]
        mask = need(4) if masked else None
        payload = need(length) if length else b''
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:                       # close
            return
        if opcode == 0x9:                       # ping, answer with a pong
            sock.sendall(bytes([0x8A, 0x00]))
            continue
        if opcode in (0x1, 0x2) and payload:
            yield payload.decode('utf-8', 'replace')


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
class Palette(object):
    """Colour for the terminal, switched off when the output is not a terminal."""

    def __init__(self, enabled=True):
        self.enabled = enabled

    def _c(self, code, text):
        return ('\033[%sm%s\033[0m' % (code, text)) if self.enabled else text

    def title(self, t):  return self._c('1;36', t)
    def rule(self, t):   return self._c('36', t)
    def group(self, t):  return self._c('1;35', t)
    def key(self, t):    return self._c('37', t)
    def value(self, t):  return self._c('1;33', t)
    def note(self, t):   return self._c('2;37', t)
    def ok(self, t):     return self._c('1;32', t)
    def warn(self, t):   return self._c('1;33', t)
    def bad(self, t):    return self._c('1;31', t)
    def info(self, t):   return self._c('34', t)


def colour_enabled(force=None):
    if force is False:
        return False
    if force is True:
        return True
    if os.environ.get('NO_COLOR') is not None:
        return False
    return sys.stdout.isatty()


def api_call(path, method='GET', payload=None, timeout=10.0, token=None):
    """Talk to a running daemon over its localhost API."""
    import urllib.request
    import urllib.error
    url = 'http://%s:%s%s' % (CONFIG['api_host'], CONFIG['api_port'], path)
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    if token is None:
        try:
            token = token_path().read_text().strip()
        except OSError:
            token = None
    if token:
        request.add_header('X-Upsmon-Token', token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {'error': str(e)}


def print_status(pal, variables, meta=None):
    """The full report, straight from the UPS - no daemon needed."""
    meta = meta or {}
    bar = '=' * 78
    print(pal.rule(bar))
    print('  ' + pal.title('UPS "%s"' % meta.get('ups', '?')))
    print('  ' + pal.note('upsmon v%s   |   NUT: %s   |   upsd: %s   |   %s'
                          % (__version__, meta.get('host', ''), meta.get('server', ''),
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
    print(pal.rule(bar))

    level, issues = assess(variables)
    paint = {'ok': pal.ok, 'warn': pal.warn, 'crit': pal.bad}[level]
    status = variables.get('ups.status', '')
    print('  %-12s: %s  %s' % ('Status', paint(status),
                               pal.note('(' + describe_status(status) + ')')))
    for key, label, unit in (('battery.charge', 'Battery', ' %'),
                             ('ups.load', 'Load', ' %'),
                             ('input.voltage', 'Input', ' V'),
                             ('output.voltage', 'Output', ' V'),
                             ('battery.voltage', 'Batt. volt', ' V'),
                             ('ups.realpower', 'Power', ' W')):
        if key in variables:
            print('  %-12s: %s%s' % (label, pal.value(variables[key]), unit))
    if 'battery.runtime' in variables:
        print('  %-12s: %s' % ('Runtime',
                               pal.value(human_runtime(variables['battery.runtime']))))
    for issue in issues:
        painter = pal.bad if issue['level'] == 'crit' else pal.warn
        print('  %-12s  %s' % ('', painter('! ' + issue['text'])))
    print(pal.rule(bar))

    groups = {}
    order = ['device', 'ups', 'battery', 'input', 'output', 'ambient', 'outlet',
             'driver', 'server']
    for key in variables:
        prefix = key.split('.')[0]
        groups.setdefault(prefix if prefix in order else 'other', []).append(key)
    ordered = ([g for g in order if g in groups]
               + [g for g in sorted(groups) if g not in order])
    width = max([len(k) for k in variables] + [10])
    for group in ordered:
        print('\n' + pal.group('-- %s ' % group.upper())
              + pal.rule('-' * max(1, 74 - len(group))))
        for key in sorted(groups[group]):
            extra = ''
            if key == 'ups.status':
                extra = '  ' + pal.note('(%s)' % describe_status(variables[key]))
            elif key.endswith('runtime'):
                pretty = human_runtime(variables[key])
                extra = ('  ' + pal.note('(%s)' % pretty)) if pretty else ''
            flag = '  ' + pal.ok('[rw]') if key in meta.get('writable', {}) else ''
            print('  %s : %s%s%s' % (pal.key(key.ljust(width)),
                                     pal.value(variables[key]), extra, flag))
            note = variable_description(key)
            if note:
                print('  %s   %s' % (' ' * width, pal.note(note)))
    print('\n' + pal.rule(bar))
    print('  ' + pal.note('Variables: %d   |   writable: %d   |   commands: %d'
                          % (len(variables), len(meta.get('writable', {})),
                             len(meta.get('commands', [])))))
    print(pal.rule(bar))


def print_command_rows(pal, rows):
    if not rows:
        return
    name_width = max(len(name) for name, _, _ in rows)
    text_width = max(len(text) for _, _, text in rows)
    marker = '[DANGEROUS]'
    try:
        import shutil
        columns = shutil.get_terminal_size((100, 25)).columns
    except Exception:
        columns = 100
    trailing = 2 + name_width + 3 + text_width + 2 + len(marker) <= columns
    for name, danger, text in rows:
        label = (pal.bad if danger else pal.info)(name.ljust(name_width))
        if danger and trailing:
            print('  %s   %s%s' % (label, pal.note(text.ljust(text_width)),
                                   pal.bad('  ' + marker)))
        elif danger:
            print('  %s  %s  %s' % (label, pal.bad(marker), pal.note(text)))
        else:
            print('  %s   %s' % (label, pal.note(text)))


def cmd_status(args, pal):
    try:
        client, name = connect_ups()
    except NUTError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2
    try:
        variables = client.list_vars(name)
        meta = {'ups': name, 'host': CONFIG['nut_host'],
                'server': client.server_version(),
                'writable': client.list_rw(name),
                'commands': client.list_cmds(name)}
    finally:
        client.close()
    if args.json:
        print(json.dumps({'ups': name, 'vars': variables,
                          'writable': meta['writable'],
                          'commands': meta['commands']}, indent=2))
    else:
        print_status(pal, variables, meta)
    return 0


def cmd_list_rw(args, pal):
    try:
        client, name = connect_ups()
    except NUTError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2
    try:
        writable = client.list_rw(name)
        commands = client.list_cmds(name)
        bar = '=' * 78
        print(pal.rule(bar))
        print('  ' + pal.title('Writable variables on "%s"' % name))
        print('  ' + pal.note('upsmon v%s   |   NUT: %s   |   %s'
                              % (__version__, CONFIG['nut_host'],
                                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
        print(pal.rule(bar))
        if not writable:
            print('  ' + pal.warn('This UPS exposes no writable variables.'))
        for var in sorted(writable):
            print('\n  %s = %s   %s' % (pal.key(var), pal.value(writable[var]),
                                        pal.note(client.get_type(name, var))))
            note = variable_description(var)
            if note:
                print('    ' + pal.note(note))
            enum = client.list_enum(name, var)
            if enum:
                print('    ' + pal.info('allowed: ' + ', '.join(enum)))
            for low, high in client.list_range(name, var):
                print('    ' + pal.info('range: %s .. %s' % (low, high)))
        print('\n' + pal.group('-- INSTANT COMMANDS ') + pal.rule('-' * 57))
        if not commands:
            print('  ' + pal.warn('This UPS exposes no instant commands.'))
        print_command_rows(pal, [(c, is_dangerous(c), command_help(c))
                                 for c in sorted(commands)])
        print('\n' + pal.rule(bar))
        print('  ' + pal.note('--set VAR=VALUE changes a variable, '
                              '--exec COMMAND runs a command.'))
        print(pal.rule(bar))
    finally:
        client.close()
    return 0


def cmd_control(args, pal):
    """--set and --exec. Uses the daemon when it is running, direct upsd if not."""
    status, _ = api_call('/api/health', timeout=2.0)
    via_daemon = status == 200
    if via_daemon:
        print(pal.note('using the running daemon'))
    else:
        print(pal.note('the daemon is not responding - talking to upsd directly'))
        if not CONFIG['nut_username'] and sys.stdin.isatty():
            CONFIG['nut_username'] = input('upsd username: ').strip()
            CONFIG['nut_password'] = getpass.getpass('upsd password: ')

    failed = False
    daemon = None if via_daemon else Daemon(storage=Storage())

    pin = args.pin
    if pin is None and via_daemon:
        status, caps = api_call('/api/capabilities', timeout=4.0)
        if status == 200 and caps.get('pin_required'):
            pin = (getpass.getpass('PIN: ') if sys.stdin.isatty()
                   else '')

    for item in args.set_vars:
        if '=' not in item:
            print(pal.bad('SET  ') + '--set expects VAR=VALUE, got "%s"' % item)
            failed = True
            continue
        var, value = item.split('=', 1)
        if via_daemon:
            code, body = api_call('/api/set', 'POST',
                                  {'var': var.strip(), 'value': value.strip(),
                                   'pin': pin},
                                  timeout=45.0)
            ok = code == 200 and body.get('ok')
            message = body.get('message') or body.get('error') or 'no reply'
        else:
            ok, message = daemon.set_variable(var.strip(), value.strip())
        print((pal.ok('SET  ') if ok else pal.bad('SET  ')) + message)
        failed = failed or not ok

    for cmd in args.exec_cmds:
        if via_daemon:
            code, body = api_call('/api/command', 'POST',
                                  {'command': cmd, 'pin': pin}, timeout=30.0)
            ok = code == 200 and body.get('ok')
            message = body.get('message') or body.get('error') or 'no reply'
        else:
            ok, message = daemon.run_command(cmd)
        print((pal.ok('EXEC ') if ok else pal.bad('EXEC ')) + message)
        failed = failed or not ok
        if ok and cmd in FOLLOWABLE_COMMANDS:
            print(pal.note('  the daemon is following this test; watch it with '
                           '--events or in the web interface'))
    return 1 if failed else 0


def cmd_oneshot(args, pal):
    store = Storage()
    daemon = Daemon(storage=store)
    variables = daemon.poll()
    if not variables:
        print(pal.bad('ERROR: ') + (daemon.last_error or 'no data'), file=sys.stderr)
        return 2
    daemon.store.add_sample(daemon.ups_name, variables)
    stats = store.stats()
    print(pal.ok('recorded ') + 'one sample for "%s"' % daemon.ups_name)
    print(pal.note('  database %s, %d samples, %d hourly rows, %d events'
                   % (store.path, stats['samples'], stats['samples_hourly'],
                      stats['events'])))
    level, issues = assess(variables)
    paint = {'ok': pal.ok, 'warn': pal.warn, 'crit': pal.bad}[level]
    print('  status ' + paint(variables.get('ups.status', '?'))
          + pal.note('  charge %s%%  load %s%%  runtime %s'
                     % (variables.get('battery.charge', '?'),
                        variables.get('ups.load', '?'),
                        human_runtime(variables.get('battery.runtime')))))
    for issue in issues:
        print('  ' + (pal.bad if issue['level'] == 'crit' else pal.warn)
              ('! ' + issue['text']))
    return 0


SERVICE_USER = 'upsmon'


def readable_by_service_user(path):
    return _accessible_by_service_user(path, write=False)


def writable_by_service_user(path):
    return _accessible_by_service_user(path, write=True)


def _accessible_by_service_user(path, write=True):
    """Can the account the service runs as read (or write) here?

    Running --check with sudo is the normal case, and root can do anything, so
    os.access() would answer yes about a file the daemon cannot touch. Ask
    about the daemon's own user instead, and say plainly whose answer it is.
    """
    import pwd
    import grp
    try:
        stat = path.stat()
    except OSError as e:
        return False, '%s: %s' % (path, e)

    try:
        owner = pwd.getpwuid(stat.st_uid).pw_name
        group = grp.getgrgid(stat.st_gid).gr_name
    except KeyError:
        owner, group = str(stat.st_uid), str(stat.st_gid)
    described = '%s (%s:%s mode %o)' % (path, owner, group, stat.st_mode & 0o777)

    wanted = os.W_OK if write else os.R_OK
    me = pwd.getpwuid(os.getuid()).pw_name
    if me != 'root':
        return os.access(path, wanted), described + ' as %s' % me

    try:
        service = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return os.access(path, wanted), described + ' (no %s user)' % SERVICE_USER

    owner_bit, group_bit, other_bit = ((0o200, 0o020, 0o002) if write
                                       else (0o400, 0o040, 0o004))
    mode = stat.st_mode
    if stat.st_uid == service.pw_uid:
        allowed = bool(mode & owner_bit)
    elif stat.st_gid == service.pw_gid or SERVICE_USER in _group_members(stat.st_gid):
        allowed = bool(mode & group_bit)
    else:
        allowed = bool(mode & other_bit)
    return allowed, described + ' for %s' % SERVICE_USER


def _group_members(gid):
    import grp
    try:
        return grp.getgrgid(gid).gr_mem
    except KeyError:
        return []


def cmd_reset(args, pal):
    """Erase recorded data. Irreversible, so it asks first."""
    scope = args.reset_data
    try:
        store = Storage()
    except StorageError as e:
        print(pal.bad('DATABASE: ') + str(e), file=sys.stderr)
        return 2

    tables = Storage.RESET_SCOPES[scope]
    counts = store.counts(tables)
    total = sum(counts.values())

    print(pal.title('Reset "%s" in %s' % (scope, store.path)))
    for table in tables:
        print('  %-16s %8d row%s' % (table, counts[table],
                                     '' if counts[table] == 1 else 's'))
    if not total:
        print(pal.note('  nothing to delete'))
        return 0

    # A running daemon will carry on writing, and keeps its own idea of the
    # current status - so the first sample after a reset would be judged
    # against state that no longer exists in the database.
    running = _running_instance()
    if running:
        print(pal.warn('  the daemon is running and will keep recording; '
                       'restart it afterwards so it starts from a clean slate'))

    if not args.yes:
        if not sys.stdin.isatty():
            print(pal.bad('refusing to delete without confirmation - '
                          'add --yes'), file=sys.stderr)
            return 1
        answer = input('  delete %d rows? this cannot be undone [y/N] ' % total)
        if answer.strip().lower() not in ('y', 'yes'):
            print(pal.note('  left alone'))
            return 0

    removed = store.reset(scope)
    print(pal.ok('  deleted ') + '%d rows' % sum(removed.values()))
    try:
        size = store.path.stat().st_size
        print(pal.note('  database is now %.2f MB' % (size / 1024.0 / 1024.0)))
    except OSError:
        pass
    if running:
        print(pal.note('  now run:  sudo systemctl restart upsmon'))
    return 0


def cmd_check(args, pal):
    """Everything that should be true when the system is healthy."""
    problems = 0

    def line(label, ok, detail=''):
        mark = pal.ok('ok   ') if ok else pal.bad('FAIL ')
        print('  %s %-34s %s' % (mark, label, pal.note(detail)))
        return 0 if ok else 1

    status, health = api_call('/api/health', timeout=4.0)
    problems += line('daemon API reachable', status == 200,
                     'http://%s:%s' % (CONFIG['api_host'], CONFIG['api_port'])
                     if status == 200 else health.get('error', ''))
    if status == 200:
        age = health.get('last_contact_age')
        problems += line('recent contact with upsd',
                         age is not None and age < 120,
                         'last reading %ss ago' % age if age is not None else 'never')
        problems += line('no polling errors', not health.get('consecutive_errors'),
                         health.get('last_error') or '')
        database = health.get('database', {})
        problems += line('history is being written', bool(database.get('samples')),
                         '%s samples, %s hourly rows'
                         % (database.get('samples'), database.get('samples_hourly')))
        size = database.get('db_bytes')
        if size:
            problems += line('database size sane', size < 2 * 1024 ** 3,
                             '%.1f MB' % (size / 1024.0 / 1024.0))
        for note in health.get('config_notes', []):
            print('  %s %s' % (pal.warn('note '), pal.note(note)))
    else:
        try:
            client, name = connect_ups()
            client.close()
            problems += line('upsd reachable without the daemon', True, name)
        except NUTError as e:
            problems += line('upsd reachable without the daemon', False, str(e))

    problems += line('token file present', token_path().is_file(), str(token_path()))

    log_path = CONFIG.get('log_file')
    if log_path:
        directory = Path(log_path).parent
        if Path(log_path).exists():
            ok_log, detail = writable_by_service_user(Path(log_path))
        elif directory.exists():
            ok_log, detail = writable_by_service_user(directory)
            detail += ' (log not created yet)'
        else:
            ok_log, detail = False, '%s does not exist' % directory
        if not ok_log:
            detail += '  -> sudo install -d -m 750 -o %s -g %s %s' % (
                SERVICE_USER, SERVICE_USER, directory)
        problems += line('log file writable', ok_log, detail)

    if CONFIG_FILE.is_file():
        readable, detail = readable_by_service_user(CONFIG_FILE)
        if not readable:
            detail += '  -> sudo chown root:%s %s' % (SERVICE_USER, CONFIG_FILE)
        problems += line('config readable by the service', readable, detail)
        problems += line('config actually loaded', not CONFIG_FATAL,
                         '; '.join(CONFIG_FATAL) if CONFIG_FATAL
                         else 'UPS at %s:%s' % (CONFIG['nut_host'], CONFIG['nut_port']))

    # The most common deployment failure: a root-owned database the daemon's
    # own user cannot write. SQLite calls that "readonly", which sends people
    # looking in the wrong place entirely.
    directory = Path(DB_FILE).parent
    if directory.exists():
        writable, detail = writable_by_service_user(directory)
        problems += line('database directory writable', writable, detail)
        db = Path(DB_FILE)
        if db.exists():
            ok_db, note = writable_by_service_user(db)
            if not ok_db:
                note += '  -> sudo chown -R %s:%s %s' % (SERVICE_USER, SERVICE_USER,
                                                         directory)
            problems += line('database file writable', ok_db, note)
    print()
    print('  ' + (pal.ok('everything looks healthy') if not problems
                  else pal.bad('%d check(s) failed' % problems)))
    return 1 if problems else 0


def cmd_diag(args, pal):
    print(pal.title('upsmon %s' % __version__))
    print(pal.note('  config file : %s (%s)'
                   % (CONFIG_FILE, 'present' if CONFIG_FILE.is_file() else 'absent, using defaults')))
    print(pal.note('  database    : %s' % DB_FILE))
    print(pal.note('  token file  : %s' % token_path()))
    log_path = CONFIG.get('log_file')
    if log_path:
        try:
            size = Path(log_path).stat().st_size
            detail = '%s (%.1f KB)' % (log_path, size / 1024.0)
        except OSError:
            detail = '%s (not created yet)' % log_path
    else:
        detail = 'disabled, journal only'
    print(pal.note('  log file    : %s' % detail))
    print()
    print(pal.group('settings in force'))
    for key in sorted(CONFIG):
        value = CONFIG[key]
        if value and ('password' in key or key == 'pin'):
            value = '(set)'
        marker = '' if value == DEFAULTS[key] else pal.value('  <- changed')
        print('  %-28s %s%s' % (key, value, marker))
    for note in CONFIG_NOTES:
        print('  ' + pal.warn(note))

    print()
    print(pal.group('daemon API'))
    started = time.time()
    status, health = api_call('/api/health', timeout=5.0)
    elapsed = (time.time() - started) * 1000
    if status == 200:
        print('  %s in %.0f ms' % (pal.ok('responded'), elapsed))
        print('  uptime %ss, ups "%s", level %s'
              % (health.get('uptime'), health.get('ups'), health.get('level')))
        suppressed = health.get('suppressed_reads')
        if suppressed:
            hours = (health.get('uptime') or 1) / 3600.0
            print('  %d single-poll status glitch(es) ignored, %.1f per hour'
                  % (suppressed, suppressed / max(hours, 0.01)))
        database = health.get('database', {})
        if database.get('db_bytes'):
            print('  database %.2f MB, %s samples, %s hourly, %s events, %s tests'
                  % (database['db_bytes'] / 1024.0 / 1024.0, database.get('samples'),
                     database.get('samples_hourly'), database.get('events'),
                     database.get('tests')))
            if database.get('wal_bytes'):
                print('    %.2f MB of data plus a %.2f MB write-ahead log, which '
                      'SQLite folds back in periodically'
                      % (database.get('file_bytes', 0) / 1024.0 / 1024.0,
                         database['wal_bytes'] / 1024.0 / 1024.0))
        first = database.get('first_hourly') or database.get('first_sample')
        if first:
            days = (time.time() - first) / 86400.0
            print('  history covers %.1f days' % days)
    else:
        print('  ' + pal.bad('no answer') + pal.note('  %s' % health.get('error', '')))

    print()
    print(pal.group('upsd'))
    started = time.time()
    try:
        client, name = connect_ups()
        variables = client.list_vars(name)
        writable = client.list_rw(name)
        commands = client.list_cmds(name)
        server = client.server_version()
        client.close()
        elapsed = (time.time() - started) * 1000
        print('  %s in %.0f ms' % (pal.ok('responded'), elapsed))
        print('  %s' % server)
        print('  ups "%s": %d variables, %d writable, %d commands'
              % (name, len(variables), len(writable), len(commands)))
        print('  status %s  (%s)' % (pal.value(variables.get('ups.status', '?')),
                                     describe_status(variables.get('ups.status'))))
    except NUTError as e:
        print('  ' + pal.bad(str(e)))
    return 0


def cmd_history(args, pal):
    store = Storage(readonly=True)
    span = parse_span(args.history)
    rows = store.series(time.time() - span, points=args.points)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(pal.warn('no samples in that period'))
        return 0
    print(pal.group('  %-19s %-14s %6s %8s %6s %7s %7s'
                    % ('time', 'status', 'chg%', 'runtime', 'load%', 'input', 'batt')))
    for row in rows:
        stamp = datetime.fromtimestamp(row['ts']).strftime('%Y-%m-%d %H:%M:%S')
        flags = set((row.get('status') or '').split())
        status = row.get('status') or ''
        painter = pal.bad if flags & CRITICAL_FLAGS else (
            pal.warn if flags & WARNING_FLAGS else pal.ok)
        print('  %-19s %s %6s %8s %6s %7s %7s'
              % (stamp, painter(status.ljust(14)),
                 _fmt(row.get('charge'), 0), human_runtime(row.get('runtime')) or '-',
                 _fmt(row.get('load'), 0), _fmt(row.get('input_v'), 1),
                 _fmt(row.get('battery_v'), 1)))
    print(pal.note('  %d rows over %s' % (len(rows), args.history)))
    return 0


def cmd_events(args, pal):
    store = Storage(readonly=True)
    rows = store.events(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(pal.warn('no events recorded yet'))
        return 0
    for row in reversed(rows):
        stamp = datetime.fromtimestamp(row['ts']).strftime('%Y-%m-%d %H:%M:%S')
        painter = {'crit': pal.bad, 'warn': pal.warn}.get(row['level'], pal.ok)
        print('  %s  %s  %-9s %s' % (pal.note(stamp), painter('*'),
                                     row['kind'], row['detail']))
    return 0


def cmd_plug(args, pal):
    """Read or switch the smart plug, with or without the daemon running."""
    if not CONFIG['plug_host']:
        print(pal.warn('no plug configured - set plug_host in %s' % CONFIG_FILE))
        return 1

    # Switching goes through the daemon when there is one, so the action lands
    # in the same event log as everything else.
    if args.plug_on or args.plug_off or args.plug_reset:
        status, _ = api_call('/api/health', timeout=2.0)
        if status == 200:
            pin = CONFIG.get('pin') or ''
            if args.plug_reset:
                types = ([] if args.plug_reset == 'all'
                         else [t.strip() for t in args.plug_reset.split(',')])
                path, body = '/api/plug/reset', {'pin': pin, 'types': types}
            else:
                path, body = '/api/plug/switch', {'on': bool(args.plug_on), 'pin': pin}
            code, reply = api_call(path, 'POST', body, timeout=20.0)
            ok = code == 200 and reply.get('ok')
            print((pal.ok('PLUG ') if ok else pal.bad('PLUG '))
                  + (reply.get('message') or reply.get('error') or 'no reply'))
            return 0 if ok else 1
        try:
            rpc = ShellyRPC()
            if args.plug_reset:
                types = ([] if args.plug_reset == 'all'
                         else [t.strip() for t in args.plug_reset.split(',')])
                rpc.reset_counters(types or None)
                print(pal.ok('PLUG ') + 'reset %s'
                      % (', '.join(types) if types else 'every counter'))
            else:
                rpc.set_output(bool(args.plug_on))
                print(pal.ok('PLUG ') + 'socket switched %s'
                      % ('on' if args.plug_on else 'off'))
            return 0
        except PlugError as e:
            print(pal.bad('PLUG ') + str(e), file=sys.stderr)
            return 2

    try:
        rpc = ShellyRPC()
        switch = rpc.switch_status()
        device = rpc.device_status()
        info = rpc.device_info()
    except PlugError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2

    flat = flatten(switch)
    flat.update(flatten(device))
    if args.json:
        print(json.dumps({'info': info, 'switch': switch, 'device': device}, indent=2))
        return 0

    bar = '=' * 78
    print(pal.rule(bar))
    print('  ' + pal.title('%s  %s' % (info.get('model', 'plug'), info.get('name', ''))))
    print('  ' + pal.note('upsmon v%s   |   %s   |   firmware %s   |   %s'
                          % (__version__, CONFIG['plug_host'], info.get('ver', '?'),
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
    print(pal.rule(bar))
    on = bool(switch.get('output'))
    print('  %-12s: %s' % ('Socket', pal.ok('ON') if on else pal.bad('OFF')))
    for key, label, unit in (('apower', 'Power', ' W'), ('voltage', 'Voltage', ' V'),
                             ('current', 'Current', ' A'), ('pf', 'Power factor', ''),
                             ('freq', 'Frequency', ' Hz')):
        if switch.get(key) is not None:
            print('  %-12s: %s%s' % (label, pal.value(str(switch[key])), unit))
    energy = to_float((switch.get('aenergy') or {}).get('total'))
    if energy is not None:
        print('  %-12s: %s Wh   %s' % ('Energy', pal.value('%.1f' % energy),
                                       pal.note('(%.3f kWh)' % (energy / 1000.0))))
    temp = to_float((switch.get('temperature') or {}).get('tC'))
    if temp is not None:
        painter = pal.bad if temp >= 70 else pal.warn if temp >= 55 else pal.value
        print('  %-12s: %s degC' % ('Plug temp', painter('%.1f' % temp)))
    print(pal.rule(bar))

    width = max([len(k) for k in flat] + [10])
    for key in sorted(flat):
        value = flat[key]
        if isinstance(value, list):
            value = ', '.join(str(v) for v in value)
        print('  %s : %s' % (pal.key(key.ljust(width)), pal.value(str(value))))
        note = PLUG_DESCRIPTIONS.get(re.sub(r'^[a-z_0-9]+:\d+\.', '', key))
        if note:
            print('  %s   %s' % (' ' * width, pal.note(note)))
    print('\n' + pal.rule(bar))
    print('  ' + pal.note('%d values' % len(flat)))
    print(pal.rule(bar))
    return 0


def cmd_sensors(args, pal):
    store = Storage(readonly=True)
    rows = store.sensor_series(time.time() - parse_span(args.history or '7d'),
                               points=args.limit)
    sources = store.sensor_sources()
    if args.json:
        print(json.dumps({'sources': sources, 'samples': rows}, indent=2))
        return 0
    if not sources:
        print(pal.warn('no sensor has pushed anything yet'))
        print(pal.note('  point the sensor webhook at http://%s:%s/?t=${ev.tC}&rh=${ev.rh}'
                       % (CONFIG['sensor_listen_host'], CONFIG['sensor_listen_port'])))
        return 0
    print(pal.group('  devices'))
    for s in sources:
        print('    %-20s %5d reading(s), last %s'
              % (s['source'], s['samples'],
                 datetime.fromtimestamp(s['last']).strftime('%Y-%m-%d %H:%M:%S')))
    print()
    print(pal.group('  %-19s %-18s %8s %8s %8s' % ('time', 'source', 'degC', 'RH %', 'batt %')))
    for row in rows[-args.limit:]:
        print('  %-19s %-18s %8s %8s %8s'
              % (datetime.fromtimestamp(row['ts']).strftime('%Y-%m-%d %H:%M:%S'),
                 row['source'], _fmt(row.get('temperature'), 1),
                 _fmt(row.get('humidity'), 1), _fmt(row.get('battery_pct'), 0)))
    return 0


def cmd_tests(args, pal):
    store = Storage(readonly=True)
    rows = store.tests(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(pal.warn('no self tests recorded yet'))
        print(pal.note('  run one with:  upsmon --exec test.battery.start.quick'))
        return 0
    print(pal.group('  %-19s %-9s %-22s %8s %9s %8s'
                    % ('started', 'source', 'result', 'duration', 'on batt', 'min V')))
    for row in rows:
        painter = pal.ok if row['passed'] == 1 else (
            pal.bad if row['passed'] == 0 else pal.warn)
        print('  %-19s %-9s %s %8s %9s %8s'
              % (datetime.fromtimestamp(row['started']).strftime('%Y-%m-%d %H:%M:%S'),
                 row['source'] or '-',
                 painter((row['result'] or 'running').ljust(22)),
                 ('%ds' % row['duration_s']) if row['duration_s'] else '-',
                 ('%ds' % row['on_battery_s']) if row['on_battery_s'] else '-',
                 _fmt(row['voltage_min'], 1)))
    passed = sum(1 for r in rows if r['passed'] == 1)
    failed = sum(1 for r in rows if r['passed'] == 0)
    print(pal.note('  %d test(s): %d passed, %d failed' % (len(rows), passed, failed)))
    return 0


def _fmt(value, digits):
    if value is None:
        return '-'
    return ('%%.%df' % digits) % value



# ---------------------------------------------------------------------------
# Shelly subcommands
# ---------------------------------------------------------------------------
def shelly_main(argv, pal):
    """upsmon shelly <plug|sensor> <command> — the whole reader, by role."""
    parser = argparse.ArgumentParser(
        prog='upsmon shelly',
        description='Talk to the Shelly devices named in the configuration. '
                    'The address and password come from there, so only the role '
                    'and the command are needed.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
  upsmon shelly plug info                identity and firmware
  upsmon shelly plug dump                every value the device reports
  upsmon shelly plug dump --json
  upsmon shelly plug poll --interval 5   a live table of the numbers
  upsmon shelly plug watch               the same, pushed rather than polled
  upsmon shelly plug on / off / toggle
  upsmon shelly plug reset-counters              every counter
  upsmon shelly plug reset-counters aenergy      just the energy total
  upsmon shelly plug reboot --wait
  upsmon shelly plug ble status
  upsmon shelly plug matter off --reboot
  upsmon shelly plug call Switch.GetStatus '{"id":0}'
  upsmon shelly sensor dump              the thermometer, when it is awake
  upsmon shelly listen --port 8088       receive pushes from a sleeping sensor
  upsmon shelly serve --port 8089        accept an outbound websocket
  upsmon shelly discover                 find devices on the LAN
""")
    parser.add_argument('device', nargs='?', default='plug',
                        help='plug or sensor (also temp, tempmeter, ht)')
    parser.add_argument('command', nargs='?', default='dump')
    parser.add_argument('rest', nargs='*', help='arguments for the command')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    parser.add_argument('--interval', type=float, default=5.0,
                        help='seconds between readings for poll (default 5)')
    parser.add_argument('--count', type=int, help='stop after this many readings')
    parser.add_argument('--csv', metavar='FILE', help='also append readings to a CSV')
    parser.add_argument('--port', type=int, help='port for listen and serve')
    parser.add_argument('--wait', action='store_true',
                        help='wait for the device to come back after a reboot')
    parser.add_argument('--reboot', action='store_true',
                        help='reboot after changing a radio setting')
    parser.add_argument('--seconds', type=float, default=4.0,
                        help='how long discover listens (default 4)')
    parser.add_argument('--no-color', action='store_true')
    args = parser.parse_args(argv)

    # "upsmon shelly discover" and "listen" need no device, so a command given
    # in the device position is taken as the command.
    if args.device not in SHELLY_DEVICES:
        args.rest = ([args.command] if args.command != 'dump' else []) + args.rest
        args.command = args.device
        args.device = 'plug'

    handler = {
        'discover': shelly_cmd_discover, 'info': shelly_cmd_info,
        'dump': shelly_cmd_dump, 'poll': shelly_cmd_poll,
        'watch': shelly_cmd_watch, 'listen': shelly_cmd_listen,
        'serve': shelly_cmd_serve, 'on': shelly_cmd_switch,
        'off': shelly_cmd_switch, 'toggle': shelly_cmd_switch,
        'reset-counters': shelly_cmd_reset, 'call': shelly_cmd_call,
        'reboot': shelly_cmd_reboot, 'ble': shelly_cmd_radio,
        'matter': shelly_cmd_radio,
    }.get(args.command)

    if handler is None:
        print(pal.bad('unknown command "%s"' % args.command), file=sys.stderr)
        parser.print_help()
        return 2
    try:
        return handler(args, pal)
    except PlugError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\nstopped.')
        return 0


def shelly_cmd_discover(args, pal):
    print(pal.note('asking the network for Shelly devices, %.0fs...' % args.seconds))
    found = shelly_discover(pal, args.seconds)
    if args.json:
        print(json.dumps(list(found.values()), indent=2))
        return 0
    if not found:
        print(pal.warn('nothing answered'))
        print(pal.note('  mDNS does not cross subnets, and some networks block it. '
                       'If the device is elsewhere, address it by IP in the config.'))
        return 1
    for entry in sorted(found.values(), key=lambda e: e['ip']):
        print('  %s  %s' % (pal.value(entry['ip'].ljust(15)),
                            pal.note(', '.join(entry['names']))))
    print(pal.note('  %d device(s)' % len(found)))
    return 0


def shelly_cmd_info(args, pal):
    rpc = shelly_for(args.device)
    info = rpc.call('Shelly.GetDeviceInfo')
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    shelly_print_flat(pal, flatten(info), '  %s' % rpc.host)
    return 0


def shelly_cmd_dump(args, pal):
    rpc = shelly_for(args.device)
    everything = shelly_everything(rpc)
    if not everything:
        raise PlugError('the device answered nothing we recognise')
    if args.json:
        print(json.dumps(everything, indent=2))
        return 0
    info = everything.get('Shelly.GetDeviceInfo', {})
    bar = '=' * 78
    print(pal.rule(bar))
    print('  ' + pal.title('%s  %s' % (info.get('model', args.device),
                                       info.get('name', ''))))
    print('  ' + pal.note('upsmon v%s   |   %s   |   firmware %s   |   %s'
                          % (__version__, rpc.host, info.get('ver', '?'),
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
    print(pal.rule(bar))
    for method in sorted(everything):
        flat = flatten(everything[method])
        if not flat:
            continue
        print('\n' + pal.group('-- %s ' % method)
              + pal.rule('-' * max(1, 74 - len(method))))
        shelly_print_flat(pal, flat)
    print('\n' + pal.rule(bar))
    return 0


# What a live table should show first. Timestamps and component ids are
# numbers too, and picking those over the actual measurements made the poll
# output useless.
POLL_PREFERRED = ['apower', 'voltage', 'current', 'pf', 'freq', 'aenergy.total',
                  'ret_aenergy.total', 'temperature.tC', 'tC', 'rh',
                  'battery.percent', 'battery.V', 'wifi.rssi', 'sys.uptime']
POLL_SKIP = re.compile(r'(_ts$|\.id$|^id$|minute_ts|\.0$|ram_|fs_)')


def shelly_numeric(flat):
    numeric = [k for k, v in flat.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)
               and not POLL_SKIP.search(k)]
    def rank(key):
        tail = re.sub(r'^[a-z_0-9]+:\d+\.', '', key)
        for index, wanted in enumerate(POLL_PREFERRED):
            if tail == wanted:
                return (0, index)
        return (1, key)
    return sorted(numeric, key=rank)


def shelly_cmd_poll(args, pal):
    rpc = shelly_for(args.device)
    import csv as csv_module
    handle = open(args.csv, 'a', newline='', encoding='utf-8') if args.csv else None
    writer = csv_module.writer(handle) if handle else None

    columns, printed, count = None, 0, 0
    try:
        while True:
            try:
                status = rpc.call('Switch.GetStatus', {'id': int(CONFIG['plug_switch_id'])}) \
                    if args.device == 'plug' else rpc.call('Shelly.GetStatus')
                flat = flatten(status)
            except PlugError as e:
                # A battery sensor is asleep most of the time; say so and retry
                # rather than giving up, which is what makes poll usable at all.
                print(pal.note('%s  %s' % (datetime.now().strftime('%H:%M:%S'), e)))
                time.sleep(args.interval)
                continue

            if columns is None:
                columns = shelly_numeric(flat)[:8]
                if writer and handle.tell() == 0:
                    writer.writerow(['timestamp'] + columns)
            if printed % 20 == 0:
                print(pal.group('  %-8s ' % 'time'
                                + ' '.join(c.rsplit('.', 1)[-1][:10].rjust(11)
                                           for c in columns)))
            print('  %-8s ' % datetime.now().strftime('%H:%M:%S')
                  + ' '.join(pal.value(('%g' % flat[c] if isinstance(flat.get(c), float)
                                        else str(flat.get(c, '-'))).rjust(11))
                             for c in columns))
            if writer:
                writer.writerow([datetime.now().isoformat(timespec='seconds')]
                                + [flat.get(c) for c in columns])
                handle.flush()
            printed += 1
            count += 1
            if args.count and count >= args.count:
                return 0
            time.sleep(args.interval)
    finally:
        if handle:
            handle.close()


def shelly_cmd_watch(args, pal):
    """Subscribe to the device's own notifications instead of polling."""
    import base64
    rpc = shelly_for(args.device)
    host = rpc.host.split(':')[0]
    port = int(rpc.host.split(':')[1]) if ':' in rpc.host else 80
    key = base64.b64encode(secrets.token_bytes(16)).decode()

    if CONFIG.get(SHELLY_DEVICES[args.device][1]):
        print(pal.warn('this device has a password set. The websocket channel '
                       'does not carry that authentication - use poll instead.'))
        return 1

    try:
        sock = socket.create_connection((host, port), 10)
    except OSError as e:
        raise PlugError('cannot reach %s (%s)' % (rpc.host, e))
    request = ('GET /rpc HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
               'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
               'Sec-WebSocket-Version: 13\r\n\r\n' % (rpc.host, key))
    sock.sendall(request.encode())
    reply = b''
    while b'\r\n\r\n' not in reply:
        chunk = sock.recv(4096)
        if not chunk:
            raise PlugError('the device closed the connection during the handshake')
        reply += chunk
    if b'101' not in reply.split(b'\r\n')[0]:
        raise PlugError('the device refused the websocket upgrade')

    print(pal.note('connected to %s - press Ctrl+C to stop' % rpc.host))
    _ws_send(sock, json.dumps({'id': 1, 'src': 'upsmon',
                               'method': 'Shelly.GetStatus'}))
    try:
        for message in _ws_frames(sock):
            try:
                body = json.loads(message)
            except ValueError:
                continue
            payload = body.get('params') or body.get('result') or {}
            flat = flatten(payload)
            if not flat:
                continue
            stamp = datetime.now().strftime('%H:%M:%S')
            if args.json:
                print(json.dumps({'ts': stamp, 'data': payload}))
                continue
            print(pal.note('  ' + stamp) + '  ' + body.get('method', 'result'))
            shelly_print_flat(pal, flat)
    except (ConnectionError, OSError) as e:
        print(pal.warn('connection lost: %s' % e))
    finally:
        sock.close()
    return 0


def shelly_cmd_listen(args, pal):
    """Print webhook pushes as they arrive, without touching the database."""
    port = args.port or int(CONFIG['sensor_listen_port'])
    host = CONFIG['sensor_listen_host']

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *ignored):
            pass

        def _show(self, payload):
            print('%s  %s  %s'
                  % (pal.note(datetime.now().strftime('%H:%M:%S')),
                     pal.info(self.client_address[0]), pal.value(payload)))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            fields = {k: v[0] for k, v in query.items() if v}
            self._show(json.dumps(fields) if fields else self.path)

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            self._show(self.rfile.read(length).decode('utf-8', 'replace'))

    server = ThreadingHTTPServer((host, port), Handler)
    print(pal.note('listening on http://%s:%s/ - point the sensor webhook here, '
                   'Ctrl+C to stop' % (host, port)))
    print(pal.note('nothing is recorded; this only shows what arrives. The daemon '
                   'stores pushes when sensor_listen is on.'))
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def shelly_cmd_serve(args, pal):
    """Accept an outbound websocket from a device configured to call us."""
    import base64
    import hashlib
    port = args.port or 8089
    guid = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', port))
    listener.listen(4)
    print(pal.note('waiting for a device to connect to ws://<this-host>:%d/ - '
                   'Ctrl+C to stop' % port))
    try:
        while True:
            client, address = listener.accept()
            request = b''
            while b'\r\n\r\n' not in request:
                chunk = client.recv(4096)
                if not chunk:
                    break
                request += chunk
            match = re.search(rb'Sec-WebSocket-Key:\s*(\S+)', request, re.I)
            if not match:
                client.close()
                continue
            accept = base64.b64encode(
                hashlib.sha1(match.group(1) + guid.encode()).digest()).decode()
            client.sendall(('HTTP/1.1 101 Switching Protocols\r\n'
                            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                            'Sec-WebSocket-Accept: %s\r\n\r\n' % accept).encode())
            print(pal.ok('  connected: ') + address[0])
            try:
                for message in _ws_frames(client):
                    stamp = datetime.now().strftime('%H:%M:%S')
                    try:
                        body = json.loads(message)
                    except ValueError:
                        print('  %s  %s' % (pal.note(stamp), message))
                        continue
                    flat = flatten(body.get('params') or body)
                    print('  %s  %s' % (pal.note(stamp),
                                        body.get('method', 'message')))
                    shelly_print_flat(pal, flat)
            except (ConnectionError, OSError):
                pass
            finally:
                client.close()
                print(pal.note('  disconnected: %s' % address[0]))
    finally:
        listener.close()
    return 0


def shelly_cmd_switch(args, pal):
    rpc = shelly_for(args.device)
    switch_id = int(CONFIG['plug_switch_id'])
    if args.command == 'toggle':
        rpc.call('Switch.Toggle', {'id': switch_id})
    else:
        rpc.call('Switch.Set', {'id': switch_id, 'on': args.command == 'on'})
    state = rpc.call('Switch.GetStatus', {'id': switch_id})
    on = bool(state.get('output'))
    print(pal.ok('  socket is ON') if on else pal.bad('  socket is OFF'))
    if state.get('apower') is not None:
        print(pal.note('  drawing %s W' % state['apower']))
    return 0


def shelly_cmd_reset(args, pal):
    """reset-counters [type ...] — no type means every counter the plug has."""
    rpc = shelly_for(args.device)
    switch_id = int(CONFIG['plug_switch_id'])
    types = [t for arg in args.rest for t in arg.split(',') if t]
    unknown = [t for t in types if t not in PLUG_COUNTER_TYPES]
    if unknown:
        print(pal.bad('  unknown counter: %s' % ', '.join(unknown)))
        print(pal.note('  choose from: %s' % ', '.join(PLUG_COUNTER_TYPES)))
        return 1

    rpc.reset_counters(types or None)
    state = rpc.call('Switch.GetStatus', {'id': switch_id})
    print(pal.ok('  reset %s' % (', '.join(types) if types else 'every counter')))
    flat = flatten(state)
    for key in ('aenergy.total', 'ret_aenergy.total', 'counts.on_time',
                'counts.switch_on', 'counts.on_above_thr'):
        if key in flat:
            print(pal.note('    %-22s %s' % (key, flat[key])))
    return 0


def shelly_cmd_call(args, pal):
    if not args.rest:
        raise PlugError('give a method, e.g. call Switch.GetStatus \'{"id":0}\'')
    rpc = shelly_for(args.device)
    method = args.rest[0]
    params = None
    if len(args.rest) > 1:
        try:
            params = json.loads(args.rest[1])
        except ValueError:
            raise PlugError('the parameters must be JSON: %s' % args.rest[1])
    result = rpc.call(method, params)
    if args.json or not isinstance(result, dict):
        print(json.dumps(result, indent=2))
        return 0
    shelly_print_flat(pal, flatten(result), '  %s' % method)
    return 0


def shelly_cmd_reboot(args, pal):
    rpc = shelly_for(args.device)
    rpc.call('Shelly.Reboot')
    print(pal.ok('  reboot requested'))
    if not args.wait:
        return 0
    print(pal.note('  waiting for it to come back...'))
    deadline = time.time() + 60
    time.sleep(3)
    while time.time() < deadline:
        try:
            info = rpc.call('Shelly.GetDeviceInfo')
            print(pal.ok('  back after %ds' % int(60 - (deadline - time.time())))
                  + pal.note('  firmware %s' % info.get('ver')))
            return 0
        except PlugError:
            time.sleep(2)
    print(pal.warn('  it has not answered within 60s'))
    return 1


def is_missing_component(error):
    """Did the device say it does not have this, or did something else fail?"""
    text = str(error).lower()
    if 'rate limiting' in text or '429' in text or 'cannot reach' in text:
        return False
    return ('unknown method' in text or 'no handler' in text
            or '-105' in text or 'not found' in text or '404' in text)


def shelly_cmd_radio(args, pal):
    """ble and matter: status, on, off — both are a config flag plus a reboot."""
    rpc = shelly_for(args.device)
    component = 'BLE' if args.command == 'ble' else 'Matter'
    action = (args.rest[0] if args.rest else 'status').lower()

    if action == 'status':
        try:
            config = rpc.call('%s.GetConfig' % component)
        except PlugError as e:
            # Only a refusal to recognise the method means the component is
            # missing. A rate limit or a network problem is a different thing
            # entirely and saying otherwise sends people looking in the wrong
            # place.
            if is_missing_component(e):
                raise PlugError('%s is not available on this device' % component)
            raise
        enabled = bool(config.get('enable'))
        print('  %s is %s' % (component, pal.ok('enabled') if enabled
                              else pal.bad('disabled')))
        try:
            shelly_print_flat(pal, flatten(rpc.call('%s.GetStatus' % component)))
        except PlugError:
            pass
        return 0

    if action not in ('on', 'off'):
        raise PlugError('use %s status, %s on or %s off'
                        % (args.command, args.command, args.command))
    rpc.call('%s.SetConfig' % component, {'config': {'enable': action == 'on'}})
    config = rpc.call('%s.GetConfig' % component)
    settled = bool(config.get('enable'))
    if settled != (action == 'on'):
        print(pal.warn('  the device did not accept the change'))
        return 1
    print(pal.ok('  %s %s' % (component, 'enabled' if settled else 'disabled')))
    if args.reboot:
        return shelly_cmd_reboot(args, pal)
    print(pal.note('  a reboot is needed for this to take effect: '
                   'upsmon shelly %s reboot --wait' % args.device))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='upsmon',
        description='UPS monitoring daemon and command line tool (upsmon %s).'
                    % __version__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Run with no arguments to start the daemon in the foreground - that is what the
systemd unit does. Everything else below is for testing and day-to-day use.

  upsmon --status                    full report straight from the UPS
  upsmon --status --json             the same as JSON
  upsmon --oneshot                   one collection into the database
  upsmon --list-rw                   what this UPS lets you change
  upsmon --set battery.charge.low=20
  upsmon --exec test.battery.start.quick
  upsmon --check                     is the whole system healthy
  upsmon --diag                      settings, API latency, database size
  upsmon --history 7d                recent samples as a table
  upsmon --events                    what the daemon has logged
  upsmon --tests                     the self-test history
  upsmon --plug                      everything the smart plug reports
  upsmon --plug-off                  switch the socket off
  upsmon --sensors                   readings pushed by battery sensors

Every command the standalone Shelly reader had is available under "shelly",
addressed by role rather than by IP - the address and password come from the
configuration:

  upsmon shelly plug info            identity and firmware
  upsmon shelly plug dump            every value the device reports
  upsmon shelly plug poll            a live table of the numbers
  upsmon shelly plug watch           the same, pushed rather than polled
  upsmon shelly plug on|off|toggle
  upsmon shelly plug reset-counters
  upsmon shelly plug reboot --wait
  upsmon shelly plug ble status
  upsmon shelly plug matter off --reboot
  upsmon shelly plug call Switch.GetStatus '{"id":0}'
  upsmon shelly sensor dump          the thermometer, while it is awake
  upsmon shelly listen               show pushes as they arrive
  upsmon shelly serve                accept an outbound websocket
  upsmon shelly discover             find Shelly devices on the LAN
  upsmon shelly --help               all of it, with the options
  upsmon --reset-data                erase all history and events
  upsmon --reset-data events         erase only the event log

Configuration lives in /etc/upsmon/config.json; every key can also be given as
an environment variable with a UPSMON_ prefix (UPSMON_NUT_HOST=10.0.0.5).
""")
    parser.add_argument('--config', metavar='FILE',
                        help='configuration file (default %s)' % CONFIG_FILE)
    parser.add_argument('--host', metavar='HOST',
                        help='NAS or NUT server address, overriding the config')
    parser.add_argument('--port', type=int, help='upsd port (default 3493)')
    parser.add_argument('--ups', metavar='NAME', help='which UPS to use')

    actions = parser.add_argument_group('actions')
    actions.add_argument('--status', action='store_true',
                         help='print a full report and exit')
    actions.add_argument('--oneshot', action='store_true',
                         help='collect one sample into the database and exit')
    actions.add_argument('--list-rw', action='store_true',
                         help='list writable variables and instant commands')
    actions.add_argument('--set', action='append', default=[], dest='set_vars',
                         metavar='VAR=VALUE', help='change a writable variable')
    actions.add_argument('--exec', action='append', default=[], dest='exec_cmds',
                         metavar='COMMAND', help='run an instant command')
    actions.add_argument('--check', action='store_true',
                         help='health check of daemon, upsd and database')
    actions.add_argument('--diag', action='store_true',
                         help='configuration, API latency and database size')
    actions.add_argument('--history', metavar='RANGE',
                         help='print recorded samples (24h, 7d, 1y...)')
    actions.add_argument('--events', action='store_true',
                         help='print the event log')
    actions.add_argument('--tests', action='store_true',
                         help='print the self-test history')
    actions.add_argument('--plug', action='store_true',
                         help='print everything the smart plug reports')
    actions.add_argument('--plug-on', action='store_true',
                         help='switch the socket on')
    actions.add_argument('--plug-off', action='store_true',
                         help='switch the socket off')
    actions.add_argument('--plug-reset', nargs='?', const='all', metavar='COUNTER',
                         help='reset a plug counter: all (the default), aenergy, '
                              'ret_aenergy, on_time, switch_on or on_above_thr')
    actions.add_argument('--sensors', action='store_true',
                         help='print the latest pushed sensor readings')
    actions.add_argument('--aggregate', action='store_true',
                         help='roll old samples into hourly averages now')
    actions.add_argument('--reset-data', nargs='?', const='all', metavar='WHAT',
                         choices=['all', 'history', 'events', 'outages'],
                         help='delete recorded data and start again. WHAT is '
                              'all (the default), history, events or outages. '
                              'Asks for confirmation unless --yes is given')

    output = parser.add_argument_group('output')
    output.add_argument('--yes', action='store_true',
                        help='answer yes to confirmations, for scripts')
    output.add_argument('--json', action='store_true', help='machine-readable output')
    output.add_argument('--points', type=int, default=200,
                        help='maximum rows for --history (default 200)')
    output.add_argument('--limit', type=int, default=50,
                        help='how many events to show (default 50)')
    output.add_argument('--log-file', metavar='FILE',
                        help='write the log here instead of the configured path; '
                             'an empty string disables the file')
    output.add_argument('--no-color', action='store_true', help='no colour codes')
    output.add_argument('-v', '--verbose', action='store_true', help='debug logging')
    output.add_argument('--version', action='version',
                        version='upsmon ' + __version__)
    return parser


def main(argv=None):
    global DEBUG, CONFIG_FILE
    arguments = list(sys.argv[1:] if argv is None else argv)

    # "upsmon shelly ..." is a world of its own, with its own options. Routing
    # it before the main parser keeps both readable.
    if arguments and arguments[0] == 'shelly':
        for index, value in enumerate(arguments):
            if value == '--config' and index + 1 < len(arguments):
                CONFIG_FILE = Path(arguments[index + 1])
        load_config()
        for problem in CONFIG_FATAL:
            print('CONFIG: %s' % problem, file=sys.stderr)
        pal = Palette(colour_enabled(False if '--no-color' in arguments else None))
        return shelly_main([a for a in arguments[1:]
                            if a != '--config' and not a.endswith('config.json')], pal)

    parser = build_parser()
    args = parser.parse_args(arguments)

    if args.config:
        CONFIG_FILE = Path(args.config)
    load_config()
    if args.host:
        CONFIG['nut_host'] = args.host
    if args.port:
        CONFIG['nut_port'] = args.port
    if args.ups:
        CONFIG['ups_name'] = args.ups
    DEBUG = args.verbose
    if args.log_file is not None:
        CONFIG['log_file'] = args.log_file

    pal = Palette(colour_enabled(False if args.no_color else None))
    for problem in CONFIG_FATAL:
        print(pal.bad('CONFIG: ') + problem, file=sys.stderr)

    try:
        if args.status:
            return cmd_status(args, pal)
        if args.list_rw:
            return cmd_list_rw(args, pal)
        if args.set_vars or args.exec_cmds:
            return cmd_control(args, pal)
        if args.oneshot:
            return cmd_oneshot(args, pal)
        if args.check:
            return cmd_check(args, pal)
        if args.diag:
            return cmd_diag(args, pal)
        if args.history:
            return cmd_history(args, pal)
        if args.events:
            return cmd_events(args, pal)
        if args.tests:
            return cmd_tests(args, pal)
        if args.plug or args.plug_on or args.plug_off or args.plug_reset:
            return cmd_plug(args, pal)
        if args.sensors:
            return cmd_sensors(args, pal)
        if args.reset_data:
            return cmd_reset(args, pal)
        if args.aggregate:
            rolled = Storage().aggregate()
            print('rolled %d hours into the summary table' % rolled)
            return 0

        if CONFIG_FATAL:
            for problem in CONFIG_FATAL:
                log('config: %s' % problem, level='error')
            log('refusing to start on defaults - that would monitor the wrong '
                'host and hide the real problem', level='error')
            return 2
        try:
            Daemon().run()
        except StorageError as e:
            log('cannot use the database: %s' % e, level='error')
            return 2
        return 0
    except StorageError as e:
        print(pal.bad('DATABASE: ') + str(e), file=sys.stderr)
        return 2
    except NUTError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\nstopped.')
        return 0
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == '__main__':
    sys.exit(main())
