#!/usr/bin/env bash
# ChargeGrid Intelligence — sobe o projeto inteiro com um comando só.
#
#   ./run.sh
#
# O que faz, na ordem:
#   1. Cria os venvs de backend/ e frontend/ (só na primeira vez)
#   2. Instala as dependências de cada um (só se algo mudou)
#   3. Gera o histórico sintético e treina o modelo de IA (só na primeira
#      vez — depois disso, ai/model.joblib já existe e fica só reusando)
#   4. Sobe os dois servidores (backend na 8000, frontend na 8001)
#   5. Ctrl+C encerra os dois de uma vez (nada de processo órfão travando a porta)
#
# Pré-requisito: Python 3.11+ instalado (`python3 --version`).

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend/backend"

# ----------------------------------------------------------------------
# 1-2. Setup do backend
# ----------------------------------------------------------------------
if [ ! -d "$BACKEND_DIR/venv" ]; then
    echo "[1/4] Criando venv do backend e instalando dependências (só a 1ª vez)..."
    python3 -m venv "$BACKEND_DIR/venv"
    "$BACKEND_DIR/venv/bin/pip" install -q -r "$BACKEND_DIR/requirements.txt"
else
    echo "[1/4] venv do backend já existe — pulando instalação."
fi

# ----------------------------------------------------------------------
# 3. Modelo de IA — gera histórico sintético + treina, se ainda não existir
# ----------------------------------------------------------------------
if [ ! -f "$BACKEND_DIR/ai/model.joblib" ]; then
    echo "[2/4] Modelo de IA não encontrado — gerando histórico e treinando (só a 1ª vez)..."
    (cd "$BACKEND_DIR" && venv/bin/python -m ai.generate_history && venv/bin/python -m ai.train_model)
else
    echo "[2/4] Modelo de IA já treinado (ai/model.joblib) — pulando."
    echo "       Pra retreinar do zero: apague backend/ai/model.joblib e rode ./run.sh de novo."
fi

# ----------------------------------------------------------------------
# Setup do frontend
# ----------------------------------------------------------------------
if [ ! -d "$ROOT_DIR/frontend/venv" ]; then
    echo "[3/4] Criando venv do frontend e instalando dependências (só a 1ª vez)..."
    python3 -m venv "$ROOT_DIR/frontend/venv"
    "$ROOT_DIR/frontend/venv/bin/pip" install -q -r "$ROOT_DIR/frontend/requirements.txt"
else
    echo "[3/4] venv do frontend já existe — pulando instalação."
fi

# ----------------------------------------------------------------------
# 4. Sobe os dois servidores
# ----------------------------------------------------------------------
SIM_PID=""
cleanup() {
    echo ""
    echo "Encerrando backend (PID $BACKEND_PID) e frontend (PID $FRONTEND_PID)..."
    kill "$BACKEND_PID" "$FRONTEND_PID" $SIM_PID 2>/dev/null
    wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
    echo "Encerrado."
    exit 0
}
trap cleanup SIGINT SIGTERM

check_port() {
    local port="$1"
    if python3 -c 'import socket, sys; s = socket.socket(); result = s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); raise SystemExit(result != 0)' "$port"; then
        echo "Erro: a porta $port já está em uso. Encerre o processo que a ocupa e rode ./run.sh novamente."
        exit 1
    fi
}

check_port 8000
check_port 8001

# Pagamento: sem frontend/.env o app do motorista tentaria o Pix real. Cria um .env de avaliação.
if [ ! -f "$ROOT_DIR/frontend/.env" ]; then
    cp "$ROOT_DIR/frontend/.env.example" "$ROOT_DIR/frontend/.env"
    echo "       Criado frontend/.env a partir do .env.example (PAYMENT_MODE=mock — pagamento simulado)."
fi

# Chave de API compartilhada entre backend, frontend e scripts. Se você não definiu
# CHARGEGRID_API_KEY, geramos uma aleatória só para esta execução (backend nunca fica aberto).
if [ -z "$CHARGEGRID_API_KEY" ]; then
    CHARGEGRID_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
export CHARGEGRID_API_KEY

# HARDWARE_MODE=modbus ./run.sh  → sobe também o simulador do HCA G2 (Modbus TCP, porta 5020)
# e o backend passa a escrever os limites de potência nos registradores de verdade.
if [ "$HARDWARE_MODE" = "modbus" ]; then
    check_port 5020
    echo "[4/4] Modo Modbus: subindo o simulador do HCA G2 na porta 5020..."
    (cd "$BACKEND_DIR" && exec venv/bin/python -m simulator.hca_g2_sim --port 5020 --chargers 12 >/dev/null) &
    SIM_PID=$!
    sleep 1
fi

echo "[4/4] Subindo backend (8000) e frontend (8001)..."
(cd "$BACKEND_DIR" && exec venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000) &
BACKEND_PID=$!

sleep 2  # dá tempo do backend subir antes do frontend tentar falar com ele

(cd "$FRONTEND_DIR" && exec ../venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001) &
FRONTEND_PID=$!

sleep 1
echo ""
echo "======================================================================"
echo " ChargeGrid Intelligence está no ar:"
echo ""
echo "   Backend real (docs interativos, é aqui que o ESP32 conecta):"
echo "     http://127.0.0.1:8000/docs"
echo ""
echo "   Frontend (dashboard do operador + app do motorista):"
echo "     http://127.0.0.1:8001"
echo ""
echo "   Para rodar os scripts de demo / simulate_esp32.py em outro terminal:"
echo "     export CHARGEGRID_API_KEY=$CHARGEGRID_API_KEY"
echo ""
echo " Ctrl+C encerra os dois servidores."
echo "======================================================================"
echo ""

wait "$BACKEND_PID" "$FRONTEND_PID"
