// Carga da API de Autorização. Cenários via CENARIO=corretude|vazao (ver tests/load/README.md).
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:18080";
const CLIENT_ID = __ENV.CLIENT_ID || "k6";
const API_KEY = __ENV.API_KEY || "";
const TOKEN_URL = __ENV.TOKEN_URL || "";
const CENARIO = __ENV.CENARIO || "vazao";

const VALOR = parseFloat(__ENV.VALOR || "100.00");
const LIMITE = parseFloat(__ENV.LIMITE || "10000.00");
const VUS = parseInt(__ENV.VUS || "100", 10);
const RPS = parseInt(__ENV.RPS || "50", 10);
const DURACAO = __ENV.DURACAO || "60s";
const P99_MS = parseInt(__ENV.P99_MS || "150", 10);
const CONTRATO = __ENV.CONTRATO || "";

// Carga espalhada como a real (80 M de contas, milésimos de op/s por contrato): N_CONTRATOS contratos
// PREFIXO + número com PAD dígitos. Concentrar em poucos contratos mede o teto do contrato quente (ADR-0016).
const N_CONTRATOS = parseInt(__ENV.N_CONTRATOS || "20", 10);
const PREFIXO = __ENV.PREFIXO || "CTR-CARGA-";
const PAD = parseInt(__ENV.PAD || "4", 10);
function contratosPadrao() {
  const lista = [];
  for (let i = 1; i <= N_CONTRATOS; i++) lista.push(`${PREFIXO}${String(i).padStart(PAD, "0")}`);
  return lista;
}
const CONTRATOS = __ENV.CONTRATOS
  ? __ENV.CONTRATOS.split(",").map((c) => c.trim()).filter((c) => c)
  : contratosPadrao();

// Contagem por status (submétricas por tag só existem com threshold; contadores dedicados não).
const STATUS_CONHECIDOS = [0, 200, 201, 202, 400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503, 504];
const contadores = {};
for (const st of STATUS_CONHECIDOS) contadores[st] = new Counter(`st_${st}`);
const contadorOutros = new Counter("st_outros");
const foraDoEsperado = new Counter("status_fora_de_201_402");

function registrar(resposta) {
  (contadores[resposta.status] || contadorOutros).add(1);
  if (resposta.status !== 201 && resposta.status !== 402) foraDoEsperado.add(1);
}

const CENARIOS = {
  corretude: {
    executor: "per-vu-iterations",
    vus: VUS,
    iterations: 1,
    maxDuration: "2m",
  },
  vazao: {
    executor: "constant-arrival-rate",
    rate: RPS,
    timeUnit: "1s",
    duration: DURACAO,
    preAllocatedVUs: Math.max(20, RPS),
    maxVUs: Math.max(100, RPS * 4),
  },
};

if (!CENARIOS[CENARIO]) {
  throw new Error(`CENARIO inválido: ${CENARIO} (use corretude ou vazao)`);
}

export const options = {
  scenarios: { [CENARIO]: CENARIOS[CENARIO] },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
  thresholds:
    CENARIO === "vazao"
      ? {
          http_req_failed: ["rate<0.01"],
          "http_req_duration{expected_response:true}": [`p(99)<${P99_MS}`],
        }
      : {
          // Thresholds são avaliados no fim: violá-los faz o k6 sair com código != 0 (99).
          st_201: [`count==${Math.floor(LIMITE / VALOR)}`],
          status_fora_de_201_402: ["count==0"],
        },
};

