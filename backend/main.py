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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.state import controller
from routers import chargers, predictions, settings

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
        controller.tick_energy(TICK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(energy_tick_loop())
    yield
    task.cancel()


app = FastAPI(title="ChargeGrid Intelligence API", lifespan=lifespan)

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


@app.get("/")
def health_check():
    return {"status": "online", "site": controller.site_name}
