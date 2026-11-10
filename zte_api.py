"""Wspolny klient API ZTE MC888 (login SHA256 + token AD dla komend set).
Haslo: ZTE_PASS ze środowiska lub jawnie wskazanego pliku.
GOTCHA: MC888 ma 1 sesje admina - kazde logowanie moze wyrzucic usera z panelu www.
GOTCHA: set bez tokena AD zwraca {"result":"failure"} mimo poprawnej sesji."""
import os, urllib.request, urllib.parse, http.cookiejar, json, hashlib

ZTE = os.environ.get("ZTE_URL", "http://192.168.0.1").rstrip("/")
ENV = os.environ.get("ZTE_ENV_FILE", "")


def _env(key):
    if key in os.environ: return os.environ[key]
    if not ENV: return ""
    try:
        for ln in open(ENV):
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest().upper()


class Zte:
    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op.addheaders = [("Referer", ZTE + "/index.html"),
                              ("X-Requested-With", "XMLHttpRequest")]

    def get(self, cmd, multi=False, extra=""):
        url = (f"{ZTE}/goform/goform_get_cmd_process?isTest=false&cmd={cmd}"
               + ("&multi_data=1" if multi else "") + extra)
        return json.loads(self.op.open(url, timeout=8).read().decode())

    def _post(self, data):
        req = urllib.request.Request(
            ZTE + "/goform/goform_set_cmd_process",
            data=urllib.parse.urlencode(data).encode(),
            headers={"Referer": ZTE + "/index.html",
                     "Content-Type": "application/x-www-form-urlencoded"})
        return json.loads(self.op.open(req, timeout=10).read().decode())

    def login(self):
        ld = self.get("LD").get("LD", "")
        r = self._post({"isTest": "false", "goformId": "LOGIN",
                        "password": sha(sha(_env("ZTE_PASS")) + ld)})
        return r.get("result") == "0"

    def set(self, **kw):
        v = self.get("wa_inner_version,cr_version", multi=True)
        ad = sha(sha(v.get("wa_inner_version", "") + v.get("cr_version", ""))
                 + self.get("RD").get("RD", ""))
        kw.update({"isTest": "false", "AD": ad})
        return self._post(kw)

    def uptime_s(self):
        try:
            return int(self.get("realtime_time").get("realtime_time") or -1)
        except (ValueError, TypeError):
            return -1

    def radio(self):
        return self.get("lte_rsrp,lte_snr,Z5g_rsrp,network_type,ppp_status,"
                        "wan_active_band,nr5g_action_band,signalbar,wan_ipaddr",
                        multi=True)

    def sms(self, n=15):
        r = self.get("sms_data_total",
                     extra=f"&page=0&data_per_page={n}&mem_store=1&tags=10"
                           "&order_by=order+by+id+desc")
        out = []
        for m in r.get("messages", []):
            try:
                txt = bytes.fromhex(m.get("content", "")).decode("utf-16-be", "replace")
            except ValueError:
                txt = ""
            out.append({"id": str(m.get("id")), "date": m.get("date", ""),
                        "from": m.get("number", ""), "text": txt})
        return out

    def reboot(self):
        return self.set(goformId="REBOOT_DEVICE")
