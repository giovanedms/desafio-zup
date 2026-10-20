# ADR-0010 — API Gateway REST, OAuth2 (Cognito), usage plans + WAF, VPC Link → NLB

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** APIM-01, APIM-02, API-09

## Contexto

O enunciado pede API Gateway (REST ou HTTP) com autorização (Cognito, Lambda Authorizer ou OAuth2) e rate limiting, throttling, usage plans e quotas por consumidor.

Fatos (da documentação oficial da AWS; os links principais estão em Referências):

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
  - **WAF por consumidor:** regra rate-based agregada pelo header `x-api-key` (custom key), teto de **120.000 requisições em 5 min (400 rps)**, o dobro do maior plano de canal (`plano-padrao`, 200 rps). O usage plan governa o plano (best-effort, nas palavras da AWS); o WAF é a barreira dura acima dele e corta antes do gateway quem passar do dobro. Requisição sem `x-api-key` não conta nessa regra;
  - **WAF por IP, alto:** **300.000 em 5 min (1.000 rps)**, 10% do throttle de 10.000 rps da conta. Pega o tráfego sem chave e quem gira chaves falsas a partir de um IP, sem barrar um egress (NAT) que carregue vários canais. Antes era 2.000 por IP (6,7 rps), 30× abaixo do plano que deveria complementar;
  - **as duas regras bloqueiam com 429** e o corpo do catálogo (`codigo` `LIMITE_DE_REQUISICOES`, `type` `…/limite-de-requisicoes`) e `Retry-After: 60` (o WAF leva até ~30 s para notar a queda e soltar o bloqueio). O custom response do WAF tem precedência sobre as gateway responses. Duas limitações do WAF, não escolhas: o `Content-Type` sai `application/json` (o WAF não aceita `application/problem+json` nem deixa sobrescrever o header) e não há `correlation_id` (o corpo é estático);
  - **regras gerenciadas** (`AWSManagedRulesCommonRuleSet`) bloqueiam sem custom response: saem pela gateway response `WAF_FILTERED`, que o módulo define como **403 `SEM_PERMISSAO` em problem+json**;
  - **load shedding no serviço** (concorrência máxima em voo → 503 + `Retry-After`);
  - o usage plan não é a única barreira. A janela de medição com o `canal-carga` (6.000 rps) precisa subir os dois tetos do WAF.
- **Erros do gateway em RFC 9457:** Gateway Responses `UNAUTHORIZED`, `ACCESS_DENIED`, `WAF_FILTERED`, `INVALID_API_KEY`, `MISSING_AUTHENTICATION_TOKEN`, `THROTTLED`, `QUOTA_EXCEEDED`, `BAD_REQUEST_BODY`, `BAD_REQUEST_PARAMETERS`, `INTEGRATION_TIMEOUT`, `DEFAULT_4XX` e `DEFAULT_5XX` com `Content-Type: application/problem+json` e corpo com `type`, `title`, `status`, `codigo` e `correlation_id` (= `$context.requestId`), mais `Retry-After` nos 429. O `DEFAULT_4XX` fixa 400, e a AWS documenta que isso muda o status de **todo** 4XX sem resposta própria: por isso os 403 que chegam ao cliente (`WAF_FILTERED`, `INVALID_API_KEY`, `MISSING_AUTHENTICATION_TOKEN`) têm entrada explícita, senão sairiam 400 "requisição mal formada".
- **Validação no gateway: só parâmetros.** O validador barra a falta do header `Idempotency-Key`; o **corpo** não é validado na borda. O importador usa JSON Schema draft-4 parcial e respondia 400 onde o contrato promete 422 (tipo errado, campo desconhecido, valor ≤ 0). O serviço é a autoridade do corpo (ADR-0011), igual no ambiente local e na AWS.
- **Integração privada: VPC Link (v1) → NLB interno → tasks.** O VPC Link v2 → ALB existe, mas preferimos o caminho com maior maturidade no provider Terraform. A troca é local ao módulo `api`.
- **Observabilidade:** access log JSON (requestId, extendedRequestId, ip, status, latency, integrationLatency, clientId) e X-Ray ligado no stage. A integração recebe `X-Gateway-Request-Id` = `$context.requestId`, a chave que junta a linha da borda com a do serviço (ADR-0017).

## Consequências

- **Positivas:**
  - Autenticação, autorização por escopo, cotas, validação e WAF saem do código do serviço.
  - O contrato publicado é o mesmo que o gateway aplica.
  - Os erros seguem o padrão em todas as camadas.
- **Negativas:**
  - **O importador da REST API é limitado** (JSON Schema draft-4, sem `nullable`/`format`): por isso o gateway não valida o corpo e o serviço valida tudo (`fail_on_warnings = false`, documentado). O custo é o corpo inválido chegar ao serviço e gastar CPU dele.
  - **O 429 do WAF não é `application/problem+json`** no `Content-Type` nem traz `correlation_id`: limitação do WAF, declarada no catálogo de erros.
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
- Tipos de gateway response (`WAF_FILTERED`, herança do `DEFAULT_4XX`): https://docs.aws.amazon.com/apigateway/latest/developerguide/supported-gateway-response-types.html
- WAF, resposta customizada do Block (precedência sobre o recurso protegido, `content-type` proibido): https://docs.aws.amazon.com/waf/latest/developerguide/customizing-the-response-for-blocked-requests.html
- WAF, agregação por chave customizada (requisição sem a chave não conta): https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-rate-based-aggregation-options.html
- Cognito resource servers e escopos: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html
