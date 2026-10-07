"""Seal and restore a complete frontend output for one commit/workflow run."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from urllib.parse import unquote, urljoin, urlsplit


class StaticDependencies(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.in_script = False

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script":
            self.in_script = True
            if attributes.get("src"):
                self.references.append(attributes["src"])
        if tag == "link" and attributes.get("rel") in {"stylesheet", "modulepreload"} and attributes.get("href"):
            self.references.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, text):
        if self.in_script:
            self.references.extend(re.findall(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)", text))


def contract(config):
    profile = config["profile"]
    if profile not in {"laravel-vite", "static-vite", "sveltekit-static", "static"}:
        raise ValueError("Unsupported build profile")
    directory = "public/build" if profile == "laravel-vite" else config["output_dir"]
    safe_name(directory)
    if directory == ".":
        raise ValueError("Build output must be a dedicated directory")
    return dict(profile=profile, build_dir=directory, immutable_dirs=config["immutable_dirs"])


def build_root(source, config):
    directory = contract(config)["build_dir"]
    root = source
    for part in PurePosixPath(directory).parts:
        root = root / part
        if root.is_symlink():
            raise ValueError(f"Symlink in build ancestry: {root}")
    return root


def extra_roots(source, config):
    if config["profile"] != "laravel-vite":
        return []
    roots = []
    for directory in config["immutable_dirs"]:
        safe_name(directory)
        if directory == "public/build" or directory.startswith("public/build/"):
            continue
        root = source
        for part in PurePosixPath(directory).parts:
            root = root / part
            if root.is_symlink():
                raise ValueError(f"Symlink in build ancestry: {root}")
        roots.append((directory, root))
    return roots


def extra_inventory(source, config):
    return {directory + "/" + name: checksum
            for directory, root in extra_roots(source, config)
            for name, checksum in inventory(root).items()}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or name == "." or "\\" in name or ":" in name or any(ord(c) < 32 for c in name)
            or path.is_absolute() or ".." in path.parts or str(path) != name):
        raise ValueError(f"Unsafe build path: {name!r}")
    return name


def inventory(root):
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Frontend build is missing or symlinked")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in build: {path}")
        if path.is_file():
            files[safe_name(path.relative_to(root).as_posix())] = digest(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported build file: {path}")
    if not files:
        raise ValueError("Frontend build contains no files")
    return files


def validate_build(root, config):
    files = inventory(root)
    if config["profile"] == "laravel-vite":
        manifests = ["manifest.json"]
    else:
        if not any(name.endswith(".html") for name in files):
            raise ValueError("Static build output must contain HTML")
        manifests = [name for name in ("manifest.json", ".vite/manifest.json") if name in files]
        for page in (name for name in files if name.endswith(".html")):
            parser = StaticDependencies()
            parser.feed((root / page).read_text(encoding="utf-8"))
            for reference in parser.references:
                url = urlsplit(urljoin("https://build.invalid/" + page, reference))
                if url.netloc != "build.invalid":
                    continue
                name = safe_name(unquote(url.path).lstrip("/"))
                if any(name.startswith(directory + "/") for directory in config["immutable_dirs"]) and name not in files:
                    raise ValueError(f"Missing static dependency: {name}")
    for filename in manifests:
        manifest = json.loads((root / filename).read_text(encoding="utf-8"))
        # Static outputs may use manifest.json as a web app manifest.
        vite_manifest = bool(manifest) and isinstance(manifest, dict) and all(isinstance(v, dict) and "file" in v for v in manifest.values())
        if filename == "manifest.json" and config["profile"] != "laravel-vite" and not vite_manifest:
            continue
        if not vite_manifest or not any(chunk.get("isEntry") for chunk in manifest.values()):
            raise ValueError("Missing Vite entry in manifest")
        for chunk in manifest.values():
            for name in [chunk["file"], *chunk.get("css", []), *chunk.get("assets", [])]:
                if safe_name(name) not in files:
                    raise ValueError(f"Missing Vite dependency: {name}")
            for key in [*chunk.get("imports", []), *chunk.get("dynamicImports", [])]:
                if key not in manifest:
                    raise ValueError(f"Missing Vite chunk: {key}")
    if "fonts-manifest.json" in files:
        fonts = json.loads((root / "fonts-manifest.json").read_text(encoding="utf-8"))
        for name in file_references(fonts):
            if safe_name(name) not in files:
                raise ValueError(f"Missing font dependency: {name}")
    for directory in config["immutable_dirs"]:
        safe_name(directory)
        prefix = "public/build/" if config["profile"] == "laravel-vite" else ""
        # Custom Laravel outputs are validated separately by extra_inventory.
        if directory == "public/build" and prefix:
            continue
        if prefix and not directory.startswith(prefix):
            continue
        relative = directory.removeprefix(prefix)
        if not any(name.startswith(relative + "/") for name in files):
            raise ValueError(f"Missing immutable build assets: {directory}")
    return files


def file_references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "file":
                yield child
            else:
                yield from file_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from file_references(child)


def identity(environment):
    sha, run = environment.get("GITHUB_SHA", ""), environment.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or not run.isdigit():
        raise ValueError("Artifact requires GITHUB_SHA and GITHUB_RUN_ID")
    return dict(commit=sha, run_id=run)


def provenance_path(environment):
    return Path(environment["RUNNER_TEMP"]) / "hostinger-build-provenance.json"


def verify_build(source, environment, config):
    metadata = json.loads(provenance_path(environment).read_text(encoding="utf-8"))
    if any(metadata.get(key) != value for key, value in identity(environment).items()):
        raise ValueError("Build artifact belongs to another commit/workflow run")
    if metadata.get("contract") != contract(config):
        raise ValueError("Build artifact belongs to another output/profile")
    root = build_root(source, config) if config["profile"] == "laravel-vite" else source
    if metadata["files"] != validate_build(root, config):
        raise ValueError("Build bytes differ from the verified artifact")
    if metadata.get("extra_files", {}) != extra_inventory(source, config):
        raise ValueError("Additional build bytes differ from the verified artifact")


def seal(source, folder, environment, config):
    root = build_root(source, config)
    files = validate_build(root, config)
    extra_files = extra_inventory(source, config)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "build.tar"
    with tarfile.open(archive, "w") as stream:
        for name in files:
            stream.add(root / name, arcname=contract(config)["build_dir"] + "/" + name, recursive=False)
        for name in extra_files:
            stream.add(source / name, arcname=name, recursive=False)
    metadata = dict(version=1, **identity(environment), contract=contract(config), files=files,
                    extra_files=extra_files, archive_sha256=digest(archive))
    (folder / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    output = environment.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"sha256={metadata['archive_sha256']}\n")
    print(f"Sealed {len(files) + len(extra_files)} files for {metadata['commit']}; SHA256 {metadata['archive_sha256']}")


def restore(source, folder, expected, environment, config):
    metadata = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    archive = folder / "build.tar"
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected or metadata.get("archive_sha256") != expected:
        raise ValueError("Artifact checksum differs from verify job output")
    if metadata.get("version") != 1 or any(metadata.get(key) != value for key, value in identity(environment).items()):
        raise ValueError("Artifact belongs to another commit/workflow run")
    if metadata.get("contract") != contract(config):
        raise ValueError("Artifact belongs to another output/profile")
    root = build_root(source, config)
    prefix = contract(config)["build_dir"] + "/"
    extras = extra_roots(source, config)
    roots = [root, *(path for _, path in extras)]
    if root.exists():
        raise ValueError("Restore requires a fresh checkout without a build")
    for directory, path in extras:
        expected_files = {name.removeprefix(directory + "/"): checksum
                          for name, checksum in metadata.get("extra_files", {}).items()
                          if name.startswith(directory + "/")}
        if path.exists() and inventory(path) != expected_files:
            raise ValueError(f"Additional checkout assets differ from artifact: {directory}")
    created_roots = [path for path in roots if not path.exists()]
    try:
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            names = [member.name for member in members]
            expected_files = {prefix + safe_name(name) for name in metadata["files"]}
            for name in metadata.get("extra_files", {}):
                safe_name(name)
                if not any(name.startswith(directory + "/") for directory, _ in extras):
                    raise ValueError("Additional artifact path is invalid")
                expected_files.add(name)
            if len(names) != len(set(names)) or set(names) != expected_files:
                raise ValueError("Artifact file set is invalid")
            for member in members:
                safe_name(member.name)
                if not member.isfile():
                    raise ValueError("Artifact contains a link or unsupported member")
            checksums = {prefix + name: checksum for name, checksum in metadata["files"].items()}
            checksums.update(metadata.get("extra_files", {}))
            # Validate payloads before overwriting matching tracked static assets.
            for member in members:
                checksum = hashlib.sha256()
                with stream.extractfile(member) as incoming:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        checksum.update(block)
                if checksum.hexdigest() != checksums[member.name]:
                    raise ValueError(f"Artifact file checksum is invalid: {member.name}")
            for member in members:
                path = source / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as incoming, path.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
        if validate_build(root, config) != metadata["files"]:
            raise ValueError("Artifact build content is invalid")
        if extra_inventory(source, config) != metadata.get("extra_files", {}):
            raise ValueError("Additional artifact build content is invalid")
        provenance_path(environment).write_text(json.dumps(metadata), encoding="utf-8")
    except Exception:
        for path in created_roots:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        raise
    print(f"Restored verified artifact: {len(metadata['files']) + len(metadata.get('extra_files', {}))} files, commit {metadata['commit']}, run {metadata['run_id']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["seal", "restore"])
    parser.add_argument("--directory", default="frontend-artifact")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--config", default=str(Path(__file__).with_name("profile.json")))
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if args.phase == "seal":
        seal(Path.cwd(), Path(args.directory), os.environ, config)
    else:
        restore(Path.cwd(), Path(args.directory), args.sha256, os.environ, config)


if __name__ == "__main__":
    main()
