locals {
  # Escopos completos do Cognito: <identifier do resource server>/<scope>. O contrato público usa
  # `autorizacoes.write`/`.read`; o Cognito fixa o separador "/" (diferença de nomenclatura, não de semântica).
  escopo_write = "autorizacoes/write"
  escopo_read  = "autorizacoes/read"

  contrato = yamldecode(file(var.contrato_openapi))

  # Extensões do API Gateway injetadas por operação (sem tocar no contrato).
  paths = {
    for caminho, operacoes in local.contrato.paths : caminho => {
      for metodo, op in operacoes : metodo => merge(op, {
        # Cognito (com escopo) E API key do usage plan: ambos exigidos no mesmo requisito.
        security = [{
          cognito = [metodo == "get" ? local.escopo_read : local.escopo_write]
          api_key = []
        }]
        "x-amazon-apigateway-request-validator" = "completo"
        "x-amazon-apigateway-integration" = {
          type                = "http_proxy"
          httpMethod          = upper(metodo)
          uri                 = "http://${aws_lb.this.dns_name}${caminho}"
          connectionType      = "VPC_LINK"
          connectionId        = aws_api_gateway_vpc_link.this.id
          passthroughBehavior = "when_no_match"
          timeoutInMillis     = 29000
          # O serviço confia no client_id que o gateway extrai do token (sobrescreve qualquer X-Client-Id do cliente).
          requestParameters = merge(
            { "integration.request.header.X-Client-Id" = "context.authorizer.claims.client_id" },
            { for p in [for g in regexall("\\{([^}]+)\\}", caminho) : g[0]] : "integration.request.path.${p}" => "method.request.path.${p}" },
          )
        }
      })
    }
  }

  especificacao = merge(local.contrato, {
    paths    = local.paths
    security = [{ cognito = [local.escopo_write], api_key = [] }]
    components = merge(local.contrato.components, {
      securitySchemes = {
        cognito = {
          type                           = "apiKey"
          name                           = "Authorization"
          in                             = "header"
          "x-amazon-apigateway-authtype" = "cognito_user_pools"
          "x-amazon-apigateway-authorizer" = {
            type         = "cognito_user_pools"
            providerARNs = [aws_cognito_user_pool.this.arn]
          }
        }
        api_key = {
          type = "apiKey"
          name = "x-api-key"
          in   = "header"
        }
      }
    })
    "x-amazon-apigateway-request-validators" = {
      completo = { validateRequestBody = true, validateRequestParameters = true }
    }
    # Gateway Responses DENTRO do body importado. Como resources separados elas eram apagadas a cada
    # reimportação do contrato (PutRestApi em modo overwrite) e o state não percebia (achado no deploy 1.0.1).
    "x-amazon-apigateway-gateway-responses" = local.respostas_do_gateway
  })

  # Gateway Responses em application/problem+json (RFC 9457); slugs e códigos iguais ao catálogo.
  # DEFAULT_4XX/DEFAULT_5XX fixam o status (400/500) para o corpo não divergir do HTTP real.
  respostas = {
    UNAUTHORIZED           = { status = 401, slug = "nao-autenticado", codigo = "NAO_AUTENTICADO", titulo = "Não autenticado", retry_after = null }
    ACCESS_DENIED          = { status = 403, slug = "sem-permissao", codigo = "SEM_PERMISSAO", titulo = "Sem permissão", retry_after = null }
    THROTTLED              = { status = 429, slug = "limite-de-requisicoes", codigo = "LIMITE_DE_REQUISICOES", titulo = "Limite de requisições excedido", retry_after = "1" }
    QUOTA_EXCEEDED         = { status = 429, slug = "limite-de-requisicoes", codigo = "LIMITE_DE_REQUISICOES", titulo = "Cota do plano excedida", retry_after = "3600" }
    BAD_REQUEST_BODY       = { status = 400, slug = "requisicao-mal-formada", codigo = "REQUISICAO_MAL_FORMADA", titulo = "Requisição mal formada", retry_after = null }
    BAD_REQUEST_PARAMETERS = { status = 400, slug = "requisicao-mal-formada", codigo = "REQUISICAO_MAL_FORMADA", titulo = "Requisição mal formada", retry_after = null }
    INTEGRATION_TIMEOUT    = { status = 504, slug = "tempo-esgotado", codigo = "TEMPO_ESGOTADO", titulo = "Tempo esgotado", retry_after = null }
    DEFAULT_4XX            = { status = 400, slug = "requisicao-mal-formada", codigo = "REQUISICAO_MAL_FORMADA", titulo = "Requisição mal formada", retry_after = null }
    DEFAULT_5XX            = { status = 500, slug = "erro-interno", codigo = "ERRO_INTERNO", titulo = "Erro interno", retry_after = null }
  }

  # Verificado na AWS (20/10/2026): com a chave "application/json" o gateway respondia Content-Type
  # application/json mesmo com o header sobrescrito. A chave do template define o media type.
  respostas_do_gateway = {
    for tipo, r in local.respostas : tipo => {
      statusCode = r.status
      responseParameters = merge(
        { "gatewayresponse.header.Content-Type" = "'application/problem+json'" },
        r.retry_after == null ? {} : { "gatewayresponse.header.Retry-After" = "'${r.retry_after}'" },
      )
      responseTemplates = {
        "application/problem+json" = jsonencode({
          type           = "https://financas.example/problemas/${r.slug}"
          title          = r.titulo
          status         = r.status
          codigo         = r.codigo
          correlation_id = "$context.requestId"
        })
      }
    }
  }
}

