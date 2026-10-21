"""Núcleo do consumidor: valida antes de reivindicar, emite uma linha só com identificadores, deduplica por
(source, id), devolve à fila o que outra invocação está processando e tolera reordenação e reentrega."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from consumidores.esquemas import CatalogoDeEsquemas
from consumidores.inbox_memoria import InboxEmMemoria
from consumidores.nucleo import ConsumirEvento, Desfecho, EventoInvalido, Situacao
from hypothesis import given, settings
from hypothesis import strategies as st

AGORA_MS = 1_790_000_000_000
PRAZO_MS = AGORA_MS + 30_000
TIPOS_DAS_FILAS = [
    "transacao-autorizada",
    "transacao-recusada",
    "transacao-estornada",
    "limite-alterado",
]


@dataclass
class Relogio:
    agora_ms: int = AGORA_MS

    def __call__(self) -> int:
        return self.agora_ms


@dataclass
class Cenario:
    consumir: ConsumirEvento
    inbox: InboxEmMemoria
    relogio: Relogio
    linhas: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def cenario(catalogo: CatalogoDeEsquemas) -> Cenario:
    inbox, relogio, linhas = InboxEmMemoria(), Relogio(), []
    consumir = ConsumirEvento("antifraude", catalogo, inbox, linhas.append, relogio)
    return Cenario(consumir, inbox, relogio, linhas)


def test_evento_valido_emite_uma_linha_so_com_identificadores(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    ce = cloudevent("transacao-autorizada")

    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS) is Desfecho.CONSUMIDO

    assert cenario.linhas == [
        {
            "evento": "evento_consumido",
            "consumidor": "antifraude",
            "tipo": "example.financas.autorizacao.TransacaoAutorizada.v1",
            "id": ce["id"],
            "source": "/financas/autorizacao",
            "ocorrido_em": ce["time"],
            "id_contrato": "CTR-000123456",
            "id_autorizacao": "6f1c2b0e-3d4a-5b6c-8d7e-9f0a1b2c3d4e",
            "versao_limite": 42,
            "correlation_id": "0b8a1f2e-9c7d-4e6f-8a5b-3c2d1e0f9a8b",
        }
    ]
    texto = json.dumps(cenario.linhas)
    for dado in (
        "0001-12345678-9",
        "EST-998877",
        "150",
        "POS",
        "BRL",
    ):  # conta, estabelecimento, valor, canal
        assert dado not in texto


@pytest.mark.parametrize("nome", TIPOS_DAS_FILAS)
def test_todo_tipo_que_as_regras_entregam_nas_filas_e_consumido(
    cenario: Cenario,
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
    nome: str,
) -> None:
    # antifraude: Autorizada e Recusada; notificacoes: essas, Estornada e LimiteAlterado (modules/eventos).
    ce = cloudevent(nome)

    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS) is Desfecho.CONSUMIDO
    assert [linha["id"] for linha in cenario.linhas] == [ce["id"]]


def _sem_data(ce: dict[str, Any], campo: str) -> dict[str, Any]:
    del ce["data"][campo]
    return ce


INVALIDOS: dict[
    str, tuple[Callable[[Callable[..., dict[str, Any]], Callable[..., str]], str], str]
] = {
    "corpo que não é JSON": (lambda ce, m: "isto não é json", "$: corpo da mensagem não é JSON"),
    "envelope sem detail": (
        lambda ce, m: json.dumps({"detail-type": "X"}),
        "$: envelope do EventBridge",
    ),
    "campo obrigatório ausente": (
        lambda ce, m: m(_sem_data(ce(), "id_autorizacao")),
        "$.data: required id_autorizacao",
    ),
    "valor negativo": (lambda ce, m: m(ce(valor=-1.5)), "$.data.valor: minimum"),
    "occurred_at fora do RFC 3339": (
        lambda ce, m: m(ce(occurred_at="ontem")),
        "$.data.occurred_at: format",
    ),
    "id fora do formato": (lambda ce, m: m({**ce(), "id": "evt-1"}), "$.id: pattern"),
    "detail-type de outro tipo": (
        lambda ce, m: m(ce(), detail_type="TransacaoRecusada"),
        "$.detail-type: diverge de data.event_type",
    ),
}


@pytest.mark.parametrize("caso", sorted(INVALIDOS))
def test_mensagem_invalida_levanta_sem_reivindicar_nem_emitir(
    cenario: Cenario,
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
    caso: str,
) -> None:
    montar, esperado = INVALIDOS[caso]

    with pytest.raises(EventoInvalido) as erro:
        cenario.consumir.executar(montar(cloudevent, mensagem), PRAZO_MS)

    assert any(e.startswith(esperado) for e in erro.value.erros), erro.value.erros
    assert cenario.linhas == []
    assert (
        cenario.inbox.itens == {}
    )  # inválido não ocupa a inbox: a correção e o redrive passam limpos


def test_reentrega_do_mesmo_cloudevent_em_outro_envelope_e_duplicata(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    # Relay e sweeper publicando o mesmo evento: envelopes do EventBridge (e mensagens SQS) diferentes, mesmo CE id.
    ce = cloudevent()

    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS) is Desfecho.CONSUMIDO
    cenario.relogio.agora_ms += 3_600_000  # muito depois do prazo: concluído não vence
    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS + 3_600_000) is Desfecho.DUPLICADO
    assert len(cenario.linhas) == 1


def test_mesmo_id_de_outro_source_nao_e_duplicata(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    autorizada = cloudevent("transacao-autorizada")
    limite = cloudevent("limite-alterado")
    limite["id"] = limite["data"]["event_id"] = autorizada[
        "id"
    ]  # CloudEvents: o id é único POR source

    assert cenario.consumir.executar(mensagem(autorizada), PRAZO_MS) is Desfecho.CONSUMIDO
    assert cenario.consumir.executar(mensagem(limite), PRAZO_MS) is Desfecho.CONSUMIDO
    assert len(cenario.linhas) == 2


def test_evento_que_outra_invocacao_esta_processando_volta_para_a_fila(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    ce = cloudevent()
    cenario.inbox.reivindicar(f"{ce['source']}|{ce['id']}", AGORA_MS, PRAZO_MS)  # a outra invocação

    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS) is Desfecho.EM_ANDAMENTO
    assert cenario.linhas == []


def test_reivindicacao_de_invocacao_que_morreu_vence_e_o_evento_sai_de_novo(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    # A outra invocação reivindicou e morreu (timeout) antes de concluir: depois do prazo dela, a reentrega emite.
    ce = cloudevent()
    cenario.inbox.reivindicar(f"{ce['source']}|{ce['id']}", AGORA_MS, PRAZO_MS)
    cenario.relogio.agora_ms = PRAZO_MS + 1

    assert cenario.consumir.executar(mensagem(ce), PRAZO_MS + 30_000) is Desfecho.CONSUMIDO
    assert [linha["id"] for linha in cenario.linhas] == [ce["id"]]


class InboxQueNaoConclui(InboxEmMemoria):
    def concluir(self, chave: str) -> None:
        raise RuntimeError("UpdateItem negado")


def test_falha_ao_concluir_levanta_e_a_reentrega_emite_de_novo(
    catalogo: CatalogoDeEsquemas,
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
) -> None:
    inbox, relogio, linhas = InboxQueNaoConclui(), Relogio(), []
    consumir = ConsumirEvento("notificacoes", catalogo, inbox, linhas.append, relogio)
    ce = cloudevent()

    with pytest.raises(RuntimeError):
        consumir.executar(mensagem(ce), PRAZO_MS)
    relogio.agora_ms = PRAZO_MS + 180_000  # a mensagem volta depois da visibilidade da fila
    with pytest.raises(RuntimeError):
        consumir.executar(mensagem(ce), relogio.agora_ms + 30_000)

    assert [linha["id"] for linha in linhas] == [ce["id"], ce["id"]]  # at-least-once, nunca zero


def test_numeros_reserializados_pelo_eventbridge_sao_aceitos(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    # ADR-0011 §4: o EventBridge reserializa os números do detail como double (99999998.88 chegou 9.999999888E7 na
    # AWS). Um inteiro com ".0" e dinheiro em notação científica continuam válidos, e a linha sai com o inteiro.
    ce = cloudevent(versao_limite=42, valor=1)
    corpo = mensagem(ce)
    corpo = corpo.replace('"versao_limite": 42', '"versao_limite": 42.0')
    corpo = corpo.replace('"valor": 1,', '"valor": 9.999999888E7,')
    assert '"versao_limite": 42.0' in corpo and "9.999999888E7" in corpo

    assert cenario.consumir.executar(corpo, PRAZO_MS) is Desfecho.CONSUMIDO
    (linha,) = cenario.linhas
    assert type(linha["versao_limite"]) is int and linha["versao_limite"] == 42
    json.dumps(linha)  # a linha continua serializável (sem Decimal)


@pytest.mark.parametrize("versao", ["42.5", "true", '"42"'])
def test_inteiro_com_fracao_booleano_ou_texto_continua_invalido(
    cenario: Cenario,
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
    versao: str,
) -> None:
    corpo = mensagem(cloudevent(versao_limite=42)).replace(
        '"versao_limite": 42', f'"versao_limite": {versao}'
    )

    with pytest.raises(EventoInvalido) as erro:
        cenario.consumir.executar(corpo, PRAZO_MS)

    assert erro.value.erros == ("$.data.versao_limite: type",)


def test_reordenacao_estorno_antes_da_autorizacao_e_versao_menor_depois_da_maior(
    cenario: Cenario, cloudevent: Callable[..., dict[str, Any]], mensagem: Callable[..., str]
) -> None:
    autorizada = cloudevent("transacao-autorizada", versao_limite=42)
    estorno = cloudevent(
        "transacao-estornada",
        id_autorizacao_original=autorizada["data"]["id_autorizacao"],
        versao_limite=43,
    )

    desfechos = [cenario.consumir.executar(mensagem(ce), PRAZO_MS) for ce in (estorno, autorizada)]

    # Nada é descartado por chegar fora de ordem: cada fato sai, com a versão e o instante para o destino ordenar.
    assert desfechos == [Desfecho.CONSUMIDO, Desfecho.CONSUMIDO]
    assert [(linha["tipo"].split(".")[3], linha["versao_limite"]) for linha in cenario.linhas] == [
        ("TransacaoEstornada", 43),
        ("TransacaoAutorizada", 42),
    ]
    assert cenario.linhas[0]["id_autorizacao_original"] == autorizada["data"]["id_autorizacao"]


@settings(max_examples=60, deadline=None)
@given(
    tipos=st.lists(st.sampled_from(TIPOS_DAS_FILAS), min_size=1, max_size=8),
    copias=st.lists(st.integers(min_value=1, max_value=3), min_size=8, max_size=8),
    ordem=st.randoms(use_true_random=False),
)
def test_qualquer_ordem_com_reentregas_emite_cada_evento_exatamente_uma_vez(
    *,
    catalogo: CatalogoDeEsquemas,
    cloudevent: Callable[..., dict[str, Any]],
    mensagem: Callable[..., str],
    tipos: list[str],
    copias: list[int],
    ordem: Any,
) -> None:
    inbox, linhas = InboxEmMemoria(), []
    consumir = ConsumirEvento("notificacoes", catalogo, inbox, linhas.append, Relogio())
    eventos = [cloudevent(t) for t in tipos]
    entregas = [ce for ce, n in zip(eventos, copias, strict=False) for _ in range(n)]
    ordem.shuffle(entregas)

    desfechos = Counter(consumir.executar(mensagem(ce), PRAZO_MS) for ce in entregas)

    assert sorted(linha["id"] for linha in linhas) == sorted(ce["id"] for ce in eventos)
    assert desfechos == Counter(
        {Desfecho.CONSUMIDO: len(eventos), Desfecho.DUPLICADO: len(entregas) - len(eventos)}
    )
    # a linha sai na ordem da PRIMEIRA chegada de cada evento, qualquer que seja ela
    primeira_chegada = list(dict.fromkeys(ce["id"] for ce in entregas))
    assert [linha["id"] for linha in linhas] == primeira_chegada
    assert {estado for estado, _ in inbox.itens.values()} == {"CONCLUIDO"}


def test_situacao_da_inbox_cobre_os_tres_desfechos() -> None:
    assert {s.value for s in Situacao} == {"NOVO", "CONCLUIDO", "EM_ANDAMENTO"}
