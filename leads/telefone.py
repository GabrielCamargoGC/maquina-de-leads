#!/usr/bin/env python3
r"""
Telefone da Receita -> numero que o WhatsApp aceita.

O PROBLEMA
----------
O campo TELEFONE da Receita comporta 8 caracteres. Medido sobre 2 milhoes de
telefones preenchidos da base de julho/2026: nenhum passa de 8, e 89% tem
exatamente 8. Celular brasileiro tem 9 desde 2012.

Ou seja: nenhum celular esta na base inteiro. Mandar o que esta gravado
direto para o WhatsApp e mandar para um numero que nao existe -- e taxa alta
de numero invalido e o sinal mais rapido de spam que existe.

A RECONSTRUCAO
--------------
Quando a Anatel criou o nono digito, ela PREFIXOU um 9 nos celulares de 8
digitos que ja existiam. Entao para um cadastro pre-2012 o numero de hoje e
"9" + os 8 digitos gravados. Isso nao e chute: e a regra da migracao.

Confere com o caso real que apareceu em producao -- empresa de numero
(17) 99656-0367, gravada como 96560367: "9" + "96560367" = "996560367".

O que distingue celular de fixo e o primeiro digito. Antes do nono digito,
fixo comecava em 2-5 e celular em 6-9. Essa faixa nao mudou.

O QUE CONTINUA SEM SOLUCAO
--------------------------
Cadastro em que o numero foi cortado na gravacao (perdeu o ULTIMO digito em
vez de nunca ter tido o primeiro) fica irrecuperavel -- seriam 10 tentativas
por numero. Aparecem os dois padroes na base e nao da para saber qual e qual.
Por isso "Celular (9 reconstruido)" e rotulado como reconstruido na planilha:
quem dispara merece saber o que e certeza e o que e inferencia.
"""

# DDD que existem de verdade. A base tem DDD malformado, e numero com DDD
# inexistente e envio queimado na certa -- o WhatsApp so responde que o
# numero nao tem conta, depois de ja ter contado a tentativa.
DDDS_VALIDOS = frozenset([
    11, 12, 13, 14, 15, 16, 17, 18, 19,              # SP
    21, 22, 24,                                       # RJ
    27, 28,                                           # ES
    31, 32, 33, 34, 35, 37, 38,                       # MG
    41, 42, 43, 44, 45, 46,                           # PR
    47, 48, 49,                                       # SC
    51, 53, 54, 55,                                   # RS
    61,                                               # DF/GO
    62, 64,                                           # GO
    63,                                               # TO
    65, 66,                                           # MT
    67,                                               # MS
    68,                                               # AC
    69,                                               # RO
    71, 73, 74, 75, 77,                               # BA
    79,                                               # SE
    81, 87,                                           # PE
    82,                                               # AL
    83,                                               # PB
    84,                                               # RN
    85, 88,                                           # CE
    86, 89,                                           # PI
    91, 93, 94,                                       # PA
    92, 97,                                           # AM
    95,                                               # RR
    96,                                               # AP
    98, 99,                                           # MA
])

DDI_BRASIL = "55"

# Rotulos que saem na planilha. Texto e nao codigo porque quem le a coluna e
# uma pessoa decidindo se dispara.
CELULAR = "Celular"
CELULAR_RECONSTRUIDO = "Celular (9 reconstruido)"
FIXO = "Fixo"
INVALIDO = "Invalido"
SEM_NUMERO = ""


def so_digitos(v):
    return "".join(c for c in str(v or "") if c.isdigit())


