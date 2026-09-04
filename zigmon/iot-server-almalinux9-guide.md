# MQTT broker, Node-RED, Zigbee2MQTT and the zigmon dashboard on AlmaLinux 9

The complete, final procedure — every step as it ended up working, with the
wrong turns taken out and the fixes folded in. Passwords are placeholders
(`PASSWORD_*`); substitute your own. Hostnames and addresses are the ones this
installation uses; change them to yours.

SELinux is **disabled** on this machine, so there are no `semanage` /
`restorecon` steps below. If yours is enforcing, see the note in §11.

## What you end up with

| What | Where | Notes |
|---|---|---|
| Node-RED | `https://red.falco81.net/` | nginx → 127.0.0.1:1880 |
| MQTT Wall | `https://red.falco81.net/wall` | static page, Basic Auth |
| Zigbee2MQTT frontend | `https://red.falco81.net/zigbee` | nginx → 127.0.0.1:8080, Basic Auth |
| MQTT over WebSocket | `wss://red.falco81.net/mqtt` | nginx → 127.0.0.1:9001 |
| **zigmon** sensor dashboard | `https://mqtt.falco81.net/` | nginx → php-fpm → daemon on 127.0.0.1:9849 |
| MQTT plaintext | `192.168.40.233:1883` | LAN only: Node-RED, Zigbee2MQTT, zigmon, Shelly |
| MQTT over TLS | `mqtt.falco81.net:8883` | external clients, Shelly over TLS |
| Zigbee coordinator | SMLIGHT SLZB-06Mg26U, `192.168.40.232:6638` | Ethernet/PoE, EmberZNet |

Certificates in `/etc/nginx/certs` (used as the files they are, no
`fullchain.pem` juggling):

- `server.pem`, `server.key`, `server.ca` — for `mqtt.falco81.net`; mosquitto's TLS listener and the zigmon site
- `server-red.pem`, `server-red.key` — for `red.falco81.net`

Broker accounts:

| User | Rights | Used by |
|---|---|---|
| `admin` | readwrite `#` | administration, Node-RED, tests |
| `wall` | read `#` | MQTT Wall (its password sits in `index.html`, hence Basic Auth on top) |
| `z2m` | readwrite `zigbee2mqtt/#` | Zigbee2MQTT |
| `shelly` | readwrite `shellies/#` | Shelly Wi-Fi devices |
| `zigmon` | read `zigbee2mqtt/#`, write `zigbee2mqtt/bridge/request/#`, read `shellies/#` | the zigmon daemon (permit-join from the dashboard needs the write) |

Give every account its own password. The one shared password that was used
while building this is exactly what an attacker who reads `index.html` wants.

```
Zigbee sensors ──► SLZB-06 ──► Zigbee2MQTT ──┐
Shelly Wi-Fi ────────────────────────────────┤        AlmaLinux 9
                                             ▼
                                        mosquitto ──► Node-RED, MQTT Wall (wss via nginx)
                                             │
                                             └──► zigmon.service ──► SQLite ──► API :9849 ──► php-fpm ◄── nginx :443
                                                        │
                                                        └── RPC ──► Shelly Plug M Gen3 (switching, counters, button bindings)
```

**Contents**

