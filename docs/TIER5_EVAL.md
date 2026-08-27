# Tier 5 — Avaliação cética de retorno real

> **TL;DR:** Das 9 ideias, **2 são quick wins de alto retorno**
> (`step_ap214_extract_metadata`, `bom_csv_export`), **1 é um
> moat real se bem-feita** (`cadquery_transpile`), **1 é marketing
> sem produto** (`voice_to_solid`), e as 5 restantes vivem num
> meio-termo onde o retorno depende de execução cirúrgica.
>
> A tabela no fim ranqueia as 9 com nota 0-10.

---

## 1. `voice_to_solid` — fala natural → caixa/cylinder

**Nota de retorno: 2/10 (marketing) / 4/10 (curiosidade em demo)**

### Sinais de "vai virar produto"
- **Demo-killer em conferências**: sim, abre keynote. 15 segundos de "voz → objeto" viraliza.
- **Acessibilidade**: pessoas com mobilidade reduzida que não conseguem clicar no Sketcher.
- **Workshop / educação**: professor falando para turma enquanto desenha.

### Sinais de "vai morrer no GitHub"
- **Faster-whisper precisa de GPU decente** (4-6 GB VRAM). A maioria dos
  operadores de CAD não tem GPU dedicada; rodar Whisper na CPU dá
  latência de 3-7s por comando falado, o que mata a UX.
- **O LLMSpeak ASR produz transcrições ruins em ambientes barulhentos**
  (tornearia, lixadeira, escritório open-space). Vocabulário técnico
  CAD (extrude, fillet, chamfer, dovetail) está fora dos modelos
  generalistas.
- **Multi-modal agents já aceitam áudio** (Gemini 1.5, ChatGPT Advanced
  Voice Mode). A maioria dos usuários CAD está usando esses
  diretamente via mic do navegador — não precisa do FreeCAD intermediário.
- **Latência end-to-end**: 4s (Whisper) + 8s (LLM decide tool) + 2s
  (RPC) = **14s por iteração**. Nenhum humano desenha assim. CAD é
  sobre iterações rápidas; voz adiciona fricção em vez de remover.
- **Comando do FreeCAD já tem atalhos de teclado e Sketcher GUI**
  que são ordens de grandeza mais rápidos que voz.

### Veredicto
**Não construir.** É a ideia mais flashy da lista e a menos útil.
Quem precisar de voice-to-CAD vai usar um wrapper sobre Gemini Live
API diretamente. Custa ~3 semanas para fazer demo; ganha 0 usuários
recorrentes. **Skip.**

---

## 2. `step_ap214_extract_metadata` — ler metadata de STEP

**Nota de retorno: 8/10 (quick win) — IMPLEMENTAR LOGO**

### Sinais de "vai virar produto"
- **STEP é o formato de troca CAD #1** — toda供应链 (supply chain)
  fala STEP. STEP carrega metadata estruturado (AP214): product
  metadata, design author, organization, validation status, units.
- **Hoje FreeCAD renderiza STEP mas descarta os metadados**. Um tool
  `extract_step_metadata(path)` retorna isso pronto pro LLM usar
  como contexto.
- **Use case imediato**: importar STEP de fornecedor → LLM pergunta
  "qual material?" → responde com base no AP214 em vez de adivinhar.
- **Use case B2B**: automatizar QA — verificar se arquivos STEP de
  fornecedores contêm `validation_date < 2024-01-01` e sinalizar
  itens obsoletos.
- **FreeCAD parseia STEP nativamente** via `importStep`. Custo:
  **zero deps novas, ~30 linhas de código.**

### Sinais de "vai morrer no GitHub"
- **STEP metadata é raramente preenchido** pelos exporters
  comerciais (SolidWorks, NX, CATIA). ~70% dos STEP em circulação
  não têm AP214 preenchido. Mas os 30% que têm são ouro.
- **Concorrentes já fazem**: alguns viewers CAD pagos (TransMagic,
  CAD Exchanger) cobram $5k+ por uma feature equivalente.

### Veredicto
**Implementar em 1 sprint**. Único bloqueador: validar que a versão
de OpenCascade do FreeCAD 0.21/1.0/1.1 parseia AP214 — pode haver
quirk de versão. **Prioridade #1 do Tier 5.**

---

## 3. `cadquery_transpile` — converter CadQuery → FreeCAD Python

**Nota de retorno: 7/10 (moat real) / 4/10 (se mal-feito)**

