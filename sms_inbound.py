"""Kanał zwrotny bramki SMS (etap 2, 7.11.2026): odbiór SMS-ów z modemu i wykonywanie ZATWIERDZONYCH komend.

SMS da się sfałszować (nadawca, ponowne wysłanie), dlatego sam numer nie wystarcza. Każda komenda wymaga:
  1. nadawcy z listy (`inbound.senders`, aliasy odbiorców; numer z `recipients.<alias>.sms`),
  2. kodu TOTP (RFC 6238, SHA-1, 6 cyfr, krok 30 s) na początku wiadomości: "123456 cwu off". Okno ±`window` kroków,
     bo SMS bywa opóźniony. Kod jest jednorazowy: przyjmujemy tylko kod z kroku nowszego niż ostatnio użyty,
  3. komendy ze stałej listy w konfiguracji (`inbound.commands`), nie dowolnego tekstu. Wolny tekst do agenta
     idzie tylko przez komendę z prefiksem z sekcji `inbound.agent` i domyślnie wymaga potwierdzenia,
  4. dla komend z `"confirm": true` potwierdzenia drugim SMS-em: "TAK <4 cyfry>" w ciągu 5 minut (kod widzi tylko
     prawdziwy odbiorca odpowiedzi), albo "NIE".
Błędne kody: po `lock_after` próbach w godzinie nadawca jest blokowany na godzinę. Nieznany nadawca jest ignorowany
bez odpowiedzi (nie płacimy za SMS-y do obcych i nie tworzymy pętli). Dziennik `inbound.jsonl` nie zawiera treści
wiadomości odrzuconych, kodów TOTP ani numerów (tylko alias lub maska).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import subprocess
import threading
import time
import unicodedata
import urllib.request

STEP = 30
CONFIRM_TTL_S = 300
SEEN_TTL_S = 7 * 86400


def totp(secret_b32: str, counter: int) -> str:
    raw = secret_b32.strip().replace(" ", "").upper()
    raw += "=" * (-len(raw) % 8)
    key = base64.b32decode(raw)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret_b32: str, code: str, now_ts: float, window: int, last_counter: int):
    """Zwraca licznik kroku, jeśli kod jest poprawny i NOWSZY od ostatnio użytego; inaczej None."""
    base = int(now_ts // STEP)
    for delta in range(-window, window + 1):
        counter = base + delta
        if counter > last_counter and hmac.compare_digest(totp(secret_b32, counter), code):
            return counter
    return None


def norm(text: str) -> str:
    """małe litery, bez ogonków, spacje zwinięte: 'CWU  wyłącz' -> 'cwu wylacz'."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("ł", "l").replace("Ł", "l")
    return re.sub(r"\s+", " ", t.strip().lower())


def same_number(a: str, b: str) -> bool:
    def number(value):
        digits = re.sub(r"\D", "", value or "")
        if digits.startswith("00"): digits = digits[2:]
        if len(digits) == 9: digits = "48" + digits
        return digits if 10 <= len(digits) <= 15 else None
    left, right = number(a), number(b)
    return left is not None and left == right


