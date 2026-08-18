#!/usr/bin/env python3
"""
Busca CNPJs por cidade (e opcionalmente bairro, CNAE, Simples/MEI) nos
arquivos de Dados Abertos do CNPJ (Receita Federal) previamente baixados
por baixar_arquivos.py.

Nao usa banco de dados: varre os zips linha a linha e filtra na hora, usando
varios processos em paralelo (um por arquivo) para aproveitar os nucleos do
computador. Isso evita duplicar os dados num banco -- o cache/ continua
sendo a unica copia em disco.

Bairro e opcional: se nao informar, traz o municipio inteiro (pode ser MUITO
mais lento e retornar muito mais linhas numa cidade grande).

Exemplo:
    python buscar_leads.py --cidade "Sao Paulo" --uf SP --bairro "Pinheiros" \\
        --cnae contabilidade --apenas-mei --saida leads_pinheiros.csv
"""
import argparse
import csv
import difflib
import glob
import io
import os
import sys
import unicodedata
import zipfile
from multiprocessing import Pool, cpu_count

import config


def normalizar(texto):
    if texto is None:
        return ""
    texto = texto.strip().upper()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto


def _detectar_encoding(prefixo_bytes):
    """Os arquivos da Receita sao documentados como latin-1, mas depois da
    migracao de infraestrutura (fev/2026) alguns vieram com BOM/encoding
    diferente. Detecta pelo cabecalho em vez de assumir."""
    if prefixo_bytes.startswith(b"\xff\xfe") or prefixo_bytes.startswith(b"\xfe\xff"):
        return "utf-16"
    if prefixo_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "latin-1"


def _linhas_sem_nul(ftext):
    """csv.reader do Python rejeita qualquer linha com um byte nulo (e uma
    protecao da propria linguagem, nao configuravel). Dado real as vezes
    vem com isso -- tira o nulo antes de entregar pro csv.reader em vez de
    deixar a busca inteira quebrar."""
    for linha_bruta in ftext:
        if "\x00" in linha_bruta:
            linha_bruta = linha_bruta.replace("\x00", "")
        yield linha_bruta


def abrir_linhas_zip(caminho_zip):
    """Gera listas de colunas para cada linha de cada CSV dentro do zip."""
    with zipfile.ZipFile(caminho_zip) as z:
        for nome in z.namelist():
            with z.open(nome) as fpeek:
                prefixo = fpeek.read(4)
            encoding = _detectar_encoding(prefixo)
            with z.open(nome) as fbin:
                ftext = io.TextIOWrapper(fbin, encoding=encoding, newline="", errors="replace")
                leitor = csv.reader(_linhas_sem_nul(ftext), delimiter=";")
                for linha in leitor:
                    if not linha:
                        continue  # linha em branco no fim do arquivo, etc.
                    yield linha


def carregar_lookup(caminho_zip):
    """Carrega Municipios.zip ou Cnaes.zip como dict codigo -> descricao."""
    mapa = {}
    if not caminho_zip or not os.path.exists(caminho_zip):
        return mapa
    for linha in abrir_linhas_zip(caminho_zip):
        if len(linha) >= 2:
            mapa[linha[config.LOOKUP_CODIGO]] = linha[config.LOOKUP_DESCRICAO]
    return mapa


def carregar_simples(cache_dir):
    """Carrega Simples.zip como dict cnpj_basico -> (optante_simples, optante_mei),
    ambos booleanos. Se o arquivo nao existir ainda (base baixada antes desse
    filtro existir), retorna vazio -- os filtros de simples/mei simplesmente
    nao encontram nada e quem chama decide o que fazer."""
    caminho = os.path.join(cache_dir, "Simples.zip")
    mapa = {}
    if not os.path.exists(caminho):
        return mapa
    for linha in abrir_linhas_zip(caminho):
        try:
            basico = linha[config.SIMPLES_CNPJ_BASICO]
            optante_simples = linha[config.SIMPLES_OPCAO_SIMPLES].strip().upper() == "S"
            optante_mei = linha[config.SIMPLES_OPCAO_MEI].strip().upper() == "S"
            mapa[basico] = (optante_simples, optante_mei)
        except IndexError:
            continue
    return mapa


def encontrar_codigos_municipio(mapa_municipios, cidade_norm):
    return {
        codigo
        for codigo, nome in mapa_municipios.items()
        if normalizar(nome) == cidade_norm
    }


