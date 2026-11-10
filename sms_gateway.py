#!/usr/bin/env python3
"""Bramka powiadomień (SMS przez ZTE MC888 + opcjonalnie WhatsApp Cloud API) dla host. (B1, 6.11.2026)

Jedyne miejsce, które wysyła wiadomości na zewnątrz. Zasady bezpieczeństwa:
  * domyślnie tryb próbny ("live": false): nic nie jest wysyłane, tylko zapis do dryrun.jsonl;
  * odbiorcy wyłącznie z listy w /etc/sms-gateway.json (aliasy), numery nie wychodzą poza ten plik;
  * wywołania tylko z dozwolonych adresów (allow_clients) i z tokenem Bearer;
  * limit dzienny na odbiorcę i kanał, deduplikacja (klucz zdarzenia + odbiorca + kanał, 7 dni), ciche godziny
    (priorytet "high" je omija), długość tekstu ograniczona;
  * w logach nie ma numerów ani treści wiadomości (tylko alias, kanał, długość, wynik).
API:  POST /v1/notify  {"key": "...", "text": "...", "priority": "normal|high", "to": ["alias", ...]}
      GET  /v1/status
      GET  /v1/result/<key>   wynik wysyłki dla klucza (tylko własny klient)
Odpowiedź dla odbiorcy: sent | sent_unconfirmed | unknown | dry_run | duplicate | deferred (cisza) | limit | failed | queued
                        | no_channel | forbidden_recipient.

Klienci (etap 1, 7.11.2026): obok dawnego wspólnego tokenu (klient "legacy" = LegacyClient, zachowanie bez zmian) bramka
obsługuje klientów z sekcji "clients" w konfiguracji: własny token (w pliku tylko skrót SHA-256), własne adresy,
lista dozwolonych aliasów odbiorców, dzienny limit klienta, priorytet "high" tylko za zgodą ("allow_high") oraz
ponowienia nieudanych wysyłek ("retry": true). Dla klienta "legacy" nic się nie zmienia: nieudana wysyłka to "failed"
bez ponowień po stronie bramki (LegacyClient ponawia sam), a "sent_unconfirmed" jest zgłaszane jako "sent".
Kanał zwrotny (etap 2): odbiór SMS-ów z modemu i komendy z potwierdzeniem, opis w sms_inbound.py i BRAMKA-ZWROTNA.md;
domyślnie wyłączony ("inbound.enabled": false). Sekret TOTP: python3 sms_gateway.py --totp-init (uruchamia człowiek).
Nowy token:  python3 sms_gateway.py --new-token   (wypisuje token i skrót; token trafia do klienta, skrót do konfiguracji).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

# Import the local modem adapter.

CONFIG = os.environ.get("SMSGW_CONFIG", "/etc/sms-gateway.json")
TOKEN_FILE = os.environ.get("SMSGW_TOKEN_FILE", "/etc/sms-gateway.token")
STATE_DIR = os.environ.get("SMSGW_STATE_DIR", "/var/lib/sms-gateway")
TZ = ZoneInfo("Europe/Warsaw")
LOCK = threading.Lock()  # jedna sesja admina ZTE: wysyłamy po kolei
RETRY_TICK_S = 15
LEGACY = "legacy"
ALIAS = re.compile(r"[a-z0-9_-]{1,30}")
KEY = re.compile(r"[A-Za-z0-9:_.|-]{1,120}")
NUMBER = re.compile(r"\+?[0-9]{9,15}")


def load_config() -> dict:
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    cfg.setdefault("live", False)
    cfg.setdefault("daily_limit", 8)
    cfg.setdefault("quiet_hours", ["21:30", "06:30"])
    cfg.setdefault("max_chars", 200)
    cfg.setdefault("allow_clients", [])
    cfg.setdefault("recipients", {})
    cfg.setdefault("channels", {"sms": True, "whatsapp": False})
    cfg.setdefault("whatsapp", {})
    cfg.setdefault("clients", {})
    cfg.setdefault("inbound", {})  # kanał zwrotny, domyślnie wyłączony (etap 2)
    cfg.setdefault("client_daily_limit", 30)
    cfg.setdefault("retry", {"attempts": 4, "backoff_s": [60, 300, 1200], "max_age_s": 21600})
    return cfg


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def load_state() -> dict:
    try:
        return json.load(open(state_path("state.json"), encoding="utf-8"))
    except FileNotFoundError:
        return {"sent": {}, "counts": {}, "queue": [], "results": {}}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = state_path("state.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path("state.json"))


def in_quiet_hours(cfg: dict, now: datetime) -> bool:
    try:
        start, end = (datetime.strptime(x, "%H:%M").time() for x in cfg["quiet_hours"])
    except (ValueError, TypeError):
        return False
    t = now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- kanały -----------------------------------------------------------------------------------------
def _in_sent_box(z, number: str, text: str) -> bool:
    """Czy wiadomość widać w skrzynce nadawczej modemu (tags=2). NIEWERYFIKOWANE na urządzeniu; błąd = False."""
    try:
        r = z.get("sms_data_total", extra="&page=0&data_per_page=5&mem_store=1&tags=2&order_by=order+by+id+desc")
        tail = number.lstrip("+")[-9:]
        for m in r.get("messages", []):
            body = bytes.fromhex(m.get("content", "")).decode("utf-16-be", "replace")
            if body == text and str(m.get("number", "")).lstrip("+").endswith(tail):
                return True
    except Exception:
        pass
    return False


def send_sms_zte(number: str, text: str) -> str:
    """SMS przez ZTE MC888 (klient /usr/local/bin/zte_api.py). Zwraca "sent" (modem potwierdził stan 3),
    "sent_unconfirmed" (modem przyjął polecenie, brak potwierdzenia stanu; nie ponawiamy, żeby nie dublować SMS-a)
    albo "failed". Raportu doręczenia do telefonu modem nie daje."""
    from zte_api import Zte  # import leniwy: tryb próbny nie dotyka modemu
    z = Zte()
    if not z.login():
        raise RuntimeError("logowanie do modemu nieudane")
    now = datetime.now(TZ)
    offset = int(now.utcoffset().total_seconds() // 3600)
    try:
        r = z.set(goformId="SEND_SMS", notCallback="true", Number=number,
                  sms_time=now.strftime("%y;%m;%d;%H;%M;%S") + f";{offset:+d}",
                  MessageBody=text.encode("utf-16-be").hex(), ID="-1", encode_type="UNICODE")
    except Exception:
        return "unknown"
    if str(r.get("result")).lower() not in ("success", "0"):
        return "failed"
    for _ in range(15):  # stan wysyłki: 1 w toku, 2 błąd, 3 sukces
        time.sleep(1)
        try:
            st = str(z.get("sms_cmd_status_info", extra="&sms_cmd=4").get("sms_cmd_status_result", ""))
        except Exception:
            continue
        if st == "3":
            return "sent"
        if st == "2":
            return "failed"
    return "sent" if _in_sent_box(z, number, text) else "sent_unconfirmed"


def send_whatsapp(number: str, text: str, cfg: dict) -> bool:
    """WhatsApp Cloud API (oficjalne). Wymaga phone_number_id i tokenu wpisanych przez użytkownika."""
    wa = cfg.get("whatsapp", {})
    token = open(wa["token_file"], encoding="utf-8").read().strip()
    if wa.get("template"):
        payload = {"messaging_product": "whatsapp", "to": number.lstrip("+"), "type": "template",
                   "template": {"name": wa["template"], "language": {"code": wa.get("lang", "pl")},
                                "components": [{"type": "body", "parameters": [{"type": "text", "text": text}]}]}}
    else:  # wiadomość swobodna działa tylko w 24-godzinnym oknie rozmowy
        payload = {"messaging_product": "whatsapp", "to": number.lstrip("+"), "type": "text", "text": {"body": text}}
    req = urllib.request.Request(
        f"https://graph.facebook.com/v21.0/{wa['phone_number_id']}/messages",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


# --- dziennik komunikacji (dowód i ślad audytowy) ---------------------------------------------------
def ledger(now, key: str, alias: str, channel: str, priority: str, result: str, text: str, kind: str) -> None:
    """Dopisuje do communication.jsonl PEŁNĄ treść każdej próby wysyłki i jej wynik (także deferred/limit/duplicate/failed).
    Bez numerów telefonów (tylko alias). Plik 600, tylko dopisywanie; zawiera dane szkolne dzieci, więc nie trafia do Gita ani vaultu."""
    units = len(text.encode("utf-16-le")) // 2  # bramka zawsze wysyła w UCS-2: 70 jednostek w jednym segmencie, 67 w wieloczęściowym
    segments = 1 if units <= 70 else -(-units // 67)
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(state_path("communication.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "key": key, "kind": kind, "to": alias, "channel": channel,
                                 "priority": priority, "result": result, "chars": len(text), "units": units, "segments": segments,
                                 "text": text}, ensure_ascii=False) + chr(10))
    except OSError as exc:
        log(f"dziennik komunikacji niedostepny: {type(exc).__name__}")


# --- klienci ------------------------------------------------------------------------------------------
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_profile(name: str, spec: dict) -> dict:
    allowed_list = spec.get("recipients", [])
    return {"name": name, "recipients": allowed_list if isinstance(allowed_list, list) else [],
            "allow_high": bool(spec.get("allow_high", False)), "retry": bool(spec.get("retry", True)),
            "daily_limit": int(spec.get("daily_limit", 0)) or None, "legacy": False, "otp_only": bool(spec.get("otp_only", False)), "otp_text_pattern": spec.get("otp_text_pattern", r"Code: [0-9]{4}"), "otp_key_pattern": spec.get("otp_key_pattern", r"otp-[a-f0-9]{32}")}


LEGACY_PROFILE = {"name": LEGACY, "recipients": ["*"], "allow_high": True, "retry": False, "daily_limit": None,
                  "legacy": True}


def identify(ip: str, auth: str, cfg: dict):
    """Zwraca (profil, kod_błędu). kod: 0 ok, 401 zły token, 403 zły adres."""
    presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if presented:
        digest = token_hash(presented)
        for name, spec in cfg["clients"].items():
            if isinstance(spec, dict) and hmac.compare_digest(digest, str(spec.get("token_sha256", "")).lower()):
                ips = spec.get("allow_ips", [])
                return (client_profile(name, spec), 0) if ip in ips else (None, 403)
    # dawny, wspólny token (LegacyClient): adres sprawdzany przed tokenem, jak dotąd
    if ip not in cfg["allow_clients"]:
        return None, 403
    try:
        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    except OSError:
        return None, 401
    if not hmac.compare_digest(("Bearer " + token).encode(), auth.encode()):
        return None, 401
    return LEGACY_PROFILE, 0


def allowed(profile: dict, alias: str) -> bool:
    return "*" in profile["recipients"] or alias in profile["recipients"]


def report(profile: dict, result: str) -> str:
    """Dawny klient nie zna nowych stanów: "sent_unconfirmed" zgłaszamy jako "sent" (jak dotąd)."""
    return "sent" if profile["legacy"] and result == "sent_unconfirmed" else result


# --- dostarczanie i ponowienia ------------------------------------------------------------------------
def deliver(channel: str, number: str, text: str, cfg: dict) -> str:
    try:
        if channel == "whatsapp":
            return "sent" if send_whatsapp(number, text, cfg) else "failed"
        return send_sms_zte(number, text)
    except Exception as exc:
        log(f"blad kanalu {channel}: {type(exc).__name__}")
        return "failed"


def remember(state: dict, client: str, key: str, alias: str, channel: str, result: str, now) -> None:
    if result == "duplicate":  # powtórka nie może nadpisać pierwotnego wyniku (np. sent)
        return
    res = state.setdefault("results", {}).setdefault(f"{client}|{key}", {"ts": now.timestamp(), "to": {}})
    res["to"].setdefault(alias, {})[channel] = result


def process_retries() -> None:
    """Jedno przejście po kolejce ponowień (wołane przez wątek co RETRY_TICK_S)."""
    cfg = load_config()
    now = datetime.now(TZ)
    if not cfg["live"]:
        return
    with LOCK:
        state = load_state()
        keep = []
        for item in state.get("queue", []):
            if now.timestamp() - item["created"] > cfg["retry"]["max_age_s"]:
                remember(state, item["client"], item["key"], item["alias"], item["channel"], "failed_final", now)
                ledger(now, item["key"], item["alias"], item["channel"], item["priority"], "failed_final",
                       item["text"], item["kind"])
                continue
            if item["next_at"] > now.timestamp():
                keep.append(item)
                continue
            rec = cfg["recipients"].get(item["alias"], {})
            number = str(rec.get(item["channel"], "")).strip()
            result = deliver(item["channel"], number, item["text"], cfg) if NUMBER.fullmatch(number) else "failed"
            today = now.strftime("%Y-%m-%d")
            if result in ("sent", "sent_unconfirmed", "unknown"):
                state["sent"][item["dedupe"]] = now.timestamp()
                ck = f"{today}|{item['channel']}|{item['alias']}"
                state["counts"][ck] = state["counts"].get(ck, 0) + 1
                cc = f"{today}|client|{item['client']}"
                state["counts"][cc] = state["counts"].get(cc, 0) + 1
                remember(state, item["client"], item["key"], item["alias"], item["channel"], result, now)
                ledger(now, item["key"], item["alias"], item["channel"], item["priority"], result + "_retry",
                       item["text"], item["kind"])
                continue
            item["attempts"] += 1
            backoff = cfg["retry"]["backoff_s"]
            too_old = now.timestamp() - item["created"] > cfg["retry"]["max_age_s"]
            if item["attempts"] >= int(cfg["retry"]["attempts"]) or too_old:
                remember(state, item["client"], item["key"], item["alias"], item["channel"], "failed_final", now)
                ledger(now, item["key"], item["alias"], item["channel"], item["priority"], "failed_final",
                       item["text"], item["kind"])
                log(f"{item['channel']} -> {item['alias']}: failed_final")
                continue
            item["next_at"] = now.timestamp() + backoff[min(item["attempts"] - 1, len(backoff) - 1)]
            keep.append(item)
        state["queue"] = keep
        save_state(state)


def retry_loop() -> None:
    while True:
        time.sleep(RETRY_TICK_S)
        try:
            process_retries()
        except Exception as exc:  # wątek nie może paść
            log(f"retry_loop: {type(exc).__name__}")


# --- logika -----------------------------------------------------------------------------------------
def notify(req: dict, profile: dict = LEGACY_PROFILE) -> dict:
    cfg = load_config()
    key, text = str(req.get("key", "")), str(req.get("text", "")).strip()
    priority = "high" if req.get("priority") == "high" and profile["allow_high"] else "normal"
    kind = re.sub(r"[^a-z_]", "", str(req.get("kind", "")).lower())[:30] or "nieznany"
    otp_only = profile.get("otp_only", False)
    if otp_only:
        try:
            text_pattern = profile.get("otp_text_pattern")
            key_pattern = profile.get("otp_key_pattern")
            if not isinstance(text_pattern, str) or not isinstance(key_pattern, str) or not re.fullmatch(text_pattern, text) or not re.fullmatch(key_pattern, key):
                return {"error": "bad request"}
        except re.error:
            return {"error": "bad request"}
    if not KEY.fullmatch(key) or not text:
        return {"error": "bad request"}
    text = re.sub(r"\s+", " ", text)[: int(cfg["max_chars"])]
    name = profile["name"]
    all_aliases = list(cfg["recipients"])
    default_to = all_aliases if "*" in profile["recipients"] else [a for a in all_aliases if a in profile["recipients"]]
    wanted = req.get("to") or default_to
    if isinstance(wanted, str):  # "contact1" zamiast ["contact1"]
        wanted = [wanted]
    if not isinstance(wanted, list) or not all(isinstance(a, str) for a in wanted):
        return {"error": "bad request"}
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    client_cap = profile["daily_limit"] or int(cfg["client_daily_limit"])
    client_count_key = f"{today}|client|{name}"
    results = {}
    with LOCK:
        state = load_state()
        state["sent"] = {k: v for k, v in state.get("sent", {}).items()
                         if v > (now - timedelta(days=7)).timestamp()}
        state["counts"] = {k: v for k, v in state.get("counts", {}).items() if k.startswith(today)}
        state["results"] = {k: v for k, v in state.get("results", {}).items()
                            if v.get("ts", 0) > (now - timedelta(days=7)).timestamp()}
        state.setdefault("queue", [])
        pending = {q["dedupe"] for q in state["queue"]}
        for alias in wanted:
            rec = cfg["recipients"].get(alias) if isinstance(alias, str) and ALIAS.fullmatch(alias) else None
            if not isinstance(rec, dict):
                results[str(alias)[:30]] = {"status": "unknown_recipient"}
                continue
            if not allowed(profile, alias):
                results[alias] = {"status": "forbidden_recipient"}
                continue
            results[alias] = {}
            for channel in (("sms",) if otp_only else ("whatsapp", "sms")):  # równolegle: wszystkie włączone kanały z numerem
                number = str(rec.get(channel, "")).strip()
                if not cfg["channels"].get(channel) or not NUMBER.fullmatch(number):
                    continue
                prefix = "" if profile["legacy"] else name + ":"  # stan LegacyClienta zostaje zgodny wstecz
                dedupe = f"{prefix}{key}|{alias}|{channel}"
                count_key = f"{today}|{channel}|{alias}"  # limit dzienny liczony na odbiorcę i kanał
                if dedupe in state["sent"]:
                    results[alias][channel] = "duplicate"
                elif dedupe in pending:
                    results[alias][channel] = "queued"
                elif priority != "high" and in_quiet_hours(cfg, now):
                    results[alias][channel] = "deferred"
                elif (priority != "high" and not otp_only) and state["counts"].get(count_key, 0) >= int(cfg["daily_limit"]):
                    results[alias][channel] = "limit"
                elif (not profile["legacy"] and (priority != "high" and not otp_only)
                      and state["counts"].get(client_count_key, 0) >= client_cap):
                    results[alias][channel] = "limit"
                elif not cfg["live"]:
                    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
                    fd = os.open(state_path("dryrun.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "key": key, "to": alias,
                                             "client": name, "channel": channel, "priority": priority,
                                             "text": "[redacted]" if otp_only else text}, ensure_ascii=False) + "\n")
                    results[alias][channel] = "dry_run"
                else:
                    real = deliver(channel, number, text, cfg)
                    if real in ("sent", "sent_unconfirmed", "unknown"):
                        state["sent"][dedupe] = now.timestamp()
                        state["counts"][count_key] = state["counts"].get(count_key, 0) + 1
                        state["counts"][client_count_key] = state["counts"].get(client_count_key, 0) + 1
                    elif profile["retry"] and not otp_only:
                        state["queue"].append({"dedupe": dedupe, "client": name, "key": key, "alias": alias,
                                               "channel": channel, "text": text, "priority": priority,
                                               "kind": kind, "attempts": 1, "created": now.timestamp(),
                                               "next_at": now.timestamp() + int(cfg["retry"]["backoff_s"][0])})
                        pending.add(dedupe)
                        real = "queued"
                    results[alias][channel] = report(profile, real)
                    log(f"{name}: {channel} -> {alias}: {real} ({len(text)} znakow)")
                    ledger(now, key, alias, channel, priority, real, "[redacted]" if otp_only else text, kind)
                    remember(state, name, key, alias, channel, real, now)
                    continue
                ledger(now, key, alias, channel, priority, results[alias][channel], "[redacted]" if otp_only else text, kind)
                remember(state, name, key, alias, channel, results[alias][channel], now)
            if not results[alias]:
                results[alias] = {"status": "no_channel"}
        save_state(state)
    return {"key": key, "live": bool(cfg["live"]), "client": name, "results": results}


def sent_today(state: dict, today: str) -> dict:
    """Liczba wysłanych dziś wiadomości wg kanału (sumarycznie dla wszystkich odbiorców)."""
    out: dict = {}
    for key, value in state.get("counts", {}).items():
        parts = key.split("|")
        if len(parts) < 3 or parts[1] == "client":
            continue
        if parts[0] == today:
            out[parts[1]] = out.get(parts[1], 0) + value
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "sms-gateway"

    def log_message(self, *args):
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _client(self):
        try:
            cfg = load_config()
        except (OSError, ValueError):
            self._send(401, {"error": "unauthorized"})
            return None
        profile, code = identify(self.client_address[0], self.headers.get("Authorization", ""), cfg)
        if profile is None:
            self._send(code, {"error": "forbidden" if code == 403 else "unauthorized"})
        return profile

    def do_GET(self):
        profile = self._client()
        if profile is None:
            return
        try:
            cfg = load_config()
            state = load_state()
        except (OSError, ValueError):
            return self._send(503, {"error": "state unavailable"})
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self.path.startswith("/v1/result/"):
            key = self.path[len("/v1/result/"):]
            if not KEY.fullmatch(key):
                return self._send(400, {"error": "bad key"})
            found = state.get("results", {}).get(f"{profile['name']}|{key}")
            if not found:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"key": key, "results": found["to"]})
        if self.path != "/v1/status":
            return self._send(404, {"error": "not found"})
        configured = {a: [c for c in ("sms", "whatsapp")
                          if cfg["channels"].get(c) and NUMBER.fullmatch(str(r.get(c, "")))]
                      for a, r in cfg["recipients"].items() if isinstance(r, dict) and allowed(profile, a)}
        mine = sum(1 for q in state.get("queue", []) if q["client"] == profile["name"])
        return self._send(200, {"live": bool(cfg["live"]), "client": profile["name"], "channels": cfg["channels"],
                                "daily_limit": cfg["daily_limit"], "quiet_hours": cfg["quiet_hours"],
                                "recipients": configured, "sent_today": sent_today(state, today),
                                "client_sent_today": state.get("counts", {}).get(
                                    f"{today}|client|{profile['name']}", 0),
                                "queued": mine})

    def do_POST(self):
        profile = self._client()
        if profile is None:
            return
        if self.path != "/v1/notify":
            return self._send(404, {"error": "not found"})
        try:
            length = min(4096, int(self.headers.get("Content-Length", "0")))
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, {"error": "bad json"})
        if not isinstance(req, dict):
            return self._send(400, {"error": "bad json"})
        try:
            out = notify(req, profile)
        except (OSError, ValueError):
            return self._send(503, {"error": "state unavailable"})
        return self._send(400 if "error" in out else 200, out)


# --- kanał zwrotny (etap 2) --------------------------------------------------------------------------
INBOUND_PROFILE = {"name": "inbound", "recipients": ["*"], "allow_high": True, "retry": True, "daily_limit": None,
                   "legacy": False}


def inbound_reply(alias: str, text: str) -> None:
    """Odpowiedź dla nadawcy komendy: priorytet high (omija ciche godziny i limity), z ponowieniami."""
    notify({"key": "in-" + secrets.token_hex(6), "text": text, "to": [alias], "priority": "high",
            "kind": "inbound"}, INBOUND_PROFILE)


def inbound_status_text() -> str:
    cfg = load_config()
    state = load_state()
    return f"Bramka OK, live={'tak' if cfg['live'] else 'nie'}, w kolejce {len(state.get('queue', []))}."


def inbox_has_new(z):
    """True/False, jeśli modem odpowiada bez logowania o nowych SMS-ach; None gdy nie wiadomo
    (wtedy odpytujemy rzadziej pełnym logowaniem). NIEWERYFIKOWANE na MC888."""
    try:
        r = z.get("sms_unread_num,sms_received_flag", multi=True)
    except Exception:
        return None
    if "sms_unread_num" not in r:
        return None
    return str(r.get("sms_unread_num") or "0") != "0" or str(r.get("sms_received_flag") or "0") == "1"


def received_sms(z, n: int) -> list:
    """Tylko wiadomości ODEBRANE (skrzynka odbiorcza, tags=1). Zte().sms() zwraca tags=10, czyli odebrane i wysłane razem,
    a przy wysłanych pole "number" to odbiorca. Takie wiadomości nie mogą trafić do kanału zwrotnego (potwierdzone na MC888
    07.11.2026: własne alerty bramki widać z tagiem 2 i numerem odbiorcy)."""
    r = z.get("sms_data_total", extra=f"&page=0&data_per_page={int(n)}&mem_store=1&tags=1&order_by=order+by+id+desc")
    out = []
    for m in r.get("messages", []):
        if str(m.get("tag")) not in ("0", "1"):  # 0 przeczytana odebrana, 1 nieprzeczytana odebrana; 2 = wysłana
            continue
        try:
            text = bytes.fromhex(m.get("content", "")).decode("utf-16-be", "replace")
        except ValueError:
            text = ""
        out.append({"id": str(m.get("id")), "date": m.get("date", ""), "from": m.get("number", ""), "text": text})
    return out


def sms_age_s(date_str: str, now: float):
    """Wiek SMS-a w sekundach z daty modemu 'RR,MM,DD,GG,MM,SS,+T' (T = strefa w kwadransach). None, gdy nie da się odczytać."""
    try:
        p = date_str.split(",")
        tz = timezone(timedelta(minutes=15 * int(p[6])))
        sent = datetime(2000 + int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]), tzinfo=tz)
        return now - sent.timestamp()
    except (ValueError, IndexError, TypeError):
        return None


def modem_forget(z, delete_ids, read_ids=()) -> None:
    """Oznacza jako przeczytane `read_ids` i usuwa `delete_ids` (best effort; deduplikacja chroni przed ponownym użyciem).
    Usuwamy WYŁĄCZNIE wiadomości od dozwolonych nadawców: SMS-y od Orange czyta sonar WAN, więc nie wolno ich kasować."""
    read_ids, delete_ids = list(read_ids), list(delete_ids)
    forms = []
    if read_ids or delete_ids:
        forms.append({"goformId": "SET_MSG_READ", "msg_id": "".join(f"{i};" for i in dict.fromkeys(read_ids + delete_ids)), "tag": "0"})
    if delete_ids:
        forms.append({"goformId": "DELETE_SMS", "msg_id": "".join(f"{i};" for i in delete_ids), "notCallback": "true"})
    for form in forms:
        try:
            z.set(**form)
        except Exception as exc:
            log(f"inbound: {form['goformId']} niepowodzenie: {type(exc).__name__}")


def poll_inbox(inb, state_box: dict) -> int:
    """Jedno odpytanie skrzynki odbiorczej. Zwraca liczbę przetworzonych SMS-ów. Sesja modemu tylko pod LOCK."""
    cfg = load_config()
    icfg = cfg.get("inbound", {})
    if not icfg.get("enabled") or inb.disabled():
        return 0
    from zte_api import Zte  # import leniwy
    now = time.time()
    with LOCK:
        z = Zte()
        hint = inbox_has_new(z)
        due_full = now - state_box.get("last_full", 0) >= int(icfg.get("fallback_poll_s", 300))
        if hint is False or (hint is None and not due_full):
            return 0
        state_box["last_full"] = now
        if not z.login():
            log("inbound: logowanie do modemu nieudane")
            return 0
        messages = received_sms(z, int(icfg.get("batch", 20)))
    max_age = int(icfg.get("max_age_s", 900))
    to_delete, to_read = [], []
    for msg in reversed(messages):  # od najstarszej
        age = sms_age_s(msg.get("date", ""), now)
        if age is not None and age > max_age:
            to_read.append(msg["id"])  # stara wiadomość: nie wykonujemy, nie kasujemy
            continue
        try:
            status = inb.handle(msg)
        except Exception as exc:  # jedna zła wiadomość nie zatrzymuje reszty
            log(f"inbound: blad obslugi: {type(exc).__name__}")
            status = "error"
        # kasujemy tylko to, co przyszło od dozwolonego nadawcy; resztę (np. Orange) tylko oznaczamy jako przeczytane
        (to_read if status in ("rejected_sender", "channel_disabled", "error") else to_delete).append(msg["id"])
    if to_delete or to_read:
        with LOCK:
            z2 = Zte()
            if z2.login():
                modem_forget(z2, to_delete, to_read)
    return len(to_delete) + len(to_read)


def inbound_loop() -> None:
    import sms_inbound
    inb = sms_inbound.Inbound(load_config, STATE_DIR, inbound_reply, inbound_status_text)
    box: dict = {}
    while True:
        try:
            poll_s = max(20, int(load_config().get("inbound", {}).get("poll_s", 120)))
        except (OSError, ValueError):
            poll_s = 120
        time.sleep(poll_s)
        try:
            poll_inbox(inb, box)
        except Exception as exc:  # wątek nie może paść
            log(f"inbound_loop: {type(exc).__name__}")


def totp_init() -> int:
    """Tworzy sekret TOTP (jeśli go nie ma) i wypisuje adres do aplikacji uwierzytelniającej. Uruchamia człowiek."""
    path = load_config().get("inbound", {}).get("totp_file", "/etc/sms-gateway.totp")
    if os.path.exists(path):
        print(f"Sekret już istnieje: {path}. Usuń plik, żeby wygenerować nowy (stary kod przestanie działać).")
        return 1
    import base64
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    print(f"Sekret zapisano w {path} (600).")
    print(f"otpauth://totp/SMS%20Gateway?secret={secret}&issuer=sms-gateway&period=30&digits=6&algorithm=SHA1")
    print("Dodaj go raz w aplikacji uwierzytelniającej, potem wyczyść ekran historii terminala.")
    return 0


if __name__ == "__main__":
    if "--totp-init" in sys.argv:
        sys.exit(totp_init())
    if "--new-token" in sys.argv:
        tok = secrets.token_urlsafe(32)
        print(f"token (przekaż klientowi, nie zapisuj w repo): {tok}")
        print(f'"token_sha256": "{token_hash(tok)}"   <- do sekcji clients w {CONFIG}')
        sys.exit(0)
    cfg = load_config()
    bind, port = os.environ.get("SMSGW_BIND", "127.0.0.1"), int(os.environ.get("SMSGW_PORT", "8095"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=retry_loop, daemon=True).start()
    threading.Thread(target=inbound_loop, daemon=True).start()
    log(f"sms-gateway na {bind}:{port}, live={cfg['live']}, kanaly={cfg['channels']}, klienci={list(cfg['clients'])}, kanal_zwrotny={bool(cfg['inbound'].get('enabled'))}")
    srv.serve_forever()
