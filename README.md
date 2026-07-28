# gitea-tor-forge

A self-hosted Gitea forge, deployed as a `podman compose` stack and managed
by systemd, where every repo -- new or existing -- gets mirrored out to a
remote git host automatically, on every push. Mirror traffic (and GitHub
API calls) go through a local Tor SOCKS proxy by default. Packaged as an
RPM, built with `mock`.

## How it works

```
push/create repo -> Gitea (system webhook, fires for ALL repos)
                       -> mirror-agent (HMAC-verified webhook receiver)
                            -> resolve per-repo policy (mirror-policy.ini)
                            -> if remote is github.com and missing: gh repo create
                            -> git clone --mirror from Gitea (local, token auth)
                            -> git push --mirror to the resolved remote
                                 (keyfile or password auth, through SOCKS
                                 proxy per policy, or direct if none set)
```

There is no per-repo setup step. Gitea's **system webhook** (as opposed to
a per-repo webhook) fires for every repository in the instance, including
ones that existed before this was ever installed -- the first push after
install lazily mirrors them too.

mirror-agent does the actual push itself rather than using Gitea's
built-in push-mirror feature. Gitea's push-mirror API can't cleanly
express "use this specific SSH keyfile" or "use this specific proxy" per
repo, which is the whole point of the per-repo policy file below.

## Layout

- `compose/compose.yaml` -- the stack: `postgres`, `gitea`, `mirror-agent`.
  All three run with `network_mode: host` so they can reach the qube's own
  Tor SOCKS proxy at `127.0.0.1:9050` directly, and so mirror-agent can
  reach Gitea's HTTP API without fighting podman's bridge networking.
- `mirror-agent/` -- the webhook receiver (`webhook_server.py`, stdlib
  Python only) and its bootstrap (`entrypoint.sh`), packaged into a
  Containerfile.
- `packaging/gitea-tor-forge.spec` -- RPM spec, built with `mock`.
- `packaging/systemd/gitea-tor-forge.service` -- wraps `podman compose up
  -d` / `down` as a systemd oneshot unit.
- `packaging/tor/50-gitea-tor-forge.conf` -- torrc.d drop-in pinning the
  SOCKS port the stack depends on.
- `packaging/config/env.example`, `mirror-policy.ini.example` -- config
  templates installed to `/etc/gitea-tor-forge/`.

## Per-repo mirror policy

`/etc/gitea-tor-forge/mirror-policy.ini` is a plain INI file, one section
per Gitea repo name. Repos with no matching section fall back to
`[default]`.

```ini
[default]
remote_template = https://github.com/${GITHUB_OWNER}/${repo}.git
auth_type = password
auth_username = ${GITHUB_OWNER}
auth_password_file = /etc/gitea-tor-forge/secrets/github.token
proxy = socks5h://127.0.0.1:9050
github_autocreate = true

[some-repo]
remote = ssh://git@example.com:2222/some-repo.git
auth_type = keyfile
auth_keyfile = /etc/gitea-tor-forge/keys/some-repo_ed25519
proxy = socks5h://127.0.0.1:9050
```

- `auth_type = password` embeds `auth_username`/the contents of
  `auth_password_file` as HTTPS basic auth on the push URL.
- `auth_type = keyfile` uses `auth_keyfile` as `ssh -i` for an `ssh://`
  remote, with `IdentitiesOnly=yes` and `StrictHostKeyChecking=accept-new`.
- `proxy` is any `socks5h://host:port` (routed via `HTTPS_PROXY`/`ALL_PROXY`
  for password auth, or an `ssh -o ProxyCommand=socat ... SOCKS5:...` for
  keyfile auth). Leave it blank to push directly, no tunnel.
- `github_autocreate` only takes effect when the resolved remote's host is
  `github.com`; it runs `gh repo view`/`gh repo create` first.

Secrets (`auth_password_file`, `auth_keyfile`) must live under
`/etc/gitea-tor-forge/secrets/` or `/etc/gitea-tor-forge/keys/`
(`root:root 0700`, mounted read-only into mirror-agent) -- never inline
them in the policy file itself.

