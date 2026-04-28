#!/usr/bin/env python3
"""
humlab-project-database-deployment helper script.

Usage:
  ./deploy.py install   — clone repos, configure, and start everything on a fresh machine
  ./deploy.py save-db   — dump the running MongoDB into showcase-mongodb-dump.archive
"""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
ENV_FILE = ROOT / ".env"
ARCHIVE = ROOT / "showcase-mongodb-dump.archive"
MONGO_EXPORTS_DIR = ROOT / "mounts" / "mongo" / "exports"
MONGO_DATA_DIR = ROOT / "mounts" / "mongo" / "data"

# ── helpers ──────────────────────────────────────────────────────────────────

def print_step(msg):
    print(f"\n\033[1;34m==> {msg}\033[0m")

def print_ok(msg):
    print(f"\033[0;32m  ✓ {msg}\033[0m")

def print_warn(msg):
    print(f"\033[0;33m  ! {msg}\033[0m")

def print_err(msg):
    print(f"\033[0;31m  ✗ {msg}\033[0m", file=sys.stderr)

def prompt(label, default=None, secret=False):
    hint = f" [{default}]" if default and not secret else (" [generated]" if secret and default else "")
    display = f"{label}{hint}: "
    if secret:
        import getpass
        value = getpass.getpass(display).strip()
    else:
        value = input(display).strip()
    return value or default or ""

def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)

def compose(*args):
    """Run a podman/docker compose command, merging the dev override when MODE=dev."""
    binary = detect_compose_binary()
    env = load_env()
    files = ["-f", "docker-compose.yml"]
    if env.get("MODE", "prod").lower() == "dev":
        files += ["-f", "docker-compose.dev.yml"]
    run([*binary, *files, *args], cwd=ROOT)

def detect_compose_binary():
    """
    Return the first compose binary that (a) exists and (b) can actually reach
    the container runtime.  'version' exits 0 without a daemon, so we use 'ps'
    (which reads docker-compose.yml and talks to the socket) as the real test.
    """
    tried = []
    for candidate in (["podman", "compose"], ["docker", "compose"], ["docker-compose"]):
        if not shutil.which(candidate[0]):
            continue
        # Quick check: does the subcommand/plugin exist?
        ver = subprocess.run(candidate + ["version"], capture_output=True)
        if ver.returncode != 0:
            continue
        # Real check: can it reach the container runtime?
        ps = subprocess.run(candidate + ["ps"], capture_output=True, cwd=ROOT)
        if ps.returncode == 0:
            return candidate
        tried.append(" ".join(candidate))

    # Nothing worked — print actionable guidance
    print_err("Could not connect to a container runtime (tried: " + ", ".join(tried or ["none found"]) + ").")
    if shutil.which("podman"):
        xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        sock = f"{xdg}/podman/podman.sock"
        print_err("Podman is installed but the socket is not running. Fix with:")
        print_err("    systemctl --user enable --now podman.socket")
        print_err(f"    export DOCKER_HOST=unix://{sock}")
        print_err("Then re-run this script.")
    elif shutil.which("docker"):
        print_err("Docker is installed but the daemon is not running. Fix with:")
        print_err("    sudo systemctl enable --now docker")
    else:
        print_err("Install podman or docker, then re-run this script.")
    sys.exit(1)

REPOS = [
    {
        "url": "https://github.com/humlab/humlab-project-database-client",
        "dir": "client",
    },
    {
        "url": "https://github.com/humlab/humlab-project-database-server",
        "dir": "server",
    },
]

# ── steps ────────────────────────────────────────────────────────────────────

def check_prerequisites():
    print_step("Checking prerequisites")
    binary = detect_compose_binary()
    print_ok(f"Compose binary: {' '.join(binary)}")

    if not shutil.which("git"):
        print_err("'git' not found. Please install git and retry.")
        sys.exit(1)
    print_ok("git found")

    if not ARCHIVE.exists():
        print_warn(f"MongoDB archive not found: {ARCHIVE.name}  (skipping restore)")
    else:
        print_ok(f"MongoDB archive found: {ARCHIVE.name}")


