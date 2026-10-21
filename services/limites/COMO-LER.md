# Como ler o serviço `limites` (BC Gestão de Limites)

**Uma frase:** o saldo de crédito de cada contrato num item do DynamoDB, alterado só por **escritas condicionais atômicas** geradas a partir de regras do domínio.

## O caminho de uma RESERVA (siga nesta ordem)

1. **`adapters/inbound/http.py` → `aplicar_operacao`**
   - Lê o corpo **cru** e faz `json.loads(parse_float=Decimal)`. O FastAPI passaria o número por float, e 19.99 viraria 19.989999…
   - Valida com `OperacaoRequest` (pydantic): 400 se o JSON está malformado, 422 se viola o contrato.
   - Chama o caso de uso **no threadpool**, porque o boto3 é bloqueante.
2. **`application/casos_de_uso.py` → `AplicarOperacaoNoLimite.executar`:** uma linha. Injeta o relógio e chama a **porta** `AplicacaoDeOperacoes`. Não sabe o que é DynamoDB (DIP).
3. **`domain/operacoes.py` → `Reservar.planejar()`:** a regra, sem ler estado.
   - **Pré-condições:** contrato ATIVO, `disponivel ≥ valor`, hold com esse id ainda não existe e, se o chamador mandou `id_conta`, o contrato é dessa conta (senão `CONTA_DIVERGENTE`).
   - **Efeitos:** `reservado += valor`, cria o hold.
   - Cada operação é uma subclasse (OCP).
4. **`adapters/outbound/dynamodb.py` → `compilar(plano)`:** traduz o plano numa **única** `UpdateItem`.
   - `ConditionExpression`: `attribute_exists(id) AND attribute_not_exists(ops.#op) AND #status = :ativo AND #disponivel >= :disp_min AND attribute_not_exists(reservas.#rn)`
   - `UpdateExpression`: `SET versao = versao + 1, ops.#op = :agora, reservado = reservado + :dr, disponivel = disponivel + :dd, reservas.#rc = :v`
5. **`RepositorioDeLimitesDynamoDB.aplicar`**
   - **Sucesso:** `ReturnValues=ALL_NEW` traz os **saldos exatos** depois da escrita.
   - **Condição falhou:** `ReturnValuesOnConditionCheckFailure=ALL_OLD` traz o item **como estava**, e `Limite.diagnosticar()` (domínio) diz por quê: já aplicada (idempotência) ou recusada com o motivo.
6. **De volta ao HTTP:** 200 com `{"resultado": "APLICADA"|"JA_APLICADA"|"RECUSADA", "motivo"?, "saldos": {...}}`. Recusa **não** é erro HTTP na API interna.

## Por que isso aguenta 100 requisições simultâneas no mesmo contrato

- **Serialização pelo banco:** o DynamoDB serializa escritas de **item único**. Cada `UpdateItem` avalia a condição contra o estado **do momento da escrita**: as 10 primeiras passam e as 90 seguintes encontram `disponivel < 100`.
- **Sem conflito entre transações:** não há transação (ADR-0005), então nada se cancela.
- **Prova:** `tests/contract/test_porta_repositorio.py::test_corrida_de_100_reservas_aprova_exatamente_10`, rodando em memória **e** no DynamoDB.

## Idempotência (commit incerto)

- O mapa `ops` guarda os ids aplicados nos últimos 5 min (`JANELA_DE_DEDUPLICACAO`). A condição `attribute_not_exists(ops.#op)` faz o **retry do SDK depois de um timeout** falhar, e o diagnóstico devolve JA_APLICADA.
- O hold (`reservas.<id>`) é a guarda da RESERVA enquanto ela existir (até 7 dias), mesmo depois da compactação do `ops`.
- **Compactação de carona:** as entradas vencidas vistas na resposta de uma escrita (`Limite.operacoes_expiradas`) saem no `REMOVE` da escrita seguinte do mesmo contrato. Não há escrita extra. Cada `REMOVE` é condicionado (`ops.#xN < :corte`, ou a entrada já sumiu): o cache é por task, e outra task pode ter reaplicado o id. Se só essa condição falhar, a escrita repete sem compactar. Prova com duas tasks: `test_porta_repositorio.py::test_remove_de_carona_nunca_apaga_entrada_fresca_reaplicada_por_outra_task`.
- Escrita que deixa a guarda com 200 entradas ou mais emite uma linha JSON `guarda_de_idempotencia` no stdout, que alimenta a métrica `OperacoesNaGuarda` e o Contributor Insights (ADR-0016).

