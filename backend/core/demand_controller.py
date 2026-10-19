"""
ChargeGrid Intelligence — Demand Controller
-------------------------------------------
Camada de inteligência central: gerencia a potência disponível
e redistribui entre os carregadores conectados em tempo real.

Projetado para ser desacoplado da interface (terminal, web, API).
Toda lógica de negócio fica aqui — a interface apenas chama os métodos.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
import logging

from core.solar import (DEFAULT_PV_PEAK_KW, GRID_EMISSION_KG_PER_KWH, hour_float,
                        pv_power_kw)
from core.tariff import TariffConfig, price_for, validate as validate_tariff

# Mapa de registradores do hardware real (GoodWe HCA G2)
# Na Sprint 2: usado como referência documental (simulação interna)
# Na Sprint 3+: usar com pymodbus para comunicação real
#
#   from core.modbus_map import HCA_G2, power_to_register
#   client = ModbusTcpClient(charger_ip, port=HCA_G2.MODBUS_TCP_PORT)
#   client.write_register(HCA_G2.WRITE.MAX_CHARGING_POWER, power_to_register(allocated_kw))
#
# Registradores chave para este módulo:
#   Leitura:  HCA_G2.READ.CHARGING_POWER (10015)  → potência atual do carregador
#             HCA_G2.READ.CHARGER_STATUS (10017)   → estado atual
#             HCA_G2.READ.CAR_CONN_STATUS (10075)  → carro conectado?
#   Escrita:  HCA_G2.WRITE.MAX_CHARGING_POWER (10029) ★ redistribuição de potência
#             HCA_G2.WRITE.EMS_DISPATCH (10000)       → throttle de emergência
try:
    from core.modbus_map import HCA_G2, power_to_register, ChargerStatus
    MODBUS_MAP_AVAILABLE = True
except ImportError:
    MODBUS_MAP_AVAILABLE = False

logger = logging.getLogger("chargegrid.controller")

MAX_EVENTS = 1000   # eventos guardados em memória (os mais recentes)

# Limite de uso da rede antes de o controlador REDUZIR a potência dos
# carregadores, em % da demanda contratada. É o inverso da margem de
# segurança: 90% de limite de uso = 10% de margem. Antes era fixo em 95%
# (margem de 5%) — agora o operador pode ajustar pelo dashboard, dentro
# desta faixa. 95% continua sendo o teto (nunca menos que 5% de margem).
DEFAULT_USABLE_LIMIT_PCT = 90.0
DEFAULT_SAFETY_MARGIN_PCT = round(1 - DEFAULT_USABLE_LIMIT_PCT / 100, 4)  # 0.10 (round evita 0.0999...98)
MIN_USABLE_LIMIT_PCT = 50.0
MAX_USABLE_LIMIT_PCT = 95.0


@dataclass
class ChargingSession:
    """Representa uma sessão de recarga ativa."""
    charger_id: str
    vehicle_id: str
    started_at: datetime
    allocated_kw: float = 0.0
    energy_consumed_kwh: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)
    # Preenchido quando a sessão fecha (vehicle_disconnect) ou quando ela é
    # importada já concluída (import_completed_sessions). Sem isto,
    # duration_minutes() media "agora - início" também para sessões JÁ
    # ENCERRADAS, e a duração delas no histórico crescia sozinha com o tempo.
    ended_at: Optional[datetime] = None
    # Preço TRAVADO na conexão (R$/kWh): o que o motorista viu é o que paga.
    tariff_rs_per_kwh: float = 0.0
    # Parte da energia que veio do solar (kWh) — o restante veio da rede.
    green_kwh: float = 0.0

    @property
    def cost_rs(self) -> float:
        return self.energy_consumed_kwh * self.tariff_rs_per_kwh

    def duration_minutes(self) -> float:
        end = self.ended_at or datetime.now()
        return (end - self.started_at).total_seconds() / 60

    def update_energy(self, interval_seconds: float = 1.0, green_fraction: float = 0.0):
        """Acumula energia consumida com base na potência alocada.
        `green_fraction` (0–1) é a parcela desse instante coberta pelo solar."""
        delta_kwh = (self.allocated_kw * interval_seconds) / 3600
        self.energy_consumed_kwh += delta_kwh
        self.green_kwh += delta_kwh * max(0.0, min(1.0, green_fraction))
        self.last_updated = datetime.now()


@dataclass
class Charger:
    """Representa um ponto de recarga físico."""
    charger_id: str
    max_kw: float          # Potência máxima suportada pelo hardware
    location: str          # Ex: "Vaga A1", "Entrada Principal"
    session: Optional[ChargingSession] = None

    @property
    def is_occupied(self) -> bool:
        return self.session is not None

    @property
    def current_kw(self) -> float:
        return self.session.allocated_kw if self.session else 0.0


class DemandController:
    """
    Controlador central de demanda.

    Recebe eventos (conexão/desconexão de veículos) e redistribui
    a potência disponível entre os carregadores ativos, respeitando
    o limite contratado com a distribuidora.

    Desacoplado da interface — pode ser usado por terminal, API REST,
    ou qualquer camada de apresentação futura.
    """

    def __init__(self, site_name: str, contracted_limit_kw: float,
                 safety_margin_pct: float = DEFAULT_SAFETY_MARGIN_PCT):
        """
        Args:
            site_name: Nome do estabelecimento comercial.
            contracted_limit_kw: Demanda contratada com a distribuidora (kW).
            safety_margin_pct: Margem de segurança (padrão 10%, ou seja, limite de uso
                de 90%) para não ultrapassar o limite. Ajustável em tempo de execução
                via update_settings().
        """
        self.site_name = site_name
        self.contracted_limit_kw = contracted_limit_kw
        self.safety_margin_pct = safety_margin_pct
        self.chargers: dict[str, Charger] = {}
        self.completed_sessions: list[ChargingSession] = []
        self.events: list[dict] = []  # Log de eventos para futura integração com frontend

        # --- Solar simulado + tarifa dinâmica (Sprint 4) ---
        self.pv_peak_kw: float = DEFAULT_PV_PEAK_KW
        self.sim_hour: Optional[float] = None      # None = relógio real; número = hora "congelada" (demo)
        self.tariff_config = TariffConfig()
        # Função (datetime -> kW previstos). Injetada em core/state.py para este
        # módulo não depender do pacote de IA (acoplamento fraco).
        self.forecast_provider: Optional[Callable[[datetime], float]] = None
        self._last_budget_kw: float = 0.0
        # Série de demanda REAL do site: (data, hora) -> [soma de kW, nº de amostras].
        # Alimenta o re-treino da IA com dado real (ver ai/train_model.py).
        self.demand_log: dict[tuple, list[float]] = {}

        # --- Hardware via Modbus TCP (Sprint 4) ---
        # None = sem hardware: o controle roda só em memória (como na Sprint 3).
        # Injetado em core/state.py a partir das variáveis HARDWARE_*.
        self.hardware = None                       # core.hardware.HardwareLink | None
        self._hw_limit_sent: dict[str, int] = {}   # último valor gravado no 10029, por carregador
        self._hw_on: set[str] = set()              # carregadores que já receberam "ligar" (10060)
        self._hw_off_pending: set[str] = set()     # "desligar" que falhou e precisa ser repetido
        self._hw_error_logged = False              # evita repetir o mesmo erro no console

    # ------------------------------------------------------------------
    # Propriedades calculadas
    # ------------------------------------------------------------------

    @property
    def usable_limit_kw(self) -> float:
        """Potência máxima utilizável (limite contratado menos margem de segurança)."""
        return self.contracted_limit_kw * (1 - self.safety_margin_pct)

    @property
    def usable_limit_pct(self) -> float:
        """Limite de uso em % da demanda contratada (inverso da margem de segurança)."""
        return round((1 - self.safety_margin_pct) * 100, 2)

    @property
    def active_sessions(self) -> list[ChargingSession]:
        return [c.session for c in self.chargers.values() if c.session]

    @property
    def total_allocated_kw(self) -> float:
        return sum(s.allocated_kw for s in self.active_sessions)

    @property
    def available_kw(self) -> float:
        return max(0.0, self.budget_kw - self.total_allocated_kw)

    @property
    def utilization_pct(self) -> float:
        """Uso da demanda CONTRATADA: considera só o que vem da rede (solar fica de fora)."""
        if self.contracted_limit_kw == 0:
            return 0.0
        return (self.grid_draw_kw / self.contracted_limit_kw) * 100

    # ------------------------------------------------------------------
    # Solar, orçamento de potência e preço
    # ------------------------------------------------------------------

    def clock_now(self) -> datetime:
        """'Agora' do sistema: relógio real, ou a hora congelada pela demo."""
        real = datetime.now()
        if self.sim_hour is None:
            return real
        h = int(self.sim_hour) % 24
        m = int(round((self.sim_hour - int(self.sim_hour)) * 60)) % 60
        return real.replace(hour=h, minute=m, second=0, microsecond=0)

    def pv_kw_at(self, when: datetime) -> float:
        return pv_power_kw(hour_float(when), self.pv_peak_kw)

    @property
    def pv_kw(self) -> float:
        return self.pv_kw_at(self.clock_now())

    @property
    def budget_kw(self) -> float:
        """Potência total distribuível aos carregadores: limite útil da rede + solar."""
        return self.usable_limit_kw + self.pv_kw

    @property
    def grid_draw_kw(self) -> float:
        """Quanto os carregadores puxam DA REDE (o que o solar não cobre)."""
        return max(0.0, self.total_allocated_kw - self.pv_kw)

    def _forecast_kw(self, when: datetime) -> float:
        if self.forecast_provider is None:
            return 0.0
        try:
            return float(self.forecast_provider(when))
        except Exception:  # modelo ausente/indisponível: tarifa cai para pressão ao vivo
            return 0.0

    def pricing_at(self, when: datetime, live: bool = False) -> dict:
        """
        Preço do kWh num instante. Pressão = demanda LÍQUIDA da rede (previsão da
        IA menos solar) em % do limite útil. Com live=True considera também a
        carga real do site agora — se ela for maior que a prevista, vale a maior.
        """
        pv = self.pv_kw_at(when)
        limit = self.usable_limit_kw or 1.0
        predicted = self._forecast_kw(when)
        pressure = 100 * max(0.0, predicted - pv) / limit
        if live:
            pressure = max(pressure, 100 * self.grid_draw_kw / limit)
        out = price_for(self.tariff_config, pressure)
        out.update({
            "timestamp": when.replace(minute=0, second=0, microsecond=0).isoformat(),
            "hour": when.hour,
            "predicted_kw": round(predicted, 2),
            "solar_kw": round(pv, 2),
            "base_rs_per_kwh": self.tariff_config.base_rs_per_kwh,
        })
        return out

    def pricing_now(self) -> dict:
        return self.pricing_at(self.clock_now(), live=True)

    def pricing_forecast(self, n: int = 24) -> list[dict]:
        """Preço hora a hora nas próximas `n` horas (meio de cada hora)."""
        start = self.clock_now().replace(minute=30, second=0, microsecond=0)
        return [self.pricing_at(start + timedelta(hours=i)) for i in range(n)]

    def update_tariff_config(self, **fields) -> dict:
        """Altera a configuração da tarifa (só os campos informados). Atômico.
        Mudar um limiar de pressão À MÃO desliga a calibração automática pela IA;
        `pressure_auto=True` religa e recalibra na hora."""
        fields = {k: v for k, v in fields.items() if v is not None}
        current = self.tariff_config
        manual = any(k in fields and fields[k] != getattr(current, k)
                     for k in ("low_pressure_pct", "high_pressure_pct"))
        if manual and "pressure_auto" not in fields:
            fields["pressure_auto"] = False
        candidate = TariffConfig(**{**current.__dict__, **fields})
        error = validate_tariff(candidate)
        if error:
            return self._error(error)
        self.tariff_config = candidate
        self._log_event("TARIFF_CONFIG_CHANGED", "SYSTEM",
                        f"Tarifa-base R$ {candidate.base_rs_per_kwh:.2f}/kWh | "
                        f"multiplicador {candidate.min_multiplier:g}×–{candidate.max_multiplier:g}×")
        if fields.get("pressure_auto") is True:
            self.calibrate_tariff_pressure()
        return self.tariff_config.as_dict()

    def calibrate_tariff_pressure(self) -> Optional[dict]:
        """
        Calibra os limiares de pressão da tarifa a partir da PRÓPRIA previsão da IA:
        olha uma semana inteira de previsão (já descontado o solar) e usa o percentil 10
        como "rede tranquila" e o 90 como "rede cheia". Assim o preço mínimo vale para
        as horas mais vazias e o máximo para as mais cheias do estabelecimento — sem o
        operador precisar entender de pressão. Não faz nada se a calibração automática
        está desligada ou se não há modelo treinado. Devolve {"low", "high"} ou None.
        """
        if not self.tariff_config.pressure_auto or self.forecast_provider is None:
            return None
        start = datetime.now().replace(minute=0, second=0, microsecond=0)
        pressures = []
        for h in range(7 * 24):
            when = start + timedelta(hours=h)
            net_kw = max(0.0, self._forecast_kw(when) - self.pv_kw_at(when))
            pressures.append(100.0 * net_kw / self.usable_limit_kw)
        pressures.sort()
        if pressures[-1] < 5:          # sem modelo (previsão zerada): mantém o que já está
            return None
        low = max(5, round(pressures[int(0.10 * (len(pressures) - 1))]))   # piso: "tranquila até 0%" não faz sentido
        high = round(pressures[int(0.90 * (len(pressures) - 1))])
        high = max(high, low + 10)     # garante uma faixa de subida mínima
        candidate = TariffConfig(**{**self.tariff_config.__dict__,
                                    "low_pressure_pct": float(low), "high_pressure_pct": float(high)})
        if validate_tariff(candidate):
            return None
        self.tariff_config = candidate
        self._log_event("TARIFF_CALIBRATED", "SYSTEM",
                        f"Limiares de pressão calibrados pela IA: {low}% (tranquila) a {high}% (cheia)")
        return {"low": low, "high": high}

    def update_solar(self, peak_kw: Optional[float] = None, sim_hour: Optional[float] = None,
                     set_sim_hour: bool = False) -> dict:
        """Ajusta a potência FV instalada e/ou congela a hora solar (demo).
        `set_sim_hour=True` + `sim_hour=None` volta ao relógio real."""
        if peak_kw is not None and not (0 <= peak_kw <= 1000):
            return self._error("A potência FV deve ficar entre 0 e 1000 kWp.")
        if set_sim_hour and sim_hour is not None and not (0 <= sim_hour < 24):
            return self._error("A hora simulada deve estar entre 0 e 24.")
        if peak_kw is not None:
            self.pv_peak_kw = peak_kw
        if set_sim_hour:
            self.sim_hour = sim_hour
        self._log_event("SOLAR_CONFIG_CHANGED", "SYSTEM",
                        f"FV {self.pv_peak_kw:g} kWp | hora "
                        f"{'real' if self.sim_hour is None else f'simulada {self.sim_hour:g}h'} "
                        f"({self.pv_kw:.1f} kW agora)")
        self._redistribute_power(log=False)
        return self._state_snapshot()

    # --- Métricas de energia verde ---

    def _all_sessions(self) -> list[ChargingSession]:
        return self.completed_sessions + self.active_sessions

    @property
    def total_energy_kwh(self) -> float:
        return sum(s.energy_consumed_kwh for s in self._all_sessions())

    @property
    def total_green_kwh(self) -> float:
        return sum(s.green_kwh for s in self._all_sessions())

    @property
    def green_share_pct(self) -> float:
        total = self.total_energy_kwh
        return 100 * self.total_green_kwh / total if total > 0 else 0.0

    @property
    def co2_avoided_kg(self) -> float:
        return self.total_green_kwh * GRID_EMISSION_KG_PER_KWH

    # --- Série de demanda real (para re-treinar a IA) ---

    def _log_demand(self, kw: float) -> None:
        now = datetime.now()
        bucket = self.demand_log.setdefault((now.date(), now.hour), [0.0, 0])
        bucket[0] += kw
        bucket[1] += 1

    def hourly_demand_samples(self, min_samples: int = 60) -> list[dict]:
        """Média de demanda por (dia, hora) com pelo menos `min_samples` amostras."""
        return [
            {"hour": hour, "day_of_week": day.weekday(), "demand_kw": total / n}
            for (day, hour), (total, n) in sorted(self.demand_log.items())
            if n >= min_samples
        ]

    def session_demand_samples(self) -> list[dict]:
        """
        Demanda média por (dia, hora) reconstruída das sessões JÁ CONCLUÍDAS — o
        histórico (real ou importado) vira dado de treino da IA. Cada sessão
        contribui com a potência média dela (kWh ÷ horas) proporcional ao tempo que
        ocupou cada hora; somando todas as sessões, sai a "potência total do site
        naquela hora" — a mesma definição do alvo do histórico sintético.
        Só dias COMPLETOS entram (hoje fica de fora: as horas que ainda não
        aconteceram contariam como demanda zero) e, nesses dias, as 24 horas
        (hora sem sessão = 0 kW, que também é informação).
        """
        today = datetime.now().date()
        by_slot: dict[tuple, float] = {}
        days: set = set()
        for session in self.completed_sessions:
            if session.ended_at is None:
                continue
            hours = (session.ended_at - session.started_at).total_seconds() / 3600
            if hours <= 0:
                continue
            avg_kw = session.energy_consumed_kwh / hours
            t = session.started_at
            while t < session.ended_at:
                slot = t.replace(minute=0, second=0, microsecond=0)
                seg_end = min(slot + timedelta(hours=1), session.ended_at)
                key = (slot.date(), slot.hour)
                by_slot[key] = by_slot.get(key, 0.0) + avg_kw * (seg_end - t).total_seconds() / 3600
                days.add(slot.date())
                t = seg_end
        return [
            {"hour": hour, "day_of_week": day.weekday(), "demand_kw": by_slot.get((day, hour), 0.0)}
            for day in sorted(d for d in days if d < today)
            for hour in range(24)
        ]

    def billing_series(self, period: str, now: Optional[datetime] = None) -> dict:
        """
        Faturamento agrupado em barras, para o gráfico do dashboard.

          period="day"   → 24 barras (uma por hora de HOJE)
          period="week"  → 7 barras (um dia cada, terminando hoje)
          period="month" → 30 barras (um dia cada, terminando hoje)

        Conta as sessões concluídas (no instante em que terminaram) e as em andamento
        (o que já foi consumido, na barra de agora). A comparação é com o período
        anterior de mesmo tamanho; no "day", com ontem ATÉ esta mesma hora.
        """
        if period not in ("day", "week", "month"):
            raise ValueError("period deve ser 'day', 'week' ou 'month'.")
        now = now or datetime.now()
        midnight = datetime.combine(now.date(), datetime.min.time())
        if period == "day":
            n, step = 24, timedelta(hours=1)
            start = midnight
            prev_start, prev_end = start - timedelta(days=1), now - timedelta(days=1)
            compare_label = "ontem até esta hora"
        else:
            n, step = (7, timedelta(days=1)) if period == "week" else (30, timedelta(days=1))
            start = midnight - timedelta(days=n - 1)
            prev_start, prev_end = start - n * step, start
            compare_label = f"{n} dias anteriores"
        end = start + n * step

        buckets = [{"start": (start + i * step).isoformat(timespec="minutes"),
                    "revenue_rs": 0.0, "kwh": 0.0, "sessions": 0} for i in range(n)]
        previous = 0.0
        for session in self._all_sessions():
            stamp = session.ended_at or now
            if start <= stamp < end:
                b = buckets[min(n - 1, int((stamp - start) / step))]
                b["revenue_rs"] += session.cost_rs
                b["kwh"] += session.energy_consumed_kwh
                b["sessions"] += 1
            elif prev_start <= stamp < prev_end:
                previous += session.cost_rs
        for b in buckets:
            b["revenue_rs"] = round(b["revenue_rs"], 2)
            b["kwh"] = round(b["kwh"], 3)

        total = round(sum(b["revenue_rs"] for b in buckets), 2)
        return {
            "period": period,
            "unit": "hour" if period == "day" else "day",
            "buckets": buckets,
            "current_index": min(n - 1, int((now - start) / step)),
            "total_rs": total,
            "total_kwh": round(sum(b["kwh"] for b in buckets), 3),
            "sessions": sum(b["sessions"] for b in buckets),
            "previous_total_rs": round(previous, 2),
            "delta_pct": round((total - previous) / previous * 100, 1) if previous > 0 else None,
            "compare_label": compare_label,
        }

    # ------------------------------------------------------------------
    # Gerenciamento de carregadores
    # ------------------------------------------------------------------

    def register_charger(self, charger_id: str, max_kw: float, location: str):
        """Registra um novo ponto de recarga na rede."""
        self.chargers[charger_id] = Charger(charger_id, max_kw, location)
        self._log_event("CHARGER_REGISTERED", charger_id, f"Registrado em '{location}' | Máx: {max_kw} kW")

    def update_settings(self, contracted_limit_kw: Optional[float] = None,
                        usable_limit_pct: Optional[float] = None) -> dict:
        """
        Ajusta a demanda contratada e/ou o limite de uso (em % do contratado)
        em tempo de execução, e redistribui a potência dos carregadores ativos
        na hora — sem isso, as sessões já em andamento continuariam com a
        alocação calculada com o limite antigo até o próximo conectar/desconectar.

        Valida tudo ANTES de alterar qualquer coisa (se um dos dois valores for
        inválido, nenhum dos dois é aplicado). Devolve o snapshot de estado em
        caso de sucesso ou {"error": ...} — mesmo padrão de vehicle_connect().
        """
        if contracted_limit_kw is not None and contracted_limit_kw <= 0:
            return self._error("A demanda contratada deve ser maior que 0 kW.")
        if usable_limit_pct is not None and not (MIN_USABLE_LIMIT_PCT <= usable_limit_pct <= MAX_USABLE_LIMIT_PCT):
            return self._error(
                f"O limite de uso deve ficar entre {MIN_USABLE_LIMIT_PCT:.0f}% e {MAX_USABLE_LIMIT_PCT:.0f}%."
            )

        old_kw, old_pct = self.contracted_limit_kw, self.usable_limit_pct
        if contracted_limit_kw is not None:
            self.contracted_limit_kw = contracted_limit_kw
        if usable_limit_pct is not None:
            self.safety_margin_pct = round(1 - usable_limit_pct / 100, 4)

        self._log_event(
            "SETTINGS_CHANGED", "SYSTEM",
            f"Demanda contratada {old_kw:g} → {self.contracted_limit_kw:g} kW | "
            f"Limite de uso {old_pct:g}% → {self.usable_limit_pct:g}% "
            f"({self.usable_limit_kw:.1f} kW utilizáveis)"
        )
        self._redistribute_power()
        return self._state_snapshot()

    # ------------------------------------------------------------------
    # Eventos de sessão
    # ------------------------------------------------------------------

    def vehicle_connect(self, charger_id: str, vehicle_id: str,
                        tariff_rs_per_kwh: Optional[float] = None) -> dict:
        """
        Processa a conexão de um veículo.
        Redistribui a potência entre todos os carregadores ativos.
        Retorna o estado resultante para a interface exibir.

        `tariff_rs_per_kwh`: preço que o motorista viu e aceitou (fica travado na
        sessão). Sem ele, vale o preço dinâmico deste instante. Um valor informado
        é limitado à faixa [mínimo, máximo] da configuração vigente.
        """
        charger = self._get_charger(charger_id)

        if charger.is_occupied:
            return self._error(f"Carregador {charger_id} já está ocupado.")

        cfg = self.tariff_config
        if tariff_rs_per_kwh is None:
            locked = self.pricing_now()["price_rs_per_kwh"]
        else:
            locked = max(cfg.base_rs_per_kwh * cfg.min_multiplier,
                         min(cfg.base_rs_per_kwh * cfg.max_multiplier, float(tariff_rs_per_kwh)))
            locked = round(locked, 2)

        session = ChargingSession(
            charger_id=charger_id,
            vehicle_id=vehicle_id,
            started_at=datetime.now(),
            tariff_rs_per_kwh=locked,
        )
        charger.session = session
        self._log_event("VEHICLE_CONNECTED", charger_id,
                        f"Veículo {vehicle_id} conectado | tarifa travada R$ {locked:.2f}/kWh")

        self._redistribute_power()
        return self._state_snapshot()

    def vehicle_disconnect(self, charger_id: str) -> dict:
        """
        Processa a desconexão de um veículo.
        Libera a potência e a redistribui para os demais carregadores ativos.
        """
        charger = self._get_charger(charger_id)

        if not charger.is_occupied:
            return self._error(f"Carregador {charger_id} não tem sessão ativa.")

        session = charger.session
        session.update_energy()
        session.ended_at = datetime.now()
        self.completed_sessions.append(session)
        charger.session = None

        self._log_event(
            "VEHICLE_DISCONNECTED", charger_id,
            f"Veículo {session.vehicle_id} desconectado | "
            f"{session.energy_consumed_kwh:.3f} kWh consumidos | "
            f"{session.duration_minutes():.1f} min | R$ {session.cost_rs:.2f}"
        )

        self._redistribute_power()
        self._release_hardware(charger_id)
        return self._state_snapshot()

    def import_completed_sessions(self, items: list[dict]) -> dict:
        """
        Importa sessões JÁ CONCLUÍDAS (histórico) sem passar pelo fluxo de
        conexão — usado para popular a demo com um dia "vivido" (clientes
        cadastrados com recargas anteriores) e serve igual para uma migração
        futura de histórico de outro sistema.

        Cada item: {charger_id, vehicle_id, started_at (datetime),
        duration_min, energy_kwh}. Não mexe em nenhum carregador nem redistribui
        potência (a sessão já acabou). Idempotente: sessão com o mesmo
        (charger_id, vehicle_id, started_at) já existente é ignorada.
        Devolve {"imported": n, "skipped": m}.
        """
        for item in items:  # valida TUDO antes de importar qualquer coisa (atômico)
            self._get_charger(item["charger_id"])  # levanta ValueError se o carregador não existe
            if item["duration_min"] < 0 or item["energy_kwh"] < 0:
                raise ValueError("duration_min e energy_kwh não podem ser negativos.")

        existing = {(s.charger_id, s.vehicle_id, s.started_at) for s in self.completed_sessions}
        imported = skipped = 0
        for item in items:
            key = (item["charger_id"], item["vehicle_id"], item["started_at"])
            if key in existing:
                skipped += 1
                continue
            started_at = item["started_at"]
            duration_min = item["duration_min"]
            energy_kwh = item["energy_kwh"]
            tariff = item.get("tariff_rs_per_kwh")
            if tariff is None:  # histórico sem preço: reconstrói o preço dinâmico da hora
                tariff = self.pricing_at(started_at)["price_rs_per_kwh"]
            green = item.get("green_kwh")
            if green is None:   # histórico sem origem da energia: estima pelo solar do meio da sessão
                avg_kw = energy_kwh / (duration_min / 60) if duration_min > 0 else 0.0
                mid = started_at + timedelta(minutes=duration_min / 2)
                # O solar é dividido com as OUTRAS sessões do site: compara com o maior entre a
                # potência desta sessão e a demanda típica prevista (estimativa conservadora).
                basis = max(avg_kw, self._forecast_kw(mid))
                green = energy_kwh * min(1.0, self.pv_kw_at(mid) / basis) if basis > 0 else 0.0
            self.completed_sessions.append(ChargingSession(
                charger_id=item["charger_id"],
                vehicle_id=item["vehicle_id"],
                started_at=started_at,
                energy_consumed_kwh=energy_kwh,
                ended_at=started_at + timedelta(minutes=duration_min),
                tariff_rs_per_kwh=tariff,
                green_kwh=green,
            ))
            existing.add(key)
            imported += 1
        # o histórico é exibido "do mais recente pro mais antigo" invertendo a
        # lista — então precisa continuar em ordem cronológica de início
        self.completed_sessions.sort(key=lambda s: s.started_at)
        if imported:
            self._log_event("HISTORY_IMPORTED", "SYSTEM", f"{imported} sessão(ões) concluída(s) importada(s) para o histórico")
        return {"imported": imported, "skipped": skipped}

    def tick_energy(self, interval_seconds: float = 1.0):
        """
        Acumula energia consumida em TODAS as sessões ativas com base
        na potência atualmente alocada a cada uma.

        Chamado periodicamente por um loop em background no FastAPI
        (ex: a cada 1s), e não por eventos externos — assim o cálculo de
        kWh não depende de nenhum hardware "avisar" o consumo, e funciona
        igual tanto para o carregador real (ESP32) quanto para os
        simulados em memória.
        """
        # O solar varia ao longo do dia → o orçamento muda sem ninguém conectar/desconectar.
        if abs(self.budget_kw - self._last_budget_kw) > 0.25:
            self._redistribute_power(log=False)

        total = self.total_allocated_kw
        green_fraction = min(1.0, self.pv_kw / total) if total > 0 else 0.0
        for session in self.active_sessions:
            session.update_energy(interval_seconds, green_fraction)
        self._log_demand(total)

    # ------------------------------------------------------------------
    # Algoritmo de redistribuição de potência (coração do sistema)
    # ------------------------------------------------------------------

    def _redistribute_power(self, log: bool = True):
        """
        Redistribui a potência disponível entre as sessões ativas.

        Algoritmo iterativo:
        1. Divide o orçamento igualmente entre os carregadores pendentes
        2. Carregadores que atingem seu teto de hardware ficam fixos
        3. A sobra é redistribuída para os demais — repete até estabilizar

        Exemplo com 90 kW úteis (100 kW contratados, limite de uso de 90%), 1 sessão em carregador de 22 kW:
          share = 90 / 1 = 90 kW → teto 22 kW atingido → aloca 22 kW
          (limite do hardware, não do orçamento)

        Exemplo com 90 kW úteis, 3 sessões (22 kW, 11 kW, 11 kW):
          share = 90 / 3 = 30,0 → todos atingem teto
          Total: 22 + 11 + 11 = 44 kW — correto, hardware esgotado

        Exemplo com 90 kW úteis, 1 sessão em carregador de 50 kW:
          share = 90 / 1 = 90 kW → teto 50 kW atingido → aloca 50 kW

        Extensível para: prioridade por SOC, tempo de chegada, tarifa etc.
        """
        self._last_budget_kw = self.budget_kw
        sessions = self.active_sessions
        if not sessions:
            return

        sessions_sorted = sorted(sessions, key=lambda s: s.started_at)
        pending = {s.charger_id: s for s in sessions_sorted}
        # Orçamento = limite útil da rede + geração solar do momento (o solar é
        # consumido "atrás do medidor", então não pesa na demanda contratada).
        remaining_kw = self.budget_kw

        while pending:
            share = remaining_kw / len(pending)
            newly_fixed = {
                cid: s for cid, s in pending.items()
                if self.chargers[cid].max_kw <= share
            }

            if not newly_fixed:
                # Nenhum teto atingido — distribui share igualmente para os pendentes
                # Arredonda individualmente e corrige o último para evitar desvio de ponto flutuante
                pending_list = list(pending.items())
                allocated_so_far = 0.0
                for idx, (charger_id, session) in enumerate(pending_list):
                    if idx < len(pending_list) - 1:
                        val = round(share, 2)
                        session.allocated_kw = val
                        allocated_so_far += val
                    else:
                        # Último recebe o que sobrar exatamente
                        session.allocated_kw = round(remaining_kw - allocated_so_far, 2)
                break

            # Fixa os que atingiram o teto e redistribui a sobra
            for charger_id, session in newly_fixed.items():
                session.allocated_kw = round(self.chargers[charger_id].max_kw, 2)
                remaining_kw -= self.chargers[charger_id].max_kw
                del pending[charger_id]

        n = len(sessions_sorted)

        if log:
            self._log_event(
                "POWER_REDISTRIBUTED", "SYSTEM",
                f"{n} carregador(es) ativo(s) | "
                f"Total alocado: {self.total_allocated_kw:.1f} kW / {self.budget_kw:.1f} kW disponíveis "
                f"({self.usable_limit_kw:.1f} rede + {self.pv_kw:.1f} solar)"
            )

        self._push_power_to_hardware()

    # ------------------------------------------------------------------
    # Snapshot de estado (contrato com a interface)
    # ------------------------------------------------------------------

    def _state_snapshot(self) -> dict:
        """
        Retorna o estado completo do sistema como dicionário.
        Este é o contrato entre a lógica e qualquer interface (terminal, API, frontend).
        """
        return {
            "site": self.site_name,
            "contracted_limit_kw": self.contracted_limit_kw,
            "usable_limit_kw": round(self.usable_limit_kw, 2),
            "usable_limit_pct": self.usable_limit_pct,
            "total_allocated_kw": round(self.total_allocated_kw, 2),
            "available_kw": round(self.available_kw, 2),
            "utilization_pct": round(self.utilization_pct, 1),
            "active_sessions": len(self.active_sessions),
            "solar_kw": round(self.pv_kw, 2),
            "solar_peak_kw": self.pv_peak_kw,
            "sim_hour": self.sim_hour,
            "budget_kw": round(self.budget_kw, 2),
            "grid_draw_kw": round(self.grid_draw_kw, 2),
            "tariff": self.pricing_now(),
            "green_kwh_total": round(self.total_green_kwh, 3),
            "green_share_pct": round(self.green_share_pct, 1),
            "co2_avoided_kg": round(self.co2_avoided_kg, 4),
            "chargers": [
                {
                    "id": c.charger_id,
                    "location": c.location,
                    "max_kw": c.max_kw,
                    "status": "OCUPADO" if c.is_occupied else "LIVRE",
                    "allocated_kw": c.current_kw,
                    "vehicle_id": c.session.vehicle_id if c.session else None,
                    "duration_min": round(c.session.duration_minutes(), 1) if c.session else None,
                    "energy_kwh": round(c.session.energy_consumed_kwh, 3) if c.session else None,
                    "tariff_rs_per_kwh": c.session.tariff_rs_per_kwh if c.session else None,
                    "cost_rs": round(c.session.cost_rs, 2) if c.session else None,
                    "green_kwh": round(c.session.green_kwh, 3) if c.session else None,
                }
                for c in self.chargers.values()
            ],
            "events": self.events[-5:],  # Últimos 5 eventos para display
        }

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------

    def _get_charger(self, charger_id: str) -> Charger:
        if charger_id not in self.chargers:
            raise ValueError(f"Carregador '{charger_id}' não encontrado na rede.")
        return self.chargers[charger_id]

    def _log_event(self, event_type: str, source: str, message: str):
        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "type": event_type,
            "source": source,
            "message": message,
        }
        self.events.append(entry)
        if len(self.events) > MAX_EVENTS:       # a lista não pode crescer para sempre
            del self.events[:-MAX_EVENTS]
        logger.info(f"[{event_type}] {source}: {message}")

    def _error(self, message: str) -> dict:
        self._log_event("ERROR", "SYSTEM", message)
        return {"error": message}

    # ------------------------------------------------------------------
    # Integração com o hardware via Modbus TCP (core/hardware.py)
    # ------------------------------------------------------------------
    # Sem `self.hardware` nada disto faz nada: o controle continua só em memória.
    # Com ele, cada redistribuição grava o limite no registrador 10029 de cada
    # carregador ativo, e ligar/desligar a carga usa o registrador 10060.
    # Qualquer falha de rede vira um evento no console — nunca derruba o controle.

    def _hardware_failed(self, charger_id: str, exc: Exception):
        """Registra a falha uma única vez até o hardware voltar a responder."""
        if not self._hw_error_logged:
            self._hw_error_logged = True
            self._log_event("MODBUS_ERROR", charger_id,
                            f"Falha ao falar com o hardware: {exc} — seguindo com o controle interno")

    def _hardware_ok(self):
        if self._hw_error_logged:
            self._hw_error_logged = False
            self._log_event("MODBUS_RECOVERED", "SYSTEM", "Comunicação Modbus restabelecida")

    def _write_power_to_hardware(self, charger_id: str, allocated_kw: float):
        """
        Grava o limite de potência do carregador no registrador 10029
        (valor = kW x 10; ex.: 15,5 kW -> 155). Só escreve se o valor mudou
        desde a última gravação bem-sucedida, para não inundar o barramento.

        O carregador ajusta o valor à faixa do modelo (22 kW: 4,2 a 22,0 kW);
        se o valor aplicado difere do pedido, isso aparece no evento.
        """
        if self.hardware is None:
            return
        register_value = power_to_register(allocated_kw)
        if self._hw_limit_sent.get(charger_id) == register_value:
            return
        from core.hardware import HardwareError
        try:
            applied_kw = self.hardware.write_limit(charger_id, allocated_kw)
        except HardwareError as exc:
            self._hardware_failed(charger_id, exc)
            return
        self._hardware_ok()
        self._hw_limit_sent[charger_id] = register_value
        note = "" if abs(applied_kw - allocated_kw) < 0.05 else f" (carregador ajustou para {applied_kw:.1f} kW)"
        self._log_event("MODBUS_WRITE", charger_id,
                        f"Reg {HCA_G2.WRITE.MAX_CHARGING_POWER} <- {register_value} ({allocated_kw:.1f} kW){note}")

    def _push_power_to_hardware(self):
        """
        Sincroniza o hardware com o estado do controller (chamado ao fim de cada
        redistribuição): grava o limite de cada sessão ativa, liga quem ainda não
        foi ligado e repete qualquer "desligar" que tenha falhado antes.
        Como só envia diferenças, serve também para se recuperar de uma queda.
        """
        if self.hardware is None:
            return
        for charger_id in list(self._hw_off_pending):
            if not self.chargers[charger_id].is_occupied:
                self._switch_hardware(charger_id, on=False)
        for session in self.active_sessions:
            self._write_power_to_hardware(session.charger_id, session.allocated_kw)
            if session.charger_id not in self._hw_on and session.charger_id in self._hw_limit_sent:
                self._switch_hardware(session.charger_id, on=True)

    def _switch_hardware(self, charger_id: str, on: bool):
        """Liga/desliga o carregamento (registrador 10060) e guarda o resultado."""
        from core.hardware import HardwareError
        try:
            self.hardware.set_charging(charger_id, on)
        except HardwareError as exc:
            self._hardware_failed(charger_id, exc)
            if not on:
                self._hw_off_pending.add(charger_id)
            return
        self._hardware_ok()
        self._hw_off_pending.discard(charger_id)
        if on:
            self._hw_on.add(charger_id)
        else:
            self._hw_on.discard(charger_id)
            self._hw_limit_sent.pop(charger_id, None)
        self._log_event("MODBUS_WRITE", charger_id,
                        f"Reg {HCA_G2.WRITE.CHARGER_ON_OFF} <- {2 if on else 1} ({'LIGAR' if on else 'DESLIGAR'})")

    def _release_hardware(self, charger_id: str):
        """Chamado quando o veículo sai: desliga a carga no carregador físico."""
        if self.hardware is None:
            return
        self._switch_hardware(charger_id, on=False)

    def _read_power_from_hardware(self, charger_id: str) -> float:
        """
        Lê a potência atual do carregador (registrador 10015, kW = valor / 10).
        Sem hardware, ou se ele não responder, devolve a potência alocada
        pelo controller (a melhor estimativa disponível).
        """
        charger = self.chargers.get(charger_id)
        fallback = charger.current_kw if charger else 0.0
        if self.hardware is None:
            return fallback
        from core.hardware import HardwareError
        try:
            value = self.hardware.read_power(charger_id)
        except HardwareError as exc:
            self._hardware_failed(charger_id, exc)
            return fallback
        self._hardware_ok()
        return value
