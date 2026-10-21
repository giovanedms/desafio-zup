# Catálogo de problem types (RFC 9457)

Base dos `type`: `https://financas.example/problemas/<slug>` (domínio reservado `.example`, RFC 2606; em produção, URL do portal de APIs).
O registro IANA de problem types não tem tipos para este domínio, por isso os tipos são próprios.

Todo problema traz `type`, `title`, `status`, `codigo` e `correlation_id`. Também traz `trace_id` quando houver trace, e `erros[]` em validação (no máximo 20 itens; `erros_omitidos` diz quantos ficaram de fora).
Numa recusa registrada (402/404/409/422 de uma autorização), `correlation_id` e `trace_id` do corpo são os da execução que a criou: o replay sai idêntico, com o header `Idempotent-Replayed: true`; o header `X-Correlation-Id` é sempre o desta requisição.
A exceção é o 429 das regras rate-based do WAF, em **Desvios declarados**, no fim.
`codigo` é o identificador estável para máquina, o "código" pedido no enunciado. `title` é para gente e pode mudar.

| codigo | slug (`type`) | HTTP | Quem emite | Quando | Repetir? |
|---|---|---|---|---|---|
| REQUISICAO_MAL_FORMADA | requisicao-mal-formada | 400 | serviço / gateway (`BAD_REQUEST_PARAMETERS`) | Corpo que não é JSON (serviço), inclusive `NaN`/`Infinity` (não são JSON pela RFC 8259 §6) e objeto com o mesmo nome duas vezes (proibido pela RFC 7493 §2.3; o serviço não escolhe qual valor vale). Na AWS, também header obrigatório ausente ou vazio, como `Idempotency-Key` (gateway: ele só confere a presença dos parâmetros e não valida o corpo). Tipo errado e campo desconhecido no corpo são 422 | Não (corrigir) |
| IDEMPOTENCY_KEY_INVALIDA | idempotency-key-invalida | 400 | serviço | `Idempotency-Key` fora do formato UUID, ou ausente quando o pedido não passa pelo gateway (local) | Não (corrigir) |
| NAO_AUTENTICADO | nao-autenticado | 401 | gateway (`UNAUTHORIZED`) / serviço | Token ausente, expirado ou inválido (gateway). No acesso direto ao serviço, `X-Client-Id` ausente ou fora de `^[A-Za-z0-9._-]{1,64}$`. Sempre com `WWW-Authenticate: Bearer` (RFC 9110 §15.5.2) | Após renovar o token |
| SEM_PERMISSAO | sem-permissao | 403 | gateway (`ACCESS_DENIED`, `INVALID_API_KEY`, `MISSING_AUTHENTICATION_TOKEN`, `WAF_FILTERED`) | Escopo insuficiente, API key inválida, rota inexistente, ou bloqueio por regra gerenciada do WAF (`AWSManagedRulesCommonRuleSet`, que sai pela gateway response `WAF_FILTERED`) | Não |
| LIMITE_INSUFICIENTE | limite-insuficiente | 402 | serviço | Disponível < valor (recusa registrada e publicada). Extensões `id_autorizacao` e `limite_disponivel` (o disponível no momento da recusa) | Mesma chave → mesma recusa |
| CONTRATO_NAO_ENCONTRADO | contrato-nao-encontrado | 404 | serviço | Contrato ausente na réplica do core (fail-closed) | Não |
| AUTORIZACAO_NAO_ENCONTRADA | autorizacao-nao-encontrada | 404 | serviço | GET de autorização inexistente | Não |
| RECURSO_NAO_ENCONTRADO | recurso-nao-encontrado | 404 | serviço | Rota inexistente no acesso direto ao serviço, inclusive a de barra final (`.../autorizacoes/`: não há redirect 307). Na AWS, o gateway responde antes com 403 `SEM_PERMISSAO` | Não |
| METODO_NAO_PERMITIDO | metodo-nao-permitido | 405 | serviço | Método que o recurso não suporta, com header `Allow` (acesso direto ao serviço; na AWS, o gateway responde antes com 403 `SEM_PERMISSAO`) | Não (corrigir) |
| REQUISICAO_EM_ANDAMENTO | requisicao-em-andamento | 409 | serviço | Mesma `Idempotency-Key` ainda em processamento | Sim, após `Retry-After` |
| CONFLITO_DE_ESTADO | conflito-de-estado | 409 | serviço | Estorno ou captura de reserva já capturada, estornada ou expirada (original compensada nunca foi aprovada: é 422 `AUTORIZACAO_ORIGINAL_INVALIDA`); captura/estorno que perdeu o hold para outra operação (`RESERVA_NAO_ENCONTRADA`); outro estorno da mesma original em andamento (`ESTORNO_EM_ANDAMENTO`); chave compensada ou em reconciliação manual (consultar com GET) | Não |
| TIPO_DE_MIDIA_NAO_SUPORTADO | tipo-de-midia-nao-suportado | 415 | serviço / gateway | `Content-Type` ≠ `application/json` | Não (corrigir) |
| VALIDACAO_DE_DOMINIO | validacao-de-dominio | 422 | serviço | Corpo fora do schema (campo obrigatório ausente, tipo errado, `null` em campo opcional, campo desconhecido, `metadata` fora da allowlist, corpo que não é objeto), valor ≤ 0 ou com > 2 casas, moeda ≠ BRL, `id_contrato` do corpo ≠ path, ESTORNO sem `id_autorizacao_original`, `id_autorizacao_original` em RESERVA ou CREDITO, parâmetro de path fora do pattern (inclusive `id_autorizacao` que não é UUID no GET). O serviço é a autoridade: o gateway não valida o corpo | Não (corrigir) |
| IDEMPOTENCY_KEY_REUTILIZADA | idempotency-key-reutilizada | 422 | serviço | Mesma chave com corpo diferente (fingerprint JCS diverge) | Não |
| CONTRATO_INATIVO | contrato-inativo | 422 | serviço | Contrato BLOQUEADO ou ENCERRADO | Não |
| CREDITO_EXCEDE_UTILIZADO | credito-excede-utilizado | 422 | serviço | O valor a devolver (CREDITO ou ESTORNO de débito) é maior que o utilizado: o modelo não tem saldo credor (ex.: estorno de um débito já pago por um CREDITO) | Não |
| CONTA_DIVERGENTE | conta-divergente | 422 | serviço | `id_conta` informado não é a conta titular do contrato na réplica do core (recusa registrada e publicada) | Não |
| AUTORIZACAO_ORIGINAL_INVALIDA | autorizacao-original-invalida | 422 | serviço | ESTORNO ou captura cuja original: (1) não existe, é de outro contrato ou de outro canal; (2) é de tipo não estornável (só RESERVA e DEBITO) ou não capturável (só RESERVA); (3) não está APROVADA; (4) tem valor diferente do estorno (estorno parcial não é suportado na v1); (5) vale menos que a captura | Não |
| LIMITE_DE_REQUISICOES | limite-de-requisicoes | 429 | gateway (`THROTTLED`, `QUOTA_EXCEEDED`) e WAF (regra rate-based) | Rate limit ou quota diária do usage plan do consumidor (gateway); taxa acima da regra rate-based (WAF). O serviço não emite 429: a proteção dele é o 503 de load shedding | Sim, após `Retry-After` |
| ERRO_INTERNO | erro-interno | 500 | serviço / gateway (`DEFAULT_5XX`) | Falha inesperada | Sim, com a mesma chave |
| SERVICO_INDISPONIVEL | servico-indisponivel | 503 | serviço | Circuit breaker aberto, load shedding, dependência fora, ou DynamoDB estrangulado, sem resposta ou em conflito de transação (no limite: contrato quente; na autorização: a própria tabela) | Sim, após `Retry-After`, com a mesma chave |
| TEMPO_ESGOTADO | tempo-esgotado | 504 | gateway (`INTEGRATION_TIMEOUT`) | Integração passou do timeout; o resultado pode ter sido aplicado | Sim (mesma chave) ou GET |

