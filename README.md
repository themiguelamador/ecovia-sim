# ecovia-sim — simulação do trânsito de Guimarães

Modelo aberto de microssimulação do trânsito da cidade de Guimarães, feito para os
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
| Autocarros | Guimabus, GTFS 2026 ([Minho Access Point](https://minhoaccesspoint.eu/pt_PT/dataset/operador-de-sptp-de-guimaraes), CC-BY) |
| Passadeiras | OpenStreetMap (`highway=crossing`), ~600 na área |
| Parâmetros | `params.toml` — todos os números do modelo, comentados |

## Começar

Precisa de [uv](https://docs.astral.sh/uv/) e `make`. O SUMO é instalado pelo `uv` (pacote `eclipse-sumo`).

```bash
make -j16 study                   # todos os cenários × 5 sementes + exportação para web/ (~1 h com 16 núcleos)
make run SCEN=s1_ecovia SEED=1    # uma simulação
make compare SCEN=s1_ecovia       # mapa local out/s1_ecovia/report.html (semente 1)
make gui SCEN=s1_ecovia           # ver o trânsito a circular (sumo-gui, começa às 7h)
make geh                          # calibração contra contagens (data/counts.csv)
uv run tools/georef.py            # refazer a georreferenciação e o traçado das vias do PDM
make test
```

`web/` é o que o site publica em [amigosdaecovia.org/simulacao](https://amigosdaecovia.org/simulacao):
copia-se para `sites/ecovia/public/simulacao/data/` no repositório do site.

## Como funciona

1. **Rede** (`Makefile`, `netconvert`). OSM recortado a `-8.325,41.415,-8.243,41.465`: a cidade
   inteira até à Circular (EN101, EN105, EN206). Rotundas, semáforos e vias de viragem vêm das
   etiquetas OSM. Vias sem `maxspeed` ficam a 50 km/h (limite urbano).
2. **Procura** (`scripts/demand.py`, `params.toml`). Modelo de quatro passos simplificado:
   - *Geração* — residentes por subsecção (Censos 2021) × taxa de viagens por motivo. Emprego,
     ensino e comércio a partir do OSM (`[attractors]`); grandes equipamentos com dimensão
     própria (`[[projects]]`: UMinho Azurém, Hospital, Campus da Justiça em 2029).
   - *Distribuição* — modelo gravitacional por motivo entre zonas de 400 m e o resto do concelho.
   - *Repartição modal* — probabilidade de ir de carro cresce com a distância; calibrada nos
     72,6% de deslocações pendulares de automóvel dos Censos 2021.
   - *Hora* — perfis horários por motivo (`[profiles]`).
   - *De fora* — residentes do resto do concelho (Censos) e tráfego de outros concelhos a partir
     do TMDA 2021 por corredor (PDM Vol. VI, Figura 4), ajustado para ~69 000 entradas/dia,
     a estimativa do próprio PDM. `demand.py` imprime a verificação.
3. **Escolha de percurso** (`sumo`). Caminho mais rápido à partida, reavaliado a cada 2 min.
   Na mesma simulação:
   - **Peões** — a parte não motorizada das viagens curtas (`walk_km`) anda nos passeios
     (`--sidewalks.guess`) e atravessa nas passadeiras do OSM (`scripts/crossings.py`: no
     cruzamento mais próximo, ou a meio do quarteirão dividindo a rua). Nas marcadas o peão
     tem prioridade: o carro pára.
   - **Autocarros** — todos os horários da Guimabus de um dia útil (`gtfs_date`), postos na rede
     com o `gtfs2pt` do SUMO. Param na via pelo menos 20 s.
   - **Estacionamento na rua** — `on_street_parking_share` das viagens que acabam numa rua local
     param na via `parking_manoeuvre_s` antes de chegar; as que partem de uma rua local
     arrancam paradas.
   - **Cargas e descargas** — carrinhas (`deliveries_per_retail_unit`) que, em
     `double_parking_share` dos casos, param na via `delivery_stop_s`.
   - Quem fica atrás de um veículo parado espera ou ultrapassa pela via contrária quando está
     livre (`--opposites.guess`).
4. **Vias do PDM** (`tools/georef.py`). A imagem das vias propostas (`data/pdm/vias-propostas.png`)
   é georreferenciada por pontos de controlo (estádio, rotunda da Av. D. João IV, nó da Circular;
   erro médio 12 m; verificação em `data/pdm/overlay.png`), as linhas vermelhas são traçadas e
   classificadas em *nova* ou *existente* (segue uma rua do OSM) e agrupadas por corredor em
   `data/pdm/tracado.geojson`. Só os troços novos entram na rede, com 1 via por sentido a 50 km/h.
   Pontas soltas ligam à rua mais próxima até 250 m (ligação assumida).
5. **Cenários** (`scripts/scenario.py`). GeoJSON em `scenarios/`: linhas próprias (`add`,
   `modify`, `remove`) e/ou `include` de corredores do traçado do PDM. `"elasticity"` activa a
   procura induzida (`scripts/induce.py`).

| Cenário | Conteúdo |
|---|---|
| base | rede actual, procura 2030 com o Campus da Justiça |
| s1_ecovia | ligação D. João IV – Parque da Cidade (sobre a Ecovia) |
| s2_via_rapida | ligação Av. D. João IV – Urgezes à via rápida, sem a via da Ecovia |
| s3_ecovia_circular | as duas: eixo contínuo da estação à Circular |
| s4_pdm_sem_ecovia | todas as vias novas do PDM excepto a da Ecovia |
| s5_pdm_completo | todas as vias novas do PDM |
| *_induzida | S3 e S5 com procura induzida (elasticidade −0,5) |
| u0_base_urbanizacao | rede actual + urbanização: lotes vazios da Costa, junto ao Hotel de Guimarães e do Monte do Cavalinho com prédios de 4–5 andares (~1 100 fogos), novo Centro de Saúde, ruas do Cavalinho (`data/urbanizacao.geojson`); comparado com a base |
| u1…u5 | S1–S5 com a urbanização; comparados com U0 (campo `reference`) |
| p0_pmus2030 | rede actual com a meta do PMUS de Guimarães para 2030: automóvel de 64% para 40% das deslocações dos residentes (`[variants.pmus2030]` em `params.toml`); comparado com a base |
| p1, p2, p5 | S1, S2 e S5 com essa meta; comparados com P0 |
| example_avenida_30 | exemplo de `modify` (fora do estudo: não começa por `s` nem `u`) |

6. **Resultados** (`scripts/export_web.py`). Indicadores por cenário com intervalo de 95% sobre as
   diferenças emparelhadas por semente, fluxos horários por rua, uso das vias novas, trajectos
   de uma amostra de 12% dos carros entre as 8h e as 9h.

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
   semafóricos nos cruzamentos principais. Para encontrar erros de vias: correr um dia sem `--no-warnings` e contar os
   teleportes "wrong lane" por cruzamento. Já corrigido: viagens com origem ou destino em
   ruas sem ligação à rede (85% desses teleportes; `demand.py` só usa ruas de onde se sai e
   onde se chega) e o entrecruzamento na Alameda Mariano Felgueiras
   (`data/corrections.con.xml`, onde se juntam as próximas correcções de ligações).

   Comportamento dos condutores (`vtypes.add.xml`): com os valores por omissão do SUMO,
   os carros esperavam indefinidamente por um intervalo nas rotundas e ~4% das viagens
   eram teleportadas. Com impaciência (30 s), intervalo mínimo de 0,6 s ao entrar de uma
   via secundária e mudanças de via mais assertivas, os teleportes caem de ~6 000–9 800
   para ~2 000–2 400 por dia (3 sementes da base), sem mais colisões nem tempos de
   viagem implausíveis. Limite de teleporte: 120 s. Com 300 s testado em 3 dias da base,
   nenhum recuperou da ponta da manhã (o SUMO não desfaz certos bloqueios circulares e o
   congestionamento espalha-se pela rede); com 120 s, os 3 recuperaram.
4. **Premissas marcadas ASSUMPTION** em `params.toml` (dimensão do Campus da Justiça, do
   hospital, de Azurém) — substituir por números oficiais.

## Limites conhecidos

- Autocarros só da Guimabus (sem GTFS público das linhas intermunicipais); sem passageiros.
- Peões só das viagens curtas dos residentes (sem turistas nem visitantes); sem bicicletas.
- A repartição modal é um parâmetro, não um modelo de escolha.
- Estacionamento na rua e descargas em segunda fila são premissas (`ASSUMPTION`), sem
  procura de lugar.
- Sem estacionamento: os veículos desaparecem ao chegar.
- Procura fixa entre cenários, excepto nas variantes `_induzida`, que usam tempos em via
  livre: subestimam a indução onde a via nova alivia um corredor congestionado.
- O PDM admite até ~100 000 entradas/dia depois da pandemia; o modelo usa ~69 000.
- Emprego estimado por pesos sobre o OSM, sem dados de emprego por local. Substituir por
  dados do INE/GEP (Quadros de Pessoal) se forem obtidos.
- As zonas fora da área têm actividade proporcional aos residentes (`outside_activity`).
- Traçado das vias do PDM a partir de uma imagem (erro ~12 m) e ligações nas pontas assumidas.

## Licença

O **código** (scripts, `Makefile`, `params.toml`, cenários) está sob a [licença MIT](LICENSE):
podes usar, copiar, alterar e redistribuir, mantendo o aviso de copyright.

Os **dados** incluídos mantêm as licenças das fontes:

| Ficheiro | Fonte | Licença |
|---|---|---|
| `data/gmr.osm.xml.gz` | © contribuidores do OpenStreetMap | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) — atribuição e partilha nos mesmos termos |
| `data/census.csv` | INE, Censos 2021 (BGRI), derivado por `scripts/census.py` | dados abertos do INE, com citação da fonte |
| `data/gtfs/guimabus_gtfs_2026.zip` | Guimabus / Município de Guimarães, via Minho Access Point | [CC-BY](https://creativecommons.org/licenses/by/4.0/) |
| `data/pdm/vias-propostas.png` | imagem divulgada com a revisão do PDM de Guimarães (Set 2026) | reproduzida para análise e crítica; direitos do autor original |
| `data/pdm/tracado.geojson`, `transform.json`, `overlay.png` | derivados da imagem do PDM e do OSM por `tools/georef.py` | ODbL (derivado de OSM) |

Os resultados publicados em [amigosdaecovia.org/simulacao](https://amigosdaecovia.org/simulacao)
podem ser citados como: *Amigos da Ecovia de Guimarães, «Simulação do trânsito de Guimarães»,
modelo ecovia-sim (versão = commit indicado na página)*.

## Contribuir

Críticas e correcções são bem-vindas, sobretudo às premissas marcadas `ASSUMPTION` em
`params.toml`, aos traçados das vias do PDM e às contagens de tráfego. Abre uma *issue* ou um
*pull request*; para contagens feitas no terreno, acrescenta-as a `data/counts.csv` com a data,
o local e o método.
