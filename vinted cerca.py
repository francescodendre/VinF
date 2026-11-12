#!/usr/bin/env python3
"""
Vinted Cerca
------------
Cerca un articolo su Vinted partendo dal prezzo più basso e tiene solo gli
annunci che sono davvero l'oggetto cercato. Scarta in automatico:
  - accessori (cinturini, cover, caricatori, scatole vuote...)
  - oggetti rotti, guasti o che non funzionano
  - falsi, repliche e imitazioni
Il risultato è una pagina HTML in ricerche/ (e un riepilogo su GitHub).

Esempi:
  python vinted_cerca.py "apple watch series 9"
  python vinted_cerca.py "apple watch series 9" --prezzo-min 80 --prezzo-max 300
  python vinted_cerca.py "iphone 13 pro" --escludi "max" --quanti 20

Le liste di parole stanno in filtri.json e si possono modificare liberamente.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlencode

try:  # curl_cffi si presenta a Vinted come un vero Chrome: viene bloccato molto meno
    from curl_cffi import requests as http
    CHROME_VERO = True
except ImportError:
    import requests as http
    CHROME_VERO = False

CARTELLA = Path(__file__).resolve().parent
FILE_FILTRI = CARTELLA / "filtri.json"
SITO = CARTELLA  # GitHub Pages pubblica la cartella principale del repository
RICERCHE = SITO / "ricerche"
SALUTO = "Ave Imperatore Francesco"  # la scritta in testa alle pagine
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ testo ---

def normalizza(testo) -> str:
    """Minuscole, senza accenti e apostrofi: «Non è rotto» -> «non e rotto»."""
    if not testo:
        return ""
    t = unicodedata.normalize("NFKD", str(testo))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[’‘'`´]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


FINE_FRASE = re.compile(
    r"[.,;:!?\n()\[\]|•–—]|\s-\s|(?<![a-z0-9])(?:ma|pero|tranne|but|except)(?![a-z0-9])")


def parole_prima(testo: str, pos: int, quante: int) -> list[str]:
    """Le ultime `quante` parole prima di `pos`, senza uscire dalla frase."""
    prima = testo[:pos]
    tagli = list(FINE_FRASE.finditer(prima))
    if tagli:
        prima = prima[tagli[-1].end():]
    return re.findall(r"[a-z0-9]+|[+&]", prima)[-quante:]


def regex_frase(frase: str) -> re.Pattern:
    parti = [re.escape(p) for p in normalizza(frase).split()]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-_/]+".join(parti) + r"(?![a-z0-9])")


def forma_parola(parola: str) -> str:
    """«45mm» trova anche «45 mm» e «45-mm»."""
    pezzi = re.findall(r"[a-z]+|[0-9]+", parola)
    return r"[\s\-]*".join(re.escape(p) for p in pezzi)


def euro(valore) -> str:
    if valore is None:
        return "–"
    s = f"{valore:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return (s[:-3] if s.endswith(",00") else s) + " €"


def importo(valore):
    if isinstance(valore, dict):
        valore = valore.get("amount")
    try:
        return float(str(valore).replace(",", "."))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------- annuncio ---

@dataclass
class Annuncio:
    id: int
    titolo: str
    prezzo: float | None
    prezzo_totale: float | None
    url: str
    foto: str | None
    stato: str
    marca: str
    preferiti: int = 0
    descrizione: str | None = None
    descrizione_letta: bool = False
    avvisi: list = field(default_factory=list)
    dubbio: str | None = None
    categoria: str = ""
    motivo: str = ""

    @classmethod
    def da_vinted(cls, it: dict, base: str) -> "Annuncio":
        foto = it.get("photo") or {}
        url_foto = foto.get("url") or foto.get("full_size_url")
        if not url_foto:
            for miniatura in foto.get("thumbnails") or []:
                url_foto = miniatura.get("url") or url_foto
        url = it.get("url") or it.get("path") or f"/items/{it.get('id')}"
        if url.startswith("/"):
            url = base + url
        stato = it.get("status") or ""
        if isinstance(stato, dict):
            stato = stato.get("title", "")
        return cls(
            id=it.get("id"),
            titolo=(it.get("title") or "").strip(),
            prezzo=importo(it.get("price")),
            prezzo_totale=importo(it.get("total_item_price")),
            url=url,
            foto=url_foto,
            stato=str(stato),
            marca=it.get("brand_title") or "",
            preferiti=it.get("favourite_count") or 0,
        )

    def aggiungi_avvisi(self, avvisi):
        for a in avvisi:
            if a not in self.avvisi:
                self.avvisi.append(a)

    def per_salvataggio(self) -> dict:
        d = asdict(self)
        if d["descrizione"]:
            d["descrizione"] = d["descrizione"][:600]
        return d


# ----------------------------------------------------------------- filtri ---

class Filtri:
    """Decide se un annuncio è davvero l'oggetto cercato."""

    def __init__(self, cfg: dict, ricerca: str, escludi: str = ""):
        self.cfg = cfg
        self.ricerca = normalizza(ricerca)
        vuote = set(cfg.get("parole_vuote", []))
        self.parole = [p for p in re.findall(r"[a-z0-9]+", self.ricerca) if p not in vuote]
        if not self.parole:
            raise ValueError("La ricerca non contiene parole utili.")
        nella_ricerca = set(self.parole)

        def utili(frasi):
            # una parola che fa parte della ricerca non può essere un motivo di scarto
            # (chi cerca «apple watch rotto» vuole proprio quelli rotti)
            return [(f.strip(), regex_frase(f)) for f in frasi
                    if normalizza(f) and not set(normalizza(f).split()) & nella_ricerca]

        self.regole = self._regole_ricerca(cfg.get("sinonimi", {}))
        self.accessori = utili(cfg["accessori_nel_titolo"])
        self.solo_accessorio = utili(cfg["solo_accessorio"])
        self.guasti = utili(cfg["guasti"])
        self.falsi = utili(cfg["falsi"])
        self.escludi = utili(re.split(r"[,;]", escludi or ""))
        self.negazioni = set(cfg["negazioni"])
        self.citazione = set(cfg["citazione_accessorio"])
        self.parole_accessorio = {normalizza(f) for f in cfg["accessori_nel_titolo"]
                                  if " " not in normalizza(f)} - nella_ricerca
        alfa = [p for p in self.parole if p.isalpha() and len(p) > 1]
        self.rx_per = self._regola_prima(cfg["prima_della_ricerca_accessorio"], alfa)
        self.rx_tipo = self._regola_prima(cfg["prima_della_ricerca_imitazione"], alfa)

    def _regole_ricerca(self, sinonimi: dict) -> list:
        regole, p, i = [], self.parole, 0
        while i < len(p):
            parola = p[i]
            dopo = p[i + 1] if i + 1 < len(p) else None
            if parola.isalpha() and dopo and dopo.isdigit() and len(dopo) <= 2:
                # «series 9»: il numero deve stare accanto alla parola (series 9, serie 9, s9)
                forme = sorted({parola, *sinonimi.get(parola, [])}, key=len, reverse=True)
                alt = "|".join(forma_parola(normalizza(f)) for f in forme)
                rx = re.compile(rf"(?<![a-z0-9])(?:{alt})[\s\-]*{dopo}(?![0-9])")
                regole.append((f"{parola} {dopo}", rx))
                i += 2
                continue
            if parola.isdigit():
                rx = re.compile(rf"(?<![0-9]){parola}(?![0-9])")  # referenze: 16610 trova 16610LV
            else:
                forme = sorted({parola, *[s for s in sinonimi.get(parola, []) if len(s) > 1]},
                               key=len, reverse=True)
                alt = "|".join(forma_parola(normalizza(f)) for f in forme)
                rx = re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")
            regole.append((parola, rx))
            i += 1
        return regole

    def _regola_prima(self, marcatori: list, alfa: list):
        """Regole tipo «per apple…» o «tipo apple…». Spente se la ricerca stessa le contiene."""
        if not alfa or not marcatori:
            return None
        m = "|".join(r"\s+".join(map(re.escape, normalizza(x).split()))
                     for x in sorted(marcatori, key=len, reverse=True))
        articolo = r"(?:(?:il|lo|la|l|i|gli|le|un|una|a|al|ad|all|allo|alla|the)\s+)?"
        parole = "|".join(re.escape(x) for x in alfa)
        rx = re.compile(rf"(?<![a-z0-9])(?:{m})\s+{articolo}(?:{parole})(?![a-z0-9])")
        return None if rx.search(self.ricerca) else rx

    # -- controlli di base

    def mancanti(self, testo: str) -> list[str]:
        return [nome for nome, rx in self.regole if not rx.search(testo)]

    def inizio_ricerca(self, titolo: str) -> int:
        inizi = [m.start() for _, rx in self.regole for m in [rx.search(titolo)] if m]
        return min(inizi) if inizi else len(titolo)

    @staticmethod
    def prima_frase(testo: str, frasi: list):
        for frase, rx in frasi:
            if rx.search(testo):
                return frase
        return None

    def accessorio_nel_titolo(self, titolo: str):
        """('certo' | 'dubbio', frase) oppure None."""
        if self.rx_per:
            m = self.rx_per.search(titolo)
            if m:
                return "certo", m.group(0)
        frase = self.prima_frase(titolo, self.solo_accessorio)
        if frase:
            return "certo", frase
        inizio = self.inizio_ricerca(titolo)
        dubbio = None
        for frase, rx in self.accessori:
            for m in rx.finditer(titolo):
                if m.start() < inizio:  # «Cinturino Apple Watch…»: l'oggetto è il cinturino
                    return "certo", frase
                if set(parole_prima(titolo, m.start(), 3)) & self.citazione:
                    continue  # «… con cinturino», «… + 2 cinturini», «… senza scatola»
                dubbio = dubbio or frase
        return ("dubbio", dubbio) if dubbio else None

    def difetto(self, testo: str):
        """Guasti e falsi non negati. Restituisce (categoria, frase, avvisi)."""
        avvisi = []
        for categoria, frasi in (("guasto", self.guasti), ("falso", self.falsi)):
            for frase, rx in frasi:
                for m in rx.finditer(testo):
                    prima = parole_prima(testo, m.start(), 4)
                    if set(prima) & self.negazioni:
                        continue  # «mai rotto», «nessun difetto», «non è un falso»
                    soggetto = [p for p in prima[-2:] if p in self.parole_accessorio]
                    if soggetto:  # «cinturino rotto»: il difetto riguarda l'accessorio
                        avvisi.append(f"Difetto su un accessorio ({soggetto[-1]}): verifica")
                        continue
                    return categoria, frase, avvisi
        return None, None, avvisi

    # -- verdetti

    def valuta_titolo(self, a: Annuncio):
        """None se l'annuncio passa, altrimenti (categoria, motivo)."""
        titolo = normalizza(a.titolo)
        manca = self.mancanti(f"{titolo} | {normalizza(a.marca)}")
        if manca:
            return "ricerca", "nel titolo manca «" + "», «".join(manca) + "»"
        frase = self.prima_frase(titolo, self.escludi)
        if frase:
            return "escluso", f"contiene «{frase}»"
        if self.rx_tipo:
            m = self.rx_tipo.search(titolo)
            if m:
                return "falso", f"nel titolo: «{m.group(0)}»"
        acc = self.accessorio_nel_titolo(titolo)
        if acc and acc[0] == "certo":
            return "accessorio", f"nel titolo: «{acc[1]}»"
        categoria, frase, avvisi = self.difetto(titolo)
        if categoria:
            return categoria, f"nel titolo: «{frase}»"
        a.aggiungi_avvisi(avvisi)
        if acc:
            a.dubbio = acc[1]
        return None

    def valuta_descrizione(self, a: Annuncio):
        testo = normalizza(a.descrizione)
        if not testo:
            return None
        frase = self.prima_frase(testo, self.solo_accessorio)
        if frase:
            return "accessorio", f"nella descrizione: «{frase}»"
        categoria, frase, avvisi = self.difetto(testo)
        if categoria:
            return categoria, f"nella descrizione: «{frase}»"
        a.aggiungi_avvisi(avvisi)
        return None


