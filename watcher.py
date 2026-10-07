#!/usr/bin/env python3
"""
Vigila reportes nuevos de operaciones bursatiles y avisa por Telegram.

Fuentes:
  - Camara de Representantes (Pelosi y quien agregues): sitio del Clerk of the House.
    Se consultan dos vias y se unen: el buscador en vivo y el indice anual (ZIP/XML).
  - Trump: indice de reportes presidenciales de la Oficina de Etica Gubernamental (OGE).

Uso:
  python3 watcher.py            una sola revision (para GitHub Actions o cron)
  python3 watcher.py --loop 60  revisa cada 60 segundos sin parar (para una PC encendida)
  python3 watcher.py --test     manda un mensaje de prueba a Telegram y sale

Variables de entorno:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   obligatorias
  WATCH_HOUSE   "Apellido:Nombre" separados por coma. Por defecto "Pelosi:Nancy"
  WATCH_OGE     palabra a buscar en el indice de OGE. Por defecto "Trump". Vacio = desactivar
  OGE_INDEX_URL por defecto el indice PAS+Index de extapps2.oge.gov
  STATE_FILE    por defecto state.json
Solo usa la libreria estandar de Python 3.8+.
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HOUSE = "https://disclosures-clerk.house.gov"
OGE_BASE = "https://extapps2.oge.gov"
OGE_INDEX_URL = os.environ.get("OGE_INDEX_URL", OGE_BASE + "/201/Presiden.nsf/PAS+Index?OpenView&Count=3000")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
WATCH_HOUSE = [p.strip().split(":") for p in os.environ.get("WATCH_HOUSE", "Pelosi:Nancy").split(",") if p.strip()]
WATCH_OGE = os.environ.get("WATCH_OGE", "Trump").strip()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
FAILS_BEFORE_WARNING = 12


def fetch(url, data=None, timeout=40):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[sin Telegram configurado]\n" + text)
        return
    fetch("https://api.telegram.org/bot%s/sendMessage" % token,
          {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"})


def years():
    now = dt.datetime.utcnow()
    return [now.year - 1, now.year] if now.month == 1 else [now.year]


# ---------- Camara de Representantes ----------

def parse_house_search(page, first):
    """Filas del buscador en vivo. Devuelve {doc_id: info} solo de reportes de operaciones (PTR)."""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I):
        m = re.search(r'href="([^"]*ptr-pdfs/(\d{4})/(\d+)\.pdf)"[^>]*>(.*?)</a>', row, flags=re.S | re.I)
        if not m:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", " ", m.group(4))).strip()
        if first.lower() not in name.lower():
            continue
        out[m.group(3)] = {"name": re.sub(r"\s+", " ", name), "date": "",
                           "url": "%s/public_disc/ptr-pdfs/%s/%s.pdf" % (HOUSE, m.group(2), m.group(3))}
    return out


def house_search(last, first, year):
    page = fetch(HOUSE + "/FinancialDisclosure/ViewMemberSearchResult",
                 {"LastName": last, "FilingYear": str(year), "State": "", "District": ""}).decode("utf-8", "replace")
    return parse_house_search(page, first)


def parse_house_xml(xml_bytes, wanted, year):
    """Indice anual. wanted = [(apellido, nombre)]. Devuelve {doc_id: info} de tipo P (PTR)."""
    out = {}
    for m in ET.fromstring(xml_bytes).iter("Member"):
        g = lambda k: (m.findtext(k) or "").strip()
        if g("FilingType") != "P" or not g("DocID"):
            continue
        for last, first in wanted:
            if g("Last").lower() == last.lower() and first.lower() in g("First").lower():
                out[g("DocID")] = {"name": "%s %s" % (g("First"), g("Last")), "date": g("FilingDate"),
                                   "url": "%s/public_disc/ptr-pdfs/%s/%s.pdf" % (HOUSE, year, g("DocID"))}
    return out


def house_zip(wanted, year):
    raw = fetch("%s/public_disc/financial-pdfs/%sFD.zip" % (HOUSE, year), timeout=90)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        return parse_house_xml(z.read(name), wanted, year)


def check_house():
    found, ok, errors = {}, False, []
    for year in years():
        try:
            found.update(house_zip(WATCH_HOUSE, year)); ok = True
        except Exception as e:
            errors.append("indice %s: %s" % (year, e))
        for last, first in WATCH_HOUSE:
            try:
                for k, v in house_search(last, first, year).items():
                    found.setdefault(k, v)
                ok = True
            except Exception as e:
                errors.append("buscador %s %s: %s" % (last, year, e))
    return found, ok, errors


# ---------- OGE (Trump) ----------

def parse_oge(page, word):
    """Enlaces del indice de OGE cuya fila menciona la palabra buscada. Devuelve {clave: info}."""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I) or [page]:
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", row))).strip()
        for href in re.findall(r'href="([^"]+)"', row, flags=re.I):
            href = html.unescape(href)
            if word.lower() not in (text + " " + urllib.parse.unquote(href)).lower():
                continue
            if "opendocument" not in href.lower() and "$file" not in href.lower():
                continue
            url = urllib.parse.urljoin(OGE_BASE + "/201/Presiden.nsf/", href)
            m = re.search(r"/([0-9A-Fa-f]{32})", url)
            key = m.group(1).upper() if m else url
            out[key] = {"name": text[:160] or word, "date": "", "url": url}
    return out


def check_oge():
    page = fetch(OGE_INDEX_URL, timeout=90).decode("utf-8", "replace")
    return parse_oge(page, WATCH_OGE)


# ---------- Estado y ciclo ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def is_trade_report(info):
    t = (info["name"] + " " + urllib.parse.unquote(info["url"])).lower().replace(" ", "").replace("-", "")
    return "278t" in t or "transaction" in t


def run_once():
    state = load_state()
    first_run = "house" not in state
    state.setdefault("house", []); state.setdefault("oge", []); state.setdefault("fails", {})
    now = dt.datetime.utcnow()
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")

    def track_failure(key, label, detail):
        n = state["fails"].get(key, 0) + 1
        state["fails"][key] = n
        print("FALLO %s (%d seguidos): %s" % (label, n, detail))
        if n == FAILS_BEFORE_WARNING:
            telegram("Aviso: llevo %d revisiones seguidas sin poder leer %s. Puede que el sitio haya cambiado.\n%s"
                     % (n, label, str(detail)[:500]))

    # Camara
    found, ok, errors = check_house()
    if ok:
        state["fails"]["house"] = 0
        new = [k for k in found if k not in state["house"]]
        if not first_run:
            for k in sorted(new):
                v = found[k]
                telegram("NUEVO REPORTE DE OPERACIONES\n%s%s\nDetectado: %s\n%s"
                         % (v["name"], (" (presentado %s)" % v["date"]) if v["date"] else "", stamp, v["url"]))
        state["house"] = sorted(set(state["house"]) | set(found))
        print("Camara: %d reportes conocidos, %d nuevos" % (len(found), 0 if first_run else len(new)))
    else:
        track_failure("house", "el sitio de la Camara", "; ".join(errors))

    # OGE
    oge_count = None
    if WATCH_OGE:
        try:
            found_oge = check_oge()
            oge_count = len(found_oge)
            if not found_oge:
                raise RuntimeError("el indice no devolvio ninguna fila con '%s'" % WATCH_OGE)
            state["fails"]["oge"] = 0
            new = [k for k in found_oge if k not in state["oge"]]
            if not first_run and state["oge"]:
                for k in sorted(new):
                    v = found_oge[k]
                    kind = "NUEVO REPORTE DE OPERACIONES (278-T)" if is_trade_report(v) else "Nuevo documento en OGE"
                    telegram("%s\n%s\nDetectado: %s\n%s" % (kind, v["name"], stamp, v["url"]))
            state["oge"] = sorted(set(state["oge"]) | set(found_oge))
            print("OGE: %d documentos conocidos" % len(found_oge))
        except Exception as e:
            track_failure("oge", "el indice de OGE (%s)" % WATCH_OGE, e)

    if first_run:
        names = ", ".join("%s %s" % (f, l) for l, f in WATCH_HOUSE)
        telegram("Vigilancia activa.\nCamara: %s (%s reportes ya publicados este periodo; no se avisan).\nOGE %s: %s\n"
                 "A partir de ahora te aviso solo de lo nuevo."
                 % (names, len(state["house"]) if ok else "no pude leer el sitio",
                    WATCH_OGE or "desactivado",
                    ("%d documentos ya publicados" % oge_count) if oge_count else "no pude leer el indice, revisa OGE_INDEX_URL"))

    state["heartbeat"] = "%d-W%02d" % now.isocalendar()[:2]   # cambia cada semana: mantiene activo el repositorio
    save_state(state)


def main():
    if "--test" in sys.argv:
        telegram("Prueba: el bot de alertas de trades esta conectado.")
        return
    if "--loop" in sys.argv:
        every = max(30, int(sys.argv[sys.argv.index("--loop") + 1]))
        while True:
            try:
                run_once()
            except Exception as e:
                print("Error en la revision:", e)
            time.sleep(every)
    run_once()


if __name__ == "__main__":
    main()
