# Compliance — o que o desenho suporta (sem exagero jurídico)

> Mapeamento técnico de requisitos normativos para mecanismos da solução. Não é parecer jurídico. A aderência formal depende das políticas do banco (segurança cibernética, retenção, terceirização).
> Fontes primárias consultadas em 19/10/2026: `01-pesquisa/03-padroes-normas-e-dominio.md` §11.

| Norma | O que exige (resumo) | Mecanismo na solução | Onde |
|---|---|---|---|
| **Res. CMN 4.893/2021**, alterada pela **CMN 5.274/2025** (adaptação até 01/03/2026) | Política de segurança cibernética. Rastreabilidade fim a fim e trilhas de auditoria com retenção definida (art. 3º, §7º, na redação nova). Requisitos de segurança em integrações por API. Contratação de nuvem com comunicação ao BCB e acesso do supervisor aos dados (arts. 15–17) | `correlation_id` + `traceparent` + `id_autorizacao` do HTTP ao lançamento, e o `requestId` do gateway repassado ao serviço (`X-Gateway-Request-Id`) para juntar a borda ao serviço; eventos imutáveis com archive; trilha WORM (S3 Object Lock); OAuth2 com escopo, WAF por consumidor e por IP, validação dos parâmetros na borda e do corpo no serviço; região sa-east-1 como premissa e DR fora do país sinalizado como gatilho dos arts. 15–17 | ADR-0017, ADR-0010, ADR-0001, ADR-0013 |
| **Circular BCB 3.978/2020** (PLD/FT) | Registro de **todas** as operações, inclusive o canal, guardado por **10 anos** (arts. 28 e 67) | Toda decisão vira evento, **inclusive recusas** (`TransacaoRecusada`), com canal em `metadata`; retenção de 10 anos em S3 Object Lock (produção) | ADR-0008, ADR-0017 |
| **Res. BCB 85/2021** (instituições de pagamento), alterada pelas BCB 538/2025 e 552/2026 | Segurança cibernética e nuvem para IPs | Mesmos mecanismos da 4.893 | — |
| **LGPD (Lei 13.709/2018)** | Minimização, finalidade, segurança; retenção por obrigação legal (art. 16, I) | Eventos, logs e traces só com identificadores pseudonimizados (`id_conta`, `id_contrato`), sem nome, CPF ou geo preciso; `metadata` com allowlist (UF e município IBGE); retenção da trilha justificada pela Circular 3.978 | ADR-0011, ADR-0017 |
| **Res. ANPD 15/2024** | Registro de incidentes por 5 anos | CloudTrail com bucket WORM (criado no bootstrap da conta, fora do Terraform do repositório); alarmes com notificação num tópico SNS criptografado (KMS do cliente); trilha de eventos | ADR-0017 |

## Pontos de atenção (declarados)

- **Evento imutável × direito de eliminação (LGPD).** Os eventos carregam só identificadores pseudonimizados, e a retenção é amparada por obrigação legal. A eliminação de dados pessoais acontece nos sistemas que mapeiam identificador → pessoa, não na trilha.
- **DR fora do Brasil.** Ativa as obrigações de comunicação e convênio dos arts. 15–17 da 4.893.
- **mTLS entre serviços.** Não está ligado no sandbox. Na produção: TLS do Service Connect com AWS Private CA.
- **Egress das tasks para 0.0.0.0/0 na porta 443.** O sandbox não tem NAT e só tem os VPC endpoints do tipo gateway (DynamoDB e S3): DynamoDB e as camadas das imagens do ECR, que vêm do S3, não passam pela internet. As demais APIs (ECR API, CloudWatch Logs, SQS) são alcançadas pelos endpoints públicos, com o IP público da task. A regra é só 443 e está suprimida no Trivy (`AWS-0104`) com justificativa no próprio Terraform. Na produção: VPC endpoints (gateway para DynamoDB/S3, interface para ECR/Logs/STS/SQS/EventBridge) e egress restrito aos prefix lists deles.
- **Role de deploy da esteira com `PowerUserAccess`.** IAM fica fora (delegada só com permissions boundary obrigatório) e a role só é assumível pela branch `main` do repositório via OIDC. Na produção: política gerada pelo IAM Access Analyzer a partir do CloudTrail das execuções da esteira.