## Captura, estorno e expiração disputam o hold

- **`CapturarReserva`** (`domain/operacoes.py`, no HTTP `CAPTURAR_RESERVA` com `id_reserva`, `valor_reservado` e `valor`): pré-condição "o hold existe com esse `valor_reservado`"; efeitos `reservado −= valor_reservado`, `utilizado += valor` e remoção do hold. Captura parcial devolve a sobra ao disponível na mesma `UpdateItem`. Vale em contrato bloqueado depois do hold.
- **O hold é o árbitro (ADR-0020):** captura, `LIBERAR_RESERVA` (estorno e expiração) removem o hold. A primeira vence; as outras voltam `RECUSADA` com `RESERVA_NAO_ENCONTRADA`, não `JA_APLICADA`. A decisão está em `Limite.diagnosticar`.
- **Prova:** `tests/contract/test_porta_repositorio.py::test_captura_estorno_e_expiracao_disputam_o_hold_e_so_um_vence` (memória e DynamoDB).

## Quando o DynamoDB aperta

- Estrangulamento e queda de conexão (`ProvisionedThroughputExceeded`, `Throttling`, `RequestLimitExceeded`, `InternalServerError`, `ServiceUnavailable`, timeouts) viram `ArmazenamentoIndisponivel` no adapter, e o HTTP responde **503** `servico-indisponivel` com `Retry-After: 1`. Antes caía em 500. Teste: `tests/unit/test_dynamodb_resiliencia.py`.

## Invariantes

Estão em `domain/limite.py` (I1–I6), são verificadas por `verificar_invariantes()` e por **property-based tests**:

- `tests/property/test_limite_propriedades.py`: qualquer sequência de operações mantém as invariantes; qualquer ordem de reservas aprova exatamente ⌊L/v⌋.

## ACL do legado

- **`adapters/inbound/legado/traducao.py`:** layout de 84 posições → `AtualizacaoDoLegado`.
  - `id_contrato` cabe em 20 posições e `id_conta` em 16, com `[A-Za-z0-9-]`. `formatar` levanta `RegistroLegadoInvalido` em vez de truncar, e `traduzir` rejeita id fora do pattern (a mensagem vai para a DLQ). Valor máximo do layout: 999.999.999.999.999,99.
- **`domain/legado.py::sincronizar`:** a regra (versão obsoleta é ignorada; o disponível acompanha o concedido).
- **`RepositorioDeLimitesDynamoDB.sincronizar`:** CAS só nos campos do legado (concedido, status, conta, produto, versão), então não conflita com reservas.

## Perguntas prováveis e onde está a resposta

| Pergunta | Onde |
|---|---|
| "E se duas reservas chegarem no mesmo milissegundo?" | Seção acima + teste de corrida |
| "Por que não `TransactWriteItems` com versão, como o Itaú descreveu?" | ADR-0005, Alternativas |
| "Onde a regra de negócio mora, no domínio ou no banco?" | No domínio (`Plano`); o banco executa a forma compilada; a suíte de contrato prova que memória e DynamoDB se comportam igual (LSP) |
| "Qual o teto por contrato?" | ~8 op/s sustentadas no item único, porque a guarda de 5 min mora no item (conta no ADR-0016) |
| "E se o item crescer demais?" | Compactação de carona da guarda + alarme `OperacoesNaGuarda` + fragmentação (ADR-0016) |
| "Como se captura um hold?" | `CapturarReserva`; ADR-0020 |
| "O que acontece se captura e estorno chegarem juntos?" | O hold é o árbitro: um vence, o outro recebe `RESERVA_NAO_ENCONTRADA`. Teste de corrida no contrato da porta (20 capturas × 10 liberações → 1 aplicada) |
| "O DynamoDB estrangulou, e agora?" | 503 com `Retry-After: 1`, não 500; o canal repete com a mesma chave |
| "Um canal pode mexer no contrato de outra conta?" | Não, se mandar `id_conta`: a condição da escrita confere a conta da réplica e recusa com `CONTA_DIVERGENTE` (ADR-0005) |
| "Quem garante que a API interna bate com o contrato?" | `tests/contract/test_api_interna_bate_com_o_contrato.py`: enums do contrato = enums do domínio, e cada resposta validada no schema |
