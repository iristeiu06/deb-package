#!/usr/bin/env python3
import os
import shlex
import shutil
import tarfile
import logging
import datetime
import argparse
import subprocess
from email.utils import formatdate

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(handler)

def export_env_variable(config):
    config_data = {}
    with open(f"configs/{config}", "r") as config_file:
        for line in config_file:
            if line.startswith("#") or not line.strip():
                continue
            key, _, value = line.partition("=")
            config_data[key.strip()] = shlex.split(value)[0]
    config_data["DATE"] = formatdate(localtime=True)
    config_data["YEAR"] = str(datetime.datetime.now().year)
    if "DEPENDS" in config_data:
        config_data["DEPENDS"] = ", " + config_data["DEPENDS"]
    else:
        config_data["DEPENDS"] = ""
    os.environ.update(config_data)
    return config_data


def download_rpi_boot_files(config_data, build_dir):
    logger.info("Downloading Raspberry Pi boot files...")
    os.makedirs(f"{build_dir}/boot/firmware")
    url = f"{config_data.get('SERVER')}/{config_data.get('FILE_VERSION')}/{config_data.get('BOOT')}"
    logger.info(f"Downloading boot files from {url}...")
    subprocess.run(["wget", "--progress=bar:force:noscroll", url], check=True)
    if tarfile.is_tarfile(config_data.get('BOOT')):
        with tarfile.open(config_data.get('BOOT'), "r:*") as tar:
            tar.extractall(path=f"{build_dir}/boot/firmware")
    os.remove(config_data.get('BOOT'))


def download_rpi_modules(config_data, build_dir):
    logger.info("Downloading Raspberry Pi kernel modules...")
    os.makedirs(f"{build_dir}/lib/modules")
    url = f"{config_data.get('SERVER')}/{config_data.get('FILE_VERSION')}/{config_data.get('MODULES')}"
    logger.info(f"Downloading kernel modules from {url}...")
    subprocess.run(["wget", "--progress=bar:force:noscroll", url], check=True)
    if tarfile.is_tarfile(config_data.get('MODULES')):
        with tarfile.open(config_data.get('MODULES'), "r:*") as tar:
            tar.extractall(path=f"{build_dir}/lib/modules")
    os.remove(config_data.get('MODULES'))


def download_artifacts_rpi(config_data, build_dir):
    download_rpi_boot_files(config_data, build_dir)
    download_rpi_modules(config_data, build_dir)


def download_rpi_version_file(config_data):
    url = f"{config_data.get('SERVER')}/{config_data.get('FILE_VERSION')}/{config_data.get('VERSION_BOOTFILES')}"
    logger.info(f"Downloading version file from {url}...")
    subprocess.run(["wget", "--progress=bar:force:noscroll", url], check=True)


def env_substitute_file(template_path, output_path):
    with open(template_path, "r") as template_file:
        result = subprocess.run(["envsubst"], stdin=template_file, capture_output=True, text=True, check=True)
    with open(output_path, "w") as output_file:
        output_file.write(result.stdout)


def get_diversions_dir_files(build_dir):
    dirs = []
    files = []
    for dirpath, _, filenames in os.walk(f"{build_dir}/boot/firmware"):
        if dirpath.startswith(f"{build_dir}/"):
            dir = dirpath[len(f"{build_dir}/"):]
        dirs.append(dir)
        files.extend([dir + "/" + filename for filename in filenames])
    return dirs, files


