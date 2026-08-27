# Backlog de features de alto retorno para mcp-freecad

> **Status:** brainstorming priorizado. Cada item tem rationale,
> esforço estimado (S/M/L/XL), dependências, e o "por que agora".
> Itens marcados **[v1.2]** têm boa chance de entrar no próximo drop;
> **[v1.3+]** são ideias maiores que precisam de pesquisa primeiro.
>
> Contexto: estado atual em v1.1.0 = **24 tools, 4 resources**,
> profiler + replay + diff + workflows + streaming + web UI,
> addon com dock panel, security-first (TLS + bearer + policy gate).

---

## Tier 1 — Preencher buracos óbvios no FreeCAD

### **[v1.2]** `close_document(doc_name, save_first=True)` — fechar documento
- **Por que:** só temos `create_document` e `save_document`. Sessões
  longas vazam handles de documento e ViewProvider; um LLM que
  itera muito acumula 50+ docs abertos.
- **Esforço:** S (~30 linhas + 4 testes).
- **Dependências:** nenhuma. RPC: `FreeCAD.closeDocument(name)`.

### **[v1.2]** `reopen_document(path)` — abrir `.FCStd` existente
- **Por que:** sem isso o LLM só consegue mexer em docs que já
  estão abertos manualmente. Bloqueador para fluxos de "reabrir
  projeto salvo".
- **Esforço:** S.
- **Dependências:** RPC: `App.openDocument(path)`.

### **[v1.2]** `duplicate_object(doc_name, obj_name, new_name=None, count=1, with_dependencies=False)` — clonar N vezes
- **Por que:** workflow extremamente comum (parafusos, furos
  padrão, padrões de array). Hoje só dá pra fazer via `execute_code`.
- **Esforço:** S.
- **Dependências:** `App.ActiveDocument.copyObject` (já usado em
  partes do addon).

### **[v1.2]** `set_view(view_name, zoom_to_fit=True)` — definir view
- **Por que:** par perfeito para `get_view`. Hoje o LLM só pode
  *observar* a view, não *controlá-la*. `set_view` +
  `zoom_to_fit(selection)` = "centraliza na seleção que acabei de
  criar".
- **Esforço:** S. RPC: `Gui.ActiveDocument.ActiveView.viewIsometric()`
  etc. + `Gui.SendMsgToActiveView("ViewFit")`.
- **Dependências:** nenhuma.

### **[v1.2]** `get_mass_properties(doc_name, obj_names=None)` — volume, área, centróide, massa (se houver material)
- **Por que:** "qual o volume dessa peça?" é uma das 5 perguntas
  mais frequentes em CAD. Calcula via `Shape.computeVolume()` /
  `computeCentroid()`. Não precisa de FEM.
- **Esforço:** S-M.
- **Dependências:** `Part::Feature.Shape` (já temos via `get_object`).

### **[v1.2]** `measure_distance(doc_name, obj_a, sub_a, obj_b, sub_b)` — distância entre features
- **Por que:** "qual a distância entre Face3 e Edge7?" requer
  seleção manual hoje. LLM é excelente em parsear
  semântica→referência, péssimo em clicar.
- **Esforço:** M (lógica de resolver Vertex/Edge/Face).
- **Dependências:** Part Workbench.

### **[v1.2]** `search_objects(doc_name, query, fields=None)` — busca por nome/propriedade/tipo
- **Por que:** `get_objects` retorna 200 objetos. Filtrar é
  trabalho braçal. Query suporta regex em `Name`, `Label`, `TypeId`
  e qualquer propriedade (`Length > 50` etc.).
- **Esforço:** M.
- **Dependências:** nenhuma (parsing client-side sobre
  `serialize_object`).

### **[v1.2]** `validate_constraints(doc_name, obj_name)` — checa geometria (degenerate, self-intersecting)
- **Por que:** `execute_code` com `Shape.isValid()` é o uso
  comum #2. Tool dedicada: menos calls, menos código hostil,
  relatório estruturado.
