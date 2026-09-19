#!/usr/bin/env python3
"""Build the private MPV runtime and its complete corresponding-source sidecar.

Usage: uv run scripts/prepare_mpv.py

Requires C/C++ build tools, CMake, Ninja, nasm, git, uv and Go 1.21 or newer.
The pinned Go toolchain is downloaded automatically. Unix builds also require
autoconf, autoconf-archive, automake, libtool and pkg-config. Windows requires
an x64 MSVC developer shell plus LLVM (clang, clang++, lld-link and llvm-rc).
macOS requires Xcode 15 or newer command-line tools. Linux additionally needs
X11/OpenGL and ALSA/PulseAudio development headers for SDL2's desktop drivers.
No installed MPV or FFmpeg is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "native" / "mpv-manifest.json"


def host_target():
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    system = "windows" if sys.platform == "win32" else sys.platform
    return f"{system}-{architecture}"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(artifact, cache):
    """Reuse only verified files; never allow a partial download into the cache."""
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / artifact["sha256"]
    if destination.is_file() and sha256(destination) == artifact["sha256"]:
        return destination
    if not artifact["url"].startswith("https://"):
        raise ValueError("Native artifacts must use HTTPS")
    request = urllib.request.Request(artifact["url"], headers={"User-Agent": "QiTV-native-build"})
    descriptor, filename = tempfile.mkstemp(dir=cache)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(request, timeout=120) as response:
                if not response.url.startswith("https://"):
                    raise ValueError("Native artifact redirected to an insecure URL")
                size = 0
                while data := response.read(1024 * 1024):
                    size += len(data)
                    if size > artifact["size"]:
                        raise ValueError(f"Native artifact exceeds pinned size: {artifact['url']}")
                    output.write(data)
        if size != artifact["size"] or sha256(temporary) != artifact["sha256"]:
            raise ValueError(f"SHA-256/size mismatch for {artifact['url']}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def unpack(archive, destination):
    """Extract pinned tar sources without traversal, special files or escaping links."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as package:
        for member in package.getmembers():
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.name
                or ":" in member.name
            ):
                raise ValueError(f"Unsafe archive member: {member.name}")
            if member.isdev() or member.isfifo():
                raise ValueError(f"Special file in native archive: {member.name}")
        # Python's data filter also validates link targets, including chains of
        # symlinks created by earlier members, while retaining executable bits.
        package.extractall(destination, filter="data")


def source_directory(archive, destination):
    unpack(archive, destination)
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError(f"Expected one source root in {archive}")
    return children[0]


def run(command, *, cwd, env):
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def copy_licenses(source, destination):
    """Keep upstream text verbatim, with package names to prevent collisions."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if path.is_file() and path.name.lower().startswith(
            ("license", "licence", "copying", "copyright", "notice", "authors")
        ):
            output = destination / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)


def make_triplet(path, target):
    lines = [
        f"set(VCPKG_TARGET_ARCHITECTURE {target['architecture']})",
        "set(VCPKG_CRT_LINKAGE static)",
        "set(VCPKG_LIBRARY_LINKAGE static)",
        "set(VCPKG_BUILD_TYPE release)",
    ]
    if target["cmake_system"]:
        lines.append(f"set(VCPKG_CMAKE_SYSTEM_NAME {target['cmake_system']})")
    if target["cmake_system"] == "Darwin":
        architecture = "x86_64" if target["architecture"] == "x64" else "arm64"
        lines += [
            f"set(VCPKG_OSX_ARCHITECTURES {architecture})",
            f"set(VCPKG_OSX_DEPLOYMENT_TARGET {target['minimum_os']})",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dependency_sources(downloads, inventory):
    """Select matching source inputs, not the binary build tools in vcpkg's cache."""
    required = set()
    for document in inventory:
        for package in document["packages"]:
            if not package["SPDXID"].startswith("SPDXRef-resource-"):
                continue
            checksums = [
                entry["checksumValue"].lower()
                for entry in package.get("checksums", [])
                if entry["algorithm"] == "SHA512"
            ]
            if not checksums:
                raise RuntimeError(f"Dependency source lacks a SHA512 checksum: {package['name']}")
            required.update(checksums)
    selected = []
    found = set()
    for path in sorted(downloads.iterdir()):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha512").hexdigest()
        if checksum in required:
            selected.append((path, f"dependency-downloads/{path.name}"))
            found.add(checksum)
    if missing := required - found:
        raise RuntimeError(
            f"Corresponding dependency source archives are missing: {sorted(missing)}"
        )
    return selected