# ----------------------------------------------------------------- vinted ---

class Bloccato(Exception):
    pass


class ClientVinted:
    def __init__(self, dominio: str = "vinted.it", pausa: float = 1.2):
        dominio = re.sub(r"^https?://", "", dominio.strip()).replace("www.", "").strip("/")
        self.base = "https://www." + dominio
        self.pausa = pausa
        if CHROME_VERO:
            self.s = http.Session(impersonate="chrome")
        else:
            self.s = http.Session()
            self.s.headers["User-Agent"] = USER_AGENT
        self.s.headers["Accept-Language"] = "it-IT,it;q=0.9,en;q=0.8"
        self.pronto = False
        self.api_annuncio = None  # None = da provare, True/False = funziona o no

    def attendi(self, fattore: float = 1.0):
        time.sleep(self.pausa * fattore * random.uniform(1.0, 1.7))

    def _apri_sessione(self):
        """La prima visita alla home dà i cookie che servono per le ricerche."""
        r = self.s.get(self.base + "/", timeout=30,
                       headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        if r.status_code in (403, 429):
            raise Bloccato(f"Vinted ha rifiutato l'accesso (errore {r.status_code}).")
        self.pronto = True

    def _get(self, indirizzo: str, json_atteso: bool = True, tentativi: int = 3):
        url = indirizzo if indirizzo.startswith("http") else self.base + indirizzo
        accept = ("application/json, text/plain, */*" if json_atteso
                  else "text/html,application/xhtml+xml,*/*;q=0.8")
        r = None
        for n in range(tentativi):
            if not self.pronto:
                self._apri_sessione()
            r = self.s.get(url, headers={"Accept": accept}, timeout=30)
            if r.status_code in (200, 404):
                return r
            if r.status_code == 401:  # sessione scaduta: la rinnovo
                self.s.cookies.clear()
                self.pronto = False
                continue
            if r.status_code == 429:  # troppe richieste: rallento
                time.sleep(5 * (n + 1))
                continue
            if r.status_code == 403:
                raise Bloccato("Vinted ha rifiutato la richiesta (errore 403).")
            time.sleep(2 * (n + 1))
        return r

    def cerca(self, ricerca: str, pagina: int, prezzo_min=None, prezzo_max=None):
        parametri = [("search_text", ricerca), ("order", "price_low_to_high"),
                     ("page", pagina), ("per_page", 96)]
        if prezzo_min is not None:
            parametri.append(("price_from", f"{prezzo_min:g}"))
        if prezzo_max is not None:
            parametri.append(("price_to", f"{prezzo_max:g}"))
        r = self._get("/api/v2/catalog/items?" + urlencode(parametri))
        if r is None or r.status_code != 200:
            raise RuntimeError(f"Vinted ha risposto con l'errore {getattr(r, 'status_code', '?')}.")
        dati = r.json()
        return dati.get("items") or [], dati.get("pagination") or {}

    def descrizione(self, a: Annuncio):
        """Testo della descrizione, oppure None se non si riesce a leggerlo."""
        if self.api_annuncio is not False:
            try:
                r = self._get(f"/api/v2/items/{a.id}", tentativi=1)
                testo = (r.json().get("item") or {}).get("description") \
                    if r is not None and r.status_code == 200 else None
            except (Bloccato, ValueError, AttributeError):
                testo = None
            if isinstance(testo, str):
                self.api_annuncio = True
                return testo
            if self.api_annuncio is None:
                self.api_annuncio = False
        r = self._get(a.url, json_atteso=False, tentativi=2)
        if r is not None and r.status_code == 200:
            return estrai_descrizione(r.text)
        return None


def estrai_descrizione(pagina: str):
    """Legge la descrizione dalla pagina dell'annuncio (dati strutturati o og:description)."""
    candidati = []
    for blocco in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
                             pagina, re.S | re.I):
        try:
            dati = json.loads(blocco.strip())
        except ValueError:
            continue
        nodi = dati if isinstance(dati, list) else [dati]
        for nodo in list(nodi):
            if isinstance(nodo, dict) and isinstance(nodo.get("@graph"), list):
                nodi.extend(nodo["@graph"])
        for nodo in nodi:
            if isinstance(nodo, dict) and isinstance(nodo.get("description"), str):
                if nodo["description"].strip():
                    candidati.append(html.unescape(nodo["description"]))
    if not candidati:
        for tag in re.findall(r"<meta\b[^>]*>", pagina, re.I):
            attr = {k.lower(): html.unescape(v)
                    for k, _, v in re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S)}
            if attr.get("property") == "og:description" and attr.get("content"):
                candidati.append(attr["content"])
    return max(candidati, key=len) if candidati else None


