#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/Neuro_rop_practice}"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
REPORTS_DIR="${RUNTIME_DIR}/reports"
LOGS_DIR="${RUNTIME_DIR}/logs"
KNOWLEDGE_DIR="${RUNTIME_DIR}/knowledge"
PIPELINE_MAP_FILE="${RUNTIME_DIR}/crm_pipeline_map.json"
AUTH_DIR="${RUNTIME_DIR}/nginx"
AUTH_FILE="${AUTH_DIR}/.htpasswd"
ACCESS_FILE="${RUNTIME_DIR}/access.txt"
NETWORK="neuro-rop-practice-net"
API_CONTAINER="neuro-rop-api"
WEB_CONTAINER="neuro-rop-web"
API_IMAGE="neuro-rop-practice-api:production"
WEB_IMAGE="neuro-rop-practice-web:production"
WEB_PORT="18081"

require_file() {
    [[ -f "$1" ]] || { echo "Не найден обязательный файл: $1" >&2; exit 1; }
}

require_directory() {
    [[ -d "$1" ]] || { echo "Не найдена обязательная папка: $1" >&2; exit 1; }
}

container_running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

require_file "${RUNTIME_DIR}/.env"
require_file "${ACCESS_FILE}"
require_file "${PIPELINE_MAP_FILE}"
require_directory "${REPORTS_DIR}"
require_directory "${LOGS_DIR}"
require_directory "${KNOWLEDGE_DIR}"
[[ -s "${ACCESS_FILE}" ]] || { echo "Пустой пароль: ${ACCESS_FILE}" >&2; exit 1; }

mkdir -p "${AUTH_DIR}"
chmod 700 "${RUNTIME_DIR}" "${AUTH_DIR}"
chmod 600 "${RUNTIME_DIR}/.env" "${ACCESS_FILE}"

password="$(<"${ACCESS_FILE}")"
printf '%s\n' "${password}" \
    | docker run --rm -i httpd:2.4-alpine htpasswd -i -nB rop > "${AUTH_FILE}"
unset password
chmod 644 "${AUTH_FILE}"

docker network inspect "${NETWORK}" >/dev/null 2>&1 \
    || docker network create "${NETWORK}" >/dev/null

docker build --tag "${API_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.api" "${PROJECT_ROOT}"
docker build --tag "${WEB_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.web" "${PROJECT_ROOT}"

# Only application containers are replaced. The API has no host-published port.
docker rm --force "${WEB_CONTAINER}" "${API_CONTAINER}" >/dev/null 2>&1 || true

chown -R 10001:10001 "${REPORTS_DIR}" "${LOGS_DIR}"

docker run --detach \
    --name "${API_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --env-file "${RUNTIME_DIR}/.env" \
    --volume "${REPORTS_DIR}:/app/reports" \
    --volume "${LOGS_DIR}:/app/logs" \
    --volume "${KNOWLEDGE_DIR}:/app/knowledge:ro" \
    --volume "${PIPELINE_MAP_FILE}:/app/crm_pipeline_map.json:ro" \
    --security-opt no-new-privileges \
    "${API_IMAGE}" >/dev/null

api_ready=false
for _ in $(seq 1 30); do
    if docker exec "${API_CONTAINER}" python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2).read()' \
        >/dev/null 2>&1; then
        api_ready=true
        break
    fi
    container_running "${API_CONTAINER}" || break
    sleep 1
done
[[ "${api_ready}" == "true" ]] || {
    echo "API не прошёл health-check. Проверьте: docker logs --tail 100 ${API_CONTAINER}" >&2
    exit 1
}

docker run --detach \
    --name "${WEB_CONTAINER}" \
    --network "${NETWORK}" \
    --publish "127.0.0.1:${WEB_PORT}:80" \
    --restart unless-stopped \
    --volume "${AUTH_FILE}:/etc/nginx/auth/.htpasswd:ro" \
    --security-opt no-new-privileges \
    "${WEB_IMAGE}" >/dev/null

docker exec "${WEB_CONTAINER}" nginx -t >/dev/null

for container in "${API_CONTAINER}" "${WEB_CONTAINER}"; do
    container_running "${container}" || {
        echo "Контейнер ${container} не запущен." >&2
        exit 1
    }
done

status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 15 "http://127.0.0.1:${WEB_PORT}/")"
case "${status}" in
    2??|401) ;;
    *) echo "Web localhost check returned unexpected HTTP ${status}." >&2; exit 1 ;;
esac

echo "Production containers are healthy; web localhost status: ${status}."
