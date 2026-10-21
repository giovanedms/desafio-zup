# Ambiente `esteira`: identidade da pipeline de CD na conta desafio-zup.
#   - provedor OIDC do GitHub Actions (sem chave de longa duração em segredo de repositório);
#   - permissions boundary que toda role da stack carrega (teto de permissões das workloads);
#   - role de deploy: só a branch de deploy do repositório assume; IAM delegada com o boundary obrigatório.
provider "aws" {
  region              = "sa-east-1"
  profile             = var.aws_profile
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      environment = "sandbox"
      team        = var.time
      managed-by  = "terraform"
      project     = "desafio-autorizacao"
    }
  }
}

locals {
  emissor_oidc = "token.actions.githubusercontent.com"
  # Claim `sub` no formato de identificadores imutáveis que o GitHub emite para este repositório
  # (dono@id/repo@id). Visto no CloudTrail no 1º deploy: o formato só com nomes era recusado. Os IDs não
  # mudam com rename e não são reaproveitados: um repositório recriado com o mesmo nome não assume a role.
  dono         = split("/", var.repositorio_github)[0]
  repo         = split("/", var.repositorio_github)[1]
  sub_do_token = "repo:${local.dono}@${var.id_dono_github}/${local.repo}@${var.id_repositorio_github}:ref:refs/heads/${var.branch_de_deploy}"
  nome_role    = "desafio-zup-esteira"
  nome_limite  = "desafio-zup-limite-de-permissoes"
  arn_limite   = "arn:aws:iam::${var.account_id}:policy/${local.nome_limite}"
  arn_role     = "arn:aws:iam::${var.account_id}:role/${local.nome_role}"
}

# Provedor OIDC do GitHub: o STS valida a assinatura do token pelo JWKS do emissor (thumbprint não é mais exigido).
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.emissor_oidc}"
  client_id_list = ["sts.amazonaws.com"]
}

