# -*- coding: utf-8 -*-
"""
Ueban Launcher — простой лаунчер Minecraft Java Edition (офлайн-режим).

Что делает:
  * берёт список версий с серверов Mojang;
  * скачивает клиент, библиотеки, ассеты, нативки;
  * при необходимости скачивает нужную версию Java (официальный рантайм Mojang);
  * собирает командную строку и запускает игру.

Только стандартная библиотека Python. Для сборки в exe см. build.bat
"""
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # для тестов без GUI
    tk = None

APP_NAME = "Ueban Launcher"
APP_VERSION = "1.0"
UA = f"UebanLauncher/{APP_VERSION}"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
JAVA_ALL_URL = ("https://launchermeta.mojang.com/v1/products/java-runtime/"
                "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
RESOURCES_URL = "https://resources.download.minecraft.net/"

IS_WIN = sys.platform == "win32"
OS_NAME = {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")
IS64 = sys.maxsize > 2 ** 32
_m = platform.machine().lower()
ARCH = "arm64" if ("arm" in _m or "aarch64" in _m) else ("x86_64" if IS64 else "x86")

if IS_WIN:
    BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / ".uebanlauncher"
else:
    BASE = Path.home() / ".uebanlauncher"

VERSIONS = BASE / "versions"
LIBS = BASE / "libraries"
ASSETS = BASE / "assets"
RUNTIME = BASE / "runtime"
GAME_DIR = BASE / "game"
CONFIG = BASE / "config.json"


# ───────────────────────── сеть / файлы ─────────────────────────

def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest, sha1=None, size=None, retries=3):
    dest = Path(dest)
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if sha1 and sha1_file(tmp) != sha1:
                raise IOError(f"sha1 mismatch: {url}")
            os.replace(tmp, dest)
            return
        except Exception as e:  # noqa
            last = e
            time.sleep(1)
    raise last


def download_many(tasks, progress=None, workers=16):
    """tasks: список (url, dest, sha1, size)."""
    total = len(tasks)
    if not total:
        return
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, *t) for t in tasks]
        for f in as_completed(futs):
            f.result()  # пробросит исключение
            done += 1
            if progress:
                progress(done, total)


# ───────────────────────── правила Mojang ─────────────────────────

def os_match(o):
    if "name" in o and o["name"] != OS_NAME:
        return False
    if "arch" in o:
        a = o["arch"]
        if a == "x86" and ARCH != "x86":
            return False
        if a in ("arm64", "aarch64") and ARCH != "arm64":
            return False
    if "version" in o:
        try:
            if not re.search(o["version"], platform.version()):
                return False
        except re.error:
            pass
    return True


def rules_ok(rules, features=None):
    if not rules:
        return True
    features = features or {}
    allowed = False
    for r in rules:
        ok = True
        if "os" in r:
            ok = os_match(r["os"])
        if ok and "features" in r:
            ok = all(features.get(k, False) == v for k, v in r["features"].items())
        if ok:
            allowed = (r.get("action") == "allow")
    return allowed


# ───────────────────────── версии ─────────────────────────

def local_versions():
    out = []
    if VERSIONS.exists():
        for d in VERSIONS.iterdir():
            if (d / f"{d.name}.json").exists():
                out.append(d.name)
    return sorted(out, reverse=True)


