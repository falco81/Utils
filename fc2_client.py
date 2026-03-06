"""
FC2 Client — Focusrite Control 2 WebSocket API
===============================================
Trvalé párování + ovládání Scarlett zařízení přes AES70/OCP.1

Porty:
  58323 — autentizační kanál  (pair, RequestApproval)
  58322 — řídicí kanál        (gain, phantom, notifikace) — šifrovaný Noise_NK

Použití:
  python fc2_client.py pair        # První spárování (generuje keypair + zobrazí QR)
  python fc2_client.py status      # Stav zařízení
  python fc2_client.py discover    # Prozkoumá ONo strom
  python fc2_client.py monitor     # Živý monitor změn (oba kanály)
  python fc2_client.py interactive # REPL (výchozí)

Volby:
  --name <jméno>          Jméno zařízení v FC2 (výchozí: iPad)
  --auth <soubor>         Auth soubor (výchozí: fc2_auth.json)
  --fc2-pubkey <64hex>    FC2 statický Noise veřejný klíč (32 b, hex)

pip install websockets cryptography
"""

import asyncio, struct, hashlib, json, os, sys, time, secrets
import websockets
from pathlib import Path

# ── Konfigurace ──────────────────────────────────────────────────
FC2_IP       = "192.168.40.12"
FC2_HOST     = "workshop.local."   # mDNS — FC2 validuje Host header
FC2_PORT     = 58323               # autentizační port
FC2_PORT2    = 58322               # řídicí / šifrovaný port
AUTH_FILE    = Path("fc2_auth.json")
DEVICE_NAME  = "iPad"

# FC2 statický Noise veřejný klíč — neznámý ze statické analýzy pcap.
# Nastav přes --fc2-pubkey <64hex> nebo uložením do AUTH_FILE jako "fc2_pubkey_hex".
# Pokud je None: 58322 se připojí, zaloguje co FC2 pošle, a pokračuje na 58323.
FC2_STATIC_PUBKEY: bytes | None = None  # přepíše --fc2-pubkey; fallback = DUMMY_EPUB

# ── Crypto ───────────────────────────────────────────────────────
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes as _hashes


def generate_keypair() -> tuple[bytes, bytes]:
    """Vygeneruj X25519 keypair. Vrátí (priv_bytes 32b, pub_bytes 32b)."""
    priv = X25519PrivateKey.generate()
    return priv.private_bytes_raw(), priv.public_key().public_bytes_raw()


