#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/Neuro_rop_practice}"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
REPORTS_DIR="${RUNTIME_DIR}/reports"
KNOWLEDGE_DIR="${RUNTIME_DIR}/knowledge"
PIPELINE_MAP_FILE="${RUNTIME_DIR}/crm_pipeline_map.json"
AUTH_DIR="${RUNTIME_DIR}/nginx"
AUTH_FILE="${AUTH_DIR}/.htpasswd"
ACCESS_FILE="${RUNTIME_DIR}/access.txt"
NETWORK="neuro-rop-practice-net"
API_CONTAINER="neuro-rop-api"
WEB_CONTAINER="neuro-rop-web"
TUNNEL_CONTAINER="neuro-rop-tunnel"
API_IMAGE="neuro-rop-practice-api:temporary"
WEB_IMAGE="neuro-rop-practice-web:temporary"

# Quick Tunnel выдаёт новый URL при любом реальном перезапуске cloudflared.
# Поэтому обычный деплой обновляет только api/web и не трогает живой туннель.
container_exists() {
    docker inspect "$1" >/dev/null 2>&1
}

container_is_running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

extract_tunnel_url() {
    docker logs "${TUNNEL_CONTAINER}" 2>&1 \
        | grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' \
        | tail -n 1 || true
}

print_tunnel_url() {
    local url
    url="$(extract_tunnel_url)"
    if [[ -z "${url}" ]]; then
        echo "URL туннеля не найден. Проверьте: docker logs ${TUNNEL_CONTAINER}" >&2
        exit 1
    fi
    printf '%s\n' "${url}"
}

if [[ "${1:-}" == "--show-url" ]]; then
    print_tunnel_url
    exit 0
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Запуск/обновление временного стенда.

  ./deploy/temporary-tunnel.sh           пересобрать api/web, не трогая живой cloudflared
  ./deploy/temporary-tunnel.sh --show-url
                                        показать текущий Quick Tunnel URL из логов
EOF
    exit 0
fi

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

wait_for_tunnel_url() {
    local url=""
    for _ in $(seq 1 30); do
        url="$(extract_tunnel_url)"
        if [[ -n "${url}" ]]; then
            printf '%s' "${url}"
            return 0
        fi
        sleep 1
    done
    return 1
}

require_file "${RUNTIME_DIR}/.env"
require_directory "${REPORTS_DIR}"
require_directory "${KNOWLEDGE_DIR}"
require_file "${PIPELINE_MAP_FILE}"

mkdir -p "${AUTH_DIR}"
chmod 700 "${RUNTIME_DIR}" "${AUTH_DIR}"
chmod 600 "${RUNTIME_DIR}/.env"

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

# Если сеть когда-то пересоздали, живой туннель нужно снова подключить к ней.
# Если он уже в этой сети, команда вернёт ошибку — это нормально и игнорируется.
if container_exists "${TUNNEL_CONTAINER}"; then
    docker network connect "${NETWORK}" "${TUNNEL_CONTAINER}" >/dev/null 2>&1 || true
fi

docker build --tag "${API_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.api" "${PROJECT_ROOT}"
docker build --tag "${WEB_IMAGE}" --file "${PROJECT_ROOT}/Dockerfile.web" "${PROJECT_ROOT}"

preserved_tunnel_id=""
preserved_tunnel_started_at=""
if container_is_running "${TUNNEL_CONTAINER}"; then
    preserved_tunnel_id="$(docker inspect --format '{{.Id}}' "${TUNNEL_CONTAINER}")"
    preserved_tunnel_started_at="$(docker inspect --format '{{.State.StartedAt}}' "${TUNNEL_CONTAINER}")"
    echo "Найден работающий ${TUNNEL_CONTAINER}: оставляю его без перезапуска."
fi

# Не включать сюда ${TUNNEL_CONTAINER}: force-remove сбрасывает Quick Tunnel URL.
docker rm --force "${WEB_CONTAINER}" "${API_CONTAINER}" >/dev/null 2>&1 || true

chown -R 10001:10001 "${REPORTS_DIR}"

docker run --detach \
    --name "${API_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --env-file "${RUNTIME_DIR}/.env" \
    --volume "${REPORTS_DIR}:/app/reports" \
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
    if [[ "$(docker inspect --format '{{.State.Running}}' "${API_CONTAINER}" 2>/dev/null || true)" != "true" ]]; then
        break
    fi
    sleep 1
done

if [[ "${api_ready}" != "true" ]]; then
    echo "API не прошёл health-check. Проверьте: docker logs --tail 100 ${API_CONTAINER}" >&2
    exit 1
fi

docker run --detach \
    --name "${WEB_CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    --volume "${AUTH_FILE}:/etc/nginx/auth/.htpasswd:ro" \
    --security-opt no-new-privileges \
    "${WEB_IMAGE}" >/dev/null

if ! docker exec "${WEB_CONTAINER}" nginx -t >/dev/null 2>&1; then
    echo "Nginx не прошёл проверку конфигурации. Проверьте: docker logs --tail 100 ${WEB_CONTAINER}" >&2
    exit 1
fi

if container_is_running "${TUNNEL_CONTAINER}"; then
    echo "Quick Tunnel уже работает, новый контейнер не создаю."
elif container_exists "${TUNNEL_CONTAINER}"; then
    echo "Контейнер ${TUNNEL_CONTAINER} остановлен. Запускаю его без пересоздания."
    docker start "${TUNNEL_CONTAINER}" >/dev/null
else
    docker run --detach \
        --name "${TUNNEL_CONTAINER}" \
        --network "${NETWORK}" \
        --restart unless-stopped \
        --security-opt no-new-privileges \
        cloudflare/cloudflared:latest tunnel --no-autoupdate --url "http://${WEB_CONTAINER}:80" >/dev/null
fi

url="$(wait_for_tunnel_url || true)"
if [[ -z "${url}" ]]; then
    echo "Контейнеры запущены, но ссылка ещё не получена. Проверьте: docker logs ${TUNNEL_CONTAINER}" >&2
    exit 1
fi

for container in "${API_CONTAINER}" "${WEB_CONTAINER}" "${TUNNEL_CONTAINER}"; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null || true)" != "true" ]]; then
        echo "Контейнер ${container} не запущен. Проверьте: docker logs --tail 100 ${container}" >&2
        exit 1
    fi
done

if [[ -n "${preserved_tunnel_id}" ]]; then
    current_tunnel_id="$(docker inspect --format '{{.Id}}' "${TUNNEL_CONTAINER}" 2>/dev/null || true)"
    if [[ "${current_tunnel_id}" != "${preserved_tunnel_id}" ]]; then
        echo "Контейнер ${TUNNEL_CONTAINER} был пересоздан во время деплоя, хотя этого нельзя делать." >&2
        exit 1
    fi
    current_tunnel_started_at="$(docker inspect --format '{{.State.StartedAt}}' "${TUNNEL_CONTAINER}")"
    if [[ "${current_tunnel_started_at}" != "${preserved_tunnel_started_at}" ]]; then
        echo "Внимание: ${TUNNEL_CONTAINER} перезапустился сам во время деплоя. URL Quick Tunnel мог измениться."
    else
        echo "Контейнер ${TUNNEL_CONTAINER} не перезапускался, текущий URL сохранён."
    fi
fi

echo
echo "Временная HTTPS-ссылка: ${url}"
echo "Логин: rop"
echo "Пароль находится только в ${ACCESS_FILE}"
echo "Текущий URL туннеля: ./deploy/temporary-tunnel.sh --show-url"
