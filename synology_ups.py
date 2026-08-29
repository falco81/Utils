#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
synology_ups.py - read and display ALL information offered by a UPS attached to
a Synology NAS over USB.

The script talks directly to the network UPS server (NUT / upsd) that DSM runs
on TCP port 3493 - the same interface DSM itself uses.

Requirements:
    Python 3.7+
    colorama   (optional, recommended on Windows:  pip install colorama)

Enable this in DSM first:
    Control Panel -> Hardware & Power -> UPS
      [x] Enable UPS support
      [x] Enable network UPS server
      -> Permitted DiskStation Devices: add the IP of this computer

Usage (read only):
    python synology_ups.py 192.168.1.10
    python synology_ups.py 192.168.1.10 --watch 2
    python synology_ups.py 192.168.1.10 --json
    python synology_ups.py 192.168.1.10 --desc --commands

Usage (write / control - needs a upsd user with SET and instcmds rights,
see the notes at the bottom of this file):
    python synology_ups.py 192.168.1.10 --list-rw
    python synology_ups.py 192.168.1.10 --username admin --password pw \\
        --set battery.charge.low=30
    python synology_ups.py 192.168.1.10 --username admin --password pw \\
        --exec beeper.disable
"""

import argparse
import getpass
import json
import os
import socket
import sys
import time
from datetime import datetime

__version__ = '1.0.0'
DEFAULT_PORT = 3493

# ---------------------------------------------------------------------------
# Colour support (colorama on Windows, graceful fallback everywhere)
# ---------------------------------------------------------------------------
COLORAMA_AVAILABLE = True
try:
    import colorama
    from colorama import Fore, Style
    # strip/convert is auto-detected; on Windows this makes ANSI codes work
    colorama.init(autoreset=False)
except ImportError:  # pragma: no cover
    COLORAMA_AVAILABLE = False

    class _Dummy(object):
        def __getattr__(self, _):
            return ''

    Fore = _Dummy()
    Style = _Dummy()


class Palette(object):
    """Named colours, switchable off in one place."""

    def __init__(self, enabled=True):
        self.enabled = enabled

    def _c(self, code, text):
        if not self.enabled:
            return text
        return code + text + Style.RESET_ALL

    def title(self, t):  return self._c(Style.BRIGHT + Fore.CYAN, t)
    def rule(self, t):   return self._c(Fore.CYAN, t)
    def group(self, t):  return self._c(Style.BRIGHT + Fore.MAGENTA, t)
    def key(self, t):    return self._c(Fore.WHITE, t)
    def value(self, t):  return self._c(Style.BRIGHT + Fore.YELLOW, t)
    def note(self, t):   return self._c(Style.DIM + Fore.WHITE, t)
    def ok(self, t):     return self._c(Style.BRIGHT + Fore.GREEN, t)
    def warn(self, t):   return self._c(Style.BRIGHT + Fore.YELLOW, t)
    def bad(self, t):    return self._c(Style.BRIGHT + Fore.RED, t)
    def info(self, t):   return self._c(Fore.BLUE, t)
    def flag(self, t):   return self._c(Fore.GREEN, t)


def supports_colour(force=None):
    if force is False:
        return False
    if force is True:
        return True
    if os.environ.get('NO_COLOR') is not None:
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == 'nt' and not COLORAMA_AVAILABLE:
        # An old console without colorama would print raw escape codes.
        return False
    return True


def clear_screen():
    if os.name == 'nt':
        os.system('cls')
    else:
        sys.stdout.write('\033[2J\033[H')


class NUTError(Exception):
    pass


# ---------------------------------------------------------------------------
# NUT protocol
# ---------------------------------------------------------------------------
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

    def __init__(self, host, port=DEFAULT_PORT, timeout=5.0):
        self.host, self.port, self.timeout = host, port, timeout
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
            raise NUTError(
                "Cannot connect to %s:%d (%s).\n"
                "  * Make sure 'Enable network UPS server' is switched on in DSM\n"
                "    (Control Panel -> Hardware & Power -> UPS).\n"
                "  * Make sure this computer's IP is in the permitted devices list.\n"
                "  * Check the DSM firewall for TCP port %d."
                % (self.host, self.port, e, self.port))
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

    # ---- low level --------------------------------------------------------
    def _send(self, cmd):
        self.f.write((cmd + '\n').encode('utf-8'))
        self.f.flush()

    def _readline(self):
        try:
            raw = self.f.readline()
        except socket.timeout:
            raise NUTError('timed out waiting for a reply from the server')
        if not raw:
            raise NUTError('connection closed by the server')
        return raw.decode('utf-8', 'replace').rstrip('\r\n')

    def command(self, cmd):
        self._send(cmd)
        line = self._readline()
        if line.startswith('ERR '):
            raise NUTError(line[4:])
        return line

    def list_query(self, subcmd):
        """LIST <subcmd> -> list of tokenised rows."""
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

    # ---- high level -------------------------------------------------------
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

    def get_desc(self, ups, var):
        try:
            return tokenize(self.command('GET DESC %s %s' % (ups, var)))[-1]
        except NUTError:
            return ''

    def get_cmddesc(self, ups, cmd):
        try:
            return tokenize(self.command('GET CMDDESC %s %s' % (ups, cmd)))[-1]
        except NUTError:
            return ''

    def get_type(self, ups, var):
        try:
            return ' '.join(tokenize(self.command('GET TYPE %s %s' % (ups, var)))[3:])
        except NUTError:
            return ''

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

    # ---- write operations (need actions=SET / instcmds in upsd.users) ------
    def set_var(self, ups, var, value):
        return self.command('SET VAR %s %s "%s"' % (ups, var, value.replace('"', '\\"')))

    def instcmd(self, ups, cmd):
        return self.command('INSTCMD %s %s' % (ups, cmd))


# ---------------------------------------------------------------------------
# Descriptions and formatting
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

BAD_FLAGS = {'OB', 'LB', 'RB', 'OVER', 'FSD', 'ALARM', 'OFF'}
WARN_FLAGS = {'DISCHRG', 'BYPASS', 'CAL', 'TRIM', 'BOOST', 'CHRG'}

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
    'battery.mfr.date': 'Battery manufacturing date',
    'battery.temperature': 'Battery temperature (deg C)',
    'input.voltage': 'Input voltage (V)',
    'input.voltage.nominal': 'Nominal input voltage (V)',
    'input.frequency': 'Input frequency (Hz)',
    'input.transfer.high': 'High transfer voltage point (V)',
    'input.transfer.low': 'Low transfer voltage point (V)',
    'input.sensitivity': 'Input sensitivity',
    'output.voltage': 'Output voltage (V)',
    'output.voltage.nominal': 'Nominal output voltage (V)',
    'output.frequency': 'Output frequency (Hz)',
    'output.current': 'Output current (A)',
    'ups.status': 'UPS status flags',
    'ups.load': 'UPS load (percent)',
    'ups.mfr': 'UPS manufacturer',
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
    'ups.productid': 'USB product ID',
    'ups.vendorid': 'USB vendor ID',
    'ups.mfr.date': 'Date the UPS was manufactured',
    'ups.timer.reboot': 'Countdown to a pending reboot (s); 0 or -1 means none',
    'battery.mfr.date': 'Battery date - on APC units this is the date the battery '
                        'was last declared new, so set it when you replace one',
    'battery.voltage.low': 'Battery voltage treated as empty (V)',
    'battery.voltage.high': 'Battery voltage treated as full (V)',
    'battery.protection': 'Deep discharge protection',
    'battery.energysave': 'Energy saving mode',
    'ups.start.battery': 'Allow cold start from battery with no mains present',
    'output.frequency.nominal': 'Nominal output frequency (Hz)',
    'outlet.desc': 'Outlet description (label only, held by the driver)',
    'outlet.id': 'Outlet number',
    'outlet.switchable': 'Whether this outlet can be switched separately',
    'outlet.1.status': 'Outlet 1 state',
    'driver.parameter.pollfreq': 'Full poll interval (seconds)',
    'driver.parameter.synchronous': 'Driver synchronous write mode',
    'driver.version.usb': 'USB backend used by the driver',
    'driver.name': 'Driver name',
    'driver.version': 'Driver version',
    'driver.version.internal': 'Internal driver version',
    'driver.version.data': 'Driver data mapping version',
    'driver.parameter.port': 'Driver port',
    'driver.parameter.pollinterval': 'Poll interval (seconds)',
}

# Word fragments used to build a description for any variable that is not in
# the table above. NUT names are structured, so an unknown name can still be
# explained: input.L2-N.voltage.maximum, outlet.3.current, battery.cell.5.voltage
# and anything else a vendor invents all resolve to something readable.
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
    'colour': 'colour', 'color': 'colour', 'quality': 'quality',
}

QUALIFIER_WORDS = {
    'nominal': 'nominal', 'low': 'low threshold', 'high': 'high threshold',
    'warning': 'warning threshold', 'critical': 'critical threshold',
    'minimum': 'minimum recorded', 'maximum': 'maximum recorded',
    'min': 'minimum recorded', 'max': 'maximum recorded',
    'start': 'on start', 'shutdown': 'on shutdown', 'reboot': 'on reboot',
    'restart': 'on restart', 'stop': 'on stop', 'return': 'on return',
    'aux': 'auxiliary', 'internal': 'internal', 'external': 'external',
    'total': 'total', 'realpower': 'real power', 'approx': 'approximate',
    'days': 'in days', 'seconds': 'in seconds', 'percent': 'as a percentage',
}


def describe_variable(name):
    """Best-effort description for a variable that has no explicit entry."""
    parts = name.split('.')
    subject = SUBJECT_WORDS.get(parts[0], parts[0])

    # A numeric segment is an index: outlet.2.x, battery.cell.3.x, input.L1-N.x
    index = None
    tail = []
    for piece in parts[1:]:
        if piece.isdigit() or (piece.upper().startswith('L') and any(
                ch.isdigit() for ch in piece)):
            index = piece
            continue
        tail.append(piece)

    measure = None
    qualifiers = []
    for piece in tail:
        if piece in MEASURE_WORDS and measure is None:
            measure = MEASURE_WORDS[piece]
        elif piece in QUALIFIER_WORDS:
            qualifiers.append(QUALIFIER_WORDS[piece])
        else:
            qualifiers.append(piece.replace('-', ' '))

    # "mfr" followed by "date" is a manufacturing date, not a manufacturer.
    if measure == 'manufacturer' and 'date' in tail:
        measure = 'manufacturing date'
        qualifiers = [q for q in qualifiers if q != 'date']
    elif measure == 'manufacturer' and 'time' in tail:
        measure = 'manufacturing time'
        qualifiers = [q for q in qualifiers if q != 'time']

    if measure is None and not qualifiers:
        return ''
    text = '%s %s' % (subject, measure or ' '.join(qualifiers))
    if measure and qualifiers:
        text += ', ' + ' '.join(qualifiers)
    if index:
        text += ' [%s]' % index
    return text[0].upper() + text[1:]


def variable_description(name, server_descriptions=None):
    """Explicit table, then the server, then a description built from the name."""
    if name in DESCRIPTIONS:
        return DESCRIPTIONS[name]
    if server_descriptions:
        fetched = server_descriptions.get(name, '')
        if fetched and fetched.lower() != 'description unavailable':
            return fetched
    return describe_variable(name)



GROUP_ORDER = ['device', 'ups', 'battery', 'input', 'output',
               'ambient', 'outlet', 'driver', 'server']
GROUP_TITLE = {
    'device': 'DEVICE',
    'ups': 'UPS',
    'battery': 'BATTERY',
    'input': 'INPUT (mains)',
    'output': 'OUTPUT (load)',
    'ambient': 'AMBIENT',
    'outlet': 'OUTLETS',
    'driver': 'DRIVER',
    'server': 'SERVER',
    'other': 'OTHER',
}


def human_runtime(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return None
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return '%dh %02dm %02ds' % (h, m, sec)
    if m:
        return '%dm %02ds' % (m, sec)
    return '%ds' % sec


def describe_status(value):
    return ', '.join(STATUS_TEXT.get(f, f) for f in value.split())


def colour_status(pal, value):
    flags = value.split()
    if any(f in BAD_FLAGS for f in flags):
        return pal.bad(value)
    if any(f in WARN_FLAGS for f in flags):
        return pal.warn(value)
    return pal.ok(value)


def colour_percent(pal, value, low, mid, invert=False):
    """low/mid are thresholds; invert=True means high numbers are the bad ones."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return pal.value(value)
    if invert:
        if v >= low:
            return pal.bad(value)
        if v >= mid:
            return pal.warn(value)
        return pal.ok(value)
    if v <= low:
        return pal.bad(value)
    if v <= mid:
        return pal.warn(value)
    return pal.ok(value)


