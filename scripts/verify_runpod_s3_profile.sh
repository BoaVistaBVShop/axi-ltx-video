#!/usr/bin/env bash
set -euo pipefail

profile_name="runpod-s3"
credentials_file="/root/.aws/credentials"
config_file="/root/.aws/config"

access_value="$(/usr/local/bin/aws configure get aws_access_key_id --profile "$profile_name" 2>/dev/null || true)"
secret_value="$(/usr/local/bin/aws configure get aws_secret_access_key --profile "$profile_name" 2>/dev/null || true)"

access_ok=false
secret_ok=false

case "$access_value" in
  user_*) access_ok=true ;;
esac

case "$secret_value" in
  rps_*) secret_ok=true ;;
esac

if [[ -f "$credentials_file" ]]; then
  chmod 600 "$credentials_file"
  credentials_mode="$(stat -c %a "$credentials_file")"
else
  credentials_mode="missing"
fi

if [[ -f "$config_file" ]]; then
  chmod 600 "$config_file"
  config_mode="$(stat -c %a "$config_file")"
else
  config_mode="missing"
fi

printf 'PROFILE=%s\n' "$profile_name"
printf 'ACCESS_FORMAT_OK=%s\n' "$access_ok"
printf 'SECRET_FORMAT_OK=%s\n' "$secret_ok"
printf 'CREDENTIALS_MODE=%s\n' "$credentials_mode"
printf 'CONFIG_MODE=%s\n' "$config_mode"

[[ "$access_ok" == true ]]
[[ "$secret_ok" == true ]]