# ---------------------------------------------------------------- ricerca ---

def esegui(client: ClientVinted, filtri: Filtri, opz, cfg: dict, log=print) -> dict:
    visti, validi, dubbi, scartati = set(), [], [], []
    analizzati, fallimenti = 0, 0
    descrizioni_attive = not opz.senza_descrizioni
    totale_vinted = None

    for pagina in range(1, opz.pagine + 1):
        try:
            elementi, paginazione = client.cerca(opz.ricerca, pagina, opz.prezzo_min, opz.prezzo_max)
        except Bloccato:
            if pagina == 1:
                raise
            log("Vinted ha fermato le pagine successive: uso gli annunci già trovati.")
            break
        if pagina == 1:
            totale_vinted = paginazione.get("total_entries")
        if not elementi:
            break
        log(f"Pagina {pagina}: {len(elementi)} annunci")

        for elemento in elementi:
            a = Annuncio.da_vinted(elemento, client.base)
            if a.id in visti:
                continue
            visti.add(a.id)
            analizzati += 1

            esito = filtri.valuta_titolo(a)
            if esito:
                a.categoria, a.motivo = esito
                scartati.append(a)
                continue

            if descrizioni_attive:
                try:
                    testo = client.descrizione(a)
                except Exception:  # blocco o errore di rete su un singolo annuncio
                    testo = None
                if testo is None:
                    fallimenti += 1
                    if fallimenti >= 4:
                        descrizioni_attive = False
                        log("Non riesco a leggere le descrizioni: da qui controllo solo i titoli.")
                else:
                    fallimenti = 0
                    a.descrizione, a.descrizione_letta = testo, True
                client.attendi()

            if a.descrizione_letta:
                esito = filtri.valuta_descrizione(a)
                if esito:
                    a.categoria, a.motivo = esito
                    scartati.append(a)
                    continue
            else:
                a.aggiungi_avvisi(["Descrizione non controllata"])

            (dubbi if a.dubbio else validi).append(a)
            if len(validi) >= opz.quanti:
                break

        if len(validi) >= opz.quanti:
            break
        pagine_totali = paginazione.get("total_pages")
        if pagine_totali and pagina >= pagine_totali:
            break
        client.attendi()

    mediana = risolvi_dubbi(validi, dubbi, scartati, cfg)
    creata = adesso()
    return {
        "ricerca": opz.ricerca,
        "dominio": client.base,
        "creata": creata.isoformat(timespec="minutes"),
        "prezzo_min": opz.prezzo_min,
        "prezzo_max": opz.prezzo_max,
        "escludi": opz.escludi,
        "analizzati": analizzati,
        "totale_vinted": totale_vinted,
        "mediana": mediana,
        "validi": [a.per_salvataggio() for a in validi],
        "scartati": [a.per_salvataggio() for a in scartati],
    }