def create_preinst_file(config_data, debian_dir, dirs, files):
    preinst_diversions_str = "\n".join([f"mkdir -p /usr/share/${{PACKAGE_DIVERT}}/{dir}/" for dir in dirs])
    preinst_diversions_str += "\n"
    preinst_diversions_str += "\n".join([f"dpkg-divert --quiet --package ${{PACKAGE_DIVERT}} --rename --divert /usr/share/${{PACKAGE_DIVERT}}/{file} /{file}" for file in files])

    with open(f"{debian_dir}/preinst", "w") as preinst_file:
        preinst_file.write("#!/bin/bash -e\n\n"
                           f"PACKAGE_DIVERT=\"{config_data.get('PACKAGE')}-temp\"\n\n"
                           "# Create diversion directories\n"
                           f"{preinst_diversions_str}\n\n"
                           "sync\n\n"
                           "#DEBHELPER#\n\n"
                           "exit 0\n")

    os.chmod(f"{debian_dir}/preinst", 0o755)


def crate_postinst_file(config_data, debian_dir, files):
    preinst_diversions_str = ""
    for file in files:
        preinst_diversions_str += f"rm -f /{file}\n"
        preinst_diversions_str += f"dpkg-divert --quiet --package ${{PACKAGE_DIVERT}} --rename --remove /{file}\n"

    with open(f"{debian_dir}/postinst", "w") as postinst_file:
        postinst_file.write("#!/bin/bash -e\n\n"
                            f"PACKAGE_DIVERT=\"{config_data.get('PACKAGE')}-temp\"\n\n"
                            "# Remove old files and diversions\n"
                            f"{preinst_diversions_str}\n\n"
                            "rm -rf /usr/share/${PACKAGE_DIVERT}\n"
                            "sync\n\n"
                            "#DEBHELPER#\n\n"
                            "exit 0\n")

    os.chmod(f"{debian_dir}/postinst", 0o755)


def create_debian_files(config_data, build_dir):
    logger.info("Creating Debian packaging files...")
    debian_dir = f"{build_dir}/debian"
    os.makedirs(debian_dir)
    shutil.copyfile("debian-templates/copyright-rpi.in", f"{debian_dir}/copyright")

    env_substitute_file("debian-templates/control.in", f"{debian_dir}/control")
    env_substitute_file("debian-templates/changelog.in", f"{debian_dir}/changelog")

    shutil.copyfile("debian-templates/rules.in", f"{debian_dir}/rules")
    os.chmod(f"{debian_dir}/rules", 0o755)

    with open(f"{debian_dir}/install", "a+") as install_file:
        existing = install_file.read()
        install_file.seek(0)
        install_file.write("boot/firmware/* /boot/firmware\n" + existing)
        install_file.write("lib/modules/* /lib/modules\n")
    os.chmod(f"{debian_dir}/install", 0o644)

    dirs, files = get_diversions_dir_files(build_dir)

    create_preinst_file(config_data, debian_dir, dirs, files)
    crate_postinst_file(config_data, debian_dir, files)


def generate_package_binary(config, version):
    logger.info(f"######################### {config} binary for version {version}")
    config_data = export_env_variable(config)
    os.environ['VERSION'] = version

    build_dir = f"{config_data.get('PACKAGE')}-{version}"
    shutil.rmtree(build_dir) if os.path.exists(build_dir) else None
    os.mkdir(build_dir)

    download_artifacts_rpi(config_data, build_dir)
    create_debian_files(config_data, build_dir)

    os.chdir(build_dir)
    logger.info("Building package...")
    subprocess.run(["dpkg-buildpackage", "-us", "-uc", "-b", f"-a{config_data.get('ARCHITECTURE')}"], check=True)

    os.chdir("..")
    os.remove(f"{config_data.get('PACKAGE')}_{version}-1_{config_data.get('ARCHITECTURE')}.buildinfo")
    os.remove(f"{config_data.get('PACKAGE')}_{version}-1_{config_data.get('ARCHITECTURE')}.changes")

    logger.info(f"Binary package: {config_data.get('PACKAGE')}_{version}-1_{config_data.get('ARCHITECTURE')}.deb")


def main():
    version = datetime.datetime.now().strftime("%d-%m-%Y")
    config = "rpi64"
    generate_package_binary(config, version)
    logger.info(f"Package for {config} generated successfully.")


if __name__ == "__main__":
    main()
