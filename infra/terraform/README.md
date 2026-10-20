# Terraform
`modules/` reutilizáveis (`rede`, `api`, `ecs_servico`, `dados`, `ecr`, `eventos`, `lambdas`, `observabilidade`) e dois ambientes:
- `environments/sandbox`: a stack (162 recursos, conta desafio-zup, sa-east-1), aplicada pela esteira ou por `make deploy`;
- `environments/esteira`: a identidade da pipeline (OIDC do GitHub, role de deploy, permissions boundary), aplicada uma vez por um administrador.

Testes com `mock_provider` em `tests/`. Toda resource com comentário e tags obrigatórias: environment, team, managed-by.
Inventário, bootstrap da conta e esteira: `docs/operacao/infraestrutura.md`.
