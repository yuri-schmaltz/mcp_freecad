# Plano de Profissionalização — `mcp-freecad`

> De demo a produto. Roadmap priorizado, com critérios de aceite e estimativa de risco por item.
>
> Baseline: `yuri-schmaltz/mcp-freecad` @ `88d985a` (fork de `neka-nat/freecad-mcp`, 32 commits à frente, v0.3.0).
> Status atual: 195 testes passando, ruff/mypy limpos, cobertura 54%.

## TL;DR

O fork já é um trabalho sério: TLS, auth, path-traversal fix, idempotency, timeouts configuráveis. Para virar **produto**, faltam três eixos:

1. **Segurança de superfície pública** — o blocklist atual deixa brechas (`socket`, `urllib`, `ctypes`, `__import__`, `pickle`), e `remote_enabled=true` sem TLS+auth é RCE na rede local. **Bloqueador.**
2. **Confiabilidade operacional** — sem retry, sem circuit breaker, sem métricas, sem graceful shutdown. Cai o servidor e ninguém fica sabendo. **Bloqueador.**
3. **UX/Professional polish** — gabarito PT-BR injetado em toda resposta, examples com placeholder, README sem badges, nenhuma matriz de compatibilidade FreeCAD. **Alto impacto, baixo risco.**

Nada disso requer refatoração massiva. Tudo é patch cirúrgico.

---

## Diagnóstico resumido

| # | Severidade | Categoria | Item | Onde |
|---|---|---|---|---|
| 1 | 🔴 Crítico | Segurança | `execute_code` blocklist incompleto (socket, urllib, ctypes, `__import__`, `pickle`, `marshal`, `breakpoint`, `compile`, `globals()`, `getattr()`) | `src/freecad_mcp/guidelines.py` |
| 2 | 🔴 Crítico | Segurança | `remote_enabled=true` sem TLS+auth abre RCE na rede | `addon/.../rpc_server.py:start_rpc_server` |
| 3 | 🔴 Crítico | Confiab. | Sem retry em falha transitória de RPC | `src/freecad_mcp/freecad_client.py` |
| 4 | 🔴 Crítico | Confiab. | Sem circuit breaker — uma falha persistente derruba a sessão MCP inteira | `src/freecad_mcp/freecad_client.py` |
| 5 | 🟠 Alto | UX | Gabarito PT-BR prependido em **toda** resposta, sem opt-out default | `src/freecad_mcp/responses.py`, `src/freecad_mcp/server.py` |
| 6 | 🟠 Alto | UX | `mcp_instructions` carrega 2.6KB PT-BR por padrão — em chamadas em inglês é poluição de tokens | `src/freecad_mcp/server.py:_load_system_directives` |
| 7 | 🟠 Alto | Operacional | Sem métricas (counters, latência p50/p95) — só `health_check` binário | `src/freecad_mcp/server.py` |
| 8 | 🟠 Alto | Operacional | Sem graceful shutdown — RPC server mata tarefas em flight no stop | `addon/.../rpc_server.py:stop_rpc_server` |
| 9 | 🟡 Médio | Validação | Tools aceitam `dict[str, Any]` cru — sem schema validation | `src/freecad_mcp/server.py` |
| 10 | 🟡 Médio | DX | Sem allowlist de tools por ambiente (produção deveria poder desligar `execute_code`) | `src/freecad_mcp/server.py` |
| 11 | 🟡 Médio | DX | Logging é texto livre — impossível de parsear em log aggregator | `src/freecad_mcp/server.py:configure_logging` |
| 12 | 🟡 Médio | Docs | README sem badges, sem matriz FreeCAD 0.21/1.0/1.1, sem tabela de comparação | `README.md` |
| 13 | 🟡 Médio | Docs | `examples/langchain/react.py` tem `path/to/mcp-freecad` placeholder | `examples/` (resolvido em v1.0.0: pasta `examples/` removida do repo) |
| 14 | 🟢 Baixo | DX | `add_part_from_library` não checa tamanho do arquivo (DoS) | `addon/.../parts_library.py` |
| 15 | 🟢 Baixo | DX | `get_active_screenshot` devolve base64 — desperdício de payload | `src/freecad_mcp/operations/core.py` |
| 16 | 🟢 Baixo | DX | Sem rate limit no RPC (LLM pode martelar `get_view` com screenshots) | `addon/.../rpc_server.py` |

---

## Roadmap (por tier)

### Tier 1 — Bloqueadores de produção

#### T1.1 — Gabarito PT-BR opt-in (não opt-out)
**Hoje**: toda resposta de texto começa com "Analisei o documento e usarei suas instruções em minhas respostas." por default, em PT-BR, e o env var pra desligar é `FREECAD_MCP_NO_DIRECTIVE_PREFIX=1`. Quem instala via `uvx` e fala inglês recebe lixo.