def encontrar_codigos_cnae(mapa_cnaes, cnae_filtro):
    """Aceita codigo (ou prefixo de codigo) numerico, ou palavra-chave que
    bate na descricao do CNAE (ex.: 'contabilidade').

    Retorna (codigos, sugestoes):
      - Se cnae_filtro for vazio: (None, None) -- sem filtro.
      - Se achou por codigo ou substring exata: (set_de_codigos, None).
      - Se nao achou nada por substring exata, tenta aproximar por palavra
        (corrige erro de digitacao/plural/acento) e devolve
        (set(), lista_de_descricoes_proximas) para quem chama decidir --
        aqui a gente so sugere, nao aplica sozinho, pra nao buscar um ramo
        errado sem o usuario perceber."""
    if not cnae_filtro:
        return None, None

    alvo = cnae_filtro.strip()
    if alvo.isdigit():
        return {codigo for codigo in mapa_cnaes if codigo.startswith(alvo)}, None

    alvo_norm = normalizar(alvo)
    codigos = {codigo for codigo, desc in mapa_cnaes.items() if alvo_norm in normalizar(desc)}
    if codigos:
        return codigos, None

    palavra_para_descricoes = {}
    for desc in mapa_cnaes.values():
        for palavra in normalizar(desc).split():
            if len(palavra) >= 4:
                palavra_para_descricoes.setdefault(palavra, set()).add(desc)

    descricoes_proximas = set()
    for termo in alvo_norm.split():
        if len(termo) < 4:
            continue
        proximas = difflib.get_close_matches(termo, palavra_para_descricoes.keys(), n=3, cutoff=0.72)
        for p in proximas:
            descricoes_proximas |= palavra_para_descricoes[p]

    return set(), sorted(descricoes_proximas)[:8]


def formatar_cnpj(basico, ordem, dv):
    n = f"{basico}{ordem}{dv}"
    if len(n) != 14 or not n.isdigit():
        return n
    return f"{n[0:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:14]}"


def montar_endereco(linha):
    partes = [
        linha[config.EST_TIPO_LOGRADOURO],
        linha[config.EST_LOGRADOURO],
    ]
    logradouro = " ".join(p for p in partes if p).strip()
    numero = linha[config.EST_NUMERO]
    complemento = linha[config.EST_COMPLEMENTO]
    endereco = logradouro
    if numero:
        endereco += f", {numero}"
    if complemento:
        endereco += f" - {complemento}"
    return endereco


# --- funcoes de trabalho por arquivo, chamadas em processos separados ---

def _filtrar_estabelecimentos(args):
    caminho, uf, codigos_municipio, bairro_norm, apenas_ativas, mapa_cnaes, codigos_cnae = args
    parciais = {}
    ignoradas = 0
    for linha in abrir_linhas_zip(caminho):
        try:
            if uf and linha[config.EST_UF] != uf.upper():
                continue
            if linha[config.EST_MUNICIPIO] not in codigos_municipio:
                continue
            if bairro_norm and bairro_norm not in normalizar(linha[config.EST_BAIRRO]):
                continue
            if apenas_ativas and linha[config.EST_SITUACAO_CADASTRAL] != config.SITUACAO_CADASTRAL_ATIVA:
                continue
            if codigos_cnae is not None and linha[config.EST_CNAE_PRINCIPAL] not in codigos_cnae:
                continue

            basico = linha[config.EST_CNPJ_BASICO]
            cnae = linha[config.EST_CNAE_PRINCIPAL]
            parciais[basico] = {
                "cnpj": formatar_cnpj(basico, linha[config.EST_CNPJ_ORDEM], linha[config.EST_CNPJ_DV]),
                "nome_fantasia": linha[config.EST_NOME_FANTASIA],
                "situacao_cadastral": config.SITUACAO_CADASTRAL_DESCRICAO.get(
                    linha[config.EST_SITUACAO_CADASTRAL], linha[config.EST_SITUACAO_CADASTRAL]
                ),
                "cnae_principal": cnae,
                "cnae_descricao": mapa_cnaes.get(cnae, ""),
                "endereco": montar_endereco(linha),
                "bairro": linha[config.EST_BAIRRO],
                "cep": linha[config.EST_CEP],
                "municipio_codigo": linha[config.EST_MUNICIPIO],
                "uf": linha[config.EST_UF],
                "ddd1": linha[config.EST_DDD1],
                "telefone1": linha[config.EST_TELEFONE1],
                "ddd2": linha[config.EST_DDD2],
                "telefone2": linha[config.EST_TELEFONE2],
                "email": linha[config.EST_EMAIL],
                "data_inicio_atividade": linha[config.EST_DATA_INICIO_ATIVIDADE],
                "razao_social": "",
            }
        except IndexError:
            ignoradas += 1
            continue
    return parciais, ignoradas