# ------------------------------------------------------------------ NLB interno + VPC Link

# NLB INTERNO na frente do serviço autorizacao. NLB não precisa de SG aqui (ver módulo rede).
# REST -> ALB via VPC Link v2 existe desde nov/2025, mas usamos NLB pela maturidade no provider (ADR-0010).
resource "aws_lb" "this" {
  #checkov:skip=CKV2_AWS_20:NLB interno com listener TCP atrás do VPC Link; não há redirect HTTP->HTTPS a fazer.
  #checkov:skip=CKV_AWS_91:Access log de NLB só existe para listeners TLS; o listener é TCP dentro do VPC Link.
  #checkov:skip=CKV_AWS_150:Sandbox: deletion_protection é variável (false para o destroy funcionar); produção liga.
  name                             = "${var.nome}-nlb"
  load_balancer_type               = "network"
  internal                         = true
  subnets                          = var.subnet_ids
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = var.deletion_protection
}

# Target group por IP (awsvpc/Fargate), health check HTTP no /saude.
resource "aws_lb_target_group" "autorizacao" {
  name                 = "${var.nome}-tg"
  port                 = var.porta_alvo
  protocol             = "TCP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    protocol            = "HTTP"
    path                = var.caminho_saude
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }
}

# Listener TCP 80: tráfego já protegido pelo VPC Link (PrivateLink); sem TLS no sandbox.
resource "aws_lb_listener" "http" {
  #checkov:skip=CKV_AWS_2:Listener TCP interno atrás do VPC Link (PrivateLink); TLS fim a fim fica para produção.
  #checkov:skip=CKV_AWS_103:Idem: sem TLS no listener interno do sandbox.
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.autorizacao.arn
  }
}

# VPC Link (REST, v1) apontando para o NLB.
resource "aws_api_gateway_vpc_link" "this" {
  name        = "${var.nome}-nlb"
  description = "VPC Link do API Gateway REST para o NLB interno do servico autorizacao"
  target_arns = [aws_lb.this.arn]
}

# ------------------------------------------------------------------ Cognito (OAuth2 client credentials)

# User pool usado só como emissor de tokens machine-to-machine (sem usuários, sem cadastro).
resource "aws_cognito_user_pool" "this" {
  name = "${var.nome}-canais"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }
}

# Resource server `autorizacoes` com os escopos write/read.
resource "aws_cognito_resource_server" "this" {
  identifier   = "autorizacoes"
  name         = "autorizacoes"
  user_pool_id = aws_cognito_user_pool.this.id

  scope {
    scope_name        = "write"
    scope_description = "Criar autorizacoes"
  }

  scope {
    scope_name        = "read"
    scope_description = "Consultar autorizacoes"
  }
}

# Domínio do endpoint /oauth2/token (prefixo único por conta).
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.nome}-${var.account_id}"
  user_pool_id = aws_cognito_user_pool.this.id
}

# Um app client por canal, com secret e fluxo client_credentials.
resource "aws_cognito_user_pool_client" "canal" {
  for_each = var.canais

  name                                 = each.key
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = [local.escopo_write, local.escopo_read]
  supported_identity_providers         = ["COGNITO"]

  depends_on = [aws_cognito_resource_server.this]
}

# ------------------------------------------------------------------ API REST

