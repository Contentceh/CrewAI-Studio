#!/bin/sh
set -eu

if [ -r /run/runtime-secrets/postgres_password ]; then
    POSTGRES_PASSWORD=$(cat /run/runtime-secrets/postgres_password)
    export DB_URL="postgresql://crewai:${POSTGRES_PASSWORD}@db:5432/crewai"
fi

exec streamlit run ./app/app.py --server.headless true --server.address 0.0.0.0
