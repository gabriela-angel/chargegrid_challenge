@echo off
REM ChargeGrid Intelligence — sobe o projeto no Windows com um duplo clique (equivalente ao run.sh).
REM Pre-requisito: Python 3.11+ no PATH ("python --version"). Feche as duas janelas para encerrar.
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend

if not exist "%BACKEND%\venv" (
    echo [1/4] Criando venv do backend e instalando dependencias ^(so a 1a vez^)...
    python -m venv "%BACKEND%\venv" || goto :erro
    "%BACKEND%\venv\Scripts\pip" install -q -r "%BACKEND%\requirements.txt" || goto :erro
) else (
    echo [1/4] venv do backend ja existe - pulando.
)

if not exist "%BACKEND%\ai\model.joblib" (
    echo [2/4] Treinando o modelo de IA ^(so a 1a vez^)...
    pushd "%BACKEND%"
    venv\Scripts\python -m ai.generate_history || goto :erro
    venv\Scripts\python -m ai.train_model || goto :erro
    popd
) else (
    echo [2/4] Modelo de IA ja treinado - pulando.
)

if not exist "%FRONTEND%\venv" (
    echo [3/4] Criando venv do frontend e instalando dependencias ^(so a 1a vez^)...
    python -m venv "%FRONTEND%\venv" || goto :erro
    "%FRONTEND%\venv\Scripts\pip" install -q -r "%FRONTEND%\requirements.txt" || goto :erro
) else (
    echo [3/4] venv do frontend ja existe - pulando.
)

if not exist "%FRONTEND%\.env" (
    copy /Y "%FRONTEND%\.env.example" "%FRONTEND%\.env" >nul
    echo        Criado frontend\.env com PAYMENT_MODE=mock ^(pagamento simulado^).
)

if "%CHARGEGRID_API_KEY%"=="" (
    for /f %%k in ('python -c "import secrets; print(secrets.token_urlsafe(24))"') do set CHARGEGRID_API_KEY=%%k
)

if /I "%HARDWARE_MODE%"=="modbus" (
    echo Modo Modbus: subindo o simulador do HCA G2 na porta 5020...
    start "ChargeGrid - simulador Modbus 5020" /D "%BACKEND%" cmd /k "venv\Scripts\python -m simulator.hca_g2_sim --port 5020 --chargers 12"
)

echo [4/4] Subindo backend ^(8000^) e frontend ^(8001^) em duas janelas...
start "ChargeGrid - backend 8000" /D "%BACKEND%" cmd /k "venv\Scripts\uvicorn main:app --host 127.0.0.1 --port 8000"
timeout /t 3 >nul
start "ChargeGrid - frontend 8001" /D "%FRONTEND%\backend" cmd /k "..\venv\Scripts\uvicorn main:app --host 127.0.0.1 --port 8001"

echo.
echo ======================================================================
echo  ChargeGrid Intelligence no ar:
echo    Frontend (dashboard + app do motorista): http://127.0.0.1:8001
echo    Backend  (Swagger):                      http://127.0.0.1:8000/docs
echo  Para scripts em outro terminal:  set CHARGEGRID_API_KEY=%CHARGEGRID_API_KEY%
echo ======================================================================
goto :eof

:erro
echo.
echo Algo falhou acima. Confira a versao do Python e a conexao com a internet.
exit /b 1