### Sinais de "vai virar produto"
- **CadQuery tem 10k+ scripts públicos** (Thingiverse derivative,
  GitHub trending, r/cadquery). Cada script é uma receita de design.
- **Portar manualmente cada script é inviável.** Tradutor
  automático cria atalho de **1 mês → 5 minutos** para 95% dos casos.
- **Comunidade CadQuery está migrando para FreeCAD** (CADQuery
  herdou da Bosch, agora usa build123d por cima do OpenCascade).
  Pivot faz a ponte bidirecional ser valiosa.
- **Diferenciação competitiva feroz**: nenhum concorrente MCP-CAD
  tem. Se fizer certo, é killer feature para o nosso projeto.
- **Pode virar paper acadêmico** (AST translation entre DSLs CAD).

### Sinais de "vai morrer no GitHub"
- **CadQuery API é instável**: quebra entre minor versions. Um
  tradutor que serve CadQuery 2.4 quebra em 2.5.
- **Construtores CadQuery são fluentes** (`Workplane().box(10).faces(...)
  .chamfer(2)`) — exige AST reescritor decente, não regex.
- **FreeCAD não tem equivalente exato** para tudo: CadQuery `polarArray`
  não tem par direto em FreeCAD API; precisa heurística.
- **Tempo estimado 3-6 sprints**, contra 1 sprint para `bom_csv_export`
  ou `step_metadata`. ROI por sprint é pior.

### Veredicto
**Implementar MAS como projeto separado** (`freecad_cadquery_bridge`,
repo à parte, em vez de inflar o core do mcp-freecad). v1.0 do
tradutor cobre `box`, `cylinder`, `sphere`, `union`, `difference`,
`translate`, `rotate` (~80% dos scripts reais). v2 expande. **Moat
real, não quick win.**
---

## 4. `bom_csv_export` — Bill of Materials em CSV/JSON

**Nota de retorno: 9/10 (quick win) — IMPLEMENTAR LOGO**

### Sinais de "vai virar produto"
- **Use case B2B universal**: "quantos parafusos M6×20 eu preciso
  pra montar 100 unidades desse chassi?" Sem BOM, o operador conta
  manualmente no Sketcher ou no MES.
- **FreeCAD + Assembly4 já expõe a geometria**, só falta agregar.
- **Padrão de indústria**: Excel/CSV/JSON é lingua franca em
  procurement. Não exige novo schema.
- **Consumível downstream**: ERP/MES lêem CSV direto. **Fecha
  o ciclo CAD → manufacturing**.
- **Estimativa de esforço: 1-2 sprints.** Parser recursivo em
  `get_objects` + `obj.Properties`, formato CSV/JSON, deduplicação
  por (Type, key dimensions), group by family.

### Sinais de "vai morrer no GitHub"
- **Não suporta fasteners catalog** (precisa library de normas
  DIN/ISO/ANSI). Operador vai ter que marcar manualmente o que é
  parafuso padrão vs peça custom.
- **Assembly4 não é instalado em 100% dos deploys**; quem usa
  A2plus tem semântica ligeiramente diferente.
- **Concorre com addons pagos**: BOM tool do Fusion 360, SolidWorks
  Toolbox. Mas esses são $4k/ano; nós somos grátis.

### Veredicto
**Implementar em 1 sprint.** Quick win com retorno B2B
garantido. **Prioridade #2 do Tier 5.** Pode virar case study
("FreeCAD tem BOM tool nativo via MCP").

---

## 5. `dxf_import` / `dxf_export` — bridge DXF

**Nota de retorno: 6/10 (use case real, mas nicho)**

### Sinais de "vai virar produto"
- **DXF é onipresente em 2D**: arquitetura,PCB, laser cutting,
  CNC routing, serralheria. 90% do desenho técnico 2D está em DXF.
- **FreeCAD já importa/exporta DXF** via Draft Workbench. Custo
  de expor limpo é **~20 linhas**.
- **Diferenciação**: poucos LLMs CAD têm MCP dedicado a DXF.

### Sinais de "vai morrer no GitHub"
- **Workflow de laser/CNC cutting é altamente específico de
  máquina**: cada máquina quer layers, colors, line types
  diferentes. Um tool genérico não satisfaz operador de Trotec,
  outro de Epilog, outro de BossLaser.
- **Draft API do FreeCAD tem quirks**: polylines splines
  importados como B-splines ficam off-by-default; usuário precisa
  ajustar tolerance manualmente.