def load_version(vid, manifest=None, log=print):
    """Возвращает json версии (скачивает при необходимости). Поддерживает inheritsFrom."""
    jpath = VERSIONS / vid / f"{vid}.json"
    if not jpath.exists():
        if not manifest:
            manifest = http_json(MANIFEST_URL)
        entry = next((v for v in manifest["versions"] if v["id"] == vid), None)
        if not entry:
            raise RuntimeError(f"Версия {vid} не найдена")
        log(f"Скачиваю описание версии {vid}...")
        fetch(entry["url"], jpath, entry.get("sha1"))
    with open(jpath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "inheritsFrom" in data:
        parent = load_version(data["inheritsFrom"], manifest, log)
        merged = dict(parent)
        for k, v in data.items():
            if k not in ("libraries", "arguments"):
                merged[k] = v
        merged["libraries"] = data.get("libraries", []) + parent.get("libraries", [])
        if "arguments" in data or "arguments" in parent:
            args = {}
            for k in ("game", "jvm"):
                args[k] = (parent.get("arguments", {}).get(k, [])
                           + data.get("arguments", {}).get(k, []))
            merged["arguments"] = args
        merged["_jar_id"] = parent.get("_jar_id", data["inheritsFrom"])
        merged["id"] = vid
        return merged
    data["_jar_id"] = vid
    return data


def lib_path_from_name(name):
    parts = name.split("@")[0].split(":")
    g, a, v = parts[0], parts[1], parts[2]
    cl = parts[3] if len(parts) > 3 else None
    fn = f"{a}-{v}" + (f"-{cl}" if cl else "") + ".jar"
    return f"{g.replace('.', '/')}/{a}/{v}/{fn}"


def collect_libraries(vj):
    """→ (tasks, classpath[list[Path]], natives[list[(Path, exclude)]])"""
    tasks, cp, natives = [], [], []
    for lib in vj.get("libraries", []):
        if not rules_ok(lib.get("rules")):
            continue
        dl = lib.get("downloads")
        if dl is None:
            rel = lib_path_from_name(lib["name"])
            base = lib.get("url", "https://libraries.minecraft.net/")
            if not base.endswith("/"):
                base += "/"
            tasks.append((base + rel, LIBS / rel, None, None))
            cp.append(LIBS / rel)
            continue
        art = dl.get("artifact")
        if art:
            p = LIBS / art["path"]
            if art.get("url"):
                tasks.append((art["url"], p, art.get("sha1"), art.get("size")))
            cp.append(p)
        nat = lib.get("natives")
        if nat and OS_NAME in nat:
            cls = nat[OS_NAME].replace("${arch}", "64" if IS64 else "32")
            a2 = dl.get("classifiers", {}).get(cls)
            if a2:
                p = LIBS / a2["path"]
                tasks.append((a2["url"], p, a2.get("sha1"), a2.get("size")))
                natives.append((p, lib.get("extract", {}).get("exclude", [])))
    # убираем дубликаты, сохраняя порядок
    seen, uniq = set(), []
    for p in cp:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return tasks, uniq, natives


def extract_natives(natives, ndir):
    ndir.mkdir(parents=True, exist_ok=True)
    if any(ndir.iterdir()):
        return
    for p, excl in natives:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith("/") or any(n.startswith(e) for e in excl):
                    continue
                z.extract(n, ndir)


def prepare_assets(vj):
    ai = vj["assetIndex"]
    idx_path = ASSETS / "indexes" / f"{ai['id']}.json"
    fetch(ai["url"], idx_path, ai.get("sha1"), ai.get("size"))
    with open(idx_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    tasks = []
    for name, o in idx["objects"].items():
        h = o["hash"]
        tasks.append((f"{RESOURCES_URL}{h[:2]}/{h}", ASSETS / "objects" / h[:2] / h, h, o["size"]))
    return ai["id"], idx, tasks


def materialize_legacy_assets(aid, idx):
    """Для очень старых версий: virtual / map_to_resources."""
    target = None
    if idx.get("map_to_resources"):
        target = GAME_DIR / "resources"
    elif idx.get("virtual"):
        target = ASSETS / "virtual" / aid
    if not target:
        return
    for name, o in idx["objects"].items():
        h = o["hash"]
        src = ASSETS / "objects" / h[:2] / h
        dst = target / name
        if not dst.exists() and src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


# ───────────────────────── Java ─────────────────────────

def java_exe_in(root):
    exe = root / "bin" / ("java.exe" if IS_WIN else "java")
    return exe if exe.exists() else None


def runtime_key():
    if IS_WIN:
        return "windows-arm64" if ARCH == "arm64" else ("windows-x64" if IS64 else "windows-x86")
    if OS_NAME == "osx":
        return "mac-os-arm64" if ARCH == "arm64" else "mac-os"
    return "linux" if IS64 else "linux-i386"


def download_runtime(component, log, progress):
    allj = http_json(JAVA_ALL_URL)
    entries = allj.get(runtime_key(), {}).get(component) or []
    if not entries:
        raise RuntimeError(f"Mojang не предоставляет рантайм {component} для {runtime_key()}")
    mf = http_json(entries[0]["manifest"]["url"])
    root = RUNTIME / component
    tasks, execs = [], []
    for rel, info in mf["files"].items():
        if info["type"] == "directory":
            (root / rel).mkdir(parents=True, exist_ok=True)
        elif info["type"] == "file":
            raw = info["downloads"]["raw"]
            tasks.append((raw["url"], root / rel, raw["sha1"], raw["size"]))
            if info.get("executable"):
                execs.append(root / rel)
    log(f"Скачиваю Java ({component}), файлов: {len(tasks)}...")
    download_many(tasks, progress)
    if not IS_WIN:
        for p in execs:
            os.chmod(p, 0o755)


def system_java_candidates():
    c = []
    if os.environ.get("JAVA_HOME"):
        c.append(Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if IS_WIN else "java"))
    w = shutil.which("java")
    if w:
        c.append(Path(w))
    if IS_WIN:
        for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not pf:
                continue
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "BellSoft"):
                d = Path(pf) / vendor
                if d.exists():
                    c += sorted(d.glob("*/bin/java.exe"), reverse=True)
    return [p for p in c if p.exists()]


