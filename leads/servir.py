#!/usr/bin/env python3
r"""
Sobe o site em modo producao.

waitress e nao gunicorn porque gunicorn nao roda no Windows (depende de fork).
waitress e WSGI puro, funciona nos dois sistemas e aguenta os 15 usuarios
desta operacao sem drama.

Escuta em 127.0.0.1 por padrao: quem publica para fora e o cloudflared, que
roda na mesma maquina. Assim nao existe porta aberta no roteador -- e mesmo
que exista, ninguem alcanca o site por IP.
"""
import argparse

from waitress import serve

from . import config, web


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 para expor na rede local (padrao: so local)")
    ap.add_argument("--porta", type=int, default=config.WEB_PORTA)
    ap.add_argument("--threads", type=int, default=config.WEB_THREADS)
    args = ap.parse_args()

    app = web.criar_app()
    print(f"Leads CNPJ ouvindo em http://{args.host}:{args.porta} "
          f"({args.threads} threads)", flush=True)
    serve(app, host=args.host, port=args.porta, threads=args.threads,
          ident="leads-cnpj")


if __name__ == "__main__":
    main()