**Depois**:
- Default: gabarito **desligado**.
- Opt-in: `FREECAD_MCP_LOAD_GABARITO=1`.
- Carrega `gabarito_ia_extracted.txt` em **inglês** quando o env do usuário é `en*` (ou simplesmente em PT-BR só se `FREECAD_MCP_GABARITO_LANG=pt-BR`).
- Removemos a frase hardcoded de `responses.py` (substituímos por config).

**Critério de aceite**:
- `pytest` passa; novo teste em `test_responses.py` valida que o prefixo NÃO aparece por default e aparece com `FREECAD_MCP_LOAD_GABARITO=1`.
- README atualizado.

**Risco**: baixo. Só muda default e uma string. Backward-compat: usuário que quer o comportamento antigo seta `FREECAD_MCP_LOAD_GABARITO=1`.

#### T1.2 — Estender o code blocklist
**Hoje**: o blocklist pega `eval`, `exec`, `os.system`, `os.popen`, `subprocess.*`, `import subprocess`, `rm -rf /`, `shutdown`, `reboot`. Buracos conhecidos: `socket.*`, `urllib.request.*`, `ctypes.CDLL`, `__import__`, `pickle.loads`, `marshal.loads`, `breakpoint()`, `compile()`, `globals()`, `getattr(obj, "__class__")`, `().__class__.__bases__[0].__subclasses__()`.

**Depois**: adicionar regex com word-bounding:
```
\b__import__\s*\(
\bpickle\s*\.\s*(?:load|loads|dump|dumps)\s*\(
\bmarshal\s*\.\s*(?:load|loads|dump|dumps)\s*\(
\bsocket\s*\.\s*(?:socket|create_connection|gethostbyname)\s*\(
\burllib\s*\.\s*(?:request|urlopen|urlretrieve)\s*\(
\bctypes\s*\.\s*(?:CDLL|WinDLL|cdll|windll)\s*\(
\bbreakpoint\s*\(
\bcompile\s*\(
\bglobals\s*\(\s*\)
```

Adicionalmente:
- O check passa a ser executado **também** em `obj_properties` (não só em `execute_code` direto), porque `Placement` aceita dict aninhado que pode carregar strings.
- Refatora: uma única função `scan_dangerous_tokens(text) -> list[match]` retorna **todos** os matches, não só o primeiro, para o log ter cobertura completa.

**Critério de aceite**:
- 30+ novos casos de teste em `test_guidelines.py` (incluindo os payloads acima).
- Tabela de testes: cada padrão com 1 caso positivo + 1 caso negativo.
- Doc atualizado em `SECURITY.md`.

**Risco**: baixo. Adição de regex; não muda comportamento default.

#### T1.3 — Tool allowlist (kill switch de `execute_code`)
**Hoje**: 18 tools expostos via MCP, todos ativos. Em produção, `execute_code` é o maior vetor.

**Depois**:
- Env var `FREECAD_MCP_DISABLED_TOOLS=execute_code,get_active_screenshot` desabilita tools no boot.
- Env var `FREECAD_MCP_REQUIRED_TOOLS=create_object,edit_object,delete_object,get_view` (whitelist mode): se setado, **só** os tools listados ficam ativos.
- Tools desabilitados respondem com erro explicativo: `"Tool 'execute_code' is disabled in this environment. See FREECAD_MCP_DISABLED_TOOLS."`
- Log de warning no startup listando tools desabilitados.

**Critério de aceite**:
- 5 testes em novo `test_server_module.py` cobrindo: sem env = tudo ligado; `DISABLED_TOOLS` filtra; `REQUIRED_TOOLS` restringe; conflito entre os dois = erro fatal no boot.
- README explica o uso para deploys multi-tenant.

**Risco**: baixo. É adição de feature opt-in.

#### T1.4 — Connection retry + circuit breaker
**Hoje**: o `FreeCADConnection` cria um `ServerProxy` no `__init__`. Falha transitória (FreeCAD reiniciando, RPC server crashed) → a próxima chamada retorna erro e o client MCP considera erro permanente. Não há retry.

**Depois**:
- Wrap das chamadas em um decorator `@resilient` que:
  - Detecta erros transientes (`ConnectionError`, `socket.timeout`, `ProtocolError`, `xmlrpc.client.Fault` no subset considerado transiente).
  - Aplica exponential backoff: 100ms, 200ms, 400ms (max 3 tentativas).
  - Se 3 falhas seguidas → abre circuit breaker (60s); durante esse tempo, todas as chamadas retornam erro imediato sem tentar.
  - Half-open após 60s: 1 chamada de teste; sucesso → fecha; falha → reabre.
