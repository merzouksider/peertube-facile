#!/usr/bin/env python3
"""PeerTube Facile - assistant d'installation.

Petit serveur web (bibliothèque standard Python uniquement) qui guide
l'installation d'une instance PeerTube : réglages de la box, test des ports
depuis Internet, vérification DNS, installation, puis tableau de bord.
"""
import collections
import glob
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ETC = os.environ.get("PTF_ETC", "/etc/peertube-facile")
PORT = int(os.environ.get("PTF_PORT", "8080"))
PROBES = os.environ.get("PTF_PROBES", "1") == "1"
PROBE_PORTS = [int(p) for p in os.environ.get("PTF_PROBE_PORTS", "80,443").split(",")]
INSTALL_SCRIPT = os.environ.get("PTF_INSTALL_SCRIPT", os.path.join(BASE, "install-peertube.sh"))
BANNER_SCRIPT = os.path.join(BASE, "banner.sh")
PEERTUBE_DIR = os.environ.get("PTF_PEERTUBE_DIR", "/srv/peertube")
FAKE_NET = os.environ.get("PTF_FAKE_NET") == "1"

STATE_FILE = os.path.join(ETC, "etat.json")
CODE_FILE = os.path.join(ETC, "code")
LOG_FILE = os.path.join(ETC, "journal.log")
LINUX_PW_FILE = os.path.join(ETC, "mot-de-passe-linux")

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

lock = threading.RLock()


# --------------------------------------------------------------------------
# Fichiers d'état
# --------------------------------------------------------------------------

def ensure_etc():
    os.makedirs(ETC, mode=0o700, exist_ok=True)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    ensure_etc()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def update_state(**kw):
    with lock:
        s = load_state()
        s.update(kw)
        save_state(s)
        return s


def ensure_code():
    """Code à 6 chiffres affiché sur l'écran du PC, demandé par l'assistant."""
    ensure_etc()
    try:
        fd = os.open(CODE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(secrets.choice("0123456789") for _ in range(6)))
    except FileExistsError:
        pass
    with open(CODE_FILE) as f:
        return f.read().strip()


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Réseau
# --------------------------------------------------------------------------

