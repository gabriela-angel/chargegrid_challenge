#!/usr/bin/env bash
# ChargeGrid Intelligence — devolve o repositório ao estado "recém-clonado".
#
#   ./reset.sh            pede confirmação e apaga TUDO que o run.sh gera (venvs inclusos)
#   ./reset.sh --leve     mantém os venvs (não precisa reinstalar dependências depois)
#   ./reset.sh --env      apaga também frontend/.env  (cuidado: pode conter suas chaves do Mercado Pago)
#   ./reset.sh --sim      não pergunta nada (para scripts)
#
# O que apaga:
#   backend/venv, frontend/venv            (ambientes virtuais — só sem --leve)
#   backend/ai/model.joblib                (modelo de IA; o run.sh treina de novo — volta às métricas do histórico sintético)
#   backend/ai/data/historico_simulado.csv (histórico sintético; o run.sh gera de novo)
#   __pycache__, .pytest_cache             (caches do Python)
# O que NUNCA apaga: código, docs/, resultados do experimento, README.
# O estado em memória (clientes, sessões, histórico da demo) já zera sozinho ao reiniciar os servidores.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEVE=0; ENV=0; SIM=0
for a in "$@"; do
    case "$a" in
        --leve) LEVE=1 ;;
        --env)  ENV=1 ;;
        --sim)  SIM=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Opção desconhecida: $a (use --help)"; exit 1 ;;
    esac
done

ALVOS=(
    "$ROOT_DIR/backend/ai/model.joblib"
    "$ROOT_DIR/backend/ai/data/historico_simulado.csv"
)
[ "$LEVE" -eq 0 ] && ALVOS+=("$ROOT_DIR/backend/venv" "$ROOT_DIR/frontend/venv")
[ "$ENV" -eq 1 ] && ALVOS+=("$ROOT_DIR/frontend/.env")

echo "Será apagado:"
for t in "${ALVOS[@]}"; do [ -e "$t" ] && echo "  - ${t#$ROOT_DIR/}"; done
echo "  - todas as pastas __pycache__ e .pytest_cache"
[ "$ENV" -eq 0 ] && [ -f "$ROOT_DIR/frontend/.env" ] && echo "(frontend/.env será mantido; use --env para apagá-lo)"

if [ "$SIM" -eq 0 ]; then
    read -r -p "Continuar? [s/N] " resp
    case "$resp" in s|S|sim|SIM) ;; *) echo "Cancelado."; exit 0 ;; esac
fi

# 1. Encerra servidores do ChargeGrid que ainda estejam de pé (portas 8000, 8001 e 5020 do simulador)
python3 - <<'PY'
import os, signal
ports = {8000, 8001, 5020}
inodes = set()
for f in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        for line in open(f).read().splitlines()[1:]:
            p = line.split()
            if p[3] == "0A" and int(p[1].split(":")[1], 16) in ports:
                inodes.add(f"socket:[{p[9]}]")
    except FileNotFoundError:
        pass
for pid in filter(str.isdigit, os.listdir("/proc")) if os.path.isdir("/proc") else []:
    if int(pid) == os.getpid():
        continue
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            if os.readlink(f"/proc/{pid}/fd/{fd}") in inodes:
                os.kill(int(pid), signal.SIGTERM)
                print(f"Servidor encerrado (PID {pid})")
                break
    except Exception:
        pass
PY

# 2. Apaga os arquivos gerados
for t in "${ALVOS[@]}"; do [ -e "$t" ] && rm -rf "$t"; done
find "$ROOT_DIR" -type d \( -name "__pycache__" -o -name ".pytest_cache" \) -not -path "*/venv/*" -prune -exec rm -rf {} + 2>/dev/null

echo ""
echo "Pronto. O repositório está como recém-clonado."
echo "Rode ./run.sh para recriar tudo (venvs, modelo de IA e servidores)."
