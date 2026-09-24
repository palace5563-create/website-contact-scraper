# website-contact-scraper
Script para extrair contatos (email, telefone, WhatsApp) do rodapé de sites automaticamente

## ml_finder.py — perfis do Mercado Livre vinculados a sites

Você envia uma lista de URLs de lojas próprias (fora de marketplace) e a ferramenta
verifica se cada site tem um perfil de vendedor/loja do Mercado Livre vinculado.

Para cada site ela analisa a página inicial + até 4 páginas internas relevantes
(contato, sobre, quem somos, onde comprar...) e procura links do ML em qualquer lugar
do HTML (links, rodapé, scripts, JSON):

| Tipo                | Exemplo                                              | Confiança |
|---------------------|------------------------------------------------------|-----------|
| `perfil`            | `perfil.mercadolivre.com.br/NICK`                    | alta      |
| `loja_oficial`      | `loja.mercadolivre.com.br/marca`                     | alta      |
| `pagina`            | `mercadolivre.com.br/pagina/marca`                   | alta      |
| `listagem_vendedor` | `lista.mercadolivre.com.br/_CustId_123`              | alta      |
| `anuncio`           | `produto.mercadolivre.com.br/MLB-123...`             | média     |
| `link_curto`        | `mercadolivre.com/sec/abc`                           | média     |
| menção em texto     | "Mercado Livre" escrito, sem link                    | baixa     |

Com `--resolve`, links curtos e anúncios são abertos para descobrir o vendedor por trás
(melhor esforço: o ML pode bloquear acessos automatizados).

### Uso

```bash
pip install -r requirements.txt

python ml_finder.py minhaloja.com.br https://outraloja.com
python ml_finder.py -f urls.txt --csv resultado.csv
python ml_finder.py -f urls.txt --json resultado.json --resolve --workers 10
```

`urls.txt`: uma URL por linha (um CSV também serve; a primeira coluna é usada).

Opções: `--max-pages` (padrão 5), `--timeout` (15s), `--workers` (5 sites em paralelo).

### Testes

```bash
python -m unittest test_ml_finder
```