- **Concorre com ferramentas dedicadas**: QCAD ($30), LibreCAD
  (free). Onde está o moat?
- **2D é commodity**. Todo mundo tem DXF hoje.

### Veredicto
**Implementar se sobrar tempo após #1 e #2 do Tier 5.** Não é
quick win, não é moat. É "nice to have" que vira obrigação se
aparecer 1 issue de usuário sobre laser cutting. **Pular por agora.**

---

## 6. `parametric_variants` — família de peças via template

**Nota de retorno: 7/10 (killer feature para SMB manufacturing)**

### Sinais de "vai virar produto"
- **Use case clássico de CAD paramétrico**: "gera parafuso M3,
  M4, M5, M6, M8". Hoje o operador cria 5 FCStd separados ou usa
  Spreadsheet Workbench (que é manual).
- **Combina com workflows.py**: template seria um `Workflow` que
  aceita `params={"sizes": ["M3", "M4", ...]}` e roda 5x.
- **Padrão de indústria**: famílias de peças são commodity em
  fasteners, brackets, sheet-metal parts. Toda fábrica mecânica
  pequena usa.
- **Excel-like UX**: o LLM recebe uma tabela de variantes e gera.
  LLM é **excelente** em parsear "M3 a M10 stepo M1".

### Sinais de "vai morrer no GitHub"
- **Requer modelagem paramétrica correta**: usuário precisa
  definir dimensões como variáveis nomeadas. Sem padrão FreeCAD
  para isso (o Spreadsheet Workbench é uma das piores UIs).
- **Templating genérico** exige interpretador de expressões.
  `Length = {M_size} * 2` — quem valida? FreeCAD Expression Engine
  existe mas é limitado.
- **LLM pode escrever um script equivalente em `execute_code`** —
 竞争力 é baixa.

### Veredicto
**Implementar MAS como generalização do `workflows.py`**, não como
feature separada. Adicionar suporte a `params: dict[str, list]`
no `Workflow.run()` que faz Cartesian product automaticamente.
**Esforço real: meio sprint** vs. 1 sprint anunciado. **Prioridade #3 do Tier 5.**

---

## 7. `interactive_sketch_session` — sessão multi-turn de Sketcher

**Nota de retorno: 9/10 (KILLER FEATURE, máxima prioridade após v1.2)**

### Sinais de "vai virar produto"
- **Sketcher é80% do trabalho CAD real** (já dito antes, vale
  repetir). LLM desenhando em Sketcher com constraints é o
  **Holy Grail do MCP-CAD**.
- **Estado multi-turn é o que humanos fazem**: criar geometry →
  adicionar constraint → solve → ver erro → ajustar. LLM
  consegue modelar isso, mas só se houver tools stateful.
- **Diferenciação massiva**: zero concorrentes têm isso hoje.
  Anthropic, Google, etc. todos querem isso mas não conseguem
  porque o Sketcher API do FreeCAD é pesadíssimo.
- **Daria talk no FreeCAD Day**: comunidade CAD-FOSS inteira
  apareceria.
- **Estimativa revisada**: 6-8 sprints (não XL anunciada). O
  truque é dividir em submódulos:
  - `sketch_session_start()` — 1 sprint
  - `sketch_add_geometry()` — 1 sprint
  - `sketch_add_constraint()` — 2 sprints (tipos complexos)
  - `sketch_solve()` — 1 sprint
  - `sketch_commit_to_part()` — 1 sprint

### Sinais de "vai morrer no GitHub"
- **Sketcher API é notoriamente instável entre versões FreeCAD**.
  Testes de regressão por versão.
- **Constraint solver é O(n²)** no tamanho do sketch; sketches
  grandes (>50 constraints) travam por minutos.
- **Sketcher é a parte do FreeCAD com mais bugs conhecidos**.
  Cada versão conserta 30,引入 20. Vai ser pesadelo de manutenção.

### Veredicto
**Implementar — mas apenas após v1.3+. É o projeto de 6 meses que
define o produto.** Enquanto isso, manter `execute_code` com
template strings para sketches simples (já funciona).

---

## 8. `fem_post_process` — pós-processar resultados CCX

**Nota de retorno: 8/10 (alto, mas depende do Tier 1 v1.2)**

### Sinais de "vai virar produto"
- **`run_fem_analysis` hoje retorna só max stress / max disp**
  (3 números). Engenheiro real quer **mapa de stresses** (PNG
  contour plot), **hot spots**, **histograma de deslocamentos**,
  **animação de deformação** (MP4/GIF).
