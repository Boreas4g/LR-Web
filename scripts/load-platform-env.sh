#!/usr/bin/env bash

load_lr_platform_environment() {
  local platform_env_file="${LR_PLATFORM_ENV_FILE:-/etc/lr-platform/platform.env}"
  local pki_env_file="${LR_PLATFORM_PKI_ENV_FILE:-/etc/lr-platform/platform-pki.env}"

  if [[ -r "$platform_env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$platform_env_file"
    set +a
  fi

  LR_PLATFORM_MODE="${LR_PLATFORM_MODE:-local}"
  export LR_PLATFORM_MODE
  if [[ "$LR_PLATFORM_MODE" == production && -r "$pki_env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$pki_env_file"
    set +a
  fi
}
