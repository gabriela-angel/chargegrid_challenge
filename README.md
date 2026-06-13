# ChargeGrid Intelligence
### Sistema Integrado de Gestão de Eletropostos Comerciais

**GoodWe EV Challenge 2026 — FIAP | 1CCR — Equipe 04**  

## Integrantes

| Nome | RM |
|------|----|
| Gabriela Angel Silva | 570808 |
| Izabelly Menezes | 570673 |
| Marcos Paulo Sampaio | 573987 |
| Otávio Santos | 570225 |
| Tiago Muhlmann | 569569 |
| Wesley Marques | 573915 |

---

## Sumário

1. [Contexto e Problema](#1-contexto-e-problema)
2. [Solução Proposta](#2-solução-proposta)
3. [Arquitetura do Sistema](#3-arquitetura-do-sistema)
4. [Estrutura do Projeto](#4-estrutura-do-projeto)
5. [Como Executar](#5-como-executar)
6. [Prova de Conceito — Sprint 2](#6-prova-de-conceito--sprint-2)
7. [Integração com Hardware Real (HCA G2)](#7-integração-com-hardware-real-hca-g2)
8. [Princípios de Energias Renováveis e Sustentabilidade](#8-princípios-de-energias-renováveis-e-sustentabilidade)
9. [Evolução do Projeto — Roadmap](#9-evolução-do-projeto--roadmap)
10. [Referências](#10-referências)

---

## 1. Contexto e Problema

O Brasil registrou **223.912 veículos elétricos e híbridos plug-in** vendidos em 2025 (+26% em relação ao ano anterior), com mais de **16.880 eletropostos públicos e semipúblicos** em operação — expansão de 290% desde 2023. Os dois primeiros meses de 2026 acumularam 48.591 emplacamentos, 90% a mais que no mesmo período de 2025.

Esse crescimento acelerado expõe um gargalo estrutural: **a infraestrutura de recarga comercial carece de inteligência operacional**. O hardware evoluiu — os sistemas de gestão não acompanharam.

### Problema Central

Em ambientes comerciais (shoppings, supermercados, estacionamentos), quatro disfunções coexistem:

| Disfunção | Impacto |
|-----------|---------|
| **Opacidade energética** | Operadores sem visibilidade em tempo real do consumo por ponto de carga |
| **Fragmentação de hardware** | Carregadores de fabricantes distintos com protocolos proprietários incompatíveis |
| **Ausência de faturamento automatizado** | Cobrança manual ou sistemas proprietários desconexos da operação financeira |
| **Desperdício de capacidade instalada** | Eletropostos operando de forma reativa, sem distribuição eficiente de carga |

### Por que não basta usar o sistema residencial da GoodWe?

A GoodWe já oferece o **SEMS (Smart Energy Management System)** para gestão residencial — um dashboard que integra painéis solares, baterias e o carregador HCA G2. Esse sistema funciona muito bem para **um único usuário, uma única fatura de energia**.

No ambiente comercial, o cenário é fundamentalmente diferente: dezenas de carregadores simultâneos, usuários distintos, pagamentos independentes, controle de demanda contratada com a distribuidora e risco de multas por ultrapassagem de pico. O SEMS residencial não foi projetado para essa complexidade.

**O ChargeGrid Intelligence é a camada de inteligência que falta**: o sistema que se posiciona entre o hardware da GoodWe e o operador comercial, orquestrando potência, faturamento e dados em tempo real.

---

## 2. Solução Proposta

O ChargeGrid Intelligence é uma **plataforma de orquestração para redes de eletropostos comerciais**, estruturada em quatro pilares:

```
┌─────────────────────────────────────────────────────────┐
│                  ChargeGrid Intelligence                │
├──────────────┬──────────────┬──────────────┬────────────┤
│  Controle de │  Protocolos  │  Tarifação e │     IA     │
│   Demanda    │    Abertos   │  Pagamento   │  Preditiva │
│              │  OCPP/MODBUS │              │            │
└──────────────┴──────────────┴──────────────┴────────────┘
```

### O que o sistema faz

**Controle Dinâmico de Potência**: define um orçamento de potência para o site (ex: 100 kW) e redistribui automaticamente entre os carregadores ativos a cada conexão ou desconexão de veículo. O consumo total nunca ultrapassa o limite contratado com a distribuidora.

**Integração via Protocolos Abertos**: comunica-se com os carregadores físicos via **Modbus TCP** (protocolo nativo do HCA G2) e **OCPP**, eliminando dependência de SDKs proprietários. Qualquer carregador compatível pode ser integrado sem desenvolvimento adicional.

**Faturamento Automatizado**: cada sessão gera automaticamente um registro com horário, energia consumida e tarifa aplicada, pronto para integração com gateways de pagamento.

**IA Preditiva**: modelos de machine learning analisam padrões históricos de uso para antecipar picos de demanda e ajustar a alocação de potência de forma proativa, antes que a sobrecarga ocorra.

---

## 3. Arquitetura do Sistema

### Visão em camadas

```
┌──────────────────────────────────────────────────────────────────┐
│  CAMADA DE APRESENTAÇÃO                                          │
│  Terminal (Sprint 2) → Interface Web / API REST (Sprint 3+)      │
│  display.py                                                      │
└───────────────────────────┬──────────────────────────────────────┘
                            │ state_snapshot() dict
┌───────────────────────────▼──────────────────────────────────────┐
│  CAMADA DE NEGÓCIO — Lógica Central                              │
│  demand_controller.py                                            │
│  ├── DemandController  (orquestração e redistribuição)           │
│  ├── Charger           (representação de cada ponto de recarga)  │
│  └── ChargingSession   (dados da sessão ativa)                   │
└───────────────────────────┬──────────────────────────────────────┘
                            │ Modbus TCP (Sprint 3+)
┌───────────────────────────▼──────────────────────────────────────┐
│  CAMADA DE HARDWARE — GoodWe HCA G2                              │
│  modbus_map.py                                                   │
│  ├── HCA_G2.READ   (registradores de leitura: potência, status)  │
│  └── HCA_G2.WRITE  (registradores de escrita: limite de potência)│
└──────────────────────────────────────────────────────────────────┘
```

### Fluxo do controle de demanda

```
Veículo conecta ao CG-07 (7º carro simultâneo)
        │
        ▼
DemandController.vehicle_connect("CG-07", "CAR-0007")
        │
        ▼
_redistribute_power()  — algoritmo iterativo com cascata de sobras
  ├── orçamento disponível: 95 kW (100 kW - 5% margem)
  ├── sessões ativas: 7
  ├── share inicial: 95 / 7 = ~13,6 kW
  ├── nenhum carregador atinge teto (13,6 < 22 kW)
  └── todos recebem 13,6 kW  ← orçamento é o limite, não o hardware

  Cenário máximo (12 carros simultâneos):
  ├── share: 95 / 12 = ~7,92 kW por carregador
  └── total alocado: 95,0 kW  ← limite respeitado, todos carregando

  Cenário de alívio (3 carros restantes):
  ├── share: 95 / 3 = ~31,7 kW → teto de 22 kW atingido
  └── todos recebem 22,0 kW  ← hardware é o gargalo, não o orçamento
        │
        ▼ (Sprint 3+)
Modbus TCP → Reg. 10029 de cada carregador ativo
  client.write_register(10029, int(7.92 * 10))  # → escreve 79
```

### Por que a arquitetura é desacoplada

A separação entre `display.py` (apresentação) e `demand_controller.py` (lógica) é intencional. O controlador expõe apenas um método `_state_snapshot()` que retorna um dicionário Python — não sabe nada sobre terminal, web ou API. Quando o projeto migrar para uma interface web, apenas a camada de apresentação muda. A lógica de negócio permanece intacta.

---

## 4. Estrutura do Projeto

```
chargegrid/
│
├── main.py                  # Ponto de entrada — orquestração e loop interativo
│
└── core/
    ├── demand_controller.py # ★ Lógica central de controle de demanda
    ├── display.py           # Camada de apresentação (terminal)
    └── modbus_map.py        # Mapa de registradores do GoodWe HCA G2
```

### Descrição dos arquivos

**`main.py`**
Ponto de entrada da aplicação. Configura o site comercial (nome, limite de potência contratada, carregadores registrados), gerencia o loop interativo e a demo automática (`--demo`). Toda orquestração passa por aqui — a lógica de negócio fica em `demand_controller.py`.

**`core/demand_controller.py`**
O cérebro do sistema. Contém as classes `DemandController`, `Charger` e `ChargingSession`. Implementa o algoritmo de redistribuição de potência (`_redistribute_power()`), processa eventos de conexão/desconexão e mantém o histórico de sessões. Completamente desacoplado da interface.

**`core/display.py`**
Camada de apresentação para o terminal. Recebe apenas o dicionário de estado (`state_snapshot`) e renderiza o dashboard, barra de utilização, tabela de carregadores e log de eventos. Não contém nenhuma lógica de negócio.

**`core/modbus_map.py`**
Mapa completo dos registradores Modbus do carregador GoodWe HCA G2 (fonte: `Mapa_MODBUS_HCA_G2.pdf`, protocolo v1.0.15). Organizado em `HCA_G2.READ` (leitura) e `HCA_G2.WRITE` (escrita), com decoders para fontes de energia, estados do carregador e bytes de falha. Na Sprint 2, serve como referência documental; na Sprint 3+, será usado com `pymodbus` para comunicação real com o hardware.

---

## 5. Como Executar

### Pré-requisitos

- Python 3.10 ou superior
- Sem dependências externas na Sprint 2 (biblioteca padrão apenas)

### Instalação

```bash
git clone https://github.com/seu-usuario/chargegrid-intelligence.git
cd chargegrid-intelligence
```

### Modo interativo

O operador conecta e desconecta veículos manualmente e observa a redistribuição de potência em tempo real:

```bash
python main.py
```

**Ações disponíveis no menu:**

| Tecla | Ação |
|-------|------|
| `c` | Conectar um veículo a um carregador |
| `d` | Desconectar um veículo |
| `s` | Ver resumo das sessões concluídas |
| `q` | Encerrar |

### Modo demo

Executa automaticamente uma simulação em 3 fases com 12 carregadores, com 2,5 segundos entre cada evento:

```bash
python main.py --demo
```

---

## 6. Prova de Conceito — Sprint 2

### O que é demonstrado

A prova de conceito implementa o **Controle Dinâmico de Potência** — o pilar central do ChargeGrid Intelligence. O sistema simula uma rede com 12 carregadores GoodWe HCA G2 em um estacionamento comercial com limite de 100 kW contratados.

### Cenário simulado

```
Site: Shopping Paulista — Estacionamento
Demanda contratada: 100 kW
Margem de segurança: 5%
Limite útil: 95 kW
Demanda potencial total: 12 × 22 kW = 264 kW

Carregadores registrados (todos GoodWe HCA G2 — 22 kW trifásico):
  CG-01  Vaga A1 | CG-02  Vaga A2 | CG-03  Vaga A3 | CG-04  Vaga A4
  CG-05  Vaga B1 | CG-06  Vaga B2 | CG-07  Vaga B3 | CG-08  Vaga B4
  CG-09  Vaga C1 | CG-10  Vaga C2 | CG-11  Vaga C3 | CG-12  Vaga C4
```

### Comportamento observável

Fase 1 — Chegada gradual: 6 carros conectam. Com poucos ativos o orçamento é folgado e cada um recebe os 22 kW completos. Conforme chegam, a potência por carregador vai caindo.

Fase 2 — Congestionamento: os outros 6 conectam. Com todos os 12 ativos simultaneamente, a demanda potencial (264 kW) ultrapassa em 2,7× o limite do shopping (95 kW). O sistema throttla cada carregador para ~7,92 kW (95 ÷ 12) — todos os carros continuam carregando, o limite contratado não é ultrapassado, e nenhuma multa é gerada.

Fase 3 — Alívio: carros vão desconectando. A potência por carregador sobe automaticamente a cada saída, até que os últimos 3 recebam os 22 kW completos — agora o gargalo é o hardware, não o orçamento.

### Evolução em relação à Sprint 1

Na Sprint 1, o ChargeGrid Intelligence foi definido como proposta arquitetural — problema, pilares, tecnologias e fundamentos de sustentabilidade. Na Sprint 2, o pilar de Controle de Demanda foi implementado como código funcional em Python, com arquitetura desacoplada preparada para integração com o hardware real (mapa Modbus) e para migração para interface web.

---

## 7. Integração com Hardware Real (HCA G2)

O `modbus_map.py` documenta o contrato exato entre o software e o hardware. Os registradores mais importantes para o controle de demanda são:

### Registradores de leitura (o sistema monitora o carregador)

| Endereço | Nome | Dado |
|----------|------|------|
| `10015` | Charging Power | Potência atual em kW (÷ 10) |
| `10017` | Charging Station Status | Estado: livre, carregando, alarme... |
| `10075` | Car Connection Status | 0=desconectado, 1=semi, 2=conectado |
| `10108` | Charging Power Source | bit0=rede, bit1=PV, bit2=bateria |
| `10103` | Green Energy | Energia renovável consumida (kWh) |

### Registrador de escrita principal (o sistema controla o carregador)

| Endereço | Nome | Função |
|----------|------|--------|
| `10029` | **Maximum Charging Power** | ★ Define o limite de potência do carregador |

Este é o registrador que o `_redistribute_power()` escreverá em cada carregador ativo após cada redistribuição. O valor é em décimos de kW (15,5 kW → escrever `155`).

**Faixas válidas por modelo:**

| Modelo | Mínimo | Máximo |
|--------|--------|--------|
| 7kW monofásico | 1,4 kW | 7,0 kW |
| 11kW trifásico | 4,2 kW | 11,0 kW |
| 22kW trifásico | 4,2 kW | 22,0 kW |

---

## 8. Princípios de Energias Renováveis e Sustentabilidade

O ChargeGrid Intelligence não trata sustentabilidade como atributo periférico — ela é central à arquitetura.

### 1. Eficiência no uso de energia já disponível

O módulo de controle dinâmico de potência redistribui a energia existente de forma inteligente, **adiando ou evitando a necessidade de ampliar a infraestrutura contratada**. A energia mais sustentável é aquela que não precisa ser gerada — a eficiência é prioritária sobre a expansão de capacidade.

### 2. Integração com fontes renováveis

A arquitetura é compatível com o ecossistema GoodWe de inversores fotovoltaicos. O registrador `10108` (Charging Power Source) do HCA G2 indica em tempo real se a energia vem da rede, de painéis PV ou de baterias. O sistema pode priorizar sessões alimentadas por energia solar e aplicar tarifas diferenciadas — incentivando o carregamento quando a energia é mais abundante e limpa.

O registrador `10032` (Advanced Charging Mode) permite configurar remotamente cada carregador para operar em modo PV ou PV+Bateria, direcionando o excedente solar gerado no estabelecimento diretamente para os eletropostos.

### 3. Redução de desperdício por previsão e planejamento

Modelos preditivos de demanda (LSTM, XGBoost) permitem planejar a operação com antecedência, evitando superdimensionamento e subaproveitamento da capacidade instalada. O desperdício de capital em infraestrutura ociosa é também desperdício de recursos materiais e energéticos empregados na fabricação desses equipamentos.

### 4. Tarifação como instrumento de demand response

A tarifação dinâmica não é apenas mecanismo de receita — é ferramenta de incentivo comportamental. Preços menores em horários de menor demanda (geralmente coincidentes com maior geração solar) incentivam os usuários a carregar quando a energia é mais abundante, contribuindo para o equilíbrio da rede elétrica.

### 5. Longevidade tecnológica como sustentabilidade sistêmica

A adoção de protocolos abertos (Modbus TCP, OCPP) garante que os equipamentos físicos instalados hoje continuem sendo gerenciados pela plataforma após atualizações de software ou mudanças de fornecedor, **evitando o descarte prematuro de hardware funcional** — uma das formas menos visíveis, mas mais impactantes, de desperdício tecnológico no setor de energia.

---

## 9. Evolução do Projeto — Roadmap

```
Sprint 1 ✅   Proposta arquitetural, análise de problemas, pilares, fundamentos

Sprint 2 ✅   Prova de conceito: Controle de Demanda em Python + Mapa Modbus HCA G2
              Arquitetura desacoplada (lógica / apresentação / hardware)

Sprint 3 →   Integração Modbus TCP real com hardware HCA G2 (pymodbus)
              Leitura em tempo real: potência, status, fonte de energia (reg. 10015, 10017, 10108)
              Escrita em tempo real: limite de potência por carregador (reg. 10029)
              Interface web — API REST (FastAPI) + dashboard

Sprint 4 →   Módulo de IA preditiva (LSTM/XGBoost) para previsão de picos de demanda
              Módulo de faturamento: registro de sessões (reg. 10158–10172) + integração PIX/cartão
              Tarifação dinâmica com incentivo a carregamento em horário de geração solar
```

---

## 10. Referências

- ABVE — Associação Brasileira de Veículos Elétricos. Dados de mercado 2025. https://abve.org.br/abve-data/
- GoodWe Technologies. HCA Series EV Charger. https://en.goodwe.com/hca-series
- GoodWe Technologies. Mapa MODBUS HCA G2 — Protocolo v1.0.15, 2025.
- Open Charge Alliance (OCA). Open Charge Point Protocol — OCPP. https://openchargealliance.org/protocols/open-charge-point-protocol/
- ANEEL. Resolução Normativa nº 1.000, de 7 de dezembro de 2021. https://www2.aneel.gov.br/cedoc/ren20211000.html
- Kyriakopoulos, I.; Theodoridis, Y. Electric Vehicle Charging Load Forecasting. ArXiv, Abril de 2026. https://arxiv.org/pdf/2512.17257
- MDPI Energies. Machine Learning-Based Electric Vehicle Charging Demand Forecasting. Setembro de 2025. https://www.mdpi.com/1996-1073/18/17/4779
- MDPI Engineering Proceedings. Dynamic Pricing for Load Balancing in EV Charging Stations. Outubro de 2025. https://www.mdpi.com/2673-4591/112/1/11
- AutoIndústria. A nova geografia da recarga elétrica no Brasil. Fevereiro de 2026. https://www.autoindustria.com.br/2026/02/24/a-nova-geografia-da-recarga-eletrica-no-brasil/
