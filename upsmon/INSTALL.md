# upsmon on AlmaLinux 9 — complete installation guide

Every command, what it should print, and how to confirm each step worked before
moving on. Written for a clean AlmaLinux 9 machine with SELinux enforcing and
firewalld running, which is the default. About twenty minutes end to end.

```
   Synology NAS                       AlmaLinux 9
  ┌──────────────┐                 ┌────────────────────────────────────────┐
  │ UPS over USB │                 │ upsmon.service   (user "upsmon")       │
  │      ↓       │    TCP 3493     │   polls every 10 s                     │
  │ upsd (NUT)   │ ───────────────►│   writes /var/lib/upsmon/history.db    │
  └──────────────┘                 │   serves 127.0.0.1:9848, token-guarded │
                                   │        ↑ unix socket                   │
                                   │ php-fpm ← nginx :443 ← your browser     │
                                   └────────────────────────────────────────┘
```

Nothing in the web tier holds UPS credentials or touches the UPS. The daemon owns
the connection; PHP only relays JSON. Neither runs as root.

**Contents**

1. [Prepare the NAS](#1-prepare-the-nas)
2. [Check the network path](#2-check-the-network-path)
3. [Install packages](#3-install-packages)
4. [Create the daemon user](#4-create-the-daemon-user)
5. [Install the daemon](#5-install-the-daemon)
6. [Configure it](#6-configure-it)
7. [First test, before any service](#7-first-test-before-any-service)
8. [Start the service](#8-start-the-service)
9. [Install the web interface](#9-install-the-web-interface)
10. [Configure php-fpm](#10-configure-php-fpm)
11. [Configure nginx with TLS](#11-configure-nginx-with-tls)
12. [SELinux](#12-selinux)
13. [Firewall](#13-firewall)
14. [Open the dashboard](#14-open-the-dashboard)
15. [Enable control actions](#15-enable-control-actions-optional)
16. [Day-to-day use](#16-day-to-day-use)
17. [Troubleshooting](#17-troubleshooting)
18. [Maintenance](#18-maintenance)

Files you should have to hand:

```
upsmon.py
web/index.php
web/upsmon-api.php
deploy/upsmon.service
deploy/config.json
deploy/nginx-upsmon.conf
```

---

## 1. Prepare the NAS

In DSM: **Control Panel → Hardware & Power → UPS**

* tick **Enable UPS support**
* tick **Enable network UPS server**
* click **Permitted DiskStation Devices** and add the IP address of the
  AlmaLinux machine, one per line
* **Apply**

That is everything monitoring needs. Control actions — self tests, the beeper,
writable settings — need one more step on the NAS, covered in
[section 15](#15-enable-control-actions-optional). You can come back to it.

Note the NAS address. Throughout this guide it is `192.168.40.253`; replace it
with yours everywhere it appears.

---

## 2. Check the network path

Before installing anything, confirm this machine can actually reach upsd.

```bash
sudo dnf install -y nmap-ncat
nc -zv 192.168.40.253 3493
```

Expected:

```
Ncat: Connected to 192.168.40.253:3493.
```

If it hangs or is refused, stop here — nothing later will work. The usual causes,
in order of likelihood:

* the AlmaLinux IP is not in the permitted devices list on the NAS
* only **Enable UPS support** was ticked, not **Enable network UPS server**
* the DSM firewall is blocking 3493

You can go further and ask upsd what it has, still without installing anything:

```bash
printf 'LIST UPS\nLOGOUT\n' | nc 192.168.40.253 3493
```

Expected:

```
BEGIN LIST UPS
UPS ups "Description unavailable"
END LIST UPS
OK Goodbye
```

The name in that reply — normally `ups` on Synology — is what goes into
`ups_name` later.

---

## 3. Install packages

```bash
sudo dnf install -y python3 nginx php-fpm php-cli policycoreutils-python-utils
```

| package | why |
|---|---|
| `python3` | the daemon. AlmaLinux 9 ships 3.9, which is enough: the daemon uses only the standard library, so there is nothing to `pip install` and no virtualenv to maintain |
| `nginx` | serves the dashboard |
| `php-fpm` | runs the PHP. Version 8.0 from AppStream; the page needs no extensions beyond the defaults |
| `php-cli` | lets you run `php -l` to syntax-check the pages |
| `policycoreutils-python-utils` | provides `semanage` and `audit2allow` for the SELinux step |

Confirm what you got:

```bash
python3 --version && php --version | head -1 && nginx -v
```

```
Python 3.9.21
PHP 8.0.30 (cli) (built: ...)
nginx version: nginx/1.20.1
```

Syntax-check the two PHP files now, while it is cheap:

```bash
php -l web/index.php && php -l web/upsmon-api.php
```

```
No syntax errors detected in web/index.php
No syntax errors detected in web/upsmon-api.php
```

---

## 4. Create the daemon user

The daemon needs no privileges — it opens one TCP connection and writes one
directory. Give it its own unprivileged account.

```bash
sudo useradd --system --home-dir /var/lib/upsmon --shell /sbin/nologin upsmon
id upsmon
```

```
uid=986(upsmon) gid=983(upsmon) groups=983(upsmon)
```

The `upsmon` **group** matters later: it is how php-fpm gets permission to read
the API token.

---

## 5. Install the daemon

```bash
sudo install -d -m 755 /usr/local/lib/upsmon
sudo install -m 755 upsmon.py /usr/local/lib/upsmon/upsmon.py
sudo install -d -m 750 -o upsmon -g upsmon /var/lib/upsmon
```

A wrapper so you can type `upsmon` from anywhere:

```bash
sudo tee /usr/local/bin/upsmon > /dev/null <<'EOF'
#!/bin/bash
exec /usr/bin/python3 /usr/local/lib/upsmon/upsmon.py "$@"
EOF
sudo chmod +x /usr/local/bin/upsmon
```

Check:

```bash
upsmon --version
```

```
upsmon 3.4.1
```

---

## 6. Configure it

```bash
sudo install -d -m 755 /etc/upsmon
sudo install -m 640 -o root -g upsmon deploy/config.json /etc/upsmon/config.json
sudo vi /etc/upsmon/config.json
```

Mode `640` owned `root:upsmon` means the daemon can read it, root can edit it,
and nothing else can see it. That matters once you put upsd credentials in it.

**Get the ownership right, and check it.** A `cp` instead of that `install` line
leaves the file `root:root`, the daemon cannot read it, and from version 3.4.4 it
refuses to start rather than silently falling back to defaults and looking for a
UPS on 127.0.0.1:

```bash
ls -l /etc/upsmon/config.json
sudo -u upsmon cat /etc/upsmon/config.json | head -3
```

```
-rw-r-----. 1 root upsmon 612 ... /etc/upsmon/config.json
{
  "nut_host": "192.168.40.253",
```

If that `cat` says `Permission denied`:

```bash
sudo chown root:upsmon /etc/upsmon/config.json
sudo chmod 640 /etc/upsmon/config.json
sudo chmod 755 /etc/upsmon
```

The only thing you must change is `nut_host`. Here is a complete file with every
option present:

```json
{
  "nut_host": "192.168.40.253",
  "nut_port": 3493,
  "ups_name": "ups",
  "nut_username": "",
  "nut_password": "",
  "nut_timeout": 5.0,

  "api_host": "127.0.0.1",
  "api_port": 9848,
  "token_file": "/run/upsmon/api-token",

  "poll_interval": 10,
  "sample_interval": 60,
  "sample_interval_battery": 10,
  "aggregate_interval": 3600,

  "retain_full_days": 14,
  "retain_hourly_days": 730,
  "retain_events_days": 730,

  "charge_warn": 50,
  "charge_crit": 25,
  "load_warn": 70,
  "load_crit": 90,
  "runtime_warn_s": 300,
  "runtime_crit_s": 120,
  "battery_life_years": 4.0,

  "allow_dangerous_commands": false,
  "pin": "1234",
  "pin_attempts": 5,
  "pin_lockout_s": 300
}
```

Anything you leave out keeps its default, so the file only needs the settings you
actually change. An unknown key is reported rather than silently ignored — check
`upsmon --diag`, which prints the settings in force.

### What each option does

**Connection**

| option | meaning |
|---|---|
| `nut_host`, `nut_port` | where upsd is: the NAS, port 3493 |
| `ups_name` | which UPS, when the server publishes more than one. Synology always calls its own `ups`. Leave it `""` to take whichever is listed first |
| `nut_username`, `nut_password` | only needed for control actions, section 15. Leave empty for monitoring |
| `nut_timeout` | how long to wait for a reply before the poll counts as failed |

**Our own API**

| option | meaning |
|---|---|
| `api_host` | keep this on `127.0.0.1`. The API can run commands on the UPS; it has no business listening on the network |
| `api_port` | change only if something already uses 9848 |
| `log_file` | the daemon's own log. Empty leaves everything to the journal |
| `log_level` | `info`, or `debug` for poll-by-poll detail |
| `log_to_journal` | whether to also write to stdout, which systemd captures. Leave it on: startup failures happen before the log file is open |
| `token_file` | where the shared secret for privileged calls is written. `/run` is cleared at every boot, which is why it lives there. If SELinux objects, section 12 has an alternative |

**Timing**

| option | meaning |
|---|---|
| `poll_interval` | how often the UPS is read. 10 s is plenty — the NUT driver on the NAS only refreshes its own cache every 5 s |
| `sample_interval` | how often a row goes into the database. 60 s gives good charts at a modest size |
| `sample_interval_battery` | the same while the UPS is discharging. Deliberately much faster: a real power cut is the one event worth having in detail |
| `aggregate_interval` | how often old samples are rolled into hourly averages. Hourly is right; no reason to change it |

**Retention**

| option | meaning |
|---|---|
| `retain_full_days` | how long full-resolution samples survive before being averaged into hours |
| `retain_hourly_days` | how long those hourly rows are kept. 730 is two years |
| `retain_events_days` | the same for the event log |

**Thresholds** — these affect only colour and the health verdict. They never
change anything on the UPS.

| option | meaning |
|---|---|
| `charge_warn`, `charge_crit` | battery percentage at which the dashboard turns amber, then red |
| `load_warn`, `load_crit` | load percentage, same idea, inverted |
| `runtime_warn_s`, `runtime_crit_s` | remaining runtime in seconds, applied only while on battery |
| `battery_life_years` | expected battery life, used to age `battery.date` or `battery.mfr.date` when the UPS reports one. 4 suits sealed lead-acid |

**Smart plug (optional)**

A Shelly Gen2+ plug can be watched alongside the UPS, which is worth doing when
the UPS itself does not measure the watts being drawn. Leave `plug_host` empty
and none of this happens.

| option | meaning |
|---|---|
| `plug_host` | the plug's IP address. Empty disables everything below |
| `plug_password` | only if you set one in the plug's web UI. The username is always `admin` and cannot be changed |
| `plug_switch_id` | which relay; single-socket plugs are always 0 |
| `plug_poll_interval` | how often the plug is read |
| `plug_timeout` | how long to wait for the plug before giving up on a request |
| `plug_min_request_gap` | the smallest gap between two requests to the plug. Gen3 firmware with authentication on answers 429 when they arrive closer together than about a second; 0 disables the pacing |
| `plug_sample_interval` | how often a reading goes into the database |

The local RPC API stays available while the plug is signed in to the Shelly app,
connected to Shelly Cloud and commissioned into Apple Home over Matter. Nothing
here interferes with any of that.

**Pushed sensor readings (optional)**

Battery sensors such as the Shelly H&T sleep between measurements, so nothing
can poll them — they push instead.

| option | meaning |
|---|---|
| `sensor_host` | address the sensor directly. A battery H&T is asleep most of the time so this usually fails, which is harmless — on USB power it answers every poll and fills the charts without any webhook |
| `sensor_password` | if one is set on the sensor |
| `sensor_listen` | switch the listener on |
| `sensor_listen_host` | which address to listen on. `0.0.0.0` for every interface, or pin it to one |
| `sensor_listen_port` | which port |
| `sensor_retain_days` | how long pushed readings are kept |

Create the webhook on the sensor once, pointing at this machine:

```bash
curl -X POST http://<sensor-ip>/rpc -H 'Content-Type: application/json' -d '{
  "id":1,"method":"Webhook.Create","params":{
    "cid":0,"enable":true,"event":"temperature.change",
    "urls":["http://<this-host>:8088/?t=${ev.tC}"]}}'

curl -X POST http://<sensor-ip>/rpc -H 'Content-Type: application/json' -d '{
  "id":1,"method":"Webhook.Create","params":{
    "cid":0,"enable":true,"event":"humidity.change",
    "urls":["http://<this-host>:8088/?rh=${ev.rh}"]}}'
```

`t`, `tC`, `temperature`, `rh`, `humidity`, `bv`, `bp` and `rssi` are all
understood, in the query string or in a JSON body. A push carrying none of them
is rejected with 400 rather than stored as an empty row.

Open the port if the sensor is on another subnet:

```bash
sudo firewall-cmd --permanent --add-port=8088/tcp
sudo firewall-cmd --reload
```

**Safety**

| option | meaning |
|---|---|
| `allow_dangerous_commands` | permits the commands that switch the UPS outlets off — `load.off`, `shutdown.stayoff`, `bypass.start` and relatives. Off by default, and the dashboard greys them out while it is off |
| `pin` | four digits asked for before anything is written or run. Empty disables it entirely |
| `pin_attempts` | wrong tries allowed before the lockout starts (5) |
| `pin_lockout_s` | how long nothing is accepted after that (300 = five minutes) |

**About the PIN.** Four digits is 10 000 combinations, so treat it as a guard
against a misclick or a passer-by at an unlocked screen, not as a password — the
address restriction and TLS are what keep strangers out. What makes it worth
having is the lockout: five wrong tries and the daemon accepts nothing for five
minutes, which turns guessing into weeks of work and fills the event log while
it happens. Every attempt, right or wrong, is recorded.

It is checked by the daemon, never by the page, so the PIN itself never reaches
the browser — only the fact that one is required.

Any option can also come from the environment with a `UPSMON_` prefix, which
takes precedence over the file — handy for a one-off test:

```bash
UPSMON_NUT_HOST=10.0.0.5 upsmon --status
```

---

## 7. First test, before any service

This talks to the UPS directly: no service, no web server, no token. If it works,
the hard part is behind you.

```bash
sudo -u upsmon upsmon --status
```

You should get a full report — status line, battery, runtime, load, voltages,
then every variable the UPS publishes, grouped and described.

**Always use `sudo -u upsmon`, never plain `sudo`, for any command that touches
the database** — `--oneshot`, `--history`, `--events`. Running one of those as
root creates `/var/lib/upsmon/history.db` owned by root, and the service then
fails to start with SQLite's misleading *"attempt to write a readonly
database"*. Section 17 has the one-line fix if it happens.

If it fails:

```bash
sudo -u upsmon upsmon --diag
```

That prints the settings actually in force, so you can see whether your edit was
picked up, plus how long upsd took to answer and what it said. `cannot reach
upsd` here almost always means section 2 was not really passing.

---

## 8. Start the service

```bash
sudo install -m 644 deploy/upsmon.service /etc/systemd/system/upsmon.service
sudo systemctl daemon-reload
sudo systemctl enable --now upsmon
systemctl status upsmon --no-pager
```

```
● upsmon.service - UPS monitoring daemon (NUT poller, history, localhost API)
     Loaded: loaded (/etc/systemd/system/upsmon.service; enabled)
     Active: active (running) since ...
   Main PID: 12345 (python3)
```

The unit is deliberately locked down: `ProtectSystem=strict`, no new privileges,
a system-call filter, and a 15% CPU cap it will never approach. It gets exactly
two writable places — `/var/lib/upsmon` via `StateDirectory`, and `/run/upsmon`
via `RuntimeDirectory`, which systemd recreates at every boot.

Now the built-in health check:

```bash
upsmon --check
```

```
  ok    daemon API reachable               http://127.0.0.1:9848
  ok    recent contact with upsd           last reading 3s ago
  ok    no polling errors
  ok    history is being written           5 samples, 0 hourly rows
  ok    database size sane                 0.1 MB
  ok    token file present                 /run/upsmon/api-token

  everything looks healthy
```

`history is being written` may fail for the first minute — that is just the first
`sample_interval` not having elapsed. Wait and run it again.

To watch it work:

```bash
journalctl -u upsmon -f
```

---

### Logging

Everything goes to the journal, and from version 3.4.4 also to
`/var/log/upsmon/upsmon.log`. The directory is created by systemd through
`LogsDirectory=`, owned by the `upsmon` user, so nothing needs setting up by
hand.

```bash
tail -f /var/log/upsmon/upsmon.log
journalctl -u upsmon -f          # the same lines, through systemd
```

Install the rotation rules:

```bash
sudo install -m 644 deploy/upsmon.logrotate /etc/logrotate.d/upsmon
sudo logrotate -d /etc/logrotate.d/upsmon      # dry run, changes nothing
```

Daily, fourteen kept, compressed. At a poll every ten seconds the daemon only
writes when something happens, so this is a few hundred kilobytes a week.

The rules end with `systemctl kill -s HUP upsmon.service`. That matters:
renaming a file does not disturb a process that has it open, so without the
signal every further line would go into the rotated copy. On the signal the
daemon reopens the file, and it also notices on its own within one poll if a
rotation happened without the signal arriving.

To force a rotation and watch it work:

```bash
sudo logrotate -f /etc/logrotate.d/upsmon
ls -l /var/log/upsmon/
head -1 /var/log/upsmon/upsmon.log
```

The new file should begin with `log file reopened after rotation`.

To turn the file off and keep only the journal, set `"log_file": ""` and
restart.

## 9. Install the web interface

```bash
sudo install -d -m 755 /var/www/upsmon
sudo install -m 644 web/index.php           /var/www/upsmon/index.php
sudo install -m 644 web/upsmon-api.php      /var/www/upsmon/upsmon-api.php
sudo install -m 644 web/favicon.svg         /var/www/upsmon/favicon.svg
sudo install -m 644 web/favicon.ico         /var/www/upsmon/favicon.ico
sudo install -m 644 web/favicon-32.png      /var/www/upsmon/favicon-32.png
sudo install -m 644 web/apple-touch-icon.png /var/www/upsmon/apple-touch-icon.png
```

---

## 10. Configure php-fpm

On AlmaLinux 9 the default pool already listens on `/run/php-fpm/www.sock` and
already permits nginx to connect, so the pool file itself needs no edits.

One thing **is** required. php-fpm runs as `apache`, and it has to read the API
token, which the daemon writes mode `0640` owned `upsmon:upsmon`. Put `apache`
into the `upsmon` group:

```bash
sudo usermod -a -G upsmon apache
sudo systemctl enable --now php-fpm
sudo systemctl restart php-fpm
```

The restart is not optional — group membership is read when a process starts, so
php-fpm will not pick it up otherwise.

Verify the whole permission chain in one command:

```bash
sudo -u apache cat /run/upsmon/api-token
```

A long random string means everything downstream will work. `Permission denied`
means the group change has not taken effect: check that `id apache` lists
`upsmon`, and that you really restarted php-fpm.

Confirm the socket exists:

```bash
ls -l /run/php-fpm/www.sock
```

```
srw-rw----. 1 root root 0 ... /run/php-fpm/www.sock
```

---

## 11. Configure nginx with TLS

The dashboard is served over HTTPS on 443; port 80 only redirects there.

### Certificates

The configuration points straight at the three files you already have:

```bash
ls -l /etc/nginx/certs
```

```
-rw-r--r--. 1 root root 1704 ... server.ca
-rw-------. 1 root root 1704 ... server.key
-rw-r--r--. 1 root root 1834 ... server.pem
```

| file | used as | sent to clients |
|---|---|---|
| `server.pem` | `ssl_certificate` | yes, verbatim |
| `server.key` | `ssl_certificate_key` | never |
| `server.ca` | `ssl_trusted_certificate` | no — only used to verify OCSP responses |

Confirm the key and the certificate are a pair. These two hashes must match:

```bash
openssl x509 -noout -modulus -in /etc/nginx/certs/server.pem | openssl md5
openssl rsa  -noout -modulus -in /etc/nginx/certs/server.key | openssl md5
```

(For an elliptic-curve key use `openssl ec` instead of `openssl rsa`.)

Check what the certificate covers and when it expires. The name you browse to
must appear in `subjectAltName`, or the browser objects however good the rest of
the configuration is:

```bash
openssl x509 -in /etc/nginx/certs/server.pem -noout -subject -dates -ext subjectAltName
```

One thing worth knowing rather than acting on: nginx serves `server.pem` exactly
as it finds it, so the chain a browser receives is whatever that file contains.

```bash
grep -c "BEGIN CERTIFICATE" /etc/nginx/certs/server.pem
```

`2` or more means the intermediates are in there and every client will be happy.
`1` means only the leaf is sent, which works on any machine that already holds
the intermediate — typically the case for an internal CA whose root is
distributed to your own machines — and fails elsewhere with "certificate not
trusted". Nothing needs changing unless you hit that.

Lock the permissions down. nginx reads the key as root before dropping
privileges, so 600 is enough:

```bash
sudo chown root:root /etc/nginx/certs/*
sudo chmod 600 /etc/nginx/certs/server.key
sudo chmod 644 /etc/nginx/certs/server.pem /etc/nginx/certs/server.ca
sudo restorecon -Rv /etc/nginx/certs
```

### The server block

```bash
sudo install -m 644 deploy/nginx-upsmon.conf /etc/nginx/conf.d/upsmon.conf
sudo vi /etc/nginx/conf.d/upsmon.conf
```

Three things to change:

1. `server_name` — the hostname on the certificate
2. the `allow` lines — your subnet
3. `resolver` — your own DNS servers, if you would rather not use public ones.
   It is only used for OCSP stapling

The stock AlmaLinux config already has a `default_server` on port 80, and ours
claims that too. Comment the stock one out, or `nginx -t` will refuse with
*"duplicate default server"*:

```bash
sudo vi /etc/nginx/nginx.conf
```

Comment out the whole `server { listen 80 default_server; ... }` block —
everything from `server {` to its closing brace.

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl restart nginx
```

```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

### What the configuration does

**Redirect.** Port 80 answers every name and returns `301` to the same URL over
https. A 301 rather than 302 so browsers remember it and stop making the
plaintext request at all.

**Protocols.** TLS 1.2 and 1.3 only. 1.0 and 1.1 are broken and long deprecated.

**Ciphers.** ECDHE suites exclusively, so every session gets forward secrecy —
recording the traffic today and stealing the key later still reveals nothing.
Because there are no DHE suites, no `dhparam.pem` is needed. TLS 1.3 ignores the
list entirely and uses its own three suites, all of which are fine.

`ssl_prefer_server_ciphers` is deliberately **off**: with only good suites on
offer, the client is better placed to pick the one its hardware accelerates.

**Session tickets are off.** The ticket key sits in worker memory for its
lifetime, and anyone who obtains it can decrypt past sessions — which is exactly
what forward secrecy is supposed to prevent. The session cache gives the same
speed-up without that.

**OCSP stapling** lets nginx present proof that the certificate has not been
revoked, so the browser does not have to ask the CA — faster, and it does not
leak your visitors to the CA. This needs `ssl_trusted_certificate`, which is what
`server.ca` is for.

**Headers.** HSTS for two years, so browsers refuse plaintext to this host at
all. `includeSubDomains` is deliberately absent — add it only if every name under
this domain is also https, otherwise you will lock yourself out of something.
Plus `nosniff`, `X-Frame-Options: DENY`, no referrer, and a Content-Security-Policy
restricted to the page itself. The dashboard loads nothing from anywhere else —
no CDN, no fonts, no analytics — so the policy can be tight. `unsafe-inline` is
present because the page carries its own CSS and JavaScript inline.

**Address restriction stays.** Encryption is not authorisation: TLS protects the
traffic, the `allow` rules decide who may ask at all. The dashboard can switch
off the power to whatever is plugged into the UPS.

**`fastcgi_param HTTPS on`** tells PHP the request arrived over TLS.

### Verify

```bash
curl -sI http://your-server/ | head -3
```

```
HTTP/1.1 301 Moved Permanently
Location: https://your-server/
```

```bash
curl -sI https://your-server/ | grep -Ei "HTTP|strict-transport|content-security"
```

Check the chain the server actually sends — `Verify return code: 0 (ok)` is what
you want:

```bash
openssl s_client -connect your-server:443 -servername your-server < /dev/null 2>&1 \
  | grep -E "Verify return code|Protocol|Cipher|depth"
```

Confirm the old protocols really are refused:

```bash
openssl s_client -connect your-server:443 -tls1_1 < /dev/null 2>&1 | tail -3
```

That should fail, not connect.

## 12. SELinux

AlmaLinux enforces SELinux by default, and two things need permitting.

**1. Let PHP make an outbound TCP connection** to the daemon on 127.0.0.1:9848.
Without this the dashboard reports the daemon as unreachable even though
`upsmon --check` is perfectly happy — a confusing failure worth recognising.

```bash
sudo setsebool -P httpd_can_network_connect 1
getsebool httpd_can_network_connect
```

```
httpd_can_network_connect --> on
```

**2. Label the web root** so nginx and php-fpm may read it:

```bash
sudo semanage fcontext -a -t httpd_sys_content_t "/var/www/upsmon(/.*)?"
sudo restorecon -Rv /var/www/upsmon
```

```
Relabeled /var/www/upsmon/index.php from unconfined_u:object_r:var_t:s0 to
          system_u:object_r:httpd_sys_content_t:s0
```

### If the token read is denied

The token lives in `/run/upsmon`, labelled `var_run_t`. Some policy versions do
not let httpd read it. The symptom is specific: read-only parts of the dashboard
work, but the control buttons return *"the API token is unreadable"*.

Check for the denial:

```bash
sudo ausearch -m AVC -ts recent | grep -i upsmon
```

There are two fixes. Either build a policy module from the actual denial:

```bash
sudo ausearch -m AVC -ts recent | audit2allow -M upsmon-web
sudo semodule -i upsmon-web.pp
```

Or move the token somewhere httpd already reads. In `/etc/upsmon/config.json`:

```json
{ "token_file": "/etc/upsmon/api-token" }
```

and in `/var/www/upsmon/upsmon-api.php`, change the constant near the top to
match:

```php
const UPSMON_TOKEN = '/etc/upsmon/api-token';
```

then restart and re-test:

```bash
sudo systemctl restart upsmon
sudo -u apache cat /etc/upsmon/api-token
```

---

## 13. Firewall

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
sudo firewall-cmd --list-services
```

```
cockpit dhcpv6-client http https ssh
```

Port 80 is open only so the redirect can happen; nothing is served there.

If you had 8080 open from an earlier attempt, close it:

```bash
sudo firewall-cmd --permanent --remove-port=8080/tcp
sudo firewall-cmd --reload
```

Nothing needs opening for the daemon itself: its API is on the loopback only,
and the connection to the NAS is outbound.

## 14. Open the dashboard

```
https://your-server/
```

Before reaching for a browser, you can confirm the whole chain from the shell:

```bash
curl -sk "https://127.0.0.1/index.php?api=health" | head -c 200
```

Expect JSON beginning `{"ok": true, "version": "2.0.0"...`. If that works but the
browser does not, the problem is the `allow` lines or the firewall, not the
stack.

The page has five tabs.

The page refreshes itself every five seconds. The indicator in the top right
shows that, blinks while a request is in flight, and pauses on a click —
useful when you want to read a value without it moving. Polling also stops
while the browser tab is in the background and resumes the moment you return
to it.

Only the snapshot comes every five seconds. The other panels have their own
cadence and are fetched only while their tab is open: events every five
seconds, the overview chart every minute, history and the control lists every
thirty. An idle dashboard therefore costs one small request every five seconds
rather than a year of history on every tick.

**Overview** — status, charge, runtime, load, mains and battery voltage, each
turning amber or red at your thresholds. Then two charts over the last 24 hours — charge with load, and the power being
drawn — both with red bands over any period spent on battery, and both draggable
and zoomable like the ones under History, the device and battery facts, and the
latest events. Refreshes every five seconds.

**History** — for the UPS, six charts from 6 hours to a year: charge, runtime, load, mains
voltage, battery voltage, power drawn. Not every model measures the watts
actually being drawn; when yours does not, that chart is worked out from the
load percentage against the nameplate rating and labelled as an estimate. Recent data comes from full-resolution
samples, older data from the hourly roll-up, so a year-long chart costs a few
hundred points rather than half a million. Underneath, if a plug is configured, ten more from it — power, energy, voltage,
current, frequency, its own temperature, power factor, returned energy, runtime
and switching cycles, plus load runtime, Wi-Fi signal, uptime and free memory —
then whatever a sensor has reported: temperature, humidity, battery percentage,
battery voltage and signal. Last of all, every power failure with its duration
and how far the battery fell.

Every chart is interactive. **Drag** to pan, **scroll** to zoom around the
pointer, **pinch** on a touch screen, **double-click** to go back to the full
range, and hover for the exact reading at that moment. Nothing is re-fetched
while you do it — the data is already in the browser, so it stays smooth on a
phone. The view cannot be zoomed into nothing or dragged off past the data.

**Tests** — self tests, past and present. The verdict of the most recent one
with everything the daemon saw while it ran, buttons to start another, a count
of how many have passed, and a chart of how far the battery voltage sagged in
each test over time. That trend is the useful part: a battery on its way out
still passes a short test, but sags further every time. Underneath, the full
history. Tests the UPS runs on its own schedule are recorded here too, marked
as such.

**Events** — status changes, self-test verdicts, settings changed on the UPS
(including changes made by something other than this dashboard), and every
command run from here.

**Control** — when a plug is configured, a card for it: switch the socket, and
a button for each counter the plug keeps — active energy, returned energy, total
runtime, switching cycles and active load runtime — plus one that clears them
all. These are the same counters the plug's own web interface lists, and each
asks for confirmation and then the PIN. Below that, buttons for whatever
commands your UPS reports, each with a plain
label and an icon rather than the raw NUT name. Rest the pointer on one for five
seconds and a hint gives the exact command name and what it does; on a phone,
press and hold. The delay is `HINT_DELAY_MS` near the top of the page script if
you want it shorter. and one editable
field per writable variable. The field matches what the UPS says it will accept:
a dropdown when it publishes a list of values, a number box with the right limits
when it publishes a range, a date picker for battery dates (written back in
whatever format that UPS uses), and plain text otherwise. Save only lights up once
something has actually changed.

If a PIN is configured, a dialog appears in the middle of the screen with four
boxes — type it, paste it, or use the arrow keys; Escape or a click outside
cancels. A wrong PIN shakes the boxes and lets you try again without losing your
place. Mostly empty until section 15 is done.

**All variables** — everything the driver publishes, grouped and described, with
`rw` marking what can be changed.

---

## 15. Enable control actions (optional)

Monitoring needs nothing more. Changing anything needs a upsd account with the
right permissions, and DSM does not provide one: the built-in `monuser` account
is declared `upsmon slave`, which is monitoring only, so every attempt comes back
`ACCESS-DENIED`.

**On the NAS.** Enable SSH in Control Panel → Terminal & SNMP, then:

```bash
ssh admin@192.168.40.253

sudo tee -a /etc/ups/upsd.users > /dev/null <<'EOT'

[upsadmin]
    password = choose-your-own
    actions = SET
    instcmds = ALL
EOT

sudo synosystemctl restart ups-usb
```

Points worth knowing:

* DSM 6 keeps the file at `/usr/syno/etc/ups/upsd.users`
* older DSM 7 releases use `sudo synoservice --restart ups-usb`
* the restart takes the driver with it, so readings are briefly unavailable
* `actions = SET` permits writable variables, `instcmds = ALL` permits commands —
  grant only the one you need
* the name in brackets has nothing to do with DSM user accounts
* DSM keeps bash history in `/var/tmp/.bash_history` if you would rather the
  password did not sit there

**On the AlmaLinux box.** Put the credentials in the daemon's config:

```bash
sudo vi /etc/upsmon/config.json
```

```json
  "nut_username": "upsadmin",
  "nut_password": "choose-your-own",
```

```bash
sudo systemctl restart upsmon
upsmon --exec beeper.disable
```

```
using the running daemon
EXEC beeper.disable accepted
```

The password never leaves that file: it is not sent to the browser, and the
dashboard's control buttons go through the daemon.

**DSM updates regenerate their UPS configuration and quietly drop this user.** If
control worked and later starts failing with `ACCESS-DENIED`, that is why. Add it
again.

### The commands that cut power

`load.off`, `shutdown.stayoff`, `bypass.start` and their relatives switch the UPS
outlets off, taking down everything plugged in — quite possibly including the
machine serving this page. They are refused by default and shown greyed out. To
allow them:

```json
{ "allow_dangerous_commands": true }
```

The dashboard then asks for confirmation before each one.

---

## 16. Day-to-day use

Everything the web interface does is available from the shell:

**`upsmon` with no arguments starts the daemon.** That is what systemd runs; if
the service is already up, a second one refuses to start and tells you so. Every
command below is safe to run alongside the service.

```bash
upsmon --status                       # full report straight from the UPS
upsmon --status --json                # the same, machine-readable
upsmon --list-rw                      # what this UPS lets you change
upsmon --exec test.battery.start.quick
upsmon --set battery.charge.low=20
upsmon --history 7d                   # recorded samples as a table
upsmon --history 30d --points 40      # coarser
upsmon --events                       # the event log
upsmon --tests                        # the self-test history
upsmon --plug                         # everything the smart plug reports
upsmon --plug-off / --plug-on         # switch the socket
upsmon --plug-reset                   # reset the plug's energy counters
upsmon --sensors                      # readings pushed by battery sensors
```

### Talking to the Shelly devices directly

Everything the standalone Shelly reader could do is available under `shelly`,
addressed by role rather than by IP — the address and password come from the
configuration, so neither has to be typed or pasted into a shell:

```bash
upsmon shelly plug info               # identity and firmware
upsmon shelly plug dump               # every value the device reports
upsmon shelly plug dump --json
upsmon shelly plug poll --interval 5 --csv plug.csv
upsmon shelly plug watch              # pushed by the device, no polling
upsmon shelly plug on | off | toggle
upsmon shelly plug reset-counters              # every counter
upsmon shelly plug reset-counters aenergy     # just the energy total
upsmon shelly plug reboot --wait
upsmon shelly plug ble status
upsmon shelly plug matter off --reboot
upsmon shelly plug call Switch.GetStatus '{"id":0}'

upsmon shelly sensor dump             # the thermometer, while it is awake
upsmon shelly listen --port 8088      # show pushes as they arrive
upsmon shelly serve --port 8089       # accept an outbound websocket
upsmon shelly discover                # find Shelly devices on the LAN
```

`plug` and `sensor` are the two roles; `temp`, `tempmeter` and `ht` also mean
the sensor. `discover` and `listen` need no role at all.

Three things worth knowing. `watch` uses the device's websocket, which carries
no authentication — with a password set it says so and points you at `poll`.
`discover` uses multicast DNS, which does not cross subnets. And `ble` and
`matter` only take effect after a reboot, which `--reboot` will do for you.

Switching the socket this way bypasses the PIN, because anyone who can run this
command is already on the machine and could edit the configuration anyway. The
dashboard is what the PIN protects.
upsmon --check                        # is the whole system healthy
upsmon --diag                         # settings, API latency, database size
upsmon --oneshot                      # one collection into the database, then exit
```

### Starting the history again

```bash
sudo -u upsmon upsmon --reset-data          # everything: samples, events, outages
sudo -u upsmon upsmon --reset-data events   # only the event log
sudo -u upsmon upsmon --reset-data history  # only the charts
sudo -u upsmon upsmon --reset-data outages  # only the power-failure list
sudo -u upsmon upsmon --reset-data tests    # only the self-test history
sudo -u upsmon upsmon --reset-data plug     # only the smart plug history
sudo -u upsmon upsmon --reset-data sensor   # only the pushed sensor readings
```

It lists what it is about to delete and asks before doing it; `--yes` skips the
question for scripts. The file is compacted afterwards, so the space actually
comes back rather than being left as free pages inside the database.

Run it as the `upsmon` user, not as root, or you will leave root-owned files
behind. If the service is running it says so and reminds you to restart it —
the daemon holds the current status in memory to compare each poll against, and
after a wipe that comparison refers to rows that no longer exist:

```bash
sudo systemctl restart upsmon
```

A few options apply to most of the commands above:

| option | what it does |
|---|---|
| `--config FILE` | read a different configuration file |
| `--host`, `--port`, `--ups` | override the NUT address or UPS name for this run |
| `--log-file FILE` | write the log somewhere else; an empty string turns the file off |
| `--json` | machine-readable output, where the command has any |
| `--limit N` | how many rows `--events` and `--sensors` print |
| `--points N` | how many rows `--history` prints |
| `--no-color` | plain text, no escape codes |
| `-v` | debug logging, including the polls that are normally silent |
| `--aggregate` | roll old samples into hourly averages now instead of waiting for the hourly pass |
| `--yes` | answer confirmations, for scripts |

Under `shelly`, `--interval` and `--count` control `poll`, `--seconds` controls
how long `discover` listens, `--csv FILE` appends readings to a spreadsheet,
`--wait` waits for a device to come back after `reboot`, and `--port` picks the
port for `listen` and `serve`.

`--set` and `--exec` use the running daemon when there is one, so they get the
same permission handling and land in the same event log as the dashboard. With
the service stopped they fall back to talking to upsd directly, and will ask for
credentials if the config has none.

### Self tests

Start one from the Control tab or the shell:

```bash
upsmon --exec test.battery.start.quick
```

The command returns immediately, but the daemon keeps watching and records the
outcome — how long the test ran, how long the load was genuinely on battery, and
how far the battery voltage sagged:

```bash
upsmon --events | tail -3
```

```
2026-08-29 12:04:11  *  test  test.battery.start.quick finished after 36s;
                        result: Done and passed; on battery for about 10s;
                        battery dipped to 13.0 V
```

That voltage figure says more about battery health than pass/fail does — a tired
battery still passes a short test. Watch it across months.

If the voltage never moves, that is normal for a short test rather than a fault:
`usbhid-ups` refreshes numeric values only every `driver.parameter.pollfreq`
seconds, typically 30, so a five-second test finishes between two reads. For a
real measurement, pull the mains plug for a minute with the History tab open.

---

## 17. Troubleshooting

| symptom | what to do |
|---|---|
| dashboard says the daemon is not responding | `systemctl status upsmon`, then `upsmon --check` |
| `--check` says the API is unreachable, but the service is running | something else holds port 9848: `sudo ss -lntp \| grep 9848`. Change `api_port` |
| service runs but has no UPS data | `upsmon --diag`. Almost always the NAS has not permitted this machine's IP |
| `upsmon --check` all ok, but the dashboard is blank or says the daemon is unreachable | SELinux. `sudo setsebool -P httpd_can_network_connect 1`, then section 12 |
| read-only pages work, control buttons say the token is unreadable | `sudo -u apache cat /run/upsmon/api-token`. If denied, `id apache` should list `upsmon`, and php-fpm must have been restarted since |
| control says ACCESS-DENIED | the upsd account — section 15. After a DSM update, add it again |
| control says USERNAME-REQUIRED | `nut_username` / `nut_password` are empty in the config |
| charts empty | nothing recorded yet. Wait one `sample_interval`, then `upsmon --history 1h` |
| page returns 502 | php-fpm is down, or its socket path differs: compare `ls -l /run/php-fpm/www.sock` with the `fastcgi_pass` line |
| page returns 403 from nginx | the `allow` lines do not include the address you are browsing from |
| `nginx -t` says duplicate default server | the stock `server` block in `/etc/nginx/nginx.conf` is still there — comment it out |
| browser warns the certificate is not trusted | the client does not hold the intermediate, and `server.pem` does not carry it. `grep -c "BEGIN CERTIFICATE" /etc/nginx/certs/server.pem` tells you which |
| browser says the name does not match | the hostname is not in the certificate's `subjectAltName`, or `server_name` is wrong |
| `SSL_CTX_use_PrivateKey` error at start | key and certificate are not a pair — compare the two modulus hashes in section 11 |
| `cannot load certificate ... No such file or directory` | the path in `ssl_certificate` does not exist. `ls -l /etc/nginx/certs` and make the config match |
| `unknown directive "http2"` | nginx older than 1.25. The shipped config uses the `listen ... ssl http2` form, which is right for AlmaLinux 9 |
| everything works but the page is stuck on http | HSTS has not been issued yet, or you are hitting an IP address rather than the certificate name |
| service fails at start, journal shows `attempt to write a readonly database` | a root-owned database file — see below |
| `Address already in use` on port 9848 | the service is already running. Plain `upsmon` with no arguments starts a *second* daemon; from 2.0.4 it refuses and says so. Use `upsmon --status` to look at the UPS, `systemctl status upsmon` to look at the service |
| journal says `refusing to start on defaults` | `/etc/upsmon/config.json` is not readable by the `upsmon` user: `sudo chown root:upsmon /etc/upsmon/config.json` |
| daemon reports `cannot reach upsd at 127.0.0.1:3493` when the NAS is elsewhere | the config was not loaded at all. `upsmon --diag` shows the host actually in force |
| a self test logged as a warning while it is still running | fixed in 2.5.1 — `In progress` is a stage, not a verdict, and is no longer an event |
| `LB` or `RB` in the event log, appearing and clearing exactly one poll apart | a bad USB read, not a real alarm. Version 3.4.4 requires two consecutive polls before logging a flag; raise `flag_confirm_polls` if any still get through |
| `battery.type changed outside upsmon: PbAc -> ` and back again | the driver publishes some values only on its slower full poll, so they vanish between reads. Fixed in 2.5.0 — a key that merely appears or disappears is no longer treated as a change |
| the log file stops growing after a rotation | the `postrotate` signal never arrived. The daemon recovers within one poll and logs that it did; check that `/etc/logrotate.d/upsmon` still contains the `systemctl kill -s HUP` line |
| `cannot write the log file ... Permission denied` | `/var/log/upsmon` is not owned by `upsmon`. `sudo install -d -m 750 -o upsmon -g upsmon /var/log/upsmon`, or let systemd make it with `LogsDirectory=` by restarting the service |
| a `shelly` command fails with 429 when run repeatedly | each run starts with no memory of the last one, so quick successive commands can trip the limit. From 3.4.4 the client waits and retries up to three times, and never reports a rate limit as a missing component |
| the plug answers HTTP 429 | it is rate limiting. Three things address it: the authentication challenge is reused rather than renegotiated, the slow-changing sections are read once a minute, and requests are spaced by `plug_min_request_gap`. Together those take a poll from six requests to two, a second apart. If it still happens, raise the gap to 2 and `plug_poll_interval` to 30 |
| the plug panel is greyed out right after a restart | the first poll has not landed yet. From 3.4.4 the panel is filled from the last recorded reading and says `from the last recorded reading` until a fresh one arrives, rather than sitting blank |
| the plug panel shows values greyed out | they are real but not current: a poll was missed. The reason and the age are printed under the socket state, and the controls stay disabled until it answers again |
| a counter will not reset | the plug answers every reset with success whether or not it understood the counter name, so upsmon reads the values back and reports what actually changed. `the plug accepted the request but nothing changed` means that firmware does not support resetting that particular counter — the plug's own web interface will not clear it either |
| the plug is not answering | `upsmon --plug`. A password set in the plug's web UI needs `plug_password`; the username is always `admin` |
| plug readings stop but the UPS is fine | the plug dropped off Wi-Fi. The daemon logs one event either way and carries on watching the UPS — a plug failure never affects UPS monitoring |
| a sensor pushes nothing | check the webhook with `Webhook.List` on the sensor, and that the port is open. `upsmon --sensors` shows what has arrived |
| a setting reverts after a NAS reboot | that value lives in the driver, not the UPS — see the note below |

Useful commands while diagnosing:

```bash
journalctl -u upsmon --since "1 hour ago"          # what the daemon has been doing
sudo tail -f /var/log/nginx/upsmon-error.log       # nginx and PHP errors
sudo ausearch -m AVC -ts recent                    # SELinux denials
curl -s 127.0.0.1:9848/api/health | head -c 300    # the daemon, bypassing PHP
sudo -u apache curl -s 127.0.0.1:9848/api/health   # ...as the web user sees it
```

A failed poll is logged once, not once per attempt, and recovery is logged too,
so a flapping network appears as pairs of lines rather than thousands. An error
inside an API endpoint answers the caller with a short message and logs one
line — a dashboard polling every five seconds would otherwise fill the journal
with identical stack traces and push the actual cause out of view.

**On `attempt to write a readonly database`:** SQLite says "readonly" when the
*directory* cannot be written, which is not what the wording suggests. The cause
is nearly always a database file created by a command run as root, leaving it
owned `root:root` while the service runs as `upsmon`. Check and fix:

```bash
sudo systemctl stop upsmon
ls -la /var/lib/upsmon
sudo chown -R upsmon:upsmon /var/lib/upsmon
sudo chmod 750 /var/lib/upsmon
sudo systemctl start upsmon
upsmon --check
```

`upsmon --check` tests for this directly — `database directory writable` and
`database file writable` name the owner and mode when they fail. From version
2.0.1 the reporting commands open the database read-only, so they can no longer
create anything, and a genuine permission problem is reported as advice rather
than a traceback.

**On settings that do not survive a reboot:** whether a change sticks depends on
the UPS, not on any of this. Values written into the UPS's own memory — transfer
voltages, nominal output voltage, beeper state, battery date on APC units —
survive both a NAS reboot and a UPS power cycle. Values the driver only holds in
memory, such as some shutdown delays, return to their defaults whenever the
driver restarts. Test it: set the value, reboot the NAS, read it back.

---

## 18. Maintenance

### Size

With the default retention, and a plug and a sensor both configured:

| what | interval | kept | rows |
|---|---|---|---|
| UPS samples | 60 s | 14 days | about 20 000 |
| UPS hourly averages | — | 2 years | about 17 500 |
| plug samples | 60 s | 14 days | about 20 000 |
| plug hourly averages | — | 2 years | about 17 500 |
| sensor readings | on push | 2 years | a few thousand |

Around 10–15 MB in total, perhaps 30 MB after a couple of years with everything
switched on. The roll-up runs hourly inside the daemon; there is no cron job to
add.

```bash
upsmon --diag | grep -A2 database
```

`upsmon --diag` reports the data file and the write-ahead log separately. SQLite
lets that log reach about 4 MB before folding it back in, so on a fresh install
the total looks large next to a database of a few hundred kilobytes — that is
the log, not the data. The daemon folds it back on its hourly pass.

To keep more or less, adjust `retain_full_days`, `retain_hourly_days` and
`sensor_retain_days`, then restart.

### Backup

SQLite in WAL mode, so use the safe form rather than `cp`:

```bash
sudo dnf install -y sqlite
sudo -u upsmon sqlite3 /var/lib/upsmon/history.db ".backup /tmp/ups-backup.db"
```

### Upgrading the daemon

```bash
sudo install -m 755 upsmon.py /usr/local/lib/upsmon/upsmon.py
sudo systemctl restart upsmon
upsmon --check
```

The schema is created with `IF NOT EXISTS`, so history survives upgrades. When
a version adds a column, the daemon adds it to the existing tables on startup
and says so in the log — `database updated: added plug_samples.rssi, …`. An
error like `table plug_samples has no column named …` means the daemon was not
restarted after the file was replaced.

### Log rotation

Handled by `/etc/logrotate.d/upsmon` — daily, fourteen kept, compressed. The
journal is rotated by systemd separately, under its own size limits.

### Uninstalling

```bash
sudo systemctl disable --now upsmon
sudo rm -f /etc/systemd/system/upsmon.service /usr/local/bin/upsmon
sudo rm -rf /usr/local/lib/upsmon /etc/upsmon /var/www/upsmon
sudo rm -f /etc/nginx/conf.d/upsmon.conf /etc/logrotate.d/upsmon
sudo rm -rf /var/log/upsmon
sudo systemctl daemon-reload && sudo systemctl reload nginx
sudo semanage fcontext -d "/var/www/upsmon(/.*)?"
sudo userdel upsmon
# the history survives unless you say otherwise
sudo rm -rf /var/lib/upsmon
```
