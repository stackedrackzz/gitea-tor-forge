#!/bin/sh
# network_mode: host (matching postgres/gitea/mirror-agent in this
# stack) means this binds directly to the host's 127.0.0.1, not an
# isolated container network. SOCKS_PORT is overridable (default 9050,
# matching mirror-agent's/mirror-policy.ini's default proxy) so this
# doesn't have to collide with a host Tor already bound to that port.
#
# Verifies Tor is actually still alive before declaring ready -- a
# startup failure (e.g. a data-dir ownership mismatch) should be a
# clear container-level crash, not a silently-dead SOCKS port that just
# refuses every connection.
set -e

SOCKS_PORT="${SOCKS_PORT:-9050}"

/usr/sbin/tor -f /etc/tor/torrc SocksPort "127.0.0.1:${SOCKS_PORT}" &
TOR_PID=$!

i=0
while [ $i -lt 30 ]; do
  if ! kill -0 "$TOR_PID" 2>/dev/null; then
    echo "entrypoint: tor exited during startup -- see logs above" >&2
    exit 1
  fi
  if grep -q "Bootstrapped 100%" /var/log/tor/notices.log 2>/dev/null; then
    break
  fi
  sleep 1
  i=$((i + 1))
done

if ! kill -0 "$TOR_PID" 2>/dev/null; then
  echo "entrypoint: tor exited during startup -- see logs above" >&2
  exit 1
fi

echo "entrypoint: tor bootstrapped, SOCKS on 127.0.0.1:${SOCKS_PORT}"
trap 'kill $TOR_PID 2>/dev/null' EXIT INT TERM
wait "$TOR_PID"