def http_get(url, headers=None, timeout=6):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "peertube-facile/1.0"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def local_ip():
    if FAKE_NET:
        return "192.168.1.42"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("9.9.9.9", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def default_route():
    """(interface, passerelle) depuis /proc/net/route."""
    if FAKE_NET:
        return "enp1s0", "192.168.1.254"
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[1] == "00000000" and int(p[3], 16) & 2:
                    gw = socket.inet_ntoa(int(p[2], 16).to_bytes(4, "little"))
                    return p[0], gw
    except (OSError, IndexError, ValueError):
        pass
    return None, None


def mac_address(iface):
    if FAKE_NET:
        return "3c:52:82:aa:bb:cc"
    return read_text(f"/sys/class/net/{iface}/address") if iface else None


_pub_cache = {}


def public_ip(v6=False, max_age=60):
    key = "v6" if v6 else "v4"
    hit = _pub_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    if FAKE_NET:
        ip = None if v6 else os.environ.get("PTF_FAKE_PUBLIC_IP", "86.200.10.20")
    else:
        urls = ["https://api6.ipify.org", "https://ipv6.icanhazip.com"] if v6 else \
               ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"]
        ip = None
        for u in urls:
            try:
                cand = http_get(u, timeout=4).strip()
                ipaddress.ip_address(cand)
                ip = cand
                break
            except Exception:
                continue
    _pub_cache[key] = (time.time(), ip)
    return ip


def is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


# --------------------------------------------------------------------------
# DNS (via DNS-over-HTTPS, pour voir ce que voit Internet)
# --------------------------------------------------------------------------

RTYPES = {"A": 1, "NS": 2, "CNAME": 5, "AAAA": 28, "CAA": 257}
FAKE_DNS = {}  # tests uniquement : {("nom", "TYPE"): [valeurs]}
if FAKE_NET and os.environ.get("PTF_FAKE_DNS"):
    with open(os.environ["PTF_FAKE_DNS"]) as _f:
        FAKE_DNS = {tuple(k.split("|")): v for k, v in json.load(_f).items()}


def doh(name, rtype, provider="google"):
    """Retourne la liste des valeurs du type demandé, ou None si erreur réseau."""
    if FAKE_NET:
        return list(FAKE_DNS.get((name, rtype, provider), FAKE_DNS.get((name, rtype), [])))
    q = urllib.parse.urlencode({"name": name, "type": rtype})
    if provider == "google":
        url, headers = "https://dns.google/resolve?" + q, {}
    else:
        url, headers = "https://cloudflare-dns.com/dns-query?" + q, {"Accept": "application/dns-json"}
    try:
        data = json.loads(http_get(url, headers, timeout=6))
    except Exception:
        return None
    want = RTYPES[rtype]
    return [a["data"].rstrip(".") for a in data.get("Answer", []) if a.get("type") == want]


def find_nameservers(domain):
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        ns = doh(name, "NS")
        if ns:
            return name, sorted(ns)
    return None, []


def find_caa(domain):
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        caa = doh(".".join(labels[i:]), "CAA")
        if caa:
            return caa
    return []


def check_dns(domain):
    checks = []

    def add(cid, titre, statut, detail):
        checks.append({"id": cid, "titre": titre, "statut": statut, "detail": detail})

    domain = (domain or "").strip().lower().rstrip(".")
    if domain.startswith(("http://", "https://")):
        domain = domain.split("://", 1)[1].split("/", 1)[0]
    if not DOMAIN_RE.match(domain):
        add("format", "Nom de domaine", "erreur",
            "Ce nom ne ressemble pas à un nom de domaine. Exemple : videos.mondomaine.fr")
        return domain, checks

    pub = public_ip(max_age=10)

    # 1. Qui gère le domaine ?
    zone, ns = find_nameservers(domain)
    if ns is None or not ns:
        add("ns", "Gestionnaire DNS", "attention",
            "Impossible de savoir qui gère ce domaine. Vérifie l'orthographe, ou que le domaine est bien acheté et actif.")
    elif any("o2switch" in n for n in ns):
        add("ns", "Gestionnaire DNS", "ok",
            f"Le domaine {zone} est géré par o2switch ({', '.join(ns[:2])}). Les réglages se font dans cPanel, « Éditeur de zone ».")
    else:
        add("ns", "Gestionnaire DNS", "ok",
            f"Le domaine {zone} est géré par {', '.join(ns[:2])}. Ce n'est pas o2switch : le principe est le même, "
            "ajoute l'enregistrement A chez ce gestionnaire.")

    # 2. Enregistrement A
    a_google = doh(domain, "A", "google")
    a_cf = doh(domain, "A", "cloudflare")
    if a_google is None and a_cf is None:
        add("a", "Adresse IPv4 (enregistrement A)", "attention",
            "Le PC n'arrive pas à interroger les serveurs DNS publics. Vérifie qu'il a bien accès à Internet.")
    else:
        a = a_google if a_google is not None else a_cf
        if not a:
            add("a", "Adresse IPv4 (enregistrement A)", "erreur",
                f"Aucune adresse n'est associée à {domain}. Ajoute un enregistrement A qui pointe vers {pub or 'ton adresse IP publique'}. "
                "Après l'ajout, compte de quelques minutes à une heure.")
        elif pub is None:
            add("a", "Adresse IPv4 (enregistrement A)", "attention",
                f"{domain} pointe vers {', '.join(a)}, mais l'adresse publique de ta box n'a pas pu être lue pour comparer.")
        elif pub in a and len(a) == 1:
            detail = f"{domain} pointe bien vers ta box ({pub})."
            if a_cf is not None and a_google is not None and sorted(a_cf) != sorted(a_google):
                detail += " Un autre serveur DNS voit encore l'ancienne valeur : la propagation est en cours, c'est normal."
                add("a", "Adresse IPv4 (enregistrement A)", "attention", detail)
            else:
                add("a", "Adresse IPv4 (enregistrement A)", "ok", detail)
        elif pub in a:
            autres = [x for x in a if x != pub]
            add("a", "Adresse IPv4 (enregistrement A)", "attention",
                f"{domain} pointe vers ta box, mais aussi vers {', '.join(autres)}. Supprime ces autres enregistrements A.")
        else:
            add("a", "Adresse IPv4 (enregistrement A)", "erreur",
                f"{domain} pointe vers {', '.join(a)} au lieu de {pub} (ta box). Modifie l'enregistrement A, "
                "ou attends la propagation si tu viens de le changer.")

    # 3. IPv6 : un AAAA qui pointe ailleurs fait échouer le certificat
    aaaa = doh(domain, "AAAA")
    pub6 = public_ip(v6=True)
    if aaaa:
        if pub6 and pub6 in aaaa:
            add("aaaa", "Adresse IPv6 (enregistrement AAAA)", "attention",
                "Le domaine a une adresse IPv6 qui correspond à ce PC. Vérifie que le pare-feu IPv6 de ta box autorise les ports 80 et 443, "
                "sinon supprime l'enregistrement AAAA.")
        else:
            add("aaaa", "Adresse IPv6 (enregistrement AAAA)", "erreur",
                f"Le domaine a aussi une adresse IPv6 ({', '.join(aaaa)}) qui ne mène pas à ce PC. "
                "Supprime cet enregistrement AAAA dans la zone DNS, sinon le certificat HTTPS échouera.")
    else:
        add("aaaa", "Adresse IPv6 (enregistrement AAAA)", "ok", "Pas d'adresse IPv6 parasite.")

    # 4. CAA : autorisation de Let's Encrypt
    caa = find_caa(domain)
    issuers = [c for c in caa if " issue" in f" {c}" or "issuewild" in c]
    if caa and issuers and not any("letsencrypt.org" in c for c in issuers):
        add("caa", "Autorisation du certificat (CAA)", "erreur",
            "La zone DNS limite les certificats à d'autres fournisseurs. Ajoute un enregistrement CAA : 0 issue \"letsencrypt.org\".")
    else:
        add("caa", "Autorisation du certificat (CAA)", "ok", "Let's Encrypt peut délivrer le certificat HTTPS.")

    return domain, checks


# --------------------------------------------------------------------------
# Test des ports depuis Internet
# --------------------------------------------------------------------------

probe_results = {}
probe_sockets = []
probe_stop = threading.Event()

PROBE_PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Port 80 ouvert</title>
<body style="font-family:system-ui,sans-serif;background:#f4f1ea;color:#1d2a26;display:grid;place-items:center;min-height:90vh;margin:0">
<div style="max-width:28rem;padding:2rem;background:#fff;border-radius:16px;text-align:center;box-shadow:0 2px 12px #0001">
<div style="font-size:3rem">&#10003;</div><h1 style="font-size:1.4rem">Le port 80 est bien ouvert</h1>
<p>Ta box laisse passer les visites vers ton PC PeerTube. Tu peux revenir à l'assistant.</p></div></body></html>"""


def _probe_loop(port, srv):
    while not probe_stop.is_set():
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        try:
            conn.settimeout(3)
            peer = addr[0].removeprefix("::ffff:")
            if is_public(peer):
                with lock:
                    probe_results[str(port)] = {"ok": True, "quand": time.time(), "ip": peer}
            if port == 80:
                try:
                    conn.recv(2048)
                except OSError:
                    pass
                body = PROBE_PAGE.encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        except OSError:
            pass
        finally:
            conn.close()


def start_probes():
    if not PROBES:
        return
    probe_stop.clear()
    for port in PROBE_PORTS:
        try:
            if HAS_V6:
                srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            else:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("::" if HAS_V6 else "0.0.0.0", port))
            srv.listen(16)
        except OSError as e:
            with lock:
                probe_results.setdefault(str(port), {"ok": False, "erreur": str(e)})
            continue
        probe_sockets.append(srv)
        threading.Thread(target=_probe_loop, args=(port, srv), daemon=True).start()


