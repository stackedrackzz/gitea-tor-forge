#!/bin/sh
# Run on dom0. NOT part of the gitea-tor-forge RPM's own install --
# that package installs ON the qube this script creates, running Gitea
# itself; qube creation and dom0's qrexec policy are dom0-only actions
# on a different machine, so they can't be a %post of that same RPM.
#
# Creates a dedicated AppVM ("git-server" by default) for
# gitea-tor-forge, tags it "gitea-forge-host", installs
# qrexec-tcp-bridge + gitea-tor-forge onto it from rpm-repo, and grants
# qubes tagged "devel" access to it on Gitea's HTTP (3000) and SSH
# (2222) ports via qrexec-tcp-bridge's local.ConnectTCP -- NOT real IP
# networking (qvm-firewall can't express "let other qubes reach this
# one" at all; it only filters a qube's own outbound traffic). A devel
# qube reaches Gitea via:
#   qrexec-client-vm git-server local.ConnectTCP+3000   # HTTP
#   qrexec-client-vm git-server local.ConnectTCP+2222   # SSH (git clone)
#
# UNTESTED against live qubesd -- no real dom0 reachable from this
# package's development environment (same limitation as
# qubes-rpc-user's and dom0-podman-tcp's dom0-only scripts). Written
# against documented qvm-create/qvm-tags/qvm-run syntax, not verified
# live.
set -eu

QUBE="${QUBE:-git-server}"
TEMPLATE="${TEMPLATE:?set TEMPLATE to an existing Fedora-based template name, e.g. fedora-42}"

qvm-create --class AppVM --template "$TEMPLATE" --label black "$QUBE"
qvm-tags "$QUBE" add gitea-forge-host

qvm-run -u root --pass-io "$QUBE" '
  set -eu
  curl -fsSL -o /etc/yum.repos.d/stackedrackzz.repo https://stackedrackzz.github.io/rpm-repo/stackedrackzz.repo
  rpm --import https://stackedrackzz.github.io/rpm-repo/RPM-GPG-KEY-stackedrackzz
  dnf install -y qrexec-tcp-bridge gitea-tor-forge
'

# The fragment + regeneration happen here on dom0, not inside the qube
# above -- qrexec-tcp-bridge must already be installed on dom0 itself
# (e.g. via qubes-rpc-user) for generate-policy.sh to exist at this path.
install -Dm0644 "$(dirname "$0")/access.conf.d/20-gitea-forge.conf" \
    /etc/qrexec-tcp-bridge/access.conf.d/20-gitea-forge.conf
/usr/share/qrexec-tcp-bridge/generate-policy.sh

echo "provision-git-server-qube.sh: done. Remember to tag any qube that"
echo "should reach $QUBE: qvm-tags <vmname> add devel"