// UUID v4 local (sem imports remotos).
function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = Math.floor(Math.random() * 16);
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function setup() {
  if (CENARIO === "corretude" && !CONTRATO) {
    throw new Error("CONTRATO é obrigatório no cenário corretude");
  }
  const clientId = __ENV.OAUTH_CLIENT_ID;
  const clientSecret = __ENV.OAUTH_CLIENT_SECRET;
  if (TOKEN_URL && clientId && clientSecret) {
    const resposta = http.post(
      TOKEN_URL,
      { grant_type: "client_credentials", client_id: clientId, client_secret: clientSecret },
      { tags: { name: "token" }, responseCallback: http.expectedStatuses(200) },
    );
    if (resposta.status !== 200) throw new Error(`falha ao obter token: HTTP ${resposta.status}`);
    return { token: resposta.json("access_token") };
  }
  return { token: "" };
}

function cabecalhos(dados) {
  const h = {
    "Content-Type": "application/json",
    "Idempotency-Key": uuidv4(),
    "X-Client-Id": CLIENT_ID,
  };
  if (API_KEY) h["x-api-key"] = API_KEY;
  if (dados && dados.token) h.Authorization = `Bearer ${dados.token}`;
  return h;
}

function autorizar(contrato, tipo, valor, dados, params) {
  const corpo = JSON.stringify({
    id_conta: "0001-99999999-9",
    valor,
    moeda: "BRL",
    tipo_operacao: tipo,
  });
  return http.post(`${BASE_URL}/v1/contratos/${contrato}/autorizacoes`, corpo, {
    headers: cabecalhos(dados),
    ...params,
  });
}

export default function (dados) {
  if (CENARIO === "corretude") {
    const r = autorizar(CONTRATO, "RESERVA", VALOR, dados, {
      responseCallback: http.expectedStatuses(201, 402),
    });
    registrar(r);
    check(r, { "status 201 ou 402": (x) => x.status === 201 || x.status === 402 });
  } else {
    const contrato = CONTRATOS[Math.floor(Math.random() * CONTRATOS.length)];
    const r = autorizar(contrato, "DEBITO", 1.0, dados, {
      responseCallback: http.expectedStatuses(201, 402),
    });
    registrar(r);
    check(r, { "status 201 ou 402": (x) => x.status === 201 || x.status === 402 });
  }
}

function contarStatus(data) {
  const contagem = {};
  for (const [nome, m] of Object.entries(data.metrics)) {
    const achado = /^st_(\d+|outros)$/.exec(nome);
    if (achado && m.values.count > 0) contagem[achado[1]] = m.values.count;
  }
  return contagem;
}

export function handleSummary(data) {
  const trend = data.metrics.http_req_duration ? data.metrics.http_req_duration.values : {};
  const reqs = data.metrics.http_reqs ? data.metrics.http_reqs.values : { count: 0, rate: 0 };
  const status = contarStatus(data);
  const linhas = [
    `cenario=${CENARIO} base=${BASE_URL}`,
    `requisicoes=${reqs.count} rps=${(reqs.rate || 0).toFixed(1)}`,
    `latencia ms: p50=${(trend.med || 0).toFixed(1)} p95=${(trend["p(95)"] || 0).toFixed(1)} p99=${(trend["p(99)"] || 0).toFixed(1)} max=${(trend.max || 0).toFixed(1)}`,
    `status: ${JSON.stringify(status)}`,
  ];
  if (CENARIO === "corretude") {
    const aprovadas = status["201"] || 0;
    const esperadas = Math.floor(LIMITE / VALOR);
    linhas.push(`aprovadas=${aprovadas} esperadas=floor(${LIMITE}/${VALOR})=${esperadas}`);
    linhas.push(aprovadas === esperadas ? "OK: corretude sob concorrência" : "FALHA: aprovadas != esperadas");
  }
  const carimbo = new Date().toISOString().replace(/[:.]/g, "-");
  const texto = linhas.join("\n") + "\n";
  // setup_data carrega o token OAuth do setup(): NUNCA vai para o arquivo de resultado (que é versionado).
  const { setup_data: _semSegredo, ...seguro } = data;
  return {
    [`tests/load/resultados/${CENARIO}-${carimbo}.json`]: JSON.stringify(seguro, null, 2),
    stdout: texto,
  };
}