## Building the RPM with mock

```sh
cd gitea-tor-forge
mkdir -p ~/rpmbuild/SOURCES
tar --transform 's,^,gitea-tor-forge-0.1.0/,' \
    -czf ~/rpmbuild/SOURCES/gitea-tor-forge-0.1.0.tar.gz \
    compose mirror-agent packaging

rpmbuild -bs packaging/gitea-tor-forge.spec

mock -l | grep fedora   # pick the current release
mock -r fedora-43-x86_64 --rebuild \
    ~/rpmbuild/SRPMS/gitea-tor-forge-0.1.0-1*.src.rpm
```

## Installing in the qube

```sh
sudo dnf install ./gitea-tor-forge-0.1.0-1*.noarch.rpm

sudo $EDITOR /etc/gitea-tor-forge/env
#   set POSTGRES_PASSWORD, GITEA_ADMIN_PASSWORD, WEBHOOK_SECRET,
#   GITHUB_OWNER, GH_TOKEN, and PODMAN_SOCKET (see below)

install -m 0600 /path/to/github-token /etc/gitea-tor-forge/secrets/github.token
sudo $EDITOR /etc/gitea-tor-forge/mirror-policy.ini   # add per-repo overrides

sudo systemctl enable --now podman.socket   # if not already active
sudo systemctl enable --now tor.service gitea-tor-forge.service
```

`PODMAN_SOCKET` must point at a *live* podman API socket -- mirror-agent
uses it (via `podman exec`) for one-time bootstrap: creating the Gitea
admin user, minting an API token, and registering the system webhook.
Check with `podman info --format '{{.Host.RemoteSocket.Path}}'`, and make
sure `podman.socket` is actually active first (`systemctl --user status
podman.socket` for rootless) -- a bind-mount of a socket path that isn't
live yet silently becomes an empty directory instead of erroring, which
looks like a connection-refused failure inside the container.

Gitea's own SSRF protection blocks webhook deliveries to loopback
addresses by default; the compose file sets
`GITEA__webhook__ALLOWED_HOST_LIST=loopback` to allow the one loopback
target (mirror-agent) it's actually meant to call.

### Starting on boot in a Qubes AppVM

`systemctl enable` alone isn't reliable for getting a service running at
boot inside a Qubes AppVM -- the qube's own init sequence doesn't always
carry that through the way a normal VM/bare-metal boot would. `%post`
therefore also appends a guarded block to `/rw/config/rc.local` (Qubes'
documented per-qube boot hook) that runs `systemctl start tor.service
gitea-tor-forge.service` on every boot; `%preun` removes it again on
uninstall. The `systemctl enable` call is kept too, harmlessly, for
non-Qubes hosts where it works normally. This assumes the RPM is
installed directly in the target qube (a StandaloneVM, or a
TemplateVM used by only that one qube) -- `/rw` is per-qube private
storage, so installing into a shared TemplateVM won't propagate the
hook to other AppVMs based on it.

## Known limitations

- `git push --mirror` doesn't update the *destination's* `HEAD` symref.
  For a raw `ssh://` remote this means a plain `git clone` of the mirror
  needs `-b <branch>` (or the remote's HEAD fixed with `git symbolic-ref`)
  until something else pushes and updates it -- there's no push refspec
  for HEAD, and GitHub-style remotes manage their own default-branch
  metadata out of band instead, so they aren't affected.
- Gitea's own baked-in administrative `sshd` (distinct from Gitea's own
  git-over-SSH listener, which is a separate Go implementation controlled
  by `SSH_PORT`/`SSH_LISTEN_PORT`) can't bind privileged port 22 under
  rootless podman and crash-loops harmlessly in the logs. It isn't used
  by this stack (mirror-agent talks to Gitea over HTTP only) and can be
  ignored.
- The compose stack assumes it's the only thing using host networking on
  this qube (ports 5432, `GITEA_HTTP_PORT`/`GITEA_SSH_PORT`, 8099). On a
  shared host, override `GITEA_HTTP_PORT`/`GITEA_SSH_PORT` in `env`.
