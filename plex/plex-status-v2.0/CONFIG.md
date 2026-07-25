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
čtení spouští `smartctl`. Tento odstup ignoruje wake tlačítko, denní průchod
a příkaz `plexmon --refresh`.

Do historie se zapisují **jen čerstvé odečty ze SMART**. Když disk spí, zůstane
v grafu mezera: zapsat místo toho poslední známou hodnotu by znamenalo tvrdit
něco, co jsme nezměřili, a všechna roztočení nasbíraná za dobu spánku by se pak
připsala k pěti minutám mezi dvěma vzorky.

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

## Ovládání televizí

| volba | výchozí | význam |
|---|---|---|
| `atv_enable` | `false` | zapne ovládání přehrávání (platí pro všechny značky) |
| `atv_remote` | `""` | cesta k `atvremote`; prázdné = najde si ho sám |
| `atv_storage` | `/var/lib/plex-status/atv-creds.json` | údaje z párování Apple TV |
| `tv_creds` | `/var/lib/plex-status/tv-creds.json` | údaje z párování LG a Samsung |
| `tv_samsung_keys` | viz níže | které klávesy posílat na Samsung |
| `tv_pair_timeout` | `90` | kolik sekund čekat, než potvrdíš dotaz na televizi |

Podporované jsou tři značky. Plex vlastní klienty ovládat neumí — jeho aplikace
se serveru jako ovladatelné nehlásí a `/clients` vrací prázdno — takže démon
mluví s televizí přímo jejím vlastním protokolem.

