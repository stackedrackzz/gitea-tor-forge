#!/usr/bin/python3
"""Gitea system-webhook receiver: on repo creation or push, mirrors the
repo to wherever a per-repo policy file says it should go.

Policy file (POLICY_FILE, default /etc/gitea-tor-forge/mirror-policy.ini)
is a plain INI file, one section per repo name. A [default] section
covers any repo without its own section -- out of the box that's
"create it on GitHub under GITHUB_OWNER if missing, push over HTTPS with
GH_TOKEN, through Tor". Example:

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

Every repo is mirrored the same way regardless of policy: mirror-agent
clones the repo from Gitea's local API, then git-push --mirrors it to
the resolved remote, with auth and SOCKS tunneling applied per policy.
"""
import configparser
import hashlib
import hmac
import http.server
import json
import logging
import os
import shutil
import string
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mirror-agent")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
GITEA_INTERNAL_URL = os.environ.get("GITEA_INTERNAL_URL", "http://127.0.0.1:3000")
GITEA_API_TOKEN_FILE = os.environ["GITEA_API_TOKEN_FILE"]
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_VISIBILITY = os.environ.get("GITHUB_VISIBILITY", "private")
POLICY_FILE = os.environ.get("POLICY_FILE", "/etc/gitea-tor-forge/mirror-policy.ini")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))
GH_TOKEN = os.environ["GH_TOKEN"]
DEFAULT_TOR_PROXY = "socks5h://127.0.0.1:9050"

BUILTIN_DEFAULT_POLICY = {
    "remote_template": "https://github.com/${GITHUB_OWNER}/${repo}.git",
    "auth_type": "password",
    "auth_username": GITHUB_OWNER,
    "proxy": DEFAULT_TOR_PROXY,
    "github_autocreate": "true",
}


