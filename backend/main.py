"""
ChargeGrid Intelligence — API (FastAPI)
------------------------------------------
Ponto de entrada da API. Roda o mesmo `DemandController` da Sprint 2,
agora exposto via HTTP em vez de terminal.

Rodar localmente:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

O --host 0.0.0.0 é IMPORTANTE: o ESP32 está numa rede WiFi separada
do seu notebook, ele precisa alcançar seu IP local (ex: 192.168.0.15),
não "localhost" (que só existe dentro da própria máquina).
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth import require_api_key, warn_if_open
from core.state import controller
from routers import billing, chargers, hardware, predictions, settings, solar, tariff

TICK_INTERVAL_SECONDS = 1.0


async def energy_tick_loop():
    """
    Loop em background: a cada TICK_INTERVAL_SECONDS, acumula energia
    em todas as sessões ativas. Roda para sempre enquanto o servidor
    estiver de pé — é isso que faz o kWh "subir sozinho" no dashboard
    e no app do motorista, sem que ninguém precise chamar nada.
    """
    while True:
        await asyncio.sleep(TICK_INTERVAL_SECONDS)
        try:
            controller.tick_energy(TICK_INTERVAL_SECONDS)
        except Exception:   # um erro num tick não pode parar o faturamento de todo mundo
            logging.getLogger("chargegrid.tick").exception("falha no tick de energia — seguindo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    warn_if_open()
    task = asyncio.create_task(energy_tick_loop())
    yield
    task.cancel()


app = FastAPI(title="ChargeGrid Intelligence API", lifespan=lifespan,
              dependencies=[Depends(require_api_key)])

# Sprint 3 (MVP): libera geral para simplificar o desenvolvimento com o
# ESP32 e os dois frontends ainda sem domínio fixo. Antes de produção,
# trocar allow_origins pelas URLs reais da Vercel.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chargers.router)
app.include_router(chargers.site_router)
app.include_router(predictions.router)
app.include_router(settings.router)
app.include_router(tariff.router)
app.include_router(solar.router)
app.include_router(hardware.router)
app.include_router(billing.router)


@app.get("/")
def health_check():
    return {"status": "online", "site": controller.site_name}
