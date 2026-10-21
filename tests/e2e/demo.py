"""Roteiro da demo ponta a ponta contra o ambiente local (`make up` antes; `make demo` roda isto).

Cada execução cria contratos NOVOS (simulando a carga vinda do legado), então dá para repetir à vontade.
Também é o teste ponta a ponta da esteira: cada seção confere o que mostra, e qualquer divergência sai com código 1.
Fala com as APIs HTTP públicas/internas — não importa nada dos serviços além do bootstrap de dados. O que não
tem API (o lançamento contábil, as filas sem consumidor local) é lido direto na LocalStack.
Como um canal correto, a demo repete 503 (load shedding) e 429 com a MESMA Idempotency-Key depois do Retry-After:
o resultado não depende do teto de requisições em voo (`MAX_EM_VOO`) do ambiente.
Todo pedido usa a conta do contrato (conta divergente é recusada com CONTA_DIVERGENTE).
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

sys.path.insert(0, "infra/local")
from bootstrap import CONTRATOS_DE_DEMO, cliente
from limites.adapters.outbound.dynamodb import RepositorioDeLimitesDynamoDB
from limites.domain.limite import Limite, TipoProduto

AUTORIZACAO = "http://127.0.0.1:18080"
LIMITES = "http://127.0.0.1:18081"
CANAL = "canal-demo"
CONTA = "0001-99999999-9"  # conta dos contratos que a demo cria
TENTATIVAS = 10  # 503/429 repetidos por pedido antes de desistir (como tests/e2e/aws.py)
COMPOSE = ["docker", "compose", "-f", "infra/local/docker-compose.yml"]


DIVERGENCIAS: list[str] = []
REPETIDAS: Counter[int] = Counter()  # 503/429 repetidos pelo "canal", por status
_TRAVA_DAS_REPETIDAS = threading.Lock()


def confere(ok: bool, o_que: str) -> None:
    """Registra a divergência (a demo continua, para mostrar tudo) e marca a linha com ❌."""
    if not ok:
        DIVERGENCIAS.append(o_que)
        print(f"❌ {o_que}")


def titulo(texto: str) -> None:
    print(f"\n\033[1m── {texto} ──\033[0m")


def novo_contrato(concedido: str) -> str:
    id_contrato = f"CTR-DEMO-{uuid.uuid4().hex[:8].upper()}"
    RepositorioDeLimitesDynamoDB(cliente("dynamodb"), "limites").criar(
        Limite.novo(id_contrato, CONTA, TipoProduto.CREDITO, Decimal(concedido))
    )
    return id_contrato


def _espera(resposta: httpx.Response) -> float:
    try:
        return max(0.0, float(resposta.headers.get("Retry-After", "1")))
    except ValueError:  # Retry-After em data HTTP: espera o mínimo
        return 1.0


def autorizar(
    http: httpx.Client, id_contrato: str, corpo: dict[str, Any], chave: str | None = None
) -> tuple[httpx.Response, float]:
    """POST como um canal correto: 503 (load shedding) e 429 são "tente de novo", com a MESMA Idempotency-Key
    depois do Retry-After. A latência devolvida inclui as repetições (é o que o canal vive)."""
    chave = chave or str(uuid.uuid4())
    inicio = time.perf_counter()
    for _ in range(TENTATIVAS):
        resposta = http.post(
            f"{AUTORIZACAO}/v1/contratos/{id_contrato}/autorizacoes",
            json=corpo,
            headers={"Idempotency-Key": chave, "X-Client-Id": CANAL},
        )
        if resposta.status_code not in (429, 503):
            break
        with _TRAVA_DAS_REPETIDAS:
            REPETIDAS[resposta.status_code] += 1
        time.sleep(_espera(resposta))
    return resposta, (time.perf_counter() - inicio) * 1000


def limite(http: httpx.Client, id_contrato: str) -> dict[str, Any]:
    return dict(http.get(f"{LIMITES}/interno/v1/contratos/{id_contrato}/limite").json())


def conta_semeada(id_contrato: str) -> str:
    """Conta de um contrato semeado pelo bootstrap (a mesma fonte que o criou)."""
    return next(conta for contrato, conta, *_ in CONTRATOS_DE_DEMO if contrato == id_contrato)


def reserva(valor: str = "100.00", conta: str = CONTA) -> dict[str, Any]:
    return {
        "id_conta": conta,
        "valor": float(valor),
        "moeda": "BRL",
        "tipo_operacao": "RESERVA",
    }


def demo_aprovacao_e_replay(http: httpx.Client) -> None:
    titulo("1. Aprovação, replay idempotente e chave reutilizada")
    id_contrato = novo_contrato("1000.00")
    chave = str(uuid.uuid4())
    primeira, _ = autorizar(http, id_contrato, reserva("150.00"), chave)
    print(
        f"POST (1ª)          → {primeira.status_code}  saldo_reservado={primeira.json()['saldo_reservado']}"
    )
    replay, _ = autorizar(http, id_contrato, reserva("150.00"), chave)
    identico = replay.content == primeira.content
    print(
        f"POST (mesma chave) → {replay.status_code}  "
        f"Idempotent-Replayed={replay.headers.get('Idempotent-Replayed')}  corpo idêntico={identico}"
    )
    reuso, _ = autorizar(http, id_contrato, reserva("151.00"), chave)
    print(f"POST (chave, outro corpo) → {reuso.status_code}  {reuso.json()['codigo']}")
    confere(primeira.status_code == 201, "1ª autorização deveria ser 201")
    confere(replay.status_code == 200 and identico, "replay deveria ser 200 com corpo idêntico")
    confere(reuso.status_code == 422, "chave reutilizada com outro corpo deveria ser 422")


def demo_corrida(http: httpx.Client, n: int = 100, clientes: int = 50) -> None:
    titulo(
        f"2. Corrida: {n} reservas de R$ 100, {clientes} clientes simultâneos, num limite de R$ 1.000"
    )
    id_contrato = novo_contrato("1000.00")
    antes = Counter(REPETIDAS)
    with ThreadPoolExecutor(max_workers=clientes) as pool:
        resultados = list(pool.map(lambda _: autorizar(http, id_contrato, reserva()), range(n)))
    if repetidas := REPETIDAS - antes:
        print(
            f"repetidas pelo canal (load shedding/cota, mesma chave): {dict(sorted(repetidas.items()))}"
        )
    status = Counter(r.status_code for r, _ in resultados)
    latencias = sorted(ms for _, ms in resultados)
    final = limite(http, id_contrato)
    print(f"status: {dict(sorted(status.items()))}   (esperado: {{201: 10, 402: {n - 10}}})")
    print(
        f"limite final: disponível={final['limite_disponivel']}  "
        f"reservado={final['saldo_reservado']}  versão={final['versao']}"
    )
    p = statistics.quantiles(latencias, n=100)
    print(
        f"latência local (ms, 50 clientes; LocalStack — não é referência): "
        f"p50={p[49]:.0f}  p95={p[94]:.0f}  p99={p[98]:.0f}"
    )
    ok = status == {201: 10, 402: n - 10} and Decimal(str(final["limite_disponivel"])) == 0
    print("✅ invariante preservada" if ok else "")
    confere(ok, "corrida: esperado {201: 10, 402: 90} e disponível 0")


def demo_operacoes(http: httpx.Client) -> None:
    titulo("3. DEBITO, CREDITO, ESTORNO e contrato bloqueado")
    id_contrato = novo_contrato("1000.00")
    debito, _ = autorizar(http, id_contrato, {**reserva("300.00"), "tipo_operacao": "DEBITO"})
    print(f"DEBITO 300   → {debito.status_code}  disponível={debito.json()['limite_disponivel']}")
    estorno_corpo = {
        **reserva("300.00"),
        "tipo_operacao": "ESTORNO",
        "id_autorizacao_original": debito.json()["id_autorizacao"],
    }
    estorno, _ = autorizar(http, id_contrato, estorno_corpo)
    print(f"ESTORNO 300  → {estorno.status_code}  disponível={estorno.json()['limite_disponivel']}")
    de_novo, _ = autorizar(http, id_contrato, estorno_corpo)
    print(f"ESTORNO de novo (outra chave) → {de_novo.status_code}  {de_novo.json()['codigo']}")
    debito2, _ = autorizar(http, id_contrato, {**reserva("200.00"), "tipo_operacao": "DEBITO"})
    credito, _ = autorizar(http, id_contrato, {**reserva("100.00"), "tipo_operacao": "CREDITO"})
    print(
        f"DEBITO 200 → {debito2.status_code}; CREDITO 100 (pagamento) → {credito.status_code}  "
        f"disponível={credito.json()['limite_disponivel']}"
    )
    bloqueado, _ = autorizar(
        http, "CTR-DEMO-0003", reserva("1.00", conta=conta_semeada("CTR-DEMO-0003"))
    )
    print(f"contrato BLOQUEADO → {bloqueado.status_code}  {bloqueado.json()['codigo']}")
    inexistente, _ = autorizar(http, "CTR-NAO-EXISTE", reserva("1.00"))
    print(f"contrato inexistente → {inexistente.status_code}  {inexistente.json()['codigo']}")
    esperado = [(debito, 201), (estorno, 201), (de_novo, 409), (debito2, 201), (credito, 201)]
    confere(
        all(r.status_code == st for r, st in esperado), "débito/estorno/crédito fora do esperado"
    )
    confere(bloqueado.status_code == 422, "contrato bloqueado deveria ser 422")
    confere(inexistente.status_code == 404, "contrato inexistente deveria ser 404")


def demo_captura(http: httpx.Client) -> None:
    titulo("4. Captura de reserva (DEBITO que referencia a RESERVA) e disputa pelo hold")
    id_contrato = novo_contrato("1000.00")
    hold, _ = autorizar(http, id_contrato, reserva("400.00"))
    id_reserva = hold.json()["id_autorizacao"]
    print(
        f"RESERVA 400            → {hold.status_code}  disponível={hold.json()['limite_disponivel']}"
    )
    captura = {
        **reserva("250.00"),
        "tipo_operacao": "DEBITO",
        "id_autorizacao_original": id_reserva,
    }
    capturada, _ = autorizar(http, id_contrato, captura)
    print(
        f"CAPTURA parcial 250    → {capturada.status_code}  "
        f"disponível={capturada.json()['limite_disponivel']} (a sobra de 150 voltou)  "
        f"utilizado={capturada.json()['limite_utilizado']}"
    )
    de_novo, _ = autorizar(http, id_contrato, captura)
    estorno = {
        **reserva("400.00"),
        "tipo_operacao": "ESTORNO",
        "id_autorizacao_original": id_reserva,
    }
    tarde, _ = autorizar(http, id_contrato, estorno)
    print(
        f"capturar de novo → {de_novo.status_code}; estornar a reserva capturada → {tarde.status_code}"
    )
    confere(capturada.status_code == 201, "captura parcial deveria ser 201")
    confere(
        de_novo.status_code == 409 and tarde.status_code == 409, "recaptura e estorno tardio: 409"
    )

    disputada, _ = autorizar(http, id_contrato, reserva("300.00"))
    id_disputada = disputada.json()["id_autorizacao"]
    pedidos = [
        {**reserva("300.00"), "tipo_operacao": "DEBITO", "id_autorizacao_original": id_disputada}
    ] * 10 + [
        {**reserva("300.00"), "tipo_operacao": "ESTORNO", "id_autorizacao_original": id_disputada}
    ] * 10
    with ThreadPoolExecutor(max_workers=20) as pool:
        respostas = list(pool.map(lambda corpo: autorizar(http, id_contrato, corpo)[0], pedidos))
    status = Counter(r.status_code for r in respostas)
    final = limite(http, id_contrato)
    print(
        f"10 capturas × 10 estornos do mesmo hold em paralelo → {dict(sorted(status.items()))}  "
        f"(esperado: {{201: 1, 409: 19}})  reservado final={final['saldo_reservado']}"
    )
    ok = status == {201: 1, 409: 19} and Decimal(str(final["saldo_reservado"])) == 0
    print("✅ um vencedor, 19 conflitos, hold consumido uma vez" if ok else "")
    confere(ok, "disputa pelo hold: esperado {201: 1, 409: 19} e reservado 0")


def demo_validacao(http: httpx.Client) -> None:
    titulo("5. Contrato da API: validação e problem+json (RFC 9457)")
    id_contrato = novo_contrato("1000.00")
    sem_chave = http.post(
        f"{AUTORIZACAO}/v1/contratos/{id_contrato}/autorizacoes",
        json=reserva(),
        headers={"X-Client-Id": CANAL},
    )
    print(
        f"sem Idempotency-Key → {sem_chave.status_code}  "
        f"{sem_chave.headers['content-type']}  {sem_chave.json()['codigo']}"
    )
    tres_casas = http.post(
        f"{AUTORIZACAO}/v1/contratos/{id_contrato}/autorizacoes",
        content=f'{{"id_conta":"{CONTA}","valor":10.001,"moeda":"BRL","tipo_operacao":"DEBITO"}}'.encode(),
        headers={
            "Idempotency-Key": str(uuid.uuid4()),
            "X-Client-Id": CANAL,
            "Content-Type": "application/json",
        },
    )
    print(f"valor com 3 casas   → {tres_casas.status_code}  {tres_casas.json()['codigo']}")
    grande = novo_contrato("99999999.00")  # contrato novo: a demo pode rodar quantas vezes quiser
    exato = http.post(
        f"{AUTORIZACAO}/v1/contratos/{grande}/autorizacoes",
        content=f'{{"id_conta":"{CONTA}","valor":12345678.91,"moeda":"BRL","tipo_operacao":"DEBITO"}}'.encode(),
        headers={
            "Idempotency-Key": str(uuid.uuid4()),
            "X-Client-Id": CANAL,
            "Content-Type": "application/json",
        },
    )
    print(
        f"valor exato         → {exato.status_code}  corpo contém 12345678.91: {b'12345678.91' in exato.content}"
    )
    confere(
        sem_chave.status_code == 400
        and sem_chave.headers["content-type"] == "application/problem+json",
        "sem Idempotency-Key deveria ser 400 problem+json",
    )
    confere(tres_casas.status_code == 422, "valor com 3 casas deveria ser 422")
    confere(
        exato.status_code == 201 and b"12345678.91" in exato.content, "valor exato não preservado"
    )


def aguardar(condicao: Any, segundos: float = 15.0) -> Any:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        resultado = condicao()
        if resultado:
            return resultado
        time.sleep(0.5)
    return None


def receber_da_fila(
    nome: str, achou: Callable[[dict[str, Any]], bool], segundos: float = 60.0
) -> dict[str, Any] | None:
    """Drena a fila até achar a mensagem que `achou` reconhece e devolve o envelope do EventBridge.

    Só para filas SEM consumidor no ambiente local (antifraude, notificacoes): a demo apaga o que lê, então
    o backlog de execuções anteriores sai no caminho. Não depende de a fila estar vazia nem da ordem
    (SQS Standard); o prazo cobre drenar esse backlog na primeira execução.
    """
    sqs = cliente("sqs")
    url = sqs.get_queue_url(QueueName=nome)["QueueUrl"]
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        mensagens = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1, VisibilityTimeout=30
        ).get("Messages", [])
        if mensagens:
            sqs.delete_message_batch(
                QueueUrl=url,
                Entries=[
                    {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]}
                    for i, m in enumerate(mensagens)
                ],
            )
        for mensagem in mensagens:
            envelope = json.loads(mensagem["Body"], parse_float=Decimal)
            if achou(envelope):
                return dict(envelope)
    return None


def demo_eventos(http: httpx.Client) -> None:
    titulo("6. Evento → outbox → relay → EventBridge → fila → contabilização (partida dobrada)")
    id_contrato = novo_contrato("1000.00")
    debito, _ = autorizar(http, id_contrato, {**reserva("250.00"), "tipo_operacao": "DEBITO"})
    id_aut = debito.json()["id_autorizacao"]
    print(f"DEBITO 250 → {debito.status_code}  id_autorizacao={id_aut}")
    dynamodb = cliente("dynamodb")

    def lancamento() -> dict[str, Any] | None:
        itens = dynamodb.scan(
            TableName="contabilizacao-lancamentos",
            FilterExpression="contains(historico, :aut)",
            ExpressionAttributeValues={":aut": {"S": id_aut}},
        )["Items"]
        return itens[0] if itens else None

    item = aguardar(lancamento)
    if not item:
        confere(False, "lançamento não chegou em 15 s (relay/contabilização de pé?)")
        return
    print(f"lançamento: {item['historico']['S']}  data contábil={item['data_contabil']['S']}")
    for partida in item["partidas"]["L"]:
        m = partida["M"]
        print(f"   {m['natureza']['S']}  {m['conta']['S']:<6} {m['valor']['N']}")
    print("✅ o fato atravessou o outbox, o bus e a fila e foi contabilizado uma vez (inbox)")

    # Fan-out: a regra do bus entrega a MESMA TransacaoAutorizada em antifraude e notificacoes (sem
    # consumidor local). A fila da contabilização não é lida aqui: ela tem consumidor (corrida); a prova
    # dela é o lançamento acima, que guarda o id do evento de origem.
    id_contabilizado = item["id_evento_origem"]["S"]

    def desta_autorizacao(envelope: dict[str, Any]) -> bool:
        detail = envelope.get("detail") or {}
        return bool(
            envelope.get("detail-type") == "TransacaoAutorizada"
            and (detail.get("data") or {}).get("id_autorizacao") == id_aut
        )

    ids = {
        fila: receber_da_fila(fila, desta_autorizacao) for fila in ("antifraude", "notificacoes")
    }
    for fila, envelope in ids.items():
        print(f"   {fila:<14} → {envelope['detail']['id'] if envelope else 'NÃO CHEGOU'}")
    print(f"   contabilizacao → {id_contabilizado} (id_evento_origem do lançamento)")
    mesmos = {e["detail"]["id"] if e else None for e in ids.values()} == {id_contabilizado}
    print(
        "✅ fan-out: o mesmo CloudEvent (mesmo id) chegou nos três consumidores" if mesmos else ""
    )
    confere(
        mesmos,
        "fan-out: a TransacaoAutorizada não chegou com o mesmo id em antifraude e notificacoes",
    )


def obsoletas_no_log(linhas: str) -> int:
    """Soma o OBSOLETA das linhas de lote da ACL (`{"msg": "lote", "OBSOLETA": n, ...}`); o resto é ignorado."""
    total = 0
    for linha in linhas.splitlines():
        inicio = linha.find("{")
        if inicio < 0:
            continue
        try:
            registro = json.loads(linha[inicio:])
        except ValueError:
            continue
        if isinstance(registro, dict) and registro.get("msg") == "lote":
            total += int(registro.get("OBSOLETA", 0))
    return total


def acl_julgou_obsoleta(desde: float) -> bool:
    """Sinal POSITIVO de que a ACL comparou as versões: linha de lote com OBSOLETA no log do `limites-acl`,
    só a partir de `desde` (logs de execuções anteriores não contam)."""
    marca = (
        datetime.fromtimestamp(desde, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    saida = subprocess.run(
        [*COMPOSE, "logs", "--no-color", "--since", marca, "limites-acl"],
        capture_output=True,
        text=True,
        check=False,
    )
    return saida.returncode == 0 and obsoletas_no_log(saida.stdout) >= 1


def mensagens_na_fila(nome: str) -> int:
    """Visíveis + em voo + atrasadas (a DLQ do feed tem de estar vazia de verdade, não só sem visíveis)."""
    sqs = cliente("sqs")
    atributos = sqs.get_queue_attributes(
        QueueUrl=sqs.get_queue_url(QueueName=nome)["QueueUrl"],
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )["Attributes"]
    return sum(int(v) for v in atributos.values())


def demo_legado(http: httpx.Client) -> None:
    titulo("7. Core legado → ACL → limite atualizado → LimiteAlterado (CDC)")
    id_contrato = f"CTR-LEG-{uuid.uuid4().hex[:6].upper()}"
    conta_do_legado = "0001-55555555-5"  # a mesma nos três registros e na reserva
    enviar = [
        sys.executable,
        "-m",
        "legado_mock",
        "enviar",
        "--contrato",
        id_contrato,
        "--conta",
        conta_do_legado,
        "--produto",
        "01",
        "--situacao",
        "A",
    ]
    subprocess.run(
        [*enviar, "--limite", "1500.00", "--versao", "1"], check=True, capture_output=True
    )
    criado = aguardar(
        lambda: http.get(f"{LIMITES}/interno/v1/contratos/{id_contrato}/limite").status_code == 200
    )
    print(f"carga inicial (registro v1, R$ 1.500) → contrato criado pela ACL: {bool(criado)}")
    reservada, _ = autorizar(http, id_contrato, reserva("400.00", conta=conta_do_legado))
    print(f"RESERVA 400 na conta do contrato → {reservada.status_code}")
    confere(bool(criado), "a ACL não criou o contrato a partir do registro do legado")
    confere(
        reservada.status_code == 201,
        "legado: a reserva de 400 no contrato criado pela ACL deveria ser 201",
    )

    def na_versao_3() -> dict[str, Any] | None:
        limite = http.get(f"{LIMITES}/interno/v1/contratos/{id_contrato}/limite").json()
        return dict(limite) if limite.get("versao_legado") == 3 else None

    # A v2 só sai depois que a v3 foi APLICADA: a fila do legado é Standard (sem ordem), então mandar as
    # duas juntas não provaria nada se a ACL pegasse a v2 primeiro.
    subprocess.run(
        [*enviar, "--limite", "1000.00", "--versao", "3"], check=True, capture_output=True
    )
    v3 = aguardar(na_versao_3)
    confere(bool(v3), "legado: a v3 não foi aplicada em 15 s")
    if v3:
        print(f"v3 (R$ 1.000) aplicada → concedido={v3['limite_concedido']}  versao_legado=3")

    sqs = cliente("sqs")
    fila_do_legado = sqs.get_queue_url(QueueName="legado-contratos")["QueueUrl"]

    def legado_consumido() -> bool:
        a = sqs.get_queue_attributes(
            QueueUrl=fila_do_legado,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return bool(
            a["ApproximateNumberOfMessages"] == a["ApproximateNumberOfMessagesNotVisible"] == "0"
        )

    # Oráculo da v2: "fila vazia" sozinho não prova nada (registro de tipo desconhecido é apagado como IGNORADO;
    # falha repetida acaba na DLQ e também esvazia a fila). Exige-se, além do estado final: a ACL AVALIOU a v2 e a
    # julgou OBSOLETA (linha de lote no log dela, só a partir de agora) e nada caiu na DLQ do feed.
    desde = time.time()
    subprocess.run(
        [*enviar, "--limite", "9999.00", "--versao", "2"], check=True, capture_output=True
    )
    consumida = aguardar(legado_consumido)  # a ACL só apaga a mensagem depois de processar
    julgada = aguardar(lambda: acl_julgou_obsoleta(desde))
    na_dlq = mensagens_na_fila("legado-contratos-dlq")
    final = limite(http, id_contrato)
    print(
        f"v2 FORA DE ORDEM (R$ 9.999) consumida pela ACL: {bool(consumida)}  julgada OBSOLETA no log da "
        f"ACL: {bool(julgada)}  legado-contratos-dlq: {na_dlq} → "
        f"concedido={final.get('limite_concedido')}  versao_legado={final.get('versao_legado')}  "
        f"reservado={final.get('saldo_reservado')}  disponível={final.get('limite_disponivel')}"
    )
    print("   (a ACL só toca concedido/status; a reserva de R$ 400 continua lá)")
    confere(
        bool(julgada), "legado: a ACL não registrou a v2 como OBSOLETA (avaliou mesmo a versão?)"
    )
    confere(na_dlq == 0, f"legado: {na_dlq} mensagem(ns) na legado-contratos-dlq")
    confere(
        bool(consumida)
        and final.get("versao_legado") == 3
        and Decimal(str(final.get("limite_concedido"))) == Decimal("1000")
        and Decimal(str(final.get("saldo_reservado"))) == Decimal("400"),
        "legado: esperado v2 consumida e ignorada (versao_legado 3, concedido 1000) e reserva de 400 intacta",
    )

    # CDC: a mudança de concedido da v3 vira LimiteAlterado no bus, roteado para notificacoes.
    alterado = receber_da_fila(
        "notificacoes",
        lambda e: bool(
            e.get("detail-type") == "LimiteAlterado"
            and ((e.get("detail") or {}).get("data") or {}).get("id_contrato") == id_contrato
        ),
    )
    dados = alterado["detail"]["data"] if alterado else {}
    if alterado:
        print(
            f"LimiteAlterado em notificacoes: concedido {dados['limite_concedido_anterior']} → "
            f"{dados['limite_concedido']}  versao_legado={dados['versao_legado']}  "
            f"id={alterado['detail']['id']}"
        )
    confere(
        bool(alterado)
        and dados.get("limite_concedido_anterior") == Decimal("1500")
        and dados.get("limite_concedido") == Decimal("1000")
        and dados.get("versao_legado") == 3,
        "LimiteAlterado (1500 → 1000, versao_legado 3) não chegou em notificacoes",
    )


def main() -> None:
    with httpx.Client(timeout=10, limits=httpx.Limits(max_connections=100)) as http:
        try:
            http.get(f"{AUTORIZACAO}/saude").raise_for_status()
        except httpx.HTTPError:
            sys.exit("ambiente fora do ar: rode `make up` antes")
        demo_aprovacao_e_replay(http)
        demo_corrida(http)
        demo_operacoes(http)
        demo_captura(http)
        demo_validacao(http)
        demo_eventos(http)
        demo_legado(http)
    if DIVERGENCIAS:
        print(f"\n❌ {len(DIVERGENCIAS)} divergência(s): " + "; ".join(DIVERGENCIAS))
        sys.exit(1)
    print("\n✅ demo completa, sem divergências")


if __name__ == "__main__":
    main()
