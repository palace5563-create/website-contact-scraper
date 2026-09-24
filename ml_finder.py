#!/usr/bin/env python3
"""
ml_finder - Descobre perfis de vendedor do Mercado Livre vinculados a sites.

Dado uma lista de URLs de lojas "proprias" (fora de marketplace), o script
visita a pagina inicial e algumas paginas internas relevantes (contato, sobre,
onde comprar...) e procura por links/mencoes ao Mercado Livre:

  * perfil de vendedor      perfil.mercadolivre.com.br/NICK
  * loja oficial            loja.mercadolivre.com.br/NOME  |  mercadolivre.com.br/loja/NOME
  * pagina da marca         mercadolivre.com.br/pagina/NOME
  * listagem do vendedor    lista.mercadolivre.com.br/_CustId_123
  * anuncio/produto         produto.mercadolivre.com.br/MLB-123...  |  .../p/MLB123
  * link curto              mercadolivre.com/sec/XXXX  (resolvido com --resolve)
  * mencao em texto         "Mercado Livre" sem link (sinal fraco)

Uso:
    python ml_finder.py https://loja1.com.br https://loja2.com.br
    python ml_finder.py -f urls.txt --csv resultado.csv
    python ml_finder.py -f urls.txt --json resultado.json --resolve
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Palavras que indicam paginas internas onde costuma haver link para marketplace.
INTERNAL_KEYWORDS = (
    "contato", "contact", "fale-conosco", "faleconosco", "sobre", "about",
    "quem-somos", "quemsomos", "institucional", "onde-comprar", "ondecomprar",
    "revendedor", "marketplace", "mercado-livre", "mercadolivre", "lojas",
    "atendimento", "sac",
)

# Qualquer dominio do Mercado Livre / Mercado Libre.
ML_URL_RE = re.compile(
    r"""(?:https?:)?//(?:[a-z0-9-]+\.)*mercado(?:livre|libre)\.com(?:\.[a-z]{2})?"""
    r"""(?:/[^\s"'<>()\\]*)?""",
    re.IGNORECASE,
)
ML_MENTION_RE = re.compile(r"mercado\s*(?:livre|libre)", re.IGNORECASE)

# Ordem importa: do mais especifico para o mais generico.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("perfil", re.compile(r"//perfil\.mercado(?:livre|libre)\.[a-z.]+/([^/?#]+)", re.I)),
    ("perfil", re.compile(r"mercado(?:livre|libre)\.[a-z.]+/perfil/([^/?#]+)", re.I)),
    ("loja_oficial", re.compile(r"//loja\.mercado(?:livre|libre)\.[a-z.]+/([^/?#]+)", re.I)),
    ("loja_oficial", re.compile(r"mercado(?:livre|libre)\.[a-z.]+/loja/([^/?#]+)", re.I)),
    ("pagina", re.compile(r"mercado(?:livre|libre)\.[a-z.]+/pagina/([^/?#]+)", re.I)),
    ("listagem_vendedor", re.compile(r"_CustId_(\d+)", re.I)),
    ("listagem_vendedor", re.compile(r"[?&]seller_id=(\d+)", re.I)),
    ("anuncio", re.compile(r"/(ML[A-Z]-?\d{6,})", re.I)),
    ("link_curto", re.compile(r"mercado(?:livre|libre)\.com(?:\.[a-z]{2})?/sec/([A-Za-z0-9]+)", re.I)),
]

# Links genericos que nao dizem nada sobre o vendedor.
IGNORED_PATHS = re.compile(
    r"^/?(?:$|ajuda|help|privacidade|privacy|termos|terms|blog|developers|"
    r"navigation|jms|gz|org-img|static|favicon)",
    re.I,
)

STRONG = {"perfil", "loja_oficial", "pagina", "listagem_vendedor"}


@dataclass
class Finding:
    tipo: str
    identificador: str
    url: str
    encontrado_em: str
    vendedor: str = ""  # preenchido quando conseguimos resolver anuncio/link curto


@dataclass
class SiteResult:
    site: str
    status: str = "ok"
    confianca: str = "nenhuma"
    paginas_analisadas: list[str] = field(default_factory=list)
    achados: list[Finding] = field(default_factory=list)
    mencao_texto: bool = False
    erro: str = ""

    def perfis(self) -> list[str]:
        """Identificadores unicos de vendedor/loja encontrados."""
        seen: dict[str, None] = {}
        for f in self.achados:
            if f.tipo in STRONG:
                seen[f"{f.tipo}:{f.identificador}"] = None
            elif f.vendedor:
                seen[f"perfil:{f.vendedor}"] = None
        return list(seen)