def risolvi_dubbi(validi: list, dubbi: list, scartati: list, cfg: dict):
    """Titoli ambigui («Apple Watch Series 9 cinturino milanese»): decide il prezzo."""
    prezzi = [a.prezzo for a in validi if a.prezzo is not None]
    mediana = statistics.median(prezzi) if len(prezzi) >= 4 else None
    soglia_accessorio = cfg.get("soglia_prezzo_accessorio", 0.4)
    soglia_sospetto = cfg.get("soglia_prezzo_sospetto", 0.5)
    for a in dubbi:
        if mediana and a.prezzo is not None and a.prezzo < soglia_accessorio * mediana:
            a.categoria = "accessorio"
            a.motivo = (f"il titolo cita «{a.dubbio}» e costa {euro(a.prezzo)}, "
                        f"molto meno degli altri (mediana {euro(mediana)})")
            scartati.append(a)
        else:
            a.avvisi.insert(0, f"Il titolo cita «{a.dubbio}»: controlla che sia l'oggetto")
            validi.append(a)
    if mediana:
        for a in validi:
            if a.prezzo is not None and a.prezzo < soglia_sospetto * mediana:
                a.avvisi.insert(0, "Prezzo molto più basso degli altri: verifica bene")
    validi.sort(key=lambda a: (a.prezzo if a.prezzo is not None else float("inf"),
                               a.prezzo_totale or 0))
    return mediana


def adesso() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        return dt.datetime.now().astimezone()


def data_leggibile(iso: str) -> str:
    try:
        d = dt.datetime.fromisoformat(iso)
        return d.strftime("%d/%m/%Y alle %H:%M")
    except (TypeError, ValueError):
        return iso or ""


# ------------------------------------------------------------------- html ---

