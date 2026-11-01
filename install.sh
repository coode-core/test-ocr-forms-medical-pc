#!/usr/bin/env sh
# macOS/Linux installer: uv -> Python 3.12 -> dependencies -> Chromium (optional engine).
# Usage: ./install.sh [--skip-browser]
set -eu
export PYTHONUTF8=1
cd "$(dirname "$0")"
echo "== Plan de Cuidado: instalación =="

if ! command -v uv >/dev/null 2>&1; then
  echo "Instalando uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi

echo "Instalando Python 3.12..."
uv python install 3.12
echo "Instalando dependencias..."
uv sync --frozen 2>/dev/null || uv sync

if [ "${1:-}" != "--skip-browser" ]; then
  echo "Instalando Chromium para el motor 'browser' (opcional)..."
  uv run playwright install chromium || echo "Chromium no se instaló; el motor http sigue funcionando."
fi

[ -f .env ] || cp .env.example .env
chmod +x pc
uv run pc --help >/dev/null

echo ""
echo "Instalación completa."
echo "Siguiente paso: ponga credentials.json en esta carpeta y ejecute:  ./pc setup"
