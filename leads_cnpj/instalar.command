#!/bin/bash
# So precisa rodar isso UMA VEZ. Depois, use sempre abrir_app.command.
cd "$(dirname "$0")"

echo "Preparando o ambiente, so vai levar um minuto..."

if ! command -v python3 &> /dev/null; then
  echo ""
  echo "Nao encontrei o Python 3 no seu Mac."
  echo "Baixe e instale em https://www.python.org/downloads/ e depois rode este arquivo de novo."
  read -n 1 -s -r -p "Aperte qualquer tecla para fechar..."
  exit 1
fi

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null
pip install -r requirements.txt

echo ""
echo "Pronto! Agora e so dar dois cliques em 'abrir_app.command' sempre que quiser usar."
read -n 1 -s -r -p "Aperte qualquer tecla para fechar esta janela..."
