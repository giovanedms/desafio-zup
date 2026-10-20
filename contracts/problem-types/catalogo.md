# Catálogo de problem types (RFC 9457)

Base dos `type`: `https://financas.example/problemas/<slug>` (domínio reservado `.example`, RFC 2606; em produção, URL do portal de APIs).
O registro IANA de problem types não tem tipos para este domínio, por isso os tipos são próprios.

Todo problema traz `type`, `title`, `status`, `codigo` e `correlation_id`. Também traz `trace_id` quando houver trace, e `erros[]` em validação.
`codigo` é o identificador estável para máquina, o "código" pedido no enunciado. `title` é para gente e pode mudar.

| codigo | slug (`type`) | HTTP | Quem emite | Quando | Repetir? |
|---|---|---|---|---|---|
| REQUISICAO_MAL_FORMADA | requisicao-mal-formada | 400 | serviço / gateway (`BAD_REQUEST_BODY`) | JSON inválido, tipo errado, campo desconhecido | Não (corrigir) |
| IDEMPOTENCY_KEY_INVALIDA | idempotency-key-invalida | 400 | serviço | `Idempotency-Key` ausente ou fora do formato UUID | Não (corrigir) |
| NAO_AUTENTICADO | nao-autenticado | 401 | gateway (`UNAUTHORIZED`) | Token ausente, expirado ou inválido | Após renovar o token |
| SEM_PERMISSAO | sem-permissao | 403 | gateway (`ACCESS_DENIED`) | Escopo insuficiente ou bloqueio do WAF | Não |
| LIMITE_INSUFICIENTE | limite-insuficiente | 402 | serviço | Disponível < valor (recusa registrada e publicada) | Mesma chave → mesma recusa |
| CONTRATO_NAO_ENCONTRADO | contrato-nao-encontrado | 404 | serviço | Contrato ausente na réplica do core (fail-closed) | Não |
| AUTORIZACAO_NAO_ENCONTRADA | autorizacao-nao-encontrada | 404 | serviço | GET de autorização inexistente | Não |
| REQUISICAO_EM_ANDAMENTO | requisicao-em-andamento | 409 | serviço | Mesma `Idempotency-Key` ainda em processamento | Sim, após `Retry-After` |
| CONFLITO_DE_ESTADO | conflito-de-estado | 409 | serviço | Estorno ou captura de reserva já capturada, estornada, expirada ou compensada; captura/estorno que perdeu o hold para outra operação (`RESERVA_NAO_ENCONTRADA`); outro estorno da mesma original em andamento (`ESTORNO_EM_ANDAMENTO`); chave compensada ou em reconciliação manual (consultar com GET) | Não |
| TIPO_DE_MIDIA_NAO_SUPORTADO | tipo-de-midia-nao-suportado | 415 | serviço / gateway | `Content-Type` ≠ `application/json` | Não (corrigir) |
| VALIDACAO_DE_DOMINIO | validacao-de-dominio | 422 | serviço | Valor ≤ 0 ou com > 2 casas, moeda ≠ BRL, `id_contrato` do corpo ≠ path, `metadata` fora da allowlist, ESTORNO sem `id_autorizacao_original` | Não (corrigir) |
| IDEMPOTENCY_KEY_REUTILIZADA | idempotency-key-reutilizada | 422 | serviço | Mesma chave com corpo diferente (fingerprint JCS diverge) | Não |
| CONTRATO_INATIVO | contrato-inativo | 422 | serviço | Contrato BLOQUEADO ou ENCERRADO | Não |
| CREDITO_EXCEDE_UTILIZADO | credito-excede-utilizado | 422 | serviço | CREDITO maior que o utilizado | Não |
| AUTORIZACAO_ORIGINAL_INVALIDA | autorizacao-original-invalida | 422 | serviço | ESTORNO de autorização inexistente, de outro contrato ou não aprovada | Não |
| LIMITE_DE_REQUISICOES | limite-de-requisicoes | 429 | gateway (`THROTTLED`, `QUOTA_EXCEEDED`), WAF, serviço | Rate limit, quota do plano ou proteção | Sim, após `Retry-After` |
| ERRO_INTERNO | erro-interno | 500 | serviço / gateway (`DEFAULT_5XX`) | Falha inesperada | Sim, com a mesma chave |
| SERVICO_INDISPONIVEL | servico-indisponivel | 503 | serviço | Circuit breaker aberto, load shedding, dependência fora ou DynamoDB estrangulado (contrato quente) | Sim, após `Retry-After` |
| TEMPO_ESGOTADO | tempo-esgotado | 504 | gateway (`INTEGRATION_TIMEOUT`) | Integração passou do timeout; o resultado pode ter sido aplicado | Sim (mesma chave) ou GET |

**Precedência quando mais de uma regra se aplica** (ADR-0006):

1. 400/415, sintaxe. Pedido inválido nunca é registrado como idempotente; é o mesmo comportamento documentado pela Stripe.
2. Idempotência: replay, 409 em andamento ou 422 de chave reutilizada.
3. 422, domínio.
4. 404.
5. 402, recusa.
6. 503.

Os 401, 403 e 429 do gateway acontecem antes de qualquer uma dessas.

**Erros do gateway em problem+json:** Gateway Responses do API Gateway REST (`UNAUTHORIZED`, `ACCESS_DENIED`, `THROTTLED`, `QUOTA_EXCEEDED`, `BAD_REQUEST_BODY`, `INTEGRATION_TIMEOUT`, `DEFAULT_5XX`) configuradas com `Content-Type: application/problem+json` e template com `type`, `title`, `status`, `codigo` e `correlation_id` (= `$context.requestId`), via Terraform (ADR-0010).