# API REGIONAL criada a partir do contrato OpenAPI + extensões injetadas.
# LIMITAÇÕES DO IMPORTADOR: o modelo de validação é JSON Schema draft-4 (sem `nullable`, sem `format`
# como validação, `example` ignorado); gera avisos que não são erros, por isso fail_on_warnings = false.
# Validação semântica fina (UUID, 2 casas decimais, allowlist) continua no serviço.
resource "aws_api_gateway_rest_api" "this" {
  name                         = var.nome
  description                  = "API de Autorização de Transações (contrato autorizacao-v1)"
  body                         = jsonencode(local.especificacao)
  fail_on_warnings             = false
  disable_execute_api_endpoint = false

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Deployment: recriado quando o contrato injetado (inclusive as gateway responses) muda.
resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeployment = sha1(aws_api_gateway_rest_api.this.body)
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Log group do access log (JSON), retenção curta no sandbox.
resource "aws_cloudwatch_log_group" "acesso" {
  #checkov:skip=CKV_AWS_338:Retenção de 7 dias é desvio consciente do sandbox (custo); produção >= 365 dias.
  #checkov:skip=CKV_AWS_158:Sandbox usa a criptografia padrão do CloudWatch Logs; produção usa CMK.
  name              = "/aws/apigateway/${var.nome}-${var.nome_stage}"
  retention_in_days = var.retencao_logs_dias
}

# Role de conta que permite ao API Gateway escrever no CloudWatch Logs (recurso singleton por região/conta).
resource "aws_iam_role" "apigw_logs" {
  name                 = "${var.nome}-apigw-cloudwatch"
  permissions_boundary = var.limite_de_permissoes_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Política gerenciada oficial da AWS para push de logs do API Gateway.
resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# Registra a role na conta (necessário para access log de stage).
resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn

  depends_on = [aws_iam_role_policy_attachment.apigw_logs]
}

# Stage v1 com access log JSON e X-Ray.
resource "aws_api_gateway_stage" "this" {
  #checkov:skip=CKV_AWS_120:Cache do stage desligado: respostas de autorização não são cacheáveis.
  #checkov:skip=CKV2_AWS_51:mTLS/certificado de cliente não se aplica (autenticação por OAuth2 + API key).
  #checkov:skip=CKV2_AWS_77:O Web ACL do sandbox tem só rate limit + CommonRuleSet (escopo do desafio); produção adiciona KnownBadInputs (Log4j).
  rest_api_id          = aws_api_gateway_rest_api.this.id
  deployment_id        = aws_api_gateway_deployment.this.id
  stage_name           = var.nome_stage
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.acesso.arn
    format = jsonencode({
      requestId          = "$context.requestId"
      ip                 = "$context.identity.sourceIp"
      requestTime        = "$context.requestTime"
      httpMethod         = "$context.httpMethod"
      resourcePath       = "$context.resourcePath"
      status             = "$context.status"
      latency            = "$context.responseLatency"
      integrationLatency = "$context.integrationLatency"
      clientId           = "$context.authorizer.claims.client_id"
    })
  }

  depends_on = [aws_api_gateway_account.this, aws_cloudwatch_log_group.execucao]
}

# Métricas detalhadas por método (base dos alarmes/dashboards).
resource "aws_api_gateway_method_settings" "todos" {
  #checkov:skip=CKV_AWS_225:Cache desligado: respostas de autorização não são cacheáveis.
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
    logging_level   = "ERROR"
  }
}

# Log group de execução do API Gateway: o nome é fixo pela AWS; criado aqui para controlar a retenção.
resource "aws_cloudwatch_log_group" "execucao" {
  #checkov:skip=CKV_AWS_338:Retenção de 7 dias é desvio consciente do sandbox (custo); produção >= 365 dias.
  #checkov:skip=CKV_AWS_158:Sandbox usa a criptografia padrão do CloudWatch Logs; produção usa CMK.
  name              = "API-Gateway-Execution-Logs_${aws_api_gateway_rest_api.this.id}/${var.nome_stage}"
  retention_in_days = var.retencao_logs_dias
}

# ------------------------------------------------------------------ Usage plans e API keys

# Usage plans: padrão (200 rps) e restrito (5 rps, para demonstrar 429).
resource "aws_api_gateway_usage_plan" "this" {
  for_each = var.planos

  name = each.key

  api_stages {
    api_id = aws_api_gateway_rest_api.this.id
    stage  = aws_api_gateway_stage.this.stage_name
  }

  throttle_settings {
    rate_limit  = each.value.rate_limit
    burst_limit = each.value.burst_limit
  }

  quota_settings {
    limit  = each.value.quota_dia
    period = "DAY"
  }
}

# Uma API key por canal (os métodos exigem x-api-key).
resource "aws_api_gateway_api_key" "canal" {
  for_each = var.canais

  name = each.key
}

# Vincula cada key ao plano do canal.
resource "aws_api_gateway_usage_plan_key" "canal" {
  for_each = var.canais

  key_id        = aws_api_gateway_api_key.canal[each.key].id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.this[each.value].id
}

# ------------------------------------------------------------------ WAF

# WAFv2 regional: rate limit por IP + regras comuns gerenciadas.
resource "aws_wafv2_web_acl" "this" {
  #checkov:skip=CKV2_AWS_31:Logging do WAF (Firehose/S3) fica fora do sandbox por custo; produção habilita.
  #checkov:skip=CKV_AWS_192:Conjunto de regras fixado em rate limit + CommonRuleSet pelo escopo do desafio; produção adiciona KnownBadInputs (Log4j).
  name  = "${var.nome}-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "limite-por-ip"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.waf_limite_por_ip
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "limite-por-ip"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "aws-common"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.nome}-waf"
    sampled_requests_enabled   = true
  }
}

# Associa o Web ACL ao stage v1.
resource "aws_wafv2_web_acl_association" "this" {
  resource_arn = aws_api_gateway_stage.this.arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}

# Região atual (monta o endpoint do Cognito).
data "aws_region" "atual" {}
