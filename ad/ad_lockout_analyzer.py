#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ad_lockout_analyzer.py - Active Directory account lockout analyzer for Security EVTX logs.

Answers four questions for a given account:
  * WHEN did it lock out              -> event 4740
  * WHAT locked it                    -> bad-password events inside the window before each 4740
  * WHERE do the bad passwords come from -> caller computer / workstation / source IP
  * IS IT STILL HAPPENING             -> failures recorded after the most recent unlock

Every conclusion in the report is derived from the log that was passed in. Anything the
tool cannot know from a domain controller log (which physical device sits behind a RADIUS
server, which process on a client holds the stale credential) is reported as a lead to
follow, not as a fact.

Usage:
    python3 ad_lockout_analyzer.py -u jsmith Security.evtx
    python3 ad_lockout_analyzer.py -u jsmith logs.zip --tz +02:00 --csv events.csv
    python3 ad_lockout_analyzer.py -u jsmith DC1.evtx DC2.evtx --window 90

Requirements:
    pip install evtx        (Rust-backed parser; roughly 30 s per GB of log)

Input should be the Security channel from a domain controller. Event 4740 is written only
on the PDC emulator, while the failed attempts are logged on whichever DC served them, so
feeding in the logs from all DCs gives the most complete picture.
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Status code tables
# ---------------------------------------------------------------------------

KERBEROS_STATUS = {
    "0x0": "success",
    "0x6": "client not found in database (typo, stale or deleted account)",
    "0x7": "server not found in database (bad SPN)",
    "0xc": "policy restriction (workstation or logon hours)",
    "0xe": "unsupported encryption type",
    "0x12": "account LOCKED OUT, disabled or expired",
    "0x17": "password expired",
    "0x18": "BAD PASSWORD (pre-authentication failed)",
    "0x1f": "integrity check failed",
    "0x20": "ticket expired",
    "0x25": "clock skew between client and DC",
}

NTLM_STATUS = {
    "0x0": "success",
    "0xc0000064": "user does not exist (often a wrong name format, e.g. UPN sent to NTLM)",
    "0xc000006a": "BAD PASSWORD",
    "0xc000006d": "generic logon failure",
    "0xc000006e": "account restriction",
    "0xc000006f": "logon outside permitted hours",
    "0xc0000070": "logon from unauthorised workstation",
    "0xc0000071": "password expired",
    "0xc0000072": "account disabled",
    "0xc0000133": "clock skew between client and DC",
    "0xc0000193": "account expired",
    "0xc0000224": "password change required",
    "0xc0000234": "account LOCKED OUT",
}

EVENT_NAMES = {
    "4624": "successful logon",
    "4625": "failed logon",
    "4634": "logoff",
    "4647": "user-initiated logoff",
    "4648": "logon with explicit credentials (runas)",
    "4720": "account created",
    "4722": "account enabled",
    "4723": "password changed by user",
    "4724": "password reset by administrator",
    "4725": "account disabled",
    "4726": "account deleted",
    "4738": "account changed",
    "4740": "ACCOUNT LOCKED OUT",
    "4767": "account unlocked",
    "4768": "Kerberos TGT requested",
    "4769": "Kerberos service ticket requested",
    "4771": "Kerberos pre-authentication failed",
    "4776": "credential validation (NTLM)",
    "4777": "NTLM credential validation failed",
}

# Failures that increment badPwdCount and therefore drive lockouts.
BAD_PASSWORD = {("4771", "0x18"), ("4776", "0xc000006a"), ("4625", "0xc000006a")}
# Attempts made while the account was already locked.
LOCKED_ATTEMPT = {("4771", "0x12"), ("4776", "0xc0000234"), ("4625", "0xc0000234")}

FAILURE_EIDS = {"4625", "4771", "4776", "4777"}
ADMIN_EIDS = {"4720", "4722", "4723", "4724", "4725", "4726", "4738", "4767"}

# Substrings that suggest a source is an authentication proxy rather than an end device.
PROXY_HINTS = ("ISE", "RADIUS", "NPS", "ACS", "FREERADIUS", "VPN", "ASA",
               "FORTIGATE", "CLEARPASS", "ADFS", "PROXY", "GATEWAY")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_tz(value):
    """Accepts 'UTC' or an offset such as +02:00 / -0500. Returns (tzinfo, label)."""
    if value is None or value.upper() in ("UTC", "Z", "0"):
        return timezone.utc, "UTC"
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", value)
    if not m:
        raise argparse.ArgumentTypeError("Time zone must be UTC or an offset like +02:00.")
    sign = 1 if m.group(1) == "+" else -1
    offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return timezone(sign * offset), value


def normalise_account(name):
    """'jsmith@corp.local' -> 'jsmith'; 'CORP\\jsmith' -> 'jsmith'."""
    if not name:
        return ""
    n = name.strip()
    if "\\" in n:
        n = n.split("\\")[-1]
    if "@" in n:
        n = n.split("@")[0]
    return n.lower()


