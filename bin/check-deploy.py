"""HTTP deployment verification with bounded diagnostic evidence on every request."""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen


class Dependencies(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.in_script = True
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("href") and attrs.get("rel") in {"stylesheet", "modulepreload"}:
            self.urls.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, text):
        if self.in_script:
            # SvelteKit's HTML bootstrap loads its entry chunks with inline
            # dynamic imports rather than script src attributes.
            self.urls.extend(re.findall(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)", text))


def fetch(url, directory, index, expected=None, contains=""):
    record = {"time_utc": datetime.now(timezone.utc).isoformat(), "url": url}
    body = b""
    try:
        request = Request(url, headers={"User-Agent": "deploy-ci-verifier/1.0", "Accept-Encoding": "identity"})
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            record.update(status=response.code, final_url=response.url,
                          headers=dict(response.headers.items()),
                          cf_ray=response.headers.get("CF-Ray"),
                          retry_after=response.headers.get("Retry-After"))
            mime = response.headers.get_content_type().lower()
            body = response.read(10 * 1024 * 1024 + 1)
            if len(body) > 10 * 1024 * 1024:
                raise ValueError("Response exceeded 10 MiB verification limit")
            if response.code != 200:
                if response.code in {403, 429}:
                    record['verification_note'] = 'HTTP access was rejected or rate-limited. Application publication is reported separately; this response alone does not identify the cause.'
                raise ValueError(f"HTTP {response.code}")
            if not body.strip():
                raise ValueError("Empty response")
            if expected is None:
                if mime != "text/html" or not re.search(rb"<(?:!doctype\s+html|html|body)\b", body, re.I):
                    raise ValueError(f"Expected an HTML page, received {mime}")
                if contains and contains.encode("utf-8") not in body:
                    raise ValueError("Page did not contain the configured marker")
            else:
                allowed = ({"text/css"} if expected.suffix == ".css" else
                           {"text/javascript", "application/javascript", "application/x-javascript"})
                if mime not in allowed:
                    raise ValueError(f"Invalid asset MIME: {mime}")
                if hashlib.sha256(body).digest() != hashlib.sha256(expected.read_bytes()).digest():
                    raise ValueError("Asset bytes differ from the current build")
            record["ok"] = True
    except (ValueError, OSError, URLError) as error:
        record.update(ok=False, error=str(error))
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / f"{index:03d}"
    prefix.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    # Enough to identify HTML error bodies without producing huge artifacts.
    prefix.with_suffix(".body").write_bytes(body[:65536])
    if not record.get("ok"):
        raise ValueError(f"HTTP verification failed for {url}: {record.get('error')}; evidence: {prefix}.json")
    return body.decode("utf-8", errors="replace"), record["final_url"]


def select_assets(config, html, page_url):
    output = Path(config["output_dir"]).resolve()
    web = (output / config["web_root"]).resolve()
    candidates = {}
    parser = Dependencies()
    parser.feed(html)
    for reference in parser.urls:
        url = urljoin(page_url, reference)
        if urlsplit(url).netloc != urlsplit(page_url).netloc:
            continue
        name = unquote(urlsplit(url).path).lstrip("/")
        path = (web / name).resolve()
        if not path.is_relative_to(web) or path.suffix not in {".js", ".css"}:
            continue
        relative = path.relative_to(output).as_posix()
        managed = any(relative.startswith(d + "/") for d in config["immutable_dirs"])
        if managed:
            if not path.is_file():
                raise ValueError(f"Page references an asset absent from the current build: {url}")
            candidates[url] = path
    # Include lazy chunks that are not directly linked by the page, checking a
    # small deterministic sample to avoid a burst of HTTP requests.
    for directory in config["immutable_dirs"]:
        for path in sorted((output / directory).rglob("*")):
            if path.is_file() and path.suffix in {".js", ".css"}:
                url = urljoin(page_url, "/" + path.relative_to(web).as_posix())
                candidates.setdefault(url, path)
    # Reserve slots for both JS and CSS when the build contains both.
    selected = {}
    for suffix in (".js", ".css"):
        match = next(((url, path) for url, path in candidates.items() if path.suffix == suffix), None)
        if match:
            selected[match[0]] = match[1]
    for url, path in candidates.items():
        if len(selected) >= config.get("max_assets", 6):
            break
        selected.setdefault(url, path)
    if not selected:
        raise ValueError("No built JS/CSS dependencies found for verification")
    return selected


def verify(config, directory):
    url = config["deploy_url"]
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
        raise ValueError("A valid HTTP deployment URL is required")
    html, final_url = fetch(url, directory, 0, contains=config.get("page_contains", ""))
    try:
        assets = select_assets(config, html, final_url)
    except ValueError as error:
        (directory / "dependency-error.json").write_text(json.dumps({"error": str(error)}), encoding="utf-8")
        raise
    for index, (url, path) in enumerate(assets.items(), 1):
        fetch(url, directory, index, expected=path)
    print(f"Verified HTML and {len(assets)} JS/CSS dependencies")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("profile.json")))
    parser.add_argument("--diagnostics", default="deploy-diagnostics")
    args = parser.parse_args()
    verify(json.loads(Path(args.config).read_text(encoding="utf-8-sig")), Path(args.diagnostics))


if __name__ == "__main__":
    main()