def clone_repos():
    print_step("Cloning / updating source repositories")
    for repo in REPOS:
        target = ROOT / repo["dir"]
        if (target / ".git").exists():
            print(f"  {repo['dir']}/ already cloned — pulling latest …")
            run(["git", "-C", str(target), "pull", "--ff-only"])
            print_ok(f"{repo['dir']}/ up to date")
        elif target.exists() and any(target.iterdir()):
            print_warn(
                f"  {repo['dir']}/ exists but is not a git repo — skipping clone.\n"
                f"  Remove or empty the directory to allow a fresh clone."
            )
        else:
            target.mkdir(parents=True, exist_ok=True)
            print(f"  Cloning {repo['url']} → {repo['dir']}/ …")
            run(["git", "clone", repo["url"], str(target)])
            print_ok(f"{repo['dir']}/ cloned")

def create_env():
    print_step("Configuring environment (.env)")

    if ENV_FILE.exists():
        answer = input(f"  {ENV_FILE.name} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print_ok("Keeping existing .env")
            return
        if any(MONGO_DATA_DIR.iterdir()) if MONGO_DATA_DIR.exists() else False:
            print_warn(
                "MongoDB data directory already exists. If you change the\n"
                "  MONGO_ROOT_PASSWORD, the existing database will reject the new\n"
                "  credentials. To start fresh, stop the containers and delete:\n"
                f"    {MONGO_DATA_DIR.relative_to(ROOT)}"
            )
            answer2 = input("  Continue anyway? [y/N] ").strip().lower()
            if answer2 != "y":
                sys.exit(0)

    print("  Press Enter to accept [defaults] or type a new value.\n")

    mongo_user  = prompt("  MongoDB root username", default="root")
    mongo_pass  = prompt("  MongoDB root password", default=secrets.token_urlsafe(16), secret=True)
    me_user     = prompt("  Mongo-Express basic-auth username", default="admin")
    me_pass     = prompt("  Mongo-Express basic-auth password", default=secrets.token_urlsafe(16), secret=True)
    admin_user  = prompt("  Admin panel username", default="admin")
    admin_pass  = prompt("  Admin panel password", default=secrets.token_urlsafe(16), secret=True)
    jwt_secret  = secrets.token_urlsafe(32)
    print(f"  Admin JWT secret: [auto-generated]")

    print()
    port_app           = prompt("  Public port (nginx)", default="80")
    port_mongo_express = prompt("  Mongo Express port (host)", default="8081")
    mode               = prompt("  Mode (dev/prod)", default="prod")

    lines = [
        f"MONGO_ROOT_USERNAME={mongo_user}",
        f"MONGO_ROOT_PASSWORD={mongo_pass}",
        f"ME_CONFIG_BASICAUTH_USERNAME={me_user}",
        f"ME_CONFIG_BASICAUTH_PASSWORD={me_pass}",
        f"ADMIN_USERNAME={admin_user}",
        f"ADMIN_PASSWORD={admin_pass}",
        f"ADMIN_JWT_SECRET={jwt_secret}",
        f"PORT_APP={port_app}",
        f"PORT_MONGO_EXPRESS={port_mongo_express}",
        f"MODE={mode}",
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    # Restrict permissions so other users can't read secrets
    ENV_FILE.chmod(0o600)
    print_ok(".env written (permissions: 600)")

def create_directories():
    print_step("Creating mount directories")
    for d in (MONGO_DATA_DIR, MONGO_EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
        print_ok(str(d.relative_to(ROOT)))

def copy_archive():
    if not ARCHIVE.exists():
        return
    print_step("Copying MongoDB archive into exports mount")
    dest = MONGO_EXPORTS_DIR / ARCHIVE.name
    shutil.copy2(ARCHIVE, dest)
    print_ok(f"Copied to {dest.relative_to(ROOT)}")

def reset_mongo_data():
    """Stop mongo and wipe its data dir so it re-initialises with current .env credentials."""
    if not MONGO_DATA_DIR.exists() or not any(MONGO_DATA_DIR.iterdir()):
        return  # Nothing to reset

    print_warn(
        "Existing MongoDB data found. During a fresh install this data must be\n"
        "  removed so MongoDB can initialise with the credentials in .env.\n"
        f"  Directory: {MONGO_DATA_DIR.relative_to(ROOT)}"
    )
    answer = input("  Wipe existing MongoDB data and start fresh? [y/N] ").strip().lower()
    if answer != "y":
        print_err("Cannot continue: MongoDB would reject the credentials in .env.")
        sys.exit(1)

    print_step("Stopping mongo container and wiping data")
    binary = detect_compose_binary()
    subprocess.run([*binary, "stop", "mongo"], cwd=ROOT, capture_output=True)
    # MongoDB files are owned by the mongodb user inside the container (uid 999),
    # so we can't delete them as the host user. Run a temporary container instead.
    run([
        *binary, "run", "--rm",
        "-v", f"{MONGO_DATA_DIR}:/data",
        "alpine",
        "sh", "-c", "rm -rf /data/* /data/.[!.]*",
    ], cwd=ROOT)
    print_ok("MongoDB data wiped — will reinitialise with new credentials")


def start_mongo():
    print_step("Starting MongoDB container")
    compose("up", "-d", "--build", "mongo")

def wait_for_mongo():
    """
    Wait in two phases:
    1. Wait until mongod accepts connections (ping without credentials).
       This can succeed during MongoDB's init phase before the root user exists.
    2. Wait until the root user credentials from .env are actually valid.
       This ensures mongorestore can authenticate.
    """
    print_step("Waiting for MongoDB to become ready")
    env = load_env()
    binary = detect_compose_binary()

    # Phase 1: wait for mongod to accept connections at all
    for attempt in range(1, 31):
        result = subprocess.run(
            [*binary, "exec", "-T", "mongo", "mongosh", "--quiet",
             "--eval", "db.adminCommand('ping')"],
            cwd=ROOT, capture_output=True,
        )
        if result.returncode == 0:
            break
        print(f"  Phase 1 — attempt {attempt}/30, waiting 2 s …", end="\r")
        time.sleep(2)
    else:
        print_err("MongoDB did not start in time. Check logs with: podman compose logs mongo")
        sys.exit(1)

    # Phase 2: wait until the root user from .env is accepted
    # MongoDB Docker initialises users during first-run setup; this can take
    # several seconds after the port becomes reachable.
    for attempt in range(1, 31):
        result = subprocess.run(
            [
                *binary, "exec", "-T", "mongo",
                "mongosh", "--quiet",
                f"--username={env['MONGO_ROOT_USERNAME']}",
                f"--password={env['MONGO_ROOT_PASSWORD']}",
                "--authenticationDatabase=admin",
                "--eval", "db.adminCommand('ping')",
            ],
            cwd=ROOT, capture_output=True,
        )
        if result.returncode == 0:
            print_ok(f"MongoDB ready and credentials verified (attempt {attempt})")
            return
        print(f"  Phase 2 — attempt {attempt}/30, waiting 2 s …", end="\r")
        time.sleep(2)
    print_err("MongoDB credentials from .env never authenticated. Check logs with: podman compose logs mongo")
    sys.exit(1)

def restore_mongodb():
    if not ARCHIVE.exists():
        print_warn("No archive found — skipping MongoDB restore.")
        return
    print_step("Restoring MongoDB from archive")
    env = load_env()
    binary = detect_compose_binary()
    archive_path = f"/mongo-exports/{ARCHIVE.name}"
    run(
        [
            *binary, "exec", "-T", "mongo",
            "mongorestore",
            f"--username={env['MONGO_ROOT_USERNAME']}",
            f"--password={env['MONGO_ROOT_PASSWORD']}",
            "--authenticationDatabase=admin",
            "--drop",
            "--nsExclude=admin.*",  # Don't restore admin users — would overwrite current credentials
            f"--archive={archive_path}",
        ],
        cwd=ROOT,
    )
    print_ok("Restore complete")

def start_all():
    print_step("Starting all services")
    compose("up", "-d", "--build")
    print_ok("All services started")


def save_db():
    """Dump the running MongoDB into showcase-mongodb-dump.archive."""
    print_step("Saving MongoDB to archive")

    env = load_env()
    if not env.get("MONGO_ROOT_USERNAME") or not env.get("MONGO_ROOT_PASSWORD"):
        print_err(".env not found or missing credentials. Run './deploy.py install' first.")
        sys.exit(1)

    binary = detect_compose_binary()

    # Ensure the mongo container is running
    result = subprocess.run(
        [*binary, "ps", "--services", "--filter", "status=running"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if "mongo" not in result.stdout.splitlines():
        print_err("The mongo container is not running. Start it with 'podman compose up -d mongo' first.")
        sys.exit(1)

    MONGO_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = ARCHIVE.name
    container_path = f"/mongo-exports/{archive_name}"

    print(f"  Dumping to {container_path} inside container …")
    run(
        [
            *binary, "exec", "-T", "mongo",
            "mongodump",
            f"--username={env['MONGO_ROOT_USERNAME']}",
            f"--password={env['MONGO_ROOT_PASSWORD']}",
            "--authenticationDatabase=admin",
            "--nsExclude=admin.*",  # Don't dump admin users — avoids credential conflicts on restore
            f"--archive={container_path}",
        ],
        cwd=ROOT,
    )
    print_ok(f"Dump written inside container at {container_path}")

    # Copy out of the exports mount to the repo root
    src = MONGO_EXPORTS_DIR / archive_name
    if src.resolve() != ARCHIVE.resolve():
        shutil.copy2(src, ARCHIVE)
        print_ok(f"Copied to {ARCHIVE.relative_to(ROOT)}")
    else:
        print_ok(f"Archive already at {ARCHIVE.relative_to(ROOT)}")

    size_mb = ARCHIVE.stat().st_size / (1024 * 1024)
    print_ok(f"Done — {size_mb:.1f} MB")

def print_summary():
    env = load_env()
    port_app           = env.get("PORT_APP", "80")
    port_mongo_express = env.get("PORT_MONGO_EXPRESS", "8081")
    print("\n\033[1;32m  Deployment complete!\033[0m\n")
    print("  Services:")
    print(f"    App (via nginx)    →  http://localhost:{port_app}")
    print(f"    API path           →  http://localhost:{port_app}/api")
    print(f"    Mongo Express      →  http://localhost:{port_mongo_express}")
    print(f"\n  Mongo Express login:  {env.get('ME_CONFIG_BASICAUTH_USERNAME')} / <your password>")
    print(f"  Admin login:          {env.get('ADMIN_USERNAME')} / <your password>")
    print()

def load_env():
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

# ── main ─────────────────────────────────────────────────────────────────────

def cmd_up():
    env = load_env()
    mode = env.get("MODE", "prod").lower()
    print(f"\033[1m=== Humlab Project Database — Up ({mode}) ===\033[0m")
    check_prerequisites()
    compose("up", "-d", "--build")
    print_summary()


def cmd_down():
    print("\033[1m=== Humlab Project Database — Down ===\033[0m")
    check_prerequisites()
    compose("down")
    print_ok("All services stopped")


def cmd_install():
    print("\033[1m=== Humlab Project Database — Install ===\033[0m")
    check_prerequisites()
    clone_repos()
    create_env()
    create_directories()
    copy_archive()
    reset_mongo_data()
    start_mongo()
    wait_for_mongo()
    restore_mongodb()
    start_all()
    print_summary()


def cmd_save_db():
    print("\033[1m=== Humlab Project Database — Save DB ===\033[0m")
    save_db()


def main():
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="Humlab Project Database deployment helper",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("install", help="Set up and start everything on a fresh machine")
    subparsers.add_parser("up",      help="Start all services (uses MODE from .env)")
    subparsers.add_parser("down",    help="Stop all services")
    subparsers.add_parser("save-db", help="Dump the running MongoDB into showcase-mongodb-dump.archive")

    args = parser.parse_args()

    if args.command == "install":
        cmd_install()
    elif args.command == "up":
        cmd_up()
    elif args.command == "down":
        cmd_down()
    elif args.command == "save-db":
        cmd_save_db()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(1)