1. [Packages](#1-packages)
2. [Certificates and permissions](#2-certificates-and-permissions)
3. [Mosquitto](#3-mosquitto)
4. [Node-RED](#4-node-red)
5. [MQTT Wall](#5-mqtt-wall)
6. [nginx for red.falco81.net](#6-nginx-for-redfalco81net)
7. [Firewall](#7-firewall)
8. [The Zigbee coordinator: SLZB-06Mg26U](#8-the-zigbee-coordinator-slzb-06mg26u)
9. [Zigbee2MQTT](#9-zigbee2mqtt)
10. [Shelly Wi-Fi devices over MQTT](#10-shelly-wi-fi-devices-over-mqtt)
11. [zigmon: the sensor dashboard](#11-zigmon-the-sensor-dashboard)
12. [Zigbee radio: range, routers, channels](#12-zigbee-radio-range-routers-channels)
13. [Reading the broker by hand](#13-reading-the-broker-by-hand)
14. [Where to look when something breaks](#14-where-to-look-when-something-breaks)

---

## 1. Packages

```bash
dnf install -y epel-release
dnf install -y nginx mosquitto unzip httpd-tools git python3-pip \
               php-fpm python3-paho-mqtt sqlite
dnf module enable -y nodejs:20
dnf install -y nodejs
npm install -g --unsafe-perm node-red
npm install -g pnpm
```

Global npm packages land in `/usr/local/bin`, which is **not** in `sudo`'s
`secure_path`. Every command below therefore uses the full path
(`/usr/local/bin/node-red`, `/usr/local/bin/pnpm`, `/usr/local/bin/zigmon`).

---

## 2. Certificates and permissions

Mosquitto reads its key after dropping to the `mosquitto` user, and nginx
reads as `nginx`. A shared group is the clean way to let both at the keys
without making them world-readable.

```bash
groupadd -r certs
usermod -aG certs mosquitto
usermod -aG certs nginx

cd /etc/nginx/certs
chown root:certs server.ca server.pem server.key server-red.pem server-red.key
chmod 644 server.ca server.pem server-red.pem
chmod 640 server.key server-red.key

sudo -u mosquitto cat /etc/nginx/certs/server.key >/dev/null && echo OK
```

nginx has no separate directive for the issuer chain: whatever
`server-red.pem` contains is what browsers see. If a client complains about an
untrusted chain, check how many certificates the file holds:
`grep -c "BEGIN CERTIFICATE" server-red.pem` (1 = the leaf only).

---

## 3. Mosquitto

### 3.1 Accounts

```bash
mosquitto_passwd -c -b /etc/mosquitto/passwd admin  'PASSWORD_ADMIN'
mosquitto_passwd    -b /etc/mosquitto/passwd wall   'PASSWORD_WALL'
mosquitto_passwd    -b /etc/mosquitto/passwd z2m    'PASSWORD_Z2M'
mosquitto_passwd    -b /etc/mosquitto/passwd shelly 'PASSWORD_SHELLY'
mosquitto_passwd    -b /etc/mosquitto/passwd zigmon 'PASSWORD_ZIGMON'
chown mosquitto:mosquitto /etc/mosquitto/passwd
chmod 600 /etc/mosquitto/passwd
```

Mosquitto 2.0 reads the password file as the `mosquitto` user and warns (or
refuses) when it is owned by root or readable by others; `mosquitto:mosquitto
600` is the arrangement that produced neither warning nor failure. Ignore the
`mosquitto_passwd` remark about the file's group.

### 3.2 ACL

```bash
cat > /etc/mosquitto/acl <<'EOF'
user admin
topic readwrite #

user wall
topic read #

user z2m
topic readwrite zigbee2mqtt/#

user shelly
topic readwrite shellies/#

user zigmon
topic read zigbee2mqtt/#
topic write zigbee2mqtt/bridge/request/#
topic read shellies/#
EOF
chown mosquitto:mosquitto /etc/mosquitto/acl
chmod 600 /etc/mosquitto/acl
```

When appending to this file later, make sure it ends with a newline first
(`tail -c1 /etc/mosquitto/acl | od -c`): `echo >>` onto a file without one
glues the new text to the last line, and mosquitto then fails with something
like `Invalid protocol value (websocketsacl_file)`.

### 3.3 Configuration

Replace the whole of `/etc/mosquitto/mosquitto.conf` (and end the file with a
newline):

```
persistence true
persistence_location /var/lib/mosquitto/
log_dest file /var/log/mosquitto/mosquitto.log

allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl

# plaintext for the LAN
listener 1883 0.0.0.0

# MQTT over TLS for external clients (mqtt.falco81.net)
listener 8883
cafile   /etc/nginx/certs/server.ca
certfile /etc/nginx/certs/server.pem
keyfile  /etc/nginx/certs/server.key
tls_version tlsv1.2

# WebSocket, local only; nginx exposes it as wss
listener 9001 127.0.0.1
protocol websockets
```

The persistence directory is not created by the package:

```bash
install -d -m 700 -o mosquitto -g mosquitto /var/lib/mosquitto
```

### 3.4 Start and test

```bash
systemctl enable --now mosquitto
ss -tlnp | grep mosquitto        # 0.0.0.0:1883, *:8883, 127.0.0.1:9001
```

If it does not start, the configuration-syntax errors are in
`journalctl -u mosquitto --no-pager`, everything else in
`/var/log/mosquitto/mosquitto.log`.

```bash
# subscriber over TLS, publisher over plaintext
mosquitto_sub -h mqtt.falco81.net -p 8883 --cafile /etc/nginx/certs/server.ca \
  -u admin -P 'PASSWORD_ADMIN' -t test -v &
sleep 2
mosquitto_pub -h 127.0.0.1 -p 1883 -u admin -P 'PASSWORD_ADMIN' -t test -m hello
kill %1

# the ACL: a publish from "wall" is silently dropped, the log says "Denied PUBLISH"
mosquitto_pub -h 127.0.0.1 -u wall -P 'PASSWORD_WALL' -t test -m x
tail -2 /var/log/mosquitto/mosquitto.log
```

---

## 4. Node-RED

### 4.1 User and settings.js

```bash
useradd -r -m -d /var/lib/nodered -s /sbin/nologin nodered
chown nodered:nodered /var/lib/nodered

# the first real run writes settings.js and is stopped after 10 s
# (--help does not create it)
sudo -u nodered timeout 10 /usr/local/bin/node-red -u /var/lib/nodered
ls -la /var/lib/nodered/settings.js
```

### 4.2 Editing settings.js

```bash
sudo -u nodered /usr/local/bin/node-red admin hash-pw        # hash for the editor login

SECRET=$(openssl rand -hex 32)
sed -i 's|^\s*//uiHost: "127.0.0.1",|    uiHost: "127.0.0.1",|' /var/lib/nodered/settings.js
sed -i "s|^\s*//credentialSecret: false,|    credentialSecret: \"$SECRET\",|" /var/lib/nodered/settings.js
grep -nE 'uiHost|credentialSecret' /var/lib/nodered/settings.js
```

Then uncomment the `adminAuth` block by hand and paste the hash:

```js
adminAuth: {
    type: "credentials",
    users: [{
        username: "admin",
        password: "$2b$08$...hash from hash-pw...",
        permissions: "*"
    }]
},
```

### 4.3 systemd

`/etc/systemd/system/nodered.service`:

```ini
[Unit]
Description=Node-RED
After=network.target mosquitto.service

[Service]
Type=simple
User=nodered
Group=nodered
WorkingDirectory=/var/lib/nodered
ExecStart=/usr/local/bin/node-red -u /var/lib/nodered
Restart=on-failure
Environment="NODE_OPTIONS=--max-old-space-size=512"

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now nodered
curl -sI http://127.0.0.1:1880 | head -1        # HTTP/1.1 200 OK
```

In the Node-RED MQTT node: broker `127.0.0.1`, port `1883`, user `admin` —
no TLS, same machine.

---

## 5. MQTT Wall

```bash
cd /tmp
curl -sLO https://github.com/bastlirna/mqtt-wall/releases/download/v0.4.1/mqtt-wall-0.4.1.zip
unzip mqtt-wall-0.4.1.zip
mkdir -p /var/www
mv wall-0.4.1 /var/www/wall
chown -R root:nginx /var/www/wall
chmod -R o-rwx /var/www/wall
echo -n 'PASSWORD_WALL' | base64            # the page wants the password base64-encoded
```

In `/var/www/wall/index.html`, the `config` block:

```js
server: {
    uri: "wss://red.falco81.net/mqtt",
    username: "wall",
    password: "BASE64_OF_PASSWORD_WALL"
},
defaultTopic: "#",
```

(The shipped `defaultTopic: "/#"` only matches topics beginning with a slash.)

Basic Auth in front of it, which also protects the password in the page:

```bash
htpasswd -Bc /etc/nginx/.htpasswd admin        # -c only the first time
chown root:nginx /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd
```

---

## 6. nginx for red.falco81.net

nginx on AlmaLinux 9 is 1.20: HTTP/2 is switched on in the `listen` line, the
directive `http2 on;` does not exist yet.

Comment out or delete the stock `server { listen 80 default_server; ... }`
block in `/etc/nginx/nginx.conf`.

`/etc/nginx/conf.d/red.falco81.net.conf`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    listen [::]:80;
    server_name red.falco81.net;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name red.falco81.net;

    ssl_certificate     /etc/nginx/certs/server-red.pem;
    ssl_certificate_key /etc/nginx/certs/server-red.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=31536000" always;

    # MQTT Wall, a static page
    location /wall {
        root /var/www;
        index index.html;
        auth_basic           "MQTT Wall";
        auth_basic_user_file /etc/nginx/.htpasswd;
    }

    # The Zigbee2MQTT web manifest is fetched by browsers without credentials,
    # which otherwise shows up as a 401 in the console. Harmless, but noisy.
    location ~ ^/zigbee/.*\.webmanifest$ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        auth_basic off;
    }

    # Zigbee2MQTT frontend
    location /zigbee {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        auth_basic           "Zigbee2MQTT";
        auth_basic_user_file /etc/nginx/.htpasswd;
    }

    # MQTT over WebSocket -> wss://red.falco81.net/mqtt
    location /mqtt {
        proxy_pass http://127.0.0.1:9001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    # Node-RED (editor and dashboard both use websockets)
    location / {
        proxy_pass http://127.0.0.1:1880;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;
    }
}
```

```bash
nginx -t && systemctl enable --now nginx
```

The zigmon site for `mqtt.falco81.net` is a second file in `conf.d/`, written
by its installer (§11). Neither block claims `default_server`, so they coexist;
nginx picks by the `Host` header.

---

## 7. Firewall

```bash
firewall-cmd --permanent --add-service=http
firewall-cmd --permanent --add-service=https
firewall-cmd --permanent --add-port=1883/tcp      # MQTT plaintext, LAN
firewall-cmd --permanent --add-port=8883/tcp      # MQTT over TLS, only if wanted from outside
firewall-cmd --reload

curl -I http://red.falco81.net                                     # 301
curl -I https://red.falco81.net                                    # 200
curl -sI https://red.falco81.net/wall/ | head -1                   # 401
curl -sI -u admin:PASSWORD https://red.falco81.net/wall/ | head -1 # 200
```

---

## 8. The Zigbee coordinator: SLZB-06Mg26U

1. Power it over PoE (or Ethernet plus USB-C).
2. In its web UI: **Mode: Ethernet**, **Zigbee role: Coordinator**. Update
   SLZB-OS and the Zigbee firmware (this installation runs EmberZNet 8.0.3 /
   EZSP 16 on Zigbee2MQTT 2.13).
3. Leave the **Ethernet/Wi-Fi watchdog** on, set a password on the web UI.
4. Give it a **DHCP reservation**; its address is in the Zigbee2MQTT config.
5. The TCP port for Zigbee2MQTT is **6638**.

The **Zigbee Hub → Settings** page (its own channel, PAN ID, Extended PAN ID)
belongs to SLZB-OS's built-in hub mode. In coordinator mode it is ignored:
Zigbee2MQTT decides the channel and PAN from `configuration.yaml`. Do not
change or save anything there.

---

## 9. Zigbee2MQTT

### 9.1 Installation

```bash
useradd -r -m -d /opt/zigbee2mqtt -s /sbin/nologin z2m
chown z2m:z2m /opt/zigbee2mqtt
cd /opt/zigbee2mqtt
sudo -u z2m git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git app
cd app
sudo -u z2m HOME=/opt/zigbee2mqtt /usr/local/bin/pnpm install --frozen-lockfile
sudo -u z2m HOME=/opt/zigbee2mqtt /usr/local/bin/pnpm run build
```

`HOME=` matters: pnpm writes its store under the home directory, and the
service user's is `/opt/zigbee2mqtt`.

### 9.2 Configuration

`/opt/zigbee2mqtt/app/data/configuration.yaml`:

```yaml
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://127.0.0.1:1883
  user: z2m
  password: PASSWORD_Z2M

serial:
  port: tcp://192.168.40.232:6638
  adapter: ember
  baudrate: 115200

frontend:
  enabled: true
  host: 127.0.0.1
  port: 8080
  base_url: /zigbee

availability:
  enabled: true
  active:
    timeout: 10
  passive:
    timeout: 1500

advanced:
  channel: 26
  transmit_power: 20
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  last_seen: ISO_8601
  log_level: info

homeassistant:
  enabled: false
```

`GENERATE` is replaced by real values on the first start. **Back them up**
afterwards — they are the network; without them every device has to be
re-paired:

```bash
cp /opt/zigbee2mqtt/app/data/configuration.yaml /root/z2m-configuration.yaml.bak
```

The channel: 25 and 26 were both measured clean here (see §12). Changing the
channel later means re-pairing battery devices, so pick once. `log_level`
stays at `info`; `debug` with the ember adapter logs every serial frame and
floods both the journal and `data/log/`. Switch it on only while diagnosing.

```bash
chown -R z2m:z2m /opt/zigbee2mqtt
```

### 9.3 systemd, surviving a coordinator outage

Zigbee2MQTT exits when it loses the coordinator and relies on its supervisor
to bring it back. Without `StartLimitIntervalSec=0` systemd gives up after
five attempts and the service stays dead until someone notices.

`/etc/systemd/system/zigbee2mqtt.service`:

```ini
[Unit]
Description=Zigbee2MQTT
After=network-online.target mosquitto.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=z2m
WorkingDirectory=/opt/zigbee2mqtt/app
ExecStart=/usr/bin/node index.js
Restart=always
RestartSec=15
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now zigbee2mqtt
journalctl -u zigbee2mqtt -f            # wait for "Zigbee2MQTT started"
```

Verified: unplug the SLZB-06, the journal shows a crash and a retry every
15 s, plug it back in and it comes up. Paired devices survive (the network
lives in the coordinator and in `data/database.db`).

### 9.4 Upgrading Zigbee2MQTT

The repository's own `update.sh` does not work with this installation, and
its failure is quiet rather than loud. Two reasons: the repository belongs to
`z2m` while you are root, so git refuses it as "dubious ownership"; and the
clone is `--depth 1`, so there are no tags for the script to read a version
from. It then compares an empty string, prints `No update available` and
changes nothing. Use the steps below instead.

**1. Stop it and copy the data out.** `data/` holds `database.db` and the
network keys — without them every device has to be paired again.

```bash
systemctl stop zigbee2mqtt
cp -a /opt/zigbee2mqtt/app/data /root/z2m-data.bak-$(date +%F)
cp /opt/zigbee2mqtt/app/data/configuration.yaml /root/z2m-configuration.yaml.bak
cd /opt/zigbee2mqtt/app && sudo -u z2m HOME=/opt/zigbee2mqtt git rev-parse HEAD   # note it down, this is the way back
```

**2. Fetch the new version.** A shallow clone cannot `git pull` in the usual
way; fetch one commit deep and move onto it.

```bash
cd /opt/zigbee2mqtt/app
sudo -u z2m HOME=/opt/zigbee2mqtt git fetch --depth 1 origin master
sudo -u z2m HOME=/opt/zigbee2mqtt git reset --hard origin/master
```

**3. Build**, with the two commands from §9.1. `HOME=` matters here as much
as it did during installation — without it pnpm writes into root's home and
leaves the tree half owned by the wrong user.

```bash
sudo -u z2m HOME=/opt/zigbee2mqtt /usr/local/bin/pnpm install --frozen-lockfile
sudo -u z2m HOME=/opt/zigbee2mqtt /usr/local/bin/pnpm run build
```

If `--frozen-lockfile` complains that the lockfile and `package.json`
disagree, run it once without that flag.

**4. Start it again.**

```bash
chown -R z2m:z2m /opt/zigbee2mqtt
systemctl start zigbee2mqtt
journalctl -u zigbee2mqtt -f            # wait for "Zigbee2MQTT started"
```

**5. Check.**

```bash
zigmon --check                          # the bridge should be online again
zigmon --map
```

Running the git commands as `z2m` is what settles the ownership complaint —
`git config --global --add safe.directory`, which git suggests, is the wrong
fix here and would only paper over running as the wrong user.

**Going back.** `git reset --hard <the commit from step 1>` followed by the
two pnpm commands, or restore `data/` from the copy.

Node stays as it is: Zigbee2MQTT 2.x wants Node 20 or 22, and §1 installs 20.

### 9.5 Pairing Shelly BLU H&T ZB and BLU Button Tough 1 ZB

1. `https://red.falco81.net/zigbee` → **Permit join** (or the zigmon Control tab).
2. On the device, **press the button five times quickly**. Out of the box
   they are in Bluetooth mode; this switches them to Zigbee and starts
   joining.
3. After the interview, rename it (`Temp-Balkon`, `Button`, …); the topic
   becomes `zigbee2mqtt/Temp-Balkon`.
4. Close permit join.

The H&T reports `temperature`, `humidity`, `battery`; the button reports
`action` (`single`, `double`, `triple`, `long`, …). Shelly BLU ZB devices
have **no Zigbee OTA** — firmware updates go through Bluetooth in the Shelly
app.

Two things about the Zigbee2MQTT frontend that look like faults and are not:
"Recent activity" lists **changes** only, so a second `single` after a first
one shows up as a link-quality change and nothing else; and the button waits
to tell single from double from triple, so three quick presses are one
`triple`, not three `single`s. `mosquitto_sub -t zigbee2mqtt/Button -v` is the
honest view.

---

## 10. Shelly Wi-Fi devices over MQTT

In each device, **Settings → MQTT**:

- Enable ✓
- Server: `192.168.40.233:1883`, or over TLS `192.168.40.233:8883` with
  *SSL: no validation* (the sensor in this installation is on another subnet
  and reaches the server that way)
- Username / Password: `shelly` / `PASSWORD_SHELLY`
- MQTT prefix: `shellies/ht-temp` (or `shellies/plug-lednice`, …)
- ✓ Enable RPC over MQTT, ✓ Generic status update over MQTT
- Save → reboot

They publish, for example:

- `shellies/ht-temp/status/temperature:0` → `{"id":0,"tC":22.4,"tF":72.3}`
- `shellies/ht-temp/status/humidity:0` → `{"id":0,"rh":47.5}`
- `shellies/ht-temp/status/devicepower:0` → `{"id":0,"battery":{"V":2.9,"percent":85},...}`
- `shellies/plug-lednice/status/switch:0` → `{"output":true,"apower":85.2,"voltage":231.1,...}`

An H&T Gen3 sleeps and reports on change or on its reporting interval
(*Sensors → reporting*); the `OpenSSL: unexpected eof` lines in the mosquitto
log when it goes back to sleep are normal. A Plug M reports continuously.

---

## 11. zigmon: the sensor dashboard

The daemon-plus-PHP dashboard built in this project, on the pattern of
`upsmon`: a Python daemon (stdlib + paho-mqtt) subscribes to the broker,
keeps history in SQLite and serves a token-guarded API on 127.0.0.1:9849; a
PHP page behind nginx relays JSON and holds no credentials. It shows every
Zigbee device with the labels and units from its Zigbee2MQTT definition, the
Shelly Wi-Fi devices from `shellies/`, and Shelly plugs read and controlled
over local RPC — with button bindings (press → plug), the full `zigmon shelly`
toolkit, and an optional webhook listener for battery sensors.

### 11.1 Install

Unpack the release beside the certificates already in place and run the
installer:

```bash
tar xzf zigmon-1.2.5.tar.gz && cd zigmon
sudo ./install.sh --mqtt-host 127.0.0.1 --mqtt-user zigmon --mqtt-password 'PASSWORD_ZIGMON' \
     --create-mqtt-user --pin 'PIN' --server-name mqtt.falco81.net \
     --allow 192.168.40.0/24,172.16.16.0/24
```

`--create-mqtt-user` writes the account and the ACL block from §3 into the
local mosquitto (and adds the trailing newline first). The installer also
puts nginx in the `zigmon` group so php-fpm can read the API token, switches
php-fpm from `apache` to `nginx`, writes `/etc/nginx/conf.d/zigmon.conf` for
`mqtt.falco81.net` (no `default_server`, so it sits beside §6), opens
http/https in the firewall, starts `zigmon.service` and runs `zigmon --check`.
`--dry-run` shows what it would do; `--upgrade` replaces the program files
later; `--uninstall` removes everything but the history.

DNS (or `/etc/hosts` on the client) must point `mqtt.falco81.net` at the
server. Then `https://mqtt.falco81.net/`.

On an SELinux-enforcing machine the installer additionally sets
`httpd_can_network_connect`, which is what lets PHP reach 127.0.0.1:9849.

### 11.2 Configuration

`/etc/zigmon/config.json` (root:zigmon 640 — it holds passwords). The
defaults fit this installation; what you may want to touch:

```json
"stale_warn_min": 180,        "stale_crit_min": 1500,
"battery_warn": 30,           "battery_crit": 15,
"lqi_warn": 40,               "lqi_crit": 20,
"temperature_min": null,      "temperature_max": null,   "humidity_max": null,

"plugs": [
  {"name": "PlugTST", "host": "172.16.16.14", "password": "", "switch_id": 0, "mqtt_name": ""}
],
"sensors": [],
"sensor_listen": false, "sensor_listen_port": 8088
```

- `plugs` — each Shelly plug the daemon reads and controls over RPC (port 80
  on the plug must be reachable from the server; test with
  `curl -s http://172.16.16.14/rpc/Shelly.GetDeviceInfo`). `mqtt_name` is the
  plug's `shellies/<name>` prefix if it also publishes to the broker, so it is
  not recorded twice.
- `sensors` — Shelly sensors with an address that answer RPC (an H&T on USB).
- `sensor_listen` — accept webhook pushes from battery sensors that cannot be
  polled (`http://server:8088/?t=${ev.tC}&rh=${ev.rh}&id=NAME`); the
  installer's `--sensor-listen` sets it and opens the port.
- A "stale" warning after 3 h is right for sensors that report every few
  minutes; raise it for ones that report hourly.

`systemctl restart zigmon` after editing.

### 11.3 Using it

The dashboard: **Overview** (counts, bridge state, permit join, a socket
panel per plug, a card per sensor, temperature and humidity charts across all
devices), **History** (any device, 6 h to a year, one chart per value),
**Devices**, **Events**, **Control**, **All values**. Control is where you
open the network for pairing, switch plugs and reset their counters, and edit
the **button bindings** — the device list offers only devices that send
actions, and the action list is exactly what the device's Zigbee2MQTT
definition declares (plus anything it has actually sent). Everything that
changes something asks for the PIN; that dialogue is the only pop-up.
Buttons carry an icon; rest the pointer on one for five seconds to see the
request behind it.

The command line covers the same and more:

```bash
zigmon --status                         # every device and its readings
zigmon --check                          # is everything healthy
zigmon --watch                          # live feed from the broker
zigmon --history Temp-Venek --range 7d
zigmon --permit-join on
zigmon --plug                           # the plugs, live
zigmon --plug-toggle PlugTST
zigmon --bindings

zigmon shelly PlugTST info | dump | config | methods --examples | poll --csv f.csv
zigmon shelly PlugTST config switch:0 '{"auto_off": true, "auto_off_delay": 300}'
zigmon shelly PlugTST ble off --reboot   # radio, RPC over BLE and the observer
zigmon shelly PlugTST matter off --reboot
zigmon shelly --host 172.16.16.13 info   # a device not in the config
zigmon shelly discover                   # mDNS on this subnet
```

Two things about Bluetooth on a Plug M Gen3: `ble off` takes effect after a
reboot, and a plug with **Matter enabled and not commissioned keeps a BLE
beacon on** for the Matter controller whatever the BLE setting says.
`ble status` points it out; `matter off --reboot` ends it.

`zigmon` with no arguments runs the daemon in the foreground, which is for
the first test only. With the service running it reports that the port is
taken, and as root over `/var/lib/zigmon` it refuses (root-owned database
files would lock the service out). Use `systemctl` and `journalctl -u zigmon`.

---

## 12. Zigbee radio: range, routers, channels

What was learned while a button "sometimes" registered and an outdoor sensor
needed two presses.

- **Read the coordinator's view, not the frontend's.** With `log_level:
  debug`, every received frame shows `lastHopLqi` and `lastHopRssi`. Here the
  button was at −79…−83 dBm (LQI 68–84) and the outdoor sensor at **−89 dBm
  (LQI 44)**. The receiver's limit is around −95 to −100 dBm; at −89 a sleepy
  end device's frame is lost often enough that its retries give up, and the
  log then shows **nothing at all** for that press. Everything that did
  arrive was decoded and published — the problem was in the air.
- **Router: 0 is the problem.** Every battery device talks straight to the
  coordinator through the walls. One mains-powered Zigbee router between
  them fixes what no setting can; sleepy devices find the better parent by
  themselves within an hour (re-pair next to the router with five presses if
  one stays put).
  - Best fit here: a **second SLZB-06Mg26U switched to Router mode** in its
    web UI — same chip, external antenna, PoE, no Ethernet needed once it is
    a router. Must be in Router mode, never a second coordinator.
  - Cheap and good: **IKEA TRETAKT** or **INSPELNING** plugs. Or a Sonoff
    ZBDongle-E / Dongle Plus MG24 flashed with router firmware in a USB
    charger.
  - Not for this: Shelly Gen4 as routers (Wi-Fi coexistence problems with
    battery children), no-neutral switches (not routers), bulbs (off at the
    wall = hole in the mesh), random Tuya plugs.
- **Placement and antenna.** The SLZB-06 is on PoE, so put it central and
  high, away from the Wi-Fi AP, the switch and metal; antenna screwed on and
  vertical. `transmit_power: 20` only helps the downlink; the weak direction
  is the coin-cell sensor talking up.
- **Channels.** The SLZB-06 energy scan measures at the coordinator's
  position — and with your own network running, your own channel reads as
  busy. Channels 25 and 26 were both clean here; 26 has no Wi-Fi overlap in
  this building, 25 sits on the edge of Wi-Fi channel 11 and fully under
  channel 13. Zigbee2MQTT's own note: 11, 15, 20 and 25 are the ZLL channels
  some (older, Hue/ZLL) devices insist on; 26 has worked for everything here.
  Keep the Wi-Fi AP on a fixed 1/6/11 rather than "auto".

---

## 13. Reading the broker by hand

```bash
mosquitto_sub -h 127.0.0.1 -u admin -P 'PASSWORD_ADMIN' -t 'zigbee2mqtt/#' -t 'shellies/#' -v
mosquitto_sub -h 127.0.0.1 -u admin -P 'PASSWORD_ADMIN' -t 'zigbee2mqtt/bridge/info' -C 1 | python3 -m json.tool
zigmon --watch --bridge --raw
```

On demand: publish `{"temperature":""}` to `zigbee2mqtt/<device>/get` (a
battery device answers when it next wakes); for a Shelly, publish
`{"id":1,"src":"me","method":"Temperature.GetStatus","params":{"id":0}}` to
`shellies/<device>/rpc` and the answer arrives on `me/rpc` — or skip MQTT and
use `zigmon shelly --host <ip> call Temperature.GetStatus '{"id":0}'`.

---

## 14. Where to look when something breaks

| Service | Where |
|---|---|
| mosquitto | `tail /var/log/mosquitto/mosquitto.log`; config syntax in `journalctl -u mosquitto --no-pager` |
| nodered | `journalctl -u nodered -f` |
| zigbee2mqtt | `journalctl -u zigbee2mqtt -f`, `/opt/zigbee2mqtt/app/data/log/` |
| zigmon | `zigmon --check`, `zigmon --diag`, `journalctl -u zigmon -f`, `/var/log/zigmon/zigmon.log` |
| nginx | `nginx -t`, `/var/log/nginx/error.log`, `/var/log/nginx/zigmon-error.log` |
| SLZB-06 | its web UI → Logs; the Zigbee channel scan under Z2M and ZHA |

Everything that actually went wrong on the way, and the fix:

| Symptom | Cause | Fix |
|---|---|---|
| mosquitto: `Unable to open pwfile` | password file not readable by the `mosquitto` user | `chown mosquitto:mosquitto`, `chmod 600` (§3.1) |
| mosquitto: `Invalid protocol value (websocketsacl_file)` | config file did not end with a newline; `echo >>` glued the next line on | end files with a newline before appending (§3.2) |
| mosquitto: `Unable to load server key` | key not readable by the `certs` group | §2 |
| mosquitto refuses to start after adding an ACL block | same glued-line problem in `/etc/mosquitto/acl` | `tail -c1 acl \| od -c`, add the newline |
| mosquitto: persistence errors | `/var/lib/mosquitto` missing | `install -d -m 700 -o mosquitto -g mosquitto /var/lib/mosquitto` |
| any service: `Start request repeated too quickly` | systemd start limit after repeated failures | fix the cause, `systemctl reset-failed <unit>`, `systemctl start <unit>` |
| nginx: `unknown directive "http2"` | nginx 1.20 | `listen 443 ssl http2;` (§6) |
| `sudo: pnpm: command not found` / `node-red: command not found` | `/usr/local/bin` not in sudo's `secure_path` | full paths |
| `settings.js` never appears | `node-red --help` does not write it | run it for real with `timeout 10` (§4.1) |
| Zigbee2MQTT frontend: 401 on `site-….webmanifest` in the console | browsers fetch the manifest without Basic Auth | the `webmanifest` location in §6; cosmetic otherwise |
| Zigbee2MQTT dies when the SLZB-06 drops off the network and stays dead | systemd start limit | `StartLimitIntervalSec=0`, `Restart=always` (§9.3) |
| Shelly BLU device will not pair | it is in Bluetooth mode | five quick presses, not a long hold (§9.5) |
| button presses "get lost" | frontend shows changes only; single/double/triple timing; or RF (§12) | `mosquitto_sub` on the device topic; then the debug log's RSSI |
| sensor reports only on the second press | −89 dBm, no router | router between them (§12) |
| SLZB-06 Settings shows a different channel and PAN | that page is the internal hub mode, unused as coordinator | ignore it; `bridge/info` → `network.channel` is the truth (§8) |
| mosquitto log: `OpenSSL: unexpected eof` from an H&T | the sensor went back to sleep mid-TLS | normal |
| zigmon dashboard: "the monitoring daemon is not responding" | service down, or another `api_port` | `systemctl status zigmon`, `journalctl -u zigmon -n 30` |
| zigmon dashboard: "the API token is unreadable" | php-fpm not running as nginx, or nginx not in the `zigmon` group | the installer does both; `systemctl restart php-fpm` |
| `zigmon` in a shell: "another zigmon is already running" / refuses as root | the service holds 9849; root would leave root-owned db files | use `systemctl`; foreground only as `sudo -u zigmon zigmon` |
| zigmon permit-join: "publish failed" | ACL lacks `write zigbee2mqtt/bridge/request/#` | §3.2 |
| a plug shows "unreachable" in zigmon | port 80 on the plug not reachable from the server (other subnet, firewall) or wrong password | `curl http://<plug>/rpc/Shelly.GetDeviceInfo` from the server |
| `zigmon shelly … ble off` but the plug still advertises | needs a reboot; or Matter enabled and not commissioned | `ble off --reboot`, then `matter off --reboot` (§11.3) |
| browser: 403 on the zigmon site | your subnet is not in an `allow` line | `/etc/nginx/conf.d/zigmon.conf`, `systemctl reload nginx` |
| `sudo` cannot find `zigmon` | `/usr/local/bin` again | `sudo /usr/local/bin/zigmon --check` |
| Zigbee2MQTT `./update.sh`: "dubious ownership", then `No update available` | run as root over a `z2m`-owned, shallow clone; the version check silently reads nothing | do not use it — §9.4 |

**Maintenance.** Back up `/root/z2m-configuration.yaml.bak`,
`/opt/zigbee2mqtt/app/data/database.db`, `/etc/zigmon/config.json` and
`/var/lib/zigmon/history.db` (`sqlite3 … ".backup"` while it runs), plus
`/var/lib/nodered`. Upgrade zigmon with `./install.sh --upgrade`; upgrade
Zigbee2MQTT with the steps in §9.4 — not with the repository's `update.sh`,
which cannot read a version out of this clone and says so only by claiming
there is nothing to do.
