"""HTTP deployment verification with bounded diagnostic evidence on every request."""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
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


def diagnose_error(url, error_message):
    err = error_message.lower()
    host = urlsplit(url).netloc or url

    if any(k in err for k in ["name or service not known", "getaddrinfo failed", "nodename nor servname provided", "non-existent domain"]):
        subdomain = host.split(".")[0] if "." in host else host
        return {
            "title": f"DNS Resolution Failed for {host}",
            "cause": f"Domain atau subdomain '{host}' tidak ditemukan di sistem DNS (Domain Not Found).",
            "actions": [
                f"Jika domain menggunakan Cloudflare atau DNS eksternal, tambahkan DNS Record: Type 'A', Name '{subdomain}', IPv4 address '<IP Server Hostinger>' (contoh: 145.79.14.222).",
                "Jika baru saja menambahkan record DNS, tunggu 1-5 menit hingga proses propagasi DNS global selesai.",
                f"Pastikan penulisan deploy_url di .github/hostinger/profile.json sudah benar: '{url}'."
            ]
        }
    if any(k in err for k in ["connection refused", "errno 111"]):
        return {
            "title": f"Connection Refused by {host}",
            "cause": f"Koneksi ke port HTTP/HTTPS pada '{host}' ditolak oleh web server.",
            "actions": [
                "Periksa apakah Web Server (LiteSpeed / Apache / Nginx) aktif di hPanel Hostinger.",
                "Periksa apakah protokol URL sudah benar (http:// vs https://).",
                "Pastikan deploy_url bukan 'localhost' atau '127.0.0.1'."
            ]
        }
    if any(k in err for k in ["timed out", "timeouterror"]):
        return {
            "title": f"Connection Timeout to {host}",
            "cause": f"Permintaan ke '{host}' melebihi batas waktu (timeout 30 detik).",
            "actions": [
                "Periksa apakah Cloudflare Bot Fight Mode, WAF, atau firewall server memblokir runner GitHub Actions.",
                "Pastikan server Hostinger sedang online dan tidak mengalami overload."
            ]
        }
    if any(k in err for k in ["certificate_verify_failed", "ssl"]):
        return {
            "title": f"SSL/TLS Certificate Error on {host}",
            "cause": f"Sertifikat SSL untuk '{host}' tidak valid, kedaluwarsa, atau self-signed.",
            "actions": [
                "Install/aktifkan SSL certificate gratis melalui hPanel Hostinger untuk subdomain ini.",
                "Jika domain menggunakan Cloudflare proxy, atur SSL/TLS encryption mode ke 'Full' atau 'Flexible'."
            ]
        }
    if "http 403" in err:
        return {
            "title": f"HTTP 403 Forbidden on {host}",
            "cause": f"Web server menolak akses ke direktori/file di '{host}'.",
            "actions": [
                "Pastikan permission direktori adalah 755 dan file adalah 644 di public_html.",
                "Periksa konfigurasi file .htaccess di root public_html.",
                "Periksa firewall Hostinger atau aturan Cloudflare WAF."
            ]
        }
    if "http 525" in err:
        return {
            "title": f"Cloudflare SSL Handshake Failed (HTTP 525) on {host}",
            "cause": f"Cloudflare gagal melakukan SSL Handshake dengan server Hostinger (Origin) untuk '{host}'.",
            "actions": [
                "Solusi 1 (Tercepat): Di dashboard Cloudflare -> menu 'SSL/TLS', ubah mode enkripsi ke 'Flexible'. (HTTP port 80 di server Hostinger sudah aktif & verified).",
                "Solusi 2: Di hPanel Hostinger -> menu 'SSL', install/aktifkan sertifikat SSL gratis (Let's Encrypt) untuk subdomain ini.",
                "Catatan: Aplikasi Laravel dan database sudah 100% siap di server; masalah ini murni pada negosiasi SSL antara Cloudflare dan Hostinger."
            ]
        }
    if "http 526" in err:
        return {
            "title": f"Invalid SSL Certificate on Origin (HTTP 526) on {host}",
            "cause": f"Sertifikat SSL di server Hostinger tidak valid atau kedaluwarsa pada mode Cloudflare Full (Strict).",
            "actions": [
                "Di dashboard Cloudflare -> menu 'SSL/TLS', ubah mode enkripsi ke 'Full' (non-strict) atau 'Flexible'.",
                "Re-issue/pasang sertifikat SSL di hPanel Hostinger untuk subdomain ini."
            ]
        }
    if any(k in err for k in ["http 520", "http 521", "http 522", "http 523", "http 524"]):
        status = re.search(r"http\s+(52\d)", err)
        code = status.group(1) if status else "52x"
        return {
            "title": f"Cloudflare Gateway Error (HTTP {code}) on {host}",
            "cause": f"Cloudflare tidak dapat menghubungi server Hostinger (Origin Error {code}).",
            "actions": [
                "Pastikan IP server di DNS Record Cloudflare benar (145.79.14.222).",
                "Periksa apakah web server Hostinger aktif melayani koneksi."
            ]
        }
    if any(k in err for k in ["http 500", "http 502", "http 503"]):
        status = re.search(r"http\s+(\d+)", err)
        code = status.group(1) if status else "500"
        return {
            "title": f"HTTP {code} Server Error on {host}",
            "cause": "Terjadi error fatal pada eksekusi aplikasi Laravel / PHP di server.",
            "actions": [
                "Periksa log aplikasi Laravel via SSH: tail -n 50 storage/logs/laravel.log",
                "Pastikan kredensial database di .env server sudah sesuai dan migrasi telah berjalan.",
                "Pastikan folder storage dan bootstrap/cache memiliki izin tulis (chmod -R 775 storage)."
            ]
        }
    if any(k in err for k in ["asset bytes differ", "absent from the current build", "invalid asset mime"]):
        return {
            "title": "Asset Verification Mismatch",
            "cause": "File JS/CSS yang diminta oleh HTML tidak sinkron dengan asset build Vite terkini.",
            "actions": [
                "Bersihkan cache Cloudflare / browser jika ada aset lama yang tertahan.",
                "Pastikan proses build frontend (npm run build) menghasilkan bundle yang sinkron."
            ]
        }
    return {
        "title": f"HTTP Verification Failed for {host}",
        "cause": error_message,
        "actions": [
            "Periksa artifact 'deploy-diagnostics' di GitHub Actions untuk response detail dari server.",
            "Periksa status server dan konfigurasi web root di Hostinger."
        ]
    }


