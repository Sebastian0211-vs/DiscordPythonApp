#!/usr/bin/env bash
# Creates the Docker network that sandbox runs use and firewalls it:
#   allowed:  the public internet (downloads, APIs, web scraping...)
#   blocked:  the VPS itself (all its IPs and ports), other containers, private and
#             internal ranges, cloud metadata (169.254.169.254)
#
# Usage (on the VPS):
#   sudo bash sandbox/network-setup.sh             create the network and apply the rules now
#   sudo bash sandbox/network-setup.sh --install   same, plus re-apply at boot and on every Docker restart
#   sudo bash sandbox/network-setup.sh --status    show the network and rule counters
#
# Then set SANDBOX_NETWORK=remotepy-net in .env and restart the bot.
set -euo pipefail

NET=remotepy-net
SUBNET=172.30.254.0/24
BRIDGE=br-remotepy
CHAIN=REMOTEPY-SANDBOX
BLOCKED=(
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16
  172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
)

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

status() {
  docker network inspect "$NET" --format 'network {{.Name}}: subnet {{(index .IPAM.Config 0).Subnet}}' || true
  iptables -L "$CHAIN" -v -n || true
  iptables -L INPUT -v -n | grep -E "pkts|$BRIDGE" || true
}

if [[ ${1:-} == "--status" ]]; then
  status
  exit 0
fi

if ! iptables -L DOCKER-USER -n >/dev/null 2>&1; then
  echo "iptables chain DOCKER-USER not found. Is Docker running with its default iptables firewall?" >&2
  exit 1
fi

# 1. Network: fixed subnet and bridge name so the rules can target it; no container-to-container traffic.
if ! docker network inspect "$NET" >/dev/null 2>&1; then
  docker network create --driver bridge --subnet "$SUBNET" \
    --opt com.docker.network.bridge.name="$BRIDGE" \
    --opt com.docker.network.bridge.enable_icc=false \
    --label remotepy=sandbox "$NET" >/dev/null
  echo "Created Docker network $NET ($SUBNET)"
fi

# 2. Forwarded traffic leaving the sandbox network: drop anything not headed to the public internet.
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
for cidr in "${BLOCKED[@]}"; do
  iptables -A "$CHAIN" -d "$cidr" -j DROP
done
iptables -A "$CHAIN" -j RETURN
iptables -C DOCKER-USER -i "$BRIDGE" -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER -i "$BRIDGE" -j "$CHAIN"

# 3. Traffic from the sandbox network to the VPS itself (any of its addresses, public IP included).
iptables -C INPUT -i "$BRIDGE" -j DROP 2>/dev/null || iptables -I INPUT -i "$BRIDGE" -j DROP

echo "Firewall rules applied for $NET"

if [[ ${1:-} == "--install" ]]; then
  script=$(readlink -f "$0")
  install -m 0755 "$script" /usr/local/sbin/remotepy-network-setup
  cat > /etc/systemd/system/remotepy-network.service <<EOF
[Unit]
Description=Firewall for remotePython sandbox network
After=docker.service
Requires=docker.service
# Re-run whenever Docker (re)starts, since Docker may rebuild its firewall chains.
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/remotepy-network-setup
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target docker.service
EOF
  systemctl daemon-reload
  systemctl enable remotepy-network.service >/dev/null
  systemctl start remotepy-network.service
  echo "Installed remotepy-network.service: rules are re-applied at boot and whenever Docker restarts"
fi