CSS = """
:root{
  --pietra:#E3DCCD;--inchiostro:#25211C;--tenue:#5F5648;--linea:#C3B8A2;
  --minio:#9E2A1E;--bronzo:#86642A;--bronzo-testo:#FCF3DC;--avviso-fondo:#EEDDCB;
  --foto:#D2C8B5;--capitali:"Cinzel","Trajan Pro","Times New Roman",serif;
  --testo:"EB Garamond",Garamond,Georgia,serif;color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --pietra:#1C1A17;--inchiostro:#E8E0D1;--tenue:#A89E8C;--linea:#3A342C;
  --minio:#E5866F;--bronzo:#B48E48;--bronzo-testo:#1B1305;--avviso-fondo:#3B231C;
  --foto:#2A2621;color-scheme:dark;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--pietra);color:var(--inchiostro);font-family:var(--testo);
  font-size:1.09rem;line-height:1.5;
  padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px)
          env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)}
main{max-width:42rem;margin:0 auto;padding:1.6rem 1.15rem 3rem}
a{color:var(--minio)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--minio);outline-offset:3px}
.indietro{display:inline-block;margin-bottom:1.6rem;font-family:var(--capitali);font-size:.82rem;
  letter-spacing:.08em;text-decoration:none}
h1{margin:0 0 1rem;font-family:var(--capitali);font-weight:600;font-size:clamp(1.65rem,7vw,2.55rem);
  line-height:1.18;letter-spacing:.07em;text-transform:uppercase;overflow-wrap:anywhere}
.punto{display:inline-block;width:.24em;height:.24em;background:var(--minio);
  clip-path:polygon(0 0,100% 0,50% 100%);vertical-align:.3em}
.sintesi{margin:0 0 .25rem;font-size:1.15rem;font-weight:600}
.dettagli{margin:0;color:var(--tenue);font-size:.98rem;max-width:36rem}
.elenco{list-style:none;margin:1.9rem 0 0;padding:0;border-top:1px solid var(--linea)}
.elenco>li{border-bottom:1px solid var(--linea)}
.annuncio{display:grid;grid-template-columns:5.6rem 1fr;gap:1rem;padding:1.1rem 0;
  color:inherit;text-decoration:none}
.annuncio img,.senza-foto{display:block;width:5.6rem;height:7.4rem;object-fit:cover;
  border-radius:2px;background:var(--foto)}
.corpo{min-width:0}
.omaggio{margin:0 0 1.7rem;text-align:center}
.fregio{display:block;width:100%;max-width:21rem;height:auto;margin:0 auto}
.risultati .fregio{max-width:15rem}
.figure{fill:var(--bronzo)}
.braccio{fill:none;stroke:var(--bronzo);stroke-width:5.2;stroke-linecap:round;stroke-linejoin:round}
.piume,.pieghe{fill:none;stroke:var(--pietra);stroke-linecap:round;opacity:.75}
.piume{stroke-width:1}
.pieghe{stroke-width:1.1}
.alloro{fill:var(--bronzo);color:var(--bronzo)}
.nastro{stroke:var(--minio)}
.suolo{stroke:var(--linea);stroke-width:1}
.saluto{margin:.55rem 0 0;font-family:var(--capitali);font-weight:600;
  font-size:clamp(1rem,4.6vw,1.3rem);line-height:1.3;letter-spacing:.12em;text-transform:uppercase}
.testa{display:flex;align-items:center;gap:.6rem}
.ordine{min-width:2.7rem;text-align:center;font-family:var(--capitali);font-size:.82rem;font-weight:700;
  letter-spacing:.1em;color:var(--minio)}
.ordine-corona{display:block;width:2.7rem;height:2.7rem;flex:none}
.ordine-corona text{fill:var(--minio);font-family:var(--capitali);font-weight:700;font-size:13px}
.tabula{position:relative;display:inline-block;padding:.46rem 1.45rem .36rem;background:var(--bronzo);
  color:var(--bronzo-testo);font-family:var(--capitali);font-size:1.4rem;font-weight:700;
  line-height:1;letter-spacing:.03em;white-space:nowrap;
  clip-path:polygon(.7rem 0,calc(100% - .7rem) 0,calc(100% - .7rem) 43%,100% 22%,100% 78%,
    calc(100% - .7rem) 57%,calc(100% - .7rem) 100%,.7rem 100%,.7rem 57%,0 78%,0 22%,.7rem 43%)}
.tabula::after{content:"";position:absolute;top:.2rem;bottom:.2rem;left:.92rem;right:.92rem;
  border:1px solid currentColor;opacity:.4}
.titolo{display:block;margin:.55rem 0 .1rem;font-weight:600;font-size:1.12rem;line-height:1.3;
  overflow-wrap:anywhere}
.annuncio:hover .titolo{text-decoration:underline;text-decoration-color:var(--minio)}
.meta{display:block;color:var(--tenue);font-size:.95rem}
.avvisi{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.5rem}
.avviso{padding:.1rem .5rem;border-radius:2px;background:var(--avviso-fondo);color:var(--minio);
  font-size:.9rem;line-height:1.35}
.vuoto{margin-top:2rem;padding:1.1rem 1.25rem;border:4px double var(--linea)}
.vuoto p{margin:.2rem 0}
details.scartati{margin-top:2.6rem}
details.scartati>summary{cursor:pointer;padding:.6rem 0;font-family:var(--capitali);
  font-weight:600;font-size:.95rem;letter-spacing:.05em}
.gruppo h2{margin:1.4rem 0 .3rem;font-family:var(--capitali);font-size:.95rem;font-weight:700;
  letter-spacing:.07em;color:var(--minio)}
.gruppo ul{list-style:none;margin:0;padding:0}
.gruppo li{padding:.5rem 0;border-bottom:1px solid var(--linea);font-size:1rem}
.gruppo li .perche{display:block;color:var(--tenue);font-size:.92rem;font-style:italic}
.gruppo .prezzo{white-space:nowrap}
.ricerche{list-style:none;margin:1.9rem 0 0;padding:0;border-top:1px solid var(--linea)}
.ricerche li{border-bottom:1px solid var(--linea)}
.ricerche a{display:flex;align-items:center;gap:1rem;padding:1.05rem 0;color:inherit;text-decoration:none}
.ricerche .nome{display:block;font-family:var(--capitali);font-weight:600;font-size:1.05rem;
  letter-spacing:.05em;text-transform:uppercase}
.ricerche a:hover .nome{text-decoration:underline;text-decoration-color:var(--minio)}
.ricerche .tabula{font-size:1.15rem;flex:none}
footer{margin-top:3rem;color:var(--tenue);font-size:.95rem;font-style:italic}
footer p{margin:.25rem 0}
"""

FONT = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500..800'
        '&family=EB+Garamond:ital,wght@0,400..600;1,400&display=swap" rel="stylesheet">')

GRUPPI = [
    ("accessorio", "Accessori"),
    ("guasto", "Rotti, guasti o non funzionanti"),
    ("falso", "Possibili falsi o imitazioni"),
    ("escluso", "Con parole che hai escluso"),
    ("ricerca", "Non corrispondono alla ricerca"),
]


