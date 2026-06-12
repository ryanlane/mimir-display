#!/usr/bin/env bash
set -euo pipefail

# Interactive helper for manually configuring a display to reach a Mimir stack.
# Intended as a fallback when auto-bootstrap or mDNS discovery is unavailable.

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
DEFAULT_ENV_FILE="${REPO_ROOT}/.env"
INSTALLED_ENV_FILE="/etc/mimir-display/.env"

ENV_FILE=""
PLATFORM_URL=""
MQTT_HOST=""
MQTT_PORT="1883"
MQTT_USERNAME=""
MQTT_PASSWORD=""
DISPLAY_NAME=""
DISPLAY_LOCATION=""
NON_INTERACTIVE=0

info() { printf "\033[36m[INFO]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[WARN]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[ERR]\033[0m %s\n" "$*" >&2; }

usage() {
  cat <<'EOF'
Usage: ./scripts/setup_connection.sh [options]

Options:
  --env-file PATH          Target env file (default: /etc/mimir-display/.env if present, else ./.env)
  --platform-url URL       External API base URL, for example http://192.168.1.50:5000
  --mqtt-host HOST         External MQTT hostname/IP
  --mqtt-port PORT         External MQTT port (default: 1883)
  --mqtt-username USER     Optional MQTT username
  --mqtt-password PASS     Optional MQTT password
  --display-name NAME      Optional display name
  --display-location LOC   Optional display location
  --non-interactive        Fail instead of prompting for missing values
  --help                   Show this help text

This script writes the minimum values a display needs to talk to the service:
  PLATFORM_URL, MQTT_BROKER_HOST, MQTT_BROKER_PORT, optional MQTT credentials,
  and optional display metadata.
EOF
}

choose_env_file() {
  if [[ -n "$ENV_FILE" ]]; then
    return
  fi

  if [[ -f "$INSTALLED_ENV_FILE" ]]; then
    ENV_FILE="$INSTALLED_ENV_FILE"
  else
    ENV_FILE="$DEFAULT_ENV_FILE"
  fi
}

read_env_value() {
  local file="$1" key="$2"
  if [[ -f "$file" ]]; then
    awk -F'=' -v k="$key" '$0 !~ /^\s*#/ && $1==k {print substr($0, index($0, "=")+1); exit}' "$file"
  fi
}

set_env_value() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    printf '%s=%s\n' "$key" "$value" > "$file"
    return
  fi
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

clear_env_value() {
  local file="$1" key="$2"
  if [[ -f "$file" ]]; then
    sed -i "/^${key}=/d" "$file"
  fi
}

prompt_if_empty() {
  local current="$1" prompt="$2"
  if [[ -n "$current" ]]; then
    printf '%s' "$current"
    return
  fi
  if [[ $NON_INTERACTIVE -eq 1 ]]; then
    err "Missing required value: ${prompt}"
    exit 2
  fi
  local entered
  read -rp "${prompt}: " entered
  printf '%s' "$entered"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --platform-url) PLATFORM_URL="$2"; shift 2 ;;
    --mqtt-host) MQTT_HOST="$2"; shift 2 ;;
    --mqtt-port) MQTT_PORT="$2"; shift 2 ;;
    --mqtt-username) MQTT_USERNAME="$2"; shift 2 ;;
    --mqtt-password) MQTT_PASSWORD="$2"; shift 2 ;;
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --display-location) DISPLAY_LOCATION="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --help) usage; exit 0 ;;
    *) err "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

choose_env_file
mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

current_platform="$(read_env_value "$ENV_FILE" "PLATFORM_URL")"
current_mqtt_host="$(read_env_value "$ENV_FILE" "MQTT_BROKER_HOST")"
current_mqtt_port="$(read_env_value "$ENV_FILE" "MQTT_BROKER_PORT")"
current_mqtt_user="$(read_env_value "$ENV_FILE" "MQTT_USERNAME")"
current_display_name="$(read_env_value "$ENV_FILE" "DISPLAY_NAME")"
current_display_location="$(read_env_value "$ENV_FILE" "DISPLAY_LOCATION")"

if [[ $NON_INTERACTIVE -eq 0 ]]; then
  info "Configuring display connection details in $ENV_FILE"
  info "Press Enter to keep an existing value when one is shown in brackets."
fi