def normalise_ip(ip):
    """Strips the IPv4-mapped IPv6 prefix and drops placeholders and loopback."""
    if not ip or ip in ("-", "::1", "127.0.0.1"):
        return ""
    return ip.replace("::ffff:", "")


def clean(value):
    """Windows writes '-' for empty fields and %%17xx for 'not changed' markers."""
    return "" if value in (None, "-", "%%1793", "%%1794") else str(value)


def hexlow(value):
    return str(value).lower() if value else ""


def stamp(ts, tz):
    return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def status_text(event_id, status):
    """Maps a status code to plain text; Kerberos and NTLM use different tables."""
    code = hexlow(status)
    if event_id in ("4768", "4769", "4771"):
        return KERBEROS_STATUS.get(code, "unknown code")
    return NTLM_STATUS.get(code, "unknown code")


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def resolve_inputs(paths, workdir):
    """Expands any .zip arguments and returns a flat list of .evtx paths."""
    resolved = []
    for path in paths:
        if not os.path.exists(path):
            sys.exit("File not found: %s" % path)
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.namelist() if m.lower().endswith(".evtx")]
                if not members:
                    sys.exit("No .evtx file inside archive: %s" % path)
                for member in members:
                    print("  extracting %s ..." % member, file=sys.stderr)
                    resolved.append(archive.extract(member, workdir))
        else:
            resolved.append(path)
    return resolved


def iter_records(path):
    try:
        from evtx import PyEvtxParser
    except ImportError:
        sys.exit(
            "Missing the 'evtx' module. Install it with:\n"
            "    pip install evtx\n"
            "(on externally managed systems: pip install evtx --break-system-packages)"
        )
    for record in PyEvtxParser(path).records_json():
        yield record


def collect(paths, account, match_mode):
    """Single pass over the logs. Returns (events for the account, file statistics).

    Records are pre-filtered with a cheap substring test on the raw JSON before being
    parsed, which is what keeps multi-gigabyte logs tractable.
    """
    token = account.lower()
    events = []
    stats = {"records": 0, "matched": 0, "first": None, "last": None, "computers": Counter()}
    event_id_re = re.compile(r'"EventID":\s*"?(\d+)')

    for path in paths:
        for record in iter_records(path):
            stats["records"] += 1
            raw = record["data"]
            if token not in raw.lower():
                continue
            try:
                event = json.loads(raw)["Event"]
            except Exception:
                continue
            system = event.get("System", {})
            data = event.get("EventData") or {}
            if not isinstance(data, dict):
                continue

            event_id = system.get("EventID", "")
            if isinstance(event_id, dict):                 # some providers nest it
                event_id = event_id.get("#text", "")
            event_id = str(event_id)
            if not event_id.isdigit():
                m = event_id_re.search(raw)
                event_id = m.group(1) if m else ""

            target = data.get("TargetUserName") or data.get("TargetName") or ""
            subject = data.get("SubjectUserName") or ""
            target_norm, subject_norm = normalise_account(target), normalise_account(subject)

            if match_mode == "exact":
                hit_target = target_norm == token
                hit_subject = subject_norm == token
            else:                                          # loose: also catches adm-x, HOST$
                hit_target = token in target_norm
                hit_subject = token in subject_norm
            if not (hit_target or hit_subject):
                continue

            try:
                ts = datetime.fromisoformat(
                    system["TimeCreated"]["#attributes"]["SystemTime"].replace("Z", "+00:00")
                )
            except Exception:
                continue
            if ts.year < 1990:                             # corrupt or unwritten record
                continue

            stats["matched"] += 1
            if stats["first"] is None or ts < stats["first"]:
                stats["first"] = ts
            if stats["last"] is None or ts > stats["last"]:
                stats["last"] = ts
            stats["computers"][system.get("Computer", "?")] += 1

            events.append({
                "ts": ts,
                "event_id": event_id,
                "dc": system.get("Computer", ""),
                "record_id": system.get("EventRecordID", ""),
                "target": target,
                "subject": subject,
                "role": "target" if hit_target else "subject",
                "status": hexlow(data.get("Status") or data.get("ResultCode") or ""),
                "substatus": hexlow(data.get("SubStatus") or ""),
                "ip": normalise_ip(data.get("IpAddress")),
                "workstation": clean(data.get("WorkstationName") or data.get("Workstation")),
                # In 4740 the TargetDomainName field carries the caller computer name
                # instead of a domain; everywhere else it really is the domain.
                "domain": clean(data.get("TargetDomainName")),
                "logon_type": clean(data.get("LogonType")),
                "sid": clean(data.get("TargetSid")),
            })

    events.sort(key=lambda e: (e["ts"], e["event_id"]))
    return events, stats


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def is_bad_password(event):
    return ((event["event_id"], event["status"]) in BAD_PASSWORD
            or (event["event_id"], event["substatus"]) in BAD_PASSWORD)


def is_locked_attempt(event):
    return ((event["event_id"], event["status"]) in LOCKED_ATTEMPT
            or (event["event_id"], event["substatus"]) in LOCKED_ATTEMPT)


