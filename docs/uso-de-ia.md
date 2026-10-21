# Como a IA foi usada (e verificada) neste desafio

O enunciado permite e encoraja IA, desde que eu saiba explicar cada decisão e cada linha. Este documento diz o que a IA fez, o que eu decidi e, principalmente, como o que a IA produziu foi verificado.

## Divisão de papéis

| Quem | Fez |
|---|---|
| **Eu (Giovane)** | As decisões que não delego. Escopo, prazo, linguagem (Python), conta AWS, custo e quotas. Aceitar ou recusar cada ADR. A cada rodada de revisão, decidir o que corrigir, o que implementar e o que fica declarado como limitação: por exemplo, conferir `id_conta` contra o titular e separar um security group por serviço, em vez de só declarar o risco. O processo que o código tem de passar antes de entrar: gates, painéis adversariais com prova de conceito e medição na AWS. Revisão final e defesa |
| **IA, modelo orquestrador** | Pesquisa dirigida, plano, ADRs (propostos para a minha aprovação), **domínio e casos de uso** (limites, autorização, contabilização, ACL, reconciliador), contratos (OpenAPI, eventos, problem types), revisão linha a linha de tudo que foi delegado |
| **IA, modelo executor** | Trabalho mecânico com especificação fechada e critério de aceite: adapters HTTP, Dockerfiles, relay, Terraform, k6, CI |

## Processo

1. **Pesquisa com fonte primária.** Três frentes paralelas (empresa e contexto; quotas e serviços AWS em sa-east-1; RFCs, padrões e normas do BCB), cada afirmação com URL e o que não teve fonte marcado `[NÃO VERIFICADO]`. Exemplos do que mudou o desenho:
   - quota de PutEvents do EventBridge em sa-east-1 de 600/s (pedi aumento: 6.000 aprovados);
   - App Mesh descontinuado em 30/09/2026;
   - o Schema Registry do EventBridge não valida eventos;
   - RFC 7807 → 9457.
2. **Plano revisado por um painel de "juízes"** (modelos instruídos a reprovar). **O plano v1 foi reprovado.** O ponto central: o plano punha o item de limite numa transação do DynamoDB, que cancela sob contenção. Isso virou o ADR-0005.
3. **Código com testes como especificação.** Cada regra tem teste; invariantes com **property-based** (Hypothesis); as portas de persistência dos três BCs com **suíte de contrato rodando nos dois adapters** (memória e DynamoDB/LocalStack), exceto `RegistroDeCompensacao.regravar`, que só tem teste em memória; integração com threads reais.
4. **Revisão de tudo que foi delegado:** o orquestrador rodou os gates de novo e leu o código; nenhum "tudo verde" relatado foi aceito sem execução.

## Erros que a IA cometeu — e como foram pegos