def corona(cx, cy, r, foglie=11, rx=4.6, ry=2.0, nastro=True):
    """Corona d'alloro: due rami che salgono dal basso e quasi si chiudono in alto."""
    parti = []
    for lato in (-1, 1):
        a0, a1 = 100, 258  # gradi, 90 = in basso
        for i in range(foglie):
            t = i / (foglie - 1)
            a = math.radians(a0 + (a1 - a0) * t)
            if lato > 0:
                a = math.pi - a
            px, py = cx + r * math.cos(a), cy + r * math.sin(a)
            tang = math.degrees(a) + (90 if lato < 0 else -90)
            for verso in (1, -1):
                ox = px + verso * 2.6 * math.cos(a)
                oy = py + verso * 2.6 * math.sin(a)
                rot = tang + verso * lato * -28
                parti.append(f'<ellipse cx="{ox:.1f}" cy="{oy:.1f}" rx="{rx}" ry="{ry}" transform="rotate({rot:.0f} {ox:.1f} {oy:.1f})"/>')
        # stelo
        pa = math.radians(a0) if lato < 0 else math.pi - math.radians(a0)
        pb = math.radians(a1) if lato < 0 else math.pi - math.radians(a1)
        x0, y0 = cx + r * math.cos(pa), cy + r * math.sin(pa)
        x1, y1 = cx + r * math.cos(pb), cy + r * math.sin(pb)
        sweep = 1 if lato < 0 else 0
        parti.append(f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 0 {sweep} {x1:.1f},{y1:.1f}" fill="none" stroke="currentColor" stroke-width="1.2"/>')
    if nastro:
        parti.append(f'<path class="nastro" d="M{cx-3},{cy+r-1} C{cx-8},{cy+r+6} {cx-12},{cy+r+9} {cx-15},{cy+r+16} M{cx+3},{cy+r-1} C{cx+8},{cy+r+6} {cx+12},{cy+r+9} {cx+15},{cy+r+16}" fill="none" stroke-width="2.2" stroke-linecap="round"/>')
    return "".join(parti)

VITTORIA = '''
<g class="vittoria">
  <path class="ala" d="M106,55 C96,31 72,13 34,6 C42,13 45,19 45,24 C51,24 55,28 57,33 C63,32 67,36 69,41 C75,41 79,45 81,50 C87,50 91,53 93,57 C97,57 100,58 102,61 Z"/>
  <path class="piume" d="M58,22 C70,28 82,37 94,49 M48,14 C64,20 80,31 98,45"/>
  <circle cx="112" cy="39.5" r="7.6"/>
  <path d="M119.2,37.2 L123.2,38.2 L119.8,41.2 Z"/>
  <circle cx="104.3" cy="34" r="4.6"/>
  <path d="M108.6,45 L114.6,45.5 L115,53.5 L108.4,53.5 Z"/>
  <path d="M120,54 C131,52 145,50 158,48 L158.5,52.6 C145,54.5 132,57.5 121,61.5 Z"/>
  <path class="braccio" d="M100.5,60 L88.5,71.5 L99,82"/>
  <path class="veste" d="M98,54.5 C105,52 117,52 124,54.5 C128.5,58 130,64 128,70.5 C127,73 125.5,74.5 124.5,76 C128,84 131.5,93 131,103 C130.5,117 131.5,131 135.5,143 C122,146.2 102,146.2 87.5,143 C89.5,129 89,115 89.5,103 C90,93 93,84 97.5,77 C96,70 95.5,61 98,54.5 Z"/>
  <path class="pieghe" d="M97.5,76.5 C106,74.8 116,74.8 124.5,76 M104,80 C101,100 97,122 94,142 M112,80 C112,102 112,124 111,144 M120,80 C123,100 125,122 128,143 M124,59 C125.5,63 125.5,67 124,70.5"/>
  <path d="M131,142 L141.5,143.4 L140.8,145.4 L130,145 Z"/>
</g>'''


FREGIO = ('<svg class="fregio" viewBox="0 0 360 150" role="img" '
          'aria-label="Due Vittorie alate reggono una corona d\'alloro">'
          f'<g class="figure">{VITTORIA}<g transform="translate(360 0) scale(-1 1)">{VITTORIA}</g></g>'
          f'<g class="alloro">{corona(180, 52, 20)}</g><path class="suolo" d="M30,146.5 H330"/></svg>')

CORONA_PRIMO = ('<svg class="ordine-corona" viewBox="0 0 40 40" aria-hidden="true">'
                f'<g class="alloro">{corona(20, 20, 13, foglie=7, rx=3.3, ry=1.45, nastro=False)}</g>'
                '<text x="20" y="24.6" text-anchor="middle">I</text></svg>')


def intestazione(saluto_come_titolo: bool) -> str:
    tag = "h1" if saluto_come_titolo else "p"
    return f'<header class="omaggio">{FREGIO}<{tag} class="saluto">{iscrizione(SALUTO)}</{tag}></header>\n'


def romano(n: int) -> str:
    valori = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
              (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    risultato = ""
    for valore, segno in valori:
        while n >= valore:
            risultato += segno
            n -= valore
    return risultato


def iscrizione(testo: str) -> str:
    """Parole separate dal punto triangolare delle epigrafi romane."""
    return ' <span class="punto" aria-hidden="true"></span> '.join(html.escape(p) for p in testo.split())


def documento(titolo: str, corpo: str, classe: str = "") -> str:
    return ("<!doctype html>\n<html lang=\"it\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<meta name=\"robots\" content=\"noindex\">\n"
            f"<title>{html.escape(titolo)}</title>\n{FONT}\n<style>{CSS}</style>\n</head>\n"
            f"<body class=\"{classe}\">\n<main>\n{corpo}\n</main>\n</body>\n</html>\n")


def frase_prezzo(r: dict) -> str:
    pmin, pmax = r.get("prezzo_min"), r.get("prezzo_max")
    if pmin is not None and pmax is not None:
        return f"Prezzo tra {euro(pmin)} e {euro(pmax)}."
    if pmin is not None:
        return f"Prezzo da {euro(pmin)} in su."
    if pmax is not None:
        return f"Prezzo fino a {euro(pmax)}."
    return ""


def html_risultati(r: dict) -> str:
    e = html.escape
    validi, scartati = r["validi"], r["scartati"]
    righe = []
    for posizione, a in enumerate(validi, 1):
        foto = (f'<img src="{e(a["foto"])}" alt="" loading="lazy" referrerpolicy="no-referrer">'
                if a.get("foto") else '<span class="senza-foto"></span>')
        meta = [a.get("stato") or None,
                f'{euro(a["prezzo_totale"])} con Protezione acquisti' if a.get("prezzo_totale") else None,
                f'{a["preferiti"]} nei preferiti' if a.get("preferiti") else None]
        avvisi = "".join(f'<span class="avviso">{e(x)}</span>' for x in a.get("avvisi") or [])
        righe.append(
            f'<li><a class="annuncio" href="{e(a["url"])}" target="_blank" rel="noopener">{foto}'
            f'<span class="corpo"><span class="testa">'
            + (CORONA_PRIMO if posizione == 1 else
               f'<span class="ordine" aria-hidden="true">{romano(posizione)}</span>')
            + f'<span class="tabula">{euro(a["prezzo"])}</span></span>'
            f'<span class="titolo">{e(a["titolo"])}</span>'
            f'<span class="meta">{e(", ".join(x for x in meta if x))}</span>'
            + (f'<span class="avvisi">{avvisi}</span>' if avvisi else "")
            + "</span></a></li>")

    n = len(validi)
    sintesi = ("I tuoi messaggeri non hanno trovato offerte degne di te." if n == 0 else
               "I tuoi messaggeri hanno trovato un'offerta degna di te." if n == 1 else
               f"I tuoi messaggeri hanno trovato {n} offerte degne di te, dalla più economica.")
    dettagli = " ".join(x for x in [
        frase_prezzo(r),
        f"Parole escluse: {r['escludi']}." if r.get("escludi") else "",
        f"Hanno esaminato {r['analizzati']} annunci e ne hanno respinti {len(scartati)}.",
        f"Aggiornato il {data_leggibile(r['creata'])}.",
    ] if x)

    if righe:
        elenco = '<ul class="elenco">' + "".join(righe) + "</ul>"
    else:
        elenco = ('<div class="vuoto"><p>Nessuna offerta ha superato l\'esame.</p>'
                  "<p>Alza il prezzo massimo, togli una parola dalla ricerca "
                  "o fai controllare più pagine.</p></div>")

    gruppi = []
    for chiave, nome in GRUPPI:
        lista = [a for a in scartati if a.get("categoria") == chiave]
        if not lista:
            continue
        voci = "".join(
            f'<li><a href="{e(a["url"])}" target="_blank" rel="noopener">{e(a["titolo"] or "Senza titolo")}</a> '
            f'<span class="prezzo">{euro(a["prezzo"])}</span>'
            f'<span class="perche">{e(a["motivo"])}</span></li>' for a in lista[:150])
        altri = f"<li>E altri {len(lista) - 150}.</li>" if len(lista) > 150 else ""
        gruppi.append(f'<div class="gruppo"><h2>{nome} ({len(lista)})</h2><ul>{voci}{altri}</ul></div>')
    blocco_scartati = (f'<details class="scartati"><summary>Respinti dall\'Imperatore ({len(scartati)}), '
                       f'con il motivo</summary>{"".join(gruppi)}</details>') if scartati else ""

    corpo = (f'<a class="indietro" href="../index.html">Tutte le ricerche</a>\n'
             f"{intestazione(False)}<h1>{iscrizione(r['ricerca'])}</h1>\n"
             f'<p class="sintesi">{sintesi}</p>\n<p class="dettagli">{e(dettagli)}</p>\n'
             f"{elenco}\n{blocco_scartati}\n"
             "<footer><p>Prezzi senza spedizione. Il prezzo grande è quello del venditore, "
             "sotto c'è il totale con la Protezione acquisti di Vinted.</p>"
             "<p>Le parole usate per scartare gli annunci sono nel file filtri.json.</p></footer>")
    return documento(f"{r['ricerca']}: Vinted", corpo, "risultati")


def html_indice(ricerche: list) -> str:
    e = html.escape
    voci = []
    for r in ricerche:
        validi = r.get("validi") or []
        prezzo = validi[0]["prezzo"] if validi else None
        dettaglio = (f"{len(validi)} annunci validi, aggiornata il {data_leggibile(r.get('creata'))}"
                     if validi else f"Nessun annuncio valido, {data_leggibile(r.get('creata'))}")
        tabula = f'<span class="tabula">{euro(prezzo)}</span>' if prezzo is not None else ""
        voci.append(f'<li><a href="ricerche/{e(r["_file"])}">{tabula}<span>'
                    f'<span class="nome">{e(r["ricerca"])}</span>'
                    f'<span class="meta">{e(dettaglio)}</span></span></a></li>')
    if voci:
        elenco = '<ul class="ricerche">' + "".join(voci) + "</ul>"
        sintesi = "Le ricerche dei tuoi messaggeri, dalla più recente. Sulla targa c'è il prezzo più basso trovato."
    else:
        elenco = ('<div class="vuoto"><p>Nessuna ricerca ancora.</p><p>Su GitHub apri Actions, '
                  "scegli Cerca su Vinted e premi Run workflow.</p></div>")
        sintesi = "Annunci Vinted dal più economico, senza accessori, oggetti rotti o falsi."
    corpo = (f"{intestazione(True)}<p class=\"dettagli\">{sintesi}</p>\n{elenco}\n"
             "<footer><p>Per una nuova ricerca: Actions, Cerca su Vinted, Run workflow. "
             "La pagina si aggiorna entro un paio di minuti.</p></footer>")
    return documento("Ricerche Vinted", corpo)


# ---------------------------------------------------------------- salvare ---

def nome_file(testo: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", normalizza(testo)).strip("-")[:60]) or "ricerca"


def aggiorna_indice():
    ricerche = []
    for f in RICERCHE.glob("*.json") if RICERCHE.exists() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        r["_file"] = f.stem + ".html"
        ricerche.append(r)
    ricerche.sort(key=lambda r: r.get("creata") or "", reverse=True)
    (SITO / "index.html").write_text(html_indice(ricerche), encoding="utf-8")
    (SITO / ".nojekyll").touch()


def salva(risultato: dict):
    RICERCHE.mkdir(parents=True, exist_ok=True)
    nome = nome_file(risultato["ricerca"])
    (RICERCHE / f"{nome}.json").write_text(
        json.dumps(risultato, ensure_ascii=False, indent=1), encoding="utf-8")
    pagina = RICERCHE / f"{nome}.html"
    pagina.write_text(html_risultati(risultato), encoding="utf-8")
    aggiorna_indice()
    return pagina, nome


def indirizzo_pages(nome: str):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    proprietario, progetto = repo.split("/", 1)
    return f"https://{proprietario.lower()}.github.io/{progetto}/ricerche/{nome}.html"


def riepilogo_github(testo: str):
    percorso = os.environ.get("GITHUB_STEP_SUMMARY")
    if percorso:
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(testo + "\n")


def riepilogo_risultati(r: dict, url):
    righe = [f"## {r['ricerca']}", "",
             f"{len(r['validi'])} annunci validi su {r['analizzati']} controllati. {frase_prezzo(r)}", ""]
    if url:
        righe += [f"[Apri la pagina completa]({url}) (pronta entro un paio di minuti)", ""]
    if r["validi"]:
        righe += ["| Prezzo | Annuncio | Condizioni |", "|---:|---|---|"]
        for a in r["validi"][:30]:
            titolo = (a["titolo"] or "").replace("|", "/").replace("[", "(").replace("]", ")")
            righe.append(f"| {euro(a['prezzo'])} | [{titolo}]({a['url']}) | {a.get('stato') or ''} |")
    return "\n".join(righe)


# ------------------------------------------------------------------- avvio ---

def numero(valore):
    v = (valore or "").strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"«{valore}» non è un numero")


def intero(predefinito):
    def leggi(valore):
        v = (str(valore) if valore is not None else "").strip()
        if not v:
            return predefinito
        try:
            return max(1, int(float(v)))
        except ValueError:
            raise argparse.ArgumentTypeError(f"«{valore}» non è un numero intero")
    return leggi


def leggi_opzioni(argv=None):
    p = argparse.ArgumentParser(
        description="Cerca su Vinted dal prezzo più basso, scartando accessori, oggetti rotti e falsi.")
    p.add_argument("ricerca", nargs="+", help="cosa cercare, es. apple watch series 9")
    p.add_argument("--prezzo-min", type=numero, default=None, metavar="EURO")
    p.add_argument("--prezzo-max", type=numero, default=None, metavar="EURO")
    p.add_argument("--quanti", type=intero(30), default=30,
                   help="quanti annunci validi trovare (predefinito 30)")
    p.add_argument("--pagine", type=intero(10), default=10,
                   help="pagine di Vinted da controllare al massimo, 96 annunci l'una (predefinito 10)")
    p.add_argument("--escludi", default="", help="parole da escludere nel titolo, separate da virgola")
    p.add_argument("--dominio", default="vinted.it", help="es. vinted.it, vinted.fr, vinted.es")
    p.add_argument("--pausa", type=float, default=1.2, help="secondi di pausa tra le richieste")
    p.add_argument("--senza-descrizioni", action="store_true",
                   help="controlla solo i titoli (più veloce, meno preciso)")
    p.add_argument("--non-aprire", action="store_true", help="non aprire il browser alla fine")
    opz = p.parse_args(argv)
    opz.ricerca = " ".join(opz.ricerca).strip()
    opz.escludi = (opz.escludi or "").strip()
    return opz


def main(argv=None) -> int:
    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    opz = leggi_opzioni(argv)
    cfg = json.loads(FILE_FILTRI.read_text(encoding="utf-8"))
    try:
        filtri = Filtri(cfg, opz.ricerca, opz.escludi)
    except ValueError as ex:
        print(ex)
        return 1

    client = ClientVinted(opz.dominio, opz.pausa)
    if not CHROME_VERO:
        print("Nota: manca curl_cffi, Vinted potrebbe bloccare le richieste. "
              "Installa i requisiti con: pip install -r requirements.txt")
    print(f"Cerco «{opz.ricerca}» su {client.base}, dal prezzo più basso…")

    try:
        risultato = esegui(client, filtri, opz, cfg)
    except Bloccato as ex:
        messaggio = (f"{ex} Vinted a volte blocca i server di GitHub: in quel caso avvia la ricerca "
                     f"dal PC con cerca.bat (oppure: python vinted_cerca.py \"{opz.ricerca}\").")
        print(messaggio)
        riepilogo_github(f"## Ricerca non riuscita\n\n{messaggio}")
        return 2
    except Exception as ex:  # rete assente, risposta inattesa…
        messaggio = f"La ricerca si è interrotta: {ex}"
        print(messaggio)
        riepilogo_github(f"## Ricerca non riuscita\n\n{messaggio}")
        return 3

    pagina, nome = salva(risultato)
    url = indirizzo_pages(nome)
    print(f"\n{len(risultato['validi'])} annunci validi su {risultato['analizzati']} controllati.")
    for a in risultato["validi"][:10]:
        print(f"  {euro(a['prezzo']):>10}  {a['titolo']}")
    print(f"\nPagina dei risultati: {url or pagina}")
    riepilogo_github(riepilogo_risultati(risultato, url))
    if not opz.non_aprire and not os.environ.get("CI"):
        webbrowser.open(pagina.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