def source_label(event):
    """Best available identifier of where an attempt came from.

    NTLM events (4776) carry a workstation or the name of the server that relayed the
    request; Kerberos events (4771) carry a client IP instead. Either may be absent.
    """
    parts = []
    if event["workstation"]:
        parts.append(event["workstation"].replace("\\\\", "\\"))
    if event["ip"]:
        parts.append(event["ip"])
    return " / ".join(parts) if parts else "(source not recorded)"


def looks_like_proxy(label):
    upper = label.upper()
    return any(hint in upper for hint in PROXY_HINTS)


def detect_cadence(times):
    """Finds a dominant interval between attempts.

    A repeating interval means something is retrying on a timer - a scheduled task, a
    service, a mail client or a supplicant - rather than a person typing a password.
    Returns (minutes, share_of_gaps, description) or None if no interval dominates.
    """
    if len(times) < 5:
        return None
    gaps = []
    for earlier, later in zip(times, times[1:]):
        minutes = (later - earlier).total_seconds() / 60.0
        if 1 <= minutes <= 24 * 60:                # ignore bursts and overnight breaks
            gaps.append(minutes)
    if len(gaps) < 4:
        return None

    best, best_count = None, 0
    for candidate in sorted({round(g) for g in gaps}):
        if candidate < 1:
            continue
        tolerance = max(1.0, candidate * 0.1)      # clients drift a little between retries
        count = sum(1 for g in gaps if abs(g - candidate) <= tolerance)
        if count > best_count:
            best, best_count = candidate, count
    if best is None or best_count / len(gaps) < 0.30:
        return None

    share = best_count / len(gaps)
    if best >= 60 and best % 60 == 0:
        description = "every %d h" % (best // 60)
    else:
        description = "every %d min" % best
    return best, share, description


def activity_profile(times, tz):
    """Describes when a source is active, which narrows down what kind of thing it is.

    Round-the-clock activity including weekends points at an always-on device holding a
    stored credential. Activity confined to weekdays and business hours points at
    something that only runs while a workstation is powered on.
    """
    if not times:
        return ""
    local = [t.astimezone(tz) for t in times]
    weekend = sum(1 for t in local if t.weekday() >= 5)
    off_hours = sum(1 for t in local if t.hour < 5 or t.hour >= 20)
    hours = sorted({t.hour for t in local})

    bits = ["weekend attempts: %d of %d" % (weekend, len(local)),
            "hour range %02d-%02d" % (hours[0], hours[-1])]
    if weekend and off_hours:
        bits.append("runs around the clock -> likely an always-on device or service "
                    "with a stored credential")
    elif not weekend:
        bits.append("weekdays only -> likely tied to a machine that is powered on for "
                    "business hours")
    return "; ".join(bits)


def is_public_ip(text):
    """True for a routable address. Bad passwords from outside the LAN change the
    reading of the whole picture: it may be exposure rather than a stale credential."""
    try:
        import ipaddress
        addr = ipaddress.ip_address(text)
        return not (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast)
    except Exception:
        return False


def max_burst(times, seconds=60):
    """Largest number of attempts inside a sliding window of N seconds.

    A tight burst means a client retrying in a loop; spaced-out singles are more
    consistent with a person or a slow poller.
    """
    if not times:
        return 0
    span = timedelta(seconds=seconds)
    best, start = 0, 0
    for end in range(len(times)):
        while times[end] - times[start] > span:
            start += 1
        best = max(best, end - start + 1)
    return best


# ---------------------------------------------------------------------------
# Findings and leads
#
# Everything below is derived at runtime from what the log actually contains. Each
# rule states a condition over the findings dictionary and produces its text from
# the same data, so the section adapts to logs that look nothing like each other:
# no lockouts at all, lockouts with no visible cause, external brute force, a
# broken computer account, an admin unlocking repeatedly without fixing anything.
# Rules are independent - zero, one or many may fire.
# ---------------------------------------------------------------------------


def build_findings(owned, events, stats, lockouts, unlocks, bad, locked_attempts,
                   window_minutes, tz, account):
    """Reduces the parsed events to the signals the rules below reason about."""
    window = timedelta(minutes=window_minutes)
    grace = timedelta(seconds=5)

    by_source = defaultdict(list)
    for event in bad:
        by_source[source_label(event)].append(event)

    success_sources = {
        source_label(e) for e in owned
        if (e["event_id"] in ("4768", "4776") and e["status"] == "0x0")
        or (e["event_id"] == "4624" and e["logon_type"] in ("2", "3", "7", "10", "11"))
    }

    empty_windows, multi_source_windows = 0, 0
    for lockout in lockouts:
        preceding = [e for e in bad if -grace <= lockout["ts"] - e["ts"] <= window]
        if not preceding:
            empty_windows += 1
        elif len({source_label(e) for e in preceding}) > 1:
            multi_source_windows += 1

    # A lockout shortly after an unlock means the cause was never addressed.
    relocks = 0
    for unlock in unlocks:
        if any(timedelta(0) < lock["ts"] - unlock["ts"] <= timedelta(hours=4)
               for lock in lockouts):
            relocks += 1

    counts = Counter({label: len(group) for label, group in by_source.items()})
    total_bad = sum(counts.values())
    dominant = None
    if counts:
        label, count = counts.most_common(1)[0]
        if total_bad and count / total_bad >= 0.75 and len(counts) > 1:
            dominant = (label, count / total_bad)

    protocols = set()
    for event in bad:
        protocols.add("kerberos" if event["event_id"] == "4771" else "ntlm")

    days_with_bad = Counter(e["ts"].astimezone(tz).date() for e in bad)

    return {
        "account": account,
        "window_minutes": window_minutes,
        "stats": stats,
        "lockout_count": len(lockouts),
        "bad_count": total_bad,
        "owned_count": len(owned),
        "unlock_count": len(unlocks),
        "locked_attempts": len(locked_attempts),
        "callers": sorted({e["domain"] for e in lockouts if e["domain"]}),
        "source_counts": counts,
        "dominant_source": dominant,
        "proxy_sources": sorted(s for s in counts if looks_like_proxy(s)),
        "public_sources": sorted(s for s in counts if any(is_public_ip(p)
                                                          for p in s.split(" / "))),
        "ip_only_sources": sorted(s for s in counts
                                  if re.fullmatch(r"[0-9a-fA-F:.]+", s)),
        "unnamed_sources": counts.get("(source not recorded)", 0),
        "cadenced_sources": [
            (label, detect_cadence(sorted(e["ts"] for e in group))[2])
            for label, group in by_source.items()
            if detect_cadence(sorted(e["ts"] for e in group))
        ],
        "burst_sources": sorted(
            label for label, group in by_source.items()
            if max_burst(sorted(e["ts"] for e in group)) >= 3
        ),
        "shared_sources": sorted(set(counts) & success_sources),
        "empty_windows": empty_windows,
        "multi_source_windows": multi_source_windows,
        "relocks": relocks,
        "protocols": protocols,
        "wrong_name_format": any(e["status"] == "0xc0000064" for e in owned),
        "clock_skew": sum(1 for e in owned if e["status"] in ("0x25", "0xc0000133")),
        "expired_password": any(e["status"] in ("0x17", "0xc0000071") for e in owned),
        "restriction": any(e["status"] in ("0xc", "0xc000006f", "0xc0000070")
                           for e in owned),
        "password_set_events": [e for e in events if e["event_id"] in ("4723", "4724")
                                and normalise_account(e["target"]) == account.lower()],
        # Only meaningful when the machine account is the one failing.
        "machine_account": any(
            e["target"].endswith("$") for e in owned
            if e["event_id"] in FAILURE_EIDS and e["status"] not in ("0x0", "")
        ),
        "peak_per_day": max(days_with_bad.values()) if days_with_bad else 0,
        "single_dc": len(stats["computers"]) == 1,
        "history_truncated": bool(
            bad and stats["first"] and min(e["ts"] for e in bad) - stats["first"]
            <= timedelta(hours=2)
        ),
    }


def _join(items, limit=4):
    items = list(items)
    shown = ", ".join(str(i) for i in items[:limit])
    return shown + (" and %d more" % (len(items) - limit) if len(items) > limit else "")


# Each rule: (condition, message). Both take the findings dict.
LEAD_RULES = [
    # --- situations where the data itself is incomplete -----------------------
    (lambda f: f["lockout_count"] == 0 and f["bad_count"] > 0,
     lambda f: "No lockout was recorded, but %d bad-password attempts were. Either the "
               "threshold was never reached in one observation window, or this log is not "
               "from the PDC emulator, which is the only DC that writes event 4740."
               % f["bad_count"]),

    (lambda f: f["lockout_count"] == 0 and f["bad_count"] == 0,
     lambda f: "The account appears %d time(s) in this log with no lockouts and no failed "
               "authentications at all. If a lockout was reported anyway, it happened "
               "outside the captured period, on a DC whose log is missing, or under a "
               "different account name." % f["owned_count"]),

    (lambda f: f["lockout_count"] > 0 and f["bad_count"] == 0,
     lambda f: "The account locked out %d time(s) but no bad-password events are present. "
               "The failures were served by a different domain controller - collect the "
               "Security logs from the remaining DCs and rerun." % f["lockout_count"]),

    (lambda f: f["empty_windows"] > 0 and f["bad_count"] > 0,
     lambda f: "%d of %d lockouts have no failures in the correlation window. Rerun with a "
               "larger --window, and add the other DCs' logs if that does not fill the gap."
               % (f["empty_windows"], f["lockout_count"])),

    (lambda f: f["single_dc"] and f["lockout_count"] > 0,
     lambda f: "Only one domain controller is represented in this data (%s). Failures "
               "handled by other DCs are invisible here, so the windows above may be "
               "incomplete."
               % _join(f["stats"]["computers"], 2)),

    (lambda f: f["history_truncated"] and f["bad_count"] > 0,
     lambda f: "The failures start within the first two hours of the log, so whatever "
               "triggered them - typically a password change - happened before this "
               "export begins. An older log would show the transition point."),

    # --- what kind of thing is failing ---------------------------------------
    (lambda f: bool(f["proxy_sources"]),
     lambda f: "Attempts arriving via %s appear to be relayed by an authentication proxy "
               "or RADIUS server. A DC log cannot see past it, so pull that system's own "
               "logs for the same timestamps to identify the client behind it (calling "
               "station ID, MAC, NAS port)." % _join(f["proxy_sources"])),

    (lambda f: bool(f["public_sources"]),
     lambda f: "Attempts came from routable addresses (%s). Treat this as possible exposure "
               "rather than a stale credential: check what service is reachable from "
               "outside, review it for password spraying against other accounts, and "
               "consider blocking the source." % _join(f["public_sources"])),

    (lambda f: bool(f["cadenced_sources"]),
     lambda f: "Fixed retry intervals were detected (%s). On the machines concerned, check "
               "scheduled tasks, services set to log on as this account, mapped drives, "
               "stored credentials, and any application holding a saved password."
               % _join("%s: %s" % (label, desc) for label, desc in f["cadenced_sources"])),

    (lambda f: bool(f["burst_sources"]) and not f["cadenced_sources"],
     lambda f: "Some sources fire several attempts within a minute (%s), which is a client "
               "retry loop rather than someone typing. Look for a process that reconnects "
               "immediately after each rejection." % _join(f["burst_sources"])),

    (lambda f: bool(f["shared_sources"]),
     lambda f: "These sources produce both successes and failures: %s. That pattern fits a "
               "partially updated machine - one process picked up the new password while "
               "another still holds the old one - or a person mistyping."
               % _join(f["shared_sources"])),

    (lambda f: f["protocols"] == {"ntlm"} and f["bad_count"] > 0,
     lambda f: "All failures are NTLM (4776) rather than Kerberos. That points at a "
               "non-domain-joined client, a legacy application, a mapped drive by IP "
               "address, or a device authenticating through a gateway."),

    (lambda f: f["protocols"] == {"kerberos"} and f["bad_count"] > 0,
     lambda f: "All failures are Kerberos pre-authentication (4771), so they come from "
               "domain-joined machines. The client IP in each event is the place to start."),

    (lambda f: f["machine_account"],
     lambda f: "Failures involve a computer account (name ending in $). This usually means "
               "a broken secure channel rather than a user credential - test with "
               "Test-ComputerSecureChannel and repair or rejoin the machine."),

    # --- configuration problems visible in the data ---------------------------
    (lambda f: f["wrong_name_format"],
     lambda f: "Status 0xC0000064 appears for an account that clearly exists, meaning the "
               "name is submitted in a format the authentication path cannot resolve "
               "(commonly a UPN sent over NTLM). It does not cause lockouts, but it doubles "
               "the noise and hides the real failures."),

    (lambda f: f["clock_skew"] > 0,
     lambda f: "%d event(s) indicate clock skew between a client and the DC. Time drift "
               "produces authentication failures that look like bad passwords - check time "
               "synchronisation before chasing credentials." % f["clock_skew"]),

    (lambda f: f["expired_password"],
     lambda f: "At least one failure is an expired password rather than a wrong one. A "
               "client that never prompts the user to change it will keep retrying the old "
               "value indefinitely."),

    (lambda f: f["restriction"],
     lambda f: "Some failures are policy restrictions (logon hours or permitted "
               "workstations) rather than wrong credentials. Review those attributes on the "
               "account before treating this as a credential problem."),

    # --- process and follow-up ------------------------------------------------
    (lambda f: bool(f["dominant_source"]),
     lambda f: "One source accounts for %.0f%% of all bad passwords (%s). Fixing that one "
               "should stop most lockouts on its own."
               % (f["dominant_source"][1] * 100, f["dominant_source"][0])),

    (lambda f: f["multi_source_windows"] > 0,
     lambda f: "In %d lockout window(s) the attempts came from more than one source. They "
               "share a single badPwdCount, so fixing one source may reduce the frequency "
               "without stopping the lockouts." % f["multi_source_windows"]),

    (lambda f: f["relocks"] > 0 and not f["password_set_events"],
     lambda f: "The account was unlocked %d time(s) and locked again shortly afterwards, "
               "with no password change or reset in the log. Unlocking treats the symptom; "
               "the credential source has to be corrected or the password reset everywhere "
               "it is stored." % f["relocks"]),

    (lambda f: bool(f["password_set_events"]),
     lambda f: "The password was changed or reset during the captured period (%s). Compare "
               "that moment with when the failures start - anything still offering the old "
               "value is the thing to fix."
               % _join(e["ts"].strftime("%Y-%m-%d %H:%M") for e in f["password_set_events"])),

    (lambda f: bool(f["ip_only_sources"]),
     lambda f: "Some sources are recorded only as an IP address (%s), because Kerberos "
               "events carry no workstation name. Resolve them through DHCP leases, DNS or "
               "the switch's MAC table, and bear in mind the address may belong to a NAT "
               "device shared by many clients." % _join(f["ip_only_sources"])),

    (lambda f: f["unnamed_sources"] > 0,
     lambda f: "%d attempt(s) carry neither a workstation name nor an IP address. Those "
               "usually arrive over a channel that does not record the origin; the "
               "receiving service's own log is the only way to attribute them."
               % f["unnamed_sources"]),

    (lambda f: bool(f["callers"]),
     lambda f: "Start on the caller machines named in the 4740 events (%s). Useful there: "
               "schtasks /query /fo LIST /v, services.msc, cmdkey /list, net use, and the "
               "local Security log filtered to event 4625." % _join(f["callers"])),

    (lambda f: f["locked_attempts"] > 0,
     lambda f: "%d attempt(s) were made while the account was already locked. If those come "
               "from the user's own machine they are just the user retrying; if they come "
               "from elsewhere, that source is worth explaining."
               % f["locked_attempts"]),

    (lambda f: f["peak_per_day"] >= 50,
     lambda f: "Up to %d bad passwords were recorded in a single day. At that rate the "
               "account will lock repeatedly whatever the threshold is, and the source "
               "should be disconnected rather than merely investigated." % f["peak_per_day"]),
]


def derive_leads(findings):
    leads = []
    for condition, message in LEAD_RULES:
        try:
            if condition(findings):
                leads.append(message(findings))
        except Exception:                     # a broken rule must not kill the report
            continue
    if not leads:
        leads.append("Nothing beyond the findings above stood out. If lockouts continue, "
                     "capture a longer period, add the other domain controllers, or enable "
                     "Netlogon logging on the suspected clients.")
    return leads


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(events, stats, account, tz, tz_label, window_minutes, max_sources, out=sys.stdout):
    emit = lambda *args: print(*args, file=out)
    window = timedelta(minutes=window_minutes)
    # The failure that trips the lockout is often written a few milliseconds after 4740.
    grace = timedelta(seconds=5)

    owned = [e for e in events if e["role"] == "target"]
    lockouts = [e for e in owned if e["event_id"] == "4740"]
    unlocks = [e for e in owned if e["event_id"] == "4767"]
    bad = [e for e in owned if is_bad_password(e)]
    locked_attempts = [e for e in owned if is_locked_attempt(e)]
    admin_actions = [e for e in events if e["event_id"] in ADMIN_EIDS]

    emit("=" * 78)
    emit("ACCOUNT LOCKOUT ANALYSIS: %s" % account)
    emit("=" * 78)
    emit("Records scanned    : %d" % stats["records"])
    emit("Records for account: %d" % stats["matched"])
    if stats["first"]:
        emit("Event range        : %s - %s (%s)"
             % (stamp(stats["first"], tz), stamp(stats["last"], tz), tz_label))
    emit("Source DCs         : %s"
         % (", ".join("%s (%d)" % (k, v) for k, v in stats["computers"].most_common(5)) or "-"))
    sids = sorted({e["sid"] for e in owned if e["sid"]})
    if sids:
        emit("SID                : %s" % ", ".join(sids))
    emit("")

    if not owned:
        emit("No events found for this account.")
        emit("Check the sAMAccountName spelling, or retry with --contains for a loose match.")
        return

    # -- 1. lockouts ---------------------------------------------------------
    emit("-" * 78)
    emit("1) LOCKOUTS (event 4740): %d" % len(lockouts))
    emit("-" * 78)
    if not lockouts:
        emit("No lockout recorded in this log. Either the account never locked, or the log")
        emit("is not from the PDC emulator - 4740 is written only there.")
    else:
        emit("%-21s  %-24s  %s" % ("time (%s)" % tz_label, "caller computer", "logged on"))
        for event in lockouts:
            emit("%-21s  %-24s  %s"
                 % (stamp(event["ts"], tz), event["domain"] or "(not recorded)", event["dc"]))
        emit("")
        emit("By caller:")
        for caller, count in Counter(e["domain"] or "(not recorded)" for e in lockouts).most_common():
            emit("   %-26s %d" % (caller, count))
        emit("")
        emit("Note: the caller name is the machine that submitted the failing credential.")
        emit("For attempts relayed by a proxy or RADIUS server it names that server, not")
        emit("the device that actually holds the wrong password.")
    emit("")

    # -- 2. administrative activity -----------------------------------------
    emit("-" * 78)
    emit("2) UNLOCKS AND OTHER CHANGES TO THE ACCOUNT")
    emit("-" * 78)
    if not admin_actions:
        emit("None recorded.")
    for event in admin_actions:
        emit("%-21s  %-5s %-32s target=%s  by=%s"
             % (stamp(event["ts"], tz), event["event_id"],
                EVENT_NAMES.get(event["event_id"], ""), event["target"],
                event["subject"] or "(system)"))
    emit("")

    # -- 3. sources of bad passwords ----------------------------------------
    emit("-" * 78)
    emit("3) SOURCES OF BAD PASSWORDS (%d attempts total)" % len(bad))
    emit("-" * 78)
    if not bad:
        emit("No bad-password events for this account in the supplied logs. If the account")
        emit("did lock out, the failures were served by a DC whose log is missing here.")
    else:
        groups = defaultdict(list)
        for event in bad:
            groups[source_label(event)].append(event)
        ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        for label, group in ordered[:max_sources]:
            times = sorted(e["ts"] for e in group)
            emit("")
            emit("### %s - %d attempts" % (label, len(group)))
            emit("    first: %s   last: %s" % (stamp(times[0], tz), stamp(times[-1], tz)))
            name_forms = Counter(e["target"] for e in group)
            emit("    name forms used: %s"
                 % ", ".join("%s (%d)" % (k, v) for k, v in name_forms.most_common(4)))
            kinds = Counter("%s/%s" % (e["event_id"], e["status"] or e["substatus"]) for e in group)
            emit("    failure types: %s"
                 % ", ".join("%s = %s (%d)"
                             % (k, status_text(k.split("/")[0], k.split("/")[1]), v)
                             for k, v in kinds.most_common(3)))
            cadence = detect_cadence(times)
            if cadence:
                emit("    CADENCE: %s in %.0f%% of gaps -> automated retry, not a person typing"
                     % (cadence[2], cadence[1] * 100))
            profile = activity_profile(times, tz)
            if profile:
                emit("    activity: %s" % profile)
            days = Counter(t.astimezone(tz).date() for t in times)
            counts = sorted(days.values())
            emit("    active days: %d, median attempts/day: %d, peak %d on %s"
                 % (len(days), counts[len(counts) // 2],
                    max(days.values()), max(days, key=days.get)))
            if looks_like_proxy(label):
                emit("    NOTE: this name looks like an authentication proxy or RADIUS server.")
                emit("    Correlate these timestamps with that system's own logs to identify")
                emit("    the client behind it (e.g. MAC address or calling station ID).")
        if len(ordered) > max_sources:
            emit("")
            emit("... and %d further sources with fewer attempts (raise --max-sources to see them)."
                 % (len(ordered) - max_sources))

        # Failures that do not increment badPwdCount but often reveal misconfiguration.
        other = [e for e in owned
                 if e["event_id"] in FAILURE_EIDS and not is_bad_password(e)
                 and not is_locked_attempt(e) and e["status"] not in ("0x0", "")]
        if other:
            emit("")
            emit("### Other failure states (do not count towards the lockout threshold)")
            counter = Counter((e["event_id"], e["status"], source_label(e), e["target"])
                              for e in other)
            for (event_id, status, label, name), count in counter.most_common(10):
                emit("    %5d  %s %s = %s  [%s, name '%s']"
                     % (count, event_id, status, status_text(event_id, status), label, name))
    emit("")

    # -- 4. what preceded each lockout ---------------------------------------
    if lockouts:
        emit("-" * 78)
        emit("4) WHAT PRECEDED EACH LOCKOUT (%d minute window)" % window_minutes)
        emit("-" * 78)
        window_counts = []
        for lockout in lockouts:
            preceding = [e for e in bad if -grace <= lockout["ts"] - e["ts"] <= window]
            window_counts.append(len(preceding))
            emit("")
            emit(">>> LOCKOUT %s   (caller: %s)"
                 % (stamp(lockout["ts"], tz), lockout["domain"] or "?"))
            if not preceding:
                emit("    No bad-password events in the window. Try a longer --window, or add")
                emit("    the logs from the other domain controllers.")
            for event in preceding[-12:]:
                emit("    %s  %s %-10s %-30s %s"
                     % (stamp(event["ts"], tz), event["event_id"],
                        event["status"] or event["substatus"], source_label(event),
                        event["target"]))
            emit("    total in window: %d bad-password attempts" % len(preceding))
            sources = Counter(source_label(e) for e in preceding)
            if len(sources) > 1:
                emit("    Attempts from %d different sources add up to the same counter: %s"
                     % (len(sources), ", ".join("%s (%d)" % (k, v) for k, v in sources.most_common())))
        observed = sorted(c for c in window_counts if c > 0)
        if observed:
            emit("")
            emit("Threshold estimate: at least %d bad attempts (median %d) within %d minutes."
                 % (observed[0], observed[len(observed) // 2], window_minutes))
            emit("This is a lower bound - badPwdCount only resets after the observation window,")
            emit("so if the number looks too low, rerun with a larger --window.")
            emit("Confirm with: Get-ADDefaultDomainPasswordPolicy | fl LockoutThreshold,LockoutObservationWindow")
        emit("")

    # -- 5. successful authentications, for contrast -------------------------
    successful = [e for e in owned
                  if (e["event_id"] in ("4768", "4776") and e["status"] == "0x0")
                  or (e["event_id"] == "4624" and e["logon_type"] in ("2", "3", "7", "10", "11"))]
    if successful:
        emit("-" * 78)
        emit("5) WHERE THE ACCOUNT AUTHENTICATES SUCCESSFULLY (for contrast)")
        emit("-" * 78)
        success_sources = Counter(source_label(e) for e in successful)
        for label, count in success_sources.most_common(10):
            emit("   %-42s %d" % (label, count))
        never_succeeded = {source_label(e) for e in bad} - set(success_sources)
        if never_succeeded:
            emit("")
            emit("   Sources that never produced a successful authentication - these are the")
            emit("   ones holding an outdated credential:")
            for label in sorted(never_succeeded):
                emit("     - %s" % label)
        emit("")

    # -- 6. current state -----------------------------------------------------
    emit("-" * 78)
    emit("6) CURRENT STATE")
    emit("-" * 78)
    anchor, anchor_label = None, ""
    if unlocks:
        anchor, anchor_label = max(e["ts"] for e in unlocks), "the last unlock"
    elif lockouts:
        anchor, anchor_label = max(e["ts"] for e in lockouts), "the last lockout"
    if anchor:
        since = [e for e in bad if e["ts"] > anchor]
        emit("Since %s (%s): %d further bad-password attempts."
             % (stamp(anchor, tz), anchor_label, len(since)))
        if since:
            for label, count in Counter(source_label(e) for e in since).most_common():
                latest = max(e["ts"] for e in since if source_label(e) == label)
                emit("   %-42s %d  (latest %s)" % (label, count, stamp(latest, tz)))
            emit("")
            emit("=> The cause is still active; the account is likely to lock again.")
        else:
            emit("=> Quiet since then.")
    else:
        emit("No lockout or unlock in this log, so there is no reference point for a")
        emit("'still happening' check.")
    if locked_attempts:
        emit("")
        emit("Attempts made while the account was already locked: %d" % len(locked_attempts))
        emit("(commonly the user themselves discovering they cannot sign in)")
    emit("Last event for this account in the log: %s"
         % stamp(max(e["ts"] for e in owned), tz))
    emit("")

    # -- 7. leads to follow ---------------------------------------------------
    emit("-" * 78)
    emit("7) LEADS TO FOLLOW")
    emit("-" * 78)
    emit("Derived from this log only. Each item fired because a specific condition was met")
    emit("in the data above; they are starting points, not conclusions.")
    emit("")
    findings = build_findings(owned, events, stats, lockouts, unlocks, bad,
                              locked_attempts, window_minutes, tz, account)
    for index, lead in enumerate(derive_leads(findings), 1):
        emit("%d. %s" % (index, lead))
    emit("")


def export_csv(events, path, tz):
    """Writes every matched event, so the timeline can be cross-referenced elsewhere."""
    columns = ["time_utc", "time_local", "event_id", "event_name", "account", "role",
               "status", "status_meaning", "workstation", "ip",
               "target_domain_or_4740_caller", "logon_type", "dc", "record_id",
               "counts_as_bad_password"]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(columns)
        for event in events:
            status = event["status"] or event["substatus"]
            writer.writerow([
                event["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                stamp(event["ts"], tz),
                event["event_id"],
                EVENT_NAMES.get(event["event_id"], ""),
                event["target"] or event["subject"],
                event["role"],
                status,
                status_text(event["event_id"], status) if status else "",
                event["workstation"],
                event["ip"],
                event["domain"],
                event["logon_type"],
                event["dc"],
                event["record_id"],
                "yes" if is_bad_password(event) else "",
            ])


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Analyse Active Directory account lockouts from Security EVTX logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python3 %(prog)s -u jsmith logs.zip --tz +02:00 --window 90 --csv events.csv")
    parser.add_argument("logs", nargs="+",
                        help="one or more .evtx files, or .zip archives containing them")
    parser.add_argument("-u", "--user", required=True,
                        help="sAMAccountName of the account to analyse")
    parser.add_argument("--window", type=int, default=60,
                        help="minutes before each lockout to correlate failures in "
                             "(default 60; set it at or above the domain observation window)")
    parser.add_argument("--tz", default="UTC",
                        help="time zone for displayed timestamps, e.g. +02:00 (default UTC)")
    parser.add_argument("--contains", action="store_true",
                        help="loose name match, so related accounts such as adm-<name> or "
                             "<name>$ are included")
    parser.add_argument("--max-sources", type=int, default=15,
                        help="how many bad-password sources to detail (default 15)")
    parser.add_argument("--csv", metavar="FILE", help="export all matched events to CSV")
    parser.add_argument("--report", metavar="FILE", help="also write the report to a text file")
    args = parser.parse_args()

    tz, tz_label = parse_tz(args.tz)

    with tempfile.TemporaryDirectory() as tmpdir:
        paths = resolve_inputs(args.logs, tmpdir)
        print("Reading %d file(s), looking for '%s' ..." % (len(paths), args.user),
              file=sys.stderr)
        events, stats = collect(paths, args.user, "contains" if args.contains else "exact")

        report(events, stats, args.user, tz, tz_label, args.window, args.max_sources)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as handle:
                report(events, stats, args.user, tz, tz_label, args.window,
                       args.max_sources, out=handle)
            print("Report written to %s" % args.report, file=sys.stderr)
        if args.csv:
            export_csv(events, args.csv, tz)
            print("CSV written to %s (%d events)" % (args.csv, len(events)), file=sys.stderr)


if __name__ == "__main__":
    main()
