# Backend S3 PARCIAL (mesmo bucket do sandbox, outra chave): o nome do bucket depende da conta e entra no init.
# Este ambiente é aplicado UMA vez por um administrador (profile desafio-zup), nunca pela própria esteira.
terraform {
  backend "s3" {
    key          = "esteira/terraform.tfstate"
    region       = "sa-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