def make_source_archive(destination, paths):
    """Ship original source archives, vcpkg patches/recipes and build provenance.

    vcpkg downloads retain the hash-verified dependency source tarballs. Its
    complete pinned source checkout supplies all ports, patches and build tools'
    acquisition recipes. No binary-cache input is allowed during preparation.
    """
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path, name in paths:
                archive.add(path, arcname=name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_dependencies(executable, target_name):
    """Reject runner-specific shared libraries without executing the new player."""
    if target_name.startswith("darwin"):
        linked = subprocess.check_output(["otool", "-L", executable], text=True)
        dependencies = [line.strip().split(" (", 1)[0] for line in linked.splitlines()[1:]]
        unexpected = [
            name for name in dependencies if not name.startswith(("/usr/lib/", "/System/Library/"))
        ]
        load_commands = subprocess.check_output(["otool", "-l", executable], text=True)
        versions = re.findall(
            r"cmd LC_(?:BUILD_VERSION|VERSION_MIN_MACOSX)\n(.*?)(?=Load command|$)",
            load_commands,
            re.S,
        )
        minima = [
            re.search(r"(?:minos|version) (\d+(?:\.\d+)+)", part).group(1) for part in versions
        ]
        if not minima or any(
            tuple(map(int, version.split("."))) > (13, 0, 0) for version in minima
        ):
            raise RuntimeError(f"MPV deployment target exceeds macOS 13: {minima}")
        metadata = {
            "dependencies": dependencies,
            "deployment_targets": minima,
            "load_commands": load_commands,
        }
    elif target_name.startswith("linux"):
        linked = subprocess.check_output(["readelf", "--dynamic", executable], text=True)
        dependencies = re.findall(r"\(NEEDED\).*?\[(.*?)\]", linked)
        system_libraries = {
            "libc.so.6",
            "libm.so.6",
            "libpthread.so.0",
            "libdl.so.2",
            "librt.so.1",
            "libgcc_s.so.1",
            "libstdc++.so.6",
            "ld-linux-x86-64.so.2",
            "libX11.so.6",
            "libXext.so.6",
            "libXfixes.so.3",
            "libXss.so.1",
            "libXpresent.so.1",
            "libXrandr.so.2",
            "libxcb.so.1",
            "libXau.so.6",
            "libXdmcp.so.6",
            "libGL.so.1",
        }
        unexpected = [name for name in dependencies if name not in system_libraries]
        metadata = {"dependencies": dependencies, "dynamic_section": linked}
    else:
        linked = subprocess.check_output(["dumpbin", "/DEPENDENTS", executable], text=True)
        dependencies = sorted(set(re.findall(r"^\s+([\w.-]+\.dll)\s*$", linked, re.M | re.I)))
        system_libraries = {
            "advapi32.dll",
            "avrt.dll",
            "bcrypt.dll",
            "cfgmgr32.dll",
            "combase.dll",
            "comctl32.dll",
            "comdlg32.dll",
            "crypt32.dll",
            "d3d9.dll",
            "d3d11.dll",
            "d3dcompiler_47.dll",
            "dwmapi.dll",
            "dxgi.dll",
            "gdi32.dll",
            "hid.dll",
            "imm32.dll",
            "iphlpapi.dll",
            "kernel32.dll",
            "mf.dll",
            "mfplat.dll",
            "mfuuid.dll",
            "msvcrt.dll",
            "ncrypt.dll",
            "ntdll.dll",
            "ole32.dll",
            "oleaut32.dll",
            "opengl32.dll",
            "powrprof.dll",
            "propsys.dll",
            "rpcrt4.dll",
            "secur32.dll",
            "setupapi.dll",
            "shell32.dll",
            "shlwapi.dll",
            "user32.dll",
            "userenv.dll",
            "usp10.dll",
            "ucrtbase.dll",
            "uxtheme.dll",
            "version.dll",
            "winmm.dll",
            "winspool.drv",
            "ws2_32.dll",
            "wtsapi32.dll",
        }
        unexpected = [
            name
            for name in dependencies
            if name.lower() not in system_libraries
            and not name.lower().startswith(("api-ms-win-", "ext-ms-win-"))
        ]
        metadata = {"dependencies": dependencies, "pe_imports": linked}
    if not dependencies and not (
        target_name.startswith("linux") and "There is no dynamic section" in linked
    ):
        raise RuntimeError("Could not inspect native executable's dependency table")
    if unexpected:
        raise RuntimeError(f"Unbundled non-system native dependencies: {unexpected}")
    return metadata


def build_uosc_helper(manifest, target_name, stage):
    """Compile the bundled UI helper instead of redistributing an old Go runtime."""
    if not shutil.which("go"):
        raise RuntimeError("Go 1.21 or newer is required to build uosc's native helper.")
    source = ROOT / "assets" / "mpv" / "uosc" / "sources" / "ziggy"
    helper = stage / "bin" / ("ziggy.exe" if target_name.startswith("windows") else "ziggy")
    helper.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        GOTOOLCHAIN=f"go{manifest['go_version']}",
        GOWORK="off",
        CGO_ENABLED="0",
        GOOS=target_name.split("-")[0],
        GOARCH="arm64" if target_name.endswith("arm64") else "amd64",
    )
    run(
        [
            "go",
            "build",
            "-trimpath",
            "-mod=vendor" if (source / "vendor").is_dir() else "-mod=readonly",
            "-buildvcs=false",
            "-ldflags=-s -w",
            "-o",
            helper,
            "./src/ziggy/ziggy.go",
        ],
        cwd=source,
        env=env,
    )
    if target_name.startswith("darwin"):
        run(["codesign", "--force", "--sign", "-", helper], cwd=source, env=env)
    metadata = {
        "executable": helper.relative_to(stage).as_posix(),
        "sha256": sha256(helper),
        "go_build_info": subprocess.check_output(
            ["go", "version", "-m", helper], env=env, text=True
        ),
        "source_provenance_sha256": sha256(ROOT / "assets" / "mpv" / "uosc" / "PROVENANCE.json"),
        "native_dependencies": inspect_dependencies(helper, target_name),
    }
    write_json(stage / "uosc-build.json", metadata)
    return metadata


