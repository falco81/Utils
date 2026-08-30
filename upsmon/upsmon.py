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

    upsmon.py --status            one-off report, straight from the UPS
    upsmon.py --oneshot           one collection into the database, then exit
    upsmon.py --list-rw           what this UPS lets you change
    upsmon.py --set VAR=VALUE     change a writable variable
    upsmon.py --exec COMMAND      run an instant command
    upsmon.py --check             health check against the running daemon
    upsmon.py --diag              API latency, database size, config in force
    upsmon.py --history 24h       recent samples as a table
    upsmon.py                     run the daemon in the foreground
"""

# Bumped by hand with every change to this file.
__version__ = '2.6.1'

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
        except sqlite3.OperationalError as e:
            raise StorageError(self._explain(e))

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

        if points and len(rows) > points:
            step = len(rows) / float(points)
            picked, i = [], 0.0
            while int(i) < len(rows):
                picked.append(rows[int(i)])
                i += step
            if picked[-1] is not rows[-1]:
                picked.append(rows[-1])
            rows = picked
        return rows

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
                         'outages', 'tests'):
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
                'tests'],
        'history': ['samples', 'samples_hourly', 'snapshots'],
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

        self.poll()
        while True:
            start = time.time()
            try:
                ensure_log_current()
                self.poll()
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

            def do_GET(self):
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

            def do_POST(self):
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog='upsmon.py',
        description='UPS monitoring daemon and command line tool (upsmon %s).'
                    % __version__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Run with no arguments to start the daemon in the foreground - that is what the
systemd unit does. Everything else below is for testing and day-to-day use.

  upsmon.py --status                    full report straight from the UPS
  upsmon.py --status --json             the same as JSON
  upsmon.py --oneshot                   one collection into the database
  upsmon.py --list-rw                   what this UPS lets you change
  upsmon.py --set battery.charge.low=20
  upsmon.py --exec test.battery.start.quick
  upsmon.py --check                     is the whole system healthy
  upsmon.py --diag                      settings, API latency, database size
  upsmon.py --history 7d                recent samples as a table
  upsmon.py --events                    what the daemon has logged
  upsmon.py --tests                     the self-test history
  upsmon.py --reset-data                erase all history and events
  upsmon.py --reset-data events         erase only the event log

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
                        version='upsmon.py ' + __version__)
    return parser


def main(argv=None):
    global DEBUG, CONFIG_FILE
    parser = build_parser()
    args = parser.parse_args(argv)

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
