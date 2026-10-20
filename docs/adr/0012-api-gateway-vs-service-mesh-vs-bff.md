# ADR-0012 — API Gateway no north-south, ECS Service Connect no east-west, sem BFF

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** APIM-04

## Contexto

O enunciado pede para "discutir o papel de um API Gateway vs. Service Mesh e de um BFF". O Itaú descreve publicamente API Gateway no north-south e service mesh no east-west (blog AWS, 2022). Fato novo: o **AWS App Mesh foi descontinuado em 30/09/2026**. As opções de mesh gerenciado hoje são **ECS Service Connect** (Envoy gerenciado, só ECS) e **VPC Lattice** (L7 entre VPCs, contas e compute heterogêneo). Istio/Linkerd exigem EKS.

## Decisão

| Fluxo | Peça | O que resolve |
|---|---|---|
| **North-south** (canais → plataforma) | **API Gateway REST** | Identidade do consumidor (OAuth2/Cognito), escopos, cotas por canal, WAF, validação de contrato, versionamento, erros padronizados na borda |
| **East-west** (autorizacao → limites; ACL e reconciliador → serviços) | **ECS Service Connect** | Descoberta por nome (`limites:8080`), balanceamento, retry e timeout de conexão no proxy, métricas por rota sem código |
| **BFF** | **Não se aplica** | Os consumidores desta API são **sistemas** (autorizador de cartão, Pix, crédito), não telas. BFF é do time de cada canal: um app que precise agregar autorização + extrato + ofertas cria o seu, consumindo esta API como qualquer cliente |

**Política de resiliência entre serviços fica na aplicação**, não no mesh:
- timeout de 1,5 s;
- retry com jitter **só porque a operação é idempotente** (ADR-0006);
- circuit breaker → 503.

O motivo é que essa política depende do significado da operação (idempotente ou não), e o proxy não sabe disso.

## Consequências

- **Positivas:**
  - Cada camada faz o que sabe fazer.
  - Não há plano de controle de mesh para operar.
  - O mesh do ECS é gerenciado e sai junto com o deploy.
- **Negativas:**
  - **mTLS entre serviços:** o Service Connect suporta TLS com AWS Private CA (custo da CA); no sandbox, a comunicação fica dentro da VPC com security groups. Ver `docs/compliance.md`.
  - **Service Connect é só ECS:** Lambda não participa. Por isso o reconciliador e o consumidor da ACL rodam como serviços ECS (ADR-0004).

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| Serviços em contas/VPCs diferentes, ou mistura EKS + ECS + Lambda chamando entre si | **VPC Lattice** (auth policies IAM entre serviços) |
| Plataforma padroniza EKS | Istio ou Linkerd, com mTLS e políticas no mesh; a política de retry sensível a idempotência continua na aplicação |
| Surgir um canal com UI própria consumindo vários BCs | BFF desse canal, mantido pelo time do canal |

## Referências

- App Mesh end of support: https://docs.aws.amazon.com/app-mesh/latest/userguide/what-is-app-mesh.html
- ECS Service Connect: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect.html
- VPC Lattice: https://docs.aws.amazon.com/vpc-lattice/latest/ug/what-is-vpc-lattice.html
- Sam Newman — Backends For Frontends: https://samnewman.io/patterns/architectural/bff/
