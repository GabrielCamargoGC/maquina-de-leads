# Máquina de Leads

<https://github.com/GabrielCamargoGC/maquina-de-leads>

Site interno de busca de empresas nos **Dados Abertos do CNPJ** da Receita
Federal. Roda num desktop da empresa, sem nuvem e sem mensalidade.

- **Instalar no servidor:** [DEPLOY.md](DEPLOY.md)
- **Fonte dos dados:** <https://arquivos.receitafederal.gov.br> (Nextcloud público da RFB)
- **Atualização:** a Receita publica 1x/mês; o job confere todo dia às 03:00 e
  só baixa quando muda

---

## O que faz

| Tela | Para quê |
|---|---|
| **Busca** | empresas por cidade(s), bairro, CNAE/ramo, porte, capital, data de abertura, Simples/MEI, com telefone/e-mail |
| **Empresas novas** | quem apareceu desde a atualização anterior da Receita |
| **Planilhas** | exports em CSV e Excel, gerados em fila |
| **Base** | qual mês está no ar, quantos registros, por UF |

---

## Por que Parquet e não Postgres

O programa original varria os 7,3 GB de zip a cada busca — minutos por
consulta, e 15 pessoas simultâneas travariam a máquina. A alternativa óbvia
seria Postgres, mas a base ocuparia ~60 GB e a máquina tem 8 GB de RAM: 13%
dos dados em cache significa ir a disco em toda busca.

Parquet resolve os dois problemas: **~5 GB em disco** e busca **sub-segundo**,
porque é colunar (lê só as colunas filtradas) e particionado (abre só os
arquivos da UF pedida). E não é servidor de banco — são arquivos, sem nada
para administrar.

Números medidos na conversão de julho/2026:

| | |
|---|---|
| Estabelecimentos | 72.318.968 |
| Empresas | 69.062.850 |
| Simples/MEI | 49.445.426 |
| Zip de origem | 7,3 GB |
| Parquet gerado | ~5 GB |
| Tempo de import | ~3 min |

---

## Estrutura

```
leads/
├── config.py        caminhos e limites (tudo sobrescrevível por env)
├── layout.py        layout dos CSV da Receita (colunas, códigos)
├── fonte_rfb.py     descobre e baixa da Receita, com retomada
├── importador.py    zip -> Parquet, dividido em baldes de cnpj_basico
├── consolidar.py    junta as partes, calcula campos, VALIDA
├── busca.py         filtros -> SQL no DuckDB
├── novidades.py     compara base atual x anterior
├── exportar.py      fila de planilhas (CSV/Excel) em SQLite
├── estado.py        metadados da base instalada
├── atualizar.py     o job das 03:00 (baixa -> converte -> valida -> troca)
├── web.py           rotas Flask
├── servir.py        waitress (produção)
└── templates/       telas

leads_cnpj/          programa original, mantido como referência
instalar.ps1         instalador do Windows (serviços + agendador)
DEPLOY.md            guia de instalação
```

### Fluxo dos dados

```
arquivos.receitafederal.gov.br
        │  fonte_rfb        7,3 GB de zip, 1x/mês
        ▼
   downloads/
        │  importador       CSV em fluxo -> Parquet por balde
        ▼
   dados/novo/
        │  consolidar       junta + campos calculados + VALIDA
        ▼
   dados/novo/empresas_final/
        │  atualizar        troca atômica (renomeia pastas)
        ▼
   dados/atual/  ◄── o site lê aqui        dados/anterior/ ◄── comparação
        │  busca / novidades                                   de novidades
        ▼
   web.py  ──► waitress ──► cloudflared ──► equipe
```

---

## Desenvolvimento

```bash
py -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# fatia pequena, para não moer a máquina de desenvolvimento
.venv/Scripts/python.exe -m leads.importador --ufs AC
.venv/Scripts/python.exe -m leads.consolidar --ufs AC

# site em modo dev
.venv/Scripts/python.exe -m leads.web
```

> **Nunca rode a conversão do Brasil inteiro numa máquina de trabalho.** São
> ~2 GB de RAM e vários minutos de disco saturado. Use `--ufs` para
> desenvolver; carga completa é operação de servidor.

### Variáveis úteis

| Variável | Padrão | Para quê |
|---|---|---|
| `LEADS_DADOS` | `./dados` | onde ficam os Parquet |
| `LEADS_DOWNLOADS` | `./leads_cnpj/cache` | onde ficam os zips |
| `LEADS_DUCKDB_MEMORIA` | `2GB` | teto do site |
| `LEADS_DUCKDB_MEMORIA_IMPORT` | `5GB` | teto da conversão |
| `LEADS_PORTA` | `8080` | porta local |

---

## Notas sobre o dado

Coisas do formato da Receita que já morderam e estão tratadas no código:

- **`Estabelecimentos0.zip` é ~6× maior** que as outras 9 partes. Não é erro
  de download.
- **Encoding varia.** Documentado como latin-1, mas apareceu BOM UTF-16 depois
  da migração de fev/2026 — `importador` detecta pelo cabeçalho.
- **Bytes nulos** no meio do CSV. O `csv` do Python recusa; o leitor do
  pyarrow aceita.
- **Sem cabeçalho.** A ordem das colunas é a única referência — daí
  `layout.py`.
- **Capital social com vírgula decimal** (`120000000000,00`).
- **Quem não está na tabela do Simples** não é "não optante", é ausente. O
  `coalesce` em `consolidar.py` transforma em `false` de propósito: sem isso a
  empresa desapareceria de filtros negativos e a planilha viria com célula
  vazia.
- **Nome de bairro não é padronizado.** Busca de bairro é por substring,
  maiúscula e sem acento.