| Erro | Pego por | Correção |
|---|---|---|
| Item de limite em `TransactWriteItems` (cancela sob contenção, não devolve saldo, LocalStack não reproduz) | Juízes + crítica adversarial | Escrita condicional de item único (ADR-0005) |
| `multipleOf: 0.01` no contrato rejeitaria R$ 19,99 (ponto flutuante) | Teste empírico com jsonschema antes de publicar | Escala validada na aplicação |
| Reserva repetida depois da compactação da guarda criaria hold duplicado | Revisão do próprio raciocínio + teste | Guarda "hold não existe" |
| Placeholder declarado e não usado no DynamoDB | **Teste de integração** (a LocalStack recusou como a AWS faria) | Placeholder sob demanda |
| `ClientRequestToken` colidindo entre pedidos diferentes | Teste de integração (`IdempotentParameterMismatch`) | Token = hash do conteúdo da transação |
| Id de estorno fora do formato UUID exigido pelo contrato interno | Executor ao implementar o cliente HTTP | UUIDv5 gerado no domínio |
| Circuit breaker preso para sempre se a prova recebesse 4xx | **Revisão do orquestrador** sobre o código delegado | Sucesso registrado antes de interpretar + teste de regressão |
| Caminho errado para os schemas no teste | Teste falhando | Corrigido |
| A IA afirmou que a credencial root estava no `.bashrc` (estava nas variáveis de ambiente da sessão) | Juiz de alucinação | Texto corrigido; testes passaram a usar credencial explícita e endpoint obrigatoriamente local |
| Compactação da guarda de idempotência documentada e nunca chamada no adapter DynamoDB | Medição na AWS (`CTR-CARGA-0001` com 246 entradas e 15 KB), confirmada pelo juiz | Janela de 5 min e remoção de carona na escrita seguinte do contrato |
| Reserva sem captura (reservar 1000 e debitar 1000 dava `LIMITE_INSUFICIENTE`) e expiração de 7 dias sem motor (`Autorizacao.expirar` só rodava em teste) | Juiz adversarial | ADR-0020: captura como DEBITO que referencia a RESERVA e expirador no loop do reconciliador |
| `LIBERAR_RESERVA` de hold inexistente respondia sucesso (`JA_APLICADA`), o que esconderia o perdedor na disputa captura × estorno | Ao desenhar a captura | `RESERVA_NAO_ENCONTRADA`: o hold é o árbitro (ADR-0020) |
| Teto por contrato de "300–1.000 op/s" no ADR-0016 ignorava o custo da guarda no item | Revisão da conta do ADR | Teto correto de ~8 op/s sustentadas, com alarme e Contributor Insights |
| Gateway Response com `Content-Type: application/json` em vez de `application/problem+json` | Deploy na AWS | Terraform corrigido |
| Dashboard do CloudWatch inválido por causa do `flatten` | Deploy na AWS | Terraform corrigido |
| Workers do ECS sem permissão de escrita em `/tmp` (heartbeat do healthcheck) | Deploy na AWS | O volume efêmero montado em `/tmp` pertence ao root e o container roda como uid 10001: root fs gravável **só nos workers** (`limites-acl`, `reconciliador`), APIs seguem somente leitura |
| Compensação "garantir + desfazer" sem progresso gravado: com a reversão falhando até o prazo, a órfã ia para a fila com o efeito que o próprio reconciliador criou ("efeito líquido zero em qualquer cenário" era falso) | Painel de juízes 3 (PoC) | Máquina de estados gravada: `COMPENSANDO` → garantida → reversão → `COMPENSADA`, com motivo e progresso na fila humana |
| Compensação mais lenta que o lease de 10 s deixava o cliente retomar e aprovar enquanto o reconciliador desfazia (APROVADA com débito zero) | Painel de juízes 3 | `COMPENSANDO` gravado antes de qualquer chamada ao limite: o cliente não retoma mais a chave |
| `compilar` podia emitir `SET` e `REMOVE` do mesmo caminho (`ValidationException` → 500) em dois retries do mesmo id depois da janela | Painel de juízes 3 (PoC na LocalStack) | O id da operação nunca entra no `REMOVE` + teste |
| ADR-0006 dizia que nada dependia de relógio entre máquinas; a compactação da guarda depende (folga real de 60 s) | Painel de juízes 3 (débito dobrado com 2 min de desvio) | Risco reescrito com a fonte (`ClockErrorBound` do Fargate) e teste acoplando as constantes dos dois BCs |
| Marca de estorno assumida quando o dono não existe mais (fail-open) | Painel de juízes 3 | Dono desconhecido bloqueia (fail-closed) |
| Órfã encaminhada saía de todos os índices: nenhuma fila listava o que precisava de gente; reserva sem hold sangrava uma chamada ao limite a cada 30 s | Painel de juízes 3 | Fila humana `RM#` no mesmo GSI; reserva vencida que segue sem hold 10 min depois do vencimento é encaminhada (`TOLERANCIA_DA_DISPUTA`) |
| 409 `requisicao-em-andamento` com `Retry-After: 60` para um estado que só uma pessoa resolve | Painel de juízes 3 | 409 `conflito-de-estado` sem `Retry-After` |
| Os testes usavam um limite falso que nunca esquecia: o motivo de existir de prazo, marca e fila nunca era exercitado | Painel de juízes 3 | `tests/integracao_bcs`: os dois BCs em processo com a guarda real, mostrando o débito dobrado sem as proteções |
| 500 esporádico do API Gateway (`Connection is closed`): keep-alive de 5 s do uvicorn fechava conexão que o gateway reaproveitava | Carga na AWS (log de execução do gateway) | `--timeout-keep-alive 400` |
| `MAX_EM_VOO=256` transformava sobrecarga em fila de segundos (p50 das aceitas de 2,7 s) em vez de 503 rápido | Carga na AWS | Dimensionado como capacidade × p99 × 2: ≈ 37 por vCPU, 18 nas tasks de 0,5 vCPU entregues |
| Serviço `autorizacao` saturava CPU a ~60 rps por vCPU (~16 ms por requisição) | Carga na AWS (Container Insights) | Sem access log por requisição e um `GetItem` a menos no caminho comum: ~8 ms por requisição |
| Estimativa de ~6–8 WRU por autorização ignorava outbox e contabilização (medido no DEBITO: 14,6 de tabela; captura ~27, derivada) | Carga na AWS (métricas por tabela) | `docs/capacidade.md` corrigido; outbox embutido no item como alavanca nº 1 |
| Mesmo gravada passo a passo, a compensação aplicava a operação ("garantir") para descobrir se ela existia: débito órfão que nunca chegou ganhava débito fantasma, e o cliente pagava por compra não autorizada se a reversão fosse recusada | Painel de juízes 4 (PoC) | Consulta só de leitura no `limites` (`GET .../operacoes/{id}`): o reconciliador pergunta antes de desfazer |
| "Recusa da garantia = nunca aplicou" dependia da ordem de diagnóstico de outro BC; uma refatoração plausível abandonaria o hold | Painel de juízes 4 (mutação sobrevivia com 414 testes verdes) | A decisão vem da observação do hold; teste entre BCs com hold vivo, sem saldo e guarda esquecida |
| Premissa "nenhuma escrita atrasada" só valia por suposição: o `limites` não tinha teto de fila e podia aplicar minutos depois | Revisão do argumento do reconciliador | Prazo propagado (`X-Prazo-Ms`): o `limites` não escreve depois que o chamador desistiu |
| Lease não era renovado entre os passos da compensação (um só `agora`) | Painel de juízes 4 | Relógio relido a cada passo + teste de perda de lease no meio |
| Fila humana sem evento e com TTL de 30 dias da criação; reserva sem hold continuava `APROVADA` | Painel de juízes 4 | Eventos `TransacaoCompensada`/`TransacaoEmReconciliacaoManual`, item na fila sem TTL, reserva vai para `RECONCILIACAO_MANUAL` |
| GET de autorização, estorno e captura aceitavam autorização de OUTRO canal (BOLA) | Agente do banco de perguntas, conferindo o código | `client_id` do token tem de bater; senão 404/inexistente |
| Motivo de recusa novo vindo do `limites` derrubava a resposta com 500 (`KeyError`) | Painel de juízes 3 (ressalva) + banco de perguntas | Recusa genérica `RECUSA_DO_LIMITE` (422) |
| ACL truncando em silêncio ids de 21 a 64 posições ao formatar o registro legado | Juiz adversarial | `formatar` falha (`RegistroLegadoInvalido`), `traduzir` rejeita id fora do pattern e os patterns da API passaram a `{1,20}` e `{1,16}` |

## O que eu preciso dominar na defesa (e onde)

- **Concorrência do limite:** `limites/domain/{operacoes,limite}.py` + `adapters/outbound/dynamodb.py::compilar` + ADR-0005.
- **Idempotência, lease e retomada:** `autorizacao/domain/idempotencia.py`, `application/autorizar.py` + ADR-0006.
- **Reconciliador:** `autorizacao/application/reconciliar.py`.
- **Eventos:** `autorizacao/adapters/outbound/{dynamodb,cloudevents}.py`, `relay/` + ADRs 0007–0009.
- Cada serviço em `services/` tem um `COMO-LER.md` com o caminho de uma requisição pelo código (na `contabilizacao`, o de um evento).
