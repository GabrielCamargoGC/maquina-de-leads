# Deploy no desktop

Guia da instalação no PC que vai hospedar. Uma tarde, sendo que 2-3 horas é
só esperar a primeira carga.

**Hardware confirmado:** i7-4790, 8 GB RAM, 160 GB livres, internet ~400 Mbps.
Folgado para esta carga. Ver [Dimensionamento](#dimensionamento).

---

## Antes de começar

| Precisa | Onde consegue |
|---|---|
| Windows instalado e atualizado | — |
| Acesso de Administrador | — |
| Python 3.11 ou mais novo | <https://www.python.org/downloads/> — marque **"Add python.exe to PATH"** |
| Git | <https://git-scm.com/download/win> |
| Conta Cloudflare (grátis) | <https://dash.cloudflare.com/sign-up> |
| Domínio no Registro.br | já tem |

---

## 1. Trazer o código

```powershell
git clone https://github.com/GabrielCamargoGC/maquina-de-leads C:\leads
cd C:\leads
```

Só o código viaja — cerca de 200 KB. Os 7,3 GB da Receita **não** vêm de
pendrive: o próprio desktop baixa direto da fonte no passo 3.

---

## 2. Rodar o instalador

PowerShell **como Administrador**:

```powershell
cd C:\leads
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\instalar.ps1
```

O que ele faz:

1. cria o ambiente Python e instala as dependências
2. baixa `nssm.exe` e `cloudflared.exe` para `tools\`
3. desliga suspensão e hibernação
4. registra o serviço **LeadsCNPJ** (o site), com auto-restart
5. agenda o job de atualização para **03:00**, todos os dias
6. testa se o site respondeu

Pode rodar de novo quantas vezes quiser — cada passo confere antes de agir.

### Ainda na BIOS

Ligue **"Restore on AC Power Loss"** (ou "AC Back", "After Power Failure →
Power On"). Sem isso, queda de luz derruba o site até alguém apertar o botão.

---

## 3. Primeira carga da base

```powershell
Start-Process C:\leads\.venv\Scripts\python.exe `
  -ArgumentList "-m","leads.atualizar","--forcar","--retomar" `
  -WorkingDirectory C:\leads -WindowStyle Minimized
```

Roda **destacado**: fechar a janela de onde voce chamou, deslogar ou ir
embora nao mata o job. Minimizado, nao oculto -- com janela oculta um erro
na partida desaparece sem deixar rastro.
Acompanhe pelo log, de qualquer janela:

```powershell
Get-Content C:\leads\logstualizar-*.log -Tail 20 -Wait
```

> Rodar direto no terminal (`python -m leads.atualizar --forcar`) tambem
> funciona, mas o job morre junto com a janela -- e sao ~50 minutos.

Baixa 7,3 GB, converte, valida e publica. Pode fechar a janela? **Não** —
deixe aberta. Se precisar sair, agende para a noite.

O que acontece:

```
1/5 baixando da Receita       ~3-5 min   (a 400 Mbps)
2/5 convertendo para Parquet  ~5 min
3/5 consolidando e validando  ~20-40 min
4/5 trocando a base no ar     instantâneo
5/5 limpando                  apaga os zips
```

No fim, `http://127.0.0.1:8080` já funciona na própria máquina.

> A validação roda **antes** de publicar. Se a Receita tiver publicado
> arquivo truncado, o job aborta e mantém a base anterior. Base velha é
> melhor que meia base.

---

## 4. Cloudflare — acesso de fora

### 4.1 Domínio na Cloudflare

1. painel Cloudflare → **Add a site** → digite seu domínio
2. escolha o plano **Free**
3. a Cloudflare mostra dois nameservers (algo como `xxx.ns.cloudflare.com`)
4. no **Registro.br** → seu domínio → **Alterar servidores DNS** → coloque os dois
5. espere a propagação (minutos a algumas horas)

> Seu domínio não tem o e-mail da empresa, então esse passo é sem risco. Se um
> dia fizer isso com o domínio principal: **confira MX, SPF e DKIM na
> Cloudflare antes de trocar o nameserver**, senão o e-mail para.

### 4.2 Criar o túnel

1. **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**
2. tipo **Cloudflared**, dê um nome (ex.: `desktop-leads`)
3. copie o **token** (texto longo começando com `eyJ`)
4. no desktop, crie `C:\leads\.env`:

   ```
   LEADS_TUNEL_TOKEN=eyJhIjoi...
   ```

5. rode o instalador de novo — agora ele registra o serviço do túnel:

   ```powershell
   .\instalar.ps1
   ```

6. de volta no painel do túnel → **Public hostname**:
   - **Subdomain**: `leads`
   - **Domain**: seu domínio
   - **Service**: `HTTP` → `localhost:8080`

### 4.3 Limitar quem entra

**Zero Trust** → **Access** → **Applications** → **Add an application** →
**Self-hosted**:

- **Application domain**: `leads.seudominio.com.br`
- **Policy**: Action `Allow`, Include → **Emails ending in** →
  `@zebrahcontabilidade.com.br`

Pronto. A equipe abre o endereço, recebe um PIN por e-mail (a cada ~30 dias) e
entra. Nenhuma senha para você administrar, nenhuma porta aberta no roteador.

---

## 5. Testar com a equipe

- [ ] abre de fora da empresa (4G do celular)
- [ ] e-mail de fora do domínio é **bloqueado**
- [ ] busca de cidade grande responde rápido
- [ ] Excel de 100 mil linhas baixa e abre com as colunas separadas
- [ ] `http://127.0.0.1:8080/status` mostra o mês certo da Receita

---

## Rotina depois

**A equipe:** abre o site, busca, baixa planilha. Nada mais.

**Você:** nada. O job das 03:00 confere sozinho.

```
29 dias/mês   "tem pasta nova?" → não → dorme          2 segundos, ~50 KB
 1 dia/mês    sim → baixa, converte, valida, troca     ~40 min dormindo
```

De manhã a equipe já está com a base nova. A anterior fica guardada — é ela
que faz a tela **Empresas novas** ser exata.

---

## Quando dá problema

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Site fora do ar | serviço parado | `Get-Service LeadsCNPJ`; se parado, `Start-Service LeadsCNPJ` |
| Site fora e serviço rodando | túnel caído | `Get-Service LeadsCNPJ-Tunel`; veja `logs\tunel.log` |
| "Base não carregada" | primeira carga não rodou | passo 3 |
| Base velha | job falhou | `logs\atualizar-AAAA-MM.log` |
| Atualização não roda | tarefa desativada | Agendador de Tarefas → `LeadsCNPJ-Atualizar` |
| Erro "já existe uma atualização rodando" | trava de execução anterior | apague `dados\atualizar.lock` |
| Disco cheio | zips não apagados | `dir C:\leads\downloads`; apague `.zip` e `.part` |

Comandos de diagnóstico:

```powershell
cd C:\leads
Get-Service LeadsCNPJ, LeadsCNPJ-Tunel
.\.venv\Scripts\python.exe -m leads.atualizar --so-checar
Invoke-WebRequest http://127.0.0.1:8080/saude -UseBasicParsing
Get-Content logs\site.log -Tail 40
```

> Use `Get-Service`, nao `sc query`. Em PowerShell, `sc` e alias de
> `Set-Content` e engole o comando sem erro nenhum -- parece que o servico
> nao existe. Se preferir a ferramenta do Windows, chame `sc.exe query`.

---

## Atualizar o código depois

```powershell
cd C:\leads
git pull
Stop-Service LeadsCNPJ
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
Start-Service LeadsCNPJ
```

30 segundos, sem mexer nos dados.

---

## Dimensionamento

### Disco — 160 GB livres, sobra muito

| | |
|---|---|
| Base no ar (`dados\atual`) | ~5 GB |
| Base anterior (`dados\anterior`) | ~5 GB |
| Zips durante a atualização | ~7,5 GB (apagados no fim) |
| Trabalho intermediário | ~6 GB (apagado no fim) |
| Planilhas geradas | poucos GB, expiram em 7 dias |
| **Pico durante a atualização** | **~25 GB** |
| **Uso normal** | **~12 GB** |

### RAM — 8 GB é o ponto apertado, e está resolvido

O job de conversão foi reescrito **três vezes** por causa disso. As duas
primeiras versões estouravam a memória; a atual divide a junção em 10 partes
menores, e nenhuma passa de ~2 GB. Detalhe em `leads/consolidar.py`.

Tetos configurados:

| Quem | Teto |
|---|---|
| Site (horário comercial) | 2 GB |
| Conversão (03:00, sozinha) | 5 GB |

### CPU — i7-4790 dá conta

4 núcleos / 8 threads. A conversão usa 3, deixando folga. Busca é
sub-segundo: o trabalho pesado é uma vez por mês, de madrugada.

### Internet — ~400 Mbps é confortável

- **Download:** 7,3 GB uma vez por mês ≈ 3-5 min, às 03:00
- **Upload:** é o que serve o site. Planilha de 200 mil linhas ≈ 50 MB.
  Mesmo que o upload seja bem menor que o download (comum em fibra
  residencial), sobra.

> O número do fast.com é o de **download**. Para ver o upload, clique em
> "Show more info". Se o upload for **abaixo de 20 Mbps**, avise — vale
> ajustar o limite de planilhas simultâneas.

---

## O que este deploy não tem

Dito na frente para não virar surpresa:

- **Não tem alta disponibilidade.** Uma máquina. Desligada = site fora.
- **Não tem backup dos dados.** Nem precisa: a fonte é pública e o job
  reconstrói tudo em ~40 min. O que vale copiar é o `app.db` (fila de
  planilhas) e o `.env`.
- **Não tem HTTPS próprio.** Quem faz é a Cloudflare. Entre o túnel e o site
  é HTTP em `localhost`, que não sai da máquina.
- **Não tem login próprio.** É o Cloudflare Access. Tirar a política de
  acesso deixa o site aberto para a internet inteira.