def _noise_hkdf2(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    """Noise HKDF-SHA256 → (ck_new, k_new), oba 32 b."""
    out = HKDF(algorithm=_hashes.SHA256(), length=64, salt=ck, info=b"").derive(ikm)
    return out[:32], out[32:]


class NoiseNK:
    """
    Noise_NK_25519_AESGCM_SHA256  —  initiator side.

    Pattern (Noise spec §7.4 NK):
        <- s                   (FC2 statický klíč — znám dopředu)
        -> e, es               (náš ephemeral + DH se serverem)
        <- e, ee               (FC2 ephemeral + DH ee)

    Framing z pcap:
        msg1 (náš init)  75 b   BEZ délkového prefixu
        msg2 (FC2 resp)  51 b   S 2b big-endian délkovým prefixem
        transport        var.   S 2b big-endian délkovým prefixem
    """
    PROTO = b"Noise_NK_25519_AESGCM_SHA256"

    def __init__(self, fc2_static_pub: bytes, epub_priv: bytes,
                 prologue: bytes = b""):
        self.fc2_s_pub    = fc2_static_pub
        self.epub_priv    = epub_priv
        self.e_priv_raw, self.e_pub = generate_keypair()
        # Noise hash stav
        h = hashlib.sha256(self.PROTO).digest()
        self.ck = h; self.h = h
        self.k: bytes | None = None
        self.n_enc = self.n_dec = 0
        self.send_k = self.recv_k = None
        self._done  = False
        self._mix_hash(prologue)
        self._mix_hash(fc2_static_pub)

    def _mix_hash(self, data: bytes):
        self.h = hashlib.sha256(self.h + data).digest()

    def _mix_key(self, ikm: bytes):
        self.ck, self.k = _noise_hkdf2(self.ck, ikm)
        self.n_enc = self.n_dec = 0

    def _nonce(self, n: int) -> bytes:
        return b"\x00\x00\x00\x00" + n.to_bytes(8, "big")

    def _enc_hash(self, pt: bytes) -> bytes:
        ct = AESGCM(self.k).encrypt(self._nonce(self.n_enc), pt, self.h) if self.k else pt
        if self.k: self.n_enc += 1
        self._mix_hash(ct)
        return ct

    def _dec_hash(self, ct: bytes) -> bytes:
        pt = AESGCM(self.k).decrypt(self._nonce(self.n_dec), ct, self.h) if self.k else ct
        if self.k: self.n_dec += 1
        self._mix_hash(ct)
        return pt

    def build_msg1(self, payload: bytes = b"") -> bytes:
        """msg1: e_pub(32) + ENCRYPT(payload) — BEZ délkového prefixu."""
        self._mix_hash(self.e_pub)
        e_key = X25519PrivateKey.from_private_bytes(self.e_priv_raw)
        dh_es = e_key.exchange(X25519PublicKey.from_public_bytes(self.fc2_s_pub))
        self._mix_key(dh_es)
        return self.e_pub + self._enc_hash(payload)

    def process_msg2(self, body: bytes) -> bytes:
        """
        Zpracuj FC2 odpověď (body = msg2 bez 2b délkového prefixu).
        Po návratu je handshake hotov, send_k/recv_k jsou nastaveny.
        """
        if len(body) < 48:
            raise ValueError(f"msg2 příliš krátký: {len(body)}b (min 48)")
        e2_pub = body[:32]
        self._mix_hash(e2_pub)
        e_key = X25519PrivateKey.from_private_bytes(self.e_priv_raw)
        dh_ee = e_key.exchange(X25519PublicKey.from_public_bytes(e2_pub))
        self._mix_key(dh_ee)
        payload = self._dec_hash(body[32:])
        # Split — odvoď transportní klíče
        self.send_k, self.recv_k = _noise_hkdf2(self.ck, b"")
        self.n_enc = self.n_dec = 0
        self._done = True
        return payload

    def enc_transport(self, pt: bytes) -> bytes:
        if not self._done: raise RuntimeError("Handshake nebyl dokončen")
        ct = AESGCM(self.send_k).encrypt(self._nonce(self.n_enc), pt, b"")
        self.n_enc += 1
        return ct

    def dec_transport(self, ct: bytes) -> bytes:
        if not self._done: raise RuntimeError("Handshake nebyl dokončen")
        pt = AESGCM(self.recv_k).decrypt(self._nonce(self.n_dec), ct, b"")
        self.n_dec += 1
        return pt

    @staticmethod
    def frame(data: bytes) -> bytes:
        return struct.pack(">H", len(data)) + data

    @staticmethod
    def unframe(raw: bytes) -> bytes:
        if len(raw) < 2: raise ValueError("Příliš krátká zpráva")
        n = struct.unpack_from(">H", raw)[0]
        if 2 + n > len(raw): raise ValueError(f"Prefix {n} přesahuje délku {len(raw)}b — bez prefixu")
        return raw[2: 2 + n]


# ── EncryptedChannel ─────────────────────────────────────────────
class EncryptedChannel:
    """WS + Noise transport — transparentní šifrování/dešifrování."""

    def __init__(self, ws, noise: NoiseNK | None):
        self.ws    = ws
        self.noise = noise

    @property
    def is_encrypted(self) -> bool:
        return self.noise is not None and getattr(self.noise, "_done", False)

    async def send(self, data: bytes):
        if self.is_encrypted:
            await self.ws.send(NoiseNK.frame(self.noise.enc_transport(data)))
        else:
            await self.ws.send(data)

    async def recv(self) -> bytes:
        raw = await self.ws.recv()
        if isinstance(raw, str): raw = raw.encode()
        if self.is_encrypted and len(raw) >= 2:
            try:
                return self.noise.dec_transport(NoiseNK.unframe(raw))
            except Exception:
                return raw
        return raw

    async def close(self):
        try: await self.ws.close()
        except Exception: pass


# ── HTTP hlavičky — identické s .pyok (minimální, bez Host, bez komprese) ───
HEADERS = {
    "Origin":     "capacitor://localhost",
    "User-Agent": "Mozilla/5.0 (iPad; CPU OS 18_7_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1",
}
HEADERS_CTRL = HEADERS  # stejné pro oba porty

# ── AES70 konstanty ──────────────────────────────────────────────
OCA_SYNC     = 0x3B
MSG_CMD_RR   = 1
MSG_CMD      = 2
MSG_NOTIF    = 3
MSG_RESPONSE = 5

KEEPALIVE = bytes.fromhex("3b00010000000b0400010001")

# ── Ověřená init sekvence z ipad-full.pcapng ─────────────────────
# Pořadí před SendQR (nutné, jinak FC2 vrátí 'rejected'):
#   1. KA                (posílá connect_ws automaticky)
#   2. DISCOVERY         (subscribes na ONo=0x64 + ONo=0x0004)
#   3. GET_ROLE          (GetRole ONo=0x1000 DL=1 MI=5 → odpověď 'AuthenticationAgent')
#   4. AUTH_SUBSCRIBE    (3× cmd: AddSubscription + MI=3 + MI=4 na ONo=0x1000)
#      → FC2 odpoví 'ready'
#   5. SendQR            (s PC=2!)

DISCOVERY = bytes.fromhex(
    "3b000100000040010002"                                  # header
    "00000026000000000000000400030001"                      # cmd1 header: ONo=4 DL=3 MI=1
    "050000006400010001 0000041f0001000100000100 00"        # cmd1 params
    "0000001100000001000000640003000500"                    # cmd2: ONo=0x64 DL=3 MI=5
    .replace(" ", "")
)

GET_ROLE = bytes.fromhex(
    "3b00010000001a0100010000001100000002000010000001000500"
)  # GetRole ONo=0x1000 DL=1 MI=5 (27b)

# SUBSCRIBE_QUERY — přesná kopie z funkčního .pyok (MI=5 v cmd3, ne MI=4!)
SUBSCRIBE_QUERY = bytes.fromhex(
    "3b000100000051010003000000260000000300000004000300010500001000000100"
    "010000041f00010001000001000000000011000000040000100000030003000000001"
    "100000005000010000003000500"
)  # (82b) — cmd1: AddSub(0x1000), cmd2: MI=3, cmd3: MI=5

# Dummy hodnoty z ipad.pcapng (jen fallback pokud není auth soubor)
DUMMY_EPUB  = bytes.fromhex("a72ef4f74bca62abe4b2479ee8549905eb8499e22e430253e7084f677dcbef39")
DUMMY_TOKEN = bytes.fromhex(
    "58c7160a361740072e3ae24645f5237d22d03c92da5e78b56c4368666707f9b5"
    "5a831ea262c533c8a98df207"
)

# ── PDU builder ──────────────────────────────────────────────────
_hdl = [1]
def next_handle() -> int:
    h = _hdl[0]; _hdl[0] += 1; return h

def oca_blob(d: bytes) -> bytes:    return struct.pack(">H", len(d)) + d
def oca_float32(v: float) -> bytes: return struct.pack(">f", v)
def oca_bool(v: bool) -> bytes:     return bytes([1 if v else 0])


def build_cmd(ono: int, dl: int, mi: int, params: bytes = b"",
              response_required: bool = True, handle: int = None) -> bytes:
    if handle is None: handle = next_handle()
    body    = struct.pack(">IIHHB", handle, ono, dl, mi, 0) + params
    total   = 10 + 4 + len(body)
    mt      = MSG_CMD_RR if response_required else MSG_CMD
    header  = struct.pack(">BHI", OCA_SYNC, 1, total - 1)
    header += struct.pack(">BH", mt, 1)
    return header + struct.pack(">I", len(body) + 4) + body


def build_send_qr(epub: bytes, token: bytes) -> bytes:
    # PC=0 — identické s .pyok (fungující verze)
    return build_cmd(0x1000, 3, 1, oca_blob(epub) + oca_blob(token), handle=6)

def build_request_approval(qr_bytes: bytes) -> bytes:
    return build_cmd(0x1000, 3, 2, oca_blob(qr_bytes), handle=7)

# ── Auth persistence (v2) ─────────────────────────────────────────
def save_auth(epub_pub: bytes, epub_priv: bytes, token: bytes,
              ip: str, fc2_pubkey: bytes | None = None,
              qr_raw: bytes | None = None):
    AUTH_FILE.write_text(json.dumps({
        "version":        2,
        "ip":             ip,
        "port_auth":      FC2_PORT,
        "port_ctrl":      FC2_PORT2,
        "epub_hex":       epub_pub.hex(),
        "epub_priv_hex":  epub_priv.hex(),
        "token_hex":      token.hex(),
        "qr_raw_hex":     qr_raw.hex() if qr_raw else None,  # 64b raw QR pro RequestApproval
        "fc2_pubkey_hex": fc2_pubkey.hex() if fc2_pubkey else None,
        "paired_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))
    print(f"[*] Uloženo: {AUTH_FILE}")


def load_auth() -> dict | None:
    if not AUTH_FILE.exists(): return None
    try:
        d = json.loads(AUTH_FILE.read_text())
        if d.get("version", 1) >= 2:
            epub_pub  = bytes.fromhex(d["epub_hex"])
            epub_priv = bytes.fromhex(d["epub_priv_hex"])
            token     = bytes.fromhex(d["token_hex"])
            fc2pk     = bytes.fromhex(d["fc2_pubkey_hex"]) if d.get("fc2_pubkey_hex") else None
        else:
            # v1 starý formát — epub_priv chybí
            qr = bytes.fromhex(d["qr_hex"])
            epub_pub = qr[:32]; token = qr[32:]
            epub_priv = None; fc2pk = None
            print("[WARN] Auth soubor v1 — chybí epub_priv. Doporučuji `pair` znovu.")
        qr_raw = bytes.fromhex(d["qr_raw_hex"]) if d.get("qr_raw_hex") else epub_pub + token
        # fc2_pubkey = Noise statický klíč FC2 pro toto párování = QR[:32] (fc2_epub).
        # FC2 generuje nový Noise keypair per-pairing → fc2_epub se mění s každým párováním.
        # DUMMY_EPUB je jen URL endpoint (konstantní), NENÍ to Noise klíč.
        if fc2pk is None and len(qr_raw) >= 32:
            fc2pk = qr_raw[:32]
        return {
            "epub_pub":  epub_pub,
            "epub_priv": epub_priv,
            "token":     token,
            "qr_bytes":  qr_raw,
            "fc2_pubkey": fc2pk,
            "ip":        d.get("ip", FC2_IP),
        }
    except Exception as e:
        print(f"[WARN] {AUTH_FILE}: {e}"); return None


# ── WebSocket helpers ─────────────────────────────────────────────
def status_of(msg: bytes) -> str:
    lo = msg.lower()
    for k in [b"approved",b"scanning",b"pending",b"ready",b"denied",b"rejected",b"error"]:
        if k in lo: return k.decode()
    if msg == KEEPALIVE: return "KA"
    if len(msg) >= 9 and msg[0] == OCA_SYNC:
        return f"oca(t{msg[7]},{len(msg)}b)"
    return f"raw({len(msg)}b)"


async def recv_msgs(ws, timeout=4.0, max_msgs=12,
                    stop_on=("approved","denied","error")):
    msgs = []
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0: break
            msg = await asyncio.wait_for(ws.recv(), timeout=rem)
            if isinstance(msg, str): msg = msg.encode()
            msgs.append(msg)
            s = status_of(msg)
            if s != "KA": print(f"  [RX] {s}")
            if s in stop_on: break
            if len(msgs) >= max_msgs: break
    except asyncio.TimeoutError: pass
    except websockets.exceptions.ConnectionClosed:
        msgs.append(b"__CLOSED__")
    return msgs


async def connect_ws(url: str, headers: dict):
    ws = await websockets.connect(url, additional_headers=headers,
                                  ping_interval=None, open_timeout=10,
                                  compression=None)
    await ws.send(KEEPALIVE)
    return ws


async def connect_and_init(url: str, headers: dict):
    """Připojí + pošle KA+DISCOVERY+GET_ROLE+SUBSCRIBE_QUERY najednou (jako iPad/.pyok)."""
    ws = await websockets.connect(url, additional_headers=headers,
                                  ping_interval=None, open_timeout=10,
                                  compression=None)
    for p in (KEEPALIVE, DISCOVERY, GET_ROLE, SUBSCRIBE_QUERY):
        await ws.send(p)
    return ws


# ── FC2Client ────────────────────────────────────────────────────
class FC2Client:
    """
    Perzistentní FC2 klient.
      self.ws   — 58323 (autentizační, AES70 plain)
      self.ch2  — 58322 (řídicí, Noise_NK šifrovaný)
    """
    GAIN_ONO    = [0x1001, 0x1002, 0x1003, 0x1004]
    PHANTOM_ONO = [0x1010, 0x1011]
    AIR_ONO     = [0x1020, 0x1021]
    MUTE_ONO    = [0x1030, 0x1031]

    def __init__(self, ip: str = FC2_IP, device_name: str = DEVICE_NAME,
                 fc2_pubkey: bytes | None = None):
        self.ip         = ip
        self.device_name= device_name
        self.fc2_pubkey = fc2_pubkey or FC2_STATIC_PUBKEY
        self.url        = f"ws://{self.ip}:{FC2_PORT}/"
        self.ws         = None
        self.ch2: EncryptedChannel | None = None
        self.epub_pub   = self.epub_priv = self.token = self.qr_bytes = None
        self._ka_task   = None

    def _ctrl_url(self) -> str:
        # URL = vždy DUMMY_EPUB (ověřeno z pcap ipad-full-working.pcapng)
        return f"ws://{self.ip}:{FC2_PORT2}/{DUMMY_EPUB.hex()}"

    # ── Párování ──────────────────────────────────────────────────
    async def pair(self):
        """
        Vygeneruje nový X25519 keypair, zobrazí QR v FC2,
        čeká na schválení, uloží auth data.
        """
        loop = asyncio.get_event_loop()

        # Nový keypair + token
        epub_priv, epub_pub = generate_keypair()
        token = secrets.token_bytes(44)
        self.epub_priv = epub_priv
        self.epub_pub  = epub_pub
        self.token     = token

        print("[pair] Vygenerován nový X25519 keypair")
        print(f"  epub_pub:  {epub_pub.hex()}")

        print("\n[pair] Zobrazuji QR v FC2...")
        # Přesně jako .pyok: SendQR posílá DUMMY hodnoty (stejné jako iPad pcap).
        # FC2 zobrazí QR s těmito hodnotami — uživatel je naskenuje a vrátí nám je.
        # Reálný epub_pub/token pro šifrování se použije až v save_auth().
        ws1 = await connect_and_init(self.url, HEADERS)
        await ws1.send(build_send_qr(DUMMY_EPUB, DUMMY_TOKEN))
        msgs = await recv_msgs(ws1, timeout=6.0)
        statuses = [status_of(m) for m in msgs]
        if "pending" not in statuses:
            print(f"[pair] WARNING: 'pending' nepřišlo. Stavy: {statuses}")
            print("[pair] QR nemusí být zobrazen — zkontroluj FC2")
        else:
            print("[pair] QR zobrazen ✓")
        try: await ws1.close()
        except Exception: pass

        print("\nNaskenuj QR kód v FC2 mobilní aplikací a zadej obsah QR:")
        raw = await loop.run_in_executor(
            None, lambda: input("  QR hex (128 znaků): ").strip().lower().replace(" ","")
        )
        print()
        if len(raw) != 128 or not all(c in "0123456789abcdef" for c in raw):
            raise ValueError(f"Neplatný QR vstup (délka={len(raw)}), očekáváno 128 hex znaků")
        qr_bytes = bytes.fromhex(raw)

        print("[pair] Odesílám RequestApproval...")
        ws2 = await connect_and_init(self.url, HEADERS)
        await recv_msgs(ws2, timeout=2.0, max_msgs=4, stop_on=())
        await ws2.send(build_request_approval(qr_bytes))
        r_msgs = await recv_msgs(ws2, timeout=10.0, stop_on=("approved","denied"))
        if "approved" not in [status_of(m) for m in r_msgs]:
            try: await ws2.close()
            except Exception: pass
            raise RuntimeError(f"Schválení selhalo: {[status_of(m) for m in r_msgs]}")

        print("[pair] ✓ SCHVÁLENO!")
        self.qr_bytes  = qr_bytes
        # fc2_epub (Noise static pro toto párování) = první 32b QR
        if not self.fc2_pubkey:
            self.fc2_pubkey = qr_bytes[:32]
        save_auth(epub_pub, epub_priv, token, self.ip, self.fc2_pubkey, qr_raw=qr_bytes)
        self.ws = ws2
        self._start_keepalive()
        await self._connect_ctrl()

    # ── Reconnect ─────────────────────────────────────────────────
    async def reconnect(self, auth: dict = None):
        if auth is None: auth = load_auth()
        if auth is None:
            print(f"[!] {AUTH_FILE} nenalezen — párování...")
            await self.pair(); return

        self.epub_pub  = auth["epub_pub"]
        self.epub_priv = auth["epub_priv"]
        self.token     = auth["token"]
        self.qr_bytes  = auth["qr_bytes"]
        if auth.get("fc2_pubkey"): self.fc2_pubkey = auth["fc2_pubkey"]

        await self.close()

        # ── 58323 auth ────────────────────────────────────────────
        # Z pcap: po connect_and_init FC2 vždy pošle "ready".
        # "ready" = FC2 čeká na RequestApproval (nebo SendQR).
        # Musíme vždy poslat RequestApproval — approved přijde jako odpověď.
        # Teprve po "approved" zůstaneme připojeni a data tečou.
        print("[connect] 58323 připojuji...")
        ws = await connect_and_init(self.url, HEADERS)
        init_msgs = await recv_msgs(ws, timeout=3.0, max_msgs=8, stop_on=("ready","scanning","approved"))
        statuses = [status_of(m) for m in init_msgs]
        print(f"  [auth] stavy: {statuses}")

        if "approved" not in statuses:
            if "scanning" in statuses:
                # FC2 má aktivní QR session → pošli RequestApproval
                await ws.send(build_request_approval(self.qr_bytes))
                ra = await recv_msgs(ws, timeout=8.0, max_msgs=10, stop_on=("approved","denied"))
                statuses2 = [status_of(m) for m in ra]
                print(f"  [auth] po RA: {statuses2}")
                if "approved" not in statuses2:
                    try: await ws.close()
                    except Exception: pass
                    raise RuntimeError(f"Autentizace selhala: {statuses + statuses2}")
            elif "ready" in statuses:
                # FC2 nás zná = jsme autentizovaní, žádný RA nepotřeba
                pass
            else:
                try: await ws.close()
                except Exception: pass
                raise RuntimeError(f"Neočekávaný stav: {statuses}")
        # Zachováme auth ws — obsahuje DISCOVERY+SUBSCRIBE subscriptions.
        # FC2 posílá notifikace (gain, signal level atd.) jen na ws se subscriptions.
        # Čisté ws (jen KA) = FC2 ukáže "disconnected" a nepošle žádná data.
        # iPad dělá totéž: nikdy nezavírá auth ws, používá ho pro celý provoz.
        self.ws = ws
        self.ch2 = None
        print("[connect] 58323 ✓")
        self._start_keepalive()
        await self._connect_ctrl()

    async def _connect_ctrl(self):
        """Připojí 58322 a provede Noise_NK handshake."""
        ctrl_url = self._ctrl_url()
        print(f"[connect] 58322 připojuji... ({ctrl_url})")
        try:
            ws2 = await websockets.connect(ctrl_url, additional_headers=HEADERS_CTRL,
                                           ping_interval=None, open_timeout=10,
                                           compression=None)
        except Exception as e:
            print(f"[connect] 58322 WS selhalo: {e}")
            self.ch2 = None; return

        # POZOR: z pcap ipad-full-working.pcapng vyplývá:
        # - iPad NEPOSÍLÁ KA před handshake (to byl předchozí bug)
        # - msg1 = 75b → 32b(e_pub) + enc(epub_pub)(27b??) — viz analýzu
        # - epub_pub jako payload (32b) = 80b, zkusíme a uvidíme
        # - msg2 od FC2 = 51b (2b prefix + 49b noise data)
        # - msg3 od iPadu = 51b (první transport nebo třetí handshake zpráva)

        if self.fc2_pubkey and self.epub_priv:
            # ── Noise_NK handshake ────────────────────────────────
            print("[connect] 58322 Noise_NK handshake...")
            # epub_pub = veřejný klíč odvozený z epub_priv (naše identita pro FC2)
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            our_epub_pub = X25519PrivateKey.from_private_bytes(self.epub_priv) \
                               .public_key().public_bytes_raw()
            noise = NoiseNK(self.fc2_pubkey, self.epub_priv)
            # Payload v msg1 = epub_pub[:27] — z pcap: msg1=75b=32(e)+enc(27b)+16(tag)
            # FC2 potřebuje identifikaci klienta (URL je vždy DUMMY_EPUB, ne náš klíč)
            msg1  = noise.build_msg1(payload=our_epub_pub[:27])
            print(f"  → msg1 ({len(msg1)}b): {msg1[:12].hex()}...")
            await ws2.send(msg1)

            try:
                raw = await asyncio.wait_for(ws2.recv(), timeout=5.0)
                if isinstance(raw, str): raw = raw.encode()
                print(f"  ← msg2 ({len(raw)}b): {raw[:12].hex()}...")

                # Odstraň 2b prefix pokud přítomen
                try:
                    body = NoiseNK.unframe(raw)
                except ValueError:
                    body = raw                  # fallback bez prefixu

                payload2 = noise.process_msg2(body)
                print(f"  Handshake hotov ✓  (payload={payload2.hex() or 'prázdný'})")
                self.ch2 = EncryptedChannel(ws2, noise)
                print("[connect] 58322 ✓ (šifrovaný)")
            except asyncio.TimeoutError:
                print("  [58322] Timeout čekání na msg2")
                self.ch2 = EncryptedChannel(ws2, None)
            except Exception as e:
                print(f"  [58322] Handshake selhal: {e}")
                self.ch2 = EncryptedChannel(ws2, None)
        else:
            # ── Debug mode — logujeme co FC2 pošle ───────────────
            print("[connect] 58322 bez fc2_pubkey — debug probe")
            self.ch2 = EncryptedChannel(ws2, None)
            try:
                raw = await asyncio.wait_for(ws2.recv(), timeout=3.0)
                if isinstance(raw, str): raw = raw.encode()
                print(f"\n  ┌─ 58322 FC2 poslalo ({len(raw)}b) ─────────────────")
                for off in range(0, len(raw), 16):
                    chunk = raw[off:off+16]
                    print(f"  │ {off:3d}: {chunk.hex(' ')}")
                print(f"  └─────────────────────────────────────────────────")
                print(f"\n  Zkopíruj hex výše + nastav --fc2-pubkey <64hex>")
            except asyncio.TimeoutError:
                print("  FC2 čeká na náš Noise msg1 — nastav --fc2-pubkey <64hex>")
            except Exception:
                pass
            print("[connect] 58322 připojeno (nešifrované)")

    # ── Keepalive ─────────────────────────────────────────────────
    def _start_keepalive(self):
        if self._ka_task: self._ka_task.cancel()
        self._ka_task = asyncio.ensure_future(self._keepalive_loop())

    async def _keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(2)
                if self.ws:
                    try: await self.ws.send(KEEPALIVE)
                    except Exception: pass
                if self.ch2:
                    try: await self.ch2.send(KEEPALIVE)
                    except Exception: pass
        except asyncio.CancelledError: pass

    async def close(self):
        if self._ka_task: self._ka_task.cancel()
        if self.ws:
            try: await self.ws.close()
            except Exception: pass
            self.ws = None
        if self.ch2:
            try: await self.ch2.close()
            except Exception: pass
            self.ch2 = None

    # ── AES70 send/recv ───────────────────────────────────────────
    async def send_cmd(self, ono: int, dl: int, mi: int,
                       params: bytes = b"", timeout: float = 3.0) -> list[bytes]:
        """
        Pošle příkaz. Pokud je 58322 šifrovaný, použije ho;
        jinak fallback na 58323.
        """
        cmd = build_cmd(ono, dl, mi, params)
        ch  = self.ch2 if (self.ch2 and self.ch2.is_encrypted) else None

        for attempt in range(2):
            try:
                if ch: await ch.send(cmd)
                else:  await self.ws.send(cmd)
                break
            except Exception:
                if attempt: raise
                print("[!] Reconnect...")
                await self.reconnect()
                await asyncio.sleep(0.3)
                ch = self.ch2 if (self.ch2 and self.ch2.is_encrypted) else None

        responses = []
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                rem = deadline - asyncio.get_event_loop().time()
                if rem <= 0: break
                if ch:   msg = await asyncio.wait_for(ch.recv(), timeout=rem)
                else:    msg = await asyncio.wait_for(self.ws.recv(), timeout=rem)
                if isinstance(msg, str): msg = msg.encode()
                if msg == KEEPALIVE: continue
                responses.append(msg); break
        except (asyncio.TimeoutError, Exception): pass
        return responses

    # ── Scarlett ovládání ─────────────────────────────────────────
    async def get_input_gain(self, ch: int = 0) -> float | None:
        if ch >= len(self.GAIN_ONO): return None
        r = await self.send_cmd(self.GAIN_ONO[ch], 4, 1)
        if not r: return None
        try: return struct.unpack_from(">f", r[0][20:])[0]
        except: return None

    async def set_input_gain(self, ch: int, db: float):
        if ch >= len(self.GAIN_ONO): raise ValueError(f"CH{ch} neexistuje")
        await self.send_cmd(self.GAIN_ONO[ch], 4, 2, oca_float32(db))

    async def get_phantom_power(self, ch: int = 0) -> bool | None:
        if ch >= len(self.PHANTOM_ONO): return None
        r = await self.send_cmd(self.PHANTOM_ONO[ch], 4, 1)
        if not r: return None
        try: return r[0][-1] != 0
        except: return None

    async def set_phantom_power(self, ch: int, enabled: bool):
        if ch >= len(self.PHANTOM_ONO): raise ValueError(f"CH{ch} nemá phantom")
        await self.send_cmd(self.PHANTOM_ONO[ch], 4, 2, oca_bool(enabled))

    async def get_air_mode(self, ch: int = 0) -> bool | None:
        if ch >= len(self.AIR_ONO): return None
        r = await self.send_cmd(self.AIR_ONO[ch], 4, 1)
        if not r: return None
        try: return r[0][-1] != 0
        except: return None

    async def set_air_mode(self, ch: int, enabled: bool):
        if ch >= len(self.AIR_ONO): raise ValueError(f"CH{ch} nemá Air")
        await self.send_cmd(self.AIR_ONO[ch], 4, 2, oca_bool(enabled))

    async def get_input_mute(self, ch: int = 0) -> bool | None:
        if ch >= len(self.MUTE_ONO): return None
        r = await self.send_cmd(self.MUTE_ONO[ch], 4, 1)
        if not r: return None
        try: return r[0][-1] != 0
        except: return None

    async def set_input_mute(self, ch: int, muted: bool):
        if ch >= len(self.MUTE_ONO): raise ValueError(f"CH{ch} nemá mute")
        await self.send_cmd(self.MUTE_ONO[ch], 4, 2, oca_bool(muted))

    async def scan_onos(self, start: int = 0x1001, end: int = 0x1050):
        print(f"\n[scan] ONo 0x{start:04x}–0x{end-1:04x}...")
        found = []
        for ono in range(start, end):
            r = await self.send_cmd(ono, 1, 1, timeout=0.5)
            if r and len(r[0]) > 18 and r[0][18] == 0:
                print(f"  ONo=0x{ono:04x}: {r[0].hex()[:60]}")
                found.append(ono)
        print(f"[scan] Nalezeno {len(found)}")
        return found

    # ── Monitor ───────────────────────────────────────────────────
    async def monitor(self, max_reconnects: int = 5):
        print("[monitor] Naslouchám oběma kanálům... (Ctrl+C)")
        print()
        reconnects = 0

        def decode(msg: bytes) -> str:
            if not msg or msg[0] != OCA_SYNC or len(msg) < 10:
                return f"RAW({len(msg)}b): {msg[:20].hex()}"
            mt = msg[7]; mc = struct.unpack_from(">H", msg, 8)[0]
            if mt == 4 and len(msg) == 12: return "KA"
            lines = []; off = 10
            if mt == 3:
                for _ in range(mc):
                    if off + 12 > len(msg): break
                    ns   = struct.unpack_from(">I", msg, off)[0]
                    ono  = struct.unpack_from(">I", msg, off+4)[0]
                    edl  = struct.unpack_from(">H", msg, off+8)[0]
                    eidx = struct.unpack_from(">H", msg, off+10)[0]
                    ctx  = msg[off+12:off+ns]
                    ci   = ctx.hex()
                    if len(ctx) == 1: ci = f"{ctx[0]} (bool/u8)"
                    elif len(ctx) == 4:
                        try: ci = f"float={struct.unpack_from('>f',ctx)[0]:.3f}  hex={ctx.hex()}"
                        except: pass
                    else:
                        try: ci = f"str='{ctx.decode()}'"
                        except: pass
                    lines.append(f"NOTIF  ONo=0x{ono:04x}  DL={edl}  Idx={eidx}  {ci}")
                    off += ns
            elif mt == MSG_RESPONSE:
                for _ in range(mc):
                    if off + 9 > len(msg): break
                    rs  = struct.unpack_from(">I", msg, off)[0]
                    hdl = struct.unpack_from(">I", msg, off+4)[0]
                    st  = msg[off+8]
                    par = msg[off+10:off+rs] if rs > 6 else b""
                    sn  = {0:"OK",1:"ProtoErr",2:"DevErr",3:"Locked",
                           4:"BadFmt",5:"BadONo",6:"NotImpl",7:"Invalid"}.get(st,f"?{st}")
                    p   = ""
                    if len(par)==4:
                        try: p=f"  → float={struct.unpack_from('>f',par)[0]:.3f}"
                        except: p=f"  → {par.hex()}"
                    elif len(par)==1: p=f"  → {par[0]}"
                    elif par: p=f"  → {par.hex()}"
                    lines.append(f"RESP   hdl={hdl}  {sn}{p}")
                    off += rs
            elif mt in (1,2):
                for _ in range(mc):
                    if off+17>len(msg): break
                    cs  = struct.unpack_from(">I",msg,off)[0]
                    ono = struct.unpack_from(">I",msg,off+8)[0]
                    dl  = struct.unpack_from(">H",msg,off+12)[0]
                    mi  = struct.unpack_from(">H",msg,off+14)[0]
                    par = msg[off+17:off+cs]
                    lines.append(f"CMD    ONo=0x{ono:04x}  DL={dl}  MI={mi}  {par.hex()}")
                    off += cs
            else:
                lines.append(f"t{mt}/n{mc}: {msg.hex()[:60]}")
            return "\n         ".join(lines) if lines else f"t{mt}/n{mc}"

        async def plain_recv(ws, lbl):
            m = await ws.recv()
            if isinstance(m, str): m = m.encode()
            return lbl, m

        while True:
            try:
                if not self.ws:
                    await asyncio.sleep(1); continue

                task = asyncio.ensure_future(plain_recv(self.ws, "58323"))
                done, _ = await asyncio.wait({task}, timeout=5.0)
                if not done:
                    task.cancel()
                    continue   # timeout — keepalive_loop posílá KA

                lbl, msg = task.result()
                d = decode(msg)
                if d == "KA": continue
                print(f"  [{time.strftime('%H:%M:%S')}] {d}")
                reconnects = 0

            except (websockets.exceptions.ConnectionClosed, Exception) as e:
                if isinstance(e, KeyboardInterrupt): break
                reconnects += 1
                if reconnects > max_reconnects:
                    print(f"\n[monitor] Přerušeno {reconnects}× — konec."); break
                print(f"[monitor] Reconnect {reconnects}/{max_reconnects}...")
                try:
                    await self.reconnect(); reconnects = 0
                except Exception as e2:
                    print(f"[monitor] Selhal: {e2}"); break
            except KeyboardInterrupt:
                break


# ── CLI ──────────────────────────────────────────────────────────
async def get_client(auto_pair: bool = True) -> FC2Client:
    auth  = load_auth()
    fc2pk = (auth.get("fc2_pubkey") if auth else None) or FC2_STATIC_PUBKEY
    c = FC2Client(fc2_pubkey=fc2pk)
    if not auth and auto_pair:
        print(f"[!] {AUTH_FILE} nenalezen — párování..."); await c.pair()
    else:
        await c.reconnect(auth)
    return c


async def cmd_pair():
    auth  = load_auth()
    fc2pk = (auth.get("fc2_pubkey") if auth else None) or FC2_STATIC_PUBKEY
    c = FC2Client(fc2_pubkey=fc2pk)
    await c.pair()
    print("\nPárování dokončeno.")
    await c.close()


async def cmd_status():
    c = await get_client()
    print(f"\n[status] Připojeno {c.ip}:{FC2_PORT}")
    if AUTH_FILE.exists():
        d = json.loads(AUTH_FILE.read_text())
        print(f"  Verze:      {d.get('version',1)}")
        print(f"  Spárováno:  {d.get('paired_at','?')}")
        if d.get("epub_hex"):
            print(f"  epub_pub:   {d['epub_hex'][:32]}...")
        if d.get("fc2_pubkey_hex"):
            print(f"  fc2_pubkey: {d['fc2_pubkey_hex'][:32]}...")
        else:
            print(f"  fc2_pubkey: NEZNÁMÝ (58322 bez šifrování)")
    enc = c.ch2 and c.ch2.is_encrypted
    print(f"  58322:      {'šifrovaný ✓' if enc else 'připojeno (nešifrované)'}")
    await c.close()


async def cmd_discover():
    c = await get_client()
    found = await c.scan_onos(0x1001, 0x1050)
    if not found:
        print("Nic 0x1001-0x104F, zkouším 0x0100-0x0200...")
        await c.scan_onos(0x0100, 0x0200)
    await c.close()


async def cmd_monitor():
    c = await get_client()
    await c.monitor()
    await c.close()


async def cmd_interactive():
    c = await get_client()
    enc = c.ch2 and c.ch2.is_encrypted
    print(f"\n[interactive] Připojeno  58322={'šifrovaný ✓' if enc else 'nešifrovaný'}")
    print("  gain <ch> [dB]       phantom <ch> [0/1]   air <ch> [0/1]")
    print("  mute <ch> [0/1]      monitor              discover")
    print("  raw <ono_hex> <dl> <mi> [params_hex]      quit")
    print()
    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("> ").strip())
        except (EOFError, KeyboardInterrupt):
            break
        parts = line.split()
        if not parts: continue
        cmd = parts[0].lower()
        try:
            if cmd == "quit":
                break
            elif cmd == "gain":
                ch = int(parts[1]) if len(parts)>1 else 0
                if len(parts)>2:
                    await c.set_input_gain(ch, float(parts[2]))
                    print(f"  → Gain CH{ch} = {parts[2]} dB")
                else:
                    print(f"  Gain CH{ch} = {await c.get_input_gain(ch)} dB")
            elif cmd == "phantom":
                ch = int(parts[1]) if len(parts)>1 else 0
                if len(parts)>2:
                    v = parts[2] in ("1","on","true")
                    await c.set_phantom_power(ch, v)
                    print(f"  → Phantom CH{ch} = {'ON' if v else 'OFF'}")
                else:
                    print(f"  Phantom CH{ch} = {await c.get_phantom_power(ch)}")
            elif cmd == "air":
                ch = int(parts[1]) if len(parts)>1 else 0
                if len(parts)>2:
                    v = parts[2] in ("1","on","true")
                    await c.set_air_mode(ch, v)
                    print(f"  → Air CH{ch} = {'ON' if v else 'OFF'}")
                else:
                    print(f"  Air CH{ch} = {await c.get_air_mode(ch)}")
            elif cmd == "mute":
                ch = int(parts[1]) if len(parts)>1 else 0
                if len(parts)>2:
                    v = parts[2] in ("1","on","true")
                    await c.set_input_mute(ch, v)
                    print(f"  → Mute CH{ch} = {'ON' if v else 'OFF'}")
                else:
                    print(f"  Mute CH{ch} = {await c.get_input_mute(ch)}")
            elif cmd == "monitor":
                await c.monitor()
            elif cmd == "raw":
                ono = int(parts[1],16) if "x" in parts[1] else int(parts[1])
                r = await c.send_cmd(ono, int(parts[2]), int(parts[3]),
                                     bytes.fromhex(parts[4]) if len(parts)>4 else b"")
                for resp in r: print(f"  RESP: {resp.hex()}")
            elif cmd == "discover":
                await c.scan_onos()
            else:
                print(f"Neznámý příkaz: {cmd}")
        except Exception as e:
            print(f"Chyba: {e}")

    await c.close()


# ── main ──────────────────────────────────────────────────────────
async def main():
    global DEVICE_NAME, HEADERS, HEADERS_CTRL, AUTH_FILE, FC2_STATIC_PUBKEY

    CMDS = {
        "pair": cmd_pair, "status": cmd_status, "discover": cmd_discover,
        "monitor": cmd_monitor, "interactive": cmd_interactive, "i": cmd_interactive,
    }

    args = sys.argv[1:]
    device_name = DEVICE_NAME
    fc2pk_hex   = None
    i, filtered = 0, []
    while i < len(args):
        if args[i] == "--name" and i+1 < len(args):
            device_name = args[i+1]; i += 2
        elif args[i] == "--auth" and i+1 < len(args):
            AUTH_FILE = Path(args[i+1]); i += 2
        elif args[i] == "--fc2-pubkey" and i+1 < len(args):
            fc2pk_hex = args[i+1]; i += 2
        else:
            filtered.append(args[i]); i += 1
    args = filtered
    cmd = args[0] if args else "interactive"

    if cmd in ("-h", "--help", "help"):
        print(__doc__); return
    if cmd not in CMDS:
        print(f"Neznámý příkaz: {cmd}"); return

    DEVICE_NAME  = device_name
    if fc2pk_hex:
        FC2_STATIC_PUBKEY = bytes.fromhex(fc2pk_hex)
        print(f"[*] fc2_pubkey: {fc2pk_hex[:32]}...")
    if device_name != "iPad":
        print(f"[*] Jméno: {device_name}")
    if AUTH_FILE.name != "fc2_auth.json":
        print(f"[*] Auth: {AUTH_FILE}")

    await CMDS[cmd]()


if __name__ == "__main__":
    asyncio.run(main())
