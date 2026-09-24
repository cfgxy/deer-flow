#!/usr/bin/env bash
#
# render-nginx-conf.sh — Generate docker/nginx/nginx.local.conf from its
# template, substituting the local dev frontend upstream port
# (DEERFLOW_FRONTEND_PORT, default 3000). Shared by serve.sh and nginx.sh so
# the two never drift — see nginx.sh header comment.
#
# The generated file is gitignored; only the .template is tracked.

set -e

REPO_ROOT="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd -P)"

: "${DEERFLOW_FRONTEND_PORT:=3000}"

sed "s/__DEERFLOW_FRONTEND_PORT__/${DEERFLOW_FRONTEND_PORT}/g" \
    "$REPO_ROOT/docker/nginx/nginx.local.conf.template" \
    > "$REPO_ROOT/docker/nginx/nginx.local.conf"
