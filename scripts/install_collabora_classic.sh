#!/bin/bash
set -e

# Target directory for extraction
TARGET_DIR="/opt/collaboraoffice"
TEMP_DIR=$(mktemp -d -t collabora-XXXXXX)

# Base URL for the Collabora snapshot repo
REPO_URL="https://www.collaboraoffice.com/downloads/Collabora-Office-25-Snapshot/Linux/apt"

# The specific version to download (from current Packages manifest)
VERSION="25.04.11-20260710"

# List of essential packages including Python dependencies
PACKAGES=(
    "collaboraoffice_${VERSION}_amd64.deb"
    "collaboraoffice-ure_${VERSION}_amd64.deb"
    "collaboraofficebasis-core_${VERSION}_amd64.deb"
    "collaboraofficebasis-writer_${VERSION}_amd64.deb"
    "collaboraoffice-writer_${VERSION}_amd64.deb"
    "collaboraofficebasis-calc_${VERSION}_amd64.deb"
    "collaboraoffice-calc_${VERSION}_amd64.deb"
    "collaboraofficebasis-images_${VERSION}_amd64.deb"
    "collaboraofficebasis-en-us_${VERSION}_amd64.deb"
    "collaboraoffice-en-us_${VERSION}_amd64.deb"
    
    # Python Support Components (Required for Python extensions)
    "collaboraofficebasis-pyuno_${VERSION}_amd64.deb"
    "collaboraofficebasis-python-script-provider_${VERSION}_amd64.deb"
)

echo "Creating target directory: $TARGET_DIR"
sudo mkdir -p "$TARGET_DIR"
sudo chown -R $USER:$USER "$TARGET_DIR"

cd "$TEMP_DIR"

for pkg in "${PACKAGES[@]}"; do
    echo "Downloading $pkg..."
    curl -LO "$REPO_URL/$pkg"
    
    echo "Extracting $pkg..."
    # Extract the control and data archives from the .deb package
    ar x "$pkg"
    
    # Extract data contents directly into our target directory
    if [ -f data.tar.xz ]; then
        tar -xf data.tar.xz -C "$TARGET_DIR" --strip-components=2
    elif [ -f data.tar.zst ]; then
        tar --use-compress-program=unzstd -xf data.tar.zst -C "$TARGET_DIR" --strip-components=2
    elif [ -f data.tar.gz ]; then
        tar -xf data.tar.gz -C "$TARGET_DIR" --strip-components=2
    fi
    
    # Clean up temp files for this package
    rm -f control.tar.gz control.tar.xz control.tar.zst data.tar.gz data.tar.xz data.tar.zst debian-binary "$pkg"
done

# Clean up temp directory
cd /
rm -rf "$TEMP_DIR"

echo "Collabora Office Classic has been extracted successfully to $TARGET_DIR"
echo "You can launch it by running: $TARGET_DIR/program/soffice"
