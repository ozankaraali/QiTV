"""Fetch AI captions' speech runtime and voice detector into a folder, as a user's first enable
would (services.captions.download, its pinned PyPI wheels and checks), but not the model.

CI runs it before the packaged smoke test, whose captions probe loads them from the folder:

    uv run python scripts/fetch_captions_runtime.py <folder>

It prints the two lines for $GITHUB_ENV: QITV_CAPTIONS_RUNTIME=… and QITV_CAPTIONS_VAD=….
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    from PySide6.QtCore import QCoreApplication, QTimer

    from services.captions import models
    from services.captions.download import CaptionsDownload

    root = Path(argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    model_folder = (root / models.TIERS["small"].folder).resolve()

    def plan(tier: str) -> list:
        """The runtime and the VAD: everything the small tier needs but its model."""
        return [d for d in models.missing(tier, root) if d.folder.resolve() != model_folder]

    app = QCoreApplication([])
    download = CaptionsDownload(root=root, plan=plan)
    outcome: dict = {}
    download.finished.connect(lambda: (outcome.update(ok=True), app.quit()))
    download.failed.connect(lambda reason: (outcome.update(ok=False, reason=reason), app.quit()))
    QTimer.singleShot(0, lambda: download.start("small"))
    QTimer.singleShot(15 * 60 * 1000, app.quit)
    app.exec()
    if not outcome.get("ok"):
        print(f"fetch failed: {outcome.get('reason', 'timed out')}", file=sys.stderr)
        return 1
    runtime = root / models.runtime_folder_name("cp314")
    vad = models.model_paths("small", root).vad
    if not (runtime / "sherpa_onnx").is_dir() or not vad.is_file():
        print(f"fetched, but not where expected: {runtime}, {vad}", file=sys.stderr)
        return 1
    print(f"QITV_CAPTIONS_RUNTIME={runtime}")
    print(f"QITV_CAPTIONS_VAD={vad}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