| značka | knihovna | co umí |
|---|---|---|
| Apple TV | [pyatv](https://pyatv.dev) | skutečné ovládání přehrávání, skok o 10 s |
| LG (webOS) | [aiowebostv](https://pypi.org/project/aiowebostv/) | skutečné ovládání přehrávání |
| Samsung (Tizen) | [samsungtvws](https://pypi.org/project/samsungtvws/) | jen stisky kláves dálkového ovladače |

### Rozdíl mezi značkami

U **Apple TV a LG** jde o opravdové ovládání přehrávání: řekneš „pauza" a
televize pauzne, bez ohledu na to, co je zrovna na obrazovce zaostřené.

U **Samsungu** taková možnost neexistuje. Jeho rozhraní umí jen posílat kódy
kláves dálkového ovladače a co s nimi udělá, rozhoduje aplikace na popředí.
V praxi to funguje, protože Plex na Tizenu na tyhle klávesy reaguje, ale je
to křehčí: když je na obrazovce menu nebo dialog, stisk se použije tam.

Které klávesy se posílají, jde změnit — ne každý ovladač posílá stejné kódy:

```json
{
  "tv_samsung_keys": {
    "play_pause": "KEY_PLAY_BACK",
    "skip_forward": "KEY_FF",
    "skip_backward": "KEY_REWIND"
  },
  "tv_samsung_key_delay": 0.4
}
```

Hodnotou může být **jedna klávesa nebo posloupnost**. Výchozí je jedna, protože
dvě jsou nepředvídatelné: jestli šipka přetočí, nebo jen probudí ovládací lištu,
záleží na tom, jestli je lišta zrovna vidět — dvojstisk tak přeskočil deset
sekund, dvacet, nebo nic. Posloupnost použij jen tehdy, když ji tvoje televize
opravdu potřebuje; `tv_samsung_key_delay` je prodleva mezi klávesami v sekundách.

### Když tlačítko nic nedělá

Který kód co udělá, není nikde dané — rozhoduje aplikace na popředí. Proto je
lepší to vyzkoušet než hádat:

```bash
plexmon --atv-key "Obývák" KEY_FF
```

Pošle jednu klávesu a ty se podíváš, jestli televize zareagovala. Kandidáti na
přetáčení, seřazení podle toho, jak často fungují:

| kód | co to bývá | jak daleko skočí |
|---|---|---|
| `KEY_FF`, `KEY_REWIND` | rychlé přetáčení — výchozí | v Plexu 30 s |
| `KEY_RIGHT`, `KEY_LEFT` | šipky | v Plexu 10 s, ale často až po probuzení lišty |
| `KEY_ENTER` | probudí ovládací lištu | — |

**O kolik se skočí, neurčuje démon, ale aplikace na televizi.** My posíláme jen
stisk klávesy. Chceš-li místo třiceti sekund deset, přepni na šipky:

```json
{
  "tv_samsung_keys": {
    "play_pause": "KEY_PLAY_BACK",
    "skip_forward": "KEY_RIGHT",
    "skip_backward": "KEY_LEFT"
  }
}
```

Nevýhoda šipek je, že první stisk po chvíli nečinnosti může jen vyvolat ovládací
lištu a nepřetočit. Lišta pak ale zůstává viditelná několik sekund, takže při
opakovaném mačkání to sedí — nazmar přijde jen ten úplně první stisk.

Ověřuj to vždy **jedním stiskem**. Když se ti zdá, že klávesa „funguje jen
někdy", bývá to tím, že jich posíláš víc za sebou a aplikace na ně reaguje
pokaždé jinak podle toho, co je na obrazovce.

Až najdeš, co funguje, zapiš to do `tv_samsung_keys`. Když je potřeba nejdřív
probudit lištu, použij posloupnost, třeba `["KEY_ENTER", "KEY_RIGHT"]`.

Starší modely mohou chtít `KEY_PLAY` a `KEY_PAUSE` zvlášť; pak nastav
`play_pause` na jednu z nich a druhou nechej být, nebo použij `KEY_ENTER`.

Poznámka k párování: některé modely (třeba řada Q60) posílají hned po připojení
zprávu `ms.remote.touchDisable`, kterou knihovna považuje za selhání spojení.
Démon proto seznam tolerovaných zpráv rozšiřuje — kdyby ti párování hlásilo
`the television refused` s nějakou jinou zprávou `ms.*`, pošli ji a doplním ji
mezi tolerované.

### Hledání televizí

Apple TV se hledají po síti (mDNS). LG a Samsung ne — nemá to smysl, protože
adresu televize, na které se zrovna hraje, **hlásí sám Plex**. Démon tedy jen
ověří tu jednu adresu:

- Samsung odpoví na `http://<ip>:8001/api/v2/` popisem zařízení
- LG má otevřený WebSocket na portu 3000

Výsledek se pamatuje, takže se to neopakuje při každém dotazu.

### Párování

**Apple TV** ukáže na obrazovce čtyřmístný kód, který zadáš do okna na stránce.

**LG a Samsung** se místo toho zeptají přímo na televizi — objeví se dotaz,
který potvrdíš fyzickým ovladačem. Žádný kód se nezadává, takže je to jeden
krok. Stránka to pozná sama a okno na PIN nenabídne.

Na potvrzení máš `tv_pair_timeout` sekund, výchozí 90. Když nestíháš dojít
k ovladači, zvyš to — čeká se na člověka, ne na stroj.

### Instalace knihoven

Všechny potřebují **Python 3.10 nebo novější** a musí být dostupné tomu
Pythonu, pod kterým běží démon:

```bash
dnf install -y python3.12
python3.12 -m pip install pyatv          # Apple TV
python3.12 -m pip install aiowebostv     # LG
python3.12 -m pip install samsungtvws    # Samsung
```

Stačí nainstalovat jen ty, které opravdu máš. Když knihovna chybí, vypne se
jen ta jedna značka — ostatní i hlídání disků běží dál.

Pokud démon dosud běžel pod systémovým Pythonem, přepni ho v unit souboru:

```ini
ExecStart=/usr/bin/python3.12 /usr/local/lib/plexmon/plexmon.py
```

Do systémového Pythonu spravovaného RPM nic z toho **neinstaluj** — `pip` se
pokusí přepsat balíčky patřící `dnf` a skončí chybou.

### Jak to funguje uvnitř

U Apple TV a LG démon **drží spojení otevřené**. Navázání šifrovaného sezení
trvá tři až pět sekund, takže kdyby se dělalo při každém stisku, tlačítko pauzy
by reagovalo za pět sekund a bylo by k ničemu. Spojení se otevře na pozadí,
jakmile se objeví přehrávání, takže i první stisk je okamžitý.

Když spojení spadne (televize usnula, změnila adresu), démon se jednou pokusí
připojit znovu. U Apple TV navíc existuje záloha přes `atvremote` — pomalejší,
řádově sekundy, ale tlačítka fungují dál.

Samsung spojení nedrží: jeho knihovna je synchronní a jedno spojení na příkaz
je tam levné.

Kvůli rychlému spojení démon knihovny **importuje**. Import je odložený na
první použití, běží na pozadí a je ošetřený: když se knihovna nenačte, ta
značka se vypne a hlídání disků tím není dotčené. Je to ale slabší záruka než
u zbytku démona, který vystačí se standardní knihovnou.

### Limity služby

S načtenými knihovnami sedí démon kolem 90–110 MB. Výchozí strop 200 MB je na
to málo:

```ini
# /etc/systemd/system/plex-status.service.d/atv.conf
[Service]
MemoryMax=400M
CPUQuota=60%
```

Podle spotřeby se dá poznat, jestli je rychlá cesta aktivní:

```bash
systemctl show plex-status.service -p MemoryCurrent
```

Pod 30 MB = knihovna ještě není načtená; kolem 90–110 MB = spojení běží.

### Používání

Zapni `atv_enable`, restartuj démona a v panelu Now Playing se u přehrávajícího
streamu objeví tlačítko **Pair this TV**. Po spárování se z něj stanou ovládací
prvky: zpět, přehrát/pauza, vpřed.

Totéž jde z příkazové řádky:

```bash
plexmon --atv                      # co je známé a co je spárované, včetně značky
plexmon --atv-scan                 # projít síť znovu (najde jen Apple TV)
plexmon --atv-probe 192.168.40.55  # co je za televize na téhle adrese
plexmon --atv-pair 192.168.40.55   # spárovat LG nebo Samsung podle adresy
plexmon --atv-pair "Obývák"        # nebo podle názvu, když už je známá
plexmon --atv-key "Obývák" KEY_FF   # vyzkoušet jednu klávesu (jen Samsung)
plexmon --atv-unpair "Obývák"
plexmon --atv-unpair-all
```

Apple TV se hlásí po síti, takže je ve výpisu hned. **LG a Samsung ne** — dokud
nejsou spárované, nikdo o nich neví, a proto se zadávají **IP adresou**. Tu
najdeš buď v nastavení televize, nebo v panelu Now Playing, kde ji hlásí Plex.

Stream se s televizí páruje podle **IP adresy**, kterou Plex u přehrávače
hlásí — ne podle názvu, který nemusí být jedinečný.

V pravém horním rohu karty je tlumený odkaz **unpair** (první klik se zeptá,
druhý provede). Zruší to jen *náš* přístup — televize si server dál vede mezi
spárovanými ovladači a odebrat ho tam jde jen v jejím nastavení.

Když něco nefunguje, důvod je v logu:

```bash
journalctl -u plex-status.service -f | grep -iE "apple tv|samsung|lg |television"
```

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
  "api_port": 9847,

  "atv_enable": false
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

  "atv_enable": false,
  "atv_remote": "",
  "atv_storage": "/var/lib/plex-status/atv-creds.json",
  "tv_creds": "/var/lib/plex-status/tv-creds.json",
  "tv_samsung_keys": {
    "play_pause": "KEY_PLAY_BACK",
    "skip_forward": "KEY_RIGHT",
    "skip_backward": "KEY_LEFT"
  },

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
