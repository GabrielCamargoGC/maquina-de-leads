<#
    Instalador do Leads CNPJ no desktop Windows.

    Roda como Administrador. Pode rodar de novo quantas vezes quiser -- cada
    passo confere antes de agir.

    O que faz:
      1. confere Python e cria o ambiente virtual
      2. baixa nssm.exe e cloudflared.exe (fontes oficiais) em tools\
      3. desliga suspensao e hibernacao (senao o Windows dorme e o site cai)
      4. registra dois servicos: o site e o tunel
      5. agenda o job de atualizacao para 03:00, todos os dias
      6. imprime o que falta fazer a mao (Cloudflare)

    Uso:
      .\instalar.ps1
      .\instalar.ps1 -SemFerramentas     # nao baixa nssm/cloudflared
      .\instalar.ps1 -SemTunel           # nao instala o servico do tunel
#>
[CmdletBinding()]
param(
    [string]$Raiz = $PSScriptRoot,
    [int]$Porta = 8080,
    [string]$HoraJob = "03:00",
    [string]$NomeServico = "LeadsCNPJ",
    [switch]$SemFerramentas,
    [switch]$SemTunel
)

$ErrorActionPreference = "Stop"

function Info($m)  { Write-Host "  $m" }
function Passo($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  OK: $m" -ForegroundColor Green }
function Aviso($m) { Write-Host "  ATENCAO: $m" -ForegroundColor Yellow }

# Programa externo que escreve em stderr (pip avisando de versao, powercfg
# reclamando de politica, nssm dizendo que o servico nao existe) vira
# ErrorRecord no Windows PowerShell 5.1. Com $ErrorActionPreference = "Stop"
# isso derruba o script inteiro por causa de uma mensagem que nem era erro.
# Toda chamada a exe passa por aqui, com a preferencia baixada so durante a
# execucao; quem decide se falhou e o codigo de saida.
function Externo {
    param([Parameter(ValueFromRemainingArguments = $true)] $Comando)
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $exe = $Comando[0]
        $resto = @($Comando[1..($Comando.Count - 1)])
        $saida = & $exe @resto 2>&1
        # Cada linha de stderr chega como ErrorRecord; imprimir isso cru
        # despeja stack trace do PowerShell na tela. Fica so a mensagem.
        $texto = @($saida | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            } else { $_ }
        }) -join [Environment]::NewLine
        return [pscustomobject]@{
            Codigo = $LASTEXITCODE
            Saida  = $texto.Trim()
        }
    } finally {
        $ErrorActionPreference = $anterior
    }
}

# --- administrador -----------------------------------------------------------
$souAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $souAdmin) {
    Write-Host "Este script precisa de Administrador (servicos, agendador, energia)." -ForegroundColor Red
    Write-Host "Abra o PowerShell como Administrador e rode de novo." -ForegroundColor Red
    exit 1
}

$venv     = Join-Path $Raiz ".venv"
$python   = Join-Path $venv "Scripts\python.exe"
$tools    = Join-Path $Raiz "tools"
$nssm     = Join-Path $tools "nssm.exe"
$cfd      = Join-Path $tools "cloudflared.exe"
$logs     = Join-Path $Raiz "logs"
$servicoTunel = "$NomeServico-Tunel"

Write-Host "Leads CNPJ - instalacao em $Raiz" -ForegroundColor White

# --- 1. Python ---------------------------------------------------------------
Passo "1/6 Python e dependencias"

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "  Python nao encontrado." -ForegroundColor Red
    Write-Host "  Instale de https://www.python.org/downloads/ marcando" -ForegroundColor Red
    Write-Host "  'Add python.exe to PATH' e rode este script de novo." -ForegroundColor Red
    exit 1
}
Info ("encontrado: " + (Externo $py.Source "--version").Saida)

if (-not (Test-Path $python)) {
    Info "criando ambiente virtual em .venv"
    $r = Externo $py.Source "-m" "venv" $venv
    if ($r.Codigo -ne 0) {
        Write-Host "  falha ao criar o ambiente virtual:" -ForegroundColor Red
        Write-Host $r.Saida
        exit 1
    }
} else {
    Info "ambiente virtual ja existe"
}