def gitea_token():
    with open(GITEA_API_TOKEN_FILE, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def gitea_api(method, path, body=None):
    url = f"{GITEA_INTERNAL_URL}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {gitea_token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def run(cmd, **kw):
    log.info("+ %s", " ".join(c if "@" not in c else "***REDACTED***" for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --- policy -----------------------------------------------------------

def load_policy_file():
    cfg = configparser.ConfigParser()
    if os.path.exists(POLICY_FILE):
        cfg.read(POLICY_FILE)
    return cfg


def resolve_policy(cfg, repo):
    if cfg.has_section(repo):
        raw = dict(cfg[repo])
    elif cfg.has_section("default"):
        raw = dict(cfg["default"])
    else:
        raw = dict(BUILTIN_DEFAULT_POLICY)

    remote_raw = raw.get("remote") or raw.get("remote_template", "")
    remote = string.Template(remote_raw).safe_substitute(repo=repo, GITHUB_OWNER=GITHUB_OWNER)

    return {
        "remote": remote,
        "auth_type": raw.get("auth_type", "password"),
        "auth_username": string.Template(raw.get("auth_username", "")).safe_substitute(
            repo=repo, GITHUB_OWNER=GITHUB_OWNER
        ),
        "auth_password_file": raw.get("auth_password_file", ""),
        "auth_keyfile": raw.get("auth_keyfile", ""),
        "proxy": raw.get("proxy", ""),
        "github_autocreate": raw.get("github_autocreate", "false").strip().lower() == "true",
        "github_visibility": raw.get("github_visibility", GITHUB_VISIBILITY),
    }


# --- github auto-create (only applies when the resolved remote is github.com) --

def github_repo_exists(full_name):
    r = run(["gh", "repo", "view", full_name, "--json", "name"])
    return r.returncode == 0


def github_repo_create(full_name, visibility):
    flag = "--private" if visibility == "private" else "--public"
    r = run(["gh", "repo", "create", full_name, flag, "-y"])
    if r.returncode != 0:
        raise RuntimeError(f"gh repo create failed: {r.stderr.strip()}")
    log.info("created github repo %s", full_name)


def maybe_autocreate_github(remote, policy):
    if not policy["github_autocreate"]:
        return
    host = urllib.parse.urlsplit(remote if "://" in remote else f"ssh://{remote}").hostname or ""
    if host != "github.com":
        return
    path = urllib.parse.urlsplit(remote if "://" in remote else f"ssh://{remote}").path
    if not path and "://" not in remote:
        # scp-like syntax, e.g. git@github.com:owner/repo.git
        path = remote.split(":", 1)[-1]
    full_name = path.strip("/").removesuffix(".git")
    if not github_repo_exists(full_name):
        github_repo_create(full_name, policy["github_visibility"])
    else:
        log.info("github repo %s already exists, reusing it", full_name)


# --- the actual mirror push --------------------------------------------

def build_gitea_clone_url(owner, repo):
    token = gitea_token()
    scheme, rest = GITEA_INTERNAL_URL.split("://", 1)
    return f"{scheme}://{urllib.parse.quote(token)}@{rest}/{owner}/{repo}.git"


def build_authenticated_push_url(remote, policy):
    if policy["auth_type"] != "password":
        return remote
    username = policy["auth_username"]
    if not username or not policy["auth_password_file"]:
        return remote
    with open(policy["auth_password_file"], "r", encoding="utf-8") as fh:
        password = fh.read().strip()
    parts = urllib.parse.urlsplit(remote)
    netloc = f"{urllib.parse.quote(username)}:{urllib.parse.quote(password)}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def proxy_env(base_env, policy):
    env = dict(base_env)
    proxy = policy["proxy"]
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
    else:
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            env.pop(k, None)
    return env


def ssh_command_for(policy):
    parts = ["ssh", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if policy["auth_keyfile"]:
        parts += ["-i", policy["auth_keyfile"]]
    proxy = policy["proxy"]
    if proxy:
        p = urllib.parse.urlsplit(proxy)
        parts += ["-o", f"ProxyCommand=socat - SOCKS5:{p.hostname}:%h:%p,socksport={p.port}"]
    return " ".join(parts)


def mirror_repo(owner, repo, policy):
    remote = policy["remote"]
    if not remote:
        log.warning("no remote resolved for %s/%s, skipping", owner, repo)
        return

    maybe_autocreate_github(remote, policy)

    workdir = tempfile.mkdtemp(prefix=f"mirror-{repo}-")
    try:
        clone_url = build_gitea_clone_url(owner, repo)
        r = run(["git", "clone", "--mirror", "--quiet", clone_url, workdir])
        if r.returncode != 0:
            raise RuntimeError(f"clone from gitea failed: {r.stderr.strip()}")

        env = os.environ.copy()
        if policy["auth_type"] == "keyfile":
            env["GIT_SSH_COMMAND"] = ssh_command_for(policy)
            push_url = remote
        else:
            env = proxy_env(env, policy)
            push_url = build_authenticated_push_url(remote, policy)

        r = run(["git", "push", "--mirror", "--quiet", push_url], cwd=workdir, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"push to {remote} failed: {r.stderr.strip()}")

        log.info("mirrored %s/%s -> %s", owner, repo, remote)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def ensure_mirror(owner, repo):
    cfg = load_policy_file()
    policy = resolve_policy(cfg, repo)
    mirror_repo(owner, repo, policy)


# --- webhook HTTP endpoint ----------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _reject(self, code, msg):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_POST(self):
        if self.path not in ("/webhook", "/webhook/"):
            self._reject(404, "not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        sig_header = self.headers.get("X-Gitea-Signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            log.warning("rejected webhook: bad signature")
            self._reject(401, "bad signature")
            return

        event = self.headers.get("X-Gitea-Event", "")
        try:
            payload = json.loads(raw_body)
        except ValueError:
            self._reject(400, "bad json")
            return

        # Ack immediately: the actual mirror push is network-bound (git
        # over Tor, gh API calls) and can take well beyond Gitea's own
        # webhook-delivery timeout. Blocking the response on it made
        # Gitea's client give up and close the connection mid-write.
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

        if event in ("repository", "push"):
            repo = payload.get("repository", {})
            action = payload.get("action")
            if event == "push" or action == "created":
                owner, name = repo.get("owner", {}).get("login"), repo.get("name")
                if owner and name:
                    threading.Thread(
                        target=_handle_mirror, args=(event, owner, name), daemon=True
                    ).start()
        else:
            log.info("ignoring event %s", event)


def _handle_mirror(event, owner, name):
    try:
        ensure_mirror(owner, name)
    except Exception:
        log.exception("failed handling %s event for %s/%s", event, owner, name)


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info("listening on :%d (policy file: %s)", LISTEN_PORT, POLICY_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()
