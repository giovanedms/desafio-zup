# Catálogo de problem types (RFC 9457)

Base dos `type`: `https://financas.example/problemas/<slug>` (domínio reservado `.example`, RFC 2606; em produção, URL do portal de APIs).
O registro IANA de problem types não tem tipos para este domínio, por isso os tipos são próprios.

Todo problema traz `type`, `title`, `status`, `codigo` e `correlation_id`. Também traz `trace_id` quando houver trace, e `erros[]` em validação.
As exceções vêm do WAF e estão em **Desvios declarados**, no fim.
`codigo` é o identificador estável para máquina, o "código" pedido no enunciado. `title` é para gente e pode mudar.

| codigo | slug (`type`) | HTTP | Quem emite | Quando | Repetir? |
|---|---|---|---|---|---|
| REQUISICAO_MAL_FORMADA | requisicao-mal-formada | 400 | serviço / gateway (`BAD_REQUEST_PARAMETERS`) | Corpo que não é JSON (serviço). Na AWS, também header obrigatório ausente ou vazio, como `Idempotency-Key` (gateway: ele só confere a presença dos parâmetros e não valida o corpo). Tipo errado e campo desconhecido no corpo são 422 | Não (corrigir) |
| IDEMPOTENCY_KEY_INVALIDA | idempotency-key-invalida | 400 | serviço | `Idempotency-Key` fora do formato UUID, ou ausente quando o pedido não passa pelo gateway (local) | Não (corrigir) |
| NAO_AUTENTICADO | nao-autenticado | 401 | gateway (`UNAUTHORIZED`) | Token ausente, expirado ou inválido | Após renovar o token |
| SEM_PERMISSAO | sem-permissao | 403 | gateway (`ACCESS_DENIED`) | Escopo insuficiente (o 403 do WAF não é este: ver desvios) | Não |
| LIMITE_INSUFICIENTE | limite-insuficiente | 402 | serviço | Disponível < valor (recusa registrada e publicada). Extensões `id_autorizacao` e `limite_disponivel` (o disponível no momento da recusa) | Mesma chave → mesma recusa |
| CONTRATO_NAO_ENCONTRADO | contrato-nao-encontrado | 404 | serviço | Contrato ausente na réplica do core (fail-closed) | Não |
| AUTORIZACAO_NAO_ENCONTRADA | autorizacao-nao-encontrada | 404 | serviço | GET de autorização inexistente | Não |
| REQUISICAO_EM_ANDAMENTO | requisicao-em-andamento | 409 | serviço | Mesma `Idempotency-Key` ainda em processamento | Sim, após `Retry-After` |
| CONFLITO_DE_ESTADO | conflito-de-estado | 409 | serviço | Estorno ou captura de reserva já capturada, estornada, expirada ou compensada; captura/estorno que perdeu o hold para outra operação (`RESERVA_NAO_ENCONTRADA`); outro estorno da mesma original em andamento (`ESTORNO_EM_ANDAMENTO`); chave compensada ou em reconciliação manual (consultar com GET) | Não |
| TIPO_DE_MIDIA_NAO_SUPORTADO | tipo-de-midia-nao-suportado | 415 | serviço / gateway | `Content-Type` ≠ `application/json` | Não (corrigir) |
| VALIDACAO_DE_DOMINIO | validacao-de-dominio | 422 | serviço | Corpo fora do schema (campo obrigatório ausente, tipo errado, campo desconhecido, `metadata` fora da allowlist, corpo que não é objeto), valor ≤ 0 ou com > 2 casas, moeda ≠ BRL, `id_contrato` do corpo ≠ path, ESTORNO sem `id_autorizacao_original`, `id_autorizacao_original` em RESERVA ou CREDITO, parâmetro de path fora do pattern. O serviço é a autoridade: o gateway não valida o corpo | Não (corrigir) |
| IDEMPOTENCY_KEY_REUTILIZADA | idempotency-key-reutilizada | 422 | serviço | Mesma chave com corpo diferente (fingerprint JCS diverge) | Não |
| CONTRATO_INATIVO | contrato-inativo | 422 | serviço | Contrato BLOQUEADO ou ENCERRADO | Não |
| CREDITO_EXCEDE_UTILIZADO | credito-excede-utilizado | 422 | serviço | CREDITO maior que o utilizado | Não |
| AUTORIZACAO_ORIGINAL_INVALIDA | autorizacao-original-invalida | 422 | serviço | ESTORNO ou captura cuja original: (1) não existe, é de outro contrato ou de outro canal; (2) é de tipo não estornável (só RESERVA e DEBITO) ou não capturável (só RESERVA); (3) não está APROVADA; (4) tem valor diferente do estorno (estorno parcial não é suportado na v1); (5) vale menos que a captura | Não |
| LIMITE_DE_REQUISICOES | limite-de-requisicoes | 429 | gateway (`THROTTLED`, `QUOTA_EXCEEDED`) e WAF (regra rate-based) | Rate limit ou quota diária do usage plan do consumidor (gateway); taxa acima da regra rate-based (WAF). O serviço não emite 429: a proteção dele é o 503 de load shedding | Sim, após `Retry-After` |
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

O 400 de parâmetro ausente, o 401, o 403 e o 429 da borda acontecem antes de qualquer uma dessas.

**Erros do gateway em problem+json:** Gateway Responses do API Gateway REST (`UNAUTHORIZED`, `ACCESS_DENIED`, `THROTTLED`, `QUOTA_EXCEEDED`, `BAD_REQUEST_PARAMETERS`, `INTEGRATION_TIMEOUT`, `DEFAULT_4XX`, `DEFAULT_5XX`) configuradas com `Content-Type: application/problem+json` e template com `type`, `title`, `status`, `codigo` e `correlation_id` (= `$context.requestId`), via Terraform (ADR-0010). `BAD_REQUEST_BODY` continua configurada, mas não dispara: o gateway não valida o corpo.

**Desvios declarados (WAF, avaliado antes do gateway):**

- **Bloqueio por regra gerenciada** (`AWSManagedRulesCommonRuleSet`): **403 com o corpo padrão do WAF**, fora do problem+json e sem `codigo`. Não é o `SEM_PERMISSAO` da tabela; o cliente trata como 403 genérico e não repete.
- **429 da regra rate-based**: resposta customizada em problem+json com `Retry-After`, mas de **corpo fixo**: sem `correlation_id`, porque o WAF não tem o id da requisição.
