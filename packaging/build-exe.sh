#!/usr/bin/env bash
# Build standalone Phonos Linux executable (bundles Playground + Production + Templates + Static).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
  PIP="${ROOT}/.venv/bin/pip"
elif [[ -x "${ROOT}/venv/bin/python" ]]; then
  PY="${ROOT}/venv/bin/python"
  PIP="${ROOT}/venv/bin/pip"
else
  PY="$(command -v python3)"
  PIP="$(command -v pip3)"
fi

echo "==> 1/2 Ensuring PyInstaller is installed"
if ! "${PY}" -c "import PyInstaller" >/dev/null 2>&1; then
  "${PIP}" install --quiet "pyinstaller>=6.10"
fi

echo "==> 2/2 Building standalone executable via PyInstaller"
"${PY}" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "${ROOT}/packaging/dist" \
  --workpath "${ROOT}/packaging/build" \
  "${ROOT}/packaging/phonos.spec"

cat > "${ROOT}/packaging/dist/start-playground.sh" <<'EOF'
#!/usr/bin/env bash
exec "$(cd "$(dirname "$0")" && pwd)/phonos" --mode playground "$@"
EOF
cat > "${ROOT}/packaging/dist/start-production.sh" <<'EOF'
#!/usr/bin/env bash
exec "$(cd "$(dirname "$0")" && pwd)/phonos" --mode production "$@"
EOF
chmod +x "${ROOT}/packaging/dist/start-playground.sh" "${ROOT}/packaging/dist/start-production.sh"

echo ""
echo "Done! Standalone executable built:"
ls -lh "${ROOT}/packaging/dist/phonos"
echo "Run Playground:  ${ROOT}/packaging/dist/phonos --mode playground"
echo "Run Production:  ${ROOT}/packaging/dist/phonos --mode production"
