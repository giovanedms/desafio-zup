.PHONY: infra demo compat carga setup lint fmt typecheck arch test test-integracao check up down logs _gate imagens deploy destroy zip tf-fmt tf-validate tf-test tf-checkov pacotes

# Instala todos os membros do workspace e o grupo dev.
setup:
	uv sync --all-packages

# Lint e verificação de formatação (sem alterar arquivos).
lint:
	uv run ruff check . && uv run ruff format --check .

# Formata e aplica correções automáticas do ruff.
fmt:
	uv run ruff format . && uv run ruff check --fix .

# Checagem de tipos (mypy strict) em todos os src/.
typecheck:
	uv run mypy services/*/src libs/*/src relay/src legado-mock/src

# Contratos de arquitetura hexagonal (import-linter).
arch:
	uv run lint-imports

# Testes unitários (integração fica fora por padrão).
test:
	uv run pytest

# Testes de integração (exigem LocalStack: make up).
test-integracao:
	uv run pytest -m integracao

# Gate completo local: o mesmo que a CI executa.
check: lint typecheck arch test

# Sobe o ambiente local (LocalStack) e espera ficar saudável.
infra:
	docker compose -f infra/local/docker-compose.yml up -d --wait localstack

# Sobe tudo: LocalStack → recursos + contratos de demo (idempotente) → serviços.
up: infra
	uv run python infra/local/bootstrap.py
	docker compose -f infra/local/docker-compose.yml build
	docker compose -f infra/local/docker-compose.yml up -d --wait

# Roteiro da demo ponta a ponta (exige `make up`): replay, corrida de 100 reservas, operações, validação.
demo:
	uv run python tests/e2e/demo.py

# Derruba o ambiente local e apaga os volumes.
down:
	docker compose -f infra/local/docker-compose.yml down -v

# Acompanha os logs do ambiente local.
logs:
	docker compose -f infra/local/docker-compose.yml logs -f

# Gate de segurança de deploy/destroy: CONFIRMO exato + conta AWS esperada (ACAO vem do alvo chamador).
_gate:
	@if [ "$$CONFIRMO" != "CONFIRMO: $(ACAO) na conta desafio-zup" ]; then \
	    echo "Bloqueado: $(ACAO) exige confirmação explícita e a conta esperada." >&2; \
	    echo "Uso: make $(ACAO) EXPECTED_ACCOUNT_ID=<id da conta> CONFIRMO=\"CONFIRMO: $(ACAO) na conta desafio-zup\"" >&2; \
	    exit 1; \
	fi
	@if [ -z "$$EXPECTED_ACCOUNT_ID" ]; then \
	    echo "Bloqueado: EXPECTED_ACCOUNT_ID é obrigatório (sem default)." >&2; \
	    exit 1; \
	fi
	@ACTUAL=$$(aws sts get-caller-identity --profile desafio-zup --query Account --output text) \
	    || { echo "Bloqueado: não foi possível consultar a conta AWS (profile desafio-zup)." >&2; exit 1; }; \
	if [ "$$ACTUAL" != "$$EXPECTED_ACCOUNT_ID" ]; then \
	    echo "Bloqueado: a conta AWS do profile desafio-zup difere de EXPECTED_ACCOUNT_ID." >&2; \
	    exit 1; \
	fi

# Imagens dos serviços: tag IMUTÁVEL no ECR. ARQ=amd64 → tasks X86_64; ARQ=arm64 → ARM64 (produção; exige buildx
# com emulação ou máquina ARM). O shell do operador exporta credenciais de outra conta: todo comando AWS as remove.
TAG ?= 1.0.4
ARQ ?= amd64
ARQ_ECS = $(if $(filter arm64,$(ARQ)),ARM64,X86_64)
REGISTRO = $(EXPECTED_ACCOUNT_ID).dkr.ecr.sa-east-1.amazonaws.com
SEM_CRED = env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN
TF_VARS = -var account_id=$(EXPECTED_ACCOUNT_ID) -var tag_imagem_limites=$(TAG) -var tag_imagem_autorizacao=$(TAG) -var arquitetura_cpu=$(ARQ_ECS)

