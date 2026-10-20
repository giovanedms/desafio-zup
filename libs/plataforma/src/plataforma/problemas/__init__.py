"""Modelo de Problem Details (RFC 9457) compartilhado pelos serviços. Somente stdlib.

O catálogo de tipos vive em `contracts/problem-types/catalogo.md`; aqui fica só a forma do corpo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

MEDIA_TYPE = "application/problem+json"
BASE_DO_TYPE = "https://financas.example/problemas/"  # domínio reservado (RFC 2606)

# Membros definidos pela RFC 9457 e pelo catálogo: `extras` nunca os sobrescreve.
_MEMBROS_RESERVADOS = frozenset(
    {"type", "title", "status", "codigo", "detail", "correlation_id", "trace_id"}
)


@dataclass(frozen=True, slots=True)
class Problema:
    """Problema HTTP independente de framework; o adapter decide como serializar e responder."""

    type_slug: str
    title: str
    status: int
    codigo: str
    detail: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    extras: Mapping[str, object] = field(default_factory=dict)

    def para_dict(self) -> dict[str, object]:
        """Corpo RFC 9457. `trace_id` e `correlation_id` sempre presentes (podem ser None)."""
        corpo: dict[str, object] = {
            "type": BASE_DO_TYPE + self.type_slug,
            "title": self.title,
            "status": self.status,
            "codigo": self.codigo,
        }
        if self.detail is not None:
            corpo["detail"] = self.detail
        corpo["correlation_id"] = self.correlation_id
        corpo["trace_id"] = self.trace_id
        corpo.update({k: v for k, v in self.extras.items() if k not in _MEMBROS_RESERVADOS})
        return corpo