- **Esforço:** M.
- **Dependências:** OCC (`BRepCheck_Analyzer`).

---

## Tier 2 — Diferenciação técnica (features que outros MCP CAD não têm)

### **[v1.2]** `snapshot_document(doc_name) → snapshot_id` + `restore_snapshot(snapshot_id)` + `list_snapshots()`
- **Por que:** checkpoint/restore estilo `git stash`. Um LLM pode
  tentar 5 abordagens para um design e o operador escolhe a
  melhor. State machine: `snapshot_id` = hash do JSON serializado
  + FreeCAD's internal state via `Document.Content` zip.
- **Esforço:** L (precisa serializar `Document.Content` para um
  blob no disco).
- **Dependências:** `FreeCAD.Units` + `zipfile` (stdlib).

### **[v1.2]** `batch_execute(operations: list[ToolCall])` — executar N tools atomicamente
- **Por que:** o LLM geralmente quer fazer 5 edits relacionados
  (criar box, criar cylinder, posicioná-los, fazer boolean).
  Hoje são 5 round-trips. `batch_execute` é 1 round-trip, e
  ainda permite rollback parcial se a operação 4 falhar.
- **Esforço:** M.
- **Dependências:** nenhuma (precisa de circuit breaker awareness
  para não acumular timeout).

### **[v1.2]** `get_tool_history(doc_name, since_seconds=None)` — git log-like dos objetos
- **Por que:** "o que mudou nessa peça nos últimos 30s?".
  Hook no `Document.Object` add/remove property + persistir em
  ring buffer. Cruza com replay pra ter timeline visual.
- **Esforço:** M-L.
- **Dependências:** nenhuma, mas precisa de hook no addon.

### **[v1.2]** `dry_run(tool_name, args)` — executa sem persistir
- **Por que:** "se eu rodar `delete_object`, o que acontece?".
  Combinado com replay/snapshot, vira "what-if" nativo.
- **Esforço:** M (precisa transação FreeCAD com rollback).
- **Dependências:** `FreeCAD.ActiveDocument.openTransaction()` +
  `abortTransaction`.

### **[v1.2]** `export_to_glb(doc_name, path, optimize=True)` — export 3D moderno
- **Por que:** STL é antigo (1987). GLB é o formato web/AR/3D
 打印 moderno. Hoje só temos STL/STEP/IGES/OBJ via
  `export_object`. GLB abre o leque para visualizadores web.
- **Esforço:** M (FreeCAD não exporta GLB nativamente; precisa
  de `importOBJ` ou instalar Addon "Glb Tools").
- **Dependências:** Addon "Glb Tools" ou conversão via
  trimesh/pyrender.

### **[v1.3+]** `sketch_create` / `sketch_add_geometry` / `sketch_add_constraint` / `sketch_solve` — Sketcher programático
- **Por que:** 80% do design real começa num sketch. Hoje só dá
  pra fazer via `execute_code` com strings enormes. Sketcher via
  MCP = killer feature competitiva.
- **Esforço:** XL (Sketcher API é a mais complexa do FreeCAD; tem
  dezenas de tipos de constraint). Provavelmente uma feature
  inteira, não um tool.
- **Dependências:** Sketcher Workbench.

### **[v1.3+]** `assembly_add_part` / `assembly_add_constraint` — Assembly4
- **Por que:** montagens são 50% do trabalho CAD profissional.
  FreeCAD tem Assembly4 (Solvespace) e A2plus. MCP dedicado é
  diferenciação massiva.
- **Esforço:** XL.
- **Dependências:** Assembly4 Addon instalado.

### **[v1.3+]** `techdraw_create_view` / `techdraw_export_pdf` — desenho técnico
- **Por que:** TechDraw é o output final de qualquer peça que vai
  pra manufatura. Hoje é 100% manual no GUI.
- **Esforço:** L.
- **Dependências:** TechDraw Workbench.

---

## Tier 3 — Observabilidade / produção

