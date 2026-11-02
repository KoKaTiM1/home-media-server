#!/usr/bin/env python3
"""Run Jellyfin plugin regression checks in a pinned, disposable SDK build."""
import argparse
import pathlib
import subprocess
import uuid

SDK = "mcr.microsoft.com/dotnet/sdk@sha256:d9ad72f6b3eb5d61adb158db510ae1c3098c12c92d23ab8da833418275f1c652"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=pathlib.Path, required=True)
    args = parser.parse_args()
    repo = args.repository.resolve()
    context = repo / "plugin-source/dynamic-library"
    dockerfile = context / "Gate.Dockerfile"
    if not dockerfile.is_file():
        parser.error(f"Missing plugin gate Dockerfile: {dockerfile}")

    tag = f"media-plugin-regression:{uuid.uuid4().hex}"
    command = [
        "docker", "build", "--pull=false", "--target", "checks", "--tag", tag,
        "--file", str(dockerfile), str(context),
    ]
    try:
        result = subprocess.run(command, check=False)
        return result.returncode
    finally:
        subprocess.run(["docker", "image", "rm", "--force", tag],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
