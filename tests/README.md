# Testes transversais
- `load/` — k6: corretude sob concorrência (N reservas paralelas num limite) e throughput/latência
- `contract/` — gate de compatibilidade dos schemas de evento (`test_compat_schemas.py`) e resultados do k6 sem segredo.
  A conformidade com os contratos fica junto do serviço que os implementa: API ≡ OpenAPI em
  `services/autorizacao/tests/contract/test_api_bate_com_o_contrato.py` (app real com dublês, corpos validados com
  jsonschema contra as respostas declaradas) e eventos ≡ JSON Schema em `services/autorizacao/tests/contract/test_cloudevents_contrato.py`
- `e2e/` — fluxo completo HTTP → evento → contabilização
