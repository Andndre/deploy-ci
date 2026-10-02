# setup-hostinger-ci

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg?logo=windows)](https://github.com/Andndre/setup-hostinger-ci)
[![PowerShell: 7+ / 5.1](https://img.shields.io/badge/PowerShell-5.1%20%7C%207%2B-5391FE.svg?logo=powershell)](https://github.com/Andndre/setup-hostinger-ci)
[![Laravel: 11 / 12 / 13](https://img.shields.io/badge/Laravel-11%20%7C%2012%20%7C%2013-FF2D20.svg?logo=laravel)](https://laravel.com)

> **Zero-config interactive CLI wizard** to automate Laravel + Vite CI/CD deployment to **Hostinger Business Web Hosting** using GitHub Actions & Rsync over SSH.

---

## ⚡ Quick Start (1-Line Install)

Buka PowerShell di laptop Anda dan jalankan perintah satu baris berikut:

```powershell
irm https://raw.githubusercontent.com/Andndre/setup-hostinger-ci/main/install.ps1 | iex
```

Setelah selesai, buka root folder proyek Laravel Anda di terminal mana saja (PowerShell, CMD, atau Git Bash), lalu jalankan:

```text
setup-hostinger-ci
```

---

## 🎯 Mengapa Perkakas Ini Dibuat?

Mendeploy Laravel modern (Vite, Inertia, Pest, SQLite/MySQL) ke shared hosting seperti Hostinger Business sering menimbulkan tantangan teknis:

1. **Hostinger Resource & Memory Limits:** Menjalankan `npm run build` atau `composer install` langsung di server Hostinger sering *killed* karena keterbatasan RAM/CPU shared hosting.
2. **Inode Quota Bloat:** Mengunggah folder `node_modules` ke server menghabiskan ~40.000–80.000 inodes kuota hosting Anda tanpa alasan yang jelas.
3. **Data Loss Bencana:** Menggunakan `rsync --delete` tanpa proteksi ketat dapat **menghapus database SQLite production** (`database/*.sqlite*`) dan file upload pengguna (`storage/**`).
4. **Git Repository Bloat:** Folder `public/build` sering terlanjur ter-commit ke Git, menimbulkan merge conflict setiap kali asset di-build ulang.
5. **Kelelahan Konfigurasi Manual:** Menyiapkan GitHub Secrets satu per satu di browser dan menulis file YAML CI/CD berulang kali memakan waktu.

**`setup-hostinger-ci` mengotomatisasi seluruh alur ini dalam hitungan detik.**

---

## ✨ Fitur Utama

* 🧙‍♂️ **Guided Interactive Wizard:** Cukup jalankan perintah tanpa opsi apa pun. Skrip akan menanyakan konfigurasi dengan nilai default cerdas (tinggal tekan `Enter`).
* 🔍 **Auto-Inspection Proyek:** Mendeteksi versi PHP (`composer.json`), Node (`.nvmrc`), Git branch, test runner (Pest / PHPUnit), linter (Laravel Pint, ESLint), dan route-generator (Wayfinder/Ziggy).
* 🛡️ **Pilihan Pipeline Gated vs Lean:**
  * **Gated Pipeline:** Menjalankan Pint, ESLint, dan Pest/PHPUnit terlebih dahulu. Deployment hanya berjalan jika semua test lolos.
  * **Lean Deploy:** Mode build & deploy cepat untuk proyek tanpa test suite.
* 💾 **Production Data Safety:** Standar rsync mengecualikan file sensitif dan stateful:
  ```text
  --exclude=.env --exclude=node_modules --exclude=.git --exclude=.github
  --exclude=storage/** --exclude=database/*.sqlite* --exclude=tests
  ```
* 🧠 **Smart Hostinger Credential Caching:** Mengingat SSH Host, User, dan Port Hostinger terakhir di `~/.hostinger-ci.json`, sehingga proyek ke-2, ke-3, dst. tidak perlu input ulang.
* 📂 **Auto-Inferred Target Directory:** Mengusulkan path `/home/<user>/domains/<nama-folder>/app` secara otomatis berdasarkan nama folder proyek Anda.
* 🔐 **Automated GitHub Secrets:** Mengunggah seluruh SSH Secrets ke repository GitHub secara instan via `gh` CLI.
* 🧹 **Git Clean-up:** Otomatis menambahkan `/public/build` ke `.gitignore` dan melakukan `git rm -r --cached` jika sudah terlanjur terlacak di Git.

---

## 🏗️ Arsitektur Pipeline CI/CD

```mermaid
flowchart TD
    subgraph GitHubRunner ["GitHub Actions Runner"]
        A["Git Push / PR"] --> B["Checkout Code"]
        B --> C["Setup PHP 8.3 & pdo_sqlite / pdo_mysql"]
        C --> D["Setup Node.js 22 & npm ci"]
        
        subgraph verifyJob ["Job 1: verify (Quality Gate)"]
            E["Generate Wayfinder/Frontend Types"]
            F["Laravel Pint Code Style"]
            G["ESLint Frontend Check"]
            H["Automated Tests: Pest / PHPUnit"]
            E --> F --> G --> H
        end
        
        D --> verifyJob
        
        subgraph deployJob ["Job 2: deploy (Hanya di Push ke Main)"]
            I["composer install --no-dev"]
            J["npm run build: Vite Production"]
            K["Rsync over SSH Port 65002<br/>Exclude storage & SQLite"]
            I --> J --> K
        end
        
        H -->|Semua Test Pass| deployJob
    end

    subgraph HostingerServer ["Hostinger Shared Hosting"]
        L[("Live App Files")]
        M[("Production SQLite / MySQL")]
        N[("User Uploads /storage")]
        O["Artisan: optimize:clear<br/>config:cache, route:cache, view:cache"]
        
        K --> L
        K -.->|Protected / Not Overwritten| M
        K -.->|Protected / Not Overwritten| N
        L --> O
    end
```

---

## 📋 Prasyarat

Sebelum menjalankan perkakas ini, pastikan Anda telah menyiapkan:

1. **Hostinger Business Web Hosting:**
   * Fitur SSH Access aktif di hPanel.
   * Port SSH default Hostinger adalah **65002**.
   * Public Key SSH lokal (`~/.ssh/id_ed25519.pub`) telah dimasukkan ke **SSH Access $\rightarrow$ Authorized Keys** di hPanel.
2. **GitHub CLI (`gh`):**
   * Terpasang di komputer Anda (`winget install GitHub.cli`).
   * Sudah terotentikasi ke akun GitHub Anda:
     ```powershell
     gh auth login
     ```

---

## 💻 Cara Menggunakan

Masuk ke folder proyek Laravel Anda di terminal:

```powershell
cd D:\proyek-laravel-saya
setup-hostinger-ci
```

Contoh output interaktif:

```text
========================================================
  Hostinger Laravel CI/CD Wizard (GitHub Actions v2)
========================================================

[1/6] Mendeteksi Konfigurasi Proyek...
  -> Git Branch       : main
  -> Versi PHP        : 8.3
  -> Versi Node       : 22
  -> Tools Terdeteksi : Laravel Pint, ESLint, Pest Tests

[2/6] Wizard Konfigurasi Deployment...
Target Git Branch untuk deploy otomatis [main]: 
Versi PHP di Hostinger [8.3]: 
Aktifkan Gated Pipeline (jalankan Linting & Automated Test sebelum deploy)? [Y/n]: 
Otomatis jalankan 'php artisan migrate --force' setelah deployment? [y/N]: 
Hostinger SSH Host / IP [195.35.1.2]: 
Hostinger SSH User [u519809216]: 
Hostinger SSH Port [65002]: 
Path SSH Private Key lokal [C:\Users\Andndre\.ssh\id_ed25519]: 
Path TARGET_DIR di Hostinger [/home/u519809216/domains/proyek-laravel-saya/app]: 

[3/6] Memeriksa .gitignore & Git Index...
  -> '/public/build' sudah ada di .gitignore

[4/6] Menyiapkan Workflow GitHub Actions...
  -> Mode: GATED PIPELINE (Verify Quality/Tests -> Deploy to Hostinger)
  -> File workflow tersimpan di: .github/workflows/deploy.yml

[5/6] Mengonfigurasi GitHub Secrets...
  -> Mengirim secrets ke repository GitHub...
  -> Semua Secrets berhasil disimpan di GitHub!

[6/6] Selesai!
Untuk mengaktifkan deployment, jalankan:
  git add .gitignore .github/workflows/deploy.yml
  git commit -m "ci: setup automated hostinger deployment"
  git push origin main
```

---

## 🗑️ Uninstall

Jika Anda ingin menghapus perkakas ini dari sistem:

```powershell
irm https://raw.githubusercontent.com/Andndre/setup-hostinger-ci/main/uninstall.ps1 | iex
```

---

## 📄 Lisensi

Didistribusikan di bawah lisensi [MIT](LICENSE). Dibuat oleh [Agung Andre](https://github.com/Andndre).
