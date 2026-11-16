#!/usr/bin/env bash
# Create .env from .env.example (if missing) and fill the Authelia secrets with
# random values. Existing real secrets are never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example"
fi

for key in AUTHELIA_SESSION_SECRET AUTHELIA_STORAGE_ENCRYPTION_KEY AUTHELIA_JWT_SECRET; do
  if grep -qE "^${key}=(GANTI_|$)" .env; then
    value=$(openssl rand -hex 32)
    sed -i "s|^${key}=.*|${key}=${value}|" .env
    echo "Generated ${key}"
  fi
done
echo "Done. Review .env (PDF_HOSTNAME, PDF_GROUP, AUTH_BACKEND, AD_*)."
