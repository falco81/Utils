# Konfigurace plexmon

Vše se nastavuje v **jednom** JSON objektu v `/etc/plex-status/config.json`.
Cokoli, co v souboru neuvedeš, si podrží výchozí hodnotu — klidně tam nech jen
to, co opravdu měníš.

JSON neumí komentáře, takže tenhle soubor slouží jako referenční příručka
a čistou šablonu k vložení najdeš na konci.

```bash
mkdir -p /etc/plex-status
# ... vlož obsah ...
systemctl restart plex-status.service
plexmon --diag        # sekce "effective settings" ukáže, co doopravdy platí
```

Neznámý klíč se **nepoužije a démon to nahlásí** (v logu, v `--diag` i v
`--check`) — překlep tedy nezapadne. Totéž platí pro rozbitý JSON.

Každou volbu lze alternativně nastavit proměnnou prostředí s prefixem
`PLEXMON_`, např. `PLEXMON_TEMP_WARN=55`. Prostředí má přednost před souborem.

---

## Teploty

| volba | výchozí | význam |
|---|---|---|
| `temp_warn` | `60` | °C, od kdy hlásit plotnový disk jako teplý |
| `temp_crit` | `70` | °C, od kdy hlásit kritickou teplotu |
| `temp_warn_nvme` | `70` | totéž pro NVMe, které nehlásí vlastní limity |
| `temp_crit_nvme` | `80` | |

Práh se pro každý disk vybírá v tomhle pořadí:

1. **co hlásí sám disk** — zná svoje zatížitelnost nejlíp (tvůj Intel hlásí
   77 °C varování / 80 °C kritické)
2. **NVMe výchozí hodnoty**, pokud jde o NVMe a nic nehlásí
3. **globální hodnoty**, určené pro plotnové disky

Flash snese víc než plotny: NVMe při 60 °C je běžný stav, plotnový disk při
60 °C už ne. Kdyby se na NVMe použily globální prahy, hlásil by poplach dávno
předtím, než by výrobce považoval teplotu za problém.

## Uspávání disků

| volba | výchozí | význam |
|---|---|---|
| `spindown_after_s` | `900` | po kolika sekundách bez I/O považovat disk za uspaný |

Je to **předpoklad, ne měření** — démon se disku na stav nikdy neptá, protože
přes USB most se na odpověď nedá spolehnout a dotaz by disk probudil. Skutečnou
hodnotu zjistíš skriptem `spindown-info.sh`. Démon si předpoklad pasivně hlídá:
když čítač roztočení naroste, aniž by disk kdy hlásil jako uspaný, upozorní na
to v `plexmon --check`.

| volba | výchozí | význam |
|---|---|---|
| `bridge_wakes_siblings` | `true` | disky na jednom USB mostu sdílejí stav napájení |

Vícepozicové USB skříně roztáčejí všechny své disky společně. Se zapnutou volbou
se disk, jehož soused pracuje, hlásí jako točící se (protože se točí) a smí se
z něj číst SMART zadarmo. Disky na **jiném** mostu to neovlivní. Nastav `false`
pro skříně, které opravdu napájejí každou pozici zvlášť.

| volba | výchozí | význam |
|---|---|---|
| `use_hdparm` | `false` | ptát se na stav napájení přes hdparm |
| `wake_standby` | `false` | číst SMART i z uspaného disku při běžném běhu |

Obojí nech vypnuté. `hdparm -C` přes JMicron most vrací nesmyslné výsledky
a navíc disk probudí. `wake_standby` ruší hlavní záruku celého démona.

## Čtení SMART

| volba | výchozí | význam |
|---|---|---|
| `smart_min_interval` | `300` | minimální odstup mezi rutinními čteními SMART (s) |
| `smart_daily_at` | `4` | hodina, kdy jednou denně přečíst SMART ze všech disků |

Bdělý disk se nečte při každém průchodu — hodnoty se mění v řádu minut a každé
čtení spouští `smartctl`. Wake tlačítko a denní průchod tento odstup ignorují.

## Kadence sběru

| volba | výchozí | význam |
|---|---|---|
| `fast_interval` | `10` | rychlá smyčka (s) — přehrávání, aktivita, stav napájení |
| `slow_interval` | `300` | pomalá práce (s) — odezva API, databáze, vzorek historie |

Rychlá smyčka pracuje jen s pamětí (`/proc`, `/sys`, Plex API) a disků se
nedotýká.

## Historie a grafy

| volba | výchozí | význam |
|---|---|---|
| `history_days` | `7` | jak dlouho držet vzorky |
| `history_max` | `25000` | tvrdý strop počtu vzorků |
| `history_points` | `140` | kolik bodů vložit do `data.json` pro první vykreslení |

Retence se řídí stářím, ne počtem — změna kadence tedy historii nezkrátí.

## Výkonnostní sonda

