# Rede do sandbox: 2 AZs, só subnets PÚBLICAS, sem NAT.
#
# DESVIO CONSCIENTE DO SANDBOX (custo): as tasks Fargate sobem em subnets públicas com IP público para
# puxar imagem do ECR e falar com APIs AWS sem pagar NAT Gateway (~US$ 0,045/h por AZ + dados).
# O tráfego de entrada continua fechado: o único caminho de fora é NLB interno <- VPC Link, e cada serviço tem SG
# próprio (segmentação leste-oeste, abaixo). Produção usa subnets privadas + interface endpoints
# (ECR, Logs, STS) + gateway endpoints, ou NAT. Ver ADR-0004.

# VPC do ambiente.
resource "aws_vpc" "this" {
  #checkov:skip=CKV2_AWS_11:Sandbox sem flow logs para conter custo; produção habilita (retenção 90 dias) em destino central.
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.nome }
}

# Remove todas as regras do SG default da VPC (boa prática CIS: ninguém deve usá-lo).
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.nome}-default-fechado" }
}

# Internet Gateway: saída das tasks (ECR/APIs AWS) sem NAT.
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = var.nome }
}

# Subnets públicas, uma por AZ (sem auto-assign: o IP público é pedido explicitamente pelo service ECS).
resource "aws_subnet" "publica" {
  count = length(var.zonas)

  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.zonas[count.index]
  cidr_block              = cidrsubnet(var.cidr, 8, count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${var.nome}-publica-${var.zonas[count.index]}" }
}

# Tabela de rotas pública: default route para o IGW.
resource "aws_route_table" "publica" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.nome}-publica" }
}

# Rota default (0.0.0.0/0) via Internet Gateway.
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.publica.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

# Associa cada subnet pública à tabela de rotas pública.
resource "aws_route_table_association" "publica" {
  count = length(var.zonas)

  subnet_id      = aws_subnet.publica[count.index].id
  route_table_id = aws_route_table.publica.id
}

# Gateway endpoint do DynamoDB (gratuito): o tráfego das tasks ao DynamoDB não sai pela internet.
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.regiao}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.publica.id]

  tags = { Name = "${var.nome}-dynamodb" }
}

# Gateway endpoint do S3 (gratuito): camadas de imagem do ECR vêm do S3.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.regiao}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.publica.id]

  tags = { Name = "${var.nome}-s3" }
}

# Segmentação leste-oeste: um SG por serviço, e a 8080 só abre para quem precisa chamar.
#   autorizacao    <- NLB interno (CIDR da VPC, ver abaixo)   -> limites:8080 (Service Connect)
#   limites        <- SG do autorizacao e SG do reconciliador
#   limites-acl    sem entrada (worker: lê SQS, grava DynamoDB)
#   reconciliador  sem entrada (worker)                        -> limites:8080 (Service Connect)
#
# Por que o autorizacao aceita o CIDR da VPC, e não o SG do NLB: o NLB foi criado SEM security group, e a AWS não deixa
# associar SG a um NLB depois de criado ("If you create a Network Load Balancer without associating any security
# groups, you can't associate them with the Network Load Balancer later on"):
# https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-security-groups.html
# Ligar agora recriaria o NLB e, com ele, o VPC Link e a integração do API Gateway. Os alvos (tipo ip) veem o IP privado
# do nó do NLB, que mora nas subnets desta VPC: sem SG no NLB, o CIDR é a origem mais estreita disponível. Produção: NLB
# com SG desde a criação e a entrada do autorizacao por referenced_security_group_id ao SG do NLB.
#
# Service Connect com SG por referência funciona porque o proxy (sidecar) roda DENTRO da task cliente: a conexão sai da
# ENI da task cliente (awsvpc) direto para a containerPort do servidor, onde escuta o proxy do servidor. A AWS manda
# liberar a containerPort para os clientes:
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-concepts-deploy.html#service-connect-concepts-network

