Name:           gitea-tor-forge
Version:        0.2.0
Release:        1%{?dist}
Summary:        Podman-composed Gitea forge that mirrors repos out over Tor
License:        MIT
URL:            https://example.invalid/gitea-tor-forge
BuildArch:      noarch

Source0:        %{name}-%{version}.tar.gz

BuildRequires:  systemd-rpm-macros

Requires:       podman >= 5.0
Requires:       podman-compose
Requires:       systemd

%description
gitea-tor-forge runs a Gitea forge (git server + web UI + API) as a
podman-compose stack, managed by systemd. A companion mirror-agent
container watches Gitea's system webhook (fired on every repo creation
and every push) and, per a per-repo policy file, ensures a mirror of
that repo exists at a remote git host -- creating it there first if
necessary -- and pushes to it. All outbound mirror traffic and GitHub
API calls are tunneled through a Tor SOCKS proxy by default, provided
by this stack's own tor sidecar (compose/compose.yaml's "tor" service,
built from tor/) -- there is no host-level Tor dependency.

Policy is per-repo: each entry chooses its own remote URL, SSH-keyfile
or username/password authentication, and its own SOCKS proxy (or none).
Repos with no explicit entry fall back to a default policy that mirrors
to a GitHub account over HTTPS through Tor, auto-creating the GitHub
repo if it doesn't already exist.

%prep
%setup -q

%build
# nothing to compile

%install
install -Dm0644 compose/compose.yaml %{buildroot}%{_datadir}/%{name}/compose/compose.yaml
install -Dm0644 mirror-agent/Containerfile %{buildroot}%{_datadir}/%{name}/mirror-agent/Containerfile
install -Dm0755 mirror-agent/entrypoint.sh %{buildroot}%{_datadir}/%{name}/mirror-agent/entrypoint.sh
install -Dm0755 mirror-agent/webhook_server.py %{buildroot}%{_datadir}/%{name}/mirror-agent/webhook_server.py

install -Dm0644 tor/Containerfile %{buildroot}%{_datadir}/%{name}/tor/Containerfile
install -Dm0644 tor/torrc %{buildroot}%{_datadir}/%{name}/tor/torrc
install -Dm0755 tor/entrypoint.sh %{buildroot}%{_datadir}/%{name}/tor/entrypoint.sh

install -Dm0644 packaging/systemd/gitea-tor-forge.service %{buildroot}%{_unitdir}/gitea-tor-forge.service

install -Dm0644 packaging/config/env.example %{buildroot}%{_sysconfdir}/%{name}/env
install -Dm0644 packaging/config/mirror-policy.ini.example %{buildroot}%{_sysconfdir}/%{name}/mirror-policy.ini

install -dm0700 %{buildroot}%{_sysconfdir}/%{name}/secrets
install -dm0700 %{buildroot}%{_sysconfdir}/%{name}/keys

%post
systemctl daemon-reload >/dev/null 2>&1 || :

# In a Qubes AppVM, `systemctl enable` alone is not reliable for getting
# a service running at boot (the qube's own init sequence doesn't always
# activate normal multi-user.target the way a bare-metal/VM boot would).
# /rw/config/rc.local is the Qubes-documented per-qube boot hook, so it's
# used here as the actual trigger; systemd enablement above is kept too
# as a no-op-safe fallback for non-Qubes hosts where it does work.
RC_LOCAL=/rw/config/rc.local
if [ -d /rw/config ]; then
    [ -f "$RC_LOCAL" ] || printf '#!/bin/sh\n' > "$RC_LOCAL"
    chmod 0755 "$RC_LOCAL"
    if ! grep -q '# BEGIN gitea-tor-forge' "$RC_LOCAL" 2>/dev/null; then
        cat >> "$RC_LOCAL" <<'HOOK'
# BEGIN gitea-tor-forge
systemctl start gitea-tor-forge.service &
# END gitea-tor-forge
HOOK
    fi
fi

cat <<'EOF'

gitea-tor-forge installed. Before starting it:

  1. Edit /etc/gitea-tor-forge/env: set POSTGRES_PASSWORD,
     GITEA_ADMIN_PASSWORD, WEBHOOK_SECRET, GITHUB_OWNER, GH_TOKEN, and
     PODMAN_SOCKET (check with:
       podman info --format '{{.Host.RemoteSocket.Path}}').
     TOR_SOCKS_PORT is optional (default 9050).
  2. Put a GitHub token (or other default remote's password) at
     /etc/gitea-tor-forge/secrets/github.token (root-only, mode 0600).
  3. Edit /etc/gitea-tor-forge/mirror-policy.ini to add any per-repo
     overrides (different remote host, SSH keyfile auth, or no tunnel).
     SSH keys go under /etc/gitea-tor-forge/keys/, secrets/tokens under
     /etc/gitea-tor-forge/secrets/ -- both are root:root 0700.
  4. systemctl enable --now gitea-tor-forge.service
     (this also happens automatically on next boot via
     /rw/config/rc.local on Qubes; run it manually now to start
     immediately without rebooting)

EOF

%preun
if [ "$1" -eq 0 ]; then
    systemctl disable --now gitea-tor-forge.service >/dev/null 2>&1 || :
    RC_LOCAL=/rw/config/rc.local
    if [ -f "$RC_LOCAL" ]; then
        sed -i '/# BEGIN gitea-tor-forge/,/# END gitea-tor-forge/d' "$RC_LOCAL" 2>/dev/null || :
    fi
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :

%files
%{_datadir}/%{name}/
%{_unitdir}/gitea-tor-forge.service
%dir %{_sysconfdir}/%{name}
%config(noreplace) %{_sysconfdir}/%{name}/env
%config(noreplace) %{_sysconfdir}/%{name}/mirror-policy.ini
%dir %attr(0700,root,root) %{_sysconfdir}/%{name}/secrets
%dir %attr(0700,root,root) %{_sysconfdir}/%{name}/keys

%changelog
* Wed Jul 29 2026 stackedrackzz <noreply@users.noreply.github.com> - 0.2.0-1
- Replace host-level Tor (torrc.d drop-in + tor.service) with a
  self-contained compose tor sidecar; TOR_SOCKS_PORT is configurable
* Tue Jul 28 2026 stackedrackzz <noreply@users.noreply.github.com> - 0.1.0-1
- Initial packaging
