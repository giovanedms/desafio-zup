# Terraform
`modules/` reutilizáveis (`rede`, `api`, `ecs_servico`, `dados`, `ecr`, `eventos`, `lambdas`, `observabilidade`, `plataforma_de_dados`) e dois ambientes:
- `environments/sandbox`: a stack (251 recursos, conta desafio-zup, sa-east-1), aplicada pela esteira ou por `make deploy`. `plataforma_de_dados` é a data platform (regra do bus → Firehose → S3 particionado, com alarme de defasagem); `plataforma_de_dados_ativa = false` a desliga na janela de medição de carga (`terraform.tfvars.example`);
- `environments/esteira`: a identidade da pipeline (OIDC do GitHub, role de deploy, permissions boundary), aplicada uma vez por um administrador.

Testes com `mock_provider` em `environments/sandbox/tests/` (`sandbox.tftest.hcl` e `consumidores.tftest.hcl`, 19 `run`; diretório real, não symlink: `make tf-test` passa `-test-directory=tests` e falha se nenhum `run` for executado). Toda resource com comentário e tags obrigatórias: environment, team, managed-by.
Inventário, bootstrap da conta e esteira: `docs/operacao/infraestrutura.md`.