# Teto de permissões de TODA role criada pela stack (tasks ECS, Lambdas, scheduler, API Gateway). As políticas
# de cada role continuam estreitas; o boundary garante que nenhuma role criada pela esteira passe disto.
resource "aws_iam_policy" "limite" {
  name        = local.nome_limite
  description = "Permissions boundary das roles da stack desafio-autorizacao (o que uma workload pode, no máximo)."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Dados"
        Effect = "Allow"
        Action = [
          "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem", "dynamodb:DeleteItem", "dynamodb:DescribeStream",
          "dynamodb:GetItem", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams",
          "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:UpdateItem",
        ]
        Resource = "arn:aws:dynamodb:sa-east-1:${var.account_id}:table/*"
      },
      {
        Sid    = "Mensageria"
        Effect = "Allow"
        Action = [
          "sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
          "sqs:ReceiveMessage", "sqs:SendMessage",
        ]
        Resource = "arn:aws:sqs:sa-east-1:${var.account_id}:*"
      },
      {
        Sid      = "Eventos"
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = "arn:aws:events:sa-east-1:${var.account_id}:event-bus/*"
      },
      {
        Sid      = "InvocarLambdasDaConta"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:sa-east-1:${var.account_id}:function:*"
      },
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogGroups", "logs:DescribeLogStreams",
          "logs:FilterLogEvents", "logs:GetLogEvents", "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:sa-east-1:${var.account_id}:*"
      },
      {
        Sid      = "ImagensDoEcr"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:aws:ecr:sa-east-1:${var.account_id}:repository/*"
      },
      {
        # Ações sem suporte a nível de recurso na AWS: só existem com Resource "*".
        Sid      = "SemNivelDeRecurso"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken", "xray:PutTelemetryRecords", "xray:PutTraceSegments", "cloudwatch:PutMetricData"]
        Resource = "*"
      },
    ]
  })
}

# Role da esteira: só workflows da branch de deploy deste repositório assumem (claim `sub` exato).
resource "aws_iam_role" "esteira" {
  name                 = local.nome_role
  description          = "Deploy do sandbox pela esteira (GitHub Actions OIDC)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.emissor_oidc}:aud" = "sts.amazonaws.com"
          "${local.emissor_oidc}:sub" = local.sub_do_token
        }
      }
    }]
  })
}

# Tudo menos IAM/Organizations/Account (política gerenciada da AWS). Desvio consciente do sandbox: em produção,
# trocar por política gerada pelo IAM Access Analyzer a partir do CloudTrail das execuções da esteira.
resource "aws_iam_role_policy_attachment" "power_user" {
  #checkov:skip=CKV_AWS_274:PowerUserAccess exclui IAM; IAM fica delegada abaixo com boundary obrigatório.
  role       = aws_iam_role.esteira.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# IAM delegada: a esteira só cria/altera role que carregue o boundary; não mexe na própria role, na role de
# administração da Organization nem no boundary (padrão "delegated admin" com permissions boundary). Na mesma política,
# o perímetro da trilha de auditoria: Deny no CloudTrail, no contorno do Object Lock e no bucket da trilha.
resource "aws_iam_role_policy" "iam_delegada" {
  name = "iam-com-boundary-obrigatorio"
  role = aws_iam_role.esteira.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CriarEAlterarRoleSoComBoundary"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:PutRolePermissionsBoundary",
          "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
        ]
        Resource  = "arn:aws:iam::${var.account_id}:role/*"
        Condition = { StringEquals = { "iam:PermissionsBoundary" = local.arn_limite } }
      },
      {
        Sid    = "ManterRolesDaStack"
        Effect = "Allow"
        Action = [
          "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole",
          "iam:UpdateRoleDescription",
        ]
        Resource  = "arn:aws:iam::${var.account_id}:role/*"
        Condition = { StringEquals = { "aws:ResourceTag/project" = "desafio-autorizacao" } }
      },
      {
        Sid       = "EtiquetarRoleNovaDaStack"
        Effect    = "Allow"
        Action    = "iam:TagRole"
        Resource  = "arn:aws:iam::${var.account_id}:role/*"
        Condition = { StringEquals = { "aws:RequestTag/project" = "desafio-autorizacao" } }
      },
      {
        Sid      = "PassarRoleSoParaOsServicosDaStack"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:aws:iam::${var.account_id}:role/*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = [
              "ecs-tasks.amazonaws.com", "lambda.amazonaws.com", "apigateway.amazonaws.com", "scheduler.amazonaws.com",
            ]
          }
        }
      },
      {
        Sid      = "LeituraDeIam"
        Effect   = "Allow"
        Action   = ["iam:Get*", "iam:List*"]
        Resource = "*"
      },
      {
        Sid    = "NuncaMexerNaPropriaIdentidade"
        Effect = "Deny"
        Action = "iam:*"
        Resource = [
          local.arn_role,
          local.arn_limite,
          "arn:aws:iam::${var.account_id}:role/OrganizationAccountAccessRole",
          aws_iam_openid_connect_provider.github.arn,
        ]
      },
      {
        Sid      = "NuncaTirarOBoundary"
        Effect   = "Deny"
        Action   = "iam:DeleteRolePermissionsBoundary"
        Resource = "*"
      },
      # Perímetro da trilha de auditoria (painel 6, G6). O PowerUserAccess dá s3:* e cloudtrail:*, então um commit na
      # main podia apagar o trail ou as versões do bucket WORM (Object Lock GOVERNANCE se contorna com
      # s3:BypassGovernanceRetention). Deny explícito vence o Allow da política gerenciada. A esteira nunca administra
      # o trail nem o bucket dele (camada 0, bootstrap manual), então nada do deploy/destroy depende destas ações.
      {
        Sid      = "NuncaDesligarATrilha"
        Effect   = "Deny"
        Action   = ["cloudtrail:DeleteTrail", "cloudtrail:StopLogging", "cloudtrail:UpdateTrail", "cloudtrail:PutEventSelectors"]
        Resource = "*"
      },
      {
        # Contornar retenção de Object Lock em modo GOVERNANCE: nenhum bucket da conta precisa disso pela esteira.
        Sid      = "NuncaContornarObjectLock"
        Effect   = "Deny"
        Action   = "s3:BypassGovernanceRetention"
        Resource = "*"
      },
      {
        # Bucket da trilha: apagar versão, trocar o Object Lock ou o lifecycle (encurtar a retenção) e reescrever a
        # policy (negar a entrega do trail também é desligar a trilha).
        Sid    = "NuncaMexerNoBucketDaTrilha"
        Effect = "Deny"
        Action = [
          "s3:DeleteObjectVersion", "s3:PutBucketObjectLockConfiguration", "s3:PutLifecycleConfiguration",
          "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
        ]
        Resource = ["arn:aws:s3:::desafio-zup-cloudtrail-*", "arn:aws:s3:::desafio-zup-cloudtrail-*/*"]
      },
    ]
  })
}
