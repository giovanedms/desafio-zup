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
2. **ACL de entrada, assíncrona.** O legado publica registros de tamanho fixo numa fila (`legado-contratos`). A ACL (`limites/adapters/inbound/legado/traducao.py`) traduz o layout de 84 posições (decimal implícito, códigos, fuso local) para o comando de domínio `AtualizacaoDoLegado`. **Nada do formato legado passa da ACL.**
3. **Ordem e idempotência pela versão do registro.**
   - Versão menor ou igual à já aplicada → **OBSOLETA**, ignorada. O feed pode reordenar e repetir: um property test prova que qualquer ordem converge para a maior versão.
   - Contrato desconhecido → **CRIADO**, o caminho de contrato novo. A carga inicial de 240–400 M contratos não passa por aqui (item 8).
4. **Sem interferência no item quente.** A sincronização é um compare-and-set **só** nos atributos do legado (`concedido`, `status`, `versao_legado`). As reservas mexem em `utilizado`, `reservado` e `disponivel` com expressões comutativas, então as duas coisas convivem sem transação nem lock (teste de corrida com reservas + atualizações do legado em paralelo).
5. **Redução de limite abaixo do comprometido é aceita.** A decisão de crédito do legado prevalece: o disponível fica negativo e **nenhuma reserva nova passa** até regularizar.
6. **`LimiteAlterado` por CDC.** O relay lê o Stream da tabela `limites` e publica só quando concedido ou status mudam (não a cada reserva). Assim nenhuma transação toca o item quente para gravar outbox.
7. **ACL de saída** (contabilização → legado contábil): os lançamentos ficam disponíveis por evento e tabela. A exportação no formato do legado (arquivo posicional) é um adapter de saída do BC Contabilização, desenhado e não implementado no prazo.
8. **Carga inicial e reconciliação noturna** (desenhadas):
   - carga inicial por **DynamoDB import from S3**: o arquivo do legado passa em lote pelo mesmo tradutor e o import cria a tabela já carregada. O feed fica só com os deltas: na task de hoje (0,25 vCPU, em série), 240–400 M registros levariam ~51 a ~85 dias (`docs/capacidade.md`, seção 1.5);
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
  - **O `LimiteAlterado` é montado fora do BC Limites**, no `relay/` (`relay/src/relay/cdc_limites.py`), a partir do diff do item no Stream. O preço: o contrato publicado do BC mora no relay, que conhece os nomes dos atributos do item (`concedido`, `status`, `disponivel`, `versao`, `versao_legado`); evoluir o evento ou renomear um atributo exige mexer lá também. É a derivação por diff de estado que o ADR-0007 recusa para a Autorização, aceita aqui porque nenhuma transação toca o item quente.

## Quando esta decisão deixa de ser boa

| Gatilho | Mudança |
|---|---|
| O negócio exige que a redução de limite tenha efeito **imediato** (bloqueio por fraude) | Canal prioritário de bloqueio: comando síncrono do legado para a API interna, ou status de bloqueio publicado pelo antifraude, com SLA próprio |
| O `LimiteAlterado` precisa de dado que não está no item, ou de evoluir no ritmo do BC Limites | Outbox próprio do `limites`: o resultado ATUALIZADO da sincronização grava o evento na mesma transação do item (só quando o legado muda concedido ou status, nunca nas reservas) e o CDC do relay sai |
| Fim da migração (o legado deixa de ser dono do concedido) | O BC Limites vira dono; a ACL de entrada é desligada e a de saída passa a alimentar o legado residual |

## Referências

- Evans — Anticorruption Layer (DDD Reference 2015): https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf
- Fowler — Strangler Fig Application: https://martinfowler.com/bliki/StranglerFigApplication.html