def mask(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return ("***" + d[-3:]) if d else "?"


class Inbound:
    """Logika odbioru. Zależności wstrzykiwane, żeby dało się testować bez modemu i bez sieci:
    reply(alias, text) wysyła odpowiedź przez bramkę; status_text() zwraca krótki opis stanu;
    state_dir to katalog stanu bramki."""

    def __init__(self, cfg_get, state_dir, reply, status_text=lambda: "bramka dziala", now=time.time, runner=None):
        self.cfg_get = cfg_get
        self.state_dir = state_dir
        self.reply = reply
        self.status_text = status_text
        self.now = now
        self.runner = runner or self._run_action
        self.lock = threading.Lock()

    # --- stan -------------------------------------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def load(self) -> dict:
        try:
            with open(self._path("inbound_state.json"), encoding="utf-8") as fh:
                st = json.load(fh)
        except FileNotFoundError:
            st = {}
        for k, v in (("seen", {}), ("last_counter", 0), ("fails", {}), ("locked", {}), ("pending", {}), ("cmds", {})):
            st.setdefault(k, v)
        return st

    def save(self, st: dict) -> None:
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        tmp = self._path("inbound_state.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, self._path("inbound_state.json"))

    def audit(self, who: str, status: str, command: str = "") -> None:
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            fd = os.open(self._path("inbound.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.now())),
                                     "from": who, "status": status, "command": command}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def disabled(self) -> bool:
        return os.path.exists(self._path("inbound.disabled"))

    # --- główna ścieżka ---------------------------------------------------------------------------
    def handle(self, msg: dict) -> str:
        """msg: {id, date, from, text}. Zwraca stan (do testów i dziennika)."""
        with self.lock:
            return self._handle(msg)

    def _alias_for(self, cfg: dict, number: str):
        icfg = cfg.get("inbound", {})
        for alias in icfg.get("senders", []):
            rec = cfg.get("recipients", {}).get(alias, {})
            if same_number(str(rec.get("sms", "")), number):
                return alias
        return None

    def _handle(self, msg: dict) -> str:
        cfg = self.cfg_get()
        icfg = cfg.get("inbound", {})
        now = self.now()
        st = self.load()
        st["seen"] = {k: v for k, v in st["seen"].items() if v > now - SEEN_TTL_S}
        seen_key = f"{msg.get('id')}|{msg.get('date')}"
        if seen_key in st["seen"]:
            return "seen"
        st["seen"][seen_key] = now
        try:
            if self.disabled() or not icfg.get("enabled"):
                self.audit(mask(msg.get("from", "")), "channel_disabled")
                return "channel_disabled"
            alias = self._alias_for(cfg, msg.get("from", ""))
            if alias is None:
                self.audit(mask(msg.get("from", "")), "rejected_sender")
                return "rejected_sender"
            if st["locked"].get(alias, 0) > now:
                self.audit(alias, "locked")
                return "locked"
            body = (msg.get("text") or "").strip()
            pend = st["pending"].get(alias)
            if pend and pend["expires"] < now:
                st["pending"].pop(alias, None)
                pend = None
            nb = norm(body)
            # --- potwierdzenie / anulowanie oczekującej komendy
            if pend and nb == "nie":
                st["pending"].pop(alias, None)
                self.audit(alias, "cancelled", pend["name"])
                self.reply(alias, f"Anulowano: {pend['name']}.")
                return "cancelled"
            m = re.fullmatch(r"tak (\d{4})", nb)
            if m:
                if pend and hmac.compare_digest(m.group(1), pend["code"]):
                    st["pending"].pop(alias, None)
                    return self._run(cfg, st, alias, pend["name"], pend["args"], confirmed=True)
                return self._fail(cfg, st, alias, "bad_confirm")
            # --- nowa komenda: "<TOTP> <komenda>"
            m = re.fullmatch(r"(\d{6})\s+(.+)", body, flags=re.S)
            if not m:
                return self._fail(cfg, st, alias, "bad_format")
            secret = self._secret(icfg)
            counter = verify_totp(secret, m.group(1), now, int(icfg.get("window", 10)), int(st["last_counter"])) if secret else None
            if counter is None:
                return self._fail(cfg, st, alias, "bad_code")
            st["last_counter"] = counter  # kod zużyty, także gdy komenda okaże się nieznana
            st["fails"].pop(alias, None)
            hour = [t for t in st["cmds"].get(alias, []) if t > now - 3600]
            if len(hour) >= int(icfg.get("max_per_hour", 12)):
                st["cmds"][alias] = hour
                self.audit(alias, "rate_limited")
                return "rate_limited"
            st["cmds"][alias] = hour + [now]
            return self._dispatch(cfg, st, alias, norm(m.group(2)), m.group(2).strip())
        finally:
            self.save(st)

    def _secret(self, icfg: dict) -> str:
        try:
            with open(icfg.get("totp_file", "/etc/sms-gateway.totp"), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _fail(self, cfg, st, alias, why) -> str:
        icfg = cfg.get("inbound", {})
        now = self.now()
        fails = [t for t in st["fails"].get(alias, []) if t > now - 3600] + [now]
        st["fails"][alias] = fails
        self.audit(alias, why)
        if len(fails) >= int(icfg.get("lock_after", 5)):
            st["locked"][alias] = now + 3600
            st["fails"].pop(alias, None)
            self.audit(alias, "locked_1h")
            self.reply(alias, "Kanał SMS zablokowany na 1 godzinę po błędnych kodach.")
            return "locked_1h"
        return why  # bez odpowiedzi: nie ujawniamy, co było błędne

    def _dispatch(self, cfg, st, alias, nb, original) -> str:
        icfg = cfg.get("inbound", {})
        commands = {norm(k): v for k, v in icfg.get("commands", {}).items()}
        builtin = {"status": "status", "pomoc": "help", "help": "help", "blokada": "lock"}
        spec = commands.get(nb)
        name, args = nb, ""
        if spec is None and nb in builtin:
            spec = {"builtin": builtin[nb]}
        if spec is None:
            agent = icfg.get("agent") or {}
            prefix = norm(agent.get("prefix", ""))
            if prefix and (nb == prefix or nb.startswith(prefix + " ")):
                spec = {"agent": True, "confirm": agent.get("confirm", True), "desc": "polecenie do agenta"}
                name, args = prefix, original[len(agent.get("prefix", "")):].strip()
        if spec is None:
            self.audit(alias, "unknown_command")
            self.reply(alias, "Nieznana komenda. Napisz: <kod> pomoc")
            return "unknown_command"
        if spec.get("confirm"):
            code = f"{secrets.randbelow(10000):04d}"
            st["pending"][alias] = {"name": name, "args": args, "code": code, "expires": self.now() + CONFIRM_TTL_S}
            self.audit(alias, "confirm_pending", name)
            self.reply(alias, f"Potwierdź: {spec.get('desc', name)}. Odpisz TAK {code} (5 min) albo NIE.")
            return "confirm_pending"
        return self._run(cfg, st, alias, name, args, confirmed=False)

    def _run(self, cfg, st, alias, name, args, confirmed) -> str:
        icfg = cfg.get("inbound", {})
        commands = {norm(k): v for k, v in icfg.get("commands", {}).items()}
        spec = commands.get(name)
        agent = icfg.get("agent") or {}
        if spec is None and name == norm(agent.get("prefix", "")) and name:
            spec = {"agent": True}
        if spec is None:
            spec = {"builtin": {"status": "status", "pomoc": "help", "help": "help", "blokada": "lock"}.get(name)}
        if spec.get("builtin") == "status":
            text = self.status_text()
        elif spec.get("builtin") == "help":
            names = sorted(set(list(commands) + ["status", "pomoc", "blokada"] + ([norm(agent["prefix"])] if agent.get("prefix") else [])))
            text = "Komendy: " + ", ".join(names)
        elif spec.get("builtin") == "lock":
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            open(self._path("inbound.disabled"), "w").close()
            text = "Kanał SMS wyłączony. Włączenie: usunąć inbound.disabled na host."
        elif spec.get("agent"):
            # agent działa w tle: odpowiedź przyjdzie osobnym SMS-em
            threading.Thread(target=self._agent_job, args=(cfg, alias, args), daemon=True).start()
            self.audit(alias, "agent_started", name)
            return "agent_started"
        else:
            try:
                text = self.runner(cfg, spec, alias, name)
            except Exception as exc:
                self.audit(alias, "failed", name)
                self.reply(alias, f"Błąd wykonania: {name} ({type(exc).__name__}).")
                return "failed"
        self.audit(alias, "executed", name)
        self.reply(alias, text or spec.get("reply") or f"Wykonano: {name}.")
        return "executed"

    # --- wykonawcy --------------------------------------------------------------------------------
    def _run_action(self, cfg, spec, alias, name) -> str:
        icfg = cfg.get("inbound", {})
        if spec.get("ha_webhook"):
            url = f"{icfg.get('ha_url', 'http://127.0.0.1:8123')}/api/webhook/{spec['ha_webhook']}"
            req = urllib.request.Request(url, data=json.dumps({"command": name, "from": alias}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if not 200 <= resp.status < 300:
                    raise RuntimeError("webhook")
            return spec.get("reply") or f"Wykonano: {name}."
        if spec.get("argv"):
            out = subprocess.run(list(spec["argv"]), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=int(spec.get("timeout_s", 30)), check=True)
            return (out.stdout.strip() or spec.get("reply") or f"Wykonano: {name}.")[:190]
        raise RuntimeError("brak wykonawcy")

    def _agent_job(self, cfg, alias, text) -> None:
        agent = cfg.get("inbound", {}).get("agent") or {}
        try:
            out = subprocess.run(list(agent["argv"]), input=text, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=int(agent.get("timeout_s", 300)), check=True)
            answer = out.stdout.strip() or "Agent nie zwrócił odpowiedzi."
        except Exception as exc:
            answer = f"Agent: błąd ({type(exc).__name__})."
        self.audit(alias, "agent_done", "agent")
        self.reply(alias, answer[:190])
