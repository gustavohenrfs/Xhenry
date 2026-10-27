#!/bin/sh
# Gera static/css/tailwind.css com as classes usadas em templates/index.html e static/js/app.js.
# Precisa do Node (npx). O arquivo gerado vai para o git: o VPS não precisa de Node.
# Depois de gerar, suba o ?v= do tailwind.css em templates/index.html.
set -e
cd "$(dirname "$0")/.."
npx -y tailwindcss@3.4.17 -c tailwind.config.js -i scripts/tailwind.input.css -o static/css/tailwind.css --minify