Externo $python "-m" "pip" "install" "--upgrade" "pip" "--quiet" | Out-Null
$r = Externo $python "-m" "pip" "install" "-r" (Join-Path $Raiz "requirements.txt") "--quiet"
if ($r.Codigo -ne 0) {
    Write-Host "  falha ao instalar as dependencias:" -ForegroundColor Red
    Write-Host $r.Saida
    exit 1
}
Ok "dependencias instaladas"

New-Item -ItemType Directory -Force -Path $logs, $tools | Out-Null

# --- 2. ferramentas ----------------------------------------------------------
Passo "2/6 nssm e cloudflared"

if ($SemFerramentas) {
    Info "-SemFerramentas: pulando download"
} else {
    if (Test-Path $nssm) {
        Info "nssm.exe ja existe"
    } else {
        Info "baixando nssm (nssm.cc)"
        $zip = Join-Path $env:TEMP "nssm.zip"
        $dst = Join-Path $env:TEMP "nssm-extraido"
        Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing
        Remove-Item -Recurse -Force $dst -ErrorAction SilentlyContinue
        Expand-Archive -Path $zip -DestinationPath $dst -Force
        $achado = Get-ChildItem -Path $dst -Recurse -Filter "nssm.exe" |
                  Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
        Copy-Item $achado.FullName $nssm
        Remove-Item $zip, $dst -Recurse -Force -ErrorAction SilentlyContinue
        Ok "nssm.exe"
    }

    if ($SemTunel) {
        Info "-SemTunel: pulando cloudflared"
    } elseif (Test-Path $cfd) {
        Info "cloudflared.exe ja existe"
    } else {
        Info "baixando cloudflared (github.com/cloudflare)"
        Invoke-WebRequest -UseBasicParsing -OutFile $cfd `
            -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        Ok "cloudflared.exe"
    }
}

# --- 3. energia --------------------------------------------------------------
Passo "3/6 energia (o site cai se a maquina dormir)"

Externo powercfg "/change" "standby-timeout-ac"   "0"  | Out-Null
Externo powercfg "/change" "hibernate-timeout-ac" "0"  | Out-Null
Externo powercfg "/change" "disk-timeout-ac"      "0"  | Out-Null
Externo powercfg "/change" "monitor-timeout-ac"   "15" | Out-Null
Externo powercfg "/hibernate" "off" | Out-Null
Ok "suspensao e hibernacao desligadas (a tela ainda apaga, o PC nao dorme)"
Aviso "na BIOS, ligue 'Restore on AC Power Loss' para religar sozinho apos queda de luz"

# --- 4. servicos -------------------------------------------------------------
Passo "4/6 servicos do Windows"

if (-not (Test-Path $nssm)) {
    Aviso "sem nssm.exe, pulando servicos. Rode sem -SemFerramentas."
} else {
    # O nssm escreve em stderr em situacao normal: "Can't open service!"
    # quando o servico ainda nao existe. Dai passar tudo pelo Externo.
    function Nssm {
        param([Parameter(ValueFromRemainingArguments = $true)] $Argumentos)
        return (Externo $nssm @Argumentos).Saida
    }

    function RegistrarServico($nome, $exe, $parametros, $logBase) {
        # Get-Service em vez de "nssm status": e cmdlet, nao programa externo,
        # entao responde "nao existe" sem passar por stderr.
        if (Get-Service -Name $nome -ErrorAction SilentlyContinue) {
            Info "$nome ja existe, reconfigurando"
            Nssm stop $nome | Out-Null
        } else {
            Info "criando $nome"
            Nssm install $nome $exe $parametros | Out-Null
        }
        Nssm set $nome Application     $exe               | Out-Null
        Nssm set $nome AppParameters   $parametros        | Out-Null
        Nssm set $nome AppDirectory    $Raiz              | Out-Null
        Nssm set $nome Start           SERVICE_AUTO_START | Out-Null
        Nssm set $nome AppStdout       (Join-Path $logs "$logBase.log") | Out-Null
        Nssm set $nome AppStderr       (Join-Path $logs "$logBase.log") | Out-Null
        Nssm set $nome AppRotateFiles  1                  | Out-Null
        Nssm set $nome AppRotateBytes  10485760           | Out-Null
        Nssm set $nome AppExit Default Restart            | Out-Null
        Nssm set $nome AppRestartDelay 5000               | Out-Null
        Nssm start $nome | Out-Null

        Start-Sleep -Seconds 2
        $svc = Get-Service -Name $nome -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq "Running") {
            Ok "$nome rodando"
        } elseif ($svc) {
            Aviso "$nome registrado mas esta '$($svc.Status)'. Veja $logs\$logBase.log"
        } else {
            Aviso "$nome nao foi registrado. Veja $logs\$logBase.log"
        }
    }

    RegistrarServico $NomeServico $python "-m leads.servir --porta $Porta" "site"

    if (-not $SemTunel) {
        $tokenFile = Join-Path $Raiz ".env"
        $token = $null
        if (Test-Path $tokenFile) {
            $token = (Get-Content $tokenFile | Where-Object { $_ -match "^LEADS_TUNEL_TOKEN=" }) `
                     -replace "^LEADS_TUNEL_TOKEN=", ""
        }
        if ([string]::IsNullOrWhiteSpace($token)) {
            Aviso "sem LEADS_TUNEL_TOKEN no .env - servico do tunel NAO instalado."
            Aviso "Crie o tunel no painel da Cloudflare, ponha o token no .env e rode de novo."
        } else {
            RegistrarServico $servicoTunel $cfd `
                "tunnel --no-autoupdate run --token $token" "tunel"
        }
    }
}

# --- 5. agendador ------------------------------------------------------------
Passo "5/6 job diario das $HoraJob"

$nomeTarefa = "$NomeServico-Atualizar"
Unregister-ScheduledTask -TaskName $nomeTarefa -Confirm:$false -ErrorAction SilentlyContinue

$acao = New-ScheduledTaskAction -Execute $python `
        -Argument "-m leads.atualizar" -WorkingDirectory $Raiz
$gatilho = New-ScheduledTaskTrigger -Daily -At $HoraJob
$conf = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
        -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName $nomeTarefa -Action $acao -Trigger $gatilho `
    -Settings $conf -Principal $principal -Force | Out-Null
Ok "tarefa '$nomeTarefa' as $HoraJob (roda mesmo sem ninguem logado)"

# --- 6. conferir -------------------------------------------------------------
Passo "6/6 conferindo"

Start-Sleep -Seconds 3
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Porta/saude" -UseBasicParsing -TimeoutSec 10
    Info "site respondeu HTTP $($r.StatusCode)"
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 503) {
        Info "site no ar, mas sem base carregada ainda (esperado antes da 1a carga)"
    } else {
        Aviso "site nao respondeu: $($_.Exception.Message)"
        Aviso "veja $logs\site.log"
    }
}

Write-Host ""
Write-Host "=== Falta fazer ===" -ForegroundColor White
Write-Host ""
Write-Host "1. Primeira carga da base (2-3 h, deixe rodando):" -ForegroundColor White
Write-Host "     $python -m leads.atualizar --forcar"
Write-Host ""
Write-Host "2. Cloudflare (uma vez):" -ForegroundColor White
Write-Host "     a) painel Cloudflare > Zero Trust > Networks > Tunnels > Create"
Write-Host "     b) copie o token e ponha no arquivo .env assim:"
Write-Host "          LEADS_TUNEL_TOKEN=eyJhIjoi..."
Write-Host "     c) no tunel, Public hostname -> seu dominio, servico"
Write-Host "        HTTP -> localhost:$Porta"
Write-Host "     d) Zero Trust > Access > Applications: libere so os e-mails da equipe"
Write-Host "     e) rode este instalador de novo para registrar o servico do tunel"
Write-Host ""
Write-Host "Comandos uteis:" -ForegroundColor White
Write-Host "     sc query $NomeServico              situacao do site"
Write-Host "     $python -m leads.atualizar --so-checar   tem base nova?"
Write-Host "     logs em $logs"
Write-Host ""
