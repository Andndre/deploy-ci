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
import uuid


def relative_path(value):
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ValueError(f"Unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError(f"Unsafe relative path: {value!r}")
    return value


def target_path(value):
    value = value.strip().rstrip("/")
    path = PurePosixPath(value)
    if (not path.is_absolute() or len(path.parts) < 4 or ".." in path.parts
            or str(path) != value or any(ord(c) < 32 for c in value)):
        raise ValueError("TARGET_DIR must be a normalized absolute path to an existing application directory")
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


class Deployment:
    def __init__(self, config, environment=None):
        self.config = config
        self.env = os.environ if environment is None else environment
        self.target = target_path(self.env.get("HOSTINGER_TARGET_DIR", ""))
        host = self.env.get("HOSTINGER_SSH_HOST", "").strip()
        user = self.env.get("HOSTINGER_SSH_USER", "").strip()
        port = self.env.get("HOSTINGER_SSH_PORT", "65002").strip()
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

    def credentials(self):
        # Host keys are supplied out of band; ssh-keyscan during a deploy cannot
        # establish the server's identity.
        for path, variable in [(self.key, "HOSTINGER_SSH_KEY"),
                               (self.known_hosts, "HOSTINGER_SSH_KNOWN_HOSTS")]:
            value = self.env.get(variable, "")
            if not value.strip():
                raise ValueError(f"Missing secret: {variable}")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(value.rstrip() + "\n")
            path.chmod(0o600)

    def remote(self, phase, run, inventory=None, incoming_bytes=0):
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
                     str(self.config.get("maintenance", False)).lower()]
        subprocess.run(self.ssh + [self.destination, "bash -s -- " + shlex.join(arguments)],
                       input=script, text=True, check=True)

    def rsync(self, source, target, excludes=(), delete=False):
        command = ["rsync", "-rlz", "--checksum", "--delay-updates", "--safe-links", "--protect-args",
                   "-e", shlex.join(self.ssh)]
        if delete:
            command.append("--delete-delay")
        command.extend(f"--exclude=/{value}" for value in excludes)
        command.extend([str(source) + "/", self.rsync_destination + ":" + target + "/"])
        subprocess.run(command, check=True)

    def transfer(self):
        if not self.source.is_dir():
            raise ValueError("Build output directory does not exist")
        if self.config["profile"] != "laravel-vite" and not any(self.source.rglob("*.html")):
            raise ValueError("Static build output must contain HTML; SSR Node requires a separate profile")
        if self.config["profile"] == "laravel-vite" and not (self.source / "public/build/manifest.json").is_file():
            raise ValueError("Laravel Vite manifest not found at public/build/manifest.json")
        inventory = asset_inventory(self.source, self.directories)
        run = uuid.uuid4().hex
        incoming_bytes = sum((self.source / line.split("  ", 1)[1]).stat().st_size for line in inventory.splitlines())
        print("Deployment stage: remote prerequisites, asset collisions and retention budget", flush=True)
        self.remote("prepare", run, inventory, incoming_bytes)
        self.state.write_text(json.dumps({"run": run}), encoding="utf-8")
        # Immutable files arrive before any manifest/HTML/application publication.
        for directory in self.directories:
            print(f"Deployment stage: upload immutable assets ({directory})", flush=True)
            self.rsync(self.source / directory, self.target + "/" + directory)
        print("Deployment stage: publish application files and manifests (in place)", flush=True)
        self.rsync(self.source, self.target,
                   excludes=self.config["protected_paths"] + self.directories, delete=True)
        print("Application transfer complete; remote optimization follows", flush=True)
        self.remote("optimize", run)

    def cleanup(self):
        self.remote("cleanup", json.loads(self.state.read_text(encoding="utf-8"))["run"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["transfer", "cleanup"])
    parser.add_argument("--config", default=str(Path(__file__).with_name("profile.json")))
    args = parser.parse_args()
    deployment = Deployment(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    try:
        deployment.credentials()
        getattr(deployment, args.phase)()
    finally:
        deployment.key.unlink(missing_ok=True)
        deployment.known_hosts.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
