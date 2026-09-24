import unittest
from unittest import mock

import ml_finder as mf

HOME = """
<html><body>
<nav><a href="/contato">Fale conosco</a> <a href="/produtos">Produtos</a>
<a href="https://outrosite.com/sobre">externo</a></nav>
<footer>
  Compre também no Mercado Livre!
  <a href="https://perfil.mercadolivre.com.br/LOJA_EXEMPLO">ML</a>
  <a href="https://www.mercadolivre.com.br/ajuda">ajuda</a>
</footer>
<script>var s = {"url":"https:\\/\\/lista.mercadolivre.com.br\\/_CustId_123456"};</script>
</body></html>
"""

CONTATO = """
<a href="https://produto.mercadolivre.com.br/MLB-1234567890-tenis-_JM">anúncio</a>
<a href="https://mercadolivre.com/sec/2abcXYZ">link curto</a>
"""


class ClassifyTests(unittest.TestCase):
    def test_types(self):
        cases = {
            "https://perfil.mercadolivre.com.br/NICK123": ("perfil", "NICK123"),
            "https://www.mercadolivre.com.br/perfil/NICK123?x=1": ("perfil", "NICK123"),
            "https://loja.mercadolivre.com.br/marca-x": ("loja_oficial", "marca-x"),
            "https://www.mercadolivre.com.br/loja/marca-x": ("loja_oficial", "marca-x"),
            "https://www.mercadolivre.com.br/pagina/marcax": ("pagina", "marcax"),
            "https://lista.mercadolivre.com.br/_CustId_998877": ("listagem_vendedor", "998877"),
            "https://produto.mercadolivre.com.br/MLB-1234567890-x-_JM": ("anuncio", "MLB1234567890"),
            "https://www.mercadolivre.com.br/tenis/p/MLB19876543": ("anuncio", "MLB19876543"),
            "https://mercadolivre.com/sec/1a2B3c": ("link_curto", "1a2B3c"),
            "https://perfil.mercadolibre.com.ar/VENDEDOR": ("perfil", "VENDEDOR"),
        }
        for url, expected in cases.items():
            self.assertEqual(mf.classify_ml_url(url), expected, url)

    def test_ignored(self):
        self.assertIsNone(mf.classify_ml_url("https://www.mercadolivre.com.br/"))
        self.assertIsNone(mf.classify_ml_url("https://www.mercadolivre.com.br/ajuda"))


class ExtractTests(unittest.TestCase):
    def test_extract_includes_escaped_script_urls(self):
        links = mf.extract_ml_links(HOME)
        self.assertIn("https://perfil.mercadolivre.com.br/LOJA_EXEMPLO", links)
        self.assertIn("https://lista.mercadolivre.com.br/_CustId_123456", links)

    def test_internal_pages_same_host_only(self):
        pages = mf.pick_internal_pages("https://www.minhaloja.com.br/", HOME, 5)
        self.assertEqual(pages, ["https://www.minhaloja.com.br/contato"])

    def test_mentions(self):
        self.assertTrue(mf.visible_mentions(HOME))
        self.assertFalse(mf.visible_mentions("<script>'mercado livre'</script><p>oi</p>"))


class AnalyzeTests(unittest.TestCase):
    def _fake_fetch(self, url):
        resp = mock.Mock()
        resp.url = url
        resp.text = {"https://minhaloja.com.br": HOME,
                     "https://minhaloja.com.br/contato": CONTATO}[url]
        return resp

    def test_analyze(self):
        finder = mf.Finder()
        with mock.patch.object(finder, "fetch", side_effect=self._fake_fetch):
            r = finder.analyze("minhaloja.com.br")
        self.assertEqual(r.confianca, "alta")
        tipos = {(f.tipo, f.identificador) for f in r.achados}
        self.assertEqual(tipos, {
            ("perfil", "LOJA_EXEMPLO"),
            ("listagem_vendedor", "123456"),
            ("anuncio", "MLB1234567890"),
            ("link_curto", "2abcXYZ"),
        })
        self.assertEqual(r.perfis(), ["perfil:LOJA_EXEMPLO", "listagem_vendedor:123456"])

    def test_seller_from_page(self):
        html = '<a href="https://perfil.mercadolivre.com.br/VEND_X">ver</a>'
        self.assertEqual(mf.seller_from_ml_page(html), "perfil:VEND_X")
        self.assertEqual(mf.seller_from_ml_page('{"seller_id": 4455}'), "seller_id:4455")


if __name__ == "__main__":
    unittest.main()
