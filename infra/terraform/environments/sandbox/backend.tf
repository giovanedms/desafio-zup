# Backend S3 PARCIAL: o nome do bucket não é versionado (depende da conta). O Makefile (`make deploy`)
# o injeta no init: -backend-config="bucket=desafio-zup-tfstate-<ACCOUNT_ID>".
# use_lockfile = true usa lock nativo do S3 (sem DynamoDB de lock).
terraform {
  backend "s3" {
    key          = "sandbox/terraform.tfstate"
    region       = "sa-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