def _buscar_razao_social(args):
    caminho, basicos_alvo = args
    encontrados = {}
    faltando = set(basicos_alvo)
    for linha in abrir_linhas_zip(caminho):
        if not faltando:
            break
        try:
            basico = linha[config.EMP_CNPJ_BASICO]
            if basico in faltando:
                encontrados[basico] = linha[config.EMP_RAZAO_SOCIAL]
                faltando.discard(basico)
        except IndexError:
            continue
    return encontrados


def _num_processos(qtd_arquivos):
    return max(1, min(qtd_arquivos, cpu_count()))


def buscar(cache_dir, cidade, uf, bairro, apenas_ativas,
           cnae=None, apenas_simples=False, apenas_mei=False):
    """bairro pode ser None ou "" -- nesse caso busca o municipio inteiro."""
    municipios_zip = os.path.join(cache_dir, "Municipios.zip")
    cnaes_zip = os.path.join(cache_dir, "Cnaes.zip")

    mapa_municipios = carregar_lookup(municipios_zip)
    mapa_cnaes = carregar_lookup(cnaes_zip)
    if not mapa_municipios:
        sys.exit(
            "Nao encontrei cache/Municipios.zip. Rode baixar_arquivos.py primeiro."
        )

    cidade_norm = normalizar(cidade)
    bairro_norm = normalizar(bairro)
    codigos_municipio = encontrar_codigos_municipio(mapa_municipios, cidade_norm)
    if not codigos_municipio:
        sugestoes = [
            nome for nome in mapa_municipios.values() if cidade_norm in normalizar(nome)
        ][:10]
        msg = f"Nenhum municipio encontrado para '{cidade}'."
        if sugestoes:
            msg += f" Voce quis dizer: {', '.join(sugestoes)}?"
        sys.exit(msg)

    codigos_cnae, sugestoes_cnae = encontrar_codigos_cnae(mapa_cnaes, cnae)
    if cnae and not codigos_cnae:
        msg = f"Nenhum CNAE encontrado para '{cnae}'."
        if sugestoes_cnae:
            msg += f" Voce quis dizer: {', '.join(sugestoes_cnae)}?"
        sys.exit(msg)

    if (apenas_simples or apenas_mei) and not os.path.exists(os.path.join(cache_dir, "Simples.zip")):
        sys.exit(
            "Voce pediu filtro de Simples/MEI mas ainda nao tem cache/Simples.zip. "
            "Rode baixar_arquivos.py de novo (ele baixa esse arquivo agora)."
        )

    estabelecimentos_zips = sorted(glob.glob(os.path.join(cache_dir, "Estabelecimentos*.zip")))
    if not estabelecimentos_zips:
        sys.exit("Nenhum Estabelecimentos*.zip em cache/. Rode baixar_arquivos.py primeiro.")

    if not bairro_norm:
        print("[aviso] sem bairro definido: buscando o municipio inteiro. Pode demorar bem mais e trazer muitas linhas.")

    resultados = {}
    linhas_ignoradas = 0
    args_list = [
        (caminho, uf, codigos_municipio, bairro_norm, apenas_ativas, mapa_cnaes, codigos_cnae)
        for caminho in estabelecimentos_zips
    ]

    n = _num_processos(len(estabelecimentos_zips))
    print(f"Varrendo {len(estabelecimentos_zips)} arquivo(s) de estabelecimentos usando {n} processo(s) em paralelo...")
    try:
        with Pool(processes=n) as pool:
            for parciais, ignoradas in pool.imap_unordered(_filtrar_estabelecimentos, args_list):
                resultados.update(parciais)
                linhas_ignoradas += ignoradas
    except Exception as e:
        print(f"[aviso] paralelismo falhou ({e}), varrendo um arquivo por vez...")
        resultados = {}
        linhas_ignoradas = 0
        for a in args_list:
            parciais, ignoradas = _filtrar_estabelecimentos(a)
            resultados.update(parciais)
            linhas_ignoradas += ignoradas

    for r in resultados.values():
        r["municipio"] = mapa_municipios.get(r.pop("municipio_codigo", ""), "")

    mapa_simples = carregar_simples(cache_dir) if (apenas_simples or apenas_mei or resultados) else {}
    if apenas_simples or apenas_mei:
        for basico in list(resultados.keys()):
            optante_simples, optante_mei = mapa_simples.get(basico, (False, False))
            resultados[basico]["optante_simples"] = "Sim" if optante_simples else "Nao"
            resultados[basico]["optante_mei"] = "Sim" if optante_mei else "Nao"
            if apenas_simples and not optante_simples:
                del resultados[basico]
                continue
            if apenas_mei and not optante_mei:
                del resultados[basico]
    else:
        for basico, dados in resultados.items():
            optante_simples, optante_mei = mapa_simples.get(basico, (False, False))
            dados["optante_simples"] = ("Sim" if optante_simples else "Nao") if mapa_simples else ""
            dados["optante_mei"] = ("Sim" if optante_mei else "Nao") if mapa_simples else ""

    print(f"  {len(resultados)} estabelecimento(s) encontrados"
          + (f" ({linhas_ignoradas} linha(s) mal-formada(s) ignorada(s))" if linhas_ignoradas else "")
          + ". Buscando razao social...")

    empresas_zips = sorted(glob.glob(os.path.join(cache_dir, "Empresas*.zip")))
    basicos_pendentes = set(resultados.keys())
    if empresas_zips and basicos_pendentes:
        args_emp = [(caminho, basicos_pendentes) for caminho in empresas_zips]
        n2 = _num_processos(len(empresas_zips))
        try:
            with Pool(processes=n2) as pool:
                for encontrados in pool.imap_unordered(_buscar_razao_social, args_emp):
                    for basico, razao in encontrados.items():
                        resultados[basico]["razao_social"] = razao
        except Exception as e:
            print(f"[aviso] paralelismo falhou ({e}), varrendo um arquivo por vez...")
            for a in args_emp:
                encontrados = _buscar_razao_social(a)
                for basico, razao in encontrados.items():
                    resultados[basico]["razao_social"] = razao

    return list(resultados.values())