def java_major(exe):
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True,
                             timeout=15,
                             creationflags=0x08000000 if IS_WIN else 0).stderr
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
        if not m:
            return None
        major = int(m.group(1))
        return int(m.group(2)) if major == 1 and m.group(2) else major
    except Exception:
        return None


def ensure_java(vj, cfg, log, progress):
    custom = cfg.get("java_path")
    if custom and Path(custom).exists():
        return str(custom)
    jv = vj.get("javaVersion") or {"component": "jre-legacy", "majorVersion": 8}
    comp, need = jv["component"], jv["majorVersion"]
    root = RUNTIME / comp
    exe = java_exe_in(root)
    if exe:
        return str(exe)
    try:
        download_runtime(comp, log, progress)
        exe = java_exe_in(root)
        if exe:
            return str(exe)
    except Exception as e:
        log(f"Не удалось скачать Java от Mojang: {e}")
    for cand in system_java_candidates():
        if (java_major(cand) or 0) >= need:
            log(f"Использую системную Java: {cand}")
            return str(cand)
    raise RuntimeError(f"Не найдена Java {need}+. Установите её (например, Temurin) "
                       f"или проверьте интернет.")


# ───────────────────────── команда запуска ─────────────────────────

def offline_uuid(name):
    d = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=d, version=3))


def subst(s, d):
    return re.sub(r"\$\{([^}]+)\}", lambda m: str(d.get(m.group(1), m.group(0))), s)


def flatten_args(args, feats, d):
    out = []
    for a in args:
        if isinstance(a, str):
            out.append(subst(a, d))
        else:
            if not rules_ok(a.get("rules"), feats):
                continue
            v = a["value"]
            for x in ([v] if isinstance(v, str) else v):
                out.append(subst(x, d))
    return out


