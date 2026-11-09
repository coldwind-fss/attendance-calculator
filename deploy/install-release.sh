#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 ]]; then
  printf 'Usage: %s RELEASE_ARCHIVE RELEASE_ID PYTHON_EXECUTABLE\n' "$0" >&2
  exit 2
fi

archive=$(realpath "$1")
release_id=$2
python=$(realpath "$3")
root=/opt/attendance-calculator
release="$root/releases/$release_id"
service=attendance-calculator.service
environment=/etc/attendance-calculator/workbench.env

if [[ ! $release_id =~ ^[0-9a-f]{7,40}$ ]]; then
  printf 'RELEASE_ID must be a Git commit identifier\n' >&2
  exit 2
fi
if [[ ! -f $environment ]]; then
  printf 'Create %s before installing the release\n' "$environment" >&2
  exit 2
fi

"$python" -c 'import sqlite3, sys; sys.exit(not (sys.version_info >= (3, 11) and sqlite3.sqlite_version_info >= (3, 38)))'
install -d -m 0755 "$root/releases"
mkdir "$release"
tar -xzf "$archive" -C "$release"
cd "$release"
"$python" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r backend/requirements.txt
.venv/bin/python deploy/check-assets.py "$environment"
npm ci --prefix backend/adapters/attendance --omit=dev
npm ci --prefix frontend
npm run --prefix frontend build
.venv/bin/python -m unittest discover -s backend/tests -v

if ! getent passwd attendance-calculator >/dev/null; then
  useradd --system --user-group --home-dir /var/lib/attendance-calculator --shell /sbin/nologin attendance-calculator
fi
install -m 0644 deploy/attendance-calculator.service /etc/systemd/system/attendance-calculator.service
systemctl daemon-reload
previous=$(readlink "$root/current" || true)

activate() {
  ln -sfn "$1" "$root/next" &&
    mv -Tf "$root/next" "$root/current" &&
    systemctl restart "$service"
}

if activate "$release"; then
  for _ in {1..30}; do
    if curl -fsS http://127.0.0.1:18405/api/v1/health/ready >/dev/null; then
      systemctl enable "$service"
      printf 'Release %s is ready on 127.0.0.1:18405\n' "$release_id"
      exit 0
    fi
    sleep 1
  done
fi

if [[ -n $previous ]]; then
  activate "$previous"
else
  systemctl stop "$service"
fi
printf 'Release %s did not pass readiness\n' "$release_id" >&2
exit 1
