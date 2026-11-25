"""本地 ASR 的独立推理进程（由主进程按需拉起，常驻复用）。

本地引擎（sherpa-onnx / faster-whisper）在 native 层偶发死锁，死锁时主进程里那几个
线程会永远卡住，Python 没有安全的办法杀掉一个卡在 C 扩展里的线程 —— 转写队列就此停摆。
把推理搬进独立进程后，主进程只发请求、等结果：超时就 kill 整个 worker，
下次调用自动重新拉起（代价是重新加载一次模型，通常几秒）。

协议（stdin/stdout 各一行 JSON）：
  转写 {"provider": {...}, "file": "...", "mime": "...", "language": "...", "durationMs": 123}
  分离 {"task": "diarize", "file": "...", "models": {...}, "threshold": 0.7}
  响应 {"ok": true, "result": {...}} / {"ok": false, "error": "..."}

启动：python -m app.providers.local_asr_worker（cwd = server-py）
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def main() -> int:
    # 推理就在本进程里做，不要再套一层子进程
    os.environ["LOCAL_ASR_ISOLATE"] = "0"

    from .asr import create_asr_engine

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("task") == "diarize":
                from .diarization import diarize_file

                result = diarize_file(req)
            else:
                from .asr import create_asr_engine

                engine = create_asr_engine(req.get("provider") or {})
                result = engine.transcribe(
                    req.get("file"),
                    req.get("mime"),
                    req.get("language"),
                    req.get("durationMs"),
                )
            out: dict = {"ok": True, "result": result}
        except Exception as e:  # noqa: BLE001
            text = traceback.format_exc()
            traceback.print_exc()
            try:
                from ..config import DATA_DIR

                log = DATA_DIR / "logs" / "local-asr-worker.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception:  # noqa: BLE001
                pass
            out = {"ok": False, "error": f"{e}"[:500]}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
