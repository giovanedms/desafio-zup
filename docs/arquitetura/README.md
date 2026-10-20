# Documento de arquitetura

O texto está em [documento-de-arquitetura.md](documento-de-arquitetura.md). Os diagramas ficam em `diagramas/`, com a fonte em Mermaid (`.mmd`) e o SVG renderizado. Matriz: PAP-02, ENT-01.

| # | Diagrama | Fonte | SVG |
|---|---|---|---|
| 1 | C4 nível 1, contexto | [01-contexto.mmd](diagramas/01-contexto.mmd) | [01-contexto.svg](diagramas/01-contexto.svg) |
| 2 | C4 nível 2, containers | [02-containers.mmd](diagramas/02-containers.mmd) | [02-containers.svg](diagramas/02-containers.svg) |
| 3 | Sequência síncrona de uma autorização | [03-sequencia-autorizacao.mmd](diagramas/03-sequencia-autorizacao.mmd) | [03-sequencia-autorizacao.svg](diagramas/03-sequencia-autorizacao.svg) |
| 4 | Fluxo de eventos (outbox, relay, bus, filas, CDC) | [04-fluxo-de-eventos.mmd](diagramas/04-fluxo-de-eventos.mmd) | [04-fluxo-de-eventos.svg](diagramas/04-fluxo-de-eventos.svg) |
| 5 | Implantação na AWS (sandbox) | [05-implantacao-aws.mmd](diagramas/05-implantacao-aws.mmd) | [05-implantacao-aws.svg](diagramas/05-implantacao-aws.svg) |

Não há diagrama C4 nível 3.

## Como regenerar os SVGs

Na raiz do repositório (precisa de Node e de um Chrome do puppeteer; em ambiente root, passe um arquivo de config com `{"args":["--no-sandbox"]}` via `-p`):

```bash
cd docs/arquitetura/diagramas
for f in *.mmd; do
  npx -y @mermaid-js/mermaid-cli@11.4.2 -i "$f" -o "${f%.mmd}.svg" -b white
done
```

Os blocos Mermaid de 1 a 3 também aparecem no documento de arquitetura. Se mudar um, mude o outro.
