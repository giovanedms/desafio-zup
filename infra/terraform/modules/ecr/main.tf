# Repositórios de imagem: scan on push, tags IMUTÁVEIS (cada release exige uma tag nova).
resource "aws_ecr_repository" "this" {
  #checkov:skip=CKV_AWS_136:Sandbox usa criptografia AES256 padrão; produção usa CMK.
  for_each = var.repositorios

  name                 = "${var.prefixo}${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Lifecycle: mantém só as N imagens mais recentes (controla custo de armazenamento).
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Manter as ${var.imagens_mantidas} imagens mais recentes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.imagens_mantidas
      }
      action = { type = "expire" }
    }]
  })
}