def build_runtime(manifest, target_name, cache, stage, source_output):
    target = manifest["targets"][target_name]
    for executable in ("cmake", "ninja", "nasm", "uv", "git"):
        if not shutil.which(executable):
            raise RuntimeError(
                f"Required native build tool missing: {executable}. See this script's docstring."
            )
    if target_name.startswith("windows") and not shutil.which("cl"):
        raise RuntimeError("Run preparation from a Visual Studio 2022 x64 developer shell.")
    uosc = build_uosc_helper(manifest, target_name, stage)
    # Keep failed work/logs for diagnosis. A successful rerun uses a new workdir,
    # avoiding artifacts built with changed compiler flags or source pins.
    work = Path(tempfile.mkdtemp(prefix=f"build-{target_name}-", dir=cache))
    print(f"Native build workspace: {work}", flush=True)
    originals = work / "original-sources"
    originals.mkdir()
    sources = {}
    for name, artifact in manifest["sources"].items():
        archive = download(artifact, cache / "archives")
        shutil.copy2(archive, originals / name)
        sources[name] = source_directory(archive, work / name.removesuffix(".tar.gz"))
    vcpkg = sources["vcpkg.tar.gz"]
    mpv = sources["mpv.tar.gz"]
    placebo = sources["libplacebo.tar.gz"]
    for name in ("Vulkan-Headers", "fast_float", "glad", "jinja", "markupsafe"):
        destination = placebo / "3rdparty" / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(sources[f"{name}.tar.gz"], destination, symlinks=True)
    recipe = work / "recipe"
    recipe.mkdir()
    (recipe / "scripts").mkdir()
    (recipe / "native").mkdir()
    shutil.copy2(__file__, recipe / "scripts" / "prepare_mpv.py")
    shutil.copy2(MANIFEST, recipe / "native" / "mpv-manifest.json")
    shutil.copy2(ROOT / "native" / "REDISTRIBUTION.txt", recipe / "native" / "REDISTRIBUTION.txt")
    shutil.copytree(ROOT / "native" / "patches", recipe / "native" / "patches")
    shutil.copytree(ROOT / "assets" / "mpv", recipe / "assets" / "mpv")
    helper_source = recipe / "assets" / "mpv" / "uosc" / "sources" / "ziggy"
    helper_env = os.environ.copy()
    helper_env.update(GOTOOLCHAIN=f"go{manifest['go_version']}", GOWORK="off")
    run(["go", "mod", "vendor"], cwd=helper_source, env=helper_env)
    write_json(recipe / "uosc-build.json", uosc)
    triplets = recipe / "triplets"
    triplets.mkdir()
    make_triplet(triplets / f"{target['triplet']}.cmake", target)
    dependencies = [
        {
            "name": "ffmpeg",
            "default-features": False,
            "features": [
                "avcodec",
                "avfilter",
                "avformat",
                "swresample",
                "swscale",
                "gpl",
                "version3",
                "openssl",
                "zlib",
                "dav1d",
                "opus",
            ],
        },
        "libass",
        "luajit",
        {"name": "pkgconf", "host": True},
    ]
    if target_name.startswith("linux"):
        dependencies.append({"name": "sdl2", "default-features": False, "features": ["alsa"]})
    write_json(
        recipe / "vcpkg.json",
        {"name": "qitv-native-mpv", "version-string": "1", "dependencies": dependencies},
    )
    downloads = work / "vcpkg-downloads"
    downloads.mkdir()
    installed = work / "installed"
    env = os.environ.copy()
    env.update(
        {
            "VCPKG_ROOT": str(vcpkg),
            "VCPKG_DOWNLOADS": str(downloads),
            "VCPKG_DISABLE_METRICS": "1",
            "VCPKG_BINARY_SOURCES": "clear",
            "MACOSX_DEPLOYMENT_TARGET": "13.0",
        }
    )
    for component, source in (("vcpkg", vcpkg), ("mpv", mpv)):
        for patch in sorted((recipe / "native" / "patches" / component).glob("*.patch")):
            run(["git", "apply", patch], cwd=source, env=env)
    if target_name.startswith("windows"):
        run(["cmd", "/c", vcpkg / "bootstrap-vcpkg.bat", "-disableMetrics"], cwd=vcpkg, env=env)
        tool = vcpkg / "vcpkg.exe"
    else:
        run(["sh", vcpkg / "bootstrap-vcpkg.sh", "-disableMetrics"], cwd=vcpkg, env=env)
        tool = vcpkg / "vcpkg"
    run(
        [
            tool,
            "install",
            f"--triplet={target['triplet']}",
            f"--host-triplet={target['host_triplet']}",
            f"--overlay-triplets={triplets}",
            f"--x-manifest-root={recipe}",
            f"--x-install-root={installed}",
            "--no-print-usage",
        ],
        cwd=recipe,
        env=env,
    )
    prefix = installed / target["triplet"]
    pkgconf = (
        installed
        / target["host_triplet"]
        / "tools"
        / "pkgconf"
        / ("pkgconf.exe" if os.name == "nt" else "pkgconf")
    )
    if not pkgconf.is_file():
        raise RuntimeError(f"Pinned pkgconf executable missing: {pkgconf}")
    env["PKG_CONFIG"] = str(pkgconf)
    env["PKG_CONFIG_PATH"] = os.pathsep.join(
        [str(prefix / "lib" / "pkgconfig"), str(prefix / "share" / "pkgconfig")]
    )
    # No Homebrew/user libraries may accidentally satisfy an optional dependency.
    env["PKG_CONFIG_LIBDIR"] = env["PKG_CONFIG_PATH"]
    if target_name.startswith("linux"):
        # X11 and OpenGL are distro/driver APIs, not bundled media libraries.
        # Exclude /usr/local so user-installed libraries cannot satisfy the build.
        env["PKG_CONFIG_LIBDIR"] += os.pathsep + os.pathsep.join(
            [
                "/usr/lib/x86_64-linux-gnu/pkgconfig",
                "/usr/lib64/pkgconfig",
                "/usr/lib/pkgconfig",
                "/usr/share/pkgconfig",
            ]
        )
    env.pop("PKG_CONFIG_SYSROOT_DIR", None)
    env["CMAKE_PREFIX_PATH"] = str(prefix)
    env["PATH"] = str(pkgconf.parent) + os.pathsep + env["PATH"]
    if target_name.startswith("darwin"):
        env["CFLAGS"] = "-mmacosx-version-min=13.0"
        env["CXXFLAGS"] = "-mmacosx-version-min=13.0"
        env["LDFLAGS"] = "-mmacosx-version-min=13.0"
    elif target_name.startswith("windows"):
        for executable in ("clang", "clang++", "lld-link", "llvm-rc"):
            if not shutil.which(executable):
                raise RuntimeError(f"LLVM build tool missing: {executable}")
        # Meson's prefer_static file search misses SDK libraries in LIB with
        # GNU-mode Clang. Allow normal SDK linker lookup below, while forcing
        # pkgconf's private dependency closure for the static-only media prefix.
        env.update(
            CC="clang --target=x86_64-pc-windows-msvc",
            CXX="clang++ --target=x86_64-pc-windows-msvc",
            CC_LD="lld-link",
            CXX_LD="lld-link",
            WINDRES="llvm-rc",
            PKG_CONFIG=subprocess.list2cmdline([str(pkgconf), "--static"]),
        )
    meson = ["uv", "tool", "run", "--from", f"meson=={manifest['meson_version']}", "meson"]
    common = [
        "--buildtype=release",
        "--wrap-mode=nodownload",
        "-Ddefault_library=static",
        "-Dprefer_static=false" if target_name.startswith("windows") else "-Dprefer_static=true",
        "-Dauto_features=disabled",
    ]
    if target_name.startswith("windows"):
        common += ["-Db_vscrt=mt"]
    placebo_build = work / "placebo-build"
    run(
        meson
        + ["setup", placebo_build, placebo, f"--prefix={prefix}", "--libdir=lib"]
        + common
        + [
            "-Ddemos=false",
            "-Dtests=false",
            "-Dopengl=enabled",
            "-Dgl-proc-addr=enabled",
        ],
        cwd=work,
        env=env,
    )
    run(meson + ["compile", "-C", placebo_build], cwd=work, env=env)
    run(meson + ["install", "-C", placebo_build], cwd=work, env=env)
    mpv_build = work / "mpv-build"
    options = ["-Dlibmpv=false", "-Dcplayer=true", "-Dlua=luajit", "-Dgpl=true", "-Dgl=enabled"]
    if target_name.startswith("darwin"):
        architecture = "x86_64" if target["architecture"] == "x64" else "arm64"
        options += [
            "-Dcocoa=enabled",
            "-Dgl-cocoa=enabled",
            "-Dmacos-cocoa-cb=enabled",
            "-Dcoreaudio=enabled",
            "-Dswift-build=enabled",
            f"-Dswift-flags=-target {architecture}-apple-macosx13.0",
        ]
    elif target_name.startswith("windows"):
        options += ["-Dgl-win32=enabled", "-Dwasapi=enabled", "-Dwin32-threads=enabled"]
    else:
        options += ["-Dx11=enabled", "-Dgl-x11=enabled", "-Dsdl2-audio=enabled"]
    run(
        meson + ["setup", mpv_build, mpv, f"--prefix={stage}"] + common + options, cwd=work, env=env
    )
    run(meson + ["compile", "-C", mpv_build], cwd=work, env=env)
    executable_name = "mpv.exe" if target_name.startswith("windows") else "mpv"
    executable = stage / "bin" / executable_name
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mpv_build / executable_name, executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if target_name.startswith("darwin"):
        run(["codesign", "--force", "--sign", "-", executable], cwd=work, env=env)
    native_dependencies = inspect_dependencies(executable, target_name)
    write_json(stage / "native-dependencies.json", native_dependencies)
    write_json(recipe / "native-dependencies.json", native_dependencies)
    licenses = stage / "licenses"
    copy_licenses(prefix / "share", licenses / "dependencies")
    copy_licenses(mpv, licenses / "mpv")
    copy_licenses(placebo, licenses / "libplacebo")
    copy_licenses(helper_source / "vendor", licenses / "uosc-dependencies")
    shutil.copy2(ROOT / "native" / "REDISTRIBUTION.txt", licenses / "REDISTRIBUTION.txt")
    # SPDX port manifests retain the exact installed versions and source hashes.
    inventory = []
    for file in (prefix / "share").rglob("vcpkg.spdx.json"):
        destination = licenses / "dependencies" / file.relative_to(prefix / "share")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)
        inventory.append(json.loads(file.read_text(encoding="utf-8")))
    write_json(recipe / "dependency-spdx.json", inventory)
    for source, name in [(placebo_build, "libplacebo"), (mpv_build, "mpv")]:
        shutil.copytree(source / "meson-info", recipe / f"{name}-meson-info")
        shutil.copy2(source / "meson-logs" / "meson-log.txt", recipe / f"{name}-meson-log.txt")
    make_source_archive(
        source_output,
        [
            (originals, "original-sources"),
            (recipe, "recipe"),
            (licenses, "licenses"),
        ]
        + dependency_sources(downloads, inventory),
    )
    return {
        "executable": executable.relative_to(stage).as_posix(),
        "version": manifest["mpv_version"],
        "redistributable": True,
        "source_archive": source_output.name,
        "source_sha256": sha256(source_output),
        "minimum_os": target["minimum_os"],
        "build_workspace": str(work),
        "source_manifest_sha256": sha256(MANIFEST),
        "uosc": uosc,
        "system_dependencies": "OS SDK/runtime and desktop audio/display drivers; all media libraries statically linked",
    }