# SG do autorizacao: alvo do NLB interno e cliente do limites.
resource "aws_security_group" "autorizacao" {
  #checkov:skip=CKV2_AWS_5:O SG é anexado às tasks Fargate pelo módulo ecs_servico (via variável), fora do alcance do checkov.
  name_prefix = "${var.nome}-autorizacao-"
  description = "autorizacao: 8080 do NLB interno (CIDR da VPC); saida 443 e 8080 so para o limites"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.nome}-autorizacao" }

  lifecycle {
    create_before_destroy = true
  }
}

# SG do limites: API interna que move o limite; só o autorizacao e o reconciliador chegam nela.
resource "aws_security_group" "limites" {
  #checkov:skip=CKV2_AWS_5:O SG é anexado às tasks Fargate pelo módulo ecs_servico (via variável), fora do alcance do checkov.
  name_prefix = "${var.nome}-limites-"
  description = "limites: 8080 so dos SGs do autorizacao e do reconciliador; saida 443"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.nome}-limites" }

  lifecycle {
    create_before_destroy = true
  }
}

# SG do limites-acl: worker sem porta exposta (fila do legado -> tabela limites).
resource "aws_security_group" "limites_acl" {
  #checkov:skip=CKV2_AWS_5:O SG é anexado às tasks Fargate pelo módulo ecs_servico (via variável), fora do alcance do checkov.
  name_prefix = "${var.nome}-limites-acl-"
  description = "limites-acl: sem entrada; saida 443"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.nome}-limites-acl" }

  lifecycle {
    create_before_destroy = true
  }
}

# SG do reconciliador: worker sem porta exposta, cliente do limites.
resource "aws_security_group" "reconciliador" {
  #checkov:skip=CKV2_AWS_5:O SG é anexado às tasks Fargate pelo módulo ecs_servico (via variável), fora do alcance do checkov.
  name_prefix = "${var.nome}-reconciliador-"
  description = "reconciliador: sem entrada; saida 443 e 8080 so para o limites"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.nome}-reconciliador" }

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  # Clientes do limites pelo Service Connect: a 8080 do limites só aceita estes SGs, e só eles saem para ela.
  clientes_do_limites = {
    autorizacao   = aws_security_group.autorizacao.id
    reconciliador = aws_security_group.reconciliador.id
  }

  # Todos os SGs de serviço (saída HTTPS e output do módulo).
  sgs_dos_servicos = {
    autorizacao   = aws_security_group.autorizacao.id
    limites       = aws_security_group.limites.id
    "limites-acl" = aws_security_group.limites_acl.id
    reconciliador = aws_security_group.reconciliador.id
  }
}

# Entrada 8080 do autorizacao a partir do CIDR da VPC: tráfego e health check do NLB, que não tem SG (ver acima).
resource "aws_vpc_security_group_ingress_rule" "autorizacao_do_nlb" {
  security_group_id = aws_security_group.autorizacao.id
  description       = "App 8080 a partir do NLB interno (sem SG; origem = IPs do NLB na VPC)"
  cidr_ipv4         = var.cidr
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
}

# Entrada 8080 do limites só a partir dos SGs dos clientes (autorizacao e reconciliador), por referência de SG.
resource "aws_vpc_security_group_ingress_rule" "limites_dos_clientes" {
  for_each = local.clientes_do_limites

  security_group_id            = aws_security_group.limites.id
  description                  = "Service Connect 8080 a partir do ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# Saída 8080 dos clientes só para o SG do limites (Service Connect).
resource "aws_vpc_security_group_egress_rule" "clientes_para_limites" {
  for_each = local.clientes_do_limites

  security_group_id            = each.value
  description                  = "Service Connect 8080 para o limites"
  referenced_security_group_id = aws_security_group.limites.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# Saída HTTPS para APIs AWS (ECR, Logs, DynamoDB, STS, SQS), em todo SG de serviço. Só a porta 443; o destino é aberto
# porque o sandbox não tem NAT nem VPC endpoints de interface (custam por hora e por AZ). Produção: VPC endpoints
# (gateway para DynamoDB/S3, interface para ECR/Logs/STS/SQS/EventBridge) e egress só para os prefix lists deles.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "https" {
  for_each = local.sgs_dos_servicos

  security_group_id = each.value
  description       = "HTTPS para APIs AWS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
