# Rede do sandbox: 2 AZs, só subnets PÚBLICAS, sem NAT.
#
# DESVIO CONSCIENTE DO SANDBOX (custo): as tasks Fargate sobem em subnets públicas com IP público para
# puxar imagem do ECR e falar com APIs AWS sem pagar NAT Gateway (~US$ 0,045/h por AZ + dados).
# O tráfego de entrada continua fechado: o único caminho é NLB interno <- VPC Link; o SG das tasks só
# aceita a porta 8080 a partir do CIDR da VPC. Produção usa subnets privadas + interface endpoints
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

# NOTA: não existe SG "nlb_alvos". O NLB interno não tem Security Group por padrão (e aqui não usamos
# o recurso opcional): os alvos veem o IP do NLB/VPC Link dentro do CIDR da VPC, por isso o SG das tasks
# libera a 8080 a partir do CIDR da VPC (NLB + Service Connect entre tasks).

# SG das tasks Fargate: entrada 8080 só da VPC; saída 443 (APIs AWS) e 8080 para a VPC (Service Connect).
resource "aws_security_group" "tasks" {
  #checkov:skip=CKV2_AWS_5:O SG é anexado às tasks Fargate pelo módulo ecs_servico (via variável), fora do alcance do checkov.
  name_prefix = "${var.nome}-tasks-"
  description = "Tasks Fargate: 8080 da VPC; saida 443 e 8080 para a VPC"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.nome}-tasks" }

  lifecycle {
    create_before_destroy = true
  }
}

# Entrada 8080 a partir do CIDR da VPC (NLB e chamadas autorizacao -> limites via Service Connect).
resource "aws_vpc_security_group_ingress_rule" "tasks_8080" {
  security_group_id = aws_security_group.tasks.id
  description       = "App 8080 a partir da VPC (NLB e Service Connect)"
  cidr_ipv4         = var.cidr
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
}

# Saída HTTPS para APIs AWS (ECR, Logs, DynamoDB, STS). Só a porta 443; o destino é aberto porque o sandbox não
# tem NAT nem VPC endpoints (endpoints de interface custam por hora e por AZ). Produção: VPC endpoints (gateway
# para DynamoDB/S3, interface para ECR/Logs/STS/SQS/EventBridge) e egress só para os prefix lists deles.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "tasks_443" {
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS para APIs AWS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Saída 8080 apenas para dentro da VPC (autorizacao -> limites).
resource "aws_vpc_security_group_egress_rule" "tasks_8080_vpc" {
  security_group_id = aws_security_group.tasks.id
  description       = "Service Connect 8080 dentro da VPC"
  cidr_ipv4         = var.cidr
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
}