- Configurável via `FREECAD_MCP_CB_THRESHOLD` (default 3), `FREECAD_MCP_CB_RESET_S` (default 60).
- Métrica de circuit state exposta no `health_check`.

**Critério de aceite**:
- 8+ testes em `test_freecad_client.py` simulando fault injection com mock de `ServerProxy`.
- `health_check` agora retorna `circuit_state`, `consecutive_failures`, `last_error`.

**Risco**: médio. Toca o cliente mas é isolado. Não muda protocolo.

#### T1.5 — RPC server recusa remote sem TLS+auth
**Hoje**: `Toggle_Remote_Connections` salva `remote_enabled=true` e o usuário tem que configurar TLS+auth manualmente. Esquecer disso = RCE na rede.

**Depois**:
- `start_rpc_server` verifica: se `host != "localhost"`, exige `FREECAD_MCP_TLS_CERT` AND `FREECAD_MCP_TLS_KEY` AND `FREECAD_MCP_AUTH_TOKEN`. Se faltar algum → **refusa iniciar** e loga warning barulhento.
- `Toggle_Remote_Connections.Activated(checked=True)` faz a mesma checagem preventiva; se faltar, exibe dialog de erro no FreeCAD e NÃO salva a setting.
- Mensagem clara: "Remote connections require TLS and a bearer token. Set FREECAD_MCP_TLS_CERT, FREECAD_MCP_TLS_KEY, and FREECAD_MCP_AUTH_TOKEN before enabling."

**Critério de aceite**:
- 4 testes em novo `test_rpc_server_security.py` validando refusals.
- Update em `SECURITY.md` (a entrada "What is not in scope" sai do lugar).

**Risco**: baixo. Só endurece uma porta que já é opt-in.

---

### Tier 2 — Production-grade

#### T2.1 — Pydantic nos parâmetros
**Hoje**: 18 tools, todos com `obj_properties: dict[str, Any] | None = None`. Sem schema, sem validação client-side, mensagens de erro vagas quando o LLM manda tipo errado.

**Depois**:
- Adicionar `pydantic>=2.0` como dependência.
- Modelo `CreateObjectRequest`, `EditObjectRequest`, `FemConstraintReferences`, etc.
- MCP `Annotated[..., Field(description=...)]` para OpenAPI schema.
- Erro de validação vira mensagem clara pro LLM, não stacktrace.

**Critério de aceite**:
- 2 tools migrados como piloto (`create_object`, `edit_object`).
- 6+ testes de validação (campo faltando, tipo errado, valor inválido).
- Compat: `dict[str, Any]` ainda aceito com `model_validate(obj)` para não quebrar tools que o LLM chama via `execute_code` no free-form.

**Risco**: médio. Mudança grande de API. **Pode quebrar scripts antigos.** Mitigação: aceitar ambos via discriminated union.

#### T2.2 — Métricas
**Hoje**: `health_check` retorna uptime, queue sizes, cached_responses. Sem contadores de operação, sem latência.

**Depois**:
- `PrometheusRegistry` em-memória no `ServerState`.
- Counters: `freecad_mcp_tool_calls_total{tool, status}`.
- Histograms: `freecad_mcp_tool_duration_seconds{tool}` (buckets: 0.01, 0.1, 1, 10, 60).
- Gauges: `circuit_state` (0=closed, 1=half_open, 2=open), `freecad_connection_up` (0/1).
- Endpoint `/metrics` (somente localhost) exposto pelo RPC server, formato `text/plain; version=0.0.4`.
- `health_check` ganha bloco `metrics_summary` com contagens e p50/p95/p99.

**Critério de aceite**:
- 5 testes em `test_metrics.py`.
- README seção "Monitoring" com scrape config Prometheus.

**Risco**: baixo. Adição de feature opt-in (`FREECAD_MCP_METRICS_ENABLED=1`).

#### T2.3 — Logging estruturado JSON
**Hoje**: `logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")`. Parseável por olho humano, não por log shipper.

**Depois**:
- `FREECAD_MCP_LOG_FORMAT=json` ativa formatter JSON: `{"ts": ..., "level": ..., "logger": ..., "msg": ..., ...extras}`.
- Sem mudança de API: `logger.info("foo", extra={"key": "value"})` continua funcionando; o JSON formatter puxa dos extras.

**Critério de aceite**:
- 3 testes em `test_logging.py` validando os dois formatos.
- README menciona `LOG_FORMAT` na seção de env vars.

**Risco**: muito baixo. Adição pura.