def build_command(vj, java, classpath, natives_dir, name, ram_mb, assets_id, log_cfg=None):
    jar_id = vj.get("_jar_id", vj["id"])
    cp = list(classpath) + [VERSIONS / jar_id / f"{jar_id}.jar"]
    sep = os.pathsep
    d = {
        "auth_player_name": name,
        "version_name": vj["id"],
        "game_directory": str(GAME_DIR),
        "assets_root": str(ASSETS),
        "assets_index_name": assets_id,
        "auth_uuid": offline_uuid(name),
        "auth_access_token": "0",
        "auth_session": "0",
        "user_type": "legacy",
        "user_properties": "{}",
        "version_type": vj.get("type", "release"),
        "natives_directory": str(natives_dir),
        "launcher_name": "UebanLauncher",
        "launcher_version": APP_VERSION,
        "classpath": sep.join(str(p) for p in cp),
        "classpath_separator": sep,
        "library_directory": str(LIBS),
        "clientid": "0",
        "auth_xuid": "0",
        "game_assets": str(ASSETS / "virtual" / assets_id),
    }
    feats = {}  # demo / custom resolution / quick play — не используем

    if "arguments" in vj:
        jvm = flatten_args(vj["arguments"].get("jvm", []), feats, d)
        game = flatten_args(vj["arguments"].get("game", []), feats, d)
    else:
        jvm = ["-Djava.library.path=" + d["natives_directory"],
               "-Dminecraft.launcher.brand=UebanLauncher",
               "-Dminecraft.launcher.version=" + APP_VERSION,
               "-cp", d["classpath"]]
        game = [subst(x, d) for x in vj.get("minecraftArguments", "").split()]

    cmd = [java, f"-Xmx{ram_mb}M", "-Xms256M"]
    if log_cfg:
        cmd.append(log_cfg[0].replace("${path}", str(log_cfg[1])))
    cmd += jvm + [vj["mainClass"]] + game
    return cmd


# ───────────────────────── подготовка и запуск ─────────────────────────

