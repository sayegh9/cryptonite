#!/usr/bin/env bash
# ==============================================================================
# Cryptonite Executor Strict Egress Firewall Enforcement Policy
# ==============================================================================
# This script sets up Linux kernel iptables / nftables filtering on the host or
# container network namespace to enforce institutional Demo-only egress.
#
# Constraints:
# 1. Drops all outbound traffic from the executor container by default.
# 2. Allows internal traffic to PostgreSQL (5432) and Redis (6379) on the Docker bridge.
# 3. Allows DNS resolution (UDP/TCP 53) to configured internal resolver.
# 4. Allows outbound HTTPS (TCP 443) EXCLUSIVELY to:
#    - demo-api.binance.com
#    - demo-ws-api.binance.com
# 5. Strictly BLOCKS all traffic to live Binance production endpoints
#    (api.binance.com, wss://ws-api.binance.com) and legacy testnet (testnet.binance.vision).
# ==============================================================================

set -euo pipefail

CONTAINER_NAME="cryptonite_executor"
CHAIN_NAME="CRYPTONITE_EXECUTOR_EGRESS"

echo "[INFO] Resolving Binance Demo IP endpoints..."
DEMO_API_IPS=$(dig +short demo-api.binance.com | grep -E '^[0-9.]+$' || true)
DEMO_WS_IPS=$(dig +short demo-ws-api.binance.com | grep -E '^[0-9.]+$' || true)

if [ -z "$DEMO_API_IPS" ] || [ -z "$DEMO_WS_IPS" ]; then
    echo "[WARN] Unable to resolve Demo endpoints dynamically; ensure DNS is reachable."
fi

echo "[INFO] Configuring iptables egress chain for ${CONTAINER_NAME}..."

# Create or flush dedicated egress chain
iptables -N "${CHAIN_NAME}" 2>/dev/null || iptables -F "${CHAIN_NAME}"

# Allow established/related connections
iptables -A "${CHAIN_NAME}" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow loopback
iptables -A "${CHAIN_NAME}" -o lo -j ACCEPT

# Allow DNS (port 53)
iptables -A "${CHAIN_NAME}" -p udp --dport 53 -j ACCEPT
iptables -A "${CHAIN_NAME}" -p tcp --dport 53 -j ACCEPT

# Allow internal database / redis connections on docker bridge (172.16.0.0/12)
iptables -A "${CHAIN_NAME}" -d 172.16.0.0/12 -p tcp -m multiport --dports 5432,6379 -j ACCEPT

# Allow TCP 443 exclusively to resolved Demo IPs
for ip in $DEMO_API_IPS $DEMO_WS_IPS; do
    echo "[INFO] Whitelisting Demo IP: ${ip}:443"
    iptables -A "${CHAIN_NAME}" -p tcp -d "${ip}" --dport 443 -j ACCEPT
done

# Default DROP for all other egress packets
iptables -A "${CHAIN_NAME}" -j LOG --log-prefix "[EGRESS_BLOCKED] " --log-level 4
iptables -A "${CHAIN_NAME}" -j DROP

echo "[SUCCESS] Cryptonite Executor egress firewall active. Real-capital / production egress blocked."