# Instant commands that cut power or shut the UPS down. Running these while the
# NAS is plugged into the UPS will kill the NAS without a clean shutdown.
DANGEROUS_COMMANDS = {
    'load.off', 'load.off.delay', 'load.cycle',
    'shutdown.return', 'shutdown.stayoff', 'shutdown.reboot',
    'shutdown.reboot.graceful', 'bypass.start',
    'driver.killpower', 'test.failure.start', 'calibrate.start',
}

# Descriptions for every instant command defined by NUT, so nothing in the
# listing is left unexplained.
COMMAND_HELP = {
    # --- load switching ---
    'load.off': 'Switch the outlets OFF immediately - cuts power to everything',
    'load.on': 'Switch the outlets on immediately',
    'load.off.delay': 'Switch the outlets off after ups.delay.shutdown seconds',
    'load.on.delay': 'Switch the outlets on after ups.delay.start seconds',
    'load.cycle': 'Switch the outlets off and back on (power cycle the load)',

    # --- shutdown sequence ---
    'shutdown.return': 'Switch the load off, switch it back on when mains returns',
    'shutdown.stayoff': 'Switch the load off and keep it off',
    'shutdown.stop': 'Cancel a shutdown that is already counting down',
    'shutdown.reboot': 'Briefly drop the load while the UPS restarts',
    'shutdown.reboot.graceful': 'Same as shutdown.reboot, but after a delay',

    # --- self tests ---
    'test.battery.start': 'Start a battery self test of unspecified length',
    'test.battery.start.quick': 'Start a quick battery self test',
    'test.battery.start.deep': 'Start a deep battery test (discharges the battery)',
    'test.battery.stop': 'Abort a running battery test',
    'test.panel.start': 'Start a front panel / indicator test',
    'test.panel.stop': 'Stop the front panel test',
    'test.failure.start': 'Simulate a mains failure - the load runs on battery',
    'test.failure.stop': 'Stop simulating a mains failure',
    'test.system.start': 'Start a general system test',

    # --- calibration ---
    'calibrate.start': 'Start runtime calibration - fully discharges the battery',
    'calibrate.stop': 'Abort runtime calibration',

    # --- beeper ---
    'beeper.enable': 'Enable the audible alarm',
    'beeper.disable': 'Disable the audible alarm permanently',
    'beeper.mute': 'Silence the alarm until the next event',
    'beeper.toggle': 'Toggle the audible alarm on or off',
    'beeper.on': 'Enable the alarm (deprecated alias for beeper.enable)',
    'beeper.off': 'Disable the alarm (deprecated alias for beeper.disable)',

    # --- bypass ---
    'bypass.start': 'Switch to bypass - the load runs unprotected on raw mains',
    'bypass.stop': 'Leave bypass mode and protect the load again',

    # --- misc ---
    'reset.input.minmax': 'Clear the recorded minimum and maximum input voltage',
    'reset.watchdog': 'Reset the watchdog timer so the UPS does not reboot the load',
    'driver.killpower': 'Tell the driver to run its shutdown sequence right now',
    'driver.reload': 'Reload the driver configuration',
    'driver.reload-or-error': 'Reload the driver configuration, report if impossible',
    'driver.reload-or-exit': 'Reload the driver configuration, or exit the driver',
    'driver.exit': 'Stop the driver process',
}