def stop_probes():
    probe_stop.set()
    while probe_sockets:
        s = probe_sockets.pop()
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


# --------------------------------------------------------------------------
# Tâches longues (installation, mise à jour…)
# --------------------------------------------------------------------------

task = {"en_cours": False, "type": None, "etape": 0, "total": 0, "libelle": "", "resultat": None,
        "message": "", "debut": None}
journal = collections.deque(maxlen=400)


def run_task(kind, args, env_extra=None, on_done=None):
    with lock:
        if task["en_cours"]:
            return False
        task.update(en_cours=True, type=kind, etape=0, total=0, libelle="Démarrage…", resultat=None,
                    message="", debut=time.time())
        journal.clear()

    def worker():
        env = dict(os.environ, **(env_extra or {}))
        env["PTF_PEERTUBE_DIR"] = PEERTUBE_DIR
        ensure_etc()
        rc = 1
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as log:
                log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} : {kind} =====\n")
                p = subprocess.Popen(["bash", INSTALL_SCRIPT] + args, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, text=True, bufsize=1)
                for line in p.stdout:
                    line = line.rstrip("\n")
                    log.write(line + "\n")
                    log.flush()
                    m = re.match(r"^##ETAPE (\d+)/(\d+) (.*)$", line)
                    with lock:
                        if m:
                            task.update(etape=int(m.group(1)), total=int(m.group(2)), libelle=m.group(3))
                        elif line.startswith("##MESSAGE "):
                            task["message"] = line[10:]
                        else:
                            journal.append(line)
                rc = p.wait()
        except OSError as e:
            with lock:
                journal.append(f"Erreur : {e}")
        with lock:
            task.update(en_cours=False, resultat=rc)
        if on_done:
            on_done(rc)

    threading.Thread(target=worker, daemon=True).start()
    return True


