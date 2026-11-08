"""Build the installable MCP wheel and the standalone desktop plugin ZIP."""
import argparse
from pathlib import Path
import json
import shutil
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/split-beat-he-plugin"
DIST = ROOT / "dist"
COMPONENTS = (
    "plugin.json",
    "mcp.json",
    "README.md",
    "skills/keyboard-control/SKILL.md",
    "skills/keyboard-control/agents/openai.yaml",
    "skills/keyboard-control/references/workflows.md",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marketplace", action="store_true", help="Refresh the wheel bundled in the repository marketplace")
    args = parser.parse_args()
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    config = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["split_keyboard"]
    wheel_name = Path(server["args"][1]).name
    archive_name = f"{manifest['name']}-{manifest['version']}.zip"
    DIST.mkdir(exist_ok=True)

    with TemporaryDirectory(prefix="split-beat-he-mcp-") as temp:
        staging = Path(temp)
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(staging)],
                       cwd=ROOT, check=True)
        wheel = staging / wheel_name
        if not wheel.is_file():
            raise ValueError(f"Plugin MCP configuration expects {wheel_name}")
        archive = staging / archive_name
        with ZipFile(archive, "w", ZIP_DEFLATED) as package:
            for name in COMPONENTS:
                package.write(PLUGIN / name, name)
            package.write(ROOT / "LICENSE", "LICENSE")
            package.write(wheel, f"server/{wheel.name}")

        if args.marketplace:
            (PLUGIN / "server").mkdir(exist_ok=True)
            shutil.copyfile(wheel, PLUGIN / "server" / wheel.name)

        for artifact in (wheel, archive):
            destination = DIST / artifact.name
            shutil.copyfile(artifact, destination)
            print(destination)


if __name__ == "__main__":
    main()