# Same commands addressed to a single switchable outlet: outlet.<n>.<action>
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


def _split_outlet(cmd):
    """'outlet.1.load.off' -> ('1', 'load.off'); anything else -> (None, None)."""
    parts = cmd.split('.')
    if len(parts) >= 3 and parts[0] == 'outlet':
        return parts[1], '.'.join(parts[2:])
    return None, None


def is_dangerous(cmd):
    if cmd in DANGEROUS_COMMANDS:
        return True
    outlet, action = _split_outlet(cmd)
    return outlet is not None and action in OUTLET_DANGEROUS


def command_help(cmd, server_desc=''):
    """Built-in description first, then whatever the server offers."""
    if cmd in COMMAND_HELP:
        return COMMAND_HELP[cmd]
    outlet, action = _split_outlet(cmd)
    if outlet and action in OUTLET_COMMAND_HELP:
        return OUTLET_COMMAND_HELP[action].replace('{n}', outlet)
    if server_desc and server_desc.lower() != 'description unavailable':
        return server_desc
    return 'Vendor-specific command - see the manual for this UPS'


# ---------------------------------------------------------------------------
# Battery age, derived only from what the UPS itself reports
# ---------------------------------------------------------------------------
def parse_date(text):
    """Accept the date formats different UPS vendors use in battery.date."""
    text = (text or '').strip()
    if not text or text.lower() in ('not set', 'unknown', 'n/a', '00/00/00'):
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d',
                '%m/%d/%y', '%m/%d/%Y', '%d/%m/%Y',
                '%d.%m.%Y', '%d-%m-%Y', '%b %d %Y', '%Y-%m'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_age(days):
    years, rest = divmod(max(days, 0), 365)
    months = int(rest // 30.44)
    if years and months:
        return '%dy %dm' % (years, months)
    if years:
        return '%dy' % years
    return '%dm' % months if months else '%dd' % days


def battery_age_text(pal, raw, life_years):
    """Colour the reported battery date by how old it is."""
    when = parse_date(raw)
    if when is None:
        return pal.value(raw)
    days = (datetime.now().date() - when).days
    age = format_age(days)
    remaining = life_years - days / 365.25
    if remaining <= 0:
        return pal.bad('%s  (%s old - past a %.0f year service life)'
                       % (raw, age, life_years))
    if remaining <= 0.5:
        return pal.warn('%s  (%s old - replacement due soon)' % (raw, age))
    return pal.ok('%s  (%s old)' % (raw, age))


def console_width(default=100):
    try:
        import shutil
        return shutil.get_terminal_size((default, 25)).columns
    except Exception:
        return default


def print_command_rows(pal, rows):
    """Print (command, dangerous, description) with the markers in one column."""
    if not rows:
        return
    name_width = max(len(name) for name, _, _ in rows)
    text_width = max(len(text) for _, _, text in rows)
    any_danger = any(danger for _, danger, _ in rows)
    marker = '[DANGEROUS]'
    trailing_width = 2 + name_width + 3 + text_width + 2 + len(marker)
    trailing = trailing_width <= console_width()

    for name, danger, text in rows:
        label = (pal.bad if danger else pal.info)(name.ljust(name_width))
        if not any_danger:
            print('  %s   %s' % (label, pal.note(text)))
        elif trailing:
            # Markers line up in a column past the longest description.
            tail = pal.bad('  ' + marker) if danger else ''
            body = pal.note(text.ljust(text_width)) if danger else pal.note(text)
            print('  %s   %s%s' % (label, body, tail))
        else:
            # Narrow console: the marker goes between name and description.
            flag = pal.bad(marker) if danger else ' ' * len(marker)
            print('  %s  %s  %s' % (label, flag, pal.note(text)))


def banner(pal, host=None, ups=None, server=None):
    """One identifying line printed at the top of every command's output."""
    bits = ['synology_ups.py v' + __version__]
    if host:
        bits.append('NAS: ' + host)
    if ups:
        bits.append('UPS: ' + ups)
    if server:
        bits.append('upsd: ' + server)
    bits.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return pal.note('   |   '.join(bits))


def print_writable(pal, client, ups, host=None):
    """Show every writable variable together with its accepted values."""
    rw = client.list_rw(ups)
    bar = '=' * 78
    print(pal.rule(bar))
    print('  ' + pal.title('Writable variables on "%s"' % ups))
    print('  ' + banner(pal, host, ups))
    print(pal.rule(bar))
    if not rw:
        print('  ' + pal.warn('This UPS/driver exposes no writable variables.'))
        print('  ' + pal.note('Cheaper USB models very often expose none at all.'))
    for var in sorted(rw):
        vtype = client.get_type(ups, var)
        print('\n  %s = %s   %s' % (pal.key(var), pal.value(rw[var]), pal.note(vtype)))
        note = variable_description(var, {var: client.get_desc(ups, var)})
        if note:
            print('    ' + pal.note(note))
        enum = client.list_enum(ups, var)
        if enum:
            print('    ' + pal.info('allowed: ' + ', '.join(enum)))
        for lo, hi in client.list_range(ups, var):
            print('    ' + pal.info('range: %s .. %s' % (lo, hi)))

    cmds = client.list_cmds(ups)
    print('\n' + pal.group('-- INSTANT COMMANDS ') + pal.rule('-' * 57))
    if not cmds:
        print('  ' + pal.warn('This UPS/driver exposes no instant commands.'))
    rows = [(c, is_dangerous(c), command_help(c, client.get_cmddesc(ups, c)))
            for c in sorted(cmds)]
    print_command_rows(pal, rows)
    print('\n' + pal.rule(bar))
    print('  ' + pal.note('Use --set VAR=VALUE to change a variable, '
                          '--exec COMMAND to run a command.'))
    print(pal.rule(bar))



def print_report(pal, entry, host, life_years=4.0):
    ups = entry['ups']
    variables = entry['vars']
    rw = entry['rw']
    cmds = entry['commands']
    descs = entry['descriptions']

    bar = '=' * 78
    print(pal.rule(bar))
    entry_desc = entry['description']
    if entry_desc.lower() in ('description unavailable', 'unavailable'):
        entry_desc = ''
    header = 'UPS "%s"%s' % (ups, ('  -  ' + entry_desc) if entry_desc else '')
    print('  ' + pal.title(header))
    print('  ' + banner(pal, host, server=entry['server']))
    print(pal.rule(bar))

    # ---- summary ----
    status = variables.get('ups.status')
    if status:
        print('  %-12s: %s  %s' % ('Status', colour_status(pal, status),
                                   pal.note('(' + describe_status(status) + ')')))
    if 'battery.charge' in variables:
        print('  %-12s: %s %%' % ('Battery',
                                  colour_percent(pal, variables['battery.charge'], 20, 50)))
    if 'battery.runtime' in variables:
        rt = human_runtime(variables['battery.runtime']) or variables['battery.runtime']
        try:
            secs = float(variables['battery.runtime'])
        except ValueError:
            secs = None
        shown = pal.bad(rt) if (secs is not None and secs < 300) else pal.ok(rt)
        print('  %-12s: %s' % ('Runtime', shown))
    if 'ups.load' in variables:
        print('  %-12s: %s %%' % ('Load',
                                  colour_percent(pal, variables['ups.load'], 90, 70, invert=True)))
    if 'input.voltage' in variables:
        print('  %-12s: %s V' % ('Input', pal.value(variables['input.voltage'])))
    if 'output.voltage' in variables:
        print('  %-12s: %s V' % ('Output', pal.value(variables['output.voltage'])))
    for key in ('battery.date', 'battery.mfr.date'):
        if key in variables:
            label = 'Batt. date' if key == 'battery.date' else 'Batt. made'
            print('  %-12s: %s' % (label,
                                   battery_age_text(pal, variables[key], life_years)))
    print(pal.rule(bar))

    # ---- every variable, grouped ----
    groups = {}
    for k in variables:
        g = k.split('.')[0]
        groups.setdefault(g if g in GROUP_ORDER else 'other', []).append(k)

    ordered = [g for g in GROUP_ORDER if g in groups] + \
              [g for g in sorted(groups) if g not in GROUP_ORDER]

    width = max([len(k) for k in variables] + [10])
    for g in ordered:
        title = GROUP_TITLE.get(g, g.upper())
        print('\n' + pal.group('-- ' + title + ' ') + pal.rule('-' * max(1, 74 - len(title))))
        for key in sorted(groups[g]):
            raw = variables[key]
            extra = ''
            if key == 'ups.status':
                shown = colour_status(pal, raw)
                extra = '  ' + pal.note('(' + describe_status(raw) + ')')
            elif key.endswith('runtime'):
                shown = pal.value(raw)
                hr = human_runtime(raw)
                extra = ('  ' + pal.note('(' + hr + ')')) if hr else ''
            elif key == 'battery.charge':
                shown = colour_percent(pal, raw, 20, 50)
            elif key == 'ups.load':
                shown = colour_percent(pal, raw, 90, 70, invert=True)
            else:
                shown = pal.value(raw)
            flag = ('  ' + pal.flag('[rw]')) if key in rw else ''
            print('  %s : %s%s%s' % (pal.key(key.ljust(width)), shown, extra, flag))
            note = variable_description(key, descs)
            if note:
                print('  %s   %s' % (' ' * width, pal.note(note)))

    if cmds:
        print('\n' + pal.group('-- SUPPORTED COMMANDS ') + pal.rule('-' * 55))
        cmd_desc = entry.get('command_descriptions', {})
        print_command_rows(pal, [(c, is_dangerous(c), command_help(c, cmd_desc.get(c, '')))
                                 for c in sorted(cmds)])

    print('\n' + pal.rule(bar))
    if entry.get('commands_queried'):
        cmd_text = 'commands: %d' % len(cmds)
    else:
        cmd_text = 'commands: not listed, add --commands'
    print('  ' + pal.note('Variables: %d   |   writable: %d   |   %s'
                          % (len(variables), len(rw), cmd_text)))
    print(pal.rule(bar))


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def collect(args):
    with NUTClient(args.host, args.port, args.timeout) as c:
        if args.username:
            c.login(args.username, args.password or '')

        server_ver = c.server_version()
        available = c.list_ups()
        if not available:
            raise NUTError('the server reports no UPS '
                           '(is the UPS attached over USB and recognised by DSM?)')

        if args.ups:
            selected = [(n, d) for n, d in available if n == args.ups]
            if not selected:
                raise NUTError('UPS "%s" not found. Available: %s'
                               % (args.ups, ', '.join(n for n, _ in available)))
        else:
            selected = available

        result = []
        for name, desc in selected:
            variables = c.list_vars(name)
            rw = c.list_rw(name)
            cmds = c.list_cmds(name) if args.commands else []
            command_descriptions = {}
            for cmd in cmds:
                d = c.get_cmddesc(name, cmd)
                if d and d.lower() != 'description unavailable':
                    command_descriptions[cmd] = d
            descriptions = {}
            if args.desc:
                for k in variables:
                    d = c.get_desc(name, k)
                    if d and d.lower() != 'description unavailable':
                        descriptions[k] = d
            result.append({
                'ups': name,
                'description': desc,
                'server': server_ver,
                'vars': variables,
                'rw': rw,
                'commands': cmds,
                'commands_queried': bool(args.commands),
                'command_descriptions': command_descriptions,
                'descriptions': descriptions,
            })
        return result


def print_full_help(pal):
    """Detailed manual shown by -h / --help."""
    import textwrap

    def section(title):
        print('\n' + pal.group(title) + ' ' + pal.rule('-' * max(1, 76 - len(title))))

    def opt(names, text, indent=4):
        print('  ' + pal.key(names))
        for line in textwrap.wrap(text, width=76 - indent):
            print(' ' * indent + pal.note(line))

    def para(text, indent=2):
        for line in textwrap.wrap(text, width=78 - indent):
            print(' ' * indent + line)

    print(pal.rule('=' * 78))
    print('  ' + pal.title('synology_ups.py - read and control a UPS attached to '
                           'a Synology NAS'))
    print('  ' + pal.note('version ' + __version__))
    print(pal.rule('=' * 78))

    section('USAGE')
    print('  synology_ups.py HOST [options]')
    print('  ' + pal.note('HOST is the IP address or hostname of the NAS, e.g. 192.168.1.10'))

    section('WHAT IT DOES')
    para('DSM runs a Network UPS Tools server (upsd) on TCP port 3493 and speaks to '
         'the UPS over USB on your behalf. This script is a client for that server. '
         'It reads every variable the UPS driver publishes, and - with an account '
         'that has the right permissions - can change writable settings and run '
         'instant commands. Nothing is stored locally; everything shown comes from '
         'the NAS at the moment you ask.')

    section('BEFORE YOU START')
    para('In DSM open Control Panel -> Hardware & Power -> UPS, tick "Enable UPS '
         'support" and "Enable network UPS server", then add the IP address of this '
         'computer to the permitted devices list. Reading works immediately after '
         'that. Writing needs an extra step, described under WRITE ACCESS below.')

    section('CONNECTION')
    opt('-p, --port PORT',
        'Port upsd listens on. Only change this if you are talking to something '
        'other than a stock DSM, which always uses 3493.')
    opt('-u, --ups NAME',
        'Which UPS to query when the server has more than one. Synology always '
        'names its own "ups". Without this the script reports every UPS it finds.')
    opt('-t, --timeout SECONDS',
        'How long to wait for the NAS to answer before giving up. Raise it on a '
        'slow or busy network; the default is 5 seconds.')
    opt('--username NAME',
        'upsd account. Not needed for reading. Required for --set and --exec; if '
        'you leave it out there, the script asks for it.')
    opt('--password PASSWORD',
        'Password for that account. Prefer leaving this out and letting the script '
        'prompt, so the password does not end up in your shell history.')

    section('READING')
    para('With no options at all the script prints a full report: a summary block '
         '(status, charge, runtime, load, voltages), then every variable grouped by '
         'subject, each with a description and an [rw] marker if it can be changed.')
    opt('--desc',
        'Ask the server for a description of each variable. DSM ships without the '
        'description database, so this usually returns nothing useful and costs one '
        'extra round trip per variable. The built-in descriptions are better.')
    opt('--commands',
        'Also list the instant commands this UPS accepts, each with an explanation '
        'and a warning marker on the ones that cut power.')
    opt('--json',
        'Print the raw data as JSON instead of a report. Use this for logging, '
        'graphing, or feeding another tool.')
    opt('-w, --watch SECONDS',
        'Refresh the report on a timer until you press Ctrl+C. Good for watching '
        'what happens during a power cut or a load change.')
    opt('--battery-life YEARS',
        'Service life used to colour a battery date, when the UPS reports one. '
        'Green while healthy, amber in the last six months, red past the end. '
        'Default is 4 years, which suits sealed lead-acid.')

    section('WRITING AND CONTROL')
    opt('--list-rw',
        'Show what this particular UPS lets you change: every writable variable '
        'with its current value, type, and the exact values it accepts, followed by '
        'the instant commands it supports. Always start here - the list is different '
        'on every model, and cheaper units allow nothing at all.')
    opt('--set VAR=VALUE',
        'Change a writable variable. The value is checked against the allowed range '
        'or list before it is sent, and the variable is read back afterwards so you '
        'can see what the UPS actually accepted. Repeat the option to set several.')
    opt('--exec COMMAND',
        'Run an instant command, for example beeper.disable or '
        'test.battery.start.quick. Repeat the option to run several in order.')
    para('When the command starts something that takes time - any battery test, a '
         'panel test, a simulated failure, or a calibration - the script stays '
         'connected afterwards and prints a line whenever the readings change, so '
         'you can watch the battery voltage sag under load and see the verdict as '
         'soon as the UPS reports it. It ends with the lowest voltage and charge '
         'seen during the test, which says more about battery health than the '
         'pass/fail result on its own. Ctrl+C stops watching; the test carries on '
         'inside the UPS either way.')
    opt('--no-follow', 'Return to the prompt immediately instead of watching.')
    opt('--follow', 'Watch after any command, not just the ones that start a test.')
    opt('--follow-seconds SECONDS',
        'Give up watching after this long. The default depends on the command: two '
        'minutes for a quick test, fifteen for a deep one, an hour for a '
        'calibration.')
    opt('--yes',
        'Permit commands that would switch the outlets off. Without this, anything '
        'that would cut power to the NAS itself is refused, because the NAS would '
        'lose power without shutting down cleanly.')

    section('PASSWORD ENTRY')
    para('The password prompt shows one * per character. Pasting works: right-click '
         'and Shift+Insert are handled by the console, and Ctrl+V is read from the '
         'clipboard directly for consoles that do not paste on their own. Alt codes '
         'work too - the console composes them before the script sees anything, so '
         'Alt+64 arrives as @. Use Alt+0nnn rather than Alt+nnn for characters above '
         '127. Backspace deletes, Ctrl+U or Esc clears the line.')
    opt('--mask-char CHAR', 'Use a different character instead of *.')
    opt('--no-mask', 'Show nothing at all while typing, hiding the length as well.')

    section('OUTPUT')
    opt('--no-color', 'Plain text with no escape codes.')
    opt('--force-color',
        'Keep colour even when the output is redirected, for example when piping '
        'into less -R. Colour is dropped automatically otherwise, and when the '
        'NO_COLOR environment variable is set.')
    opt('--usage', 'The short option summary instead of this manual.')

    section('EXAMPLES')
    for cmd, what in [
        ('synology_ups.py 192.168.1.10',
         'full report'),
        ('synology_ups.py 192.168.1.10 --watch 2',
         'live view, refreshed every two seconds'),
        ('synology_ups.py 192.168.1.10 --commands',
         'report plus the list of supported commands'),
        ('synology_ups.py 192.168.1.10 --list-rw',
         'what this UPS lets you change'),
        ('synology_ups.py 192.168.1.10 --json > ups.log',
         'append a machine-readable snapshot to a log'),
        ('synology_ups.py 192.168.1.10 --exec beeper.disable --username upsadmin',
         'silence the alarm, prompting for the password'),
        ('synology_ups.py 192.168.1.10 --set ups.delay.shutdown=60 --username upsadmin',
         'change the shutdown delay'),
    ]:
        print('  ' + pal.info(cmd))
        print('      ' + pal.note(what))

    section('WRITE ACCESS - HOW TO SET IT UP')
    para('Reading needs nothing beyond the network UPS server being switched on. '
         'Changing anything does, and DSM does not grant it out of the box. This is '
         'a one-off setup on the NAS.')
    print()
    print('  ' + pal.warn('Why the built-in account is not enough'))
    para('DSM ships with a upsd account called monuser, password secret, but it is '
         'declared as "upsmon slave" - monitoring only. Using it gets you past the '
         'login and straight into ACCESS-DENIED. You need your own account with SET '
         'and instcmds rights.', indent=4)
    print()
    print('  ' + pal.key('Step 1 - enable SSH on the NAS'))
    para('In DSM open Control Panel -> Terminal & SNMP and tick "Enable SSH '
         'service". Then log in from this machine with an account that has the '
         'administrator role:', indent=4)
    print('      ' + pal.info('ssh admin@192.168.1.10'))
    print()
    print('  ' + pal.key('Step 2 - add a upsd account'))
    para('Append a new user to the end of upsd.users. On DSM 7 the file is at '
         '/etc/ups/upsd.users, on DSM 6 at /usr/syno/etc/ups/upsd.users:', indent=4)
    for line in ["sudo tee -a /etc/ups/upsd.users > /dev/null <<'EOT'",
                 '',
                 '[upsadmin]',
                 '    password = choose-your-own',
                 '    actions = SET',
                 '    instcmds = ALL',
                 'EOT']:
        print('      ' + (pal.info(line) if line else ''))
    para('actions = SET allows --set, instcmds = ALL allows --exec. Grant only one '
         'of them if that is all you need. The name in brackets is what you pass to '
         '--username; it has nothing to do with DSM user accounts.', indent=4)
    print()
    print('  ' + pal.key('Step 3 - restart the UPS service'))
    para('upsd reads the file at startup, so it has to be restarted before the new '
         'account exists:', indent=4)
    print('      ' + pal.info('sudo synosystemctl restart ups-usb'))
    para('On older DSM 7 releases the command is:', indent=4)
    print('      ' + pal.info('sudo synoservice --restart ups-usb'))
    para('This restarts the driver too, so for a few seconds the NAS loses contact '
         'with the UPS and readings will be stale or unavailable.', indent=4)
    print()
    print('  ' + pal.key('Step 4 - use it'))
    print('      ' + pal.info('synology_ups.py 192.168.1.10 --exec beeper.disable '
                              '--username upsadmin'))
    para('The password is asked for interactively and echoed as asterisks, so it '
         'never lands in your shell history. Pass --password only in scripts. If '
         'something is refused, the script translates the terse upsd error - '
         'USERNAME-REQUIRED, ACCESS-DENIED, INVALID-VALUE and the rest - into a '
         'sentence saying what to do about it.', indent=4)
    print()
    print('  ' + pal.warn('Keep in mind'))
    para('DSM updates regenerate their UPS configuration and quietly drop this user. '
         'If writing worked and later starts failing with ACCESS-DENIED, that is '
         'why - repeat steps 2 and 3. Also note that bash history on DSM is kept in '
         '/var/tmp/.bash_history, so clear it if you would rather the password not '
         'sit there.', indent=4)

    section('WHAT SURVIVES A RESTART')
    para('This depends on the UPS, not on the NAS. Settings the UPS keeps in its own '
         'memory - typically transfer voltages, nominal output voltage, beeper state '
         'and battery date - survive both a NAS reboot and a UPS power cycle. '
         'Settings the driver only holds in memory, such as some shutdown delays, go '
         'back to their defaults whenever the driver restarts, which includes every '
         'NAS reboot. Test it: set the value, reboot, read it back.')

    section('EXIT CODES')
    opt('0', 'Everything succeeded.')
    opt('1', 'One or more --set or --exec operations failed.')
    opt('2', 'Could not connect, or the server refused the request.')
    print()


def resolve_ups(client, wanted):
    available = client.list_ups()
    if not available:
        raise NUTError('the server reports no UPS')
    if wanted:
        for name, _ in available:
            if name == wanted:
                return name
        raise NUTError('UPS "%s" not found. Available: %s'
                       % (wanted, ', '.join(n for n, _ in available)))
    return available[0][0]


def read_clipboard_text():
    """Return the clipboard contents as text, or '' if unavailable."""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return ''
        CF_UNICODETEXT = 13
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        except OSError:
            return ''
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not user32.OpenClipboard(None):
            return ''
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ''
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ''
            try:
                return ctypes.c_wchar_p(pointer).value or ''
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    import subprocess
    for cmd in (['pbpaste'],
                ['wl-paste', '-n'],
                ['xclip', '-selection', 'clipboard', '-o'],
                ['xsel', '-b', '-o']):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return proc.stdout.decode('utf-8', 'replace')
    return ''


def _clean_paste(text):
    """A password is one line - drop anything after the first line break."""
    if not text:
        return ''
    for sep in ('\r\n', '\n', '\r'):
        if sep in text:
            text = text.split(sep, 1)[0]
    return ''.join(c for c in text if c >= ' ' and c != '\x7f')


def _read_masked_windows(prompt, mask):
    import msvcrt
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf = []

    def erase(count):
        if count:
            sys.stdout.write('\b \b' * count)

    while True:
        ch = msvcrt.getwch()
        if ch in ('\r', '\n'):
            sys.stdout.write('\n')
            break
        if ch == '\x03':
            sys.stdout.write('\n')
            raise KeyboardInterrupt
        if ch in ('\x00', '\xe0'):
            # Arrow / function key: the second half of the code is discarded.
            msvcrt.getwch()
            continue
        if ch in ('\b', '\x7f'):
            if buf:
                buf.pop()
                erase(1)
            continue
        if ch == '\x16':  # Ctrl+V in consoles that do not paste for us
            pasted = _clean_paste(read_clipboard_text())
            if pasted:
                buf.extend(pasted)
                sys.stdout.write(mask * len(pasted))
            continue
        if ch in ('\x15', '\x1b'):  # Ctrl+U or Esc clears the line
            erase(len(buf))
            buf = []
            continue
        if ch < ' ':
            continue
        buf.append(ch)
        sys.stdout.write(mask)
        sys.stdout.flush()
    return ''.join(buf)


def _read_masked_posix(prompt, mask):
    import termios
    import tty
    import codecs
    import select

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    buf = []
    try:
        tty.setraw(fd)
        while True:
            byte = os.read(fd, 1)
            if not byte:
                break
            if byte == b'\x1b':
                # Drain an escape sequence (arrow keys, bracketed paste).
                while select.select([fd], [], [], 0.02)[0]:
                    os.read(fd, 1)
                sys.stdout.write('\b \b' * len(buf))
                buf = []
                sys.stdout.flush()
                continue
            text = decoder.decode(byte)
            for ch in text:
                if ch in ('\r', '\n'):
                    raise StopIteration
                if ch == '\x03':
                    raise KeyboardInterrupt
                if ch in ('\x7f', '\b'):
                    if buf:
                        buf.pop()
                        sys.stdout.write('\b \b')
                elif ch == '\x16':
                    pasted = _clean_paste(read_clipboard_text())
                    if pasted:
                        buf.extend(pasted)
                        sys.stdout.write(mask * len(pasted))
                elif ch == '\x15':
                    sys.stdout.write('\b \b' * len(buf))
                    buf = []
                elif ch >= ' ':
                    buf.append(ch)
                    sys.stdout.write(mask)
            sys.stdout.flush()
    except StopIteration:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write('\n')
        sys.stdout.flush()
    return ''.join(buf)


def read_password(prompt='Password: ', mask='*'):
    """Read a password, echoing one mask character per typed character.

    Pasting works: right-click and Shift+Insert are handled by the console
    itself, and Ctrl+V is read from the clipboard directly for consoles that
    pass it through as a raw character. Alt codes (Alt+64 for @, Alt+0233 for
    e-acute) are composed by the console before the script sees them, so they
    arrive as ordinary characters.
    """
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip('\r\n')
    if not mask:
        return getpass.getpass(prompt)
    try:
        if os.name == 'nt':
            return _read_masked_windows(prompt, mask)
        return _read_masked_posix(prompt, mask)
    except (ImportError, OSError, termios_error()):
        return getpass.getpass(prompt)


def termios_error():
    """termios.error if the module exists, otherwise a type that never matches."""
    try:
        import termios
        return termios.error
    except ImportError:
        class _Never(Exception):
            pass
        return _Never


def explain_write_error(message, needed_right):
    """Turn a terse upsd error into something actionable."""
    if 'USERNAME-REQUIRED' in message or 'PASSWORD-REQUIRED' in message:
        return ('upsd refuses anonymous writes. Supply an account with '
                '--username, or let the script prompt you for one.')
    if 'ACCESS-DENIED' in message:
        return ('That account exists but lacks "%s". The default Synology account '
                '(monuser) can only monitor - add your own to /etc/ups/upsd.users '
                'over SSH; see the notes at the end of this script.' % needed_right)
    if 'CMD-NOT-SUPPORTED' in message or 'VAR-NOT-SUPPORTED' in message:
        return 'The driver accepted the request but this UPS does not implement it.'
    if 'INVALID-VALUE' in message:
        return 'The value is outside what the UPS accepts - check --list-rw.'
    if 'UNKNOWN-UPS' in message:
        return 'No UPS by that name on this server - check -u / --ups.'
    return 'See the notes at the end of this script for the required upsd rights.'


def resolve_credentials(pal, args):
    """Ask for a upsd account when one is needed and none was supplied."""
    username, password = args.username, args.password
    if not (args.set_vars or args.exec_cmds):
        return username, password
    if not username:
        if not sys.stdin.isatty():
            raise NUTError('changing anything requires a upsd account; pass --username')
        print(pal.warn('This operation needs a upsd account with write rights.'))
        print(pal.note('The default Synology account (monuser) can only monitor, so it '
                       'will be rejected.\nSee the notes at the end of this script for '
                       'how to create one over SSH.'))
        username = input('upsd username: ').strip()
        if not username:
            raise NUTError('no username given')
    if password is None:
        mask = '' if args.no_mask else (args.mask_char[:1] or '*')
        password = read_password('upsd password: ', mask)
    return username, password


# Commands worth staying connected for: they start something that takes time
# and changes readings while it runs.
FOLLOWABLE_COMMANDS = {
    'test.battery.start': 120,
    'test.battery.start.quick': 120,
    'test.battery.start.deep': 900,
    'test.system.start': 120,
    'test.panel.start': 60,
    'test.failure.start': 300,
    'calibrate.start': 3600,
}

# Values ups.test.result takes while a test is still running.
RUNNING_RESULTS = ('in progress', 'pending', 'scheduled', 'running')
BUSY_FLAGS = {'OB', 'DISCHRG', 'CAL', 'TEST'}


def _test_running(variables, baseline_result):
    result = variables.get('ups.test.result', '').lower()
    if any(word in result for word in RUNNING_RESULTS):
        return True
    flags = set(variables.get('ups.status', '').split())
    return bool(flags & BUSY_FLAGS)


def follow_test(pal, client, ups, cmd, limit=None):
    """Stay connected after starting a test and report what the UPS does."""
    if limit is None:
        limit = FOLLOWABLE_COMMANDS.get(cmd, 120)

    watched = ['ups.status', 'battery.charge', 'battery.voltage',
               'battery.runtime', 'ups.test.result']
    started = time.time()
    variables = client.list_vars(ups)
    baseline_result = variables.get('ups.test.result', '')

    is_test = cmd in FOLLOWABLE_COMMANDS
    print('\n' + pal.title('Following %s' % cmd)
          + pal.note('  -  Ctrl+C stops watching, the UPS carries on regardless'))
    print('  ' + pal.key('%-7s %-16s %-8s %-9s %-10s %s'
                         % ('time', 'status', 'charge', 'voltage', 'runtime',
                            'test result')))

    def row(elapsed, v):
        runtime = v.get('battery.runtime', '')
        pretty = human_runtime(runtime) or runtime
        status = v.get('ups.status', '')
        status_text = (pal.warn(status.ljust(16)) if set(status.split()) & BUSY_FLAGS
                       else pal.ok(status.ljust(16)))
        print('  %-7s %s %-8s %-9s %-10s %s'
              % ('%ds' % elapsed, status_text,
                 (v.get('battery.charge', '') + ' %'),
                 (v.get('battery.voltage', '') + ' V'),
                 pretty, pal.note(v.get('ups.test.result', ''))))

    def snapshot(v):
        return tuple(v.get(k, '') for k in watched)

    last = None
    last_print = 0.0
    seen_running = False
    min_voltage = None
    min_charge = None
    voltages_seen = set()
    charges_seen = set()
    busy_since = None
    busy_until = None
    discharge_seconds = 0.0
    discharge_flags = {'OB', 'DISCHRG'}
    last_sample = None
    outcome = None

    try:
        while True:
            elapsed = time.time() - started
            variables = client.list_vars(ups)

            try:
                voltage = float(variables.get('battery.voltage', 'nan'))
                min_voltage = voltage if min_voltage is None else min(min_voltage, voltage)
                voltages_seen.add(voltage)
            except ValueError:
                pass
            try:
                charge = float(variables.get('battery.charge', 'nan'))
                min_charge = charge if min_charge is None else min(min_charge, charge)
                charges_seen.add(charge)
            except ValueError:
                pass

            now = time.time()
            if set(variables.get('ups.status', '').split()) & discharge_flags:
                discharge_seconds += (now - last_sample) if last_sample else 0.0
            last_sample = now

            current = snapshot(variables)
            if current != last or elapsed - last_print >= 5:
                row(int(elapsed), variables)
                last, last_print = current, elapsed

            running = _test_running(variables, baseline_result)
            if running:
                if busy_since is None:
                    busy_since = time.time()
                busy_until = time.time()
                seen_running = True
            elif seen_running:
                outcome = variables.get('ups.test.result', '')
                break
            elif elapsed > 20 and variables.get('ups.test.result', '') != baseline_result:
                outcome = variables.get('ups.test.result', '')
                break
            elif elapsed > 20:
                break

            if elapsed >= limit:
                if seen_running:
                    print('  ' + pal.warn('Still running after %ds - no longer '
                                          'watching. Check ups.test.result later.'
                                          % int(limit)))
                elif is_test:
                    print('  ' + pal.warn('Nothing changed within %ds. The UPS may '
                                          'have declined the test.' % int(limit)))
                else:
                    print('  ' + pal.note('Watched for %ds, nothing changed.'
                                          % int(limit)))
                return
            time.sleep(1.0)
    except KeyboardInterrupt:
        print('\n  ' + pal.note('Stopped watching. The UPS carries on regardless.'))
        return

    elapsed = int(time.time() - started)
    if outcome:
        good = 'pass' in outcome.lower()
        verdict = pal.ok(outcome) if good else pal.warn(outcome)
        print('\n  ' + pal.title('Finished after %ds: ' % elapsed) + verdict)
    else:
        print('\n  ' + pal.warn('No change observed in %ds.' % elapsed))
        print('  ' + pal.note('The UPS may have finished between polls - the driver '
                              'only refreshes every few seconds. Current result: %s'
                              % (baseline_result or 'not reported')))

    on_battery = int(round(discharge_seconds))
    if busy_since is not None and busy_until is not None:
        print('  ' + pal.note('The test ran for about %ds.'
                              % max(1, int(round(busy_until - busy_since)))))
    if on_battery:
        print('  ' + pal.note('Of that, the status showed a discharge for roughly '
                              '%ds - the rest was the UPS recharging between '
                              'stages.' % on_battery))
    elif seen_running:
        print('  ' + pal.note('The status never showed a discharge, so the switch '
                              'to battery fell between polls.'))

    if min_voltage is not None and len(voltages_seen) > 1:
        note = 'Battery dipped to %.1f V' % min_voltage
        if min_charge is not None and len(charges_seen) > 1:
            note += ' and %d%%' % min_charge
        print('  ' + pal.note(note + ' while under test.'))
        nominal = variables.get('battery.voltage.nominal')
        try:
            if nominal and min_voltage < float(nominal) * 0.92:
                print('  ' + pal.warn('That is well under the %s V nominal - worth '
                                      'watching if it keeps dropping.' % nominal))
        except ValueError:
            pass
    elif min_voltage is not None:
        raw_pollfreq = variables.get('driver.parameter.pollfreq', '30')
        try:
            pollfreq = float(raw_pollfreq)
        except ValueError:
            pollfreq = 30.0
        print('  ' + pal.note('battery.voltage stayed at %.1f V throughout.'
                              % min_voltage))
        if on_battery < pollfreq:
            print('  ' + pal.note('The driver refreshes that value only every %s s, '
                                  'and the load was on battery for less than that, '
                                  'so the dip fell between two polls.' % raw_pollfreq))
        else:
            print('  ' + pal.note('The load was on battery for longer than the %s s '
                                  'poll cycle, so the value should have moved. This '
                                  'model may simply not report voltage changes to '
                                  'the driver.' % raw_pollfreq))
        print('  ' + pal.note('To judge the battery by voltage, watch a real '
                              'discharge: run --watch 5 and pull the mains plug for '
                              'a minute, or start test.battery.start.deep.'))


def wait_for_value(client, ups, var, expected, variables=None, limit=None):
    """Poll a variable until the driver reports the new value.

    usbhid-ups only refreshes its cache every driver.parameter.pollinterval
    seconds, so a value read back immediately after SET is still the old one.
    """
    if limit is None:
        try:
            interval = float((variables or {}).get(
                'driver.parameter.pollinterval', 5))
        except (TypeError, ValueError):
            interval = 5.0
        limit = max(6.0, interval * 3)
    deadline = time.time() + limit
    current = None
    while True:
        current = client.list_vars(ups).get(var, '<unknown>')
        if current == expected or time.time() >= deadline:
            return current
        time.sleep(1.0)


def run_write_actions(pal, args):
    """Handle --list-rw, --set and --exec."""
    username, password = resolve_credentials(pal, args)
    with NUTClient(args.host, args.port, args.timeout) as c:
        if username:
            c.login(username, password or '')
        ups = resolve_ups(c, args.ups)

        if args.list_rw:
            print_writable(pal, c, ups, args.host)
            return 0

        print(banner(pal, args.host, ups))
        failed = False
        for item in args.set_vars:
            if '=' not in item:
                print(pal.bad('ERROR: ') + '--set expects VAR=VALUE, got "%s"' % item)
                failed = True
                continue
            var, value = item.split('=', 1)
            var, value = var.strip(), value.strip()
            rw = c.list_rw(ups)
            if var not in rw:
                print(pal.bad('SET  ') + '%s is not writable on this UPS.' % var)
                if rw:
                    print('     ' + pal.note('Writable here: ' + ', '.join(sorted(rw))))
                else:
                    print('     ' + pal.note('This UPS exposes no writable variables.'))
                failed = True
                continue
            enum = c.list_enum(ups, var)
            if enum and value not in enum:
                print(pal.bad('SET  ') + '%s only accepts: %s' % (var, ', '.join(enum)))
                failed = True
                continue
            ranges = c.list_range(ups, var)
            if ranges:
                try:
                    numeric = float(value)
                    if not any(float(lo) <= numeric <= float(hi) for lo, hi in ranges):
                        print(pal.bad('SET  ') + '%s must be within %s' % (
                            var, ' or '.join('%s..%s' % r for r in ranges)))
                        failed = True
                        continue
                except ValueError:
                    pass
            try:
                variables = c.list_vars(ups)
                before = variables.get(var, '<unknown>')
                c.set_var(ups, var, value)
                after = wait_for_value(c, ups, var, value, variables)
                if after == value:
                    print(pal.ok('SET  ') + '%s: %s -> %s'
                          % (var, before, pal.value(after)))
                else:
                    print(pal.warn('SET  ') + '%s: accepted, but the UPS reports %s'
                          % (var, pal.value(after)))
                    print('     ' + pal.note(
                        'Either the UPS adjusted the value to something it supports, '
                        'or the driver has not polled it back yet. Check again with '
                        '--list-rw in a few seconds.'))
            except NUTError as e:
                failed = True
                print(pal.bad('SET  ') + '%s failed: %s' % (var, e))
                print('     ' + pal.note(explain_write_error(str(e), 'actions = SET')))

        for cmd in args.exec_cmds:
            if is_dangerous(cmd) and not args.yes:
                print(pal.bad('REFUSED ') + '"%s" can cut power to everything on the '
                                            'UPS, including the NAS.' % cmd)
                print('     ' + pal.note('Re-run with --yes if you really mean it.'))
                failed = True
                continue
            try:
                c.instcmd(ups, cmd)
                print(pal.ok('EXEC ') + '%s accepted' % cmd)
                if not args.no_follow and (cmd in FOLLOWABLE_COMMANDS or args.follow):
                    follow_test(pal, c, ups, cmd, args.follow_seconds)
            except NUTError as e:
                failed = True
                print(pal.bad('EXEC ') + '%s failed: %s' % (cmd, e))
                print('     ' + pal.note(explain_write_error(str(e), 'instcmds = ALL')))
        return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description='Read every piece of information a UPS attached to a Synology NAS '
                    'exposes, via the NUT network server on TCP 3493.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Run with --help for the full manual, including examples and how to\n'
               'grant an account the rights needed for --set and --exec.',
        add_help=False)
    parser.add_argument('-h', '--help', action='store_true', dest='show_help',
                        help='show the full manual with examples and explanations')
    parser.add_argument('--usage', action='store_true',
                        help='show the short option summary instead')
    parser.add_argument('--version', action='version',
                        version='synology_ups.py ' + __version__)
    parser.add_argument('host', help='IP address or hostname of the Synology NAS')
    parser.add_argument('-p', '--port', type=int, default=DEFAULT_PORT,
                        help='upsd port (default 3493)')
    parser.add_argument('-u', '--ups',
                        help='UPS name (default: every one found; usually "ups" on Synology)')
    parser.add_argument('--username', help='upsd username (usually not required)')
    parser.add_argument('--password', help='upsd password')
    parser.add_argument('--desc', action='store_true',
                        help='ask the server for a description of every variable')
    parser.add_argument('--commands', action='store_true',
                        help='also list the instant commands the UPS supports')
    parser.add_argument('--json', action='store_true', dest='as_json',
                        help='print machine-readable JSON instead of a report')
    parser.add_argument('-w', '--watch', type=float, metavar='SECONDS',
                        help='refresh every SECONDS (Ctrl+C to stop)')
    parser.add_argument('-t', '--timeout', type=float, default=5.0,
                        help='connection timeout in seconds')
    parser.add_argument('--list-rw', action='store_true',
                        help='list writable variables, their allowed values, '
                             'and the instant commands the UPS supports')
    parser.add_argument('--set', action='append', default=[], dest='set_vars',
                        metavar='VAR=VALUE',
                        help='change a writable variable (may be repeated)')
    parser.add_argument('--exec', action='append', default=[], dest='exec_cmds',
                        metavar='COMMAND',
                        help='run an instant command such as beeper.disable '
                             '(may be repeated)')
    parser.add_argument('--yes', action='store_true',
                        help='allow commands that would cut power to the load')
    parser.add_argument('--follow', action='store_true',
                        help='after --exec, keep watching the UPS even for commands '
                             'that are not tests')
    parser.add_argument('--no-follow', action='store_true',
                        help='return to the prompt immediately after --exec')
    parser.add_argument('--follow-seconds', type=float, metavar='SECONDS',
                        help='how long to keep watching before giving up '
                             '(default depends on the command)')
    parser.add_argument('--mask-char', default='*', metavar='CHAR',
                        help='character echoed for each password character (default *)')
    parser.add_argument('--no-mask', action='store_true',
                        help='echo nothing at all while the password is typed')
    parser.add_argument('--battery-life', type=float, default=4.0, metavar='YEARS',
                        help='service life used to colour a battery date the UPS '
                             'reports (default 4 years)')
    colour = parser.add_mutually_exclusive_group()
    colour.add_argument('--no-color', action='store_true', help='disable coloured output')
    colour.add_argument('--force-color', action='store_true',
                        help='force colour even when redirecting to a file')
    try:
        if '-h' in sys.argv[1:] or '--help' in sys.argv[1:]:
            print_full_help(Palette(supports_colour(
                False if '--no-color' in sys.argv else None)))
            return 0
        if '--usage' in sys.argv[1:]:
            parser.print_help()
            return 0
    except BrokenPipeError:
        return 0

    args = parser.parse_args()

    force = True if args.force_color else (False if args.no_color else None)
    pal = Palette(supports_colour(force))

    if os.name == 'nt' and not COLORAMA_AVAILABLE and not args.no_color:
        print('Note: colorama is not installed, running without colour. '
              'Install it with:  pip install colorama\n', file=sys.stderr)

    def once():
        data = collect(args)
        if args.as_json:
            print(json.dumps(data, indent=2))
        else:
            for entry in data:
                print_report(pal, entry, args.host, args.battery_life)

    try:
        if args.list_rw or args.set_vars or args.exec_cmds:
            return run_write_actions(pal, args)
        if args.watch:
            while True:
                clear_screen()
                once()
                print(pal.note('Refreshing every %.1fs - press Ctrl+C to stop' % args.watch))
                time.sleep(args.watch)
        else:
            once()
    except NUTError as e:
        print(pal.bad('ERROR: ') + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\nStopped.')
        return 0
    except BrokenPipeError:
        # Output was piped into something that closed early, e.g. head or less.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    finally:
        if COLORAMA_AVAILABLE:
            colorama.deinit()
    return 0


if __name__ == '__main__':
    sys.exit(main())

# ---------------------------------------------------------------------------
# NOTES ON WRITING TO THE UPS FROM A SYNOLOGY NAS
# ---------------------------------------------------------------------------
# By default DSM's network UPS server only grants monitoring rights, so SET VAR
# and INSTCMD come back as ERR ACCESS-DENIED no matter what credentials you use.
# To allow writing you have to SSH into the NAS and add a upsd user yourself:
#
#   sudo vi /etc/ups/upsd.users          # DSM 7 (DSM 6: /usr/syno/etc/ups/upsd.users)
#
#     [myadmin]
#         password = choose-something
#         actions = SET
#         instcmds = ALL
#
#   sudo synoservice --restart ups-usb   # or: sudo /usr/syno/bin/synoservicecfg
#                                        #     --restart ups-usb   (older DSM)
#
# Caveats:
#   * DSM regenerates its UPS configuration on updates and sometimes when you
#     touch the UPS page in Control Panel, which silently removes this user.
#     If writes suddenly fail with ACCESS-DENIED, add it again.
#   * Whether a value survives a power cycle depends on the UPS, not on NUT.
#     Values written into the UPS EEPROM (typical for APC Smart-UPS, Eaton,
#     CyberPower with the right driver) persist. Values the driver only keeps
#     in memory are lost when the NAS reboots and the driver restarts.
#   * Many cheap USB HID units expose no writable variables at all.