class _LinkParser(HTMLParser):
    """Coleta hrefs e texto de ancoras de uma pagina."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None


def normalize_input_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def classify_ml_url(url: str) -> tuple[str, str] | None:
    """Retorna (tipo, identificador) para uma URL do Mercado Livre, ou None."""
    url = unquote(unescape(url))
    if url.startswith("//"):
        url = "https:" + url
    for tipo, pattern in PATTERNS:
        m = pattern.search(url)
        if m:
            ident = m.group(1).strip()
            if tipo == "anuncio":
                ident = ident.upper().replace("-", "")
            return tipo, ident
    path = urlparse(url).path
    if IGNORED_PATHS.match(path):
        return None
    return "outro", path.strip("/") or urlparse(url).netloc


def extract_ml_links(html: str) -> list[str]:
    """Encontra URLs do ML em qualquer lugar do HTML (hrefs, scripts, JSON-LD...)."""
    text = unescape(html).replace("\\/", "/")
    found: dict[str, None] = {}
    for m in ML_URL_RE.finditer(text):
        found[m.group(0).rstrip(".,;")] = None
    return list(found)


def visible_mentions(html: str) -> bool:
    stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.I | re.S)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return bool(ML_MENTION_RE.search(unescape(stripped)))


def pick_internal_pages(base_url: str, html: str, limit: int) -> list[str]:
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    picked: dict[str, None] = {}
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        parsed = urlparse(absolute)
        if parsed.netloc.lower().removeprefix("www.") != base_host:
            continue
        haystack = (parsed.path + " " + text).lower().replace(" ", "-")
        if any(k in haystack for k in INTERNAL_KEYWORDS):
            picked[absolute] = None
        if len(picked) >= limit:
            break
    return list(picked)


class Finder:
    def __init__(self, timeout: float = 15, max_pages: int = 5, resolve: bool = False):
        self.timeout = timeout
        self.max_pages = max_pages
        self.resolve = resolve
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        })

    def fetch(self, url: str) -> requests.Response:
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp

    def analyze(self, site: str) -> SiteResult:
        site = normalize_input_url(site)
        result = SiteResult(site=site)
        try:
            home = self.fetch(site)
        except requests.RequestException as exc:
            result.status = "erro"
            result.erro = str(exc)[:200]
            return result

        pages = [(home.url, home.text)]
        for extra in pick_internal_pages(home.url, home.text, self.max_pages - 1):
            try:
                pages.append((extra, self.fetch(extra).text))
            except requests.RequestException:
                continue

        seen: set[tuple[str, str]] = set()
        for page_url, html in pages:
            result.paginas_analisadas.append(page_url)
            if visible_mentions(html):
                result.mencao_texto = True
            for link in extract_ml_links(html):
                classified = classify_ml_url(link)
                if not classified:
                    continue
                tipo, ident = classified
                if (tipo, ident) in seen:
                    continue
                seen.add((tipo, ident))
                result.achados.append(Finding(tipo, ident, link, page_url))

        if self.resolve:
            self._resolve_findings(result)

        result.confianca = self._confidence(result)
        return result

    def _resolve_findings(self, result: SiteResult) -> None:
        """Segue links curtos/anuncios para tentar descobrir o vendedor."""
        for f in result.achados:
            if f.tipo not in ("link_curto", "anuncio", "outro"):
                continue
            url = f.url if f.url.startswith("http") else "https:" + f.url
            try:
                resp = self.fetch(url)
            except requests.RequestException:
                continue
            final = classify_ml_url(resp.url)
            if final and final[0] in STRONG:
                f.vendedor = f"{final[0]}:{final[1]}"
                continue
            f.vendedor = seller_from_ml_page(resp.text)

    @staticmethod
    def _confidence(result: SiteResult) -> str:
        tipos = {f.tipo for f in result.achados}
        if tipos & STRONG or any(f.vendedor for f in result.achados):
            return "alta"
        if tipos & {"anuncio", "link_curto"}:
            return "media"
        if tipos or result.mencao_texto:
            return "baixa"
        return "nenhuma"


def seller_from_ml_page(html: str) -> str:
    """Tenta extrair o vendedor do HTML de um anuncio do ML (melhor esforco)."""
    text = unescape(html).replace("\\/", "/")
    for link in extract_ml_links(text):
        c = classify_ml_url(link)
        if c and c[0] in ("perfil", "loja_oficial"):
            return f"{c[0]}:{c[1]}"
    m = re.search(r'"seller_?[iI]d"\s*:\s*"?(\d+)', text)
    if m:
        return f"seller_id:{m.group(1)}"
    return ""


def read_urls(args) -> list[str]:
    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip().split(",")[0].strip()
                if line and not line.startswith("#") and line.lower() not in ("url", "site"):
                    urls.append(line)
    if not args.urls and not args.file and not sys.stdin.isatty():
        urls.extend(l.strip() for l in sys.stdin if l.strip())
    return list(dict.fromkeys(urls))


def write_csv(results: list[SiteResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["site", "status", "confianca", "perfis_ml", "tipo", "identificador",
                    "vendedor_resolvido", "url_ml", "encontrado_em", "mencao_texto", "erro"])
        for r in results:
            perfis = " | ".join(r.perfis())
            if not r.achados:
                w.writerow([r.site, r.status, r.confianca, perfis, "", "", "", "", "",
                            r.mencao_texto, r.erro])
            for f in r.achados:
                w.writerow([r.site, r.status, r.confianca, perfis, f.tipo, f.identificador,
                            f.vendedor, f.url, f.encontrado_em, r.mencao_texto, r.erro])


def write_json(results: list[SiteResult], path: str) -> None:
    data = [{**asdict(r), "perfis_ml": r.perfis()} for r in results]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def print_summary(r: SiteResult) -> None:
    icon = {"alta": "[OK]   ", "media": "[?]    ", "baixa": "[fraco]", "nenhuma": "[--]   "}.get(r.confianca, "[--]   ")
    if r.status == "erro":
        print(f"[ERRO] {r.site}  ->  erro: {r.erro}")
        return
    print(f"{icon} {r.site}  ->  confianca {r.confianca}")
    for f in r.achados:
        extra = f"  [vendedor: {f.vendedor}]" if f.vendedor else ""
        print(f"     - {f.tipo}: {f.identificador}  ({f.url}){extra}")
    if not r.achados and r.mencao_texto:
        print("     - menciona 'Mercado Livre' no texto, mas sem link")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Encontra perfis do Mercado Livre vinculados a sites.")
    ap.add_argument("urls", nargs="*", help="URLs dos sites a analisar")
    ap.add_argument("-f", "--file", help="arquivo .txt/.csv com uma URL por linha")
    ap.add_argument("--csv", help="salvar resultado em CSV")
    ap.add_argument("--json", help="salvar resultado em JSON")
    ap.add_argument("--max-pages", type=int, default=5, help="paginas por site (padrao 5)")
    ap.add_argument("--timeout", type=float, default=15, help="timeout por requisicao (s)")
    ap.add_argument("--workers", type=int, default=5, help="sites analisados em paralelo")
    ap.add_argument("--resolve", action="store_true",
                    help="seguir links curtos/anuncios do ML para descobrir o vendedor")
    args = ap.parse_args(argv)

    # Sem argumentos (ex.: duplo clique): usa url.txt ao lado do script
    # e salva resultado.csv automaticamente.
    auto = not args.urls and not args.file
    if auto:
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("url.txt", "urls.txt", "url", "urls"):
            candidate = os.path.join(here, name)
            if os.path.isfile(candidate):
                args.file = candidate
                break
        if args.file and not args.csv:
            args.csv = os.path.join(here, "resultado.csv")
        if args.file:
            print(f"Lendo URLs de {args.file}\n")

    urls = read_urls(args)
    if not urls:
        ap.error("informe ao menos uma URL (argumento, -f arquivo ou stdin) "
                 "ou coloque um url.txt na mesma pasta do script")

    finder = Finder(timeout=args.timeout, max_pages=max(1, args.max_pages), resolve=args.resolve)
    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = []
        for r in pool.map(finder.analyze, urls):
            print_summary(r)
            results.append(r)

    if args.csv:
        write_csv(results, args.csv)
        print(f"\nCSV salvo em {args.csv}")
    if args.json:
        write_json(results, args.json)
        print(f"JSON salvo em {args.json}")

    com_perfil = sum(1 for r in results if r.confianca == "alta")
    print(f"\n{len(results)} sites analisados em {time.time() - start:.1f}s - "
          f"{com_perfil} com perfil ML identificado.")
    if auto and sys.stdin.isatty():
        input("\nPressione Enter para fechar...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