COLUNAS_SAIDA = [
    "cnpj", "razao_social", "nome_fantasia", "situacao_cadastral",
    "cnae_principal", "cnae_descricao", "optante_simples", "optante_mei",
    "endereco", "bairro", "cep", "municipio", "uf",
    "ddd1", "telefone1", "ddd2", "telefone2", "email",
    "data_inicio_atividade",
]


def salvar_csv(linhas, caminho_saida):
    with open(caminho_saida, "w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUNAS_SAIDA, extrasaction="ignore", restval="")
        escritor.writeheader()
        for linha in linhas:
            escritor.writerow(linha)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cidade", required=True, help='ex.: "Sao Paulo"')
    ap.add_argument("--bairro", default=None, help='opcional; ex.: "Pinheiros". Sem isso, busca o municipio inteiro (mais lento, mais resultado)')
    ap.add_argument("--uf", help="ex.: SP (recomendado, evita ambiguidade de nomes de cidade)")
    ap.add_argument("--cnae", help='codigo (ou prefixo) de CNAE, ou palavra-chave, ex.: "contabilidade"')
    ap.add_argument("--apenas-simples", action="store_true", help="so empresas optantes pelo Simples Nacional")
    ap.add_argument("--apenas-mei", action="store_true", help="so MEI")
    ap.add_argument("--saida", default="leads.csv")
    ap.add_argument("--cache-dir", default=config.CACHE_DIR)
    ap.add_argument("--todas-situacoes", action="store_true", help="inclui suspensas/baixadas/inaptas, nao so ativas")
    args = ap.parse_args()

    linhas = buscar(
        cache_dir=args.cache_dir,
        cidade=args.cidade,
        uf=args.uf,
        bairro=args.bairro,
        apenas_ativas=not args.todas_situacoes,
        cnae=args.cnae,
        apenas_simples=args.apenas_simples,
        apenas_mei=args.apenas_mei,
    )

    if not linhas:
        print("Nenhum resultado. Confira a grafia do bairro/cidade (o cadastro da Receita nao e padronizado).")
        return

    salvar_csv(linhas, args.saida)
    print(f"OK: {len(linhas)} empresa(s) salvas em {args.saida}")


if __name__ == "__main__":
    main()