#### T2.4 — Test markers + smoke runner
**Hoje**: marker `freecad` (integration), `slow`. Mas o CI só roda `pytest` sem marker filter — não há `pytest -m "not freecad"`. Quem rodar local sem FreeCAD pega 0 testes (não é o caso, mas a config não está explícita).

**Depois**:
- `pytest.ini` com:
  ```
  [pytest]
  markers =
      freecad: integration tests requiring FreeCAD
      slow: long-running tests
  addopts = -m "not freecad"
  ```
- `tests/run_all_tests.py` substituído por `make test` ou instrução clara no CONTRIBUTING.
- Novo `tests/test_smoke_imports.py` que importa `freecad_mcp` e o addon e garante que nada quebra em import time (catches `ImportError` em refactors futuros).

**Critério de aceite**:
- CI roda `pytest -m "not freecad"` por default.
- Novo marker tem doc.

**Risco**: muito baixo.

#### T2.5 — README profissional
**Hoje**: tem demo, install, tools, contributors. Falta: badges, matriz FreeCAD, security disclosure, comparison, license, FAQ, troubleshooting, version compatibility.

**Depois**:
- Badges: CI, PyPI, license, version, Python supported.
- Tabela de compatibilidade: FreeCAD 0.21, 1.0, 1.1; OS Windows/macOS/Linux; Python 3.11/3.12/3.13.
- Seção "When NOT to use" (transparência).
- Seção "Production deployment checklist" (TLS, auth, sandbox, monitoring).
- "Reporting a security issue" proeminente (link pro SECURITY.md).
- Comparação com `mcp-blender`, `onshape-mcp`, etc. (se existirem).
- Diagrama de arquitetura mermaid.

**Critério de aceite**:
- Markdown lint passa.
- Sem info desatualizada.

**Risco**: nenhum.

---

### Tier 3 — Polish (post-MVP)

#### T3.1 — Decompose `rpc_server.py` (1659 LOC → ~5 módulos de ~300)
- `rpc_dispatch.py` (queue + QTimer)
- `rpc_methods.py` (as classes de método)
- `rpc_settings.py` (load/save + fallback)
- `rpc_security.py` (TLS + auth + IP filter)
- `rpc_screenshot.py` (transcode + view utils)
**Risco**: alto. Requer FreeCAD rodando para validar. Só fazer com suíte de integração local.

#### T3.2 — Fuzzing do blocklist
Adotar `atheris` ou `hypothesis` para property-based testing. Inserir 10k inputs randomizados contra o `check_code_conflict` e medir false-positive rate. Esperado: <0.1%.

#### T3.3 — Suporte oficial a múltiplas versões FreeCAD
CI matrix com FreeCAD 0.21, 1.0, 1.1. Hoje: só roda sem FreeCAD. Requer Docker images com FreeCAD instalado.

#### T3.4 — Versionamento semântico automatizado
Adotar `python-semantic-release` ou `release-please`. Bumps automáticos em PR.

---

## Sequência de execução proposta

```
Semana 1:   Tier 1 inteiro (T1.1 → T1.5) + testes
Semana 2:   Tier 2 inteiro (T2.1 → T2.5) + atualização de docs
Semana 3:   Tier 3 (Fuzzing + decomposição parcial)
Sempre:     CHANGELOG.md atualizado, git tag por release
```

## Critérios de "produto profissional"

Para marcar v1.0:

- [ ] Todos os itens de Tier 1 entregues e com testes
- [ ] ≥ 80% de cobertura de código
- [ ] CI verde em Python 3.11/3.12/3.13 com 3 OS (ubuntu/macos/windows)
- [x] PyPI publicado (com nome próprio, não `freecad-mcp` collidindo com upstream) — feito no Cut Oficial v1.0.0; nome PyPI agora é `mcp-freecad`.
- [ ] Documentação publicada em readthedocs
- [ ] Docker image oficial
- [ ] 1 release de segurança feito e divulgado publicamente (transparência)
- [ ] Adoption: ≥ 3 issues de bug report de users externos pós-release

## Não-objetivos (explícitos)

- Tornar-se uma alternativa ao plugin CAD nativo — é bridge, não modelador.
- Sandbox completo de `execute_code` — documentado como limitação, recomenda-se container.
- FreeCAD < 0.21 — muito antigo, ABI mudou demais.
- Auto-update — feature perigosa pra ferramenta de segurança.

## Auditorias e planos posteriores

* [`AUDIT_2026-08-27.md`](AUDIT_2026-08-27.md) — feature drop v1.1.0
  (6 novos tools MCP, 4 resources, 6 novos módulos, 84 testes novos).
* [`CHANGELOG.md`](../CHANGELOG.md) — histórico de releases.