if [[ -z "$PLATFORM_URL" && -n "$current_platform" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "Platform URL [${current_platform}]: " PLATFORM_URL
  PLATFORM_URL=${PLATFORM_URL:-$current_platform}
fi
PLATFORM_URL="$(prompt_if_empty "$PLATFORM_URL" "Platform URL (for example http://192.168.1.50:5000)")"

if [[ -z "$MQTT_HOST" && -n "$current_mqtt_host" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "MQTT host [${current_mqtt_host}]: " MQTT_HOST
  MQTT_HOST=${MQTT_HOST:-$current_mqtt_host}
fi
MQTT_HOST="$(prompt_if_empty "$MQTT_HOST" "MQTT host (for example 192.168.1.50)")"

if [[ "$MQTT_PORT" == "1883" && -n "$current_mqtt_port" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "MQTT port [${current_mqtt_port}]: " entered_port
  MQTT_PORT=${entered_port:-$current_mqtt_port}
fi
MQTT_PORT="$(prompt_if_empty "$MQTT_PORT" "MQTT port")"

if [[ -z "$MQTT_USERNAME" && -n "$current_mqtt_user" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "MQTT username [${current_mqtt_user}] (optional): " MQTT_USERNAME
  MQTT_USERNAME=${MQTT_USERNAME:-$current_mqtt_user}
elif [[ -z "$MQTT_USERNAME" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "MQTT username (optional): " MQTT_USERNAME
fi

if [[ -z "$MQTT_PASSWORD" && -n "$(read_env_value "$ENV_FILE" "MQTT_PASSWORD")" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rsp "MQTT password [saved] (optional, leave blank to keep): " MQTT_PASSWORD
  printf '\n'
elif [[ -z "$MQTT_PASSWORD" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rsp "MQTT password (optional): " MQTT_PASSWORD
  printf '\n'
fi

if [[ -z "$DISPLAY_NAME" && -n "$current_display_name" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "Display name [${current_display_name}] (optional): " DISPLAY_NAME
  DISPLAY_NAME=${DISPLAY_NAME:-$current_display_name}
elif [[ -z "$DISPLAY_NAME" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "Display name (optional): " DISPLAY_NAME
fi

if [[ -z "$DISPLAY_LOCATION" && -n "$current_display_location" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "Display location [${current_display_location}] (optional): " DISPLAY_LOCATION
  DISPLAY_LOCATION=${DISPLAY_LOCATION:-$current_display_location}
elif [[ -z "$DISPLAY_LOCATION" && $NON_INTERACTIVE -eq 0 ]]; then
  read -rp "Display location (optional): " DISPLAY_LOCATION
fi

set_env_value "$ENV_FILE" "PLATFORM_URL" "$PLATFORM_URL"
set_env_value "$ENV_FILE" "MQTT_BROKER_HOST" "$MQTT_HOST"
set_env_value "$ENV_FILE" "MQTT_BROKER_PORT" "$MQTT_PORT"

if [[ -n "$MQTT_USERNAME" ]]; then
  set_env_value "$ENV_FILE" "MQTT_USERNAME" "$MQTT_USERNAME"
else
  clear_env_value "$ENV_FILE" "MQTT_USERNAME"
fi

if [[ -n "$MQTT_PASSWORD" ]]; then
  set_env_value "$ENV_FILE" "MQTT_PASSWORD" "$MQTT_PASSWORD"
fi

if [[ -n "$DISPLAY_NAME" ]]; then
  set_env_value "$ENV_FILE" "DISPLAY_NAME" "$DISPLAY_NAME"
fi

if [[ -n "$DISPLAY_LOCATION" ]]; then
  set_env_value "$ENV_FILE" "DISPLAY_LOCATION" "$DISPLAY_LOCATION"
fi

info "Saved display connection details to $ENV_FILE"
echo
echo "Applied values:"
echo "  PLATFORM_URL=${PLATFORM_URL}"
echo "  MQTT_BROKER_HOST=${MQTT_HOST}"
echo "  MQTT_BROKER_PORT=${MQTT_PORT}"
if [[ -n "$MQTT_USERNAME" ]]; then
  echo "  MQTT_USERNAME=${MQTT_USERNAME}"
fi
if [[ -n "$DISPLAY_NAME" ]]; then
  echo "  DISPLAY_NAME=${DISPLAY_NAME}"
fi
if [[ -n "$DISPLAY_LOCATION" ]]; then
  echo "  DISPLAY_LOCATION=${DISPLAY_LOCATION}"
fi
echo
echo "Next step: restart the display service or rerun the client."
echo "  sudo systemctl restart mimir-display"