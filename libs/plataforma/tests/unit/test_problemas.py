"""Testes do modelo de Problem Details (RFC 9457)."""

from dataclasses import FrozenInstanceError

import pytest
from plataforma.problemas import BASE_DO_TYPE, MEDIA_TYPE, Problema


def _basico(**kwargs: object) -> Problema:
    base: dict[str, object] = {
        "type_slug": "contrato-nao-encontrado",
        "title": "Contrato não encontrado",
        "status": 404,
        "codigo": "CONTRATO_NAO_ENCONTRADO",
    }
    return Problema(**{**base, **kwargs})  # type: ignore[arg-type]


def test_media_type_e_base_do_type() -> None:
    assert MEDIA_TYPE == "application/problem+json"
    assert BASE_DO_TYPE == "https://financas.example/problemas/"


def test_corpo_minimo_tem_type_absoluto_e_ids_nulos_sempre_presentes() -> None:
    corpo = _basico().para_dict()

    assert corpo == {
        "type": "https://financas.example/problemas/contrato-nao-encontrado",
        "title": "Contrato não encontrado",
        "status": 404,
        "codigo": "CONTRATO_NAO_ENCONTRADO",
        "correlation_id": None,
        "trace_id": None,
    }


def test_detail_so_aparece_quando_informado() -> None:
    assert "detail" not in _basico().para_dict()
    assert _basico(detail="CTR-9 inexistente").para_dict()["detail"] == "CTR-9 inexistente"


def test_ids_de_correlacao_e_trace_sao_copiados() -> None:
    corpo = _basico(
        correlation_id="0b0e8f4e-9c0a-4d4e-9a53-2f6a6a0b1c11",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    ).para_dict()

    assert corpo["correlation_id"] == "0b0e8f4e-9c0a-4d4e-9a53-2f6a6a0b1c11"
    assert corpo["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_extras_entram_no_corpo() -> None:
    erros = [{"campo": "valor", "mensagem": "maior que zero"}]

    corpo = _basico(status=422, extras={"erros": erros}).para_dict()

    assert corpo["erros"] == erros


def test_extras_nao_sobrescrevem_membros_reservados() -> None:
    corpo = _basico(extras={"status": 200, "codigo": "OUTRO", "type": "x", "extra": 1}).para_dict()

    assert corpo["status"] == 404
    assert corpo["codigo"] == "CONTRATO_NAO_ENCONTRADO"
    assert corpo["type"] == "https://financas.example/problemas/contrato-nao-encontrado"
    assert corpo["extra"] == 1


def test_extras_padrao_nao_e_compartilhado_entre_instancias() -> None:
    a, b = _basico(), _basico()

    assert a.extras is not b.extras


def test_problema_e_imutavel() -> None:
    problema = _basico()

    with pytest.raises(FrozenInstanceError):
        problema.status = 500  # type: ignore[misc]
