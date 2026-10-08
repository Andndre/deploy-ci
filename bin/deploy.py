"""Shared runner-side SSH/rsync deployment. Requires only Bash/coreutils remotely."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from build_artifact import verify_build
from measure import record, timed


def relative_path(value):
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ValueError(f"Unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError(f"Unsafe relative path: {value!r}")
    return value


def target_path(value):
    value = value.strip().lstrip("\ufeff").strip()
    if not value:
        raise ValueError("HOSTINGER_TARGET_DIR secret is empty or not set")
    value = value.strip().strip("'\"").rstrip("/")
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ValueError(f"TARGET_DIR must be an absolute path (got {value!r})")
    if len(path.parts) < 4:
        raise ValueError(f"TARGET_DIR path is too shallow (got {len(path.parts)} parts: {path.parts!r})")
    if ".." in path.parts:
        raise ValueError(f"TARGET_DIR contains '..' traversal (got {value!r})")
    if str(path) != value:
        raise ValueError(f"TARGET_DIR is not normalized (normalized={str(path)!r}, original={value!r})")
    if any(ord(c) < 32 for c in value):
        bad = [ord(c) for c in value if ord(c) < 32]
        raise ValueError(f"TARGET_DIR contains control characters (ASCII: {bad})")
    return value


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def asset_inventory(source, directories):
    lines = []
    for directory in directories:
        relative_path(directory)
        folder = source / directory
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError(f"Missing immutable asset directory: {directory}")
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Symlink in immutable assets: {path}")
            if path.is_file():
                name = relative_path(path.relative_to(source).as_posix())
                lines.append(f"{digest(path)}  {name}")
    if not lines:
        raise ValueError("Build produced no immutable assets; check the project profile")
    return "\n".join(lines)


def parse_rsync_stats(output):
    patterns = {
        "files_listed": r"Number of files:\s*([0-9,]+)",
        "files_created": r"Number of created files:\s*([0-9,]+)",
        "files_deleted": r"Number of deleted files:\s*([0-9,]+)",
        "files_transferred": r"Number of regular files transferred:\s*([0-9,]+)",
        "total_file_size": r"Total file size:\s*([0-9,]+)",
        "total_transferred_size": r"Total transferred file size:\s*([0-9,]+)",
        "literal_data": r"Literal data:\s*([0-9,]+)",
        "matched_data": r"Matched data:\s*([0-9,]+)",
        "bytes_sent": r"Total bytes sent:\s*([0-9,]+)",
        "bytes_received": r"Total bytes received:\s*([0-9,]+)",
    }
    stats = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            stats[key] = int(match.group(1).replace(",", ""))
    return stats


class Deployment:
    def __init__(self, config, environment=None):
        self.config = config
        self.env = os.environ if environment is None else environment
        self.target = target_path(self.secret("HOSTINGER_TARGET_DIR"))
        host = self.secret("HOSTINGER_SSH_HOST").strip()
        user = self.secret("HOSTINGER_SSH_USER").strip()
        port = self.secret("HOSTINGER_SSH_PORT", "65002").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
            raise ValueError("Invalid SSH host")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", user):
            raise ValueError("Invalid SSH user")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid SSH port")
        self.destination = f"{user}@{host}"
        self.rsync_destination = f"{user}@[{host}]" if ":" in host else self.destination
        self.temp = Path(self.env.get("RUNNER_TEMP", tempfile.gettempdir()))
        self.key = self.temp / "deploy-ci-key"
        self.known_hosts = self.temp / "deploy-ci-known-hosts"
        self.state = self.temp / "deploy-ci-run.json"
        self.ssh = ["ssh", "-p", port, "-i", str(self.key), "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
                    "-o", f"UserKnownHostsFile={self.known_hosts}"]
        self.source = Path(config["output_dir"]).resolve()
        self.directories = [relative_path(d) for d in config["immutable_dirs"]]
        if not self.directories:
            raise ValueError("At least one immutable directory is required")
        if config["profile"] not in {"laravel-vite", "static-vite", "sveltekit-static", "static"}:
            raise ValueError("Unsupported deployment profile")
        if config["keep_releases"] < 2 or config["retention_days"] < 1:
            raise ValueError("Keep at least two releases and one day of assets")

    def secret(self, name, default=""):
        # Windows PowerShell can prepend a UTF-8 BOM when piping to gh.
        return self.env.get(name, default).lstrip("\ufeff")

    def credentials(self):
        # Host keys are supplied out of band; ssh-keyscan during a deploy cannot
        # establish the server's identity.
        for path, variable in [(self.key, "HOSTINGER_SSH_KEY"),
                               (self.known_hosts, "HOSTINGER_SSH_KNOWN_HOSTS")]:
            value = self.secret(variable)
            if not value.strip():
                raise ValueError(f"Missing secret: {variable}")
            value = value.replace("\r\n", "\n").replace("\r", "\n").rstrip()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
            path.chmod(0o600)

    def remote(self, phase, run, inventory=None, incoming_bytes=0, composer_lock_hash="", force_vendor_sync=False):
        script = Path(__file__).with_name("remote-deploy.sh").read_text(encoding="utf-8")
        if inventory is not None:
            script = "incoming=$(cat <<'HOSTINGER_ASSET_INVENTORY'\n" + inventory + "\nHOSTINGER_ASSET_INVENTORY\n)\n" + script
        arguments = [self.target, phase, run, ":".join(self.directories),
                     str(self.config["keep_releases"]), str(self.config["retention_days"]),
                     self.config["profile"], str(self.config["migrate"]).lower(),
                     str(self.config.get("max_retained_files", 10000)),
                     str(self.config.get("max_retained_bytes", 512 * 1024 * 1024)), str(incoming_bytes),
                     self.config.get("php_version", ""),
                     str(self.env.get("HOSTINGER_HTTP_VERIFIED", "false")).lower(),
                     str(self.config.get("maintenance", False)).lower(),
                     self.config.get("php_web_user", "auto"),
                     composer_lock_hash,
                     str(force_vendor_sync).lower()]
        with timed("remote-" + phase):
            started = time.monotonic()
            remote_seconds = 0
            with subprocess.Popen(self.ssh + [self.destination, "bash -s -- " + shlex.join(arguments)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True) as process:
                process.stdin.write(script)
                process.stdin.close()
                for line in process.stdout:
                    if line.startswith("HOSTINGER_TIMING "):
                        metric = json.loads(line.removeprefix("HOSTINGER_TIMING "))
                        if metric.get("stage") == "vendor-reusable":
                            self.vendor_reusable = bool(metric.get("reusable", False))
                        remote_seconds += metric["seconds"]
                        record(**metric)
                    else:
                        print(line, end="", flush=True)
                code = process.wait()
                record("ssh-overhead-" + phase, max(0, time.monotonic() - started - remote_seconds),
                       "failure" if code else "success", note="SSH connection, script transport and uninstrumented overhead")
                if code:
                    raise subprocess.CalledProcessError(code, process.args)

    def rsync(self, source, target, excludes=(), delete=False, *, includes=(), stage_name=None):
        stage = stage_name or ("rsync-assets" if not delete else "rsync-application")
        command = ["rsync", "-rlzp", "--chmod=Du=rwx,Dgo=rx,Fu=rwX,Fgo=rX",
                   "--checksum", "--delay-updates", "--safe-links", "--protect-args",
                   "--stats",
                   "-e", shlex.join(self.ssh)]
        bwlimit = str(self.secret("HOSTINGER_RSYNC_BWLIMIT") or self.config.get("rsync_bwlimit", "")).strip()
        if bwlimit and bwlimit != "0":
            if not re.fullmatch(r"[0-9]+[kKmMgGtT]?", bwlimit):
                raise ValueError(f"Invalid rsync bwlimit format: {bwlimit!r}")
            command.append(f"--bwlimit={bwlimit}")
        if delete:
            command.append("--delete-delay")
        command.extend(f"--include=/{value}" for value in includes)
        command.extend(f"--exclude=/{value}" for value in excludes)
        command.extend([str(source) + "/", self.rsync_destination + ":" + target + "/"])
        started = time.monotonic()
        outcome = "failure"
        stats = {}
        try:
            process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if process.returncode != 0:
                print(f"Rsync {stage} failed with exit code {process.returncode}:\n{process.stdout}", flush=True)
                raise subprocess.CalledProcessError(process.returncode, command, output=process.stdout)
            outcome = "success"
            stats = parse_rsync_stats(process.stdout)
            summary = (f"Rsync {stage} complete: {stats.get('files_transferred', 0)} files transferred of "
                       f"{stats.get('files_listed', 0)} listed, sent {stats.get('bytes_sent', 0)} bytes, "
                       f"received {stats.get('bytes_received', 0)} bytes")
            print(summary, flush=True)
            return stats
        finally:
            record(stage, time.monotonic() - started, outcome, **stats)

    def transfer(self):
        if self.config.get("require_build_artifact", False):
            with timed("artifact-preflight"):
                verify_build(self.source, self.env, self.config)
        if not self.source.is_dir():
            raise ValueError("Build output directory does not exist")
        if self.config["profile"] != "laravel-vite" and not any(self.source.rglob("*.html")):
            raise ValueError("Static build output must contain HTML; SSR Node requires a separate profile")
        if self.config["profile"] == "laravel-vite" and not (self.source / "public/build/manifest.json").is_file():
            raise ValueError("Laravel Vite manifest not found at public/build/manifest.json")
        inventory = asset_inventory(self.source, self.directories)
        run = uuid.uuid4().hex
        incoming_bytes = sum((self.source / line.split("  ", 1)[1]).stat().st_size for line in inventory.splitlines())
        composer_lock_file = self.source / "composer.lock"
        composer_lock_hash = digest(composer_lock_file) if composer_lock_file.is_file() else ""
        force_vendor_sync = str(self.secret("HOSTINGER_FORCE_VENDOR_SYNC") or self.config.get("force_vendor_sync", False)).lower() == "true"
        print("Deployment stage: remote prerequisites, asset collisions and retention budget", flush=True)
        self.remote("prepare", run, inventory, incoming_bytes, composer_lock_hash, force_vendor_sync)
        self.state.write_text(json.dumps({"run": run}), encoding="utf-8")
        # Immutable files arrive before any manifest/HTML/application publication.
        for directory in self.directories:
            print(f"Deployment stage: upload immutable assets ({directory})", flush=True)
            self.rsync(self.source / directory, self.target + "/" + directory)
        includes = []
        excludes = list(self.config["protected_paths"]) + list(self.directories)
        if self.config.get("profile") == "laravel-vite" and getattr(self, "vendor_reusable", False):
            print("Deployment stage: production vendor matches verified composer.lock; syncing autoloader metadata while preserving packages", flush=True)
            includes.extend(["vendor", "vendor/autoload.php", "vendor/composer/***"])
            excludes.append("vendor/***")
        elif self.config.get("profile") == "laravel-vite":
            print("Deployment stage: vendor dependencies changed or unverified; synchronizing vendor with checksum", flush=True)
        print("Deployment stage: publish application files and manifests (in place)", flush=True)
        self.rsync(self.source, self.target, excludes=excludes, includes=includes, delete=True)
        print("Application transfer complete; remote optimization follows", flush=True)
        self.remote("optimize", run, composer_lock_hash=composer_lock_hash)

    def cleanup(self):
        self.remote("cleanup", json.loads(self.state.read_text(encoding="utf-8"))["run"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["transfer", "cleanup"])
    parser.add_argument("--config", default=str(Path(__file__).with_name("profile.json")))
    args = parser.parse_args()
    deployment = Deployment(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    try:
        if args.phase == "transfer" and deployment.config.get("require_build_artifact", False):
            verify_build(deployment.source, deployment.env, deployment.config)
        deployment.credentials()
        getattr(deployment, args.phase)()
    finally:
        deployment.key.unlink(missing_ok=True)
        deployment.known_hosts.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