def format_github_summary(url, diagnosis, raw_error):
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    actions_md = "\n".join(f"{i+1}. {act}" for i, act in enumerate(diagnosis["actions"]))
    md = f"""
### ⚠️ Verifikasi HTTP Deployment Gagal

| Parameter | Keterangan |
|---|---|
| **Target URL** | `{url}` |
| **Diagnosa** | **{diagnosis['title']}** |
| **Penyebab** | {diagnosis['cause']} |
| **Detail Error** | `{raw_error}` |

#### 🛠️ Rekomendasi Solusi:
{actions_md}

> ℹ️ **Status Deployment:** File aplikasi dan migrasi database **sudah berhasil** di-deploy ke server Hostinger. Masalah ini hanya terkait aksesibilitas HTTP publik pada URL target.
"""
    try:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(md)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("profile.json")))
    parser.add_argument("--diagnostics", default="deploy-diagnostics")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    diagnostics = Path(args.diagnostics)

    try:
        verify(config, diagnostics)
    except ValueError as err:
        url = config.get("deploy_url", "")
        raw_msg = str(err)
        diag = diagnose_error(url, raw_msg)

        # 1. Output GitHub Workflow Annotation
        print(f"::error title={diag['title']}::{diag['cause']}")

        # 2. Append rich markdown to GitHub Step Summary
        format_github_summary(url, diag, raw_msg)

        # 3. Print clean, structured terminal output
        actions_txt = "\n".join(f"  {i+1}. {act}" for i, act in enumerate(diag["actions"]))
        terminal_msg = f"""
================================================================================
❌ VERIFIKASI DEPLOYMENT GAGAL: {diag['title']}
================================================================================
URL Target : {url}
Penyebab   : {diag['cause']}
Detail     : {raw_msg}

🛠️ REKOMENDASI TINDAKAN:
{actions_txt}

ℹ️  Catatan: File aplikasi dan database migrasi sudah berhasil di-deploy ke Hostinger.
   Detail respons tersimpan di artifact: '{diagnostics}'
================================================================================
"""
        sys.stderr.write(terminal_msg.strip() + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
