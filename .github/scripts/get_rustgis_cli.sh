#!/usr/bin/env bash
# Fetches rustgis-cli, the engine of RustGIS without its window, for Linux x86-64 from a release of
# opengeos/RustGIS and prints the absolute path of the executable (the last line of the output).
#
#   get_rustgis_cli.sh v0.4.0 /some/empty/folder
#
# The release holds the archive RustGIS-<version>-linux-x86_64.tar.gz (the application and rustgis-cli in one folder)
# and a SHA256SUMS file; the archive is checked against it before anything is unpacked, and only rustgis-cli is taken
# out. Needs the gh CLI with GH_TOKEN (set in the workflows), tar and sha256sum (or shasum).
set -euo pipefail

tag="${1:?the release tag, such as v0.4.0}"
dest="${2:?the folder to put rustgis-cli in}"
version="${tag#v}"
archive="RustGIS-${version}-linux-x86_64.tar.gz"

mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
# a transient error of github.com is no news about RustGIS: three tries before the run is red
for attempt in 1 2 3; do
  if gh release download "$tag" -R opengeos/RustGIS -p SHA256SUMS -p "$archive" --dir "$dest" --clobber >&2; then
    break
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "$archive of $tag could not be downloaded after $attempt tries" >&2
    exit 1
  fi
  sleep $((attempt * 10))
done

expected="$(awk -v f="$archive" '$2 == f || $2 == "*" f {print $1}' "$dest/SHA256SUMS")"
if [ -z "$expected" ]; then
  echo "SHA256SUMS of $tag does not list $archive" >&2
  exit 1
fi
if command -v sha256sum >/dev/null; then
  actual="$(sha256sum "$dest/$archive" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$dest/$archive" | awk '{print $1}')"
fi
if [ "$actual" != "$expected" ]; then
  echo "$archive: SHA-256 $actual, but SHA256SUMS says $expected" >&2
  exit 1
fi

# the archive keeps everything in one folder: rustgis-cli is the one entry of that name, whatever the folder is called
member="$(tar -tzf "$dest/$archive" | awk -F/ 'NF == 2 && $2 == "rustgis-cli" {print}' | head -n 1)"
if [ -z "$member" ]; then
  echo "$archive holds no rustgis-cli" >&2
  exit 1
fi
tar -xzf "$dest/$archive" -C "$dest" --strip-components=1 "$member"
rm -f "$dest/$archive"
chmod +x "$dest/rustgis-cli"
echo "$dest/rustgis-cli"
