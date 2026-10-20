# ADR-0010 — API Gateway REST, OAuth2 (Cognito), usage plans + WAF, VPC Link → NLB

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** APIM-01, APIM-02, API-09

## Contexto

O enunciado pede API Gateway (REST ou HTTP) com autorização (Cognito, Lambda Authorizer ou OAuth2) e rate limiting, throttling, usage plans e quotas por consumidor.

Fatos (`01-pesquisa/02`):

- **Usage plans, API keys, quotas, request validation, WAF e canary só existem na REST API.** A HTTP API não tem.
- **Usage plan é *best-effort*:** a própria doc diz para não usá-lo como controle de acesso ou de custo.
- **Throttle de conta:** 10.000 RPS / burst 5.000 por região, somando todas as APIs.
- **Integração privada da REST API:** VPC Link (v1) → NLB. Desde nov/2025 existe VPC Link v2 → ALB, inclusive em São Paulo.

## Decisão

- **REST API regional**, criada **a partir do contrato OpenAPI** (`contracts/openapi/autorizacao-v1.yaml`).
  - As extensões `x-amazon-apigateway-*` (integração, authorizer, validador) são injetadas pelo Terraform; o arquivo do contrato não é editado.
- **Autenticação:** OAuth2 client credentials no **Amazon Cognito**.
  - Resource server `autorizacoes`, escopos `autorizacoes/write` e `autorizacoes/read`.
  - Um app client por canal.
  - Authorizer `COGNITO_USER_POOLS` exigindo o escopo.
  - O `client_id` do token é injetado no header `X-Client-Id` da integração e compõe o escopo de idempotência (ADR-0006).
  - O serviço só é alcançável pelo VPC Link, então não aceita o header vindo de fora.
- **Cotas por consumidor:** usage plan + API key por canal, com dois planos para a demo.

  | Plano | Taxa | Burst | Quota |
  |---|---|---|---|
  | `plano-padrao` | 200 rps | 400 | 1 M/dia |
  | `plano-restrito` | 5 rps | 10 | 1.000/dia, para demonstrar o **429** |

- **Proteção dura:**
  - **WAF** rate-based por IP + regras gerenciadas;
  - **load shedding no serviço** (concorrência máxima em voo → 503 + `Retry-After`);
  - o usage plan não é a única barreira.
- **Erros do gateway em RFC 9457:** Gateway Responses `UNAUTHORIZED`, `ACCESS_DENIED`, `THROTTLED`, `QUOTA_EXCEEDED`, `BAD_REQUEST_BODY`, `BAD_REQUEST_PARAMETERS`, `INTEGRATION_TIMEOUT`, `DEFAULT_4XX` e `DEFAULT_5XX` com `Content-Type: application/problem+json` e corpo com `type`, `title`, `status`, `codigo` e `correlation_id` (= `$context.requestId`), mais `Retry-After` nos 429.
- **Integração privada: VPC Link (v1) → NLB interno → tasks.** O VPC Link v2 → ALB existe, mas preferimos o caminho com maior maturidade no provider Terraform. A troca é local ao módulo `api`.
- **Observabilidade:** access log JSON (requestId, ip, status, latency, integrationLatency) e X-Ray ligado no stage.

## Consequências

- **Positivas:**
  - Autenticação, autorização por escopo, cotas, validação e WAF saem do código do serviço.
  - O contrato publicado é o mesmo que o gateway aplica.
  - Os erros seguem o padrão em todas as camadas.
- **Negativas:**
  - **O importador da REST API é limitado** (JSON Schema draft-4, sem `nullable`/`format`): a validação no gateway é **parcial**, e a validação completa fica no serviço (`fail_on_warnings = false`, documentado).
  - **Cognito M2M é cobrado por token emitido:** os canais devem fazer cache do token até expirar.
  - **Throttle de conta compartilhado:** conta dedicada por domínio.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| O banco tem IdP corporativo (não Cognito) | Lambda Authorizer validando o JWT do IdP (JWKS em cache), com o mesmo contrato |
| Exigência de **mTLS** entre canal e banco | Domínio customizado com truststore no API Gateway (suportado na REST API) |
| Latência do gateway pesar no p99 | Medir `IntegrationLatency` contra `Latency` no access log. A HTTP API é mais barata e rápida, mas perde usage plans e WAF; só troca se a governança de cotas for para outra camada |

## Referências

- REST vs HTTP API: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html
- Usage plans (best-effort): https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-api-usage-plans.html
- Gateway responses: https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-gatewayResponse-definition.html
- Cognito resource servers e escopos: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html