- **FreeCAD já tem FEM PostPipeline** que renderiza pipelines.
  Custo de expor: ~2 sprints.
- **Concorre com ANSYS Viewer** ($10k+), Abaqus Viewer
  ($5k+). Somos grátis.
- **Case B2B**: empresa que faz análise pontual de peça não
  precisa de ANSYS inteiro; pode usar nosso pipeline.

### Sinais de "vai morrer no GitHub"
- **CCX output é binário `.frd`**, parser custom. Já existem
  parsers em pyCalculiX mas a API não é estável.
- **Render de contour plots em PNG dentro do FreeCAD** é lento
  (10-30s). LLM tem que esperar muito.
- **Visualização interativa não dá pra fazer via MCP**: precisa
  GUI FreeCAD aberta. Só funciona com operador olhando.

### Veredicto
**Implementar como v1.4+**, depois de validar que `run_fem_analysis`
está maduro. Quick win interno: ler `.frd` e extrair tabela de
nós × deslocamento para análise numérica (sem imagem). Depois
acrescentar contour plot PNG. **Prioridade #4 do Tier 5.**

---

## 9. `git_for_fcstd` — versionamento interno

**Nota de retorno: 3/10 (interessante mas redundante)**

### Sinais de "vai virar produto"
- **FCStd é um zip** com `Document.xml` + thumbnails + shapes
  binários. Diff textual só funciona em `Document.xml`, que é onde
  estão as propriedades.
- **Integrar com git externamente** é o caso real (operador
  commita FCStd no git normalmente).
- **Ter "merge" de FCStd é difícil**: shapes binários têm
  conflitos impossíveis de resolver sem heurística pesada.

### Sinais de "vai morrer no GitHub"
- **Já existe**: `git archive` + textual diff funciona razoavelmente
  para 60% dos casos. `fcstd-diff` como tool separado dá cobertura
  de 80%.
- **O usuário já tem git**. Adicionar MAIS um sistema de
  versionamento confunde.
- **Concorre com Onshape / Plasticity** que já fazem CAD-Version-
  Control em cloud. Não somos cloud.

### Veredicto
**Não construir.** Resolve problema que já está 80% resolvido por
`git` + `gitattributes` para XML. Se aparecer issue real, fazer
`fcstd_diff()` simples (já temos `diff_documents`). **Skip.**

---

## Tabela final de retorno

| # | Feature | Nota | Esforço real | Quick win? | Moat? |
|---|---|:-:|---|:-:|:-:|
| 1 | `voice_to_solid` | **2** | L | ❌ | ❌ |
| 2 | `step_ap214_extract_metadata` | **8** | S | ✅ | ✅ |
| 3 | `cadquery_transpile` | **7** | XL | ❌ | ✅ |
| 4 | `bom_csv_export` | **9** | S-M | ✅ | ⚠️ |
| 5 | `dxf_import/export` | **6** | S-M | ⚠️ | ❌ |
| 6 | `parametric_variants` | **7** | M (½ sprint) | ✅ | ⚠️ |
| 7 | `interactive_sketch_session` | **9** | XL | ❌ | ✅✅ |
| 8 | `fem_post_process` | **8** | L | ❌ | ✅ |
| 9 | `git_for_fcstd` | **3** | XL | ❌ | ❌ |

## Recomendações ordenadas por ROI/sprint

| Prioridade | Feature | Razão |
|:-:|---|---|
| **1º** | `step_ap214_extract_metadata` (8/10, S) | Quick win que abre porta B2B |
| **2º** | `bom_csv_export` (9/10, S-M) | Quick win B2B mais visível |
| **3º** | `parametric_variants` (7/10, ½ sprint) | Generalização de `workflows.py` |
| **4º** | `fem_post_process` (8/10, L) | Constrói sobre `run_fem_analysis` maduro |
| **5º** | `cadquery_transpile` (7/10, XL) | Moat real, projeto separado |
| **6º** | `interactive_sketch_session` (9/10, XL) | Killer feature, projeto 6 meses |
| skip | `voice_to_solid` | Marketing > produto |
| skip | `dxf_import/export` | Commodity |
| skip | `git_for_fcstd` | Já existe (`git`) |

**Resumo executivo:** Das 9, **4 têm retorno real comprovado** (step
metadata, BOM, parametric, FEM post), **2 são moats de longo prazo**
(CadQuery, Sketcher), **3 devem ser descartadas** (voice, DXF, git).

