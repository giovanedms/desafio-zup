# ADR-0014 — Anticorruption Layer assíncrona com o core legado e autoridade de dados por atributo

- **Status:** Aceito
- **Data:** 2026-10-20
- **Decisores:** Giovane de Morais
- **Requisitos atendidos:** RN-05 (e RN-07)

## Contexto

O core banking legado (mainframe) é a fonte da verdade de contratos e limites concedidos. A nova plataforma precisa do limite em tempo real **sem** chamar o legado no caminho síncrono: um legado lento ou fora do ar não pode derrubar a autorização. Durante a migração (strangler fig) os dois sistemas coexistem, e cada atributo precisa de **um** dono.

## Decisão

1. **Autoridade por atributo:**

   | Atributo | Dono |
   |---|---|
   | Existência do contrato, conta, produto (crédito, investimento, seguro, consórcio), **limite concedido**, **status** | **Legado** |
   | **Utilizado, reservado e reservas** (saldo em tempo real) | **BC Limites** |

   A sincronização do legado **nunca** toca utilizado ou reservado. O disponível acompanha a variação do concedido.

   Conta e produto são aplicados quando mudam (portabilidade de conta, reclassificação de produto): versão nova com conta ou produto diferente é `ATUALIZADO`, não `SEM_MUDANCA`. Antes a réplica os congelava no primeiro registro (painel 6, F-4). A conta da réplica é a que a API interna confere quando o chamador manda `id_conta` (`CONTA_DIVERGENTE`, ADR-0005). Consequência aceita: depois de uma portabilidade, captura ou liberação de um hold feito na conta antiga, enviada com a conta antiga, é recusada; a compensação de sistema (expiração, reconciliação), que não vem de um pedido do canal, pode omitir o `id_conta`.
2. **ACL de entrada, assíncrona.** O legado publica registros de tamanho fixo numa fila (`legado-contratos`). A ACL (`limites/adapters/inbound/legado/traducao.py`) traduz o layout de 84 posições (decimal implícito, códigos, fuso local) para o comando de domínio `AtualizacaoDoLegado`. **Nada do formato legado passa da ACL.**
3. **Ordem e idempotência pela versão do registro.**
   - Versão menor ou igual à já aplicada → **OBSOLETA**, ignorada. O feed pode reordenar e repetir: um property test prova que qualquer ordem converge para a maior versão.
   - Contrato desconhecido → **CRIADO**, que também é o caminho da carga inicial.
4. **Sem interferência no item quente.** A sincronização é um compare-and-set **só** nos atributos do legado (`concedido`, `status`, `id_conta`, `tipo_produto`, `versao_legado`). As reservas mexem em `utilizado`, `reservado` e `disponivel` com expressões comutativas, então as duas coisas convivem sem transação nem lock (testes de corrida com reservas + atualizações do legado em paralelo, inclusive trocando o produto). A reserva que confere a conta lê `id_conta` na condição: se a troca de conta for serializada antes dela, ela é recusada com `CONTA_DIVERGENTE`, que é a ordem certa.
5. **Redução de limite abaixo do comprometido é aceita.** A decisão de crédito do legado prevalece: o disponível fica negativo e **nenhuma reserva nova passa** até regularizar.
6. **`LimiteAlterado` por CDC.** O relay lê o Stream da tabela `limites` e publica só quando concedido ou status mudam (não a cada reserva). Assim nenhuma transação toca o item quente para gravar outbox. Troca só de conta ou de produto atualiza a réplica e não publica evento: o `LimiteAlterado` v1 não carrega esses campos.
7. **ACL de saída** (contabilização → legado contábil): os lançamentos ficam disponíveis por evento e tabela. A exportação no formato do legado (arquivo posicional) é um adapter de saída do BC Contabilização, desenhado e não implementado no prazo.
8. **Carga inicial e reconciliação noturna** (desenhadas):
   - carga em lote do arquivo do legado pelo mesmo tradutor;
   - conciliação diária concedido × legado, com divergência → alarme.
9. **Defasagem:**
   - alarme `defasagem-do-legado`: `ApproximateAgeOfOldestMessage` da fila `legado-contratos` acima de **300 s** (`modules/observabilidade`, `var.defasagem_legado_limite_s`), com ação no tópico SNS de alarmes. Também há o widget de idade da mensagem mais antiga no dashboard (agora com a fila do legado) e o alarme da DLQ do feed;
   - contrato ausente na réplica → **fail-closed** (404, nada autorizado).
10. **Simulação:** `legado-mock` gera registros pelo layout **sem** importar o código da ACL (ele é "o legado"). Um teste de contrato prova que todo registro do mock é traduzido corretamente.

## Consequências

- **Positivas:**
  - O caminho síncrono não depende do legado.
  - O legado pode reordenar e repetir sem efeito colateral.
  - A migração é incremental (strangler), com dono claro por dado.
- **Negativas:**
  - O limite concedido é **eventualmente consistente** com o legado (segundos).
  - Uma redução de limite pode chegar depois de uma autorização feita com o limite antigo. Esse risco é aceito e limitado pela defasagem monitorada: o alarme dispara quando a réplica fica mais de 5 min atrás do feed.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| O negócio exige que a redução de limite tenha efeito **imediato** (bloqueio por fraude) | Canal prioritário de bloqueio: comando síncrono do legado para a API interna, ou status de bloqueio publicado pelo antifraude, com SLA próprio |
| Fim da migração (o legado deixa de ser dono do concedido) | O BC Limites vira dono; a ACL de entrada é desligada e a de saída passa a alimentar o legado residual |

## Referências

- Evans — Anticorruption Layer (DDD Reference 2015): https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf
- Fowler — Strangler Fig Application: https://martinfowler.com/bliki/StranglerFigApplication.html