def prepare_and_launch(vid, name, ram_mb, cfg, log, progress, manifest=None):
    GAME_DIR.mkdir(parents=True, exist_ok=True)

    vj = load_version(vid, manifest, log)

    # клиентский jar
    jar_id = vj.get("_jar_id", vid)
    jar_path = VERSIONS / jar_id / f"{jar_id}.jar"
    cl = (vj.get("downloads") or {}).get("client")
    tasks = []
    if cl:
        tasks.append((cl["url"], jar_path, cl.get("sha1"), cl.get("size")))
    elif not jar_path.exists():
        raise RuntimeError("Нет клиентского jar для этой версии")

    # библиотеки
    lib_tasks, classpath, natives = collect_libraries(vj)
    tasks += lib_tasks

    # ассеты
    aid, idx, asset_tasks = prepare_assets(vj)
    tasks += asset_tasks

    # конфиг логирования
    log_cfg = None
    lg = (vj.get("logging") or {}).get("client")
    if lg and lg.get("file"):
        f = lg["file"]
        lp = ASSETS / "log_configs" / f["id"]
        tasks.append((f["url"], lp, f.get("sha1"), f.get("size")))
        log_cfg = (lg["argument"], lp)

    log(f"Проверяю/скачиваю файлы: {len(tasks)}...")
    download_many(tasks, lambda c, t: progress(c, t, "Файлы игры"))
    materialize_legacy_assets(aid, idx)

    # Java
    progress(0, 1, "Java")
    java = ensure_java(vj, cfg, log, lambda c, t: progress(c, t, "Java"))

    # нативки
    ndir = VERSIONS / vid / "natives"
    extract_natives(natives, ndir)

    cmd = build_command(vj, java, classpath, ndir, name, ram_mb, aid, log_cfg)
    log("Запуск: " + " ".join(cmd[:3]) + " ...")
    proc = subprocess.Popen(
        cmd, cwd=str(GAME_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
        creationflags=0x08000000 if IS_WIN else 0)
    for line in proc.stdout:
        log(line.rstrip())
    code = proc.wait()
    log(f"Игра завершилась с кодом {code}")
    return code


# ───────────────────────── GUI ─────────────────────────

def load_cfg():
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    BASE.mkdir(parents=True, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.manifest = None
        self.all_versions = []
        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("640x460")
        root.minsize(560, 400)

        f = ttk.Frame(root, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Ник:").grid(row=0, column=0, sticky="w", pady=4)
        self.name = tk.StringVar(value=self.cfg.get("name", "Player"))
        ttk.Entry(f, textvariable=self.name).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(f, text="Версия:").grid(row=1, column=0, sticky="w", pady=4)
        self.version = tk.StringVar(value=self.cfg.get("version", ""))
        self.combo = ttk.Combobox(f, textvariable=self.version, state="readonly")
        self.combo.grid(row=1, column=1, sticky="ew", pady=4)
        self.snap = tk.BooleanVar(value=self.cfg.get("snapshots", False))
        ttk.Checkbutton(f, text="Снапшоты", variable=self.snap,
                        command=self.refresh_versions).grid(row=1, column=2, padx=8)

        ttk.Label(f, text="Память (МБ):").grid(row=2, column=0, sticky="w", pady=4)
        self.ram = tk.IntVar(value=self.cfg.get("ram", 2048))
        ttk.Spinbox(f, from_=512, to=32768, increment=256, textvariable=self.ram,
                    width=8).grid(row=2, column=1, sticky="w", pady=4)

        self.btn = ttk.Button(f, text="▶  ИГРАТЬ", command=self.on_play)
        self.btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8, ipady=6)

        self.pbar = ttk.Progressbar(f, mode="determinate")
        self.pbar.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.status = ttk.Label(f, text="Загрузка списка версий...")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w", pady=2)

        f.rowconfigure(6, weight=1)
        box = ttk.Frame(f)
        box.grid(row=6, column=0, columnspan=3, sticky="nsew")
        self.text = tk.Text(box, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.refresh_versions()
        threading.Thread(target=self.load_manifest, daemon=True).start()
        root.after(100, self.poll)

    # --- версии
    def load_manifest(self):
        try:
            self.manifest = http_json(MANIFEST_URL)
            self.q.put(("versions", None))
        except Exception as e:
            self.q.put(("log", f"Нет связи с Mojang ({e}). Доступны только установленные версии."))
            self.q.put(("versions", None))

    def refresh_versions(self):
        vs = []
        if self.manifest:
            for v in self.manifest["versions"]:
                if v["type"] == "release" or (self.snap.get() and v["type"] == "snapshot"):
                    vs.append(v["id"])
        for lv in local_versions():
            if lv not in vs:
                vs.append(lv)
        self.combo["values"] = vs
        if vs and self.version.get() not in vs:
            latest = self.manifest["latest"]["release"] if self.manifest else vs[0]
            self.version.set(latest if latest in vs else vs[0])

    # --- запуск
    def on_play(self):
        name = self.name.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            messagebox.showerror(APP_NAME, "Ник: 3–16 символов, латиница, цифры и _")
            return
        vid = self.version.get()
        if not vid:
            messagebox.showerror(APP_NAME, "Выберите версию")
            return
        try:
            ram = int(self.ram.get())
        except Exception:
            ram = 2048
        self.cfg.update(name=name, version=vid, ram=ram, snapshots=self.snap.get())
        save_cfg(self.cfg)
        self.btn.state(["disabled"])
        threading.Thread(target=self.worker, args=(vid, name, ram), daemon=True).start()

    def worker(self, vid, name, ram):
        try:
            prepare_and_launch(
                vid, name, ram, self.cfg,
                log=lambda s: self.q.put(("log", s)),
                progress=lambda c, t, lbl="": self.q.put(("progress", (c, t, lbl))),
                manifest=self.manifest)
        except Exception as e:
            self.q.put(("log", "ОШИБКА: " + str(e)))
            self.q.put(("log", traceback.format_exc()))
        finally:
            self.q.put(("done", None))

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.text.configure(state="normal")
                    self.text.insert("end", data + "\n")
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif kind == "progress":
                    c, t, lbl = data
                    self.pbar.configure(maximum=max(t, 1), value=c)
                    self.status.configure(text=f"{lbl}: {c}/{t}")
                elif kind == "versions":
                    self.refresh_versions()
                    self.status.configure(text="Готово")
                elif kind == "done":
                    self.btn.state(["!disabled"])
                    self.status.configure(text="Готово")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if IS_WIN else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