def refresh_banner():
    if os.path.exists(BANNER_SCRIPT) and os.geteuid() == 0:
        subprocess.run(["bash", BANNER_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def certificate_ok(domain):
    if not domain:
        return False
    pattern = os.path.join(PEERTUBE_DIR, "caddy", "data", "caddy", "certificates", "*", domain, domain + ".crt")
    return bool(glob.glob(pattern))


def containers_status():
    if FAKE_NET:
        return [{"nom": n, "etat": "Up 2 hours", "ok": True} for n in ("peertube", "peertube-postgres", "peertube-redis", "peertube-caddy")]
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    res = []
    for line in out.splitlines():
        name, _, status = line.partition("\t")
        if name.startswith("peertube"):
            res.append({"nom": name, "etat": status, "ok": status.startswith("Up") and "unhealthy" not in status})
    return sorted(res, key=lambda x: x["nom"])


# --------------------------------------------------------------------------
# DNS dynamique (facultatif) : appelle l'URL fournie quand l'IP change
# --------------------------------------------------------------------------

def ddns_loop():
    while True:
        time.sleep(600)
        s = load_state()
        url = s.get("ddns_url")
        if not url:
            continue
        ip = public_ip(max_age=0)
        if ip and ip != s.get("ddns_derniere_ip"):
            try:
                http_get(url, timeout=15)
                update_state(ddns_derniere_ip=ip, ddns_dernier_appel=time.time(), ddns_erreur=None)
            except Exception as e:
                update_state(ddns_erreur=str(e))


# --------------------------------------------------------------------------
# Serveur web
# --------------------------------------------------------------------------

sessions = {}
failures = {"n": 0, "jusqua": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PeerTubeFacile/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- outils
    def send_json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def authed(self):
        c = self.headers.get("Cookie") or ""
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ptf" and v in sessions and sessions[v] > time.time():
                sessions[v] = time.time() + 12 * 3600
                return True
        return False

    # -- routes
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "web", "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_error(500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/session":
            return self.send_json({"connecte": self.authed()})
        if not self.authed():
            return self.send_json({"erreur": "Code d'accès requis."}, 401)
        if path == "/api/etat":
            return self.send_json(self.etat())
        if path == "/api/journal":
            with lock:
                return self.send_json({"lignes": list(journal)})
        if path == "/api/tableau":
            return self.send_json(self.tableau())
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        data = self.body_json()
        if path == "/api/connexion":
            return self.connexion(data)
        if not self.authed():
            return self.send_json({"erreur": "Code d'accès requis."}, 401)
        if path == "/api/verifier-dns":
            domain, checks = check_dns(data.get("domaine", ""))
            ok = all(c["statut"] != "erreur" for c in checks)
            if ok:
                update_state(domaine=domain)
            return self.send_json({"domaine": domain, "verifications": checks, "ok": ok})
        if path == "/api/fai":
            update_state(fai=str(data.get("fai", ""))[:20])
            return self.send_json({"ok": True})
        if path == "/api/installer":
            return self.installer(data)
        if path == "/api/action":
            return self.action(data)
        if path == "/api/ddns":
            url = (data.get("url") or "").strip()
            if url and not url.startswith("https://"):
                return self.send_json({"erreur": "L'adresse doit commencer par https://"}, 400)
            update_state(ddns_url=url or None, ddns_derniere_ip=None)
            return self.send_json({"ok": True})
        self.send_error(404)

    # -- logique
    def connexion(self, data):
        now = time.time()
        if failures["jusqua"] > now:
            return self.send_json({"erreur": "Trop d'essais. Patiente quelques minutes."}, 429)
        code = ensure_code()
        if hmac.compare_digest(str(data.get("code", "")).strip(), code):
            failures["n"] = 0
            token = secrets.token_urlsafe(24)
            sessions[token] = now + 12 * 3600
            return self.send_json({"ok": True}, extra_headers={
                "Set-Cookie": f"ptf={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
        failures["n"] += 1
        if failures["n"] >= 8:
            failures.update(n=0, jusqua=now + 300)
        return self.send_json({"erreur": "Code incorrect. Il est affiché sur l'écran du PC PeerTube."}, 403)

    def etat(self):
        s = load_state()
        iface, gw = default_route()
        with lock:
            t = dict(task)
            probes = dict(probe_results)
        return {
            "installe": bool(s.get("installe")),
            "domaine": s.get("domaine"),
            "email": s.get("email"),
            "fai": s.get("fai"),
            "reseau": {"ip_locale": local_ip(), "passerelle": gw, "interface": iface, "mac": mac_address(iface),
                       "ip_publique": public_ip()},
            "ports": probes,
            "tache": t,
        }

    def installer(self, data):
        s = load_state()
        domain = (data.get("domaine") or s.get("domaine") or "").strip().lower()
        email = (data.get("email") or "").strip()
        pw = data.get("motdepasse") or ""
        if not DOMAIN_RE.match(domain):
            return self.send_json({"erreur": "Nom de domaine invalide."}, 400)
        if not EMAIL_RE.match(email):
            return self.send_json({"erreur": "Adresse e-mail invalide."}, 400)
        if len(pw) < 10:
            return self.send_json({"erreur": "Le mot de passe doit faire au moins 10 caractères."}, 400)
        if not re.match(r"^[A-Za-z0-9_.,;:!?@#%&*+=/()\[\]{}<>~^-]+$", pw):
            return self.send_json({"erreur": "Le mot de passe ne doit contenir ni espace, ni guillemet, ni $, ni accent."}, 400)
        update_state(domaine=domain, email=email, root_motdepasse=pw)
        stop_probes()

        def done(rc):
            if rc in (0, 3):
                update_state(installe=True, installe_le=time.time())
                refresh_banner()

        if not run_task("installation", [], {"PTF_DOMAINE": domain, "PTF_EMAIL": email, "PTF_ROOT_PASSWORD": pw}, done):
            return self.send_json({"erreur": "Une opération est déjà en cours."}, 409)
        return self.send_json({"ok": True})

    def action(self, data):
        act = data.get("action")
        args = {"mettre-a-jour": ["--mettre-a-jour"], "redemarrer": ["--redemarrer"],
                "certificat": ["--certificat"]}.get(act)
        if not args:
            return self.send_json({"erreur": "Action inconnue."}, 400)
        s = load_state()
        if not s.get("installe"):
            return self.send_json({"erreur": "PeerTube n'est pas encore installé."}, 400)
        if not run_task(act, args, {"PTF_DOMAINE": s.get("domaine", "")}):
            return self.send_json({"erreur": "Une opération est déjà en cours."}, 409)
        return self.send_json({"ok": True})

    def tableau(self):
        s = load_state()
        domain = s.get("domaine")
        pub = public_ip()
        a = doh(domain, "A") if domain else None
        du = shutil.disk_usage(PEERTUBE_DIR if os.path.isdir(PEERTUBE_DIR) else "/")
        return {
            "domaine": domain,
            "root_motdepasse": s.get("root_motdepasse"),
            "linux_motdepasse": read_text(LINUX_PW_FILE),
            "conteneurs": containers_status(),
            "certificat": certificate_ok(domain),
            "ip_publique": pub,
            "dns_a": a,
            "dns_a_jour": (a is None or pub is None or pub in a),
            "disque": {"total": du.total, "libre": du.free},
            "ddns_url": s.get("ddns_url"),
            "ddns_erreur": s.get("ddns_erreur"),
            "ip_locale": local_ip(),
        }


def ipv6_available():
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


HAS_V6 = ipv6_available()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET6 if HAS_V6 else socket.AF_INET

    def server_bind(self):
        if HAS_V6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    if "--code" in sys.argv:
        print(ensure_code())
        return
    ensure_code()
    if not load_state().get("installe"):
        start_probes()
    threading.Thread(target=ddns_loop, daemon=True).start()
    srv = Server(("::" if HAS_V6 else "0.0.0.0", PORT), Handler)
    print(f"Assistant PeerTube Facile sur le port {PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
