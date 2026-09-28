#!/usr/bin/env python3
"""Build, repair, verify and install private LGPL-only PyAV wheels.

Run through uv with --frozen --no-sync. A verified cache hit reinstalls the wheel
without compiling. Windows needs MSVC x64, NASM and MSYS2 bash/make/pkgconf;
macOS needs Xcode tools, make, NASM and pkg-config; Linux additionally needs
binutils and Perl. Build tools live in a disposable, version-pinned environment.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile

try:
    from . import prepare_mpv as native
except ImportError:
    import prepare_mpv as native

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "native" / "pyav-manifest.json"
INPUTS = (
    "scripts/prepare_pyav.py",
    "scripts/prepare_mpv.py",
    "native/pyav-manifest.json",
    "native/REDISTRIBUTION.txt",
)
LIBRARIES = {"avcodec", "avformat", "avdevice", "avutil", "avfilter", "swscale", "swresample"}
LINUX_SYSTEM = {
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "libgcc_s.so.1",
    "ld-linux-x86-64.so.2",
}
WINDOWS_SYSTEM = {
    "kernel32.dll",
    "user32.dll",
    "advapi32.dll",
    "ole32.dll",
    "oleaut32.dll",
    "ws2_32.dll",
    "secur32.dll",
    "ncrypt.dll",
    "crypt32.dll",
    "bcrypt.dll",
    "shell32.dll",
    "shlwapi.dll",
    "psapi.dll",
    "gdi32.dll",
    "winmm.dll",
    "ntdll.dll",
    "msvcrt.dll",
    "ucrtbase.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "mfplat.dll",
    "mfuuid.dll",
    "strmiids.dll",
    "avrt.dll",
    "version.dll",
}


def source_names(target):
    if target.startswith("linux"):
        return ("ffmpeg", "av", "openssl")
    if target.startswith("windows"):
        # delvewheel injects its MIT-licensed DLL bootstrap into av/__init__.py.
        return ("ffmpeg", "av", "delvewheel")
    return ("ffmpeg", "av")


def input_hashes(root):
    result = {}
    for name in INPUTS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or symlinked native input: {path}")
        result[name] = native.sha256(path)
    return result


def native_cache_key(root, target, toolchain):
    identity = {"target": target, "inputs": input_hashes(root), "toolchain": toolchain}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return f"qitv-pyav-v1-{target}-{digest}"


def output(command, env=None):
    return subprocess.check_output(list(map(str, command)), text=True, env=env, errors="replace")


def posix_tool(name, target):
    if target.startswith("windows"):
        path = (
            Path(os.environ.get("QITV_MSYS2_ROOT", "C:/msys64")) / "usr" / "bin" / (name + ".exe")
        )
        if not path.is_file():
            raise RuntimeError(f"Required MSYS2 tool missing: {path}")
        return str(path)
    return name


def toolchain_identity(target):
    commands = {
        "uv": ["uv", "--version"],
        "make": [posix_tool("make", target), "--version"],
        "nasm": ["nasm", "-v"],
        "bash": [posix_tool("bash", target), "--version"],
    }
    if target.startswith("windows"):
        commands.update(clang=["clang", "--version"], linker=["lld-link", "--version"])
        compiler = subprocess.run(["cl"], capture_output=True, text=True, check=False)
        if compiler.returncode not in (0, 2) or "Microsoft" not in compiler.stderr:
            raise RuntimeError("MSVC x64 developer environment is required")
        versions = {"msvc": compiler.stderr.strip()}
    else:
        commands.update(
            cc=shlex.split(os.environ.get("CC", "cc")) + ["--version"],
            pkg_config=["pkg-config", "--version"],
        )
        versions = {}
        if target.startswith("darwin"):
            commands["sdk"] = ["xcrun", "--sdk", "macosx", "--show-sdk-build-version"]
        else:
            commands.update(
                libc=["getconf", "GNU_LIBC_VERSION"],
                linker=["ld", "--version"],
                perl=["perl", "-v"],
            )
    versions.update({name: output(command).strip() for name, command in commands.items()})
    return {
        "tools": versions,
        "python": sys.version,
        "abi": sysconfig.get_config_var("SOABI"),
        "platform": platform.platform(),
        "environment": {
            name: os.environ.get(name, "")
            for name in (
                "CC",
                "CXX",
                "CFLAGS",
                "CPPFLAGS",
                "CXXFLAGS",
                "LDFLAGS",
                "SDKROOT",
                "DEVELOPER_DIR",
                "WindowsSDKVersion",
                "VCToolsVersion",
                "VSCMD_VER",
                "CL",
                "_CL_",
                "LINK",
                "INCLUDE",
                "LIB",
                "MACOSX_DEPLOYMENT_TARGET",
            )
        },
    }


def validate_ffmpeg(info):
    """A license string alone is insufficient: reject enabling external codecs too."""
    options = shlex.split(info["configuration"])
    forbidden = [
        arg
        for arg in options
        if arg.split("=", 1)[0] in ("--enable-gpl", "--enable-nonfree")
        or arg.startswith("--enable-lib")
        or "x264" in arg
        or "x265" in arg
    ]
    required = {"--disable-autodetect", "--disable-gpl", "--disable-nonfree", "--enable-version3"}
    if forbidden or not required.issubset(options) or info["license"] != "LGPL version 3 or later":
        raise ValueError(f"Not a verified LGPL-only FFmpeg configuration: {info}")
    if info["version"] != "8.1.2":
        raise ValueError(f"Unexpected FFmpeg version: {info['version']}")


def validate_sources(sources, manifest, target, inputs):
    """Validate the actual sidecar members, not merely an attacker-updated outer hash."""
    required = {"recipe/" + name: digest for name, digest in inputs.items()}
    for name in source_names(target):
        artifact = manifest["sources"][name]
        required["sources/" + artifact["filename"]] = artifact["sha256"]
    with tarfile.open(sources, "r:gz") as archive:
        members = archive.getmembers()
        files = {member.name: member for member in members if member.isfile()}
        if len(files) != sum(member.isfile() for member in members):
            raise ValueError("Duplicate source archive members")
        for name, digest in required.items():
            member = files.get(name)
            if member is None:
                raise ValueError(f"Missing corresponding source/recipe: {name}")
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(f"Changed corresponding source/recipe: {name}")
        for name in source_names(target):
            license_name = f"licenses/{name}/{manifest['sources'][name]['license']}"
            if license_name not in files or files[license_name].size == 0:
                raise ValueError(f"Missing source license: {license_name}")


def cached_runtime_valid(directory, sources, target, key, manifest, inputs):
    try:
        if (directory / "bundle.json").is_symlink() or sources.is_symlink():
            return False
        metadata = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
        wheel = metadata["wheel"]
        if PurePosixPath(wheel).name != wheel or not wheel.endswith(".whl"):
            return False
        if (
            metadata["schema"] != 1
            or metadata["target"] != target
            or metadata["cache_key"] != key
            or metadata["inputs"] != inputs
            or metadata["source_archive"] != sources.name
            or metadata["source_sha256"] != native.sha256(sources)
            or metadata["wheel_sha256"] != native.sha256(directory / wheel)
            or metadata["sources"] != {n: manifest["sources"][n] for n in source_names(target)}
            or metadata["files"] != native.runtime_inventory(directory)
        ):
            return False
        for name in source_names(target):
            license_path = directory / "licenses" / name / manifest["sources"][name]["license"]
            if not license_path.is_file() or license_path.stat().st_size == 0:
                return False
        validate_ffmpeg(metadata["ffmpeg"])
        if set(metadata["native_libraries"]) != LIBRARIES or not metadata["binary_inspection"]:
            return False
        validate_sources(sources, manifest, target, inputs)
        return True
    except OSError, ValueError, KeyError, TypeError, EOFError, tarfile.TarError:
        return False


def unpack_wheel(wheel, directory):
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.filename
                or ":" in member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError(f"Unsafe wheel member: {member.filename}")
        archive.extractall(directory)


def library_name(path):
    match = re.match(
        r"^(?:lib)?(avcodec|avformat|avdevice|avutil|avfilter|swscale|swresample)[.-]", path.name
    )
    return match.group(1) if match else None


def inspect_wheel(wheel, target, directory):
    """Check every binary, recursively closing imports within the repaired wheel."""
    unpack_wheel(wheel, directory)
    binaries = [
        p
        for p in directory.rglob("*")
        if p.is_file()
        and (p.suffix.lower() in (".dylib", ".so", ".pyd", ".dll") or ".so." in p.name)
    ]
    available = {p.name.lower(): p for p in binaries}
    libraries = {
        library_name(p): p.relative_to(directory).as_posix() for p in binaries if library_name(p)
    }
    if set(libraries) != LIBRARIES:
        raise ValueError(f"Incomplete FFmpeg native closure: {sorted(libraries)}")
    inspections = {}
    for binary in binaries:
        if re.search(r"(?:x264|x265|postproc)", binary.name, re.I):
            raise ValueError(f"GPL library bundled in wheel: {binary.name}")
        relative = binary.relative_to(directory).as_posix()
        if target.startswith("darwin"):
            linked = output(["otool", "-L", binary])
            dependencies = [line.strip().split(" (", 1)[0] for line in linked.splitlines()[1:]]
            commands = output(["otool", "-l", binary])
            identities = re.findall(r"cmd LC_ID_DYLIB\n.*?name (.*?) \(offset", commands, re.S)
            dependencies = [dep for dep in dependencies if dep not in identities]
            blocks = re.findall(
                r"cmd LC_(?:BUILD_VERSION|VERSION_MIN_MACOSX)\n(.*?)(?=Load command|$)",
                commands,
                re.S,
            )
            minima = [
                re.search(r"(?:minos|version) (\d+(?:\.\d+)+)", block).group(1) for block in blocks
            ]
            if not minima or any(tuple(map(int, v.split("."))) > (13, 0, 0) for v in minima):
                raise ValueError(f"Binary exceeds macOS 13 deployment target: {relative}: {minima}")
            # delocate emits loader-relative references, never a build-prefix rpath.
            for dep in dependencies:
                if dep.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if not dep.startswith("@loader_path/"):
                    raise ValueError(f"Non-portable Mach-O dependency: {relative}: {dep}")
                resolved = (binary.parent / dep.removeprefix("@loader_path/")).resolve()
                if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
                    raise ValueError(f"Missing Mach-O dependency: {relative}: {dep}")
            inspections[relative] = {"dependencies": dependencies, "minimum_os": minima}
        elif target.startswith("linux"):
            linked = output(["readelf", "--dynamic", binary])
            dependencies = re.findall(r"\(NEEDED\).*?\[(.*?)\]", linked)
            paths = re.findall(r"\((?:RPATH|RUNPATH)\).*?\[(.*?)\]", linked)
            roots = []
            for entry in (part for value in paths for part in value.split(":")):
                if not entry.startswith(("$ORIGIN", "${ORIGIN}")):
                    raise ValueError(f"Non-portable ELF search path: {relative}: {entry}")
                resolved = Path(
                    entry.replace("${ORIGIN}", str(binary.parent)).replace(
                        "$ORIGIN", str(binary.parent)
                    )
                ).resolve()
                if not resolved.is_relative_to(directory.resolve()):
                    raise ValueError(f"Escaping ELF search path: {relative}: {entry}")
                roots.append(resolved)
            for dep in dependencies:
                if dep not in LINUX_SYSTEM and not any((root / dep).is_file() for root in roots):
                    raise ValueError(f"Unbundled ELF dependency: {relative}: {dep}")
            versions = output(["readelf", "--version-info", binary])
            glibc = sorted(
                set(re.findall(r"\bGLIBC_(\d+\.\d+)", versions)),
                key=lambda v: tuple(map(int, v.split("."))),
            )
            if any(tuple(map(int, v.split("."))) > (2, 35) for v in glibc):
                raise ValueError(f"Binary exceeds glibc 2.35: {relative}: {glibc}")
            inspections[relative] = {
                "dependencies": dependencies,
                "minimum_os": "glibc " + (glibc[-1] if glibc else "none"),
                "rpaths": paths,
            }
        else:
            linked = output(["dumpbin", "/DEPENDENTS", binary])
            dependencies = sorted(set(re.findall(r"^\s+([\w.-]+\.dll)\s*$", linked, re.M | re.I)))
            for dep in dependencies:
                name = dep.lower()
                if (
                    name not in WINDOWS_SYSTEM
                    and not name.startswith(("api-ms-win-", "ext-ms-win-"))
                    and not re.fullmatch(r"python3(?:\d+)?(?:_d)?\.dll", name)
                    and name not in available
                ):
                    raise ValueError(f"Unbundled PE dependency: {relative}: {dep}")
            headers = output(["dumpbin", "/HEADERS", binary])
            minima = re.findall(
                r"^\s*([\d.]+) (?:operating system|subsystem) version", headers, re.M
            )
            if not minima or any(tuple(map(int, v.split("."))) > (10, 0) for v in minima):
                raise ValueError(f"Unexpected Windows minimum version: {relative}: {minima}")
            inspections[relative] = {"dependencies": dependencies, "minimum_os": minima}
        if any(re.search(r"(?:x264|x265|postproc)", dep, re.I) for dep in dependencies):
            raise ValueError(f"GPL native dependency in {relative}")
    return libraries, inspections


# Runs in an isolated interpreter, after installing only the repaired wheel. No
# project imports, registry PyAV, compiler library paths or source tree are used.
PROBE = r'''
import av, ctypes, io, json
from pathlib import Path
root = Path(av.__file__).parent.parent
libraries = json.loads(__import__('sys').argv[1])
info = {}
handles = []
for name, relative in libraries.items():
    handle = ctypes.CDLL(str(root / relative))
    handles.append(handle)
    values = {}
    for suffix in ('configuration', 'license'):
        function = getattr(handle, name + '_' + suffix)
        function.restype = ctypes.c_char_p
        values[suffix] = function().decode()
    info[name] = values
util = handles[list(libraries).index('avutil')]
util.av_version_info.restype = ctypes.c_char_p
info['avcodec']['version'] = util.av_version_info().decode()
assert av.__version__ == '18.1.0', av.__version__
for codec in ('mpeg2video', 'mp2', 'aac'):
    av.Codec(codec, 'w')
for format in ('mpegts', 'hls', 'rtsp'):
    av.format.ContainerFormat(format)
buffer = io.BytesIO()
with av.open(buffer, 'w', format='mpegts') as container:
    stream = container.add_stream('mpeg2video', rate=25)
    stream.width, stream.height, stream.pix_fmt = 64, 48, 'yuv420p'
    for index in range(3):
        frame = av.VideoFrame(64, 48, 'yuv420p')
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = index
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
buffer.seek(0)
with av.open(buffer, format='mpegts') as container:
    frames = list(container.decode(video=0))
assert len(frames) == 3 and all((f.width, f.height) == (64, 48) for f in frames)
print(json.dumps({'av_file': av.__file__, 'av_version': av.__version__, 'library_versions': av.library_versions,
                  'ffmpeg': info['avcodec'], 'library_licenses': info, 'roundtrip_frames': len(frames)}))
'''


def clean_environment():
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
    ):
        environment.pop(name, None)
    return environment


def python_in(venv):
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_wheel(wheel, python, env):
    native.run(
        ["uv", "pip", "install", "--python", python, "--no-deps", "--reinstall", wheel],
        cwd=wheel.parent,
        env=env,
    )


def probe_wheel(wheel, libraries, temporary):
    environment = clean_environment()
    venv = temporary / "probe-env"
    native.run(["uv", "venv", "--python", sys.executable, venv], cwd=temporary, env=environment)
    python = python_in(venv)
    install_wheel(wheel, python, environment)
    result = json.loads(output([python, "-I", "-c", PROBE, json.dumps(libraries)], environment))
    validate_ffmpeg(result["ffmpeg"])
    for info in result["library_licenses"].values():
        validate_ffmpeg({**info, "version": result["ffmpeg"]["version"]})
    return result


def build_environment(prefix, target):
    environment = clean_environment()
    for name in (
        "CFLAGS",
        "CPPFLAGS",
        "CXXFLAGS",
        "LDFLAGS",
        "CL",
        "_CL_",
        "LINK",
        "PKG_CONFIG_PATH",
    ):
        environment.pop(name, None)
    environment["PKG_CONFIG_LIBDIR"] = str(prefix / "lib" / "pkgconfig")
    if target.startswith("darwin"):
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
        environment["CFLAGS"] = "-mmacosx-version-min=13.0"
        environment["LDFLAGS"] = "-mmacosx-version-min=13.0"
    elif target.startswith("windows"):
        environment.update(MSYS2_ARG_CONV_EXCL="*", MSYS_NO_PATHCONV="1")
        msys_bin = str(Path(posix_tool("bash", target)).parent)
        environment["PATH"] = environment["PATH"] + os.pathsep + msys_bin
        environment["SHELL"] = posix_tool("bash", target).replace("\\", "/")
        environment["INCLUDE"] = (
            str(prefix / "include") + os.pathsep + environment.get("INCLUDE", "")
        )
        environment["LIB"] = str(prefix / "lib") + os.pathsep + environment.get("LIB", "")
    else:
        environment["LD_LIBRARY_PATH"] = str(prefix / "lib")
    return environment


def shell_path(path):
    # MSYS bash accepts C:/...; backslashes would be shell escapes.
    return Path(path).resolve().as_posix()


def build_runtime(manifest, target, cache, stage, sources, toolchain, inputs):
    work = stage.parent
    prefix = work / "prefix"
    prefix.mkdir()
    environment = build_environment(prefix, target)
    archives, trees = {}, {}
    for name in source_names(target):
        archives[name] = native.download(manifest["sources"][name], cache)
        trees[name] = native.source_directory(archives[name], work / (name + "-source"))
        license_path = trees[name] / manifest["sources"][name]["license"]
        if not license_path.is_file() or license_path.stat().st_size == 0:
            raise ValueError(f"Missing upstream license: {license_path}")
        native.copy_licenses(trees[name], stage / "licenses" / name)
    shutil.copy2(ROOT / "native" / "REDISTRIBUTION.txt", stage / "licenses" / "REDISTRIBUTION.txt")
    jobs = str(os.cpu_count() or 2)
    if target.startswith("linux"):
        native.run(
            [
                "perl",
                "Configure",
                "linux-x86_64",
                "shared",
                "no-tests",
                "no-module",
                f"--prefix={prefix}",
                "--libdir=lib",
                "--openssldir=/etc/ssl",
            ],
            cwd=trees["openssl"],
            env=environment,
        )
        native.run(["make", "-j" + jobs], cwd=trees["openssl"], env=environment)
        native.run(["make", "install_sw"], cwd=trees["openssl"], env=environment)
    flags = manifest["ffmpeg_options"] + [
        f"--prefix={shell_path(prefix)}",
        "--enable-" + manifest["targets"][target]["tls"],
    ]
    if target.startswith("darwin"):
        flags += [
            "--extra-cflags=-mmacosx-version-min=13.0",
            "--extra-ldflags=-mmacosx-version-min=13.0",
        ]
    elif target.startswith("windows"):
        flags += [
            "--toolchain=msvc",
            "--arch=x86_64",
            "--target-os=win64",
            "--shlibdir=" + shell_path(prefix / "lib"),
        ]
    native.run(
        [posix_tool("bash", target), "./configure", *flags], cwd=trees["ffmpeg"], env=environment
    )
    config = (trees["ffmpeg"] / "config.h").read_text()
    components = (trees["ffmpeg"] / "config_components.h").read_text()
    required = ("NETWORK", "AVCODEC", "AVFORMAT", "AVDEVICE", "AVFILTER", "SWSCALE", "SWRESAMPLE")
    required_components = (
        "MPEGTS_DEMUXER",
        "MPEGTS_MUXER",
        "HLS_DEMUXER",
        "RTSP_DEMUXER",
        "UDP_PROTOCOL",
        "TCP_PROTOCOL",
        "HTTP_PROTOCOL",
        "HTTPS_PROTOCOL",
        "TLS_PROTOCOL",
        "CRYPTO_PROTOCOL",
        "MPEG2VIDEO_ENCODER",
        "MP2_ENCODER",
        "AAC_ENCODER",
    )
    for name in required + (manifest["targets"][target]["tls"].upper(),):
        if not re.search(rf"^#define CONFIG_{name} 1$", config, re.M):
            raise ValueError(f"Missing FFmpeg capability: {name}")
    for name in required_components:
        if not re.search(rf"^#define CONFIG_{name} 1$", components, re.M):
            raise ValueError(f"Missing FFmpeg component: {name}")
    for name in ("GPL", "NONFREE", "LIBX264", "LIBX265"):
        if re.search(rf"^#define CONFIG_{name} 1$", config, re.M):
            raise ValueError(f"Forbidden FFmpeg configuration: {name}")
    native.run([posix_tool("make", target), "-j" + jobs], cwd=trees["ffmpeg"], env=environment)
    native.run([posix_tool("make", target), "install"], cwd=trees["ffmpeg"], env=environment)
    build_env = work / "build-env"
    native.run(["uv", "venv", "--python", sys.executable, build_env], cwd=work, env=environment)
    python = python_in(build_env)
    system = target.split("-", 1)[0]
    tools = manifest["python_tools"]["common"] + manifest["python_tools"][system]
    native.run(
        ["uv", "pip", "install", "--python", python, "--no-deps", *tools], cwd=work, env=environment
    )
    tool_versions = json.loads(
        output(
            [
                python,
                "-I",
                "-c",
                "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name'].lower():d.version for d in m.distributions()}))",
            ]
        )
    )
    for requirement in tools:
        name, version = requirement.split("==")
        if tool_versions.get(name.lower()) != version:
            raise ValueError(f"Build tool version mismatch: {requirement}")
    raw = work / "unrepaired"
    native.run(
        [python, "setup.py", "--ffmpeg-dir=" + str(prefix), "bdist_wheel", "--dist-dir", raw],
        cwd=trees["av"],
        env=environment,
    )
    wheels = list(raw.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected exactly one built PyAV wheel")
    repair_env = environment.copy()
    repair_env["PATH"] = str(python.parent) + os.pathsep + repair_env["PATH"]
    if system == "darwin":
        command = [
            python.parent / "delocate-wheel",
            "--require-archs",
            target.split("-", 1)[1],
            "--require-target-macos-version",
            "13.0",
            "-w",
            stage,
            wheels[0],
        ]
    elif system == "linux":
        repair_env["LD_LIBRARY_PATH"] = str(prefix / "lib")
        command = [
            python,
            "-m",
            "auditwheel",
            "repair",
            "--plat",
            manifest["targets"][target]["wheel_platform"],
            "-w",
            stage,
            wheels[0],
        ]
    else:
        command = [
            python,
            "-m",
            "delvewheel",
            "repair",
            "--add-path",
            prefix / "lib",
            "-w",
            stage,
            wheels[0],
        ]
    native.run(command, cwd=work, env=repair_env)
    repaired = list(stage.glob("*.whl"))
    if len(repaired) != 1:
        raise ValueError("Expected exactly one repaired PyAV wheel")
    wheel = repaired[0]
    libraries, inspections = inspect_wheel(wheel, target, work / "wheel-inspection")
    # Hide the entire native prefix before execution to catch accidental linkage.
    hidden = work / "unavailable-build-prefix"
    prefix.rename(hidden)
    try:
        probe = probe_wheel(wheel, libraries, work)
    finally:
        hidden.rename(prefix)
    probe.pop("av_file")
    recipe = work / "recipe"
    for name in INPUTS:
        destination = recipe / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    native.write_json(recipe / "toolchain.json", toolchain)
    native.write_json(recipe / "python-tools.json", tool_versions)
    native.write_json(recipe / "ffmpeg-options.json", flags)
    shutil.copy2(trees["ffmpeg"] / "config.h", recipe / "ffmpeg-config.h")
    shutil.copy2(trees["ffmpeg"] / "config_components.h", recipe / "ffmpeg-components.h")
    paths = [(recipe, "recipe"), (stage / "licenses", "licenses")]
    paths.extend(
        (archives[name], "sources/" + manifest["sources"][name]["filename"])
        for name in source_names(target)
    )
    native.make_source_archive(sources, paths)
    validate_sources(sources, manifest, target, inputs)
    return {
        "wheel": wheel.name,
        "wheel_sha256": native.sha256(wheel),
        "sources": {name: manifest["sources"][name] for name in source_names(target)},
        "source_archive": sources.name,
        "source_sha256": native.sha256(sources),
        "toolchain": toolchain,
        "python_tools": tool_versions,
        "inputs": inputs,
        "native_libraries": libraries,
        "binary_inspection": inspections,
        "minimum_os": manifest["targets"][target]["minimum_os"],
        **probe,
    }


def validate_installed(destination: Path) -> dict:
    """Fail closed before freezing if registry PyAV replaced the private install."""
    destination = Path(destination)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    metadata = json.loads((destination / "bundle.json").read_text(encoding="utf-8"))
    target = native.host_target()
    inputs = input_hashes(ROOT)
    key = native_cache_key(ROOT, target, metadata["toolchain"])
    sources = destination.parent / f"pyav-sources-{target}.tar.gz"
    if metadata["toolchain"]["abi"] != sysconfig.get_config_var(
        "SOABI"
    ) or not cached_runtime_valid(destination, sources, target, key, manifest, inputs):
        raise ValueError(
            "Prepared private PyAV wheel, licenses or corresponding sources are invalid"
        )
    with tempfile.TemporaryDirectory(prefix="qitv-pyav-verify-") as temporary:
        extracted = Path(temporary)
        libraries, inspections = inspect_wheel(destination / metadata["wheel"], target, extracted)
        if (
            libraries != metadata["native_libraries"]
            or inspections != metadata["binary_inspection"]
        ):
            raise ValueError("Prepared native closure does not match bundle metadata")
        # Find the installed package without importing its potentially untrusted native code.
        origin = output(
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib.util; s=importlib.util.find_spec('av'); print(s.origin if s else '')",
            ],
            clean_environment(),
        ).strip()
        if not origin:
            raise ValueError("Private PyAV is not installed")
        installed = Path(origin).parent.parent
        package_files = {}
        for path in extracted.rglob("*"):
            relative = path.relative_to(extracted)
            if not path.is_file() or relative.parts[0].endswith((".dist-info", ".data")):
                continue
            actual = installed / relative
            if (
                actual.is_symlink()
                or not actual.is_file()
                or native.sha256(actual) != native.sha256(path)
            ):
                raise ValueError(f"Installed PyAV differs from private wheel: {relative}")
            package_files[relative.as_posix()] = actual
        if "av/__init__.py" not in package_files:
            raise ValueError("Private wheel has no av package")
        for top in {PurePosixPath(name).parts[0] for name in package_files}:
            for actual in (installed / top).rglob("*"):
                if actual.is_file() and (
                    actual.suffix.lower() in (".dll", ".pyd", ".so", ".dylib")
                    or ".so." in actual.name
                ):
                    if actual.relative_to(installed).as_posix() not in package_files:
                        raise ValueError(f"Unexpected installed native library: {actual}")
        result = json.loads(
            output([sys.executable, "-I", "-c", PROBE, json.dumps(libraries)], clean_environment())
        )
        if Path(result["av_file"]).resolve() != Path(origin).resolve():
            raise ValueError("PyAV import changed during verification")
        validate_ffmpeg(result["ffmpeg"])
        for info in result["library_licenses"].values():
            validate_ffmpeg({**info, "version": result["ffmpeg"]["version"]})
        if (
            result["ffmpeg"] != metadata["ffmpeg"]
            or result["library_versions"] != metadata["library_versions"]
        ):
            raise ValueError("Loaded FFmpeg does not match the prepared private runtime")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=native.host_target())
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "qitv" / "pyav")
    parser.add_argument("--print-cache-key", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.target not in manifest["targets"] or args.target != native.host_target():
        parser.error(
            "Native source builds must run on a supported matching OS/architecture runner."
        )
    if sys.prefix == sys.base_prefix:
        parser.error("Run preparation in the application's virtual environment.")
    toolchain = toolchain_identity(args.target)
    inputs = input_hashes(ROOT)
    key = native_cache_key(ROOT, args.target, toolchain)
    if args.print_cache_key:
        print(key)
        return
    destination = ROOT / "native" / "pyav"
    sources = destination.parent / f"pyav-sources-{args.target}.tar.gz"
    lock = destination.parent / ".pyav-prepare.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(
            f"Another preparation owns {lock}; remove only after confirming it was interrupted."
        )
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        if not args.rebuild and cached_runtime_valid(
            destination, sources, args.target, key, manifest, inputs
        ):
            metadata = json.loads((destination / "bundle.json").read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(
                prefix=".pyav-check-", dir=destination.parent
            ) as temporary:
                work = Path(temporary)
                libraries, _ = inspect_wheel(
                    destination / metadata["wheel"], args.target, work / "wheel"
                )
                probe_wheel(destination / metadata["wheel"], libraries, work)
            print(f"Reusing verified private wheel: {key}", flush=True)
        else:
            with tempfile.TemporaryDirectory(prefix=".pyav-", dir=destination.parent) as temporary:
                stage = Path(temporary) / "runtime"
                stage.mkdir()
                metadata = build_runtime(
                    manifest,
                    args.target,
                    args.cache_dir.expanduser().resolve(),
                    stage,
                    sources,
                    toolchain,
                    inputs,
                )
                metadata.update(
                    schema=1,
                    target=args.target,
                    cache_key=key,
                    files=native.runtime_inventory(stage),
                )
                native.write_json(stage / "bundle.json", metadata)
                native.publish(stage, destination)
        install_wheel(destination / metadata["wheel"], sys.executable, clean_environment())
        result = validate_installed(destination)
        print(
            f"Installed verified private PyAV {result['av_version']}: {destination / metadata['wheel']}"
        )
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        raise SystemExit(f"PyAV preparation failed: {exc}") from exc
