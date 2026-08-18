#!/bin/bash
# Clique duplo neste arquivo sempre que quiser usar o app.
# Ele abre uma pagina no seu navegador sozinho.
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Ainda nao instalei o ambiente. Rode 'instalar.command' primeiro (uma vez so)."
  read -n 1 -s -r -p "Aperte qualquer tecla para fechar..."
  exit 1
fi

source venv/bin/activate
echo "Abrindo o app no navegador... nao feche esta janela enquanto estiver usando."
python3 app.py