### **[v1.2]** `GET /metrics` (endpoint HTTP nativo) + `GET /healthz` (k8s probe)
- **Por que:** o README já tem um snippet sugerindo expor
  `metrics.py` num sidecar. Fazer isso nativo elimina a fricção
  de "qual framework usar?". `healthz` é o contrato de facto de
  Kubernetes/Docker.
- **Esforço:** S (FastAPI é opt-in; o endpoint pode coexistir com
  o `web_ui.py` ou ser módulo separado).
- **Dependências:** nenhuma se já temos FastAPI em web_ui.

### **[v1.2]** `get_circuit_breakers()` — estado detalhado de cada breaker
- **Por que:** já temos `health_check` que retorna 1 breaker
  agregado. Com 5+ tools pesadas (fem, export, parts) cada uma
  com seu próprio breaker, o operador precisa ver qual abriu.
- **Esforço:** S.
- **Dependências:** `circuit_breaker.CircuitBreaker.state` (já
  exposto em `circuit_breaker.py`).

### **[v1.2]** `set_rate_limit(tool_name, calls_per_minute)` — rate limiter por tool
- **Por que:** um LLM travado em loop pode martelar `get_view` 60×
  por minuto. Rate limit por tool protege FreeCAD sem matar
  throughput legítimo.
- **Esforço:** M.
- **Dependências:** nenhuma (token bucket in-process).