**Precedência quando mais de uma regra se aplica** (ADR-0006):

0. 503 de load shedding: avaliado antes de tudo, inclusive da autenticação e da sintaxe (o serviço sobrecarregado não lê o pedido; corrija o pedido depois do `Retry-After` se ele voltar 400/415/422).
1. 401 do serviço (`X-Client-Id`), depois 400/415, sintaxe. Pedido inválido nunca é registrado como idempotente; é o mesmo comportamento documentado pela Stripe.
2. Idempotência: replay, 409 em andamento ou 422 de chave reutilizada.
3. 422, domínio.
4. 404.
5. 402, recusa.
6. 503 de dependência (limite ou banco de autorizações fora).

O 400 de parâmetro ausente, o 401, o 403 e o 429 da borda acontecem antes de qualquer uma dessas.

**Erros do gateway em problem+json:** Gateway Responses do API Gateway REST (`UNAUTHORIZED`, `ACCESS_DENIED`, `INVALID_API_KEY`, `MISSING_AUTHENTICATION_TOKEN`, `WAF_FILTERED`, `THROTTLED`, `QUOTA_EXCEEDED`, `BAD_REQUEST_PARAMETERS`, `INTEGRATION_TIMEOUT`, `DEFAULT_4XX`, `DEFAULT_5XX`) configuradas com `Content-Type: application/problem+json` e template com `type`, `title`, `status`, `codigo` e `correlation_id` (= `$context.requestId`), via Terraform (ADR-0010). `BAD_REQUEST_BODY` continua configurada, mas não dispara: o gateway não valida o corpo.

**Desvios declarados (WAF, avaliado antes do gateway):**

- **Por que `WAF_FILTERED` é definida:** sem ela, o bloqueio do WAF herdaria a `DEFAULT_4XX`, fixada em 400 `REQUISICAO_MAL_FORMADA`, e o cliente veria "requisição mal formada" para um bloqueio de proteção.
- **429 das regras rate-based** (por consumidor, `x-api-key`, e por IP): resposta customizada do WAF, que tem precedência sobre as gateway responses, com `Retry-After: 60` e o corpo do catálogo (`codigo` `LIMITE_DE_REQUISICOES`). Dois desvios: o `Content-Type` sai **`application/json`**, porque o WAF não permite sobrescrever esse header e só aceita JSON, HTML ou texto; e o corpo é **fixo**, sem `correlation_id`, porque o WAF não tem o id da requisição.