def publish(stage, destination):
    """Publish only the complete prepared directory, with rollback on rename failure."""
    backup = stage.parent / "previous-runtime"
    if destination.is_symlink():
        raise ValueError("Refusing to replace a symlink runtime directory")
    had_previous = destination.exists()
    if had_previous:
        destination.replace(backup)
    try:
        stage.replace(destination)
    except BaseException:
        if had_previous:
            backup.replace(destination)
        raise
    if had_previous:
        shutil.rmtree(backup)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", default=host_target())
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "qitv" / "mpv")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.target not in manifest["targets"]:
        parser.error(
            f"Unsupported target {args.target}; supported: {', '.join(manifest['targets'])}"
        )
    if args.target != host_target():
        parser.error("Native source builds must run on the matching OS/architecture runner.")
    destination = ROOT / "native" / "mpv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    args.cache_dir = args.cache_dir.expanduser().resolve()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    lock = destination.parent / ".mpv-prepare.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(
            f"Another preparation owns {lock}. Remove it only if that preparation was interrupted."
        )
    try:
        with os.fdopen(lock_fd, "w") as stream:
            stream.write(str(os.getpid()))
        with tempfile.TemporaryDirectory(prefix=".mpv-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "runtime"
            stage.mkdir()
            sources = destination.parent / f"mpv-sources-{args.target}.tar.gz"
            metadata = build_runtime(manifest, args.target, args.cache_dir, stage, sources)
            metadata.update({"schema": 1, "target": args.target})
            metadata["runtime_bytes"] = sum(
                p.stat().st_size for p in stage.rglob("*") if p.is_file()
            )
            write_json(stage / "bundle.json", metadata)
            publish(stage, destination)
            print(json.dumps(metadata, indent=2))
            print(f"Prepared {destination / metadata['executable']}")
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"MPV preparation failed: {exc}") from exc