def classificar(ddd, numero):
    """(tipo, numero_e164_sem_mais) a partir do DDD e do telefone da base.

    Devolve (SEM_NUMERO, "") quando nao ha telefone, e (INVALIDO, "") quando
    ha telefone mas ele nao da para discar. Os dois casos sao diferentes na
    planilha: "vazio" e um lead sem contato, "Invalido" e um lead cujo
    contato esta quebrado na origem -- e so o segundo vale reclamar da fonte.
    """
    d = so_digitos(ddd)
    n = so_digitos(numero)

    if not n:
        return SEM_NUMERO, ""

    # Alguns cadastros trazem o DDD grudado no proprio campo do telefone.
    if not d and len(n) in (10, 11):
        d, n = n[:2], n[2:]

    if not d or len(d) != 2 or int(d) not in DDDS_VALIDOS:
        return INVALIDO, ""

    if len(n) == 9:
        # Nao aparece na base de hoje, mas o dia em que a Receita ampliar o
        # campo isto passa a valer sozinho, sem ninguem lembrar de mexer aqui.
        if n[0] == "9":
            return CELULAR, DDI_BRASIL + d + n
        return INVALIDO, ""

    if len(n) != 8:
        return INVALIDO, ""            # 7 digitos ou menos: truncado demais

    primeiro = n[0]
    if primeiro in "2345":
        return FIXO, DDI_BRASIL + d + n
    if primeiro in "6789":
        return CELULAR_RECONSTRUIDO, DDI_BRASIL + d + "9" + n

    return INVALIDO, ""                # comeca com 0 ou 1: nao existe


def e_celular(tipo):
    """Serve para o filtro "so com celular" e para a lista de disparo."""
    return tipo in (CELULAR, CELULAR_RECONSTRUIDO)


def para_disparo(ddd, numero):
    """Numero pronto para o campo `number` do DigiSac, ou "" se nao serve.

    Fixo fica de fora de proposito: linha fixa raramente tem WhatsApp, e
    encher a fila com ela derruba a taxa de entrega da campanha inteira.
    """
    tipo, e164 = classificar(ddd, numero)
    return e164 if e_celular(tipo) else ""


def melhor_numero(ddd1, tel1, ddd2, tel2):
    """O melhor dos dois telefones do cadastro, para disparo.

    A base traz dois e ate hoje so o primeiro era usado. Quando o primeiro e
    fixo e o segundo e celular, o segundo e que interessa -- e isso acontece
    bastante, porque muita empresa cadastrou o fixo como principal.

    Devolve (tipo, numero). Celular de verdade ganha do reconstruido; os dois
    ganham de fixo.
    """
    opcoes = [classificar(ddd1, tel1), classificar(ddd2, tel2)]
    ordem = {CELULAR: 0, CELULAR_RECONSTRUIDO: 1, FIXO: 2,
             INVALIDO: 3, SEM_NUMERO: 4}
    opcoes.sort(key=lambda t: ordem[t[0]])
    return opcoes[0]


def para_disparo_par(ddd1, tel1, ddd2, tel2):
    """Numero de disparo olhando os dois telefones do cadastro, ou "".

    E o que a planilha de disparo usa: pega celular onde houver, no primeiro
    ou no segundo telefone, e devolve vazio quando nenhum dos dois serve.
    """
    tipo, numero = melhor_numero(ddd1, tel1, ddd2, tel2)
    return numero if e_celular(tipo) else ""


def nome_curto(razao_social, nome_fantasia=None, limite=40):
    """Nome que vai na mensagem, no lugar da razao social crua.

    "MERCADO SAO JOSE LTDA ME" numa mensagem de WhatsApp denuncia disparo em
    massa na primeira linha. Nome fantasia quando existe, caixa de nome
    proprio, e sem os sufixos societarios.
    """
    bruto = (nome_fantasia or "").strip() or (razao_social or "").strip()
    if not bruto:
        return ""

    sufixos = (" LTDA", " ME", " EPP", " EIRELI", " S/A", " SA", " S.A",
               " MEI", " - ME", " EIRELLI")
    alto = bruto.upper()
    mudou = True
    while mudou:
        mudou = False
        for s in sufixos:
            if alto.endswith(s):
                alto = alto[: -len(s)].rstrip(" -,.")
                mudou = True

    miudas = {"DA", "DE", "DO", "DAS", "DOS", "E"}
    palavras = []
    for i, p in enumerate(alto.split()):
        if i > 0 and p in miudas:
            palavras.append(p.lower())
        else:
            palavras.append(p[:1] + p[1:].lower())
    nome = " ".join(palavras).strip()
    return nome[:limite].rstrip() if len(nome) > limite else nome