# Constrói e publica limites e autorizacao no ECR. Tag que já existe é pulada (imutável: republicar falharia).
imagens:
	@test -n "$(EXPECTED_ACCOUNT_ID)" || { echo "Uso: make imagens EXPECTED_ACCOUNT_ID=<id> [TAG=$(TAG)] [ARQ=amd64|arm64]" >&2; exit 1; }
	$(SEM_CRED) aws ecr get-login-password --profile desafio-zup --region sa-east-1 | docker login --username AWS --password-stdin $(REGISTRO)
	@for s in limites autorizacao; do \
	    if $(SEM_CRED) aws ecr describe-images --profile desafio-zup --region sa-east-1 --repository-name desafio-zup/$$s \
	        --image-ids imageTag=$(TAG) >/dev/null 2>&1; then echo "desafio-zup/$$s:$(TAG) já existe; pulando"; continue; fi; \
	    docker buildx build --platform linux/$(ARQ) -f services/$$s/Dockerfile -t $(REGISTRO)/desafio-zup/$$s:$(TAG) --push . || exit 1; \
	done

# Deploy na conta desafio-zup (protegido por _gate). Ordem: repositórios ECR → imagens → todo o resto (as tasks ECS
# precisam da imagem para subir). O bucket do state vem por -backend-config (backend.tf é parcial).
deploy: ACAO = deploy
deploy: _gate pacotes
	$(SEM_CRED) terraform -chdir=$(TF_ENV) init -backend-config="bucket=desafio-zup-tfstate-$(EXPECTED_ACCOUNT_ID)"
	$(SEM_CRED) terraform -chdir=$(TF_ENV) apply $(TF_VARS) -target=module.ecr
	$(MAKE) imagens EXPECTED_ACCOUNT_ID=$(EXPECTED_ACCOUNT_ID) TAG=$(TAG) ARQ=$(ARQ)
	$(SEM_CRED) terraform -chdir=$(TF_ENV) apply $(TF_VARS)

# Destroy na conta desafio-zup (protegido por _gate).
destroy: ACAO = destroy
destroy: _gate
	$(SEM_CRED) terraform -chdir=$(TF_ENV) init -backend-config="bucket=desafio-zup-tfstate-$(EXPECTED_ACCOUNT_ID)" \
	    && $(SEM_CRED) terraform -chdir=$(TF_ENV) destroy $(TF_VARS)

# Gera o ZIP de entrega a partir do HEAD (exige ao menos um commit; arquivos não commitados ficam de fora).
zip:
	git archive --format=zip -o ../entrega-desafio-zup.zip HEAD

# Ambiente Terraform do sandbox (alvos tf-*). Nenhum deles fala com a AWS: init sem backend e credenciais removidas.
# Diretório de dados próprio: o .terraform de quem já fez deploy guarda o backend S3, e o validate o leria.
TF_ENV = infra/terraform/environments/sandbox
TF_SEM_AWS = env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN TF_DATA_DIR=.terraform-offline

# Verifica a formatação do HCL (não altera arquivos).
tf-fmt:
	terraform fmt -check -recursive infra/terraform

# Valida os ambientes sandbox e esteira (init sem backend, sem tocar na AWS).
tf-validate:
	@for env in sandbox esteira; do \
	    $(TF_SEM_AWS) terraform -chdir=infra/terraform/environments/$$env init -backend=false -input=false >/dev/null \
	        && $(TF_SEM_AWS) terraform -chdir=infra/terraform/environments/$$env validate || exit 1; \
	done

# Testes do Terraform com mock_provider (plan apenas; nenhuma chamada à AWS). Os testes moram num diretório REAL dentro do
# ambiente (o Terraform 1.16 recusa -test-directory fora da configuração). Antes era um symlink, que vira arquivo de texto
# no unzip do Windows e fazia o `terraform test` passar sem achar teste nenhum: o grep falha o alvo se nenhum run rodar.
tf-test: tf-validate
	$(TF_SEM_AWS) terraform -chdir=$(TF_ENV) test -test-directory=tests | tee /dev/stderr | grep -Eq '[1-9][0-9]* passed, 0 failed'

# Análise estática de IaC com Checkov.
tf-checkov:
	uvx checkov -d infra/terraform --quiet --compact

# Gera os zips das Lambdas (arm64, py3.13) em infra/terraform/build/.
pacotes:
	bash scripts/empacotar_lambdas.sh

# Gate de compatibilidade de contratos (schemas de evento + OpenAPI) contra a base git (ADR-0008).
compat:
	scripts/compat_contratos.sh $(or $(BASE),origin/main)

# Carga k6 (exige make up): make carga CENARIO=vazao RPS=20 DURACAO=10s
carga:
	k6 run -e CENARIO=$(or $(CENARIO),vazao) $(foreach v,CONTRATO LIMITE VALOR VUS RPS DURACAO P99_MS BASE_URL,$(if $($(v)),-e $(v)=$($(v)))) tests/load/autorizacao.js
