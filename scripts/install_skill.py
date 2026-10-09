#!/usr/bin/env python3
from pathlib import Path
import os
import shutil

root = Path(__file__).resolve().parents[1]
target = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills/wechat-publisher"
if target.exists():
    raise SystemExit("Skill already exists; compare or back it up before installing: " + str(target))
shutil.copytree(root / "skill/wechat-publisher", target)
entry = target / "scripts/publish.py"
entry.parent.mkdir(parents=True, exist_ok=True)
entry.write_text("import runpy\nrunpy.run_path(" + repr(str(root / "src/wechat_publisher.py")) + ", run_name='__main__')\n", encoding="utf-8")
print("Installed skill: " + str(target))
