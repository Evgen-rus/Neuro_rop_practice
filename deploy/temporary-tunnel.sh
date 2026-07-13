#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/Neuro_rop_practice}"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
REPORTS_DIR="${RUNTIME_DIR}/reports"
KNOWLEDGE_DIR="${RUNTIME_DIR}/knowledge"
AUTH_DIR="${RUNTIME_DIR}/nginx"
AUTH_FILE="${AUTH_DIR}/.htpasswd"
ACCESS_FILE="${RUNTIME_DIR}/access.txt"
NETWORK="neuro-rop-practice-net"
API_CONTAINER="neuro-rop-api"
WEB_CONTAINER="neuro-rop-web"
TUNNEL_CONTAINER="neuro-rop-tunnel"
API_IMAGE="neuro-rop-practice-api:temporary"
WEB_IMAGE="neuro-rop-practice-web:temporary"

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "Не найден обязательный файл: $1" >&2
        exit 1
    fi
}

require_directory() {
    if [[ ! -d "$1" ]]; then
        echo "Не найдена обязательная папка: $1" >&2
        exit 1
    fi
}

require_file "${RUNTIME_DIR}/.env"
require_directory "${REPORTS_DIR}"
require_directory "${KNOWLEDGE_DIR}"

mkdir -p "${AUTH_DIR}"
chmod 700 "${RUNTIME_DIR}" "${AUTH_DIR}"

if [[ ! -s "${ACCESS_FILE}" ]]; then
    umask 077
    head -c 24 /dev/urandom | base64 | tr -d '\n' > "${ACCESS_FILE}"
    printf '\n' >> "${ACCESS_FILE}"
    echo "Создан временный пароль. Он сохранён только в ${ACCESS_FILE}."
fi

password="$(<"${ACCESS_FILE}")"
printf '%s\n' "${password}" | docker run --rm -i httpd:2.4-alpine htpasswd -i -nB rop > "${AUTH_FILE}"
unset password
chmod 644 "${AUTH_FILE}"
chmod 600 "${ACCESS_FILE}"

docker network inspect "${NETWORK}" >/dev/null 2>&1 || docker network create "${NETWORK}" >/dev/null

docker build --tag "${API_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.api" "${PROJECT_ROOT}"
docker build --tag "${WEB_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.web" "${PROJECT_ROOT}"

docker rm --force "${TUNNEL_CONTAINER}" "${WEB_CONTAINER}" "${API_CONTAINER}" >/dev/null 2>&1 || true

chown -R 10001:10001 "${REPORTS_DIR}"

docker run --detach \
    --name "${API_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --env-file "${RUNTIME_DIR}/.env" \
    --volume "${REPORTS_DIR}:/app/reports" \
    --volume "${KNOWLEDGE_DIR}:/app/knowledge:ro" \
    --security-opt no-new-privileges \
    "${API_IMAGE}" >/dev/null

docker run --detach \
    --name "${WEB_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --volume "${AUTH_FILE}:/etc/nginx/auth/.htpasswd:ro" \
    --security-opt no-new-privileges \
    "${WEB_IMAGE}" >/dev/null

docker run --detach \
    --name "${TUNNEL_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --security-opt no-new-privileges \
    cloudflare/cloudflared:latest tunnel --no-autoupdate --url "http://${WEB_CONTAINER}:80" >/dev/null

for _ in $(seq 1 30); do
    url="$(docker logs "${TUNNEL_CONTAINER}" 2>&1 | grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' | tail -n 1 || true)"
    if [[ -n "${url}" ]]; then
        break
    fi
    sleep 1
done

if [[ -z "${url:-}" ]]; then
    echo "Контейнеры запущены, но ссылка ещё не получена. Проверьте: docker logs ${TUNNEL_CONTAINER}" >&2
    exit 1
fi

echo
echo "Временная HTTPS-ссылка: ${url}"
echo "Логин: rop"
echo "Пароль находится только в ${ACCESS_FILE}"
