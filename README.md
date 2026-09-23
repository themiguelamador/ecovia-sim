# ecovia-sim — simulação do trânsito de Guimarães

Modelo aberto de microssimulação do trânsito no centro de Guimarães, feito para os
**Amigos da Ecovia** avaliarem a via proposta no PDM sobre a Ecopista e as alternativas
(estudo em [amigosdaecovia.org/estudo](https://amigosdaecovia.org/estudo)).

Simula cada veículo, cruzamento e rotunda num dia útil inteiro (0–24 h), a partir de dados
públicos, e compara cenários: novas vias, vias alteradas (velocidade, vias de circulação),
vias fechadas.

| | |
|---|---|
| Simulador | [Eclipse SUMO](https://eclipse.dev/sumo/) 1.27 (open source, EPL-2.0) |
| Rede viária | OpenStreetMap (ODbL), snapshot em `data/gmr.osm.xml.gz` |
| População | INE, Censos 2021, BGRI por subsecção (`data/census.csv`) |
| Emprego, comércio, ensino | OpenStreetMap (lojas, serviços, escolas, universidade, hospital…) |
| Parâmetros | `params.toml` — todos os números do modelo, comentados |

## Começar

Precisa de [uv](https://docs.astral.sh/uv/) e `make`. O SUMO é instalado pelo `uv` (pacote `eclipse-sumo`).

```bash
make                              # cenário base: rede + procura + simulação (~12 min)
make SCEN=s1_pdm                  # cenário S1: via do PDM
make compare SCEN=s1_pdm          # indicadores S1 vs base + mapa out/s1_pdm/report.html
make compare SCEN=base            # mapa só do cenário base
make gui SCEN=s1_pdm              # ver o trânsito a circular (sumo-gui, começa às 7h)
make geh                          # calibração contra contagens (data/counts.csv)
uv run python tests/test_geometry.py
```

O mapa (`out/<cenário>/report.html`) abre no browser: tráfego por via e por hora (largura =
veículos, cor = velocidade face ao limite), diferença cenário − base, pressão de cada zona
(partidas + chegadas) ao longo do dia, e a tabela de indicadores: veículos·km, veículos·hora,
atraso, CO₂, bloqueios.

## Como funciona

1. **Rede** (`Makefile`, `netconvert`). OSM recortado a `-8.315,41.425,-8.265,41.460`;
   rotundas, semáforos e vias de viragem vêm das etiquetas OSM. Vias sem `maxspeed` ficam a
   50 km/h (limite urbano).
2. **Procura** (`scripts/demand.py`). Modelo de quatro passos simplificado:
   - *Geração* — residentes por subsecção (Censos 2021) × taxa de viagens por motivo
     (trabalho, ensino, compras, não-domiciliárias). Emprego, ensino e comércio estimados a
     partir do OSM com os pesos de `[attractors]`.
   - *Distribuição* — modelo gravitacional por motivo (`beta_per_km`) entre zonas de 400 m no
     centro e zonas de 1,5 km no resto do concelho.
   - *Repartição modal* — probabilidade de ir de carro cresce com a distância
     (`car_share_max`, `car_half_km`); viagens curtas são sobretudo a pé.
   - *Hora* — cada motivo tem um perfil horário (`[profiles]`): é aqui que se definem as
     pontas da manhã e da tarde, a entrada e saída das escolas e as horas do comércio.
   - Residentes do resto do concelho entram pela *porta* (via principal na fronteira da área)
     mais próxima em direcção; o tráfego de outros concelhos é um volume por porta
     (`[gate_daily_vehicles]`), parte de passagem.
3. **Escolha de percurso** (`sumo`). Cada condutor escolhe o caminho mais rápido à partida
   e reavalia a cada 2 min com os tempos actuais, reagindo ao congestionamento e a vias novas.
4. **Cenários** (`scripts/scenario.py`). Um cenário é um GeoJSON em `scenarios/`, desenhado em
   [geojson.io](https://geojson.io), com linhas e uma propriedade `action`:
   - `add` (omissão) — via nova; `lanes` por sentido (1), `speed_kmh` (50), `oneway`, `name`.
     As pontas ligam ao cruzamento existente a menos de 40 m, ou criam um novo.
   - `modify` — altera `lanes`/`speed_kmh` das vias existentes ao longo da linha (25 m).
   - `remove` — fecha as vias ao longo da linha (pedonalização, corte).

   Guardar como `scenarios/<nome>.geojson` e correr `make compare SCEN=<nome>`.

| Cenário | Ficheiro | Estado |
|---|---|---|
| S0 base | — | rede actual |
| S1 via do PDM | `s1_pdm.geojson` | traçado reconstituído da imagem do PDM (erro ~11 m), 1 via por sentido, 50 km/h |
| exemplo | `example_avenida_30.geojson` | Av. D. João IV a 30 km/h — mostra o uso de `modify` |
| S2–S4 | por fazer | prioridade ao autocarro, gestão de tráfego, ligações alternativas |

## Calibração — o que falta para os resultados serem defensáveis

O modelo corre e responde a alterações, mas **ainda não está calibrado**: os parâmetros
são estimativas da literatura, não medições em Guimarães. Antes de publicar conclusões:

1. **Contagens.** Copiar `data/counts.example.csv` para `data/counts.csv` com as contagens
   reais (sentido em graus, hora, veículos), correr `make geh` e ajustar `params.toml` até
   GEH < 5 em ≥ 85 % das contagens. Pedir contagens à Câmara e à Infraestruturas de
   Portugal; os volumes do diagnóstico do PDM (Vol. VI) também servem.
2. **Tempos de percurso.** Comparar a duração de alguns percursos na hora de ponta com
   Google Maps / TomTom.
3. **Rede.** Rever no `netedit` o número de vias, as viragens proibidas e os planos
   semafóricos nos cruzamentos principais. Os ~650 "teleportes" por bloqueio de mudança de
   via no cenário base são quase sempre erros de vias no OSM.
4. **Várias sementes.** A simulação é estocástica: comparar cenários com várias sementes
   (`make SCEN=x SEED=1`, `SEED=2`, …) e reportar média e intervalo.

## Limites conhecidos

- Só automóveis. Sem autocarros, bicicletas nem peões na rede (a repartição modal é um
  parâmetro, não um modelo de escolha). S2 precisa das linhas do Guimabus (GTFS).
- Sem estacionamento: os veículos desaparecem ao chegar.
- Procura fixa entre cenários: a mesma matriz de viagens em todos. A procura induzida por
  uma via nova não está modelada; testar com `scale` (+5 a +10 % nos pares servidos).
- Emprego estimado por pesos sobre o OSM, sem dados de emprego por local. Substituir por
  dados do INE/GEP (Quadros de Pessoal) se forem obtidos.
- As zonas fora da área têm actividade proporcional aos residentes (`outside_activity`).
- O Campus da Justiça (2029) ainda não está na procura: acrescentar como atractor.