### **[v1.3+]** OpenTelemetry tracing (`opentelemetry-api` + exporter OTLP)
- **Por que:** métricas Prometheus são agregados; traces OTLP
  mostram causalidade ("essa chamada de `edit_object` veio
  dessa chamada de `run_workflow`"). Para debugging distribuído
  futuro (web UI + LLM host + FreeCAD), tracing é insubstituível.
- **Esforço:** L.
- **Dependências:** `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp`.

### **[v1.3+]** `audit_log(query, since, tool_name=None, request_id=None)` — log persistente de TODOS os tool calls
- **Por que:** replay é por sessão efêmero. Audit log é append-only
  em SQLite/SQL, retido N dias, queryable por operador. Compliance
  para deployments pagos/multi-tenant.
- **Esforço:** L.
- **Dependências:** `sqlite3` (stdlib).

---

## Tier 4 — DX / segurança

### **[v1.2]** `preview_diff(before_doc, after_doc, format="ascii"|"image")` — preview antes de aplicar
- **Por que:** `diff_documents` é útil, mas um ASCII art / heatmap
  da geometria (top-down view) ajuda muito em revisão. Pode
  retornar imagem PNG base64 (já temos `get_view`).
- **Esforço:** M.
- **Dependências:** render 2D do FreeCAD.

### **[v1.2]** `validate_script(code, level="basic"|"strict")` — linter estático antes de `execute_code`
- **Por que:** `execute_code` é o vetor de risco #1. Um linter
  que rejeita `os.system` antes de chegar no FreeCAD dá uma
  camada extra sem mover o código.
- **Esforço:** M (regex + AST simples).
- **Dependências:** `ast` (stdlib) + `guidelines.py` (já existe).

### **[v1.2]** `get_active_selection()` / `set_selection(...)` — bridge com a GUI selection
- **Por que:** LLM pode precisar "esse objeto específico que o
  operador acabou de clicar". Bridge para
  `Gui.Selection.getSelection()`.
- **Esforço:** S.
- **Dependências:** nenhuma.

### **[v1.2]** `watch_property(doc_name, obj_name, prop_name, callback_url)` — webhook em mudança de propriedade
- **Por que:** "notifica meu agente quando Length mudar de 10
  para 11". Com callbacks HTTP, vira o primeiro passo para
  reactive MCP (LLM que reage a eventos).
- **Esforço:** L (precisa observer pattern no FreeCAD).
- **Dependências:** `FreeCAD.addPropertyObserver` + HTTP client
  (httpx já é dep).

### **[v1.3+]** OAuth2 / OIDC bearer (substituir shared token por JWT)
- **Por que:** bearer estático não escala pra SSO empresarial.
  JWT permite rotação, escopo por tool, expiração.
- **Esforço:** XL.
- **Dependências:** `authlib`, keycloak/Okta externo.

---

## Tier 5 — "Wild ideas" / pesquisa

### **`voice_to_solid`** — fala natural → caixa/cylinder
- Mic input → Whisper local → LLM → `create_object`. Demo-killer.
- Esforço: L. Deps: faster-whisper, sounddevice.

### **`step_ap214_extract_metadata`** — ler metadata de STEP
- Part 21 STEP carrega author, organization, units, validation
  date. Hoje a gente só renderiza. `extract_metadata` retorna
  tudo estruturado.
- Esforço: S. Deps: nenhuma (FreeCAD parseia STEP).

### **`cadquery_transpile`** — converter CadQuery Python → FreeCAD Python
- CadQuery é o "OpenSCAD moderno". Tradutor automático = ponte
  pra 100k+ scripts existentes da comunidade.
- Esforço: XL. Deps: AST manipulation.

### **`bom_csv_export`** — gerar BOM (Bill of Materials) em CSV/JSON
- Hoje `get_parts_list` é da parts library. BOM é da geometria
  atual. "Quantos parafusos M6 eu preciso?" exige varrer
  assemblies.
- Esforço: M. Deps: nenhuma (loop em `get_objects`).

### **`dxf_import` / `dxf_export`** — bridge DXF
- 90% dos desenhos 2D do mundo estão em DXF. Hoje só dá via
  Draft Workbench manual.
- Esforço: M. Deps: Draft Workbench + `importDXF`.

### **`parametric_variants`** — eval `{var}` em dimensões
- "Crie uma família de parafusos M3, M4, M5, M6, M8". Hoje é
  loop manual em `create_object`. `parametric_variants` aceita
  template + dict de valores e gera os N objetos.
- Esforço: M. Deps: nenhuma (estende workflows.py).

### **`interactive_sketch_session`** — sessão multi-turn de Sketcher
- Em vez de criar geometria em 1 tool call, abre uma "sessão"
  com `add_geometry`, `add_constraint`, `solve`, `undo`,
  `commit`. Mais natural para LLM porque modela o jeito humano
  de trabalhar com Sketcher.
- Esforço: XL. Deps: state machine + Sketcher API.

### **`fem_post_process`** — pós-processar resultados CCX
- Hoje `run_fem_analysis` retorna só max stress / max disp.
  `fem_post_process` carrega o `.frd` e retorna contour plots
  PNG, hot spots, animações de deformação.
- Esforço: L. Deps: `OCC`, parser FRD.

### **`git_for_fcstd`** — versionamento interno do FCStd
- FCStd é um zip. Extrair, diffar, merge. Toolchain estilo git
  mas operando direto no zip sem externalizar.
- Esforço: XL. Deps: nenhuma (zipfile + JSON).

---

## Resumo de esforço estimado

| Tier | # itens | Esforço total |
|---|---:|---|
| 1 (CAD básico) | 8 | ~3-4 sprints (1 pessoa) |
| 2 (Diferenciação) | 8 | ~6-8 sprints |
| 3 (Observabilidade) | 5 | ~2-3 sprints |
| 4 (DX/segurança) | 5 | ~3 sprints |
| 5 (Wild) | 9 | ~10+ sprints |

**Recomendação para v1.2:** pegar os 4 itens **[v1.2]** do Tier 1 que
mais pagam (close_document, duplicate_object, search_objects,
mass_properties) + 2 do Tier 3 (metrics endpoint, circuit
breakers detalhados). Isso dá 6 features ~3 sprints, mantém o
cadência mensal.

**Recomendação para v1.3:** investir pesado em **Sketcher via MCP**
(Tier 2, item XL). É a feature que vai diferenciar o projeto de
todos os outros MCP-CAD.