| volba | výchozí | význam |
|---|---|---|
| `perf_endpoints` | `["/identity","/library/sections","/hubs"]` | co měřit |
| `perf_samples` | `5` | kolik vzorků na endpoint (bere se medián) |
| `perf_proxy_url` | `"https://plex.falco81.net"` | adresa přes reverzní proxy |

Rozdíl mezi měřením napřímo a přes proxy dává číslo na kartě „Reverse proxy".
Prázdný řetězec `""` sondu vypne. Měří se přes jedno znovupoužité spojení, aby
se do výsledku nepočítal TLS handshake.

## Síť a API

| volba | výchozí | význam |
|---|---|---|
| `api_host` | `"127.0.0.1"` | adresa, na které démon poslouchá |
| `api_port` | `9847` | port API |

`api_host` nech na `127.0.0.1`. Démon běží jako root a přes API jde probouzet
disky — vystavit ho do sítě není dobrý nápad ani s tokenem. Když změníš port,
uprav ho i nahoře v `plexmon-api.php`.

| volba | výchozí | význam |
|---|---|---|
| `plex_url` | `"http://127.0.0.1:32400"` | Plex Media Server |
| `plex_prefs` | cesta k `Preferences.xml` | odsud se čte token |

## Cesty

| volba | výchozí | význam |
|---|---|---|
| `state_dir` | `/var/lib/plex-status` | cache SMART a historie |
| `web_dir` | `/var/www/html/smart` | kam se píše `data.json` a plakáty |
| `run_dir` | `/run/plex-status` | token API |

Změníš-li adresář, soubory v něm se posunou s ním. Jednotlivé soubory
(`data_file`, `cache_file`, `history_file`, `sessions_web`, `history_web`,
`token_file`) jde nastavit i samostatně, pak mají přednost.

Při změně `web_dir` nezapomeň upravit `ReadWritePaths` v `plex-status.service`
a nasadit `index.php` do nového umístění.

| volba | výchozí | význam |
|---|---|---|
| `smartctl` | `/usr/sbin/smartctl` | cesta k binárce |
| `hdparm` | `/usr/sbin/hdparm` | cesta k binárce (nepoužívá se) |

---

## Šablona k vložení

Běžně měněné volby s výchozími hodnotami. Smaž řádky, které neměníš — co
neuvedeš, si podrží výchozí hodnotu.

```json
{
  "temp_warn": 60,
  "temp_crit": 70,
  "temp_warn_nvme": 70,
  "temp_crit_nvme": 80,

  "spindown_after_s": 900,
  "bridge_wakes_siblings": true,

  "smart_min_interval": 300,
  "smart_daily_at": 4,

  "fast_interval": 10,
  "slow_interval": 300,

  "history_days": 7,
  "history_max": 25000,
  "history_points": 140,

  "perf_samples": 5,
  "perf_proxy_url": "https://plex.falco81.net",

  "api_host": "127.0.0.1",
  "api_port": 9847
}
```

## Úplná šablona

Všechno, co jde nastavit — včetně cest a voleb, které se běžně nemění.
Ber ji spíš jako referenci než jako soubor k nasazení.

```json
{
  "temp_warn": 60,
  "temp_crit": 70,
  "temp_warn_nvme": 70,
  "temp_crit_nvme": 80,

  "spindown_after_s": 900,
  "bridge_wakes_siblings": true,
  "use_hdparm": false,
  "wake_standby": false,

  "smart_min_interval": 300,
  "smart_daily_at": 4,

  "fast_interval": 10,
  "slow_interval": 300,

  "history_days": 7,
  "history_max": 25000,
  "history_points": 140,

  "perf_endpoints": ["/identity", "/library/sections", "/hubs"],
  "perf_samples": 5,
  "perf_proxy_url": "https://plex.falco81.net",

  "api_host": "127.0.0.1",
  "api_port": 9847,

  "plex_url": "http://127.0.0.1:32400",
  "plex_prefs": "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",

  "smartctl": "/usr/sbin/smartctl",
  "hdparm": "/usr/sbin/hdparm",

  "state_dir": "/var/lib/plex-status",
  "web_dir": "/var/www/html/smart",
  "run_dir": "/run/plex-status",

  "data_file": "/var/www/html/smart/data.json",
  "sessions_web": "/var/www/html/smart/sessions.json",
  "history_web": "/var/www/html/smart/history-full.json",
  "cache_file": "/var/lib/plex-status/smart-cache.json",
  "history_file": "/var/lib/plex-status/history.json",
  "token_file": "/run/plex-status/api-token"
}
```

Poslední blok cest uveď jen tehdy, když chceš jednotlivé soubory jinam než do
jejich adresáře — jinak stačí `state_dir` / `web_dir` / `run_dir` a soubory se
posunou s nimi.

## Minimální varianta

Pro tvůj server v praxi stačí tohle:

```json
{
  "spindown_after_s": 600,
  "bridge_wakes_siblings": true
}
```